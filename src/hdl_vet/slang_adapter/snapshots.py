"""The elaborated instances of a `Design`: the structural snapshot and the primitive snapshot."""

from __future__ import annotations

import re
import sys
from typing import Any

import pyslang

from hdl_vet.slang_adapter.host import _DesignHost
from hdl_vet.slang_adapter.records import InstanceSnap
from hdl_vet.slang_adapter.text import _clean_text

_UNNAMED = re.compile(r"\$\d+")  # slang numbers an unnamed struct or union per compilation


def _resolved(t: Any) -> str:
    """The type behind the aliases, as a key that two compilations agree on.

    The printed canonical form is not a key by itself. Two corrections make it one, and both
    were found by a review after the first attempt used the printed form as it comes.

    Slang ends an unnamed struct or union with a counter (`s$3`). The counter belongs to one
    compilation and moves with the set of elaborated types, so the same typedef prints `s$8`
    on one instance and `s$1` on another, in one compilation. Without the pattern below, an
    identical struct on both sides of a swap is refused, with a message that prints the same
    type twice. The pattern leaves the unpacked-array form `logic$[0:1]`, whose `$` has no
    digit after it.

    The printed form of an enum lists the members and the typedef name, not the base type.
    A four-state and a two-state enum with the same members print the same text. Without the
    marker below, a swap between them is accepted and the sign-off passes, and nothing later
    catches it, because Yosys has no four-state value. A four-state type gets no marker,
    which is the common case, so the usual refusal message stays clean.

    The bit width is not part of the key. The caller compares it on its own. The result is
    interned, because a design has few distinct types and many ports.
    """
    c = t.canonicalType
    text = _UNNAMED.sub("", str(c))
    return sys.intern(text if c.isFourState else f"{text} (two-state)")


_DIRECTIONS = {
    "In": "input",
    "Out": "output",
    "InOut": "inout",
    "Ref": "ref",
}


