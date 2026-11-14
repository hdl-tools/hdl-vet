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
from dataclasses import dataclass
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
        if info.port_list_kind != "ansi":
            what = "a non-ANSI" if info.port_list_kind == "nonansi" else "no"
            raise VetError(f"add_port {mod}: the module has {what} port list; only ANSI port lists can be extended")
        if info.header_has_macros:
            raise VetError(f"add_port {mod}: the module header contains macros, so it cannot be edited safely")
        data = v.session.text(info.path)
        existing = next((p for p in info.ports if p.name == name), None)
        if existing is not None:
            found = data[existing.start : existing.end].decode("utf-8", errors="replace")
            if _same(found, decl):
                v._record("add_port", mod, "skipped")
                return
            raise DriftError(f"add_port {mod}: port {name!r} exists as {found!r}, not {decl!r}")
        other = v._other_use(info, name, "port")
        if other is not None:
            raise DriftError(f"add_port {mod}: the name {name!r} is already used by {other}")
        added = v._added.get(mod, {}).get(name)
        if added is not None:
            if added == ("port", _norm(decl)):
                v._record("add_port", mod, "skipped")
                return
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
        v._queue(info.path, splices)
        v._mark("ports", mod)
        v._added.setdefault(mod, {})[name] = ("port", _norm(decl))
        for p in paths:
            v.session.expect("port-added", p, name, exact=True)
            v.session.permit("connection-changed", p, name, exact=True)

        def check(design: Design) -> str | None:
            try:
                m = design.module(mod)
            except AdapterError:
                return f"add_port {mod} {name}: module not found in the final design"
            port = next((p for p in m.ports if p.name == name), None)
            if port is None:
                return f"add_port {mod} {name}: port not found in the final design"
            text = v.session.text(m.path)[port.start : port.end].decode("utf-8", errors="replace")
            if not _same(text, decl):
                return f"add_port {mod} {name}: expected {decl!r}, found {text!r}"
            return None

        v._set_check(("add_port", mod, name), check)
        v._record("add_port", mod, "applied")

    def add_param(self, name: str, default: object, type: str = "int") -> None:
        v, mod = self.vet, self._name
        paths = self._check_scope("add_param")
        decl = f"parameter {type} {name} = {default}"
        info = v._info(mod, [("params", mod)])
        if info.header_has_macros:
            raise VetError(f"add_param {mod}: the module header contains macros, so it cannot be edited safely")
        existing = next((p for p in info.params if p.name == name), None)
        if existing is not None:
            if (
                info.param_kinds.get(name) == "value"
                and _same(existing.type_text, type)
                and _same(existing.default or "", default)
            ):
                v._record("add_param", mod, "skipped")
                return
            raise DriftError(
                f"add_param {mod}: parameter {name!r} exists as type {existing.type_text!r} with default "
                f"{existing.default!r}, not {type!r} with default {str(default)!r}"
            )
        if name in info.param_kinds:
            raise DriftError(f"add_param {mod}: the name {name!r} is already used by a parameter in the module body")
        other = v._other_use(info, name, "param")
        if other is not None:
            raise DriftError(f"add_param {mod}: the name {name!r} is already used by {other}")
        added = v._added.get(mod, {}).get(name)
        if added is not None:
            if added == ("param", _norm(decl)):
                v._record("add_param", mod, "skipped")
                return
            raise DriftError(f"add_param {mod}: parameter {name!r} was already added with a different default")
        _refuse_directive(f"add_param {mod}", "parameter", info.params_directive)
        data = v.session.text(info.path)
        item = decl.encode()
        if info.params_close is None:
            at = info.name_end if info.imports_end is None else info.imports_end
            splices = [(at, at, b" #(" + item + b")")]
        elif not info.params:
            splices = [(info.params_close, info.params_close, item)]
        else:
            last = info.params[-1]
            splices = _append_to_list(data, last.start, last.end, info.params_close, item, _eol(data))
        v._queue(info.path, splices)
        v._mark("params", mod)
        v._added.setdefault(mod, {})[name] = ("param", _norm(decl))
        self._expect_param(name, paths)

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

        v._set_check(("add_param", mod, name), check)
        v._record("add_param", mod, "applied")

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
        ports = {p.name: p for p in info.ports}
        names = set(ports) | {d.name for d in info.decls}
        if target not in names:
            raise VetError(
                f"add_assign {mod}: no net, variable or port {target!r}{_did_you_mean(target, names)}"
            )
        if any(a.left == target and _same(a.right, expr) for a in info.assigns):
            v._record("add_assign", mod, "skipped")
            return
        if target in ports and ports[target].direction == "input":
            raise VetError(f"add_assign {mod}: {target!r} is an input port and cannot be assigned")
        try:
            drivers = v.session.loaded.drivers_in(mod, target)
        except AdapterError as e:
            raise VetError(f"add_assign {mod}: cannot analyse {target!r}: {e}") from e
        if drivers:
            what = "an input port" if any(d.is_input_port for d in drivers) else "already driven"
            raise VetError(f"add_assign {mod}: {target!r} is {what} and cannot be assigned")
        data = v.session.text(info.path)
        ls = _line_start(data, info.body_end)
        if data[ls : info.body_end].strip() != b"":
            raise VetError(f"add_assign {mod}: endmodule does not start its line")
        text = info.body_indent.encode() + f"assign {target} = {expr};".encode() + _eol(data)
        v._queue(info.path, [(ls, ls, text)])
        v._mark("decls", mod)

        def check(design: Design) -> str | None:
            try:
                m = design.module(mod)
            except AdapterError:
                return f"add_assign {mod} {target}: module not found in the final design"
            if not any(a.left == target and _same(a.right, expr) for a in m.assigns):
                return f"add_assign {mod} {target}: the assignment 'assign {target} = {expr};' is missing"
            return None

        v._set_check(("add_assign", mod, target, _norm(expr)), check)
        v._record("add_assign", mod, "applied")


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
                v._record("connect", self.target, "skipped")
                return
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
        v._queue(inst.path, splices)
        v._mark("conns", self._mod, self._name)
        for p in paths:
            v.session.expect("connection-changed", p, port, exact=True)
        v._set_check(
            ("conn", self._mod, self._name, port),
            self._conn_check("connect", port, lambda f: f is not None and _same(f, text), repr(text)),
        )
        v._record("connect", self.target, "applied")

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
        self._refuse_macro("set_param", inst)
        mi = v._info(self._mod)
        if any(o.name is None for o in inst.param_overrides):
            raise VetError(f"set_param {self.target}: the instance uses positional parameter overrides")
        _refuse_directive(f"set_param {self.target}", "parameter override", inst.override_directive)
        if sum(1 for i in mi.instances if i.path == inst.path and i.stmt_start == inst.stmt_start) > 1:
            raise VetError(f"set_param {self.target}: the instance shares its parameter list with others")
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
        data = v.session.text(inst.path)
        text = str(value)
        cur = next((o for o in inst.param_overrides if o.name == name), None)
        if cur is not None:
            if _same(cur.value, text):
                v._record("set_param", self.target, "skipped")
                return
            if was is None or not _same(was, cur.value):
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
        v._queue(inst.path, splices)
        v._mark("overrides", self._mod, self._name)
        for p in paths:
            v.session.permit("param-changed", p, name, exact=True)
        mod, iname = self._mod, self._name

        def check(design: Design) -> str | None:
            try:
                found = design.module(mod).instance(iname).param_override(name).value
            except AdapterError:
                return f"set_param {mod}.{iname} {name}: override not found in the final design"
            if not _same(found, text):
                return f"set_param {mod}.{iname} {name}: expected {text!r}, found {found!r}"
            return None

        v._set_check(("param", self._mod, self._name, name), check)
        v._record("set_param", self.target, "applied")
