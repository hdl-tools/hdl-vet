"""Sign-off comparisons: what is new in the diagnostics, what changed structurally.

Pure Python; the data types come from `hdl_vet.slang_adapter`, the offset maps
from `hdl_vet.splice`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from hdl_vet.slang_adapter import Diag, InstanceSnap
from hdl_vet.splice import OffsetMap

__all__ = ["Change", "Expect", "check_changes", "diff_snapshots", "new_diagnostics"]


# --- diagnostics ------------------------------------------------------------------


_Loc = tuple[str, Path | None, int | None]


def _key_after(d: Diag, maps: Mapping[Path, Sequence[OffsetMap]]) -> tuple[_Loc, bool]:
    """Canonical location of an `after` diagnostic, and whether edited text holds it."""
    off = d.offset
    touched = False
    if off is not None and d.path is not None and d.path in maps:
        for m in reversed(maps[d.path]):
            touched = touched or m.in_new_text(off)
            off = m.backward(off)
    return (d.code, d.path, off), touched


def _key_before(d: Diag, maps: Mapping[Path, Sequence[OffsetMap]]) -> tuple[_Loc, bool]:
    """Canonical location of a baseline diagnostic, and whether an edit replaced it."""
    off = d.offset
    touched = False
    if off is not None and d.path is not None and d.path in maps:
        stages = maps[d.path]
        for m in stages:
            touched = touched or m.replaces(off)
            off = m.forward(off)
        for m in reversed(stages):
            off = m.backward(off)
    return (d.code, d.path, off), touched


def _sort_key(d: Diag) -> tuple[bool, str, int, str]:
    return (d.path is None, "" if d.path is None else str(d.path), -1 if d.offset is None else d.offset, d.code)


def new_diagnostics(
    before: Sequence[Diag], after: Sequence[Diag], maps: Mapping[Path, Sequence[OffsetMap]]
) -> list[Diag]:
    """The `after` diagnostics in excess of the baseline, compared by location.

    Two passes, each consuming one baseline diagnostic per match: exact (location
    and message), then location only, which is allowed only between diagnostics
    that no edit touched and that do not come from a macro expansion.
    """
    # location -> baseline diagnostics there: [message, loose (untouched, not macro)]
    pool: dict[_Loc, list[list[object]]] = {}
    for d in before:
        if d.severity == "note":
            continue
        key, touched = _key_before(d, maps)
        pool.setdefault(key, []).append([d.message, not touched and not d.in_macro])
    pending: list[tuple[Diag, _Loc, bool]] = []
    for d in after:
        if d.severity == "note":
            continue
        key, touched = _key_after(d, maps)
        cands = pool.get(key, [])
        for i, c in enumerate(cands):
            if c[0] == d.message:
                del cands[i]
                break
        else:
            pending.append((d, key, not touched and not d.in_macro))
    out: list[Diag] = []
    for d, key, loose in pending:
        cands = pool.get(key, [])
        if loose:
            for i, c in enumerate(cands):
                if c[1]:
                    del cands[i]
                    break
            else:
                out.append(d)
        else:
            out.append(d)
    out.sort(key=_sort_key)
    return out


# --- structure --------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    kind: str
    path: str
    name: str
    before: str | None
    after: str | None


@dataclass(frozen=True)
class Expect:
    kind: str
    path: str = "*"
    name: str = "*"
    required: bool = True
    exact: bool = False  # `path` is a hierarchical path, not a glob pattern
    below: bool = False  # matches every path strictly below `path`



def _port(p: tuple[str, int] | None) -> str | None:
    return None if p is None else f"{p[0]} {p[1]}"


def diff_snapshots(before: Mapping[str, InstanceSnap], after: Mapping[str, InstanceSnap]) -> list[Change]:
    out: list[Change] = []
    for path in sorted(set(before) | set(after)):
        a, b = before.get(path), after.get(path)
        if a is None:
            assert b is not None  # noqa: S101 - the path is in one of the two, and not in `before`
            out.append(Change("instance-added", path, "", None, b.module))
            continue
        if b is None:
            out.append(Change("instance-removed", path, "", a.module, None))
            continue
        if a.module != b.module:
            out.append(Change("module-changed", path, "", a.module, b.module))
        for n in set(a.params) | set(b.params):
            if a.params.get(n) != b.params.get(n):
                out.append(Change("param-changed", path, n, a.params.get(n), b.params.get(n)))
        for n in set(a.ports) | set(b.ports):
            x, y = a.ports.get(n), b.ports.get(n)
            if x != y:
                kind = "port-added" if x is None else "port-removed" if y is None else "port-changed"
                out.append(Change(kind, path, n, _port(x), _port(y)))
            elif x is not None:
                # same direction and width, different type (e.g. an unpacked dimension)
                ta, tb = a.port_types.get(n), b.port_types.get(n)
                if ta is not None and tb is not None and ta != tb:
                    out.append(Change("port-type-changed", path, n, ta, tb))
        for n in set(a.connections) | set(b.connections):
            if a.connections.get(n) != b.connections.get(n):
                out.append(Change("connection-changed", path, n, a.connections.get(n), b.connections.get(n)))
    out.sort(key=lambda c: (c.path, c.kind, c.name))
    return out


def _matches(e: Expect, c: Change) -> bool:
    if e.below:
        where = c.path.startswith(e.path + ".")
    elif e.exact:
        where = c.path == e.path
    else:
        where = fnmatchcase(c.path, e.path)
    return where and fnmatchcase(c.kind, e.kind) and fnmatchcase(c.name, e.name)


def check_changes(
    changes: Sequence[Change], expects: Sequence[Expect]
) -> tuple[list[Change], list[Expect]]:
    """(changes no expectation covers, expectations that matched nothing)."""
    unexpected = [c for c in changes if not any(_matches(e, c) for e in expects)]
    unmatched = [e for e in expects if e.required and not any(_matches(e, c) for c in changes)]
    return unexpected, unmatched
