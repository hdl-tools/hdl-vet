"""The equivalence check at sign-off: what a logic operation changed, proved with `hdl_vet.equiv`.

A logic operation (`Module.override_net`) leaves a record that names the module, the net, the enable and the
waiver. `prove` makes one comparison for each module that such an operation edited and for each set of
parameter values the module is elaborated with. The gold side is the module of the input design, the gate
side is the edited design. The engine reads files, so both sides are written to a temporary folder that keeps
the layout of the real tree (an `include` resolves as in the real tree), and the folder is removed after.

The tie: every enable of an override in the module is tied to 0. The `same` list holds the children of the
module whose own text, and the text of every module below them, is byte-identical in the two designs and
that are not the subject of a logic operation. See `tests/ops/test_override.py` for the rules.

This module does not import pyslang; it talks to `hdl_vet.slang_adapter`, `hdl_vet.equiv` and the session.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hdl_vet import equiv
from hdl_vet.slang_adapter import Design

if TYPE_CHECKING:
    from hdl_vet.session import Session

__all__ = ["LogicOp", "Record", "prove"]

_STATUSES = ("proved", "refuted", "error")


@dataclass(frozen=True)
class LogicOp:
    """One logic operation as the sign-off needs it."""

    module: str
    net: str
    enable: str
    waiver: str | None = None

    @property
    def label(self) -> str:
        return f"override_net {self.module}.{self.net}"


@dataclass(frozen=True)
class Record:
    """The sign-off record of one comparison.

    `status` is "proved", "refuted", "error" or "waived". `check` is what the engine answered, or None
    when it did not run. `waivers` are the (operation, text) pairs of the operations that carry a waiver.
    """

    module: str
    params: dict[str, str]
    operations: list[str]
    status: str
    detail: str
    waiver: str | None = None
    check: str | None = None
    enables: list[str] = field(default_factory=list)
    waivers: list[tuple[str, str]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status in ("proved", "waived")

    @property
    def label(self) -> str:
        if not self.params:
            return self.module
        return f"{self.module} [{', '.join(f'{k}={v}' for k, v in self.params.items())}]"

    def _says(self) -> str:
        if self.check is None:
            return "the check did not run: Yosys is missing"
        if self.check == "proved":
            return "the check says: proved"
        return f"the check says: {self.check}, {self.detail}"

    def lines(self) -> list[str]:
        """The lines of the report for this record."""
        ops = ", ".join(self.operations)
        out: list[str] = []
        if self.status == "proved":
            zero = f"{self.enables[0]} is 0" if len(self.enables) == 1 else f"{' and '.join(self.enables)} are 0"
            out.append(f"  equivalence proved: {self.label} ({ops}; equal when {zero})")
        elif self.status == "refuted":
            out.append(f"  not equivalent: {self.label}: {self.detail} ({ops})")
        elif self.status == "error":
            out.append(f"  equivalence not checked: {self.label}: {self.detail} ({ops})")
        out.extend(f"  waived: {op}: {json.dumps(text)} ({self._says()})" for op, text in self.waivers)
        return out

    def manifest(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "params": dict(self.params),
            "operations": list(self.operations),
            "status": self.status,
            "detail": self.detail,
        }


def prove(session: Session, design: Design, ops: Sequence[LogicOp]) -> list[Record]:
    """One record for each module of `ops` and each set of parameter values of it. Never raises."""
    if not ops:
        return []
    by_module: dict[str, list[LogicOp]] = {}
    for op in ops:
        by_module.setdefault(op.module, []).append(op)
    logic = set(by_module) | _moved_modules(session, design)
    records: list[Record] = []
    with tempfile.TemporaryDirectory(prefix="hdl_vet_proof_", ignore_cleanup_errors=True) as folder:
        trees = _Trees(session, Path(folder))
        memo: dict[str, bool] = {}
        for module, module_ops in by_module.items():
            for params in _param_sets(design, module):
                records.append(_judge(session, design, module, params, module_ops, trees, logic, memo))
    return records


# --- one comparison --------------------------------------------------------------------------------


def _judge(
    session: Session,
    design: Design,
    module: str,
    params: dict[str, str],
    ops: Sequence[LogicOp],
    trees: _Trees,
    logic: set[str],
    memo: dict[str, bool],
) -> Record:
    labels = [op.label for op in ops]
    enables = list(dict.fromkeys(op.enable for op in ops))
    waivers = [(op.label, op.waiver) for op in ops if op.waiver is not None]
    waiver = "; ".join(text for _, text in waivers) or None
    everything_waived = len(waivers) == len(ops)
    if everything_waived and not equiv.available():
        return Record(module, params, labels, "waived", "", waiver, None, enables, waivers)
    status, detail = "error", ""
    try:
        gold, gate = trees.sources()
        same = _same_children(session, design, module, logic, memo)
        answer = equiv.check(gold, gate, module, params=params, tie=dict.fromkeys(enables, 0), same=same)
        status, detail = answer.status, trees.clean(answer.detail)
    except Exception as exc:
        status, detail = "error", f"the check could not be run: {type(exc).__name__}: {exc}"
    if status not in _STATUSES:  # only the engine's three answers count
        status, detail = "error", f"the engine gave an answer that is not known: {status!r}"
    return Record(
        module,
        params,
        labels,
        "waived" if everything_waived else status,
        detail,
        waiver,
        status,
        enables,
        waivers,
    )


def _param_sets(design: Design, module: str) -> list[dict[str, str]]:
    """The sets of values of the overridable parameters the module is elaborated with, as text.

    Every value is given: the snapshot lists the parameters with their default as well. Nothing but
    value parameters that an instance can override is named, as the engine needs.
    """
    try:
        kinds = design.module(module).param_kinds
        snapshot = design.snapshot()
    except Exception:
        return [{}]
    names = [name for name, kind in kinds.items() if kind == "value"]
    found: dict[tuple[tuple[str, str], ...], dict[str, str]] = {}
    for path in sorted(snapshot):
        snap = snapshot[path]
        if snap.module != module:
            continue
        values = {name: snap.params[name] for name in names if name in snap.params}
        found.setdefault(tuple(sorted(values.items())), values)
    return [found[key] for key in sorted(found)] or [{}]


# --- `same`: the children that did not change -----------------------------------------------------


def _moved_modules(session: Session, design: Design) -> set[str]:
    """The modules of an instance whose parameter values or module changed, and of every instance above it.

    A child of such a module is not the same child, whatever its text says.
    """
    try:
        old, new = session.baseline_snapshot(), design.snapshot()
        found: set[str] = set()
        for path in set(old) | set(new):
            before, after = old.get(path), new.get(path)
            if (
                before is not None
                and after is not None
                and before.params == after.params
                and before.module == after.module
            ):
                continue
            parts = path.split(".")
            for end in range(1, len(parts) + 1):
                for snap in (old, new):
                    here = snap.get(".".join(parts[:end]))
                    if here is not None:
                        found.add(here.module)
        return found
    except Exception:
        return set(design.modules())  # a doubt costs time, never a wrong proof


def _same_children(session: Session, design: Design, module: str, logic: set[str], memo: dict[str, bool]) -> list[str]:
    """The modules of the instances of `module` that are the same in the two designs, with all below them."""
    try:
        sources = set(session.filelist.files)
        if any(session.text(p) != session.original(p) for p in session.files if p not in sources):
            return []  # a header changed: any module may read it
        if any(_outside_changed(session, design, p) for p in session.files if p in sources):
            return []  # a package or an item of the compilation unit changed: a child may read it
        children = {inst.module for inst in design.module(module).instances}
        return sorted(c for c in children if _identical(session, design, c, logic, memo, ()))
    except Exception:
        return []  # a doubt costs time (the engine looks into the child), never a wrong proof


def _outside_changed(session: Session, design: Design, path: Path) -> bool:
    """True when the text of the file outside its modules is not the same in the two designs.

    The header line that the sign-off adds does not count. When it cannot be told, the answer is True.
    """
    before, after = session.original(path), session.text(path)
    if before == after:
        return False
    try:
        rest = _without_modules(design, path, after)  # the offsets of the design count the header line
        rest = re.sub(rb"^// Modified by hdl-vet[^\r\n]*(\r\n|\n)", b"", rest, count=1, flags=re.MULTILINE)
        return _without_modules(session.baseline, path, before) != rest
    except Exception:
        return True


def _without_modules(design: Design, path: Path, data: bytes) -> bytes:
    """`data` with the text of every module, interface and program defined in `path` taken out."""
    spans = sorted((info.start, info.end) for info in (design.module(n) for n in design.defined_in(path)))
    out, at = [], 0
    for start, end in spans:
        out.append(data[at:start])
        at = end
    out.append(data[at:])
    return b"".join(out)


def _identical(
    session: Session, design: Design, name: str, logic: set[str], memo: dict[str, bool], above: tuple[str, ...]
) -> bool:
    if name in memo:
        return memo[name]
    if name in above:
        return True
    memo[name] = False
    if name in logic:
        return False
    try:
        new, old = design.module(name), session.baseline.module(name)
    except Exception:
        return False
    before = session.original(old.path)[old.start : old.end]
    after = session.text(new.path)[new.start : new.end]
    if before != after:
        return False
    ok = all(_identical(session, design, inst.module, logic, memo, (*above, name)) for inst in new.instances)
    memo[name] = ok
    return ok


# --- the two trees ---------------------------------------------------------------------------------


class _Trees:
    """The input design and the edited design as files, in the layout of the real tree."""

    def __init__(self, session: Session, folder: Path) -> None:
        self._session = session
        self._folder = folder
        self._pair: tuple[equiv.Source, equiv.Source] | None = None

    def clean(self, text: str) -> str:
        """`text` with the path of the temporary folder taken out: a file is named by its path in the design."""
        found = text
        for name in ("gold", "gate"):
            for form in self._forms(self._folder / name):
                found = re.sub(re.escape(form) + r"[/\\]?", "", found, flags=re.IGNORECASE)
        for form in self._forms(self._folder):
            found = re.sub(re.escape(form) + r"[/\\]?", "", found, flags=re.IGNORECASE)
        return found

    @staticmethod
    def _forms(path: Path) -> list[str]:
        forms = {path.as_posix(), str(path), path.resolve().as_posix(), str(path.resolve())}
        return sorted(forms, key=len, reverse=True)

    def sources(self) -> tuple[equiv.Source, equiv.Source]:
        if self._pair is None:
            self._pair = (self._write("gold", False), self._write("gate", True))
        return self._pair

    def _write(self, name: str, edited: bool) -> equiv.Source:
        session = self._session
        base = self._folder / name
        root = session.layout_root
        listing = session.filelist
        for path in session.files:
            dest = base / path.relative_to(root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(session.text(path) if edited else session.original(path))
        files = [base / path.relative_to(root) for path in listing.files]
        if edited:
            for out, data in session.added.items():
                dest = base / out
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                files.append(dest)
        incdirs = [base / path.relative_to(root) for path in listing.incdirs]
        for folder in incdirs:
            folder.mkdir(parents=True, exist_ok=True)
        return equiv.Source(files, incdirs, dict(listing.defines))
