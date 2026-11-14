"""The only module that talks to pyslang.

Loads a `.f` file list into a slang compilation and exposes the few capabilities
hdl-vet needs: byte-exact round trip, diagnostics (parse, elaboration and
analysis), elaborated instance counts, instance statements as source byte ranges,
drivers of a hierarchical name, and syntax equivalence.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyslang

from hdl_vet.filelist import Filelist, read_filelist

__all__ = [
    "AdapterError",
    "ConnInfo",
    "Design",
    "Diag",
    "Driver",
    "InstanceInfo",
    "InstanceSite",
    "InstanceSnap",
    "ModuleInfo",
    "ParamInfo",
    "ParamOverride",
    "PortInfo",
    "load",
    "syntax_equivalent",
]

# Warnings slang leaves off by default but that a port-level edit can trigger:
# an undeclared name in a port connection becomes an implicit net, and a wrong
# width is only truncated or extended. Deliberately not -Wextra / -Wconversion.
_ALWAYS_WARN = ("ImplicitNet", "PortWidthTruncate", "PortWidthExpand")
_DIRECTIONS = {
    "In": "input",
    "Out": "output",
    "InOut": "inout",
    "Ref": "ref",
}
_WS_RE = re.compile(r"\s+")

_BOM = b"\xef\xbb\xbf"
_SEVERITY = {
    pyslang.DiagnosticSeverity.Note: "note",
    pyslang.DiagnosticSeverity.Warning: "warning",
    pyslang.DiagnosticSeverity.Error: "error",
    pyslang.DiagnosticSeverity.Fatal: "error",
}
_CODE_NAMES: dict[Any, str] = {
    getattr(pyslang.Diags, n): n for n in dir(pyslang.Diags) if not n.startswith("_")
}
_INCLUDE_RE = re.compile(r'`include\s+"([^"\r\n]+)"')
_MAX_INCLUDE_DEPTH = 64
_SAFE_NAME = re.compile(r"[A-Za-z0-9._-]+")


class AdapterError(Exception):
    """Raised when a design cannot be loaded or a query cannot be answered."""


@dataclass(frozen=True)
class Diag:
    severity: str
    code: str
    message: str
    path: Path | None
    line: int | None
    offset: int | None
    in_macro: bool = False


@dataclass(frozen=True)
class PortInfo:
    name: str
    direction: str
    start: int
    end: int


@dataclass(frozen=True)
class ParamInfo:
    name: str
    default: str | None
    start: int
    end: int


@dataclass(frozen=True)
class ConnInfo:
    port: str | None
    expr: str
    start: int
    end: int


@dataclass(frozen=True)
class ParamOverride:
    name: str | None
    value: str
    start: int
    end: int


@dataclass(frozen=True)
class InstanceInfo:
    name: str
    module: str
    path: Path
    from_macro: bool
    stmt_start: int
    stmt_end: int
    module_start: int
    module_end: int
    start: int
    end: int
    params_close: int | None
    ports_close: int
    connections: list[ConnInfo]
    param_overrides: list[ParamOverride]

    def connection(self, port: str) -> ConnInfo:
        for c in self.connections:
            if c.port == port:
                return c
        raise AdapterError(f"instance {self.name!r} has no connection for port {port!r}")

    def param_override(self, name: str) -> ParamOverride:
        for p in self.param_overrides:
            if p.name == name:
                return p
        raise AdapterError(f"instance {self.name!r} has no override of parameter {name!r}")


@dataclass(frozen=True)
class ModuleInfo:
    name: str
    path: Path
    start: int
    end: int
    ports_close: int | None
    body_end: int
    ports: list[PortInfo]
    params: list[ParamInfo]
    instances: list[InstanceInfo]
    has_macros: bool

    def port(self, name: str) -> PortInfo:
        for p in self.ports:
            if p.name == name:
                return p
        raise AdapterError(f"module {self.name!r} has no port {name!r}")

    def param(self, name: str) -> ParamInfo:
        for p in self.params:
            if p.name == name:
                return p
        raise AdapterError(f"module {self.name!r} has no parameter {name!r}")

    def instance(self, name: str) -> InstanceInfo:
        found = [i for i in self.instances if i.name == name]
        if not found:
            raise AdapterError(f"module {self.name!r} has no instance {name!r}")
        if len(found) > 1:
            raise AdapterError(f"module {self.name!r} has {len(found)} instances named {name!r}")
        return found[0]


@dataclass(frozen=True)
class InstanceSnap:
    module: str
    params: dict[str, str]
    ports: dict[str, tuple[str, int]]
    connections: dict[str, str]
    port_types: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Driver:
    is_input_port: bool


@dataclass(frozen=True)
class InstanceSite:
    instance_name: str
    module_name: str
    path: Path
    start: int
    end: int
    from_macro: bool


def _key(path: Path) -> str:
    return os.path.normcase(str(path))


def _read(path: Path, overrides: Mapping[Path, bytes]) -> bytes:
    data = overrides.get(path)
    if data is None:
        try:
            data = path.read_bytes()
        except OSError as e:
            raise AdapterError(f"{path}: cannot read ({e})") from e
    return data


class _Loader:
    """Builds syntax trees from bytes read in Python, on one shared SourceManager.

    slang never touches the file system: every source and every header the design
    includes is handed over as text (`fromText` for sources, `assignText` for
    headers) under a synthetic ASCII path, which sidesteps its narrow-string path
    handling. The synthetic tree mirrors the real one (`<anchor>/__hdlvet__/<drive>/
    <real components>`, non-ASCII components renamed consistently), so relative
    names, ".." included, resolve inside it exactly as they would on the real tree.
    Each header is registered under the mirror of the file it really resolves to;
    slang is given the mirrored include directories. Python resolves the real file
    in slang's order (the includer's real directory, then the include directories),
    applying overrides.
    """

    def __init__(self, fl: Filelist, overrides: Mapping[Path, bytes], sm: Any) -> None:
        self.fl = fl
        self.overrides = overrides
        self.sm = sm
        self.real: dict[str, Path] = {}  # synthetic buffer path -> real path
        self.boms: dict[Path, bool] = {}
        self.bad: dict[Path, str] = {}  # headers that are not valid UTF-8
        self.bytes: dict[Path, bytes] = {}  # raw bytes of every header read
        self._bodies: dict[Path, str] = {}
        self._names: dict[str, str] = {}
        self._root = Path(fl.root.anchor) / "__hdlvet__"

    def _component(self, part: str) -> str:
        if _SAFE_NAME.fullmatch(part):
            return part
        k = os.path.normcase(part)
        name = self._names.get(k)
        if name is None:
            name = self._names[k] = f"__u{len(self._names)}__"
        return name

    def mirror(self, real: Path) -> Path:
        """The synthetic path standing for the real, resolved `real`."""
        parts = real.parts
        out = self._root / ("a" + re.sub(r"[^A-Za-z0-9]", "_", parts[0]))
        for part in parts[1:]:
            out = out / self._component(part)
        return out

    def _resolve(self, name: str, real_dir: Path) -> Path | None:
        for base in (real_dir, *self.fl.incdirs):
            cand = (base / name).resolve()
            if cand in self.overrides or cand.is_file():
                return cand
        return None

    def _body(self, header: Path) -> str:
        body = self._bodies.get(header)
        if body is None:
            raw = _read(header, self.overrides)
            self.bytes[header] = raw
            try:
                body = raw.decode("utf-8")
            except UnicodeDecodeError as e:
                self.bad[header] = str(e)
                body = raw.decode("utf-8", errors="replace")
            self._bodies[header] = body
        return body

    def _register_includes(self, text: str, real_dir: Path, depth: int = 0) -> None:
        if depth > _MAX_INCLUDE_DEPTH:
            return
        for name in _INCLUDE_RE.findall(text):
            header = self._resolve(name, real_dir)
            if header is None:
                continue
            body = self._body(header)
            if Path(name).is_absolute():
                keys = [Path(name)] if name.isascii() else []
            else:
                # Where slang looks first: next to the includer. It differs from the
                # mirror of the resolved file only through case or a symlink.
                keys = [self.mirror(Path(os.path.normpath(real_dir / name)))]
            keys.append(self.mirror(header))
            fresh = False
            for key in keys:
                if _key(key) in self.real:
                    continue
                self.real[_key(key)] = header
                self.sm.assignText(str(key), body)
                fresh = True
            if fresh:
                self._register_includes(body, header.parent, depth + 1)

    def tree(self, src: Path, bag: Any) -> Any:
        raw = _read(src, self.overrides)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise AdapterError(f"{src}: not valid UTF-8, refusing to load ({e})") from e
        self.boms[src] = raw.startswith(_BOM)
        key = self.mirror(src)
        self.real[_key(key)] = src
        self._register_includes(text, src.parent)
        return pyslang.syntax.SyntaxTree.fromText(text, self.sm, src.name, str(key), bag)

    def included(self) -> set[Path]:
        """Real paths of the headers slang actually included."""
        out: set[Path] = set()
        for buf in self.sm.getAllBuffers():
            if self.sm.getIncludedFrom(buf).buffer.id != 0:
                path = self.real.get(_key(Path(str(self.sm.getFullPath(buf)))))
                if path is not None:
                    out.add(path)
        return out


def load(
    filelist: Path | Filelist,
    top: str | None = None,
    overrides: Mapping[Path, bytes] | None = None,
    base: Path | None = None,
    *,
    check_overrides: bool = True,
) -> Design:
    """Load a design.

    `filelist` is a path (read now) or an already parsed `Filelist`. With
    `check_overrides`, an override for a file that is neither a source nor an
    included file is an error.
    """
    fl = filelist if isinstance(filelist, Filelist) else read_filelist(filelist, base=base)
    ov = {Path(k).resolve(): bytes(v) for k, v in (overrides or {}).items()}

    # A driver is used only for its configured diagnostic engine, option defaults
    # and SourceManager; "-" is a placeholder input that is never read.
    driver = pyslang.driver.Driver()
    driver.addStandardArgs()
    if not driver.parseCommandLine("slang -", pyslang.driver.CommandLineOptions()):
        raise AdapterError("slang rejected its default command line")
    if not driver.processOptions():
        raise AdapterError("slang rejected its default options")
    for name in _ALWAYS_WARN:
        driver.diagEngine.setSeverity(getattr(pyslang.Diags, name), pyslang.DiagnosticSeverity.Warning)
    bag = driver.createOptionBag()
    loader = _Loader(fl, ov, driver.sourceManager)
    pre = bag.preprocessorOptions
    pre.predefines = [n if v is None else f"{n}={v}" for n, v in fl.defines.items()]
    pre.additionalIncludePaths = [str(loader.mirror(d)) for d in fl.incdirs]
    bag.preprocessorOptions = pre
    if top is not None:
        comp_opts = bag.compilationOptions
        comp_opts.topModules = {top}
        bag.compilationOptions = comp_opts

    trees = [loader.tree(src, bag) for src in fl.files]
    used = loader.included()
    for header in sorted(used & loader.bad.keys()):
        raise AdapterError(f"{header}: not valid UTF-8, refusing to load ({loader.bad[header]})")
    if check_overrides:
        for key in ov:
            if key not in fl.files and key not in used:
                raise AdapterError(f"{key}: override is neither a source nor an included file")

    comp = pyslang.ast.Compilation(bag)
    for tree in trees:
        comp.addSyntaxTree(tree)
    if top is not None and not comp.getRoot().topInstances:
        raise AdapterError(f"top module {top!r} not found")
    return Design(fl, loader, driver.diagEngine, dict(zip(fl.files, trees, strict=True)), comp)


class Design:
    def __init__(
        self, fl: Filelist, loader: _Loader, engine: Any, trees: dict[Path, Any], comp: Any
    ) -> None:
        self._fl = fl
        self._loader = loader
        self._real = loader.real
        self._boms = loader.boms
        self._sm = loader.sm
        self._trees = trees
        self._comp = comp
        self._engine = engine
        self._diags: list[Diag] | None = None
        self._analysis: Any = None
        self._snap: dict[str, InstanceSnap] | None = None
        self._modules: dict[str, ModuleInfo] = {}

    @property
    def files(self) -> list[Path]:
        return list(self._fl.files)

    @property
    def filelist(self) -> Filelist:
        return self._fl

    @property
    def included(self) -> list[Path]:
        """Headers slang actually included, sorted."""
        return sorted(self._loader.included())

    @property
    def included_bytes(self) -> dict[Path, bytes]:
        """The bytes this load read for each header it actually included."""
        return {p: self._loader.bytes[p] for p in self.included if p in self._loader.bytes}

    # --- helpers -----------------------------------------------------------

    def _buffer_path(self, buffer: Any) -> Path | None:
        return self._real.get(_key(Path(str(self._sm.getFullPath(buffer)))))

    def _original(self, loc: Any) -> Any:
        while self._sm.isMacroLoc(loc):
            loc = self._sm.getExpansionLoc(loc)
        return loc

    # --- round trip --------------------------------------------------------

    def roundtrip(self, path: Path) -> bytes:
        key = Path(path).resolve()
        tree = self._trees.get(key)
        if tree is None:
            raise AdapterError(f"{path}: not a source file of this design")
        out = pyslang.syntax.SyntaxPrinter.printFile(tree).encode()
        if self._boms[key] and not out.startswith(_BOM):
            out = _BOM + out
        return out

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
            message = engine.formatMessage(diag)
            out.append(
                (
                    (str(path or ""), offset or 0, code, message),
                    Diag(severity, code, message, path, line, offset, in_macro),
                )
            )
        out.sort(key=lambda item: item[0])
        return [d for _, d in out]

    # --- elaborated structure ----------------------------------------------

    def _visit_instances(self, fn: Any) -> None:
        """Call `fn` on every instance reachable from the top instance(s).

        Visiting the whole root would also reach the "$unit" instances slang makes
        for definitions nobody instantiates; those are not part of the design.
        """
        kind = pyslang.ast.SymbolKind.Instance

        def visit(node: Any) -> None:
            if node.kind == kind:
                fn(node)

        for top in self._comp.getRoot().topInstances:
            top.visit(visit)

    def elaborated_instance_count(self) -> int:
        count = 0

        def one(_: Any) -> None:
            nonlocal count
            count += 1

        self._visit_instances(one)
        return count

    def snapshot(self) -> dict[str, InstanceSnap]:
        if self._snap is None:
            self._snap = self._build_snapshot()
        return dict(self._snap)

    def _build_snapshot(self) -> dict[str, InstanceSnap]:
        sk = pyslang.ast.SymbolKind
        tops = {t.hierarchicalPath for t in self._comp.getRoot().topInstances}
        out: dict[str, InstanceSnap] = {}

        def one(sym: Any) -> None:
            body = sym.body
            params: dict[str, str] = {}
            for p in body.parameters:
                params[p.name] = str(p.value) if p.kind == sk.Parameter else str(p.targetType.type)
            ports: dict[str, tuple[str, int]] = {}
            port_types: dict[str, str] = {}
            conns: dict[str, str] = {}
            is_top = sym.hierarchicalPath in tops
            for port in body.portList:
                if port.kind == sk.Port:
                    ports[port.name] = (_DIRECTIONS[port.direction.name], port.type.bitWidth)
                    port_types[port.name] = str(port.type)
                else:
                    ports[port.name] = ("interface", 0)
                if not is_top:
                    conns[port.name] = self._connection_text(port, sym.getPortConnection(port))
            out[sym.hierarchicalPath] = InstanceSnap(body.definition.name, params, ports, conns, port_types)

        self._visit_instances(one)
        return out

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

    def drivers_of(self, hier_path: str) -> list[Driver]:
        symbol = self._comp.getRoot().lookupName(hier_path)
        if symbol is None:
            raise AdapterError(f"no such hierarchical name: {hier_path}")
        try:
            drivers = self._analyze().getDrivers(symbol)
        except TypeError as e:
            raise AdapterError(f"{hier_path}: not a net or variable, so it has no drivers") from e
        return [Driver(bool(d.isInputPort)) for d in drivers]

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

    def _sites_of(self, inst: Any) -> list[InstanceSite]:
        sm = self._sm
        from_macro = False

        def scan(node: Any) -> None:
            nonlocal from_macro
            if isinstance(node, pyslang.parsing.Token) and sm.isMacroLoc(node.location):
                from_macro = True

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
            if i.kind == pyslang.syntax.SyntaxKind.HierarchicalInstance
        ]


    # --- source model --------------------------------------------------------

    def modules(self) -> list[str]:
        dk = pyslang.ast.DefinitionKind
        kinds = (dk.Module, dk.Interface, dk.Program)
        return sorted({d.name for d in self._comp.getDefinitions() if d.definitionKind in kinds})

    def module(self, name: str) -> ModuleInfo:
        info = self._modules.get(name)
        if info is None:
            info = self._modules[name] = self._module_info(name)
        return info

    def _module_info(self, name: str) -> ModuleInfo:
        sk = pyslang.syntax.SyntaxKind
        found = self._comp.tryGetDefinition(name, self._comp.getRoot()).definition
        if found is None or found.syntax is None:
            raise AdapterError(f"no such module: {name}")
        syn = found.syntax
        first = _first_token(syn)
        path = self._buffer_path(self._original(first.location).buffer)
        if path is None:
            raise AdapterError(f"module {name}: declaration is not in a source file")

        def check(tok: Any, what: str) -> None:
            """Refuse items that come from an `include: their offsets are not ours."""
            origin = self._buffer_path(self._original(tok.location).buffer)
            if origin is not None and origin != path:
                raise AdapterError(
                    f"module {name}: {what} comes from included file {origin}; "
                    f"edit that file or the include directly, not the module's items"
                )

        hdr = syn.header
        ports, ports_close = self._ports_of(syn, hdr, check)
        params: list[ParamInfo] = []
        if hdr.parameters is not None:
            for decl in _items(hdr.parameters.declarations):
                for d in _items(decl.declarators):
                    check(d.getFirstToken(), f"parameter {d.name.valueText!r}")
                    init = getattr(d, "initializer", None) or getattr(d, "assignment", None)
                    value = getattr(init, "expr", None) or getattr(init, "type", None)
                    params.append(
                        ParamInfo(
                            name=d.name.valueText,
                            default=None if value is None else _text(value),
                            start=d.getFirstToken().location.offset,
                            end=d.getLastToken().range.end.offset,
                        )
                    )
        instances: list[InstanceInfo] = []
        has_macros = _has_macro_tokens(syn, self._sm)

        def visit(node: Any) -> None:
            if node.kind == sk.HierarchyInstantiation:
                check(_first_token(node), f"instantiation of {node.type.valueText!r}")
                instances.extend(self._instances_of(node, path))

        syn.visit(visit)
        return ModuleInfo(
            name=name,
            path=path,
            start=first.location.offset,
            end=syn.endmodule.range.end.offset,
            ports_close=ports_close,
            body_end=syn.endmodule.location.offset,
            ports=ports,
            params=params,
            instances=instances,
            has_macros=has_macros,
        )

    @staticmethod
    def _ports_of(syn: Any, hdr: Any, check: Any) -> tuple[list[PortInfo], int | None]:
        sk = pyslang.syntax.SyntaxKind
        tk = pyslang.parsing.TokenKind
        dirs = {
            tk.InputKeyword: "input",
            tk.OutputKeyword: "output",
            tk.InOutKeyword: "inout",
            tk.RefKeyword: "ref",
        }
        ports: list[PortInfo] = []
        plist = hdr.ports
        if plist is None:
            return ports, None
        if plist.kind == sk.AnsiPortList:
            last = "input"
            for p in _items(plist.ports):
                header = getattr(p, "header", None)
                tok = getattr(header if header is not None else p, "direction", None)
                if header is not None and header.kind == sk.InterfacePortHeader:
                    direction = "interface"
                elif tok is not None and tok.kind in dirs:
                    direction = dirs[tok.kind]
                else:
                    direction = last  # `output logic a, b`: b shares a's declaration
                last = direction
                ident = p.declarator.name if hasattr(p, "declarator") else p.name
                start = _first_token(p)
                check(start, f"port {ident.valueText!r}")
                ports.append(
                    PortInfo(
                        ident.valueText,
                        direction,
                        start.location.offset,
                        p.getLastToken().range.end.offset,
                    )
                )
            check(plist.closeParen, "the port list")
            return ports, plist.closeParen.location.offset
        declared: dict[str, str] = {}
        for mem in syn.members:
            if mem.kind == sk.PortDeclaration:
                tok = getattr(mem.header, "direction", None)
                direction = "interface" if mem.header.kind == sk.InterfacePortHeader else dirs.get(
                    getattr(tok, "kind", None), "inout"
                )
                for d in _items(mem.declarators):
                    declared[d.name.valueText] = direction
        for p in _items(plist.ports):
            if p.kind == sk.ExplicitNonAnsiPort:
                ident = p.name
                # `.c(cc)`: the direction is declared for the inner name.
                inner = None if p.expr is None or p.expr.kind != sk.PortReference else p.expr.name
                direction = "inout" if inner is None else declared.get(inner.valueText, "inout")
            else:
                ident = p.getFirstToken()
                direction = declared.get(ident.valueText, "inout")
            check(p.getFirstToken(), f"port {ident.valueText!r}")
            ports.append(
                PortInfo(
                    ident.valueText,
                    direction,
                    p.getFirstToken().location.offset,
                    p.getLastToken().range.end.offset,
                )
            )
        return ports, None

    def _instances_of(self, node: Any, mod_path: Path) -> list[InstanceInfo]:
        sk = pyslang.syntax.SyntaxKind
        from_macro = _has_macro_tokens(node, self._sm)
        module = node.type.valueText
        hier = [i for i in node.instances if i.kind == sk.HierarchicalInstance]
        try:
            sites: list[InstanceSite | None] = list(self._sites_of(node))
        except AdapterError:
            sites = [None] * len(hier)
        overrides: list[ParamOverride] = []
        params_close: int | None = None
        if node.parameters is not None:
            params_close = node.parameters.closeParen.location.offset
            for a in _items(node.parameters.parameters):
                expr = getattr(a, "expr", None)
                overrides.append(
                    ParamOverride(
                        name=a.name.valueText if a.kind == sk.NamedParamAssignment else None,
                        value="" if expr is None else _text(expr),
                        start=a.getFirstToken().location.offset,
                        end=a.getLastToken().range.end.offset,
                    )
                )
        out: list[InstanceInfo] = []
        first = _first_token(node)
        for hi, site in zip(hier, sites, strict=True):
            out.append(
                InstanceInfo(
                    name=hi.decl.name.valueText,
                    module=module,
                    path=site.path if site is not None else mod_path,
                    from_macro=from_macro or site is None,
                    stmt_start=site.start if site is not None else first.location.offset,
                    stmt_end=site.end if site is not None else node.getLastToken().range.end.offset,
                    module_start=node.type.location.offset,
                    module_end=node.type.range.end.offset,
                    start=hi.getFirstToken().location.offset,
                    end=hi.getLastToken().range.end.offset,
                    params_close=params_close,
                    ports_close=hi.closeParen.location.offset,
                    connections=[_connection(c, sk) for c in _items(hi.connections)],
                    param_overrides=list(overrides),
                )
            )
        return out


def _items(lst: Any) -> list[Any]:
    """The nodes of a separated syntax list (separator tokens dropped)."""
    return [x for x in lst if not isinstance(x, pyslang.parsing.Token)]


def _text(node: Any) -> str:
    return str(node).strip()


def _has_macro_tokens(node: Any, sm: Any) -> bool:
    found = False

    def scan(n: Any) -> None:
        nonlocal found
        if not found and isinstance(n, pyslang.parsing.Token) and sm.isMacroLoc(n.location):
            found = True

    node.visit(scan)
    return found


def _first_token(node: Any) -> Any:
    """First token of `node`, skipping leading attribute instances `(* ... *)`."""
    attrs = getattr(node, "attributes", None)
    if not attrs:
        return node.getFirstToken()
    last = attrs[len(attrs) - 1].getLastToken()
    cut, buf = last.range.end.offset, last.location.buffer
    found: list[Any] = []

    def scan(n: Any) -> None:
        if not found and isinstance(n, pyslang.parsing.Token):
            if n.location.buffer == buf and n.location.offset >= cut:
                found.append(n)

    node.visit(scan)
    return found[0] if found else node.getFirstToken()


def _connection(c: Any, sk: Any) -> ConnInfo:
    if c.kind == sk.EmptyPortConnection:
        p = c.getFirstToken().location.offset
        return ConnInfo(None, "", p, p)
    if c.kind == sk.NamedPortConnection:
        port = c.name.valueText
        if not c.openParen:  # `.name`: an empty token
            expr = port
        else:
            expr = "" if c.expr is None else _text(c.expr)
    elif c.kind == sk.WildcardPortConnection:
        port, expr = "*", ""
    else:  # OrderedPortConnection
        port, expr = None, "" if c.expr is None else _text(c.expr)
    return ConnInfo(port, expr, _first_token(c).location.offset, c.getLastToken().range.end.offset)


def _clean_text(node: Any) -> str:
    """Source text of `node` without comments and with whitespace runs collapsed."""
    raw = str(node)
    if "/*" not in raw and "//" not in raw:
        return _WS_RE.sub(" ", raw).strip()
    ws = (pyslang.parsing.TriviaKind.Whitespace, pyslang.parsing.TriviaKind.EndOfLine)
    parts: list[str] = []

    def visit(n: Any) -> None:
        if isinstance(n, pyslang.parsing.Token):
            if any(t.kind in ws for t in n.trivia):
                parts.append(" ")
            parts.append(n.rawText)

    node.visit(visit)
    return _WS_RE.sub(" ", "".join(parts)).strip()


def syntax_equivalent(a: str, b: str) -> bool:
    trees = []
    for text in (a, b):
        tree = pyslang.syntax.SyntaxTree.fromText(text)
        if any(d.isError() for d in tree.diagnostics):
            raise AdapterError(f"text does not parse: {text!r}")
        trees.append(tree)
    return bool(trees[0].root.isEquivalentTo(trees[1].root))
