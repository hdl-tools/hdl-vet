"""Hierarchical references and the sites of instances in a `Design`."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

import pyslang

from hdl_vet.slang_adapter.host import _DesignHost
from hdl_vet.slang_adapter.records import AdapterError, Driver, DriverDetail, HierRef, InstanceSite
from hdl_vet.slang_adapter.text import _first_token

_HEAD_RE = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_$]*)")


class _HierRefOps(_DesignHost):
    """`drivers_in`, `drivers_of`, `hierarchical_refs` and `instance_sites`."""

    def _canonical_bodies(self, module: str) -> dict[str, Any]:
        """The canonical body of every distinct parameterisation of `module`, by hierarchical path."""
        bodies: dict[str, Any] = {}

        def one(sym: Any) -> None:
            if sym.body.definition.name == module:
                body = sym.canonicalBody or sym.body
                bodies.setdefault(str(body.hierarchicalPath), body)

        self._visit_instances(one)
        if not bodies:
            raise AdapterError(f"module {module!r} is not elaborated under the top")
        return bodies

    def drivers_in(self, module: str, name: str, hints: Sequence[str] = ()) -> list[Driver]:
        """Drivers of module-level `name` over every elaborated instance of `module`.

        slang analyses one canonical body per distinct parameterisation, so every
        instance is visited and each canonical body is looked at once. A declaration
        initialiser counts as a driver. The implicit driver an inout port has from
        its own port connection is not reported.
        """
        out: list[Driver] = []
        self._analyze()  # the canonical bodies are known only after the analysis
        for body in self._canonical_bodies(module).values():
            sym = body.find(name)
            if sym is None:
                raise AdapterError(f"module {module!r} has no member {name!r}")
            try:
                drivers = self._analyze().getDrivers(sym)
            except TypeError as e:
                raise AdapterError(f"{module}.{name}: not a net or variable, so it has no drivers") from e
            for d in drivers:
                if d.isInputPort or d.containingSymbol.kind != pyslang.ast.SymbolKind.InstanceBody:
                    via = d.containingSymbol if d.containingSymbol.kind == pyslang.ast.SymbolKind.Instance else None
                    out.append(Driver(bool(d.isInputPort), None if via is None else str(via.name)))
            if getattr(sym, "initializer", None) is not None:
                out.append(Driver(False))
        return out

    def driven_signals(self) -> frozenset[tuple[str, str]]:
        """Every `(module, name)` net or variable that has a driver in some elaborated body.

        The same facts as `drivers_in`, for the whole design in one pass. It exists because
        asking `drivers_in` per name re-walks every instance each time (`_canonical_bodies`),
        so a scan over all signals cost modules times signals times instances. Here the
        instances are walked once and each member is read once. A declaration initialiser
        counts as a driver. A member that is not a net or variable is not listed.
        """
        sk = pyslang.ast.SymbolKind
        an = self._analyze()  # the canonical bodies are known only after the analysis
        bodies: dict[str, list[Any]] = {}
        for sym in self._instances():
            body = sym.canonicalBody or sym.body
            seen = bodies.setdefault(body.definition.name, [])
            if not any(b is body for b in seen):
                seen.append(body)
        out: set[tuple[str, str]] = set()
        for module, module_bodies in bodies.items():
            for body in module_bodies:
                for member in body:
                    name = getattr(member, "name", "")
                    if not name or member.kind not in (sk.Net, sk.Variable):
                        continue
                    try:
                        drivers = an.getDrivers(member)
                    except TypeError:
                        continue
                    if getattr(member, "initializer", None) is not None or any(
                        d.isInputPort or d.containingSymbol.kind != sk.InstanceBody for d in drivers
                    ):
                        out.add((module, str(name)))
        return frozenset(out)

    def elaborated_modules(self) -> frozenset[str]:
        """The names of the modules with at least one instance under the top (one walk, for the whole design)."""
        return frozenset(sym.body.definition.name for sym in self._instances())

    def unfollowable_heads(self, module: str, names: Iterable[str]) -> list[str]:
        """The names of `names` that the loop walk cannot follow in `module`, sorted.

        A name is followable only when, in EVERY elaborated body of `module`, it resolves to a net,
        a variable or a parameter: the walk reads those by name. Anything else (a child instance,
        a generate block, a subroutine, a package, or a name that does not resolve) leads to
        logic that the walk does not enter, so a loop through it would go unseen.
        """
        kinds = {pyslang.ast.SymbolKind.Net, pyslang.ast.SymbolKind.Variable, pyslang.ast.SymbolKind.Parameter}
        bodies = list(self._canonical_bodies(module).values())

        def followable(name: str) -> bool:
            return all(getattr(b.find(name), "kind", None) in kinds for b in bodies)

        return sorted(n for n in set(names) if not followable(n))

    def combinational_closure(self, module: str, net: str) -> frozenset[str]:
        """Every module-level name that `net` drives through combinational logic in `module`.

        Fails closed: what the walk cannot classify, it follows. Every driver of a net is followed
        (an `assign`, a combinational block, a gate primitive), and of each driver the WHOLE
        symbol is read, so a condition, a case selector, a loop bound and an index on the left
        side count as reads, and so does a name that the driver itself writes. A net declared
        with an initialiser (`wire t = n;`) also reads the initialiser. Only two things end the
        walk: a driver that is a child instance (a stated limit), and a driver that is an
        edge-clocked procedural block (a register). An input port is skipped. The walk stays in
        the one module body. The result is the union over every elaborated body of `module`.
        """
        sk, ek, pk = pyslang.ast.SymbolKind, pyslang.ast.ExpressionKind, pyslang.ast.ProceduralBlockKind
        an = self._analyze()

        def reads(node: Any) -> set[str]:
            out: set[str] = set()

            def go(n: Any) -> None:
                sym = getattr(n, "symbol", None)
                if getattr(n, "kind", None) == ek.NamedValue and sym is not None and getattr(sym, "name", ""):
                    out.add(str(sym.name))

            node.visit(go)
            return out

        def clocked(blk: Any) -> bool:
            if blk.procedureKind in (pk.AlwaysComb, pk.AlwaysLatch):
                return False
            events: list[Any] = []
            blk.visit(lambda n: events.append(n) if str(getattr(n, "kind", "")).endswith("SignalEvent") else None)
            return bool(events) and all(str(getattr(e, "edge", "")) != "EdgeKind.None_" for e in events)

        result: set[str] = set()
        for body in self._canonical_bodies(module).values():

            def sources(name: str, body: Any = body) -> set[str]:
                sym = body.find(name)
                if sym is None:
                    return set()
                try:
                    drivers = an.getDrivers(sym)
                except TypeError:
                    return set()
                out: set[str] = set()
                for d in drivers:
                    cs = d.containingSymbol
                    if d.isInputPort or cs.kind == sk.Instance:
                        continue
                    if cs.kind == sk.ProceduralBlock and clocked(cs):
                        continue
                    out |= reads(cs)
                if getattr(sym, "initializer", None) is not None:
                    out |= reads(sym.initializer)
                return {x for x in out if body.find(x) is not None}

            if body.find(net) is None:
                raise AdapterError(f"module {module!r} has no member {net!r}")
            fanout: dict[str, set[str]] = {}
            for member in body:
                if member.name:
                    for src in sources(str(member.name)):
                        fanout.setdefault(src, set()).add(str(member.name))
            seen: set[str] = set()
            stack = [net]
            while stack:
                for nxt in fanout.get(stack.pop(), ()):
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
            result |= seen
        return frozenset(result)

    def driver_details(self, module: str, name: str) -> list[tuple[int, list[DriverDetail]]]:
        """For each elaborated body of `module`: the width of the signal `name` and each driver of it.

        A driver is an `assign`, a procedural block (`process`), an output pin of an instance, an input
        port, an initialiser, or `other` (a primitive, for example). `low` and `high` are the bits it drives.
        """
        sk = pyslang.ast.SymbolKind
        kinds = {sk.ContinuousAssign: "assign", sk.ProceduralBlock: "process", sk.Instance: "instance"}
        out: list[tuple[int, list[DriverDetail]]] = []
        self._analyze()  # the canonical bodies are known only after the analysis
        for body in self._canonical_bodies(module).values():
            sym = body.find(name)
            if sym is None:
                raise AdapterError(f"module {module!r} has no member {name!r}")
            try:
                drivers = self._analyze().getDrivers(sym)
            except TypeError as e:
                raise AdapterError(f"{module}.{name}: not a net or variable, so it has no drivers") from e
            width = int(sym.type.bitWidth)
            found = [
                DriverDetail(
                    "input" if d.isInputPort else kinds.get(d.containingSymbol.kind, "other"),
                    int(d.bounds[0]),
                    int(d.bounds[1]),
                )
                for d in drivers
            ]
            if getattr(sym, "initializer", None) is not None:
                found.append(DriverDetail("initializer", 0, max(width - 1, 0)))
            out.append((width, found))
        return out

    def parameter_values(self, module: str, names: Sequence[str]) -> list[dict[str, int | None]]:
        """For each elaborated body of `module`: the integer value of each named parameter (None if it has none)."""
        out: list[dict[str, int | None]] = []
        for body in self._canonical_bodies(module).values():
            found: dict[str, int | None] = {}
            for name in names:
                sym = body.find(name)
                try:
                    found[name] = int(str(sym.value)) if sym is not None else None
                except (ValueError, AttributeError):
                    found[name] = None
            out.append(found)
        return out

    def drivers_of(self, hier_path: str) -> list[Driver]:
        symbol = self._comp.getRoot().lookupName(hier_path)
        if symbol is None:
            raise AdapterError(f"no such hierarchical name: {hier_path}")
        try:
            drivers = self._analyze().getDrivers(symbol)
        except TypeError as e:
            raise AdapterError(f"{hier_path}: not a net or variable, so it has no drivers") from e
        return [Driver(bool(d.isInputPort)) for d in drivers]

    def hierarchical_refs(self) -> list[HierRef]:
        """Every hierarchical name in the elaborated design, once per instance that holds it.

        Seen: a hierarchical value (any use of a signal, down, up or from the top) and a call of a
        task or function reached through a hierarchical name, in the module's own items (generate
        blocks included) and in the port connections of the instances it holds (also those in
        instance arrays and generate blocks). Each instance body is looked at on its own, so a
        name that is relative (`u_c0.q`, `blk.m`) is resolved for every instance. A name that
        starts at one of the module's own interface ports is a use of the port and is not reported.
        """
        sk, ek = pyslang.ast.SymbolKind, pyslang.ast.ExpressionKind
        out: list[HierRef] = []
        for inst in self._instances():
            host, module, body = str(inst.hierarchicalPath), inst.body.definition.name, inst.body

            def add(node: Any, host: str = host, module: str = module, body: Any = body) -> None:
                if node.kind == ek.HierarchicalValue:
                    target = str(node.symbol.hierarchicalPath)
                elif node.isSystemCall:
                    return
                else:  # a call; one of its own module's subroutines, or a package's, is not hierarchical
                    target = str(node.subroutine.hierarchicalPath)
                    if "." not in target or target.rpartition(".")[0] == host:
                        return
                head = _HEAD_RE.match(str(node.syntax))
                port = None if head is None else body.find(head.group(1))
                if port is not None and port.kind == sk.InterfacePort:
                    return
                out.append(HierRef(host, module, target))

            table = {ek.HierarchicalValue: add, ek.Call: add}

            def walk(scope: Any, table: dict[Any, Any] = table) -> None:
                for member in scope:
                    if member.kind == sk.Instance:
                        for conn in member.portConnections:
                            if conn.expression is not None:
                                conn.expression.visit(lookup_table=table)
                    elif member.kind in (sk.InstanceArray, sk.GenerateBlock, sk.GenerateBlockArray):
                        walk(member)
                    else:
                        member.visit(lookup_table=table)

            walk(inst.body)
        return out

    # --- instance sites ----------------------------------------------------

    def instance_sites(self, module: str) -> list[InstanceSite]:
        definition = self._comp.tryGetDefinition(module, self._comp.getRoot()).definition
        if definition is None or definition.syntax is None:
            raise AdapterError(f"no such module: {module}")
        sites: list[InstanceSite] = []

        def visit(node: Any) -> None:
            if node.kind == pyslang.syntax.SyntaxKind.HierarchyInstantiation:
                sites.extend(self._sites_of(node))

        definition.syntax.visit(visit)
        return sites

    def _udp_names(self) -> frozenset[str]:
        """Names of the user-defined primitives: definitions that are not modules, interfaces or programs."""
        if self._udps is None:
            self._udps = frozenset(
                d.name for d in self._comp.getDefinitions() if getattr(d, "definitionKind", None) is None
            )
        return self._udps

    def _sites_of(self, inst: Any, plain: bool | None = None) -> list[InstanceSite]:
        if inst.type.valueText in self._udp_names():
            return []
        sm = self._sm
        from_macro = False

        def scan(node: Any) -> None:
            nonlocal from_macro
            if isinstance(node, pyslang.parsing.Token) and sm.isMacroLoc(node.location):
                from_macro = True

        if not (self._plain(inst) if plain is None else plain):
            inst.visit(scan)
        first, last = _first_token(inst), inst.getLastToken()
        start = first.location
        while sm.isMacroLoc(start):
            start = sm.getExpansionRange(start).start
        if sm.isMacroLoc(last.location):
            rng = sm.getExpansionRange(last.location)
            while sm.isMacroLoc(rng.end):
                rng = sm.getExpansionRange(rng.end)
            end = rng.end
        else:
            end = last.range.end
        path = self._buffer_path(start.buffer)
        if path is None or end.buffer != start.buffer:
            raise AdapterError("instantiation does not lie in one source file")
        if not start.offset < end.offset:
            raise AdapterError(f"{path}: cannot map instantiation to a source range")
        module_name = inst.type.valueText
        return [
            InstanceSite(
                instance_name=i.decl.name.valueText,
                module_name=module_name,
                path=path,
                start=start.offset,
                end=end.offset,
                from_macro=from_macro,
            )
            for i in inst.instances
            if i.kind == pyslang.syntax.SyntaxKind.HierarchicalInstance and i.decl is not None
        ]
