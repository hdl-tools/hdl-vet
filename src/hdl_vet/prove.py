"""The equivalence check at sign-off: what a logic operation changed, proved with `hdl_vet.equiv`.

A logic operation (`Module.override_net`) leaves a record that names the module, the net, the enable and the
waiver. A raw splice (`Vet.splice`) inside a module is one too, of the kind "splice": it has no net and no enable,
and its label is `splice <module>`. A logic call is one of the kind "logic", with the label `add_assign`, `connect`,
`set_param` or `replace_module` and the name of the module. `prove` makes one comparison for each module that such
an operation edited and for each set of parameter values the module is elaborated with. The gold side is the module
of the input design, the gate side is the edited design. The engine reads files, so both sides are written to a
temporary folder that keeps the layout of the real tree (an `include` resolves as in the real tree), and the folder
is removed after.

The tie: every enable of an override in the module, and every enable that it carries up from a child, is tied
to 0. The `same` list holds the children of the module whose own text, and the text of every module below them,
is byte-identical in the two designs and that are not the subject of a logic operation. The `inert` map holds
the modules that this run has proved on their own, with their enables: the comparisons go from the leaves
upward, and a module whose records are all "proved" is a black box in the comparisons of the modules above it.
The engine checks that each of its enable pins is the constant 0. See `tests/ops/test_override.py` and
`tests/ops/test_override_levels.py` for the rules. `hdl_vet.enables` follows each enable up to the top.

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
from hdl_vet.enables import Enable, follow
from hdl_vet.slang_adapter import Design

if TYPE_CHECKING:
    from hdl_vet.session import Session

__all__ = ["LogicOp", "Outside", "Proof", "Record", "prove"]

_STATUSES = ("proved", "refuted", "error")


@dataclass(frozen=True)
class LogicOp:
    """One logic operation as the sign-off needs it."""

    module: str
    net: str
    enable: str
    waiver: str | None = None
    kind: str = "override_net"  # "override_net" | "splice" | "logic"
    name: str = ""  # the label of a "logic" call, for example `set_param core.u_sync.STAGES`

    @property
    def label(self) -> str:
        if self.kind == "logic":
            return self.name
        if self.kind == "splice":
            return f"splice {self.module}"
        return f"override_net {self.module}.{self.net}"


@dataclass(frozen=True)
class Outside:
    """A raw splice that changes text outside every module: it has a waiver and no comparison is run for it."""

    file: str  # the path in the design, with slashes
    waiver: str

    @property
    def label(self) -> str:
        return f"splice {self.file}"

    def line(self) -> str:
        return f"  waived: {self.label}: {json.dumps(self.waiver)} (outside the modules: not compared)"

    def manifest(self) -> dict[str, Any]:
        return {"operation": self.label, "waiver": self.waiver, "check": None}


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
            if not self.enables:  # a raw splice with no enable: the module is equal as it is
                out.append(f"  equivalence proved: {self.label} ({ops})")
            else:
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


@dataclass(frozen=True)
class Proof:
    """What the sign-off found: the records of the comparisons, and the way of each enable to the top."""

    records: list[Record] = field(default_factory=list)
    enables: list[Enable] = field(default_factory=list)
    outside: list[Outside] = field(default_factory=list)


def prove(session: Session, design: Design, ops: Sequence[LogicOp], outside: Sequence[Outside] = ()) -> Proof:
    """One record for each module of `ops` and each set of parameter values of it, and the way of the enables.

    The modules are compared from the leaves upward. The records are in the order of the first operation of each
    module. An exception of the engine is an answer ("error") of the record, not an escape. `outside` are the raw
    splices outside the modules: no comparison is run for them.
    """
    spared = list(dict.fromkeys(outside))
    if not ops:
        return Proof(outside=spared)
    by_module: dict[str, list[LogicOp]] = {}
    for op in ops:
        by_module.setdefault(op.module, []).append(op)
    ways = follow(session, design, [op for op in ops if op.kind == "override_net"])
    logic = set(by_module) | _moved_modules(session, design)
    below = {module: _below(design, module) for module in by_module}
    reads = _read_from_outside(session, design, list(by_module))
    order = sorted(by_module, key=lambda module: len(below[module] & set(by_module)))  # stable: ties keep script order
    done: dict[str, list[Record]] = {}
    inert: dict[str, list[str]] = {}
    with tempfile.TemporaryDirectory(prefix="hdl_vet_proof_", ignore_cleanup_errors=True) as folder:
        trees = _Trees(session, Path(folder))
        memo: dict[str, bool] = {}
        for module in order:
            own = list(dict.fromkeys(op.enable for op in by_module[module] if op.kind == "override_net"))
            ties = list(dict.fromkeys([*own, *ways.carried.get(module, [])]))
            use = {name: pins for name, pins in inert.items() if name in below[module]}
            moved = _moved_params(session, design, module)
            done[module] = [
                _judge(session, design, module, params, by_module[module], trees, logic, memo, ties, use, reads, moved)
                for params in _param_sets(design, module)
            ]
            if ties and all(record.status == "proved" for record in done[module]):
                inert[module] = ties  # a module with no enable is not inert: its parent is compared with it inside
    return Proof([record for module in by_module for record in done[module]], ways.records, spared)


def _children(design: Design, module: str) -> set[str]:
    try:
        return {inst.module for inst in design.module(module).instances}
    except Exception:
        return set()  # a module that the reader does not know holds nothing that is looked at


def _below(design: Design, module: str) -> set[str]:
    """The modules that an instance of `module` holds, at any depth (not `module` itself, unless it holds itself)."""
    found: set[str] = set()
    todo = [module]
    while todo:
        children = _children(design, todo.pop())
        todo += sorted(children - found)
        found |= children
    return found


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
    enables: list[str],
    inert: dict[str, list[str]],
    reads: dict[str, str],
    moved: dict[str, str],
) -> Record:
    labels = list(dict.fromkeys(op.label for op in ops))  # a module with several raw splices has one label
    waivers = list(dict.fromkeys((op.label, op.waiver) for op in ops if op.waiver is not None))
    waiver = "; ".join(dict.fromkeys(text for _, text in waivers)) or None
    everything_waived = all(op.waiver is not None for op in ops)
    if everything_waived and not equiv.available():
        return Record(module, params, labels, "waived", "", waiver, None, enables, waivers)
    try:
        wrong = _other_types(design, module)
    except Exception:
        wrong = "the type parameters of the module could not be read"
    wrong = wrong or reads.get(module) or moved.get(_key(params)) or moved.get("*")
    if wrong is not None:  # the proof is at the values of the value parameters only: never proved, never inert
        status = "waived" if everything_waived else "error"
        return Record(module, params, labels, status, wrong, waiver, "error", enables, waivers)
    status, detail, seen = "error", "", False
    try:
        gold, gate = trees.sources()
        same = _same_children(session, design, module, logic, memo)
        used = inert
        answer = equiv.check(gold, gate, module, params=params, tie=dict.fromkeys(enables, 0), same=same, inert=used)
        if answer.inert_refused:  # only the interface of a child was refused: compare with the children inside
            used = {}
            answer = equiv.check(gold, gate, module, params=params, tie=dict.fromkeys(enables, 0), same=same)
        status, detail, seen = answer.status, trees.clean(answer.detail), answer.undefined_seen
    except Exception as exc:
        status, detail = "error", f"the check could not be run: {type(exc).__name__}: {exc}"
        used = {}
    # The gold can leave a value undefined, so the gate may "choose" it: equal by that freedom only. It is no
    # proof when the value can be seen from outside the module (a port, or a pin of a child kept as a box). The
    # engine decides that from the netlist of the gold (`equiv._leads_outside`), not from names.
    if status == "proved" and seen:
        status, detail = "error", f"not proved, because the input design is undefined at a port: {detail}"
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


def _read_from_outside(session: Session, design: Design, modules: Sequence[str]) -> dict[str, str]:
    """For each module that is compared: the first hierarchical name written outside it that leads into one of its
    instances, as text, in the edited design or in the input design.

    The comparison is of one module with its own inputs and outputs. A name from outside reads what is inside
    (a net, a variable, a function), which the comparison does not see: such a module is never proved.
    A name written inside the module is not this case, nor is a name from inside it into its own children.
    """
    found: dict[str, str] = {}
    try:
        for known in dict.fromkeys([design, session.baseline]):
            owner: dict[str, list[str]] = {}
            for path, snap in known.snapshot().items():
                if snap.module in modules:
                    owner.setdefault(path, []).append(snap.module)
            if not owner:
                continue
            _bound_from_outside(known, owner, modules, found)
            for ref in known.hierarchical_refs():
                parts = ref.target.split(".")
                for end in range(1, len(parts) + 1):
                    path = ".".join(parts[:end])
                    if path in owner and not (ref.host == path or ref.host.startswith(path + ".")):
                        for module in owner[path]:
                            found.setdefault(module, f"{ref.host} reads {ref.target}, inside an instance of {module}")
    except Exception:
        return dict.fromkeys(modules, "the hierarchical names of the design could not be listed")
    return found


def _bound_from_outside(
    known: Design, owner: dict[str, list[str]], modules: Sequence[str], found: dict[str, str]
) -> None:
    """Add to `found` each module that holds an instance which a `bind` written outside its text put there.

    The comparison reads one module alone (`read_slang --top M`), and a bind of another module is not in it.
    `Design.binds` gives what the compiler resolved: the instances that a bind built, with the path of the
    instance that holds each. A bind at file level counts too. Where the compiler cannot say, the caller makes
    every compared module "error".
    """
    for bind in known.binds():
        where = f"module {bind.host}" if bind.host else "a file"
        note = f"a bind written in {where} puts an instance into {bind.target}: {bind.text[:60]}"
        parts = bind.target.split(".")
        for end in range(1, len(parts) + 1):
            for module in owner.get(".".join(parts[:end]), []):
                if bind.host != module:
                    found.setdefault(module, note)


def _other_types(design: Design, module: str) -> str | None:
    """The complaint when an instance sets a type parameter of `module` to another type than its default.

    The comparison is at the values of the value parameters. A type parameter is compared at its default
    only, so a module with another type is not proved. A type is the default when the two texts are the
    same without blanks.
    """
    info = design.module(module)
    for name, text in design.overridden_types(module):
        default = next((p.default for p in info.params if p.name == name), None)
        if default is None or "".join(default.split()) != "".join(text.split()):
            return (
                f"the type parameter {name} is set to the type {text}, not to its default: the check does not cover it"
            )
    return None


def _key(params: dict[str, str]) -> str:
    return ",".join(f"{name}={value}" for name, value in sorted(params.items()))


def _moved_params(session: Session, design: Design, module: str) -> dict[str, str]:
    """For each set of parameter values of `module` in the edited design: the first parameter whose value at an
    instance differs from the one that the input design gives the same instance, as text; "*" when it cannot be told.

    The engine compares the two sides at one value of each parameter. A value that the edit changed (a default that
    a raw splice changed, or an override that `set_param` changed) is a change of the design that the comparison of the
    module does not cover, so the record of that set is "error", never "proved".
    """
    found: dict[str, str] = {}
    try:
        names = [name for name, kind in design.module(module).param_kinds.items() if kind == "value"]
        before = session.baseline_snapshot()
        for path, snap in sorted(design.snapshot().items()):
            old = before.get(path)
            if snap.module != module or old is None or old.module != module:
                continue
            values = {name: snap.params[name] for name in names if name in snap.params}
            for name in names:
                if name in old.params and name in snap.params and old.params[name] != snap.params[name]:
                    found.setdefault(
                        _key(values),
                        f"the parameter {name} of {module} is {old.params[name]} in the input design and "
                        f"{snap.params[name]} in the edited design at {path}: one value is used for both sides",
                    )
    except Exception:
        return {"*": "the parameter values of the two designs could not be compared"}
    return found


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
        # Any other spelling of the folder, such as the mount point of the Yosys runtime on POSIX.
        other = r"[^\s'\"()]*" + re.escape(self._folder.name) + r"(?:[/\\](?:gold|gate))?[/\\]?"
        return re.sub(other, "", found, flags=re.IGNORECASE)

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
