"""`Vet.add_file` and the plans of the underlying operations."""

from __future__ import annotations

import os
from pathlib import Path

from hdl_vet.ops import args
from hdl_vet.ops.common import DriftError, VetError, _Check, _Plan
from hdl_vet.ops.host import _VetHost
from hdl_vet.ops.text import (
    _append_to_list,
    _did_you_mean,
    _eol,
    _line_start,
    _norm,
    _param_decl,
    _refuse_directive,
    _same,
)
from hdl_vet.paths import same_file
from hdl_vet.session import SessionError
from hdl_vet.slang_adapter import AdapterError, InstanceInfo, ModuleInfo, driven_bases


class _PlanOps(_VetHost):
    """`Vet.add_file`, the commit of plans and the plans of add_port, add_param and add_assign."""

    def add_file(self, path: Path | str) -> None:
        """Bring a source file outside the design into it (loaded last, never edited)."""
        args.path("add_file", path)
        name = Path(path).name

        def run() -> None:
            try:
                resolved = self._script_path(path)
            except VetError as e:
                raise VetError(f"add_file {name}: {e}") from e
            try:
                added = self.session.add_file(resolved)
            except SessionError as e:
                raise VetError(f"add_file {name}: {e}") from e
            self._sync()
            if not added:
                self._record("add_file", name, "skipped")
                return
            self._set_check(_Check(("add_file", name), "add_file", (name, str(resolved.resolve()))))
            self._record("add_file", name, "applied")

        self._checked("add_file", name, run, tried=True)

    def _script_path(self, path: Path | str) -> Path:
        """A relative `path` starts from the folder of the running script when a file is there.

        Two branches look wider than they need to be, and both are there for a reason (#63).

        `Path.is_file()` hides the error for a name that is absent, but it raises for a folder
        that cannot be searched and for a name the filesystem cannot hold. A question that
        cannot be answered takes the plain route, so `Session.add_file` refuses it the way it
        did before this rule existed. Nothing is chosen on a guess.

        The test for the current folder asks whether it holds the name, not whether it holds a
        readable file. A dangling link, a pipe and a folder are all things the owner of the
        tree put there, so hdl-vet refuses instead of reaching for the file by the script.
        """
        p = Path(path)
        if p.is_absolute() or self._script_dir is None:
            return p
        beside = self._script_dir / p
        try:
            if not beside.is_file():
                return p
            here = p.exists() or p.is_symlink()
        except OSError:  # the filesystem cannot answer: the plain route reports it
            return p
        if here and not same_file(beside, p):
            raise VetError(
                f"{os.path.abspath(beside)} is next to the script and {os.path.abspath(p)} is in the current folder, "
                "and they are not one file: give an absolute path to say which one to add"
            )
        return beside

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
                c.key[1:3] == (module, name)
                and (c.key[0] in ("replace_module", "conn") or (c.key[0] == "param" and len(c.key) == 4))
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
        if plan.forgets:
            self.session.drop_expects(plan.forgets)
        if plan.drops is not None:
            self._drop_registered(*plan.drops)
        self._drove.extend(plan.drove)
        if plan.mark is not None:
            self._mark(*plan.mark)
        if plan.added is not None:
            mod, name, value = plan.added
            self._added.setdefault(mod, {})[name] = value
        if plan.retires:  # before the plan's own check is set, so a retired key is never one it sets
            self._checks = [c for c in self._checks if c.key not in plan.retires]
        for kind, path, name in plan.cancels:
            self.session.drop_expect(kind, [path], name)
        for kind, path, name in plan.expects:
            self.session.expect(kind, path, name, exact=True)
        for kind, path, name in plan.permits:
            self.session.permit(kind, path, name, exact=True)
        if plan.check is not None:
            self._set_check(plan.check)
        if not plan.quiet:
            self._record(plan.op, plan.target, "applied")

    def _plan_add_port(
        self, mod: str, name: str, decl: str, info: ModuleInfo, paths: list[str], width: str | None = None
    ) -> _Plan:
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
        self._refuse_disabled("add_port", mod, info, name)
        if info.ports_close is None:
            raise VetError(f"add_port {mod}: cannot find the end of the port list")
        eol = _eol(data)
        item = decl.encode()
        if not info.ports:
            splices = [(info.ports_close, info.ports_close, item)]
        else:
            last = info.ports[-1]
            splices = _append_to_list(data, last.start, last.end, info.ports_close, item, eol)

        base = self.session.baseline_snapshot()
        had = any(name in base[p].ports for p in paths if p in base)
        removed = set(self.session.expected_at("port-removed", name))
        return _Plan(
            "add_port",
            mod,
            path=info.path,
            splices=splices,
            mark=("ports", mod),
            added=(mod, name, ("port", _norm(decl))),
            expects=[("port-added", p, name) for p in paths if not had],
            cancels=[("port-removed", p, name) for p in paths if p in removed],
            permits=[("connection-changed", p, name) for p in paths],
            check=_Check(("port", mod, name), "add_port", (mod, name, decl, width)),
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
        self._refuse_disabled("add_param", mod, info, name)
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
            check=_Check(("param", mod, name), "add_param", (mod, name, str(default), str(type))),
        )

    def _plan_assign(self, mod: str, info: ModuleInfo, target: str, expr: object, planned_port: bool = False) -> _Plan:
        """`planned_port`: `target` is an output port this operation adds, so it is undriven."""
        if not planned_port:
            ports = {p.name: p for p in info.ports}
            names = set(ports) | {d.name for d in info.decls}
            if target not in names:
                raise VetError(f"add_assign {mod}: no net, variable or port {target!r}{_did_you_mean(target, names)}")
            if any(a.left == target and _same(a.right, expr) for a in info.assigns):
                return _Plan("add_assign", mod, skip=True)
            self._refuse_disabled("add_assign", mod, info, target)
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
            check=_Check(("assign", mod, target), "add_assign", (mod, target, str(expr))),
        )
