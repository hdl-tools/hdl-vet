"""The hierarchical operations of `Vet`: `punch_port` and `propagate_param`."""

from __future__ import annotations

import re
from collections.abc import Iterable

from hdl_vet.ops import args
from hdl_vet.ops.common import VetError, _Check, _Plan
from hdl_vet.ops.host import _VetHost
from hdl_vet.ops.instance import Instance
from hdl_vet.ops.text import _IDENT, _bare_name, _did_you_mean, _judged_width, _logic_type, _range, _unsegment
from hdl_vet.slang_adapter import AdapterError, InstanceInfo, ModuleInfo, is_simple_identifier, tokens


class _HierOps(_VetHost):
    """`Vet.punch_port`, `propagate_param` and their helpers."""

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
                raise VetError(f"{op}: {seg!r} in {to!r}: generate blocks and instance arrays are not supported")
            nxt = f"{path}.{seg}"
            if nxt not in snap:
                try:
                    info = self.session.loaded.module(mods[-1])
                except AdapterError as e:
                    raise VetError(str(e)) from e
                hit = next((i for i in info.instances if i.name == _unsegment(seg)), None)
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
            names.append(_unsegment(seg))
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
        args.name(op, "signal", signal, none=True)
        args.width(op, width)
        args.text(op, "type", type, none=True)
        args.flag(op, "all_instances", all_instances)
        to, walked = self._scoped_walk(op, to)
        if direction not in ("input", "output"):
            raise VetError(f"{op}: direction must be 'input' or 'output', not {direction!r}")
        if not is_simple_identifier(name):
            raise VetError(f"{op}: {name!r} is not a simple identifier (or is a SystemVerilog keyword)")
        if signal is not None and not _IDENT.fullmatch(signal):
            raise VetError(f"{op}: the signal must be a plain identifier, not {signal!r}")
        out = direction == "output"
        snap = self._snap()
        mods, _paths, inames = walked or self._walk_path(op, to)
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
                    raise VetError(f"{op}: {p} is inside a generate block or an instance array, which is not supported")
                if snap[parent].module not in mods:
                    raise VetError(
                        f"{op}: {p} is an instance of {m!r} in module {snap[parent].module!r}, "
                        f"which is not on the path to {to!r}, so it would have no {name!r} to connect"
                    )
                # `iname` comes from a snapshot path, so an escaped name arrives spelled as
                # SystemVerilog writes it. The model names it without that (#54).
                targets[(snap[parent].module, _unsegment(iname))] = m
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
        special = out and sig == name and any(p.name == name and p.direction == "output" for p in infos[mods[-1]].ports)
        for i, m in enumerate(mods):
            if i == 0 and not out and signal is not None:
                continue
            if special and i == n:
                continue
            port_plan = self._plan_add_port(m, name, decl, infos[m], self.module_paths(m), _judged_width(pwidth))
            plans.append(port_plan)
            if out and i == n:
                assert sig is not None  # noqa: S101 - an output always has a source signal
                plans.append(self._plan_assign(m, infos[m], name, sig, planned_port=not port_plan.skip))
        for (host, iname), child in sorted(targets.items()):
            handle = Instance(self, host, iname)  # type: ignore[arg-type]  # self is a Vet: this is one of its mixins
            inst = self._instance_info(host, iname)
            handle._refuse_connection_list("connect", inst)
            where = handle.paths
            if not where:
                raise VetError(f"{op}: {host}.{iname} is not elaborated under the top at a known path")
            expr = signal if (host == mods[0] and not out and signal is not None) else name
            plans.append(handle._plan_connect(inst, infos[child], where, name, expr, None))
        self._commit_all(op, plans, f"{op} {name}")

    def propagate_param(self, name: str, to: str, *, default: object | None = None) -> None:
        op = "propagate_param"
        args.value(op, "default", default, none=True)
        to, walked = self._scoped_walk(op, to)
        mods, paths, inames = walked or self._walk_path(op, to)
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
            raise VetError(f"{op}: module {mods[-1]!r} has no parameter {name!r}{_did_you_mean(name, values)}")
        if kind != "value":
            what = "a localparam" if kind == "local" else "a type parameter"
            raise VetError(f"{op}: parameter {name!r} of module {mods[-1]!r} is {what}, not an overridable value")
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
                handle = Instance(self, mods[i], inames[i])  # type: ignore[arg-type]  # self is a Vet: this is one of its mixins
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
        self._commit_all(op, plans, f"{op} {name}")
        if any(not p.skip for p in plans):
            self._set_check(_Check((op, name, to), op, (name, to, before)))
