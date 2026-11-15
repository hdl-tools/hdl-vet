"""The script API: `Vet` and the structural operations scripts are made of.

Every operation follows one contract: if the design already shows exactly the
requested result nothing is queued and the operation is logged "skipped"; if it is
in the expected "before" state the edit is queued (guarded with `old=`), its
structural expectations are registered and a post-condition is registered that
`finish` evaluates on the final design; otherwise `DriftError`. See
`tests/test_ops.py` for the full specification.

Phase 5 adds the *checked* operations (`add_file`, `replace_module`, `remove_*`):
each is tried on the design before it returns and taken back, with everything it did,
when the edited design has a new diagnostic or leaves something dangling; `Vet.group`
makes the same check once for a block of operations. See `tests/test_instances.py`.

Handles are live: each operation and each model attribute looks the design up
again. A re-load happens only when the lookup depends on something this `Vet` has
edited since the last load (a module's port list, parameter list or declarations;
an instance's connection or override list), on anything queued for `add_assign`,
or on anything at all after a raw `splice`.

This module does not import pyslang; it talks to `hdl_vet.session` and the
data types of `hdl_vet.slang_adapter`.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from functools import partial
from pathlib import Path
from typing import Any

from hdl_vet.archive import (
    ArchiveError,
    need_dict,
    need_list,
    need_str,
    read_archive,
    write_archive,
)
from hdl_vet.listedit import append_items, edit_list, line_rule, remove_all
from hdl_vet.session import Result, Session, SessionError, describe
from hdl_vet.slang_adapter import (
    AdapterError,
    ConnInfo,
    Design,
    InstanceInfo,
    ModuleInfo,
    ParamOverride,
    driven_bases,
    is_simple_identifier,
    tokens,
)
from hdl_vet.splice import SpliceError

__all__ = ["DriftError", "Instance", "Module", "OpRecord", "Vet", "VetError"]

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_DIRECTIONS = ("input", "output", "inout")
_BASED = re.compile(r"'[sS]?[bBoOdDhH]")
_DIGITS = re.compile(r"[0-9a-fA-FxXzZ?_]+")
_NET_KEYWORDS = frozenset(
    [
        "wire", "tri", "tri0", "tri1", "triand", "trior", "trireg",
        "wand", "wor", "uwire", "supply0", "supply1", "interconnect",
    ]
)


class VetError(Exception):
    """A request that cannot be carried out."""


class DriftError(VetError):
    """The design is in neither the "before" nor the "after" state the operation knows."""


@dataclass(frozen=True)
class OpRecord:
    op: str
    target: str
    status: str  # "applied" | "skipped"


# --- small helpers -----------------------------------------------------------------


def _same(a: object, b: object) -> bool:
    """True when both texts have the same tokens (whitespace and comments do not count)."""
    return tokens(a) == tokens(b)


def _norm(text: object) -> str:
    """A string that is equal for texts with the same tokens."""
    return "\x00".join(tokens(text))


def _refuse_directive(op: str, what: str, directive: str | None) -> None:
    if directive is not None:
        raise VetError(
            f"{op}: the {what} list contains a {directive} conditional directive; sign-off only sees the "
            f"current defines, so the other configuration cannot be checked. Edit it by hand or with a raw splice."
        )


def _header_line_in_comment(data: bytes, start: int, end: int) -> bool:
    """True when the text from `start` to `end` opens a block comment it does not close."""
    line = data[start:end]
    i = 0
    while i < len(line) - 1:
        pair = line[i : i + 2]
        if pair == b"//":
            return False
        if pair == b"/*":
            close = line.find(b"*/", i + 2)
            if close < 0:
                return True
            i = close + 2
            continue
        i += 1
    return False


def _eol(data: bytes) -> bytes:
    """The file's line ending: CRLF if its first break is CRLF, else LF."""
    i = data.find(b"\n")
    return b"\r\n" if i > 0 and data[i - 1 : i] == b"\r" else b"\n"


def _did_you_mean(name: str, candidates: Iterable[str]) -> str:
    close = difflib.get_close_matches(name, sorted(set(candidates)), n=3)
    quoted = [repr(c) for c in close]
    if not quoted:
        return ""
    if len(quoted) == 1:
        return f"; did you mean {quoted[0]}?"
    return f"; did you mean {', '.join(quoted[:-1])} or {quoted[-1]}?"


def _range(width: int | str | None) -> str:
    if width is None:
        return ""
    if isinstance(width, int):
        if width < 1:
            raise VetError(f"width must be at least 1, not {width}")
        return "" if width == 1 else f" [{width - 1}:0]"
    w = str(width).strip()
    return " " + w if w.startswith("[") else f" [{w}-1:0]"


def _line_start(data: bytes, offset: int) -> int:
    return data.rfind(b"\n", 0, offset) + 1


def _append_to_list(
    data: bytes, last_start: int, last_end: int, close: int, item: bytes, eol: bytes
) -> list[tuple[int, int, bytes]]:
    """Splices that append `item` after the last element of a comma separated list."""
    return append_items(data, last_start, last_end, close, [item], eol)


_INT_TEXT = re.compile(r"[+-]?[0-9]+")
_SIZED_TEXT = re.compile(r"([0-9]*)'([sS]?)([bBoOdDhH])([0-9a-fA-F_]+)")


def _as_int(text: str) -> int | None:
    """The integer a parameter value stands for (`4`, `32'd4`, `8'hFF`), or None."""
    t = "".join(text.split())
    if _INT_TEXT.fullmatch(t):
        return int(t)
    m = _SIZED_TEXT.fullmatch(t)
    if m is None:
        return None
    try:
        value = int(m.group(4).replace("_", ""), {"b": 2, "o": 8, "d": 10, "h": 16}[m.group(3).lower()])
    except ValueError:
        return None
    if m.group(2) and m.group(1) and value >> (int(m.group(1)) - 1) & 1:
        value -= 1 << int(m.group(1))
    return value


def _same_value(a: str, b: str) -> bool:
    """Equal as integers when both are integral, whatever their type or width; else as text."""
    x, y = _as_int(a), _as_int(b)
    return (x == y) if x is not None and y is not None else a == b


_REG_WORD = re.compile(r"(?<![A-Za-z0-9_$])reg(?![A-Za-z0-9_$])")


def _type_key(text: str) -> str:
    """A port type for comparison: `reg` is `logic` under another keyword."""
    return _REG_WORD.sub("logic", text)


def _common_width(fields: Iterable[int]) -> int:
    """The width all name fields share, or 0 when they differ (or there are none)."""
    fs = list(fields)
    return fs[0] if fs and len(set(fs)) == 1 else 0


def _renamed_field(start: int, open_: int | None, name: str, new: str | None) -> int:
    """Width of the name field of a connection or override (name and blanks before "("), after a rename."""
    base = (open_ - start - 1) if open_ is not None else len(name)
    return base + (len(new) - len(name) if new is not None else 0)


def _list_gap(data: bytes, close: int) -> bytes:
    """One blank when removing a list that ends at the ")" at `close` would glue the next name to the previous one."""
    nxt = data[close + 1 : close + 2]
    return b" " if nxt and (nxt.isalnum() or nxt in (b"_", b"$", b"\\")) else b""


def _name_width(items: Iterable[ConnInfo | ParamOverride]) -> int:
    """Common width of the name fields (name plus blanks before "(") or 0."""
    widths = []
    for c in items:
        name = c.port if isinstance(c, ConnInfo) else c.name
        widths.append(c.open - c.start - 1 if c.open is not None else len(name or ""))
    return widths[0] if widths and len(set(widths)) == 1 else 0


def _bare_name(text: object) -> str | None:
    """The first token of `text` that would not mean the same in every module, or None.

    Allowed: keywords, system functions, literals (also based ones, whose digits lex as
    identifier-looking tokens) and package-scoped names `pkg::NAME`. Anything else that
    looks like an identifier is a bare name; `.` (a hierarchical name) and macro uses
    are refused too.
    """
    toks = tokens(text)
    i = 0
    while i < len(toks):
        t = toks[i]
        if _BASED.fullmatch(t):
            i += 1
            while i < len(toks) and _DIGITS.fullmatch(toks[i]):
                i += 1
            continue
        if t.startswith("$"):
            i += 1
            continue
        if t == "." or t.startswith("`"):
            return t
        if _IDENT.fullmatch(t) and is_simple_identifier(t):
            if i + 1 < len(toks) and toks[i + 1] == "::":
                i += 3
                continue
            return t
        i += 1
    return None


def _logic_type(text: str) -> str:
    """A net keyword or an implicit type becomes `logic` with the same dimensions."""
    toks = tokens(text)
    if toks and toks[0] in _NET_KEYWORDS:
        text = text.strip()[len(toks[0]) :].strip()
        toks = toks[1:]
    if not toks or toks[0] in ("[", "signed", "unsigned"):
        return ("logic " + text).strip()
    return text.strip()


def _param_decl(type: str, name: str, default: object) -> str:
    t = str(type).strip()
    return f"parameter {t + ' ' if t else ''}{name} = {default}"


@dataclass(frozen=True)
class _Check:
    """A post-condition as plain data: `kind` names its evaluator, `args` are the strings it needs.

    `key` is the target: a later check with the same key replaces the earlier one. Nothing here
    is a closure, so a session can be saved with its checks and evaluate them after a restore.
    """

    key: tuple[str, ...]
    kind: str
    args: tuple[str | None, ...]


_MISSING = "{}: module not found in the final design"


def _module_of(design: Design, mod: str) -> ModuleInfo | None:
    try:
        return design.module(mod)
    except AdapterError:
        return None


