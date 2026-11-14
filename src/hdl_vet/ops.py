"""The script API: `Vet` and the structural operations scripts are made of.

Every operation follows one contract: if the design already shows exactly the
requested result nothing is queued and the operation is logged "skipped"; if it is
in the expected "before" state the edit is queued (guarded with `old=`), its
structural expectations are registered and a post-condition is registered that
`finish` evaluates on the final design; otherwise `DriftError`. See
`tests/test_ops.py` for the full specification.

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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from hdl_vet.session import Result, Session
from hdl_vet.slang_adapter import (
    AdapterError,
    ConnInfo,
    Design,
    InstanceInfo,
    ModuleInfo,
    ParamOverride,
    is_simple_identifier,
    tokens,
)

__all__ = ["DriftError", "Instance", "Module", "OpRecord", "Vet", "VetError"]

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_DIRECTIONS = ("input", "output", "inout")
_BASED = re.compile(r"'[sS]?[bBoOdDhH]")
_DIGITS = re.compile(r"[0-9a-fA-FxXzZ?_]+")
_NET_KEYWORDS = frozenset(
    "wire tri tri0 tri1 triand trior trireg wand wor uwire supply0 supply1 interconnect".split()
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


def _indent_of(data: bytes, offset: int) -> bytes:
    start = _line_start(data, offset)
    line = data[start:offset]
    return line[: len(line) - len(line.lstrip(b" \t"))]


def _append_to_list(
    data: bytes, last_start: int, last_end: int, close: int, item: bytes, eol: bytes
) -> list[tuple[int, int, bytes]]:
    """Splices that append `item` after the last element of a comma separated list."""
    ls = _line_start(data, close)
    if data[ls:close].strip() == b"":
        # the ")" opens its line: "," after the last element, a new line before it
        return [(last_end, last_end, b","), (ls, ls, _indent_of(data, last_start) + item + eol)]
    return [(last_end, last_end, b", " + item)]


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
    check_key: tuple[str, ...] | None = None
    check: Callable[[Design], str | None] | None = None


# --- Vet ---------------------------------------------------------------------------


class Vet:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.log: list[OpRecord] = []
        self._checks: dict[tuple[str, ...], Callable[[Design], str | None]] = {}
        self._dirty: set[tuple[str, ...]] = set()
        self._all = False
        self._added: dict[str, dict[str, tuple[str, str]]] = {}
        self._seen = session.reloads
        self._paths: dict[str, list[str]] | None = None
        self._snap_cache: dict[str, Any] | None = None

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
        return self.session.finish(scripts, list(self._checks.values()))

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

    def _set_check(self, key: tuple[str, ...], check: Callable[[Design], str | None]) -> None:
        """Register a post-condition; a later one on the same target replaces the earlier."""
        self._checks.pop(key, None)
        self._checks[key] = check

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
        design = self.session.loaded
        for host in design.modules():
            try:
                info = design.module(host)
            except AdapterError:
                continue
            for inst in info.instances:
                if inst.module in wanted and (inst.in_generate or inst.is_array):
                    what = "inside a generate block" if inst.in_generate else "an instance array"
                    raise VetError(
                        f"{op}: instance {inst.name!r} of module {inst.module!r} in module {host!r} is "
                        f"{what} (generate blocks and instance arrays are not supported): a new port could "
                        f"stay unconnected under another parameterisation"
                    )

    def _record(self, op: str, target: str, status: str) -> None:
        self.log.append(OpRecord(op, target, status))

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
        assert plan.path is not None
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
        if plan.check_key is not None and plan.check is not None:
            self._set_check(plan.check_key, plan.check)
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

        def check(design: Design) -> str | None:
            try:
                m = design.module(mod)
            except AdapterError:
                return f"add_port {mod} {name}: module not found in the final design"
            port = next((p for p in m.ports if p.name == name), None)
            if port is None:
                return f"add_port {mod} {name}: port not found in the final design"
            text = self.session.text(m.path)[port.start : port.end].decode("utf-8", errors="replace")
            if not _same(text, decl):
                return f"add_port {mod} {name}: expected {decl!r}, found {text!r}"
            return None

        return _Plan(
            "add_port",
            mod,
            path=info.path,
            splices=splices,
            mark=("ports", mod),
            added=(mod, name, ("port", _norm(decl))),
            expects=[("port-added", p, name) for p in paths],
            permits=[("connection-changed", p, name) for p in paths],
            check_key=("add_port", mod, name),
            check=check,
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

        def check(design: Design) -> str | None:
            try:
                m = design.module(mod)
            except AdapterError:
                return f"add_param {mod} {name}: module not found in the final design"
            p = next((p for p in m.params if p.name == name), None)
            if p is None or m.param_kinds.get(name) != "value":
                return f"add_param {mod} {name}: parameter not found in the final design"
            if not _same(p.default or "", default):
                return f"add_param {mod} {name}: expected default {str(default)!r}, found {p.default!r}"
            if not _same(p.type_text, type):
                return f"add_param {mod} {name}: expected type {type!r}, found {p.type_text!r}"
            return None

        return _Plan(
            "add_param",
            mod,
            path=info.path,
            splices=splices,
            mark=("params", mod),
            added=(mod, name, ("param", _norm(decl))),
            expects=[("param-changed", p, name) for p in paths],
            check_key=("add_param", mod, name),
            check=check,
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

        def check(design: Design) -> str | None:
            try:
                m = design.module(mod)
            except AdapterError:
                return f"add_assign {mod} {target}: module not found in the final design"
            if not any(a.left == target and _same(a.right, expr) for a in m.assigns):
                return f"add_assign {mod} {target}: the assignment 'assign {target} = {expr};' is missing"
            return None

        return _Plan(
            "add_assign",
            mod,
            path=info.path,
            splices=[(ls, ls, text)],
            mark=("decls", mod),
            check_key=("add_assign", mod, target, _norm(expr)),
            check=check,
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
            raise VetError(f"{op}: module {mod!r} has no net, variable or port {signal!r}{_did_you_mean(signal, names)}")
        if port is not None and self._is_interface(mod, signal):
            raise VetError(f"{op}: {signal!r} in module {mod!r} is an interface port, which cannot be punched")
        if port is not None:
            if port.unpacked:
                raise VetError(f"{op}: port {signal!r} of module {mod!r} has unpacked dimensions")
            if not port.type_text:
                raise VetError(f"{op}: cannot tell the type of port {signal!r} of module {mod!r}; pass type= or width=")
            return port.type_text
        assert decl is not None
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
        mods, paths, inames = self._walk_path(op, to)
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
        self._refuse_generate_copies(op, mods[1:])
        sig = signal if signal is not None else (name if out else None)
        self._refresh_for(mods)
        if out and sig != name:
            if any(p.name == name for p in self._info(mods[-1]).ports):
                # an assign to an existing port needs its drivers: they depend on everything queued
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
            base, pwidth = self._signal_type(op, mods[-1] if out else mods[0], infos[mods[-1] if out else mods[0]], sig), None
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
                assert sig is not None
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

            def check(design: Design) -> str | None:
                found = design.snapshot().get(to)
                after = None if found is None else found.params.get(name)
                if after != before:
                    return f"{op} {name} {to}: the value in use changed from {before!r} to {after!r}"
                return None

            self._set_check((op, name, to), check)


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
            raise VetError(f"module {self._name!r}: expected {count} matching instances, found {len(found)} ({listing})")
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

        def check(design: Design) -> str | None:
            try:
                m = design.module(mod)
            except AdapterError:
                return f"add_wire {mod} {name}: module not found in the final design"
            d = next((d for d in m.decls if d.name == name), None)
            if d is None:
                return f"add_wire {mod} {name}: declaration not found in the final design"
            if not _same(d.type_text, type_text):
                return f"add_wire {mod} {name}: expected type {type_text!r}, found {d.type_text!r}"
            return None

        v._set_check(("add_wire", mod, name), check)
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
        out = []
        for base in v.module_paths(self._mod):
            p = f"{base}.{self._name}"
            found = snap.get(p)
            if found is not None and found.module == inst.module:
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

    def _conn_check(
        self, op: str, port: str, want: Callable[[str | None], bool], expect: str
    ) -> Callable[[Design], str | None]:
        mod, name = self._mod, self._name

        def check(design: Design) -> str | None:
            try:
                c = self._conn(design.module(mod).instance(name), port)
            except AdapterError:
                return f"{op} {mod}.{name} {port}: instance not found in the final design"
            found = None if c is None else c.expr
            if not want(found):
                return f"{op} {mod}.{name} {port}: expected {expect}, found {found!r}"
            return None

        return check

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
            check_key=("conn", self._mod, self._name, port),
            check=self._conn_check("connect", port, lambda f: f is not None and _same(f, text), repr(text)),
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
        v._set_check(
            ("conn", self._mod, self._name, port),
            self._conn_check("disconnect", port, lambda f: f is None or not tokens(f), "no connection"),
        )
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
                f"set_param {self.target}: parameter {name!r} of module {inst.module!r} is {what}, not an overridable value"
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
        mod, iname = self._mod, self._name

        def check(design: Design) -> str | None:
            try:
                found = design.module(mod).instance(iname).param_override(name).value
            except AdapterError:
                return f"set_param {mod}.{iname} {name}: override not found in the final design"
            if not _same(found, text):
                return f"set_param {mod}.{iname} {name}: expected {text!r}, found {found!r}"
            return None

        return _Plan(
            "set_param",
            self.target,
            path=inst.path,
            splices=splices,
            mark=("overrides", self._mod, self._name),
            permits=[("param-changed", p, name) for p in paths] if permit else [],
            check_key=("param", self._mod, self._name, name),
            check=check,
        )
