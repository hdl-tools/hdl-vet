"""Diagnostics, drivers, and the dangling and unused analysis of a `Design`."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pyslang

from hdl_vet.slang_adapter.host import _DesignHost
from hdl_vet.slang_adapter.records import Diag
from hdl_vet.slang_adapter.text import _IDENT_TOKEN, is_simple_identifier, tokens

# What the separate unused / undriven analysis reports (Design.dangling / Design.unused).
_DANGLING_CODES = frozenset({"UndrivenNet", "UndrivenPort", "UnassignedVariable"})
_UNUSED_CODES = frozenset(
    {
        "UnusedNet",
        "UnusedVariable",
        "UnusedPort",
        "UnusedButSetNet",
        "UnusedButSetVariable",
        "UnusedButSetPort",
        "UnusedImplicitNet",
    }
)


_SEVERITY = {
    pyslang.DiagnosticSeverity.Note: "note",
    pyslang.DiagnosticSeverity.Warning: "warning",
    pyslang.DiagnosticSeverity.Error: "error",
    pyslang.DiagnosticSeverity.Fatal: "error",
}
_CODE_NAMES: dict[Any, str] = {getattr(pyslang.Diags, n): n for n in dir(pyslang.Diags) if not n.startswith("_")}


def _merge_ranges(spans: Sequence[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    """Inclusive integer ranges, sorted, with overlapping and adjacent ones joined."""
    out: list[list[int]] = []
    for lo, hi in sorted((min(a, b), max(a, b)) for a, b in spans):
        if out and lo <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return tuple((lo, hi) for lo, hi in out)


def _reads_symbol(body: Any, symbol: Any) -> bool:
    """True when an expression in `body` names `symbol`."""
    hits: list[int] = []

    def visit(node: Any) -> None:
        if isinstance(node, pyslang.ast.NamedValueExpression) and node.symbol == symbol:
            hits.append(1)

    body.visit(visit)
    return bool(hits)


def driven_bases(text: object) -> list[str]:
    """Base names an assignment target or output connection expression drives.

    Through selects and concatenations: `y[3:0]` is `y`, `{a, b[i]}` is `a` and `b`.
    Names inside a select (`i` in `b[i]`) are read, not driven.
    """
    out: list[str] = []
    depth = 0
    prev = ""
    for t in tokens(text):
        if t == "[":
            depth += 1
        elif t == "]":
            depth = max(depth - 1, 0)
        elif (
            depth == 0
            and _IDENT_TOKEN.fullmatch(t)
            and prev not in (".", "::")
            and t not in out
            and is_simple_identifier(t)
        ):
            out.append(t)
        prev = t
    return out


class _DiagnosticsOps(_DesignHost):
    """Diagnostics, `driven_bits`, `dangling`, `unused` and `defined_in`."""

    # --- diagnostics -------------------------------------------------------

    def diagnostics(self) -> list[Diag]:
        if self._diags is None:
            self._diags = self._collect_diagnostics()
        return list(self._diags)

    def _analyze(self) -> Any:
        if self._analysis is None:
            self._comp.getAllDiagnostics()
            self._comp.freeze()
            self._analysis = pyslang.analysis.AnalysisManager()
            self._analysis.analyze(self._comp)
        return self._analysis

    def _collect_diagnostics(self) -> list[Diag]:
        raw = list(self._comp.getAllDiagnostics())
        raw += list(self._analyze().getDiagnostics())
        engine = self._engine
        out: list[tuple[tuple[str, int, str, str], Diag]] = []
        for diag in raw:
            severity = _SEVERITY.get(engine.getSeverity(diag.code, diag.location))
            if severity is None:
                continue
            made = self._make_diag(diag, severity)
            out.append(((str(made.path or ""), made.offset or 0, made.code, made.message), made))
        out.sort(key=lambda item: item[0])
        return [d for _, d in out]

    def _module_spans(self) -> list[tuple[Path, int, int, str]]:
        if self._spans is None:
            dk = pyslang.ast.DefinitionKind
            kinds = (dk.Module, dk.Interface, dk.Program)
            spans: list[tuple[Path, int, int, str]] = []
            for d in self._comp.getDefinitions():
                if getattr(d, "definitionKind", None) not in kinds or d.syntax is None:
                    continue
                first = d.syntax.getFirstToken()
                if not first:
                    continue
                loc = self._original(first.location)
                path = self._buffer_path(loc.buffer)
                if path is not None:
                    spans.append((path, loc.offset, d.syntax.getLastToken().range.end.offset, d.name))
            self._spans = spans
        return self._spans

    def _module_at(self, path: Path | None, offset: int | None) -> str | None:
        if path is None or offset is None:
            return None
        for p, a, b, name in self._module_spans():
            if p == path and a <= offset <= b:
                return name
        return None

    def driven_bits(
        self, signals: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], dict[str, tuple[tuple[int, int], ...]] | None]:
        """Which bits of module-level signals have a driver, per elaborated body of the module.

        For each (module, name): the merged inclusive bit ranges that some driver covers
        (a continuous assignment, an output port connection, a procedural block, the
        initialiser), keyed by the canonical body's hierarchical path; None when the
        module is not elaborated or has no such signal. slang reports a signal with no
        driver at all, but is silent when only some of its bits lose theirs.
        """
        wanted = {m for m, _ in signals}
        bodies: dict[str, dict[str, Any]] = {m: {} for m in wanted}

        def one(sym: Any) -> None:
            name = sym.body.definition.name
            if name in wanted:
                body = sym.canonicalBody or sym.body
                bodies[name].setdefault(str(body.hierarchicalPath), body)

        analysis = self._analyze()  # first: the canonical bodies are known only after the analysis
        self._visit_instances(one)
        out: dict[tuple[str, str], dict[str, tuple[tuple[int, int], ...]] | None] = {}
        for module, name in signals:
            found: dict[str, tuple[tuple[int, int], ...]] = {}
            for key, body in bodies[module].items():
                sym = body.find(name)
                if sym is None:
                    continue
                try:
                    drivers = analysis.getDrivers(sym)
                except TypeError:
                    continue
                spans = [tuple(d.bounds) for d in drivers]
                if getattr(sym, "initializer", None) is not None:
                    spans.append((0, max(int(sym.type.bitWidth) - 1, 0)))
                found[key] = _merge_ranges(spans)
            out[(module, name)] = found or None
        return out

    def _make_diag(self, diag: Any, severity: str, with_module: bool = False) -> Diag:
        in_macro = bool(self._sm.isMacroLoc(diag.location))
        loc = self._original(diag.location)
        path: Path | None = None
        line: int | None = None
        offset: int | None = None
        if loc.buffer.id != 0 and loc != pyslang.SourceLocation.NoLocation:
            path = self._buffer_path(loc.buffer)
            line = self._sm.getLineNumber(loc)
            offset = loc.offset
        code = _CODE_NAMES.get(diag.code, str(diag.code))
        body = self._body_of(getattr(diag, "symbol", None)) if with_module else None
        module = self._module_at(path, offset) if with_module and body is None else None
        port: str | None = None
        read = False
        args = list(diag.args) if with_module else []
        name = args[0] if args and isinstance(args[0], str) else None  # the signal's name
        if body is not None:
            module = str(body.definition.name)
            if code == "UndrivenPort" and name:
                port, read = self._port_reads(body, str(name))
        return Diag(
            severity,
            code,
            self._engine.formatMessage(diag),
            path,
            line,
            offset,
            in_macro,
            module,
            str(name) if name else None,
            port,
            read,
        )

    @staticmethod
    def _body_of(sym: Any) -> Any:
        """The instance body a diagnostic's symbol belongs to (an instance's own, else the one holding its scope).

        None when there is none.
        """
        if sym is None:
            return None
        try:
            if sym.kind == pyslang.ast.SymbolKind.Instance:
                body = sym.body
            else:
                scope = sym.parentScope
                body = scope.containingInstance if scope is not None else None
            return body if body is not None and body.definition is not None else None
        except AttributeError:
            return None

    @staticmethod
    def _port_reads(body: Any, signal: str) -> tuple[str | None, bool]:
        """The external name of the port of `body` whose internal signal is `signal`, and whether the body reads it.

        (None, True) when no port matches: an entry that cannot be identified is treated as read.
        """
        for port in body.portList:
            if port.kind != pyslang.ast.SymbolKind.Port:
                continue
            inner = port.internalSymbol
            if inner is None or inner.name != signal:
                continue
            return str(port.name), _reads_symbol(body, inner)
        return None, True

    def _unused_pass(self) -> list[Diag]:
        """Every diagnostic of the dangling and unused codes, from an analysis of its own.

        slang reports these only when its unused checking is on. That analysis is a
        separate manager over the same (frozen) compilation, so `diagnostics()` and
        what sign-off counts are untouched by it.
        """
        if self._unused is None:
            self._comp.getAllDiagnostics()
            self._comp.freeze()
            opts = pyslang.analysis.AnalysisOptions()
            opts.flags = pyslang.analysis.AnalysisFlags.CheckUnused
            manager = pyslang.analysis.AnalysisManager(opts)
            manager.analyze(self._comp)
            out: list[tuple[tuple[str, int, str, str], Diag]] = []
            for diag in manager.getDiagnostics():
                if _CODE_NAMES.get(diag.code) not in _DANGLING_CODES | _UNUSED_CODES:
                    continue
                made = self._make_diag(diag, "warning", with_module=True)
                out.append(((str(made.path or ""), made.offset or 0, made.code, made.message), made))
            out.sort(key=lambda item: item[0])
            self._unused = [d for _, d in out]
        return self._unused

    def dangling(self) -> list[Diag]:
        """What slang reports as driverless: UndrivenNet, UndrivenPort, UnassignedVariable."""
        return [d for d in self._unused_pass() if d.code in _DANGLING_CODES]

    def unused(self) -> list[Diag]:
        """What slang reports as unused or set but never read (nets, variables, ports)."""
        return [d for d in self._unused_pass() if d.code in _UNUSED_CODES]

    def unused_names(self) -> frozenset[tuple[str, str]]:
        """(module, signal) of every entry of `unused`, from the symbol slang names, not from the message."""
        return frozenset((d.module, d.symbol) for d in self.unused() if d.module and d.symbol)

    def reads_port(self, module: str, port: str) -> bool:
        """True when `module` reads its port `port` in some elaborated body (True when that cannot be told)."""
        self._analyze()  # the canonical bodies are known only after the analysis
        bodies: dict[str, Any] = {}

        def one(sym: Any) -> None:
            if sym.body.definition.name == module:
                body = sym.canonicalBody or sym.body
                bodies.setdefault(str(body.hierarchicalPath), body)

        self._visit_instances(one)
        return not bodies or any(self._port_reads(b, port)[1] for b in bodies.values())

    def defined_in(self, path: Path) -> list[str]:
        """Names of the modules, interfaces and programs whose definition lies in the file `path`."""
        dk = pyslang.ast.DefinitionKind
        kinds = (dk.Module, dk.Interface, dk.Program)
        out: set[str] = set()
        for d in self._comp.getDefinitions():
            if getattr(d, "definitionKind", None) not in kinds or d.syntax is None:
                continue
            first = d.syntax.getFirstToken()
            if first and self._buffer_path(self._original(first.location).buffer) == path:
                out.add(d.name)
        return sorted(out)