def _ev_add_file(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    name, path = a
    mods = set(design.defined_in(Path(path)))
    if any("." in p and s.module in mods for p, s in design.snapshot().items()):
        return None
    return f"add_file {name}: none of its modules is instantiated"


def _ev_add_port(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, name, decl = a
    m = _module_of(design, mod)
    if m is None:
        return _MISSING.format(f"add_port {mod} {name}")
    port = next((p for p in m.ports if p.name == name), None)
    if port is None:
        return f"add_port {mod} {name}: port not found in the final design"
    text = session.text(m.path)[port.start : port.end].decode("utf-8", errors="replace")
    if not _same(text, decl):
        return f"add_port {mod} {name}: expected {decl!r}, found {text!r}"
    return None


def _ev_add_param(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, name, default, type = a
    m = _module_of(design, mod)
    if m is None:
        return _MISSING.format(f"add_param {mod} {name}")
    p = next((p for p in m.params if p.name == name), None)
    if p is None or m.param_kinds.get(name) != "value":
        return f"add_param {mod} {name}: parameter not found in the final design"
    if not _same(p.default or "", default):
        return f"add_param {mod} {name}: expected default {default!r}, found {p.default!r}"
    if not _same(p.type_text, type):
        return f"add_param {mod} {name}: expected type {type!r}, found {p.type_text!r}"
    return None


def _ev_add_wire(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, name, type_text = a
    m = _module_of(design, mod)
    if m is None:
        return _MISSING.format(f"add_wire {mod} {name}")
    d = next((d for d in m.decls if d.name == name), None)
    if d is None:
        return f"add_wire {mod} {name}: declaration not found in the final design"
    if not _same(d.type_text, type_text):
        return f"add_wire {mod} {name}: expected type {type_text!r}, found {d.type_text!r}"
    return None


def _ev_add_assign(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, target, expr = a
    m = _module_of(design, mod)
    if m is None:
        return _MISSING.format(f"add_assign {mod} {target}")
    if not any(x.left == target and _same(x.right, expr) for x in m.assigns):
        return f"add_assign {mod} {target}: the assignment 'assign {target} = {expr};' is missing"
    return None


def _ev_propagate_param(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    name, to, before = a
    found = design.snapshot().get(to)
    after = None if found is None else found.params.get(name)
    if after != before:
        return f"propagate_param {name} {to}: the value in use changed from {before!r} to {after!r}"
    return None


def _ev_remove_instance(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, name = a
    now = _module_of(design, mod)
    if now is None:
        return _MISSING.format(f"remove_instance {mod}.{name}")
    if any(i.name == name for i in now.instances):
        return f"remove_instance {mod}.{name}: the instance is still there in the final design"
    return None


def _ev_remove_wire(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, name = a
    now = _module_of(design, mod)
    if now is None:
        return _MISSING.format(f"remove_wire {mod} {name}")
    if any(d.name == name for d in now.decls):
        return f"remove_wire {mod} {name}: the declaration is still there in the final design"
    return None


def _ev_remove_assign(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, target = a
    now = _module_of(design, mod)
    if now is None:
        return _MISSING.format(f"remove_assign {mod} {target}")
    if any(x.left == target for x in now.assigns):
        return f"remove_assign {mod} {target}: the assignment is still there in the final design"
    return None


def _ev_remove_port(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, name = a
    now = _module_of(design, mod)
    if now is None:
        return _MISSING.format(f"remove_port {mod} {name}")
    if any(p.name == name for p in now.ports):
        return f"remove_port {mod} {name}: the port is still there in the final design"
    return None


def _ev_remove_param(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, name = a
    now = _module_of(design, mod)
    if now is None:
        return _MISSING.format(f"remove_param {mod} {name}")
    if any(p.name == name for p in now.params):
        return f"remove_param {mod} {name}: the parameter is still there in the final design"
    return None


def _ev_conn(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    """`text` is the expression a `connect` wants; None: a `disconnect` wants no connection."""
    mod, name, port, text = a
    op = "disconnect" if text is None else "connect"
    try:
        inst = design.module(mod).instance(name)
    except AdapterError:
        return f"{op} {mod}.{name} {port}: instance not found in the final design"
    c = next((c for c in inst.connections if c.port == port), None)
    found = None if c is None else c.expr
    if text is None:
        ok, expect = found is None or not tokens(found), "no connection"
    else:
        ok, expect = found is not None and _same(found, text), repr(text)
    return None if ok else f"{op} {mod}.{name} {port}: expected {expect}, found {found!r}"


def _ev_set_param(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, iname, name, text = a
    try:
        found = design.module(mod).instance(iname).param_override(name).value
    except AdapterError:
        return f"set_param {mod}.{iname} {name}: override not found in the final design"
    if not _same(found, text):
        return f"set_param {mod}.{iname} {name}: expected {text!r}, found {found!r}"
    return None


def _ev_replace_module(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, inst, module = a
    try:
        found = design.module(mod).instance(inst).module
    except AdapterError:
        return f"replace_module {mod}.{inst}: instance not found in the final design"
    if found != module:
        return f"replace_module {mod}.{inst}: expected module {module!r}, found {found!r}"
    return None


# kind -> (evaluator, number of arguments)
_EVALUATORS: dict[str, tuple[Callable[[Session, tuple[Any, ...], Design], str | None], int]] = {
    "add_file": (_ev_add_file, 2),
    "add_port": (_ev_add_port, 3),
    "add_param": (_ev_add_param, 4),
    "add_wire": (_ev_add_wire, 3),
    "add_assign": (_ev_add_assign, 3),
    "propagate_param": (_ev_propagate_param, 3),
    "remove_instance": (_ev_remove_instance, 2),
    "remove_wire": (_ev_remove_wire, 2),
    "remove_assign": (_ev_remove_assign, 2),
    "remove_port": (_ev_remove_port, 2),
    "remove_param": (_ev_remove_param, 2),
    "conn": (_ev_conn, 4),
    "param": (_ev_set_param, 4),
    "replace_module": (_ev_replace_module, 3),
}


_DAMAGE = (KeyError, TypeError, ValueError, AttributeError, IndexError, SpliceError, RecursionError)


def _stored_check(data: object) -> _Check:
    """A post-condition from an archive's index; TypeError or ValueError when it is not one."""
    fields = need_dict(data)
    kind = need_str(fields["kind"])
    if kind not in _EVALUATORS:
        raise ValueError(f"unknown post-condition {kind!r}")
    key = tuple(need_str(x) for x in need_list(fields["key"]))
    args = tuple(None if x is None else need_str(x) for x in need_list(fields["args"], _EVALUATORS[kind][1]))
    if not key:
        raise ValueError(f"post-condition {kind!r} has no key")
    return _Check(key, kind, args)


def _read_vet_state(
    index: dict[str, Any],
) -> tuple[list[OpRecord], list[str], list[_Check], dict[tuple[str, str], set[str]]]:
    """The Vet's own part of an archive's index, type-checked."""
    log = [OpRecord(*(need_str(x) for x in need_list(e, 3))) for e in need_list(index["log"])]
    notes = [need_str(n) for n in need_list(index["notes"])]
    checks = [_stored_check(c) for c in need_list(index["checks"])]
    if len({c.key for c in checks}) != len(checks):
        raise ValueError("two post-conditions have the same key")
    swapped: dict[tuple[str, str], set[str]] = {}
    for entry in need_list(index["swapped"]):
        mod, name, mods = need_list(entry, 3)
        swapped[(need_str(mod), need_str(name))] = {need_str(m) for m in need_list(mods)}
    return log, notes, checks, swapped


def _evaluate(session: Session, check: _Check, design: Design) -> str | None:
    """The message of a failed post-condition on `design`, or None."""
    return _EVALUATORS[check.kind][0](session, check.args, design)


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


# --- Vet ---------------------------------------------------------------------------


class Vet:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.log: list[OpRecord] = []
        self.notes: list[str] = []
        self._depth = 0  # open `group` blocks (a checked operation counts as one while it runs)
        # (module, instance) -> modules the instance's baseline path had before `replace_module`
        self._swapped: dict[tuple[str, str], set[str]] = {}
        # (module, signal) pairs that a removed connection or assignment used to drive
        self._drove: list[tuple[str, str]] = []
        self._checks: list[_Check] = []  # in the order registered; a key is there once
        self._dirty: set[tuple[str, ...]] = set()
        self._all = False
        self._added: dict[str, dict[str, tuple[str, str]]] = {}
        self._seen = session.reloads
        self._paths: dict[str, list[str]] | None = None
        self._snap_cache: dict[str, Any] | None = None
        self._gen_cache: dict[str, tuple[Path, bytes, list[tuple[str, str, bool]]]] = {}
        self._gen_headers: tuple[bytes, ...] | None = None

    # --- raw escape hatch ----------------------------------------------------------

    def splice(self, path: Path, start: int, end: int, text: bytes, old: bytes | None = None) -> None:
        self.session.splice(path, start, end, text, old=old)
        self._sync()
        self._all = True

    def expect(self, kind: str, path: str = "*", name: str = "*", *, exact: bool = False) -> None:
        self.session.expect(kind, path, name, exact=exact)

    def permit(self, kind: str, path: str = "*", name: str = "*", *, exact: bool = False) -> None:
        self.session.permit(kind, path, name, exact=exact)

    def text(self, path: Path) -> bytes:
        return self.session.text(path)

    @property
    def design(self) -> Design:
        design = self.session.design
        self._sync()
        return design

    def finish(self, scripts: Iterable[tuple[str, str]] = ()) -> Result:
        return self.session.finish([*self.session.scripts, *scripts], self._evaluators())

    def _idle(self, op: str) -> None:
        """Refuse `op` inside a `group` block and after `finish`."""
        if self._depth:
            raise VetError(f"{op}: not allowed inside a group")
        if self.session.finished:
            raise VetError(f"{op}: the session is already finished")

    def check(self, scripts: Iterable[tuple[str, str]] = ()) -> Result:
        """What `finish` would return now, without finishing: text, queue and operations are as before.

        Only `session.reloads` advances. What this Vet knows is waiting for a re-load (dirty marks,
        names added since the last load) is kept: the rolled-back session is as unloaded as before.
        """
        self._idle("check")
        self._sync()
        dirty, everything = set(self._dirty), self._all
        added = {mod: dict(names) for mod, names in self._added.items()}
        try:
            return self.session.check([*self.session.scripts, *scripts], self._evaluators())
        finally:
            self._dirty, self._all, self._added = dirty, everything, added
            self._seen = self.session.reloads

    # --- saved sessions ------------------------------------------------------------

    def save(self, path: Path | str, scripts: Iterable[tuple[str, str]] = ()) -> None:
        """Apply and load what is queued, then write the session to the archive `path`.

        `scripts`: (name, sha256) of the scripts applied since the session began or was resumed;
        they join `session.scripts`. The session stays usable. Refused when `path` is inside the
        input tree, is the file list or the source of an added file. A save in the middle of a
        script ends an offset-map stage there, as a load does. See `tests/test_save_resume.py`.
        """
        self._idle("save")
        dest = Path(path)
        root = self.session.layout_root
        target = dest.resolve()
        if target == root or root in target.parents:
            raise VetError(f"save: {dest} is inside the input tree {root}; the archive must not be written there")
        for owned, what in self.session.owned_paths().items():
            if target == owned:
                raise VetError(f"save: {dest} is {what}; the archive must not replace it")
        try:
            self.session.refresh()
        except AdapterError as e:
            raise VetError(f"save: the edits queued cannot be loaded: {e}") from e
        self._sync()
        applied = [(str(n), str(h)) for n, h in scripts]
        extra = {
            "log": [[r.op, r.target, r.status] for r in self.log],
            "notes": list(self.notes),
            "checks": [{"key": list(c.key), "kind": c.kind, "args": list(c.args)} for c in self._checks],
            "swapped": [[mod, name, sorted(mods)] for (mod, name), mods in sorted(self._swapped.items())],
        }
        try:
            index, files = self.session.to_state([*self.session.scripts, *applied], extra)
            write_archive(dest, index, files)
        except (SessionError, ArchiveError) as e:
            raise VetError(f"save: {e}") from e
        self.session.scripts.extend(applied)

    @classmethod
    def restore(cls, path: Path | str) -> Vet:
        """A Vet on a new session in the state saved at `path`; the input tree is not needed.

        SessionError (naming `path`) when it is not a session archive, is damaged in any way, has
        another format version or too much data, or names a path outside its layout root.
        """
        try:
            index, files = read_archive(Path(path))
        except ArchiveError as e:
            raise SessionError(str(e)) from e
        try:
            log, notes, checks, swapped = _read_vet_state(index)
            vet = cls(Session.from_state(index, files))
        except SessionError as e:
            raise SessionError(f"{path}: {e}") from e
        except AdapterError as e:
            raise SessionError(f"{path}: the saved design cannot be loaded: {e}") from e
        except _DAMAGE as e:
            raise SessionError(f"{path}: the session archive is damaged ({type(e).__name__}: {e})") from e
        vet.log, vet.notes, vet._checks, vet._swapped = log, notes, checks, swapped
        return vet

    # --- selectors -----------------------------------------------------------------

    def module(self, name: str, instances: int | None = None) -> Module:
        self._sync()
        if self._all:
            self.session.refresh()
            self._sync()
        known = self.session.loaded.modules()
        if name not in known:
            raise VetError(f"no module {name!r} in the design{_did_you_mean(name, known)}")
        paths = self.module_paths(name)
        if not paths:
            try:
                where = self.session.loaded.module(name).path
            except AdapterError:
                where = None
            if where is not None and self.session.is_added(where):
                raise VetError(f"module {name!r} is defined in the added file {where.name}, which is never edited")
            raise VetError(f"module {name!r} is not elaborated under the top, so it cannot be edited")
        if instances is not None and instances != len(paths):
            raise VetError(
                f"module {name!r} is elaborated {len(paths)} time(s) ({', '.join(paths)}), not {instances}"
            )
        return Module(self, name, instances)

    # --- bookkeeping ---------------------------------------------------------------

    def _sync(self) -> None:
        """Forget what was edited when the design has been re-loaded since."""
        if self.session.reloads != self._seen:
            self._seen = self.session.reloads
            self._dirty.clear()
            self._all = False
            self._added.clear()

    def _mark(self, *key: str) -> None:
        self._sync()
        self._dirty.add(key)

    def _refresh_if(self, keys: Iterable[tuple[str, ...]]) -> None:
        self._sync()
        if self._all or any(k in self._dirty for k in keys):
            self.session.refresh()
            self._sync()

    def _info(self, module: str, keys: Iterable[tuple[str, ...]] = ()) -> ModuleInfo:
        self._refresh_if(keys)
        try:
            return self.session.loaded.module(module)
        except AdapterError as e:
            raise VetError(str(e)) from e

    def _snap(self) -> dict[str, Any]:
        if self._snap_cache is None:
            self._snap_cache = self.session.baseline_snapshot()
        return self._snap_cache

    def module_paths(self, module: str) -> list[str]:
        if self._paths is None:
            snap = self._snap()
            paths: dict[str, list[str]] = {}
            for p in sorted(snap):
                paths.setdefault(snap[p].module, []).append(p)
            self._paths = paths
        return list(self._paths.get(module, []))

    def _set_check(self, check: _Check) -> None:
        """Register a post-condition; a later one on the same target replaces the earlier."""
        self._checks = [c for c in self._checks if c.key != check.key]
        self._checks.append(check)

    def _evaluators(self) -> list[Callable[[Design], str | None]]:
        """The registered post-conditions as `Session.finish` takes them."""
        session = self.session
        return [partial(_evaluate, session, c) for c in self._checks]

    def _instance_info(self, module: str, name: str) -> InstanceInfo:
        info = self._info(module)
        found = [i for i in info.instances if i.name == name]
        if not found:
            raise VetError(
                f"module {module!r} has no instance {name!r}{_did_you_mean(name, [i.name for i in info.instances])}"
            )
        if len(found) > 1:
            raise VetError(f"module {module!r} has {len(found)} instances named {name!r}")
        return found[0]

    def _queue(self, path: Path, splices: list[tuple[int, int, bytes]]) -> None:
        data = self.session.text(path)
        for start, end, text in splices:
            self.session.splice(path, start, end, text, old=data[start:end])

    def _commit_all(self, op: str, plans: list[_Plan]) -> None:
        """Commit validated plans; nothing is queued unless every file to edit is editable."""
        bad = sorted(
            {
                str(p.path)
                for p in plans
                if not p.skip and p.path is not None and not self.session.is_editable(p.path)
            }
        )
        if bad:
            raise VetError(f"{op}: cannot edit {', '.join(bad)}: not editable in this session")
        for plan in plans:
            self._commit(plan)

    def _refuse_generate_copies(self, op: str, mods: list[str]) -> None:
        """Refuse an instance of one of `mods` written in a generate block or array, in any module."""
        wanted = set(mods)
        for host, flagged in self._generate_copies().items():
            for name, module, in_generate in flagged:
                if module in wanted:
                    what = "inside a generate block" if in_generate else "an instance array"
                    raise VetError(
                        f"{op}: instance {name!r} of module {module!r} in module {host!r} is "
                        f"{what} (generate blocks and instance arrays are not supported): a new port could "
                        f"stay unconnected under another parameterisation"
                    )

    def _generate_copies(self) -> dict[str, list[tuple[str, str, bool]]]:
        """Per module: its instances written in a generate block or instance array.

        Each entry is (instance name, instantiated module, in a generate block).
        Building the source model of every module costs about 0.2 s on the demo_ss
        sample, and a re-load used to throw the result away. The answer for a
        module depends on the text of the file that defines it and on the headers:
        a macro or a `define in a header can add an instance to a module whose own
        file is unchanged. So an entry is kept while its file's text is unchanged,
        and everything is dropped when any header's text changes (headers are few
        and rarely edited). A module whose definition moved or changed has a file
        with different text and is looked at again.
        """
        design = self.session.loaded
        sources = set(self.session.filelist.files)
        headers = tuple(self.session.text(p) for p in self.session.files if p not in sources)
        known = self._gen_headers
        if (
            known is None
            or len(known) != len(headers)
            or any(a is not b and a != b for a, b in zip(known, headers, strict=True))
        ):
            self._gen_cache.clear()
            self._gen_headers = headers
        out: dict[str, list[tuple[str, str, bool]]] = {}
        for host in design.modules():
            cached = self._gen_cache.get(host)
            if cached is not None:
                path, text, flagged = cached
                try:
                    current = self.session.text(path)
                except SessionError:
                    current = None
                if current is text or current == text:
                    out[host] = flagged
                    continue
            try:
                info = design.module(host)
            except AdapterError:
                self._gen_cache.pop(host, None)
                continue
            flagged = [
                (inst.name, inst.module, inst.in_generate)
                for inst in info.instances
                if inst.in_generate or inst.is_array
            ]
            try:
                text = self.session.text(info.path)
            except SessionError:
                out[host] = flagged  # a file the session does not track: do not cache
                continue
            self._gen_cache[host] = (info.path, text, flagged)
            out[host] = flagged
        return out

    def _record(self, op: str, target: str, status: str) -> None:
        self.log.append(OpRecord(op, target, status))

    # --- checked operations, groups ---------------------------------------------------

    def _begin(self, label: str) -> _VetState:
        """Apply and load what is queued, then take a checkpoint.

        So a checkpoint never has a queue: rolling back to it also means "nothing of this
        Vet is waiting for a re-load", which is what the dirty marks say after a re-load.
        """
        try:
            self.session.refresh()
        except AdapterError as e:
            raise VetError(f"{label}: the edits queued earlier cannot be loaded: {e}") from e
        self._sync()
        return _VetState(
            self.session.checkpoint(),
            len(self.log),
            len(self.notes),
            list(self._checks),
            {k: set(v) for k, v in self._swapped.items()},
            len(self._drove),
        )

    def _rollback(self, st: _VetState) -> None:
        self.session.rollback(st.session)
        del self.log[st.log :]
        del self.notes[st.notes :]
        self._checks = list(st.checks)
        self._swapped = {k: set(v) for k, v in st.swapped.items()}
        del self._drove[st.drove :]
        self._dirty.clear()
        self._all = False
        self._added.clear()
        self._seen = self.session.reloads

    def _conclude(self, st: _VetState, prefix: str) -> None:
        """Compare the edited design with the one at `st`: refuse (taking everything back) or note."""
        try:
            self.session.refresh()
            self._sync()
            found = self.session.compare_since(st.session, self._drove[st.drove :])
        except AdapterError as e:
            self._rollback(st)
            raise VetError(f"{prefix}: the edited design cannot be loaded: {e}") from e
        if found is None:
            return
        problems = [f"new {d.severity} {d.code}: {describe(d)}" for d in found.diagnostics]
        problems += [f"left dangling: {describe(d)}" for d in found.dangling]
        problems += [f"left partly undriven: {msg}" for msg in found.partial]
        if problems:
            self._rollback(st)
            more = f" (and {len(problems) - 3} more)" if len(problems) > 3 else ""
            raise VetError(f"{prefix}: " + "; ".join(problems[:3]) + more)
        self.notes.extend(f"{prefix}: {describe(d)}" for d in found.unused)

    def _checked(self, op: str, target: str, run: Callable[[], None], tried: bool = False) -> None:
        """Run a checked operation: alone it is a group of its own, in a group it is part of it.

        `tried`: the operation makes checks of its own on the edited design at the call,
        so inside a group it still loads what is queued first and is taken back by itself.
        """
        prefix = f"{op} {target}"
        if self._depth and not tried:
            run()
            return
        st = self._begin(prefix)
        outer = self._depth
        self._depth += 1
        try:
            run()
        except BaseException:
            self._depth = outer
            self._rollback(st)
            raise
        self._depth = outer
        if not outer:
            self._conclude(st, prefix)

    @contextmanager
    def group(self) -> Iterator[None]:
        """`with vet.group():` the comparison of the checked operations is made once, at the end."""
        if self._depth:
            # Part of the outer group (compared at its end), but taken back on its own when it fails.
            st = self._begin("group")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                self._rollback(st)
                raise
            self._depth -= 1
            return
        st = self._begin("group")
        self._depth = 1
        try:
            yield
        except BaseException:
            self._depth = 0
            self._rollback(st)
            raise
        self._depth = 0
        self._conclude(st, "group")

    def add_file(self, path: Path | str) -> None:
        """Bring a source file outside the design into it (loaded last, never edited)."""
        name = Path(path).name

        def run() -> None:
            try:
                added = self.session.add_file(path)
            except SessionError as e:
                raise VetError(f"add_file {name}: {e}") from e
            self._sync()
            if not added:
                self._record("add_file", name, "skipped")
                return
            self._set_check(_Check(("add_file", name), "add_file", (name, str(Path(path).resolve()))))
            self._record("add_file", name, "applied")

        self._checked("add_file", name, run, tried=True)

    def _driven_by(self, host: str, inst: InstanceInfo, only: str | None = None) -> list[tuple[str, str]]:
        """(module, signal) pairs that the output connections of `inst` (in `host`) drive.

        `only`: just the connection to that port.
        """
        try:
            child = self._info(inst.module)
        except VetError:
            return []
        out: list[tuple[str, str]] = []
        for i, c in enumerate(inst.connections):
            port = c.port
            if port is None and i < len(child.ports):
                port = child.ports[i].name
            if port is None or port == "*" or (only is not None and port != only):
                continue
            info = next((p for p in child.ports if p.name == port), None)
            if info is not None and info.direction in ("output", "inout"):
                out.extend((host, base) for base in driven_bases(c.expr))
        return out

    def _drop_registered(self, module: str, name: str) -> None:
        """Forget the post-conditions earlier operations registered for the instance `name` of `module`."""
        self._checks = [
            c
            for c in self._checks
            if not (
                len(c.key) >= 3 and c.key[0] in ("replace_module", "conn", "param") and c.key[1:3] == (module, name)
            )
        ]

    def _refuse_disabled(self, op: str, mod: str, info: ModuleInfo, name: str) -> None:
        """Refuse a name that conditional directives hide from this configuration but not from another."""
        directive = info.disabled_names.get(name)
        if directive is not None:
            raise VetError(
                f"{op} {mod}: {name!r} appears in text disabled by {directive}; in another configuration "
                f"the module would no longer compile. Edit it by hand or with a raw splice."
            )

    def _refresh_all_dirty(self) -> None:
        """Re-load when anything this Vet edited since the last load is still waiting."""
        self._sync()
        if self._all or self._dirty:
            self.session.refresh()
            self._sync()

    def _instances_of(self, mod: str) -> list[tuple[str, InstanceInfo]]:
        """Every instance of `mod` in any module of the design, as (the module it is in, its model)."""
        design = self.session.loaded
        out: list[tuple[str, InstanceInfo]] = []
        for host in design.modules():
            try:
                info = design.module(host)
            except AdapterError:
                continue
            out.extend((host, i) for i in info.instances if i.module == mod)
        return out

    def _other_use(self, info: ModuleInfo, name: str, own: str) -> str | None:
        """What else, besides an item of kind `own`, already uses `name` in the module."""
        if own != "port" and any(p.name == name for p in info.ports):
            return "a port"
        if own != "param" and name in info.param_kinds:
            return "a parameter"
        if own != "decl" and any(d.name == name for d in info.decls):
            return "a net or variable"
        if any(i.name == name for i in info.instances):
            return "an instance"
        if name in info.other_names:
            return "a typedef, subroutine, genvar or named block"
        added = self._added.get(info.name, {}).get(name)
        if added is not None and added[0] != own:
            return f"a {added[0]} added earlier by this script"
        return None

    # --- plans: what one underlying operation will do ------------------------------

    def _commit(self, plan: _Plan) -> None:
        """Queue a plan, mark what it edited, register its expectations and post-condition, log it."""
        if plan.skip:
            self._record(plan.op, plan.target, "skipped")
            return
        assert plan.path is not None  # noqa: S101 - every plan that is not skipped names its file
        self._queue(plan.path, plan.splices)
        if plan.mark is not None:
            self._mark(*plan.mark)
        if plan.added is not None:
            mod, name, value = plan.added
            self._added.setdefault(mod, {})[name] = value
        for kind, path, name in plan.expects:
            self.session.expect(kind, path, name, exact=True)
        for kind, path, name in plan.permits:
            self.session.permit(kind, path, name, exact=True)
        if plan.check is not None:
            self._set_check(plan.check)
        if not plan.quiet:
            self._record(plan.op, plan.target, "applied")

    def _plan_add_port(self, mod: str, name: str, decl: str, info: ModuleInfo, paths: list[str]) -> _Plan:
        if info.port_list_kind != "ansi":
            what = "a non-ANSI" if info.port_list_kind == "nonansi" else "no"
            raise VetError(f"add_port {mod}: the module has {what} port list; only ANSI port lists can be extended")
        if info.header_has_macros:
            raise VetError(f"add_port {mod}: the module header contains macros, so it cannot be edited safely")
        data = self.session.text(info.path)
        existing = next((p for p in info.ports if p.name == name), None)
        if existing is not None:
            found = data[existing.start : existing.end].decode("utf-8", errors="replace")
            if _same(found, decl):
                return _Plan("add_port", mod, skip=True)
            raise DriftError(f"add_port {mod}: port {name!r} exists as {found!r}, not {decl!r}")
        other = self._other_use(info, name, "port")
        if other is not None:
            raise DriftError(f"add_port {mod}: the name {name!r} is already used by {other}")
        added = self._added.get(mod, {}).get(name)
        if added is not None:
            if added == ("port", _norm(decl)):
                return _Plan("add_port", mod, skip=True)
            raise DriftError(f"add_port {mod}: port {name!r} was already added with a different declaration")
        _refuse_directive(f"add_port {mod}", "port", info.ports_directive)
        if info.ports_close is None:
            raise VetError(f"add_port {mod}: cannot find the end of the port list")
        eol = _eol(data)
        item = decl.encode()
        if not info.ports:
            splices = [(info.ports_close, info.ports_close, item)]
        else:
            last = info.ports[-1]
            splices = _append_to_list(data, last.start, last.end, info.ports_close, item, eol)

        return _Plan(
            "add_port",
            mod,
            path=info.path,
            splices=splices,
            mark=("ports", mod),
            added=(mod, name, ("port", _norm(decl))),
            expects=[("port-added", p, name) for p in paths],
            permits=[("connection-changed", p, name) for p in paths],
            check=_Check(("add_port", mod, name), "add_port", (mod, name, decl)),
        )

    def _plan_add_param(
        self, mod: str, name: str, type: str, default: object, info: ModuleInfo, paths: list[str]
    ) -> _Plan:
        decl = _param_decl(type, name, default)
        if info.header_has_macros:
            raise VetError(f"add_param {mod}: the module header contains macros, so it cannot be edited safely")
        existing = next((p for p in info.params if p.name == name), None)
        if existing is not None:
            if (
                info.param_kinds.get(name) == "value"
                and _same(existing.type_text, type)
                and _same(existing.default or "", default)
            ):
                return _Plan("add_param", mod, skip=True)
            raise DriftError(
                f"add_param {mod}: parameter {name!r} exists as type {existing.type_text!r} with default "
                f"{existing.default!r}, not {type!r} with default {str(default)!r}"
            )
        if name in info.param_kinds:
            raise DriftError(f"add_param {mod}: the name {name!r} is already used by a parameter in the module body")
        other = self._other_use(info, name, "param")
        if other is not None:
            raise DriftError(f"add_param {mod}: the name {name!r} is already used by {other}")
        added = self._added.get(mod, {}).get(name)
        if added is not None:
            if added == ("param", _norm(decl)):
                return _Plan("add_param", mod, skip=True)
            raise DriftError(f"add_param {mod}: parameter {name!r} was already added with a different default")
        _refuse_directive(f"add_param {mod}", "parameter", info.params_directive)
        data = self.session.text(info.path)
        item = decl.encode()
        if info.params_close is None:
            at = info.name_end if info.imports_end is None else info.imports_end
            splices = [(at, at, b" #(" + item + b")")]
        elif not info.params:
            splices = [(info.params_close, info.params_close, item)]
        else:
            last = info.params[-1]
            splices = _append_to_list(data, last.start, last.end, info.params_close, item, _eol(data))

        return _Plan(
            "add_param",
            mod,
            path=info.path,
            splices=splices,
            mark=("params", mod),
            added=(mod, name, ("param", _norm(decl))),
            expects=[("param-changed", p, name) for p in paths],
            check=_Check(("add_param", mod, name), "add_param", (mod, name, str(default), str(type))),
        )

    def _plan_assign(
        self, mod: str, info: ModuleInfo, target: str, expr: object, planned_port: bool = False
    ) -> _Plan:
        """`planned_port`: `target` is an output port this operation adds, so it is undriven."""
        if not planned_port:
            ports = {p.name: p for p in info.ports}
            names = set(ports) | {d.name for d in info.decls}
            if target not in names:
                raise VetError(
                    f"add_assign {mod}: no net, variable or port {target!r}{_did_you_mean(target, names)}"
                )
            if any(a.left == target and _same(a.right, expr) for a in info.assigns):
                return _Plan("add_assign", mod, skip=True)
            if target in ports and ports[target].direction == "input":
                raise VetError(f"add_assign {mod}: {target!r} is an input port and cannot be assigned")
            try:
                drivers = self.session.loaded.drivers_in(mod, target)
            except AdapterError as e:
                raise VetError(f"add_assign {mod}: cannot analyse {target!r}: {e}") from e
            if drivers:
                what = "an input port" if any(d.is_input_port for d in drivers) else "already driven"
                raise VetError(f"add_assign {mod}: {target!r} is {what} and cannot be assigned")
        data = self.session.text(info.path)
        ls = _line_start(data, info.body_end)
        if data[ls : info.body_end].strip() != b"":
            raise VetError(f"add_assign {mod}: endmodule does not start its line")
        text = info.body_indent.encode() + f"assign {target} = {expr};".encode() + _eol(data)

        return _Plan(
            "add_assign",
            mod,
            path=info.path,
            splices=[(ls, ls, text)],
            mark=("decls", mod),
            check=_Check(("add_assign", mod, target, _norm(expr)), "add_assign", (mod, target, str(expr))),
        )

    # --- hierarchical operations ----------------------------------------------------

    def _walk_path(self, op: str, to: str) -> tuple[list[str], list[str], list[str]]:
        """Validate `to`; return the modules and paths M0..Mn and the instance names between them."""
        snap = self._snap()
        segs = str(to).split(".")
        tops = sorted(p for p in snap if "." not in p)
        if len(segs) == 1 and segs[0] in tops:
            raise VetError(f"{op}: {to!r} is the top module; the path must name an instance below the top")
        if segs[0] not in tops:
            raise VetError(
                f"{op}: the path {to!r} starts at {segs[0]!r}, not at the top module "
                f"({', '.join(repr(t) for t in tops)})"
            )
        path = segs[0]
        mods = [snap[path].module]
        paths = [path]
        names: list[str] = []
        for seg in segs[1:]:
            if "[" in seg or "]" in seg:
                raise VetError(
                    f"{op}: {seg!r} in {to!r}: generate blocks and instance arrays are not supported"
                )
            nxt = f"{path}.{seg}"
            if nxt not in snap:
                try:
                    info = self.session.loaded.module(mods[-1])
                except AdapterError as e:
                    raise VetError(str(e)) from e
                hit = next((i for i in info.instances if i.name == seg), None)
                if hit is not None and hit.is_array:
                    raise VetError(f"{op}: {seg!r} in module {mods[-1]!r} is an instance array, which is not supported")
                if hit is not None and hit.in_generate:
                    raise VetError(
                        f"{op}: {seg!r} in module {mods[-1]!r} is inside a generate block, which is not supported"
                    )
                if hit is not None:
                    raise VetError(f"{op}: instance {seg!r} of module {mods[-1]!r} is not elaborated under the top")
                raise VetError(
                    f"{op}: module {mods[-1]!r} has no instance {seg!r}"
                    f"{_did_you_mean(seg, [i.name for i in info.instances])}"
                )
            names.append(seg)
            path = nxt
            paths.append(path)
            mods.append(snap[path].module)
        if len(set(mods)) != len(mods):
            raise VetError(f"{op}: a module appears twice on the path {to!r}")
        return mods, paths, names

    def _refresh_for(self, mods: Iterable[str]) -> None:
        """Re-load once if a list of one of `mods` was edited since the last load."""
        self._sync()
        wanted = set(mods)
        self._refresh_if([k for k in self._dirty if k[1] in wanted])

    def _is_interface(self, mod: str, port: str) -> bool:
        snap = self._snap()
        return any(snap[p].ports.get(port, ("",))[0] == "interface" for p in self.module_paths(mod))

    def _signal_type(self, op: str, mod: str, info: ModuleInfo, signal: str) -> str:
        """Validate `signal` in `mod` and return its data type as written."""
        port = next((p for p in info.ports if p.name == signal), None)
        decl = next((d for d in info.decls if d.name == signal), None)
        if port is None and decl is None:
            names = [p.name for p in info.ports] + [d.name for d in info.decls]
            raise VetError(
                f"{op}: module {mod!r} has no net, variable or port {signal!r}{_did_you_mean(signal, names)}"
            )
        if port is not None and self._is_interface(mod, signal):
            raise VetError(f"{op}: {signal!r} in module {mod!r} is an interface port, which cannot be punched")
        if port is not None:
            if port.unpacked:
                raise VetError(f"{op}: port {signal!r} of module {mod!r} has unpacked dimensions")
            if not port.type_text:
                raise VetError(f"{op}: cannot tell the type of port {signal!r} of module {mod!r}; pass type= or width=")
            return port.type_text
        assert decl is not None  # noqa: S101 - no port, so the check above found a declaration
        toks = tokens(self.session.text(info.path)[decl.start : decl.end].decode("utf-8", errors="replace"))
        if "interconnect" in (tokens(decl.type_text)[:1] or toks[:1]):
            raise VetError(f"{op}: {signal!r} in module {mod!r} is declared interconnect, which cannot be punched")
        if len(toks) > 1 and toks[1] == "[":
            raise VetError(f"{op}: {signal!r} in module {mod!r} has unpacked dimensions")
        return _logic_type(decl.type_text)

    def punch_port(
        self,
        name: str,
        direction: str,
        to: str,
        *,
        signal: str | None = None,
        width: int | str | None = None,
        type: str | None = None,
        all_instances: bool = False,
    ) -> None:
        op = "punch_port"
        if direction not in ("input", "output"):
            raise VetError(f"{op}: direction must be 'input' or 'output', not {direction!r}")
        if not is_simple_identifier(name):
            raise VetError(f"{op}: {name!r} is not a simple identifier (or is a SystemVerilog keyword)")
        if signal is not None and not _IDENT.fullmatch(signal):
            raise VetError(f"{op}: the signal must be a plain identifier, not {signal!r}")
        out = direction == "output"
        snap = self._snap()
        mods, _paths, inames = self._walk_path(op, to)
        n = len(mods) - 1
        for m in mods[1:]:
            where = self.module_paths(m)
            if len(where) > 1 and out:
                raise VetError(
                    f"{op} {name}: module {m!r} is elaborated {len(where)} times ({', '.join(where)}); "
                    f"an output cannot go through a shared module, several copies would drive one net"
                )
            if len(where) > 1 and not all_instances:
                raise VetError(
                    f"{op} {name}: module {m!r} is elaborated {len(where)} times ({', '.join(where)}) and a new port "
                    f"changes every instance; pass all_instances=True to connect them all"
                )
        targets: dict[tuple[str, str], str] = {}
        for m in mods[1:]:
            for p in self.module_paths(m):
                parent, _, iname = p.rpartition(".")
                if parent not in snap or "[" in iname:
                    raise VetError(
                        f"{op}: {p} is inside a generate block or an instance array, which is not supported"
                    )
                if snap[parent].module not in mods:
                    raise VetError(
                        f"{op}: {p} is an instance of {m!r} in module {snap[parent].module!r}, "
                        f"which is not on the path to {to!r}, so it would have no {name!r} to connect"
                    )
                targets[(snap[parent].module, iname)] = m
        sig = signal if signal is not None else (name if out else None)
        self._refresh_for(mods)
        # After the refresh, so a copy added by an earlier raw splice is seen.
        self._refuse_generate_copies(op, mods[1:])
        # an assign to an existing port needs its drivers: they depend on everything queued
        if out and sig != name and any(p.name == name for p in self._info(mods[-1]).ports):
            self.session.refresh()
            self._sync()
        infos = {m: self._info(m) for m in mods}
        if out:
            existing = [
                i for i in range(n) if any(p.name == name and p.direction == "output" for p in infos[mods[i]].ports)
            ]
            if existing:
                # the drivers depend on everything queued
                if self.session.refresh():
                    self._sync()
                    infos = {m: self._info(m) for m in mods}
                for i in existing:
                    try:
                        drivers = self.session.loaded.drivers_in(mods[i], name)
                    except AdapterError as e:
                        raise VetError(f"{op} {name}: cannot analyse {name!r} in module {mods[i]!r}: {e}") from e
                    if any(d.instance != inames[i] for d in drivers):
                        raise VetError(
                            f"{op} {name}: module {mods[i]!r} already has output port {name!r}, which is driven "
                            f"by something other than the connection from {inames[i]!r}, so it cannot carry "
                            f"another signal"
                        )

        # the data type of the new ports
        if type is not None or width is not None:
            base, pwidth = (type if type is not None else "logic"), width
            if sig is not None:
                self._signal_type(op, mods[-1] if out else mods[0], infos[mods[-1] if out else mods[0]], sig)
        elif sig is not None:
            base, pwidth = (
                self._signal_type(op, mods[-1] if out else mods[0], infos[mods[-1] if out else mods[0]], sig),
                None,
            )
        else:
            base, pwidth = "logic", None
        text = f"{base}{_range(pwidth)}"
        bare = _bare_name(text)
        if bare is not None:
            raise VetError(
                f"{op} {name}: the type {text!r} uses {bare!r}, which is not package-scoped and may not mean the "
                f"same in every module on the path; pass an explicit width= (or type=) that does"
            )
        decl = f"{direction:<6} {text} {name}"

        plans: list[_Plan] = []
        special = out and sig == name and any(
            p.name == name and p.direction == "output" for p in infos[mods[-1]].ports
        )
        for i, m in enumerate(mods):
            if i == 0 and not out and signal is not None:
                continue
            if special and i == n:
                continue
            port_plan = self._plan_add_port(m, name, decl, infos[m], self.module_paths(m))
            plans.append(port_plan)
            if out and i == n:
                assert sig is not None  # noqa: S101 - an output always has a source signal
                plans.append(self._plan_assign(m, infos[m], name, sig, planned_port=not port_plan.skip))
        for (host, iname), child in sorted(targets.items()):
            handle = Instance(self, host, iname)
            inst = self._instance_info(host, iname)
            handle._refuse_connection_list("connect", inst)
            where = handle.paths
            if not where:
                raise VetError(f"{op}: {host}.{iname} is not elaborated under the top at a known path")
            expr = signal if (host == mods[0] and not out and signal is not None) else name
            plans.append(handle._plan_connect(inst, infos[child], where, name, expr, None))
        self._commit_all(op, plans)

    def propagate_param(self, name: str, to: str, *, default: object | None = None) -> None:
        op = "propagate_param"
        mods, paths, inames = self._walk_path(op, to)
        n = len(mods) - 1
        for m in mods[:-1]:
            where = self.module_paths(m)
            if len(where) != 1:
                raise VetError(
                    f"{op} {name}: module {m!r} is elaborated {len(where)} times ({', '.join(where)}) "
                    f"and the parameter would be added to every copy"
                )
        self._refresh_for(mods)
        infos = {m: self._info(m) for m in mods}
        dest = infos[mods[-1]]
        kind = dest.param_kinds.get(name)
        if kind is None:
            values = [k for k, v in dest.param_kinds.items() if v == "value"]
            raise VetError(
                f"{op}: module {mods[-1]!r} has no parameter {name!r}{_did_you_mean(name, values)}"
            )
        if kind != "value":
            what = "a localparam" if kind == "local" else "a type parameter"
            raise VetError(
                f"{op}: parameter {name!r} of module {mods[-1]!r} is {what}, not an overridable value"
            )
        pinfo = next((p for p in dest.params if p.name == name), None)
        if pinfo is None:
            raise VetError(
                f"{op}: parameter {name!r} of module {mods[-1]!r} is declared in the module body, "
                f"not in its parameter port list, so its type is unknown"
            )
        def passes_on(j: int) -> bool:
            if infos[mods[j]].param_kinds.get(name) != "value":
                return False
            ov = next((o for o in self._instance_info(mods[j], inames[j]).param_overrides if o.name == name), None)
            return ov is not None and tokens(ov.value) == [name]

        # M(k)..M(n-1) already carry the parameter down; the rest of the path needs it
        k = n
        while k > 0 and passes_on(k - 1):
            k -= 1
        plans: list[_Plan] = []
        if k > 0:
            for m in mods[:k]:
                if name in infos[m].param_kinds:
                    raise VetError(
                        f"{op} {name}: module {m!r} already has a parameter {name!r} that is not passed down to "
                        f"{to!r}; tying it in would join two unrelated parameters"
                    )
            handles: list[tuple[Instance, InstanceInfo]] = []
            for i in range(k):
                handle = Instance(self, mods[i], inames[i])
                inst = self._instance_info(mods[i], inames[i])
                handle._guard_overrides(op, inst)
                handles.append((handle, inst))
            cur = next((o for o in handles[-1][1].param_overrides if o.name == name), None)
            declared = next((p for p in infos[mods[k]].params if p.name == name), None)
            if default is not None:
                dtext = str(default)
            elif cur is not None and tokens(cur.value):
                dtext = cur.value
            elif declared is not None and declared.default is not None:
                dtext = declared.default
            else:
                raise VetError(f"{op} {name}: the parameter has no default and no override; pass an explicit default=")
            if "//" in dtext:
                raise VetError(f"{op} {name}: the value {dtext!r} contains a comment; pass an explicit default=")
            dtext = re.sub(r"\s+", " ", dtext).strip()
            for what, value in (("type", pinfo.type_text), ("value", dtext)):
                bare = _bare_name(value)
                if bare is not None:
                    raise VetError(
                        f"{op} {name}: the {what} {value!r} uses {bare!r}, which is not package-scoped and may not "
                        f"mean the same in every module on the path; pass an explicit default= that does"
                    )
        for i in range(n):
            if i < k:
                handle, inst = handles[i]
                plans.append(
                    self._plan_add_param(
                        mods[i], name, pinfo.type_text, dtext, infos[mods[i]], self.module_paths(mods[i])
                    )
                )
                plans.append(handle._plan_set_param(inst, [paths[i + 1]], name, name, None, force=True, permit=False))
            else:
                plans.append(_Plan("add_param", mods[i], skip=True))
                plans.append(_Plan("set_param", f"{mods[i]}.{inames[i]}", skip=True))
        if self.session.reloads == 0:
            before = self._snap()[to].params.get(name)
        else:
            found = self.session.loaded.snapshot().get(to)
            before = None if found is None else found.params.get(name)
        self._commit_all(op, plans)
        if any(not p.skip for p in plans):
            self._set_check(_Check((op, name, to), op, (name, to, before)))


# --- Module handle -----------------------------------------------------------------


class Module:
    def __init__(self, vet: Vet, name: str, instances: int | None) -> None:
        self.vet = vet
        self._name = name
        self._instances = instances

    @property
    def name(self) -> str:
        return self._name

    @property
    def info(self) -> ModuleInfo:
        mod = self._name
        self.vet._sync()
        keys = [k for k in self.vet._dirty if k[1] == mod]
        return self.vet._info(mod, keys)

    def __getattr__(self, attr: str) -> Any:
        if attr.startswith("_"):
            raise AttributeError(attr)
        return getattr(self.info, attr)

    @property
    def paths(self) -> list[str]:
        return self.vet.module_paths(self._name)

    # --- selectors

    def instance(self, name: str) -> Instance:
        self.vet._instance_info(self._name, name)
        return Instance(self.vet, self._name, name, self._instances)

    def instances(self, module: str | None = None, name: str | None = None, *, count: int) -> list[Instance]:
        found = [
            i
            for i in self.vet._info(self._name).instances
            if (module is None or i.module == module) and (name is None or fnmatchcase(i.name, name))
        ]
        if len(found) != count:
            listing = ", ".join(i.name for i in found) or "none"
            raise VetError(
                f"module {self._name!r}: expected {count} matching instances, found {len(found)} ({listing})"
            )
        return [Instance(self.vet, self._name, i.name, self._instances) for i in found]

    # --- operations

    def _check_scope(self, op: str) -> list[str]:
        paths = self.paths
        if len(paths) > 1 and self._instances != len(paths):
            raise VetError(
                f"{op} {self._name}: the module is elaborated {len(paths)} times ({', '.join(paths)}) and a "
                f"module-level edit changes every instance; pass instances={len(paths)} to confirm"
            )
        return paths

    def _expect_param(self, name: str, paths: list[str]) -> None:
        for p in paths:
            self.vet.session.expect("param-changed", p, name, exact=True)

    def add_port(self, name: str, direction: str, type: str = "logic", width: int | str | None = None) -> None:
        v, mod = self.vet, self._name
        if direction not in _DIRECTIONS:
            raise VetError(f"add_port {mod}: direction must be 'input', 'output' or 'inout', not {direction!r}")
        paths = self._check_scope("add_port")
        decl = f"{direction:<6} {type}{_range(width)} {name}"
        info = v._info(mod, [("ports", mod)])
        v._commit(v._plan_add_port(mod, name, decl, info, paths))

    def add_param(self, name: str, default: object, type: str = "int") -> None:
        v, mod = self.vet, self._name
        paths = self._check_scope("add_param")
        info = v._info(mod, [("params", mod)])
        v._commit(v._plan_add_param(mod, name, type, default, info, paths))

    def add_wire(self, name: str, width: int | str | None = None, type: str = "logic") -> None:
        v, mod = self.vet, self._name
        self._check_scope("add_wire")
        if not is_simple_identifier(name):
            raise VetError(f"add_wire {mod}: {name!r} is not a simple identifier (or is a SystemVerilog keyword)")
        type_text = f"{type}{_range(width)}"
        info = v._info(mod)
        existing = next((d for d in info.decls if d.name == name), None)
        if existing is not None:
            if _same(existing.type_text, type_text):
                v._record("add_wire", mod, "skipped")
                return
            raise DriftError(f"add_wire {mod}: {name!r} is declared as {existing.type_text!r}, not {type_text!r}")
        other = v._other_use(info, name, "decl")
        if other is not None:
            raise DriftError(f"add_wire {mod}: the name {name!r} is already used by {other}")
        added = v._added.get(mod, {}).get(name)
        if added is not None:
            if added == ("decl", _norm(type_text)):
                v._record("add_wire", mod, "skipped")
                return
            raise DriftError(f"add_wire {mod}: {name!r} was already added with a different type")
        data = v.session.text(info.path)
        nl = data.find(b"\n", info.header_end)
        if nl < 0 or nl + 1 > info.body_end:
            raise VetError(f"add_wire {mod}: the module header shares its last line with the body")
        if _header_line_in_comment(data, info.header_end, nl):
            raise VetError(f"add_wire {mod}: the module header line ends inside a block comment")
        eol = _eol(data)
        text = info.body_indent.encode() + f"{type_text} {name};".encode() + eol
        v._queue(info.path, [(nl + 1, nl + 1, text)])
        v._mark("decls", mod)
        v._added.setdefault(mod, {})[name] = ("decl", _norm(type_text))

        v._set_check(_Check(("add_wire", mod, name), "add_wire", (mod, name, type_text)))
        v._record("add_wire", mod, "applied")

    def add_assign(self, target: str, expr: object) -> None:
        v, mod = self.vet, self._name
        self._check_scope("add_assign")
        if not _IDENT.fullmatch(target):
            raise VetError(f"add_assign {mod}: the target must be a plain identifier, not {target!r}")
        v.session.refresh()  # drivers depend on everything queued
        v._sync()
        info = v._info(mod)
        v._commit(v._plan_assign(mod, info, target, expr))

    # --- removals (checked operations)

    def _fresh(self) -> ModuleInfo:
        v = self.vet
        return v._info(self._name, [k for k in v._dirty if k[1] == self._name])

    def remove_instance(self, name: str, module: str | None = None) -> None:
        v, mod, op = self.vet, self._name, "remove_instance"
        target = f"{mod}.{name}"
        self._check_scope(op)

        def run() -> None:
            info = self._fresh()
            found = [i for i in info.instances if i.name == name]
            if not found:
                v._record(op, target, "skipped")
                return
            if len(found) > 1:
                raise VetError(f"{op} {target}: module {mod!r} has {len(found)} instances named {name!r}")
            inst = found[0]
            if module is not None and inst.module != module:
                raise DriftError(f"{op} {target}: the instance instantiates {inst.module!r}, not {module!r}")
            Instance(v, mod, name)._refuse_macro(op, inst)
            sharing = [i.name for i in info.instances if i.path == inst.path and i.stmt_start == inst.stmt_start]
            if len(sharing) > 1:
                raise VetError(
                    f"{op} {target}: the statement declares several instances ({', '.join(sharing)}); "
                    f"edit it by hand or with a raw splice"
                )
            data = v.session.text(inst.path)
            a, b = line_rule(data, inst.stmt_start, inst.stmt_end)
            snap = v._snap()
            where = [f"{base}.{name}" for base in self.paths if f"{base}.{name}" in snap]
            # what earlier operations expected of this instance is moot now it is going
            v.session.drop_expects(where)
            v._drop_registered(mod, name)
            v._drove.extend(v._driven_by(mod, inst))

            v._commit_all(
                op,
                [
                    _Plan(
                        op,
                        target,
                        path=inst.path,
                        splices=[(a, b, b"")],
                        mark=("decls", mod),
                        expects=[("instance-removed", p, "") for p in where],
                        check=_Check((op, mod, name), op, (mod, name)),
                    )
                ],
            )
            for p in where:
                v.session.permit("*", p, "*", below=True)

        v._checked(op, target, run)

    def remove_wire(self, name: str) -> None:
        v, mod, op = self.vet, self._name, "remove_wire"
        self._check_scope(op)

        def run() -> None:
            info = self._fresh()
            found = [d for d in info.decls if d.name == name]
            if not found:
                if name in info.generate_decls:
                    raise VetError(
                        f"{op} {mod}: {name!r} is declared inside a generate block, which is not supported"
                    )
                other = v._other_use(info, name, "decl")
                if other is not None:
                    raise VetError(f"{op} {mod}: {name!r} is {other}, not a net or variable")
                v._record(op, mod, "skipped")
                return
            decl = found[0]
            if len(found) > 1:
                raise VetError(f"{op} {mod}: {name!r} is declared {len(found)} times")
            v._refuse_disabled(op, mod, info, name)
            if decl.stmt_start < 0:
                raise VetError(f"{op} {mod}: the declaration of {name!r} comes from a macro or an include")
            if decl.stmt_count > 1:
                raise VetError(
                    f"{op} {mod}: the declaration of {name!r} declares {decl.stmt_count} names; "
                    f"edit it by hand or with a raw splice"
                )
            data = v.session.text(info.path)
            a, b = line_rule(data, decl.stmt_start, decl.stmt_end)

            v._commit_all(
                op,
                [
                    _Plan(
                        op,
                        mod,
                        path=info.path,
                        splices=[(a, b, b"")],
                        mark=("decls", mod),
                        check=_Check((op, mod, name), op, (mod, name)),
                    )
                ],
            )

        v._checked(op, mod, run)

    def remove_assign(self, target: str, was: object | None = None) -> None:
        v, mod, op = self.vet, self._name, "remove_assign"
        self._check_scope(op)

        def run() -> None:
            info = self._fresh()
            found = [a for a in info.assigns if a.left == target]
            if not found:
                if any(b in info.generate_assigned for b in driven_bases(target)):
                    raise VetError(
                        f"{op} {mod}: {target!r} is assigned inside a generate block, which is not supported"
                    )
                v._record(op, mod, "skipped")
                return
            if len(found) > 1:
                raise VetError(f"{op} {mod}: {len(found)} assignments to {target!r}; edit them by hand")
            assign = found[0]
            if was is not None and not _same(was, assign.right):
                raise DriftError(
                    f"{op} {mod}: {target!r} is assigned {assign.right!r}, not {str(was)!r}"
                )
            if assign.stmt_start < 0:
                raise VetError(f"{op} {mod}: the assignment to {target!r} comes from a macro or an include")
            if assign.stmt_count > 1:
                raise VetError(
                    f"{op} {mod}: the statement assigning {target!r} holds {assign.stmt_count} assignments; "
                    f"edit it by hand or with a raw splice"
                )
            data = v.session.text(info.path)
            a, b = line_rule(data, assign.stmt_start, assign.stmt_end)
            v._drove.extend((mod, base) for base in driven_bases(assign.left))

            v._commit_all(
                op,
                [
                    _Plan(
                        op,
                        mod,
                        path=info.path,
                        splices=[(a, b, b"")],
                        mark=("decls", mod),
                        check=_Check((op, mod, "assign", target), op, (mod, target)),
                    )
                ],
            )

        v._checked(op, mod, run)

    def remove_port(self, name: str) -> None:
        v, mod, op = self.vet, self._name, "remove_port"
        paths = self._check_scope(op)

        def run() -> None:
            v._refresh_all_dirty()
            info = v._info(mod)
            port = next((p for p in info.ports if p.name == name), None)
            if port is None:
                v._record(op, mod, "skipped")
                return
            if info.port_list_kind != "ansi":
                what = "a non-ANSI" if info.port_list_kind == "nonansi" else "no"
                raise VetError(f"{op} {mod}: the module has {what} port list; only ANSI port lists can be edited")
            if info.header_has_macros:
                raise VetError(f"{op} {mod}: the module header contains macros, so it cannot be edited safely")
            _refuse_directive(f"{op} {mod}", "port", info.ports_directive)
            if info.ports_close is None:
                raise VetError(f"{op} {mod}: cannot find the end of the port list")
            v._refuse_disabled(op, mod, info, name)
            data = v.session.text(info.path)
            at = info.ports.index(port)
            if at + 1 < len(info.ports):
                nxt = info.ports[at + 1]
                if tokens(data[nxt.start : nxt.end].decode("utf-8", errors="replace"))[:1] == [nxt.name]:
                    raise VetError(
                        f"{op} {mod}: port {nxt.name!r} takes its direction and type from {name!r}; "
                        f"remove or declare it first"
                    )
            items = [(p.start, p.end) for p in info.ports]
            splices = edit_list(data, items, {at}, info.ports_close, _eol(data))
            if splices is None:
                splices = [remove_all(data, items)]
            hosts = v._instances_of(mod)
            v._refuse_generate_copies(op, [mod])
            plans = [
                _Plan(
                    op,
                    mod,
                    path=info.path,
                    splices=splices,
                    mark=("ports", mod),
                    expects=[("port-removed", p, name) for p in paths],
                    permits=[("connection-changed", p, name) for p in paths],
                    check=_Check((op, mod, name), op, (mod, name)),
                )
            ]
            for host, inst in hosts:
                handle = Instance(v, host, inst.name)
                handle._refuse_connection_list(op, inst)
                conns = [(c.start, c.end) for c in inst.connections]
                drop = {i for i, c in enumerate(inst.connections) if c.port == name}
                if not drop:
                    continue
                idata = v.session.text(inst.path)
                sp = edit_list(idata, conns, drop, inst.ports_close, _eol(idata))
                if sp is None:
                    sp = [remove_all(idata, conns)]
                if port.direction in ("output", "inout"):
                    v._drove.extend(
                        (host, base) for c in inst.connections if c.port == name for base in driven_bases(c.expr)
                    )
                plans.append(
                    _Plan(op, mod, path=inst.path, splices=sp, mark=("conns", host, inst.name), quiet=True)
                )
            v._commit_all(op, plans)

        v._checked(op, mod, run)

    def remove_param(self, name: str) -> None:
        v, mod, op = self.vet, self._name, "remove_param"
        paths = self._check_scope(op)

        def run() -> None:
            v._refresh_all_dirty()
            info = v._info(mod)
            param = next((p for p in info.params if p.name == name), None)
            if param is None:
                if name in info.param_kinds:
                    raise VetError(
                        f"{op} {mod}: parameter {name!r} is declared in the module body, not in the "
                        f"parameter port list"
                    )
                v._record(op, mod, "skipped")
                return
            if info.header_has_macros:
                raise VetError(f"{op} {mod}: the module header contains macros, so it cannot be edited safely")
            _refuse_directive(f"{op} {mod}", "parameter", info.params_directive)
            if info.params_close is None or info.params_open is None or param.decl_start < 0:
                raise VetError(f"{op} {mod}: cannot find the parameter port list")
            if param.decl_count > 1:
                raise VetError(
                    f"{op} {mod}: the declaration of {name!r} declares {param.decl_count} names; "
                    f"edit it by hand or with a raw splice"
                )
            v._refuse_disabled(op, mod, info, name)
            data = v.session.text(info.path)
            decls: list[tuple[int, int]] = []
            for p in info.params:
                if (p.decl_start, p.decl_end) not in decls:
                    decls.append((p.decl_start, p.decl_end))
            at = decls.index((param.decl_start, param.decl_end))
            splices = edit_list(data, decls, {at}, info.params_close, _eol(data))
            if splices is None:
                if data[info.name_end : info.params_open].strip(b" \t\r\n") == b"":
                    splices = [(info.name_end, info.params_close + 1, _list_gap(data, info.params_close))]
                else:
                    splices = [remove_all(data, decls)]
            hosts = v._instances_of(mod)
            plans = [
                _Plan(
                    op,
                    mod,
                    path=info.path,
                    splices=splices,
                    mark=("params", mod),
                    expects=[("param-changed", p, name) for p in paths],
                    check=_Check((op, mod, name), op, (mod, name)),
                )
            ]
            for host, inst in hosts:
                handle = Instance(v, host, inst.name)
                handle._guard_overrides(op, inst)
                drop = {i for i, o in enumerate(inst.param_overrides) if o.name == name}
                if not drop:
                    continue
                assert inst.params_close is not None  # noqa: S101 - it has overrides, so a parameter list
                idata = v.session.text(inst.path)
                items = [(o.start, o.end) for o in inst.param_overrides]
                sp = edit_list(idata, items, drop, inst.params_close, _eol(idata))
                if sp is None:
                    sp = [(inst.module_end, inst.params_close + 1, _list_gap(idata, inst.params_close))]
                plans.append(
                    _Plan(op, mod, path=inst.path, splices=sp, mark=("overrides", host, inst.name), quiet=True)
                )
            v._commit_all(op, plans)

        v._checked(op, mod, run)


# --- Instance handle ---------------------------------------------------------------


class Instance:
    def __init__(self, vet: Vet, module: str, name: str, instances: int | None = None) -> None:
        self.vet = vet
        self._mod = module
        self._name = name
        self._instances = instances  # the confirmed number of copies of the enclosing module

    @property
    def name(self) -> str:
        return self._name

    @property
    def target(self) -> str:
        return f"{self._mod}.{self._name}"

    @property
    def info(self) -> InstanceInfo:
        self.vet._sync()
        keys = [k for k in self.vet._dirty if len(k) == 3 and k[1:] == (self._mod, self._name)]
        self.vet._refresh_if(keys)
        return self.vet._instance_info(self._mod, self._name)

    def __getattr__(self, attr: str) -> Any:
        if attr.startswith("_"):
            raise AttributeError(attr)
        return getattr(self.info, attr)

    @property
    def paths(self) -> list[str]:
        v = self.vet
        inst = v._instance_info(self._mod, self._name)
        snap = v._snap()
        swapped = v._swapped.get((self._mod, self._name), ())
        out = []
        for base in v.module_paths(self._mod):
            p = f"{base}.{self._name}"
            found = snap.get(p)
            if found is not None and (found.module == inst.module or found.module in swapped):
                out.append(p)
        return sorted(out)

    def allow_changes_below(self) -> None:
        """Permit (not require) any structural change strictly below this instance."""
        for p in self.paths:
            self.vet.session.permit("*", p, "*", below=True)

    # --- lookup helpers

    def _lookup(self, own: tuple[str, ...], module_kinds: tuple[str, ...]) -> tuple[InstanceInfo, ModuleInfo]:
        v = self.vet
        inst = v._instance_info(self._mod, self._name)
        keys = [(k, self._mod, self._name) for k in own] + [(k, inst.module) for k in module_kinds]
        v._refresh_if(keys)
        inst = v._instance_info(self._mod, self._name)
        return inst, v._info(inst.module)

    def _paths(self, op: str) -> list[str]:
        owners = self.vet.module_paths(self._mod)
        if len(owners) > 1 and self._instances != len(owners):
            raise VetError(
                f"{op} {self.target}: the enclosing module is elaborated {len(owners)} times "
                f"({', '.join(owners)}) and an instance edit changes every copy; "
                f"pass instances={len(owners)} to vet.module({self._mod!r}) to confirm"
            )
        paths = self.paths
        if not paths:
            raise VetError(f"{op} {self.target}: the instance is not elaborated under the top at a known path")
        return paths

    def _refuse_macro(self, op: str, inst: InstanceInfo) -> None:
        if inst.from_macro:
            raise VetError(f"{op} {self.target}: the instance comes from a macro and cannot be edited")
        if inst.in_generate:
            raise VetError(f"{op} {self.target}: the instance is inside a generate block, which is not supported")
        if inst.is_array:
            raise VetError(f"{op} {self.target}: the instance is an instance array, which is not supported")

    def _refuse_connection_list(self, op: str, inst: InstanceInfo) -> None:
        self._refuse_macro(op, inst)
        if any(c.port is None or c.port == "*" for c in inst.connections):
            raise VetError(f"{op} {self.target}: the instance uses positional or .* connections")
        _refuse_directive(f"{op} {self.target}", "connection", inst.conn_directive)

    def _conn(self, inst: InstanceInfo, port: str) -> ConnInfo | None:
        return next((c for c in inst.connections if c.port == port), None)

    def _port_check(self, op: str, inst: InstanceInfo, im: ModuleInfo, port: str) -> None:
        if not any(p.name == port for p in im.ports):
            raise VetError(
                f"{op} {self.target}: module {inst.module!r} has no port {port!r}"
                f"{_did_you_mean(port, [p.name for p in im.ports])}"
            )

    def _is_interface_port(self, port: str) -> bool:
        snap = self.vet._snap()
        return any(
            (found := snap.get(p)) is not None and found.ports.get(port, ("",))[0] == "interface"
            for p in self.paths
        )

    def _tie_message(self, port: str, inst: InstanceInfo, cur: ConnInfo | None) -> str:
        hint = f", was={cur.expr!r}" if cur is not None and tokens(cur.expr) else ""
        return (
            f"cannot disconnect input port {port!r} of {self.target}: the input would be left floating. "
            f"Either tie it to a value with connect({port!r}, <value>{hint}), "
            f"or remove the port from module {inst.module!r}."
        )

    # --- operations

    def connect(self, port: str, expr: object, was: object | None = None) -> None:
        v = self.vet
        inst, im = self._lookup(("conns",), ("ports",))
        self._refuse_connection_list("connect", inst)
        paths = self._paths("connect")
        self._port_check("connect", inst, im, port)
        v._commit(self._plan_connect(inst, im, paths, port, expr, was))

    def _plan_connect(
        self, inst: InstanceInfo, im: ModuleInfo, paths: list[str], port: str, expr: object, was: object | None
    ) -> _Plan:
        v = self.vet
        data = v.session.text(inst.path)
        cur = self._conn(inst, port)
        text = str(expr)
        if not tokens(text):
            if im.port(port).direction == "input" and not self._is_interface_port(port):
                raise VetError(self._tie_message(port, inst, cur))
            raise VetError(
                f"connect {self.target}: the expression for port {port!r} is empty; "
                f"use disconnect({port!r}) to leave an output unconnected"
            )
        if cur is not None and tokens(cur.expr):
            if _same(cur.expr, text):
                return _Plan("connect", self.target, skip=True)
            if was is None or not _same(was, cur.expr):
                note = "" if was is None else f" (was={str(was)!r} does not match)"
                raise DriftError(
                    f"connect {self.target} {port}: currently connected to {cur.expr!r}, not {text!r}{note}"
                )
        if cur is not None:
            if cur.open is not None:
                splices = [(cur.open + 1, cur.end - 1, text.encode())]
            else:  # `.p`: the implicit connection becomes explicit
                splices = [(cur.start, cur.end, f".{port}({text})".encode())]
        else:
            item = f".{port:<{_name_width(inst.connections)}}({text})".encode()
            if inst.connections:
                last = inst.connections[-1]
                splices = _append_to_list(data, last.start, last.end, inst.ports_close, item, _eol(data))
            else:
                splices = [(inst.ports_open + 1, inst.ports_open + 1, item)]
        return _Plan(
            "connect",
            self.target,
            path=inst.path,
            splices=splices,
            mark=("conns", self._mod, self._name),
            expects=[("connection-changed", p, port) for p in paths],
            check=_Check(("conn", self._mod, self._name, port), "conn", (self._mod, self._name, port, text)),
        )

    def disconnect(self, port: str, was: object | None = None) -> None:
        v = self.vet
        inst, im = self._lookup(("conns",), ("ports",))
        self._refuse_connection_list("disconnect", inst)
        paths = self._paths("disconnect")
        self._port_check("disconnect", inst, im, port)
        pinfo = im.port(port)
        cur = self._conn(inst, port)
        if self._is_interface_port(port):
            raise VetError(f"disconnect {self.target}: port {port!r} is an interface port")
        if pinfo.direction == "input":
            raise VetError(self._tie_message(port, inst, cur))
        if pinfo.direction not in ("output", "inout"):
            article = "an" if pinfo.direction[:1] in "aeiou" else "a"
            raise VetError(f"disconnect {self.target}: port {port!r} is {article} {pinfo.direction} port")
        if cur is None or not tokens(cur.expr):
            v._record("disconnect", self.target, "skipped")
            return
        if was is not None and not _same(was, cur.expr):
            raise DriftError(
                f"disconnect {self.target} {port}: currently connected to {cur.expr!r}, not {str(was)!r}"
            )
        if cur.open is not None:
            splices = [(cur.open + 1, cur.end - 1, b"")]
        else:
            splices = [(cur.start, cur.end, f".{port}()".encode())]
        v._queue(inst.path, splices)
        v._mark("conns", self._mod, self._name)
        for p in paths:
            v.session.expect("connection-changed", p, port, exact=True)
        v._set_check(_Check(("conn", self._mod, self._name, port), "conn", (self._mod, self._name, port, None)))
        v._record("disconnect", self.target, "applied")

    def set_param(self, name: str, value: object, was: object | None = None) -> None:
        v = self.vet
        inst, im = self._lookup(("overrides",), ("params",))
        self._guard_overrides("set_param", inst)
        paths = self._paths("set_param")
        kind = im.param_kinds.get(name)
        if kind is None:
            values = [n for n, k in im.param_kinds.items() if k == "value"]
            raise VetError(
                f"set_param {self.target}: module {inst.module!r} has no parameter {name!r}"
                f"{_did_you_mean(name, values)}"
            )
        if kind != "value":
            what = "a localparam" if kind == "local" else "a type parameter"
            raise VetError(
                f"set_param {self.target}: parameter {name!r} of module {inst.module!r} is {what}, "
                f"not an overridable value"
            )
        v._commit(self._plan_set_param(inst, paths, name, value, was))

    def _guard_overrides(self, op: str, inst: InstanceInfo) -> None:
        v = self.vet
        self._refuse_macro(op, inst)
        mi = v._info(self._mod)
        if any(o.name is None for o in inst.param_overrides):
            raise VetError(f"{op} {self.target}: the instance uses positional parameter overrides")
        _refuse_directive(f"{op} {self.target}", "parameter override", inst.override_directive)
        if sum(1 for i in mi.instances if i.path == inst.path and i.stmt_start == inst.stmt_start) > 1:
            raise VetError(f"{op} {self.target}: the instance shares its parameter list with others")

    def _plan_set_param(
        self,
        inst: InstanceInfo,
        paths: list[str],
        name: str,
        value: object,
        was: object | None,
        *,
        force: bool = False,
        permit: bool = True,
    ) -> _Plan:
        """`force`: replace a different existing override without `was`; `permit`: allow the value change."""
        v = self.vet
        data = v.session.text(inst.path)
        text = str(value)
        cur = next((o for o in inst.param_overrides if o.name == name), None)
        if cur is not None:
            if _same(cur.value, text):
                return _Plan("set_param", self.target, skip=True)
            if not force and (was is None or not _same(was, cur.value)):
                note = "" if was is None else f" (was={str(was)!r} does not match)"
                raise DriftError(
                    f"set_param {self.target} {name}: currently {cur.value!r}, not {text!r}{note}"
                )
            if cur.open is not None:
                splices = [(cur.open + 1, cur.end - 1, text.encode())]
            else:
                splices = [(cur.start, cur.end, f".{name}({text})".encode())]
        elif inst.params_close is None:
            splices = [(inst.module_end, inst.module_end, f" #(.{name}({text}))".encode())]
        else:
            item = f".{name:<{_name_width(inst.param_overrides)}}({text})".encode()
            if inst.param_overrides:
                last = inst.param_overrides[-1]
                splices = _append_to_list(data, last.start, last.end, inst.params_close, item, _eol(data))
            else:
                splices = [(inst.params_close, inst.params_close, item)]
        return _Plan(
            "set_param",
            self.target,
            path=inst.path,
            splices=splices,
            mark=("overrides", self._mod, self._name),
            permits=[("param-changed", p, name) for p in paths] if permit else [],
            check=_Check(("param", self._mod, self._name, name), "param", (self._mod, self._name, name, text)),
        )

    # --- replace_module (a checked operation)

    def replace_module(
        self,
        module: str,
        *,
        ports: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        connect: dict[str, object] | None = None,
        set_params: dict[str, object] | None = None,
        drop_ports: Iterable[str] = (),
        drop_params: Iterable[str] = (),
        unconnected: Iterable[str] = (),
        was: str | None = None,
    ) -> None:
        """Instantiate `module` instead of the current one; see `tests/test_instances.py`."""
        args = (
            module,
            dict(ports or {}),
            dict(params or {}),
            {k: str(v) for k, v in (connect or {}).items()},
            {k: str(v) for k, v in (set_params or {}).items()},
            list(drop_ports),
            list(drop_params),
            list(unconnected),
            was,
        )
        self.vet._checked("replace_module", self.target, lambda: self._replace(*args), tried=True)

    def _replace(
        self,
        module: str,
        ports: dict[str, str],
        params: dict[str, str],
        connect: dict[str, str],
        set_params: dict[str, str],
        drop_ports: list[str],
        drop_params: list[str],
        unconnected: list[str],
        was: str | None,
    ) -> None:
        v, op, target = self.vet, "replace_module", self.target
        inst = v._instance_info(self._mod, self._name)
        self._refuse_connection_list(op, inst)
        host = v._info(self._mod)
        if sum(1 for i in host.instances if i.path == inst.path and i.stmt_start == inst.stmt_start) > 1:
            raise VetError(
                f"{op} {target}: the statement declares several instances, which would all change module; "
                f"edit it by hand or with a raw splice"
            )
        if any(o.name is None for o in inst.param_overrides):
            raise VetError(f"{op} {target}: the instance uses positional parameter overrides")
        _refuse_directive(f"{op} {target}", "parameter override", inst.override_directive)
        known = v.session.loaded.modules()
        if module not in known:
            raise VetError(f"{op} {target}: no module {module!r} in the design{_did_you_mean(module, known)}")
        cur = inst.module

        if cur == module:
            # Only the new side is compared: the maps describe the module that is gone.
            for port, expr in connect.items():
                c = self._conn(inst, port)
                if c is None:
                    raise DriftError(
                        f"{op} {target}: the instance already instantiates {module!r} but has no connection for "
                        f"{port!r} (expected {expr!r})"
                    )
                if not _same(c.expr, expr):
                    raise DriftError(
                        f"{op} {target}: connection {port!r} is {c.expr!r}, not {expr!r}"
                    )
            for name, value in set_params.items():
                o = next((o for o in inst.param_overrides if o.name == name), None)
                if o is None:
                    raise DriftError(
                        f"{op} {target}: the instance already instantiates {module!r} but does not override "
                        f"{name!r} (expected {value!r})"
                    )
                if not _same(o.value, value):
                    raise DriftError(f"{op} {target}: override {name!r} is {o.value!r}, not {value!r}")
            v._record(op, target, "skipped")
            return
        if was is not None and cur != was:
            raise DriftError(f"{op} {target}: the instance instantiates {cur!r}, not {was!r} (or {module!r})")
        paths = self._paths(op)

        old_info, new_info = v._info(cur), v._info(module)
        for mi in (old_info, new_info):
            if mi.port_list_kind == "nonansi":
                raise VetError(f"{op} {target}: module {mi.name!r} has a non-ANSI port list")
            odd = [p.name for p in mi.ports if p.direction not in _DIRECTIONS]
            if odd:
                raise VetError(
                    f"{op} {target}: module {mi.name!r} has an interface or ref port "
                    f"({', '.join(repr(n) for n in odd)})"
                )

        old_conns = {c.port: c for c in inst.connections if c.port is not None}
        old_ovr = {o.name: o for o in inst.param_overrides if o.name is not None}
        new_ports = {p.name: p for p in new_info.ports}
        new_values = [n for n, k in new_info.param_kinds.items() if k == "value"]
        problems: list[str] = []

        for what, names, pool in (
            ("ports", ports, old_conns),
            ("drop_ports", drop_ports, old_conns),
            ("params", params, old_ovr),
            ("drop_params", drop_params, old_ovr),
        ):
            for n in names:
                if n not in pool:
                    kind = "connection" if "port" in what else "parameter override"
                    problems.append(f"{what} names {n!r}, which is not a {kind} of {target}{_did_you_mean(n, pool)}")
        for n in ports:
            if n in drop_ports:
                problems.append(f"connection {n!r} is both renamed and dropped")
        for n in params:
            if n in drop_params:
                problems.append(f"override {n!r} is both renamed and dropped")
        for old, new in ports.items():
            if new not in new_ports:
                problems.append(
                    f"ports maps {old!r} to {new!r}, which is not a port of module {module!r}"
                    f"{_did_you_mean(new, new_ports)}"
                )
        for old, new in params.items():
            if new not in new_values:
                problems.append(
                    f"params maps {old!r} to {new!r}, which is not an overridable value parameter of module "
                    f"{module!r}{_did_you_mean(new, new_values)}"
                )
        for n, expr in connect.items():
            if n not in new_ports:
                problems.append(
                    f"connect names {n!r}, which is not a port of module {module!r}{_did_you_mean(n, new_ports)}"
                )
            elif not tokens(expr):
                problems.append(f"connect gives port {n!r} an empty expression; use unconnected= for an output")
        for n in unconnected:
            if n not in new_ports:
                problems.append(
                    f"unconnected names {n!r}, which is not a port of module {module!r}{_did_you_mean(n, new_ports)}"
                )
            elif new_ports[n].direction == "input":
                problems.append(f"unconnected names {n!r}, an input port, which cannot be left unconnected")
        for n in set_params:
            if n not in new_values:
                pkind = new_info.param_kinds.get(n)
                why = "is not a parameter" if pkind is None else "is not an overridable value parameter"
                problems.append(
                    f"set_params names {n!r}, which {why} of module {module!r}{_did_you_mean(n, new_values)}"
                )

        landing: dict[str, list[str]] = {}
        for n in old_conns:
            if n in drop_ports:
                continue
            if n in ports:
                landing.setdefault(ports[n], []).append(f"connection {n!r}")
            elif n in new_ports:
                landing.setdefault(n, []).append(f"connection {n!r}")
            else:
                problems.append(
                    f"connection {n!r} is not accounted for: module {module!r} has no port {n!r} "
                    f"(rename it with ports= or remove it with drop_ports=)"
                )
        for n in connect:
            landing.setdefault(n, []).append("connect")
        for n in unconnected:
            landing.setdefault(n, []).append("unconnected")
        for t, who in landing.items():
            if len(who) > 1:
                problems.append(f"{' and '.join(who)} both land on port {t!r}")
        for n in new_ports:
            if n not in landing:
                problems.append(
                    f"port {n!r} of module {module!r} is not accounted for: connect it with connect= "
                    f"(an output or inout may be listed in unconnected=)"
                )

        carried: dict[str, list[tuple[str, str]]] = {}  # new parameter -> [(how, old name)]
        for n in old_ovr:
            if n in drop_params:
                continue
            if n in params:
                carried.setdefault(params[n], []).append(("renamed", n))
            elif n in new_values:
                carried.setdefault(n, []).append(("kept", n))
            else:
                problems.append(
                    f"override {n!r} is not accounted for: module {module!r} has no overridable parameter {n!r} "
                    f"(rename it with params= or remove it with drop_params=)"
                )
        for t, olds in carried.items():
            if len(olds) > 1:
                problems.append(f"overrides {' and '.join(repr(n) for _, n in olds)} both land on parameter {t!r}")
        for n in set_params:
            if any(how == "renamed" for how, _ in carried.get(n, [])):
                problems.append(f"set_params and the renamed override both land on parameter {n!r}")
        if problems:
            raise VetError(f"{op} {target}: " + "; ".join(problems))

        data = v.session.text(inst.path)
        eol = _eol(data)
        splices: list[tuple[int, int, bytes]] = []
        if data[inst.module_start : inst.module_end] != cur.encode():
            raise VetError(f"{op} {target}: cannot find the module name in the text")
        splices.append((inst.module_start, inst.module_end, module.encode()))

        # connections
        conns = inst.connections
        drop = {i for i, c in enumerate(conns) if c.port in drop_ports}
        w = _common_width(
            _renamed_field(c.start, c.open, c.port or "", ports.get(c.port or ""))
            for i, c in enumerate(conns)
            if i not in drop
        )
        fresh = [f".{n:<{w}}({e})".encode() for n, e in connect.items()]
        fresh += [f".{n:<{w}}()".encode() for n in unconnected]
        items = [(c.start, c.end) for c in conns]
        if conns:
            sp = edit_list(data, items, drop, inst.ports_close, eol, fresh)
            if sp is None:
                sp = [(items[0][0], items[-1][1], b", ".join(fresh))] if fresh else [remove_all(data, items)]
            splices += sp
        elif fresh:
            splices.append((inst.ports_open + 1, inst.ports_open + 1, b", ".join(fresh)))
        for i, c in enumerate(conns):
            if i in drop or c.port not in ports:
                continue
            assert c.port is not None  # noqa: S101 - `c.port in ports` above, and None is never a key
            if c.open is None:
                splices.append((c.start, c.end, f".{ports[c.port]}({c.port})".encode()))
                continue
            a = c.start + 1
            if data[a : a + len(c.port)] != c.port.encode():
                raise VetError(f"{op} {target}: cannot find the name of connection {c.port!r} in the text")
            splices.append((a, a + len(c.port), ports[c.port].encode()))

        # parameter overrides, then the added ones
        ovs = inst.param_overrides
        odrop = {i for i, o in enumerate(ovs) if o.name in drop_params}
        replaced = {
            o.name: o for i, o in enumerate(ovs) if i not in odrop and o.name not in params and o.name in set_params
        }
        w = _common_width(
            _renamed_field(o.start, o.open, o.name or "", params.get(o.name or ""))
            for i, o in enumerate(ovs)
            if i not in odrop
        )
        fresh = [f".{n:<{w}}({x})".encode() for n, x in set_params.items() if n not in replaced]
        oitems = [(o.start, o.end) for o in ovs]
        if ovs:
            assert inst.params_close is not None  # noqa: S101 - overrides exist, so the list has its paren
            sp = edit_list(data, oitems, odrop, inst.params_close, eol, fresh)
            if sp is None:
                whole = b" #(" + b", ".join(fresh) + b")" if fresh else _list_gap(data, inst.params_close)
                sp = [(inst.module_end, inst.params_close + 1, whole)]
            splices += sp
        elif fresh:
            if inst.params_close is None:
                splices.append((inst.module_end, inst.module_end, b" #(" + b", ".join(fresh) + b")"))
            else:
                splices.append((inst.params_close, inst.params_close, b", ".join(fresh)))
        for i, o in enumerate(ovs):
            if i in odrop:
                continue
            assert o.name is not None  # noqa: S101 - positional overrides were refused above
            if o.name in replaced:
                text = set_params[o.name].encode()
                splices.append(
                    (o.open + 1, o.end - 1, text)
                    if o.open is not None
                    else (o.start, o.end, f".{o.name}({set_params[o.name]})".encode())
                )
            elif o.name in params:
                if o.open is None:
                    splices.append((o.start, o.end, f".{params[o.name]}({o.name})".encode()))
                    continue
                a = o.start + 1
                if data[a : a + len(o.name)] != o.name.encode():
                    raise VetError(f"{op} {target}: cannot find the name of override {o.name!r} in the text")
                splices.append((a, a + len(o.name), params[o.name].encode()))

        mod_name, inst_name = self._mod, self._name

        before = v.session.loaded
        v._commit_all(
            op,
            [
                _Plan(
                    op,
                    target,
                    path=inst.path,
                    splices=splices,
                    mark=("conns", mod_name, inst_name),
                    expects=[("module-changed", p, "*") for p in paths],
                    permits=[("*", p, "*") for p in paths],
                    check=_Check((op, mod_name, inst_name), op, (mod_name, inst_name, module)),
                )
            ],
        )
        v._mark("overrides", mod_name, inst_name)
        for p in paths:
            v.session.permit("*", p, "*", below=True)
        v._swapped.setdefault((mod_name, inst_name), set()).add(cur)

        # The checks of this operation alone, on the edited design (slang's own
        # PortWidthExpand and PortWidthTruncate do not name the port).
        try:
            v.session.refresh()
        except AdapterError as e:
            raise VetError(f"{op} {target}: the edited design cannot be loaded: {e}") from e
        v._sync()
        after = v.session.loaded
        for p in paths:
            old_snap, new_snap = before.instance_snap(p), after.instance_snap(p)
            if new_snap is None:
                raise VetError(f"{op} {target}: there is no instance at {p} in the edited design")
            if old_snap is None:
                continue
            wrong: list[str] = []
            for old in old_conns:
                if old in drop_ports:
                    continue
                new = ports.get(old, old)
                x, y = old_snap.ports.get(old), new_snap.ports.get(new)
                if x is None or y is None:
                    continue
                tx, ty = old_snap.port_types.get(old), new_snap.port_types.get(new)
                if x[0] != y[0]:
                    wrong.append(f"port {old!r} of {cur!r} is an {x[0]} but port {new!r} of {module!r} is an {y[0]}")
                elif x[1] != y[1] and x[1] > 0 and y[1] > 0:
                    wrong.append(
                        f"port {old!r} of {cur!r} is {x[1]} bits wide but port {new!r} of {module!r} "
                        f"is {y[1]} bits wide"
                    )
                elif tx is not None and ty is not None and _type_key(tx) != _type_key(ty):
                    wrong.append(f"port {old!r} of {cur!r} has type {tx} but port {new!r} of {module!r} has type {ty}")
            if wrong:
                raise VetError(f"{op} {target}: " + "; ".join(wrong))
            for n in new_values:
                if n in set_params:
                    continue
                src = carried[n][0][1] if n in carried else n  # the parameter it takes over from
                if src == n and old_info.param_kinds.get(n) != "value":
                    continue
                xv, yv = old_snap.params.get(src), new_snap.params.get(n)
                if xv is not None and yv is not None and not _same_value(xv, yv):
                    raise VetError(
                        f"{op} {target}: parameter {n!r} would change from {xv} to {yv}: {module!r} takes "
                        f"the value differently from {cur!r}; keep the value with set_params={{{n!r}: ...}} "
                        f"or accept the new one with an explicit set_params"
                    )
        for old in drop_ports:
            conn = old_conns[old]
            direction = next((q.direction for q in old_info.ports if q.name == old), None)
            if direction in ("output", "inout"):
                v._drove.extend((self._mod, base) for base in driven_bases(conn.expr))
