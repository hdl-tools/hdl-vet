"""The connection of a pin on every elaborated instance of a module, as the compiler resolves it."""

from __future__ import annotations

import re
from typing import Any

import pyslang

from hdl_vet.slang_adapter.host import _DesignHost
from hdl_vet.slang_adapter.records import PinConn
from hdl_vet.slang_adapter.text import _clean_text

_LITERALS = frozenset(
    {
        pyslang.syntax.SyntaxKind.IntegerLiteralExpression,
        pyslang.syntax.SyntaxKind.IntegerVectorExpression,
        pyslang.syntax.SyntaxKind.UnbasedUnsizedLiteralExpression,
    }
)
# A literal of the value 0: no x or z digit, no sign marker, no brackets (those are other syntax nodes).
_ZERO = re.compile(r"(?:\d+)?'[bBoOdDhH]0[0_]*\Z|0[0_]*\Z|'0\Z")
_INDEX = re.compile(r"\[[^\]]*\]")


class _PinOps(_DesignHost):
    """`pin_connections`."""

    def pin_connections(self, child: str, pin: str) -> list[PinConn]:
        """The connection of `pin` on each elaborated instance of the module `child`.

        Every elaboration of the parent counts: one entry for each distinct (parent module, instance path,
        connection). An instance with no connection for the pin has the text "".
        """
        ek = pyslang.ast.ExpressionKind
        found: dict[tuple[str, str, str], PinConn] = {}
        for sym in self._instances():
            if sym.body.definition.name != child:
                continue
            parent = sym.parentScope.containingInstance if sym.parentScope is not None else None
            if parent is None:
                continue  # a top instance has no parent
            # A multiport is reported as its constituents, as the instance's own connection list does.
            names = {pin}
            for p in sym.body.portList:
                if p.name == pin and p.kind == pyslang.ast.SymbolKind.MultiPort:
                    names = {str(q.name) for q in p.ports}
            exprs = [c.expression for c in sym.portConnections if str(c.port.name) in names]
            for expr in exprs or [None]:
                while expr is not None and (
                    expr.kind == ek.Assignment or (expr.kind == ek.Conversion and expr.isImplicit)
                ):
                    expr = expr.left if expr.kind == ek.Assignment else expr.operand
                text, kind, port = "", "other", ""
                if expr is not None:
                    if expr.syntax is None:  # an implicit `.name` or `.*`: the signal of that name
                        symbol = getattr(expr, "symbol", None)
                        text = str(getattr(symbol, "name", None) or pin)
                    else:
                        text = _clean_text(expr.syntax)
                    port = self._parent_port(parent, expr)
                    if port:
                        kind = "port"
                    elif self._is_zero(expr):
                        kind = "zero"
                path, base = str(sym.hierarchicalPath), str(parent.hierarchicalPath)
                label = _INDEX.sub("", path[len(base) + 1 :])
                module = str(parent.definition.name)
                found.setdefault(
                    (module, label, text), PinConn(module, label, text, kind, port if kind == "port" else "")
                )
        return list(found.values())

    @staticmethod
    def _parent_port(parent: Any, expr: Any) -> str:
        """The name of the input port of `parent` that the expression is, else ''. The symbol decides, not the text."""
        if expr.kind != pyslang.ast.ExpressionKind.NamedValue:
            return ""
        target = str(expr.symbol.hierarchicalPath)
        for port in parent.portList:
            inner = getattr(port, "internalSymbol", None)
            is_input = port.kind == pyslang.ast.SymbolKind.Port and port.direction.name == "In"
            if is_input and inner is not None and str(inner.hierarchicalPath) == target:
                return str(port.name)
        return ""

    def _is_zero(self, expr: Any) -> bool:
        """True when the connection is, as written, one literal of the value 0 (not in brackets, not from a macro)."""
        syntax = expr.syntax
        if syntax is None or syntax.kind not in _LITERALS:
            return False
        first = syntax.getFirstToken()
        if not first or self._sm.isMacroLoc(first.location):
            return False
        return _ZERO.match(str(syntax).strip()) is not None