class _SnapshotOps(_DesignHost):
    """The elaborated instances, `snapshot`, `gate_snapshot` and `instance_snap`."""

    # --- elaborated structure ----------------------------------------------

    def _visit_instances(self, fn: Any) -> None:
        """Call `fn` on every instance reachable from the top instance(s).

        Visiting the whole root would also reach the "$unit" instances slang makes
        for definitions nobody instantiates; those are not part of the design.
        """
        for sym in self._instances():
            fn(sym)

    def _instances(self, exhaustive: bool = False) -> tuple[Any, ...]:
        """Every instance symbol under the top instance(s), in visit order.

        The whole tree is traversed either way; the difference is who filters.
        By default slang filters by node kind in C++ (`lookup_table`) and calls
        back only for instances. Filtering in a Python callback instead means
        2.1 million calls for 4,836 instances on the demo_ss sample, about fifteen
        times slower; that is what `exhaustive=True` does, uncached, so tests can
        cross-check the fast path. Nothing is pruned, so there is no list of
        "kinds that can contain instances" to keep right.
        The result is cached (as a tuple): a Design never changes.
        """
        if not exhaustive and self._instance_cache is not None:
            return self._instance_cache
        instance = pyslang.ast.SymbolKind.Instance
        out: list[Any] = []

        def plain(node: Any) -> None:
            if node.kind == instance:
                out.append(node)

        for top in self._comp.getRoot().topInstances:
            if exhaustive:
                top.visit(plain)
            else:
                top.visit(lookup_table={instance: out.append})
        result = tuple(out)
        if not exhaustive:
            self._instance_cache = result
        return result

    def elaborated_instance_paths(self, exhaustive: bool = False) -> list[str]:
        """Hierarchical paths of all instances under the top(s), sorted."""
        return sorted(str(s.hierarchicalPath) for s in self._instances(exhaustive))

    def elaborated_instance_count(self) -> int:
        return len(self._instances())

    def snapshot(self) -> dict[str, InstanceSnap]:
        if self._snap is None:
            self._snap = self._build_snapshot()
        return dict(self._snap)

    def _build_snapshot(self) -> dict[str, InstanceSnap]:
        tops = {t.hierarchicalPath for t in self._comp.getRoot().topInstances}
        out: dict[str, InstanceSnap] = {}

        def one(sym: Any) -> None:
            out[sym.hierarchicalPath] = self._snap_of(sym, sym.hierarchicalPath in tops)

        self._visit_instances(one)
        return out

    def _snap_of(self, sym: Any, is_top: bool) -> InstanceSnap:
        sk = pyslang.ast.SymbolKind
        body = sym.body
        params: dict[str, str] = {}
        for p in body.parameters:
            params[p.name] = str(p.value) if p.kind == sk.Parameter else str(p.targetType.type)
        ports: dict[str, tuple[str, int]] = {}
        port_types: dict[str, str] = {}
        port_resolved: dict[str, str] = {}
        conns: dict[str, str] = {}
        for port in body.portList:
            if port.kind in (sk.Port, sk.MultiPort):
                ports[port.name] = (_DIRECTIONS[port.direction.name], port.type.bitWidth)
                port_types[port.name] = str(port.type)
                port_resolved[port.name] = _resolved(port.type)
            else:
                ports[port.name] = ("interface", 0)
        if not is_top:
            # portConnections flattens a multiport into its constituents; getPortConnection raises on one.
            for conn in sym.portConnections:
                conns[conn.port.name] = self._connection_text(conn.port, conn)
        return InstanceSnap(body.definition.name, params, ports, conns, port_types, port_resolved)

    def parameters_of(self, names: frozenset[str]) -> dict[str, dict[str, str]]:
        """The parameter values of each instance of a module in `names`, by hierarchical path.

        A value parameter is its value as text, a type parameter its type, as in `snapshot()`.
        """
        sk = pyslang.ast.SymbolKind
        out: dict[str, dict[str, str]] = {}
        for sym in self._instances():
            body = sym.body
            if body.definition.name in names:
                out[str(sym.hierarchicalPath)] = {
                    p.name: str(p.value) if p.kind == sk.Parameter else str(p.targetType.type) for p in body.parameters
                }
        return out

    def overridden_types(self, module: str) -> list[tuple[str, str]]:
        """(name, type) of each type parameter that an instance of `module` sets, over every instance.

        The type is as slang prints it. A type parameter that no instance sets is at its default.
        """
        sk = pyslang.ast.SymbolKind
        found: dict[tuple[str, str], None] = {}
        for sym in self._instances():
            body = sym.body
            if body.definition.name == module:
                for p in body.parameters:
                    if p.kind == sk.TypeParameter and p.isOverridden:
                        found[(str(p.name), str(p.targetType.type))] = None
        return list(found)

    def packed_ranges(self, module: str, name: str) -> list[tuple[str, tuple[tuple[int, int], ...]]]:
        """For each instance of `module`: (path, the packed dimensions of the port, net or variable `name`).

        Only the dimensions that the declaration writes are given, outermost first: the walk stops at a typedef.
        A dimension is (left, right) as declared, so `logic [-1:0] w` gives ((-1, 0),).
        The tuple is empty when the symbol is not there or has no packed dimension, so a caller
        cannot take "nothing read" for "good".
        """
        out: list[tuple[str, tuple[tuple[int, int], ...]]] = []
        for sym in self._instances():
            body = sym.body
            if body.definition.name != module:
                continue
            dims: list[tuple[int, int]] = []
            found = body.find(name)
            t = None if found is None else getattr(found, "type", None)
            # a type alias is where the declaration's own dimensions end: what is inside is the typedef's
            while t is not None and t.kind == pyslang.ast.SymbolKind.PackedArrayType:
                r = t.fixedRange
                dims.append((r.left, r.right))
                t = t.elementType
            out.append((str(sym.hierarchicalPath), tuple(dims)))
        return out

    def gate_snapshot(self) -> dict[str, InstanceSnap]:
        """The snapshot of the instances that are not module instances: primitives and checkers.

        Same shape as `snapshot()`. The `module` is the primitive or checker type, there are
        no parameters and no ports, and `connections` maps the position of the terminal ("0",
        "1", ...) to its text. A named instance has the path of its name; an unnamed one has
        `<scope>.<TYPE:N>`, N counting the unnamed instances of that type in the scope from 0.
        Kept apart from `snapshot()` so that nothing that reads the module instances sees these.
        """
        if self._gate_snap is None:
            self._gate_snap = self._build_gate_snapshot()
        return dict(self._gate_snap)

    def _build_gate_snapshot(self) -> dict[str, InstanceSnap]:
        sk = pyslang.ast.SymbolKind
        found: list[Any] = []
        for top in self._comp.getRoot().topInstances:
            top.visit(lookup_table={sk.PrimitiveInstance: found.append, sk.CheckerInstance: found.append})
        out: dict[str, InstanceSnap] = {}
        unnamed: dict[tuple[str, str], int] = {}
        for sym in found:
            if sym.parentScope.isUninstantiated:  # a generate branch that is not taken
                continue
            if sym.kind == sk.PrimitiveInstance:
                kind = str(sym.primitiveType.name)
                terminals = list(sym.portConnections)
            else:
                kind = str(sym.body.name)
                terminals = [c.actual for c in sym.portConnections]
            path = str(sym.hierarchicalPath)
            if sym.arrayName:  # an element of an array of primitives: its path ends in the index
                path += "".join(f"[{i}]" for i in sym.arrayPath)
            elif not sym.name:
                n = unnamed.get((path, kind), 0)
                unnamed[(path, kind)] = n + 1
                path = f"{path}.<{kind}:{n}>"
            written = self._written_terminals(sym)
            conns = {}
            for i, e in enumerate(terminals):
                text = self._terminal_text(e)
                if not text and i < len(written):  # an element of an array: its bit has no text of its own
                    text = written[i]
                conns[str(i)] = text
            delay, strength = self._gate_timing(sym)
            out[path] = InstanceSnap(kind, {}, {}, conns, delay=delay, strength=strength)
        return out

    def _gate_timing(self, sym: Any) -> tuple[str, str]:
        """The written delay and drive strength of a gate, from its syntax; ("", "") when there is none.

        The elaborated route is unusable (see tests/core/test_gate_timing.py). A checker
        instance or a node without syntax has no gate statement, so it gets ("", "").
        """
        syntax = sym.syntax
        stmt = None if syntax is None else syntax.parent
        if stmt is None:
            return "", ""
        delay, strength = getattr(stmt, "delay", None), getattr(stmt, "strength", None)
        return (_clean_text(delay) if delay is not None else "", _clean_text(strength) if strength is not None else "")

    def _written_terminals(self, sym: Any) -> list[str]:
        """The texts of the ordered connections of the statement that holds `sym`."""
        syntax = sym.syntax
        if syntax is None or not hasattr(syntax, "connections"):
            return []
        return [_clean_text(c.expr) for c in syntax.connections if getattr(c, "expr", None) is not None]

    def _terminal_text(self, e: Any) -> str:
        ek = pyslang.ast.ExpressionKind
        if e is None:
            return ""
        while e.kind in (ek.Assignment, ek.Conversion):
            if e.kind == ek.Assignment:
                e = e.left
            elif e.isImplicit:
                e = e.operand
            else:
                break
        return "" if e.syntax is None else _clean_text(e.syntax)

    def instance_snap(self, path: str) -> InstanceSnap | None:
        """The snapshot of the one instance at `path`, without building the whole snapshot.

        None when there is no instance at that path. Equal to `snapshot().get(path)`.
        """
        if self._snap is not None:
            return self._snap.get(path)
        try:
            sym = self._comp.getRoot().lookupName(path)
        except RuntimeError:  # slang cannot look up a name such as `top.module`
            sym = None
        if sym is None:
            return self.snapshot().get(path)
        if sym.kind != pyslang.ast.SymbolKind.Instance:
            return None
        return self._snap_of(sym, "." not in path)

    def _connection_text(self, port: Any, conn: Any) -> str:
        ek = pyslang.ast.ExpressionKind
        if conn is None:
            return ""
        e = conn.expression
        if e is None:
            iface = conn.ifaceConn
            if iface is not None and iface[0] is not None:
                return str(iface[0].name)
            return ""
        # Outputs and inouts arrive as an assignment to the connected signal, and
        # a width mismatch as an implicit conversion around it; the text is the
        # signal's. An explicit cast is part of what the designer wrote: it stays.
        while True:
            if e.kind == ek.Assignment:
                e = e.left
            elif e.kind == ek.Conversion and e.isImplicit:
                e = e.operand
            else:
                break
            if e.syntax is not None:
                break
        if e.syntax is None:
            # Implicit `.name`, `.*` and interface ports have no source expression.
            symbol = getattr(e, "symbol", None)
            return str(getattr(symbol, "name", None) or port.name)
        return _clean_text(e.syntax)
