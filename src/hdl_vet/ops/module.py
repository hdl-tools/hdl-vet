"""`Module`: the handle of a module, its ports, parameters, wires and assigns."""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from hdl_vet.listedit import edit_list, line_rule, remove_all
from hdl_vet.ops.common import DriftError, VetError, _Check, _Plan
from hdl_vet.ops.instance import Instance
from hdl_vet.ops.override import override_net as _override_net
from hdl_vet.ops.text import (
    _DIRECTIONS,
    _IDENT,
    _did_you_mean,
    _eol,
    _header_line_in_comment,
    _instance_name,
    _list_gap,
    _norm,
    _range,
    _refuse_directive,
    _same,
    _segment,
)
from hdl_vet.slang_adapter import ModuleInfo, driven_bases, is_simple_identifier, tokens

if TYPE_CHECKING:
    from hdl_vet.ops.vet import Vet


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
        name = _instance_name(name)
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

    def _confirm_instances(self, op: str) -> list[str]:
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
        v._refuse_scope("add_port", mod)
        if direction not in _DIRECTIONS:
            raise VetError(f"add_port {mod}: direction must be 'input', 'output' or 'inout', not {direction!r}")
        if not is_simple_identifier(name):
            raise VetError(f"add_port {mod}: {name!r} is not a simple identifier (or is a SystemVerilog keyword)")
        paths = self._confirm_instances("add_port")
        decl = f"{direction:<6} {type}{_range(width)} {name}"
        info = v._info(mod, [("ports", mod)])
        v._commit(v._plan_add_port(mod, name, decl, info, paths))

    def add_param(self, name: str, default: object, type: str = "int") -> None:
        v, mod = self.vet, self._name
        v._refuse_scope("add_param", mod)
        if not is_simple_identifier(name):
            raise VetError(f"add_param {mod}: {name!r} is not a simple identifier (or is a SystemVerilog keyword)")
        paths = self._confirm_instances("add_param")
        info = v._info(mod, [("params", mod)])
        v._commit(v._plan_add_param(mod, name, type, default, info, paths))

    def add_wire(self, name: str, width: int | str | None = None, type: str = "logic") -> None:
        v, mod = self.vet, self._name
        v._refuse_scope("add_wire", mod)
        self._confirm_instances("add_wire")
        if not is_simple_identifier(name):
            raise VetError(f"add_wire {mod}: {name!r} is not a simple identifier (or is a SystemVerilog keyword)")
        type_text = f"{type}{_range(width)}"
        info = v._info(mod)
        # Inside a group a `remove_wire` of this name is queued but not loaded, so `info`
        # still shows the declaration. Skipping on it signed off on a design without the
        # wire, and left the name undeclared for the next operation. The registered check of
        # the removal says so without a re-load, which would place the declaration above the
        # one an earlier call of this run added instead of below it (#57).
        gone = any(c.kind == "remove_wire" and c.key == ("wire", mod, name) for c in v._checks)
        existing = None if gone else next((d for d in info.decls if d.name == name), None)
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
        v._refuse_disabled("add_wire", mod, info, name)
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

        v._set_check(_Check(("wire", mod, name), "add_wire", (mod, name, type_text)))
        v._record("add_wire", mod, "applied")

    def add_assign(self, target: str, expr: object) -> None:
        v, mod = self.vet, self._name
        v._refuse_scope("add_assign", mod)
        self._confirm_instances("add_assign")
        if not _IDENT.fullmatch(target):
            raise VetError(f"add_assign {mod}: the target must be a plain identifier, not {target!r}")
        v.session.refresh()  # drivers depend on everything queued
        v._sync()
        info = v._info(mod)
        v._commit(v._plan_assign(mod, info, target, expr))

    def override_net(
        self, net: str, value: object, *, enable: str, keep: str | None = None, waiver: str | None = None
    ) -> None:
        """Put a mux on `net`: `assign net = enable ? value : keep;`. The driver of `net` then drives `keep`.

        `enable` is a one bit input port of the module. `keep` names the new signal that holds the original
        value (default `<net>_pre`). The sign-off proves the module equal when `enable` is 0, or `waiver`
        (a text) says why it is not needed. Without Yosys the call needs a waiver.
        """
        _override_net(self, net, value, enable, keep, waiver)

    # --- removals (checked operations)

    def _fresh(self) -> ModuleInfo:
        v = self.vet
        return v._info(self._name, [k for k in v._dirty if k[1] == self._name])

    def remove_instance(self, name: str, module: str | None = None) -> None:
        v, mod, op = self.vet, self._name, "remove_instance"
        v._refuse_scope(op, mod)
        name = _instance_name(name)
        target = f"{mod}.{name}"
        self._confirm_instances(op)

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
            where = [f"{base}.{_segment(name)}" for base in self.paths if f"{base}.{_segment(name)}" in snap]
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

    def _refuse_name(self, op: str, name: object, declared: list[str]) -> None:
        mod = self._name
        if not isinstance(name, str):
            raise VetError(f"{op} {mod}: the name must be a str, not {type(name).__name__}")
        if name.startswith("\\"):
            hint = _did_you_mean(name, declared)
            raise VetError(
                f"{op} {mod}: {name!r} is an escaped spelling; give the identifier without the backslash{hint}"
            )
        if name not in declared and not is_simple_identifier(name):
            raise VetError(f"{op} {mod}: {name!r} is not a simple identifier (or is a SystemVerilog keyword)")

    def remove_wire(self, name: str) -> None:
        v, mod, op = self.vet, self._name, "remove_wire"
        v._refuse_scope(op, mod)
        self._refuse_name(op, name, [d.name for d in self._fresh().decls])
        self._confirm_instances(op)

        def run() -> None:
            info = self._fresh()
            found = [d for d in info.decls if d.name == name]
            if not found:
                if name in info.generate_decls:
                    raise VetError(f"{op} {mod}: {name!r} is declared inside a generate block, which is not supported")
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
                        check=_Check(("wire", mod, name), op, (mod, name)),
                    )
                ],
            )

        v._checked(op, mod, run)

    def remove_assign(self, target: str, was: object | None = None) -> None:
        v, mod, op = self.vet, self._name, "remove_assign"
        v._refuse_scope(op, mod)
        self._confirm_instances(op)

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
                raise DriftError(f"{op} {mod}: {target!r} is assigned {assign.right!r}, not {str(was)!r}")
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
                        check=_Check(("assign", mod, target), op, (mod, target)),
                    )
                ],
            )

        v._checked(op, mod, run)

    def remove_port(self, name: str) -> None:
        v, mod, op = self.vet, self._name, "remove_port"
        v._refuse_scope_with_hosts(op, mod)
        self._refuse_name(op, name, [d.name for d in v._info(mod).ports])
        paths = self._confirm_instances(op)

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
            # An expectation of port-added means this run added the port, so the pair cancels
            # and neither change can be seen. The cancel goes on the plan: `run` can be tried
            # again after a re-load, so it must only read (#57). A real removal still expects
            # port-removed when the design we started from had the port.
            base = v.session.baseline_snapshot()
            had = any(name in base[p].ports for p in paths if p in base)
            added = set(v.session.expected_at("port-added", name))
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
                    expects=[("port-removed", p, name) for p in paths] if had else [],
                    cancels=[("port-added", p, name) for p in paths if p in added],
                    permits=[("connection-changed", p, name) for p in paths],
                    check=_Check(("port", mod, name), op, (mod, name)),
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
                plans.append(_Plan(op, mod, path=inst.path, splices=sp, mark=("conns", host, inst.name), quiet=True))
            v._commit_all(op, plans)

        v._checked(op, mod, run)

    def remove_param(self, name: str) -> None:
        v, mod, op = self.vet, self._name, "remove_param"
        v._refuse_scope_with_hosts(op, mod)
        self._refuse_name(op, name, [d.name for d in v._info(mod).params])
        paths = self._confirm_instances(op)

        def run() -> None:
            v._refresh_all_dirty()
            info = v._info(mod)
            param = next((p for p in info.params if p.name == name), None)
            if param is None:
                if name in info.param_kinds:
                    raise VetError(
                        f"{op} {mod}: parameter {name!r} is declared in the module body, not in the parameter port list"
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
            # If this run added the parameter, the pair cancels (#57). The baseline decides here,
            # not an expectation: `_expect_param` registers param-changed for a change that stays.
            base = v.session.baseline_snapshot()
            undone = all(name not in base[p].params for p in paths if p in base)
            hosts = v._instances_of(mod)
            plans = [
                _Plan(
                    op,
                    mod,
                    path=info.path,
                    splices=splices,
                    mark=("params", mod),
                    expects=[] if undone else [("param-changed", p, name) for p in paths],
                    cancels=[("param-changed", p, name) for p in paths] if undone else [],
                    check=_Check(("param", mod, name), op, (mod, name)),
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
