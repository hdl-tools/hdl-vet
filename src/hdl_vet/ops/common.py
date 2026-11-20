"""Errors and the records the operations share."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class VetError(Exception):
    """A request that cannot be carried out."""


class DriftError(VetError):
    """The design is in neither the "before" nor the "after" state the operation knows."""


@dataclass(frozen=True)
class OpRecord:
    op: str
    target: str
    status: str  # "applied" | "skipped"


@dataclass(frozen=True)
class _Check:
    """A post-condition as plain data: `kind` names its evaluator, `args` are the strings it needs.

    `key` is the target: a later check with the same key replaces the earlier one. Nothing here
    is a closure, so a session can be saved with its checks and evaluate them after a restore.
    """

    key: tuple[str, ...]
    kind: str
    args: tuple[str | None, ...]


@dataclass
class _Plan:
    """What one underlying operation will do; `_commit` queues it and registers the rest."""

    op: str
    target: str
    skip: bool = False
    path: Path | None = None
    splices: list[tuple[int, int, bytes]] = field(default_factory=list)
    mark: tuple[str, ...] | None = None
    added: tuple[str, str, tuple[str, str]] | None = None  # (module, name, (kind, text))
    expects: list[tuple[str, str, str]] = field(default_factory=list)
    permits: list[tuple[str, str, str]] = field(default_factory=list)
    check: _Check | None = None
    # The inverses, for an operation that undoes one of this run (see `_commit`). They are
    # data on the plan, not something the operation does while it looks at the design.
    #
    # The rule a checked body has to keep is narrower than "only read", which this comment
    # used to say. A body runs inside `Vet._begin` / `Vet._rollback`, which take back the
    # text, the queue, the stages, the expectations, the added files, the loaded design,
    # `log`, `notes`, `_checks`, `_swapped` and the tail of `_drove`. A mutation of any of
    # those is safe, because a refusal or a rolled-back group restores it; `remove_instance`
    # relies on that for `drop_expects`, `_drop_registered` and `_drove` (#95, checked, not
    # reachable). Anything the checkpoint does NOT hold must go on the plan instead, and so
    # must a decision that the body would otherwise make twice.
    cancels: list[tuple[str, str, str]] = field(default_factory=list)  # expectations to forget
    quiet: bool = False  # part of an operation whose first plan is logged


@dataclass(frozen=True)
class _VetState:
    """What a rollback of the Vet restores besides the session (see `Vet._begin`)."""

    session: object
    log: int
    notes: int
    checks: list[_Check]
    swapped: dict[tuple[str, str], set[str]]
    drove: int
