"""`Instance`: the handle of an instance, and `replace_module`."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hdl_vet.listedit import edit_list, remove_all
from hdl_vet.ops.common import DriftError, VetError, _Check, _Plan
from hdl_vet.ops.text import (
    _DIRECTIONS,
    _append_to_list,
    _common_width,
    _did_you_mean,
    _eol,
    _instance_name,
    _list_gap,
    _name_width,
    _refuse_directive,
    _renamed_field,
    _same,
    _same_value,
    _segment,
    _type_key,
)
from hdl_vet.slang_adapter import AdapterError, ConnInfo, InstanceInfo, ModuleInfo, driven_bases, tokens

if TYPE_CHECKING:
    from hdl_vet.ops.vet import Vet


# --- Instance handle ---------------------------------------------------------------


class Instance:
    def __init__(self, vet: Vet, module: str, name: str, instances: int | None = None) -> None:
        self.vet = vet
        self._mod = module
        self._name = _instance_name(name)
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
            p = f"{base}.{_segment(self._name)}"
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
            (found := snap.get(p)) is not None and found.ports.get(port, ("",))[0] == "interface" for p in self.paths
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
        v._refuse_scope("connect", self._mod)
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
        v._refuse_scope("disconnect", self._mod)
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
            raise DriftError(f"disconnect {self.target} {port}: currently connected to {cur.expr!r}, not {str(was)!r}")
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
        v._refuse_scope("set_param", self._mod)
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
                raise DriftError(f"set_param {self.target} {name}: currently {cur.value!r}, not {text!r}{note}")
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
        """Instantiate `module` instead of the current one; see `tests/ops/test_instances.py`."""
        self.vet._refuse_scope("replace_module", self._mod)
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
                    raise DriftError(f"{op} {target}: connection {port!r} is {c.expr!r}, not {expr!r}")
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
        new_types = [n for n, k in new_info.param_kinds.items() if k == "type"]
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
            is_type = old_info.param_kinds.get(old) == "type"
            targets = new_types if is_type else new_values
            if new not in targets:
                word = "type" if is_type else "value"
                other = new_values if is_type else new_types
                if new in other:
                    why = f"a {'value' if is_type else 'type'} parameter, and override {old!r} is a {word} parameter"
                else:
                    why = f"not an overridable {word} parameter of module {module!r}{_did_you_mean(new, targets)}"
                problems.append(f"params maps {old!r} to {new!r}, which is {why}")
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
            elif n in (new_types if old_info.param_kinds.get(n) == "type" else new_values):
                carried.setdefault(n, []).append(("kept", n))
            elif old_info.param_kinds.get(n) == "type":
                hint = (
                    "rename it with params= onto one of its type parameters"
                    if new_types
                    else "it has no type parameter to rename onto"
                )
                problems.append(
                    f"override {n!r} is not accounted for: module {module!r} has no type parameter {n!r} "
                    f"({hint}, or remove it with drop_params=, which leaves the port types of {module!r} in force)"
                )
            else:
                hint = "rename it with params= or remove it" if new_values else "remove it"
                problems.append(
                    f"override {n!r} is not accounted for: module {module!r} has no overridable parameter {n!r} "
                    f"({hint} with drop_params=)"
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
                tx, ty = old_snap.port_resolved.get(old), new_snap.port_resolved.get(new)
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
