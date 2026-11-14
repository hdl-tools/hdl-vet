"""The only module that talks to pyslang.

Loads a `.f` file list into a slang compilation and exposes the few capabilities
hdl-vet needs: byte-exact round trip, diagnostics (parse, elaboration and
analysis), elaborated instance counts, instance statements as source byte ranges,
drivers of a hierarchical name, and syntax equivalence.
"""

from __future__ import annotations

import functools
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyslang

from hdl_vet.filelist import Filelist, read_filelist

__all__ = [
    "AdapterError",
    "ConnInfo",
    "DeclInfo",
    "AssignInfo",
    "Design",
    "Diag",
    "Driver",
    "InstanceInfo",
    "InstanceSite",
    "LoadCache",
    "InstanceSnap",
    "ModuleInfo",
    "ParamInfo",
    "ParamOverride",
    "PortInfo",
    "load",
    "syntax_equivalent",
    "is_simple_identifier",
    "tokens",
]

# Warnings slang leaves off by default but that a port-level edit can trigger:
# an undeclared name in a port connection becomes an implicit net, and a wrong
# width is only truncated or extended. Deliberately not -Wextra / -Wconversion.
_ALWAYS_WARN = ("ImplicitNet", "PortWidthTruncate", "PortWidthExpand", "WidthTruncate")
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
    type_text: str = ""  # ANSI: the data type as `logic ...` with packed dimensions ("" if unknown)
    unpacked: bool = False  # ANSI: the port has unpacked dimensions


@dataclass(frozen=True)
class ParamInfo:
    name: str
    default: str | None
    start: int
    end: int
    type_text: str = ""  # the declared data type as written ("" when implicit)


@dataclass(frozen=True)
class ConnInfo:
    port: str | None
    expr: str
    start: int
    end: int
    open: int | None = None  # offset of the "(" of a `.name(...)` connection


@dataclass(frozen=True)
class ParamOverride:
    name: str | None
    value: str
    start: int
    end: int
    open: int | None = None  # offset of the "(" of a `.name(...)` override


@dataclass(frozen=True)
class DeclInfo:
    """A module-level net or variable declaration."""

    name: str
    kind: str  # "net" | "var"
    type_text: str  # data type (with the net type for a net) and unpacked dimensions
    start: int
    end: int


@dataclass(frozen=True)
class AssignInfo:
    """One continuous assignment `left = right`."""

    left: str
    right: str
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
    ports_open: int = -1
    params_open: int | None = None
    in_generate: bool = False  # inside a generate block, loop or conditional
    is_array: bool = False  # an instance array `u [3:0] (...)`
    conn_directive: str | None = None  # a conditional directive inside the connection list
    override_directive: str | None = None  # ... inside the parameter value list

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
    name_end: int = 0
    ports_open: int | None = None
    params_open: int | None = None
    params_close: int | None = None
    port_list_kind: str = "none"  # "ansi" | "nonansi" | "none"
    header_end: int = 0
    body_indent: str = "  "
    decls: list[DeclInfo] = field(default_factory=list)
    assigns: list[AssignInfo] = field(default_factory=list)
    header_has_macros: bool = False
    param_kinds: dict[str, str] = field(default_factory=dict)  # "value" | "local" | "type"
    other_names: frozenset[str] = frozenset()  # typedefs, subroutines, genvars, named blocks
    imports_end: int | None = None  # end of the last package import in the header
    ports_directive: str | None = None  # a conditional directive inside the port list
    params_directive: str | None = None  # ... inside the parameter port list

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
    instance: str | None = None  # the instance whose output port connection drives it, else None


@dataclass(frozen=True)
class InstanceSite:
    instance_name: str
    module_name: str
    path: Path
    start: int
    end: int
    from_macro: bool


@functools.lru_cache(maxsize=4096)
def _tokens(text: str) -> tuple[str, ...]:
    sm = pyslang.SourceManager()
    buf = sm.assignText(text)
    lexer = pyslang.parsing.Lexer(buf, pyslang.BumpAllocator(), pyslang.Diagnostics(), sm)
    eof = pyslang.parsing.TokenKind.EndOfFile
    out: list[str] = []
    while True:
        tok = lexer.lex()
        if tok.kind == eof:
            return tuple(out)
        out.append(tok.rawText)


def tokens(text: object) -> list[str]:
    """The raw texts of the tokens of `text`; whitespace and comments are not tokens."""
    return list(_tokens(str(text)))


def is_simple_identifier(name: str) -> bool:
    """True for one plain identifier that is not a SystemVerilog keyword."""
    sm = pyslang.SourceManager()
    buf = sm.assignText(name)
    lexer = pyslang.parsing.Lexer(buf, pyslang.BumpAllocator(), pyslang.Diagnostics(), sm)
    tok = lexer.lex()
    if tok.kind != pyslang.parsing.TokenKind.Identifier or tok.rawText != name:
        return False
    return lexer.lex().kind == pyslang.parsing.TokenKind.EndOfFile


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


class LoadCache:
    """File-system answers that stay valid across the loads of one session.

    A session reads every file once and then re-loads from its own bytes, so
    which real file an include name resolves to, and what a path resolves to,
    cannot change between its loads. Without this, every re-load asked the file
    system again for each include of each source (over a third of a re-load on
    the demo_ss sample). Pass one instance to every `load` of a session; a load
    without a cache behaves as before.
    """

    def __init__(self) -> None:
        self.includes: dict[tuple[str, Path], Path | None] = {}
        self.paths: dict[Path, Path] = {}
        self.headers: dict[Path, bytes] = {}
        self._filelist: Filelist | None = None

    def bind(self, fl: Filelist) -> None:
        """Tie the cache to one file list: its answers depend on the include directories."""
        if self._filelist is None:
            self._filelist = fl
        elif self._filelist is not fl and self._filelist != fl:
            raise AdapterError("a LoadCache belongs to one file list and cannot be shared with another")

    def resolved(self, path: Path) -> Path:
        # A relative path means something else after a change of directory.
        if not path.is_absolute():
            return path.resolve()
        out = self.paths.get(path)
        if out is None:
            out = self.paths[path] = path.resolve()
        return out


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

    def __init__(
        self, fl: Filelist, overrides: Mapping[Path, bytes], sm: Any, cache: LoadCache | None = None
    ) -> None:
        self.fl = fl
        self.overrides = overrides
        self.sm = sm
        # Even one load asks for the same header many times; a private cache
        # covers that when the caller shares none.
        self.cache = cache if cache is not None else LoadCache()
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
        key = (name, real_dir)
        known = self.cache.includes
        if key in known:
            return known[key]
        found: Path | None = None
        for base in (real_dir, *self.fl.incdirs):
            cand = self.cache.resolved(base / name)
            if cand in self.overrides or cand.is_file():
                found = cand
                break
        known[key] = found
        return found

    def _body(self, header: Path) -> str:
        body = self._bodies.get(header)
        if body is None:
            # A header the caller did not supply (one named only in an inactive
            # `ifdef branch) is read from disk once per cache, not once per load.
            raw = self.overrides.get(header)
            if raw is None:
                raw = self.cache.headers.get(header)
            if raw is None:
                raw = self.cache.headers[header] = _read(header, self.overrides)
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
    cache: LoadCache | None = None,
) -> Design:
    """Load a design.

    `filelist` is a path (read now) or an already parsed `Filelist`. With
    `check_overrides`, an override for a file that is neither a source nor an
    included file is an error. `cache` carries file-system answers from one load
    of a session to the next (see LoadCache).
    """
    fl = filelist if isinstance(filelist, Filelist) else read_filelist(filelist, base=base)
    if cache is None:
        ov = {Path(k).resolve(): bytes(v) for k, v in (overrides or {}).items()}
    else:
        cache.bind(fl)
        ov = {cache.resolved(Path(k)): bytes(v) for k, v in (overrides or {}).items()}

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
    loader = _Loader(fl, ov, driver.sourceManager, cache)
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
        self._instance_cache: tuple[Any, ...] | None = None
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

    def drivers_in(self, module: str, name: str, hints: Sequence[str] = ()) -> list[Driver]:
        """Drivers of module-level `name` over every elaborated instance of `module`.

        slang analyses one canonical body per distinct parameterisation, so every
        instance is visited and each canonical body is looked at once. A declaration
        initialiser counts as a driver. The implicit driver an inout port has from
        its own port connection is not reported.
        """
        bodies: dict[str, Any] = {}

        def one(sym: Any) -> None:
            if sym.body.definition.name == module:
                body = sym.canonicalBody or sym.body
                bodies.setdefault(str(body.hierarchicalPath), body)

        self._visit_instances(one)
        if not bodies:
            raise AdapterError(f"module {module!r} is not elaborated under the top")
        out: list[Driver] = []
        for body in bodies.values():
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
        param_kinds: dict[str, str] = {}
        if hdr.parameters is not None:
            for decl in _items(hdr.parameters.declarations):
                kind = _param_kind(decl)
                for d in _items(decl.declarators):
                    param_kinds[d.name.valueText] = kind
                    check(d.getFirstToken(), f"parameter {d.name.valueText!r}")
                    init = getattr(d, "initializer", None) or getattr(d, "assignment", None)
                    value = getattr(init, "expr", None) or getattr(init, "type", None)
                    params.append(
                        ParamInfo(
                            name=d.name.valueText,
                            default=None if value is None else _text(value),
                            start=d.getFirstToken().location.offset,
                            end=d.getLastToken().range.end.offset,
                            type_text=_param_type_text(decl),
                        )
                    )
        instances: list[InstanceInfo] = []
        has_macros = _has_macro_tokens(syn, self._sm)

        def visit(node: Any) -> None:
            if node.kind == sk.HierarchyInstantiation:
                check(_first_token(node), f"instantiation of {node.type.valueText!r}")
                instances.extend(self._instances_of(node, path))

        syn.visit(visit)
        decls, assigns = self._body_items(syn, param_kinds, hdr.parameters is None)
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
            name_end=hdr.name.range.end.offset,
            ports_open=None
            if hdr.ports is None or hdr.ports.kind != sk.AnsiPortList
            else hdr.ports.openParen.location.offset,
            params_open=None if hdr.parameters is None else hdr.parameters.hash.location.offset,
            params_close=None if hdr.parameters is None else hdr.parameters.closeParen.location.offset,
            port_list_kind="none"
            if hdr.ports is None
            else "ansi"
            if hdr.ports.kind == sk.AnsiPortList
            else "nonansi",
            header_end=hdr.semi.range.end.offset,
            body_indent=self._body_indent(syn),
            decls=decls,
            assigns=assigns,
            header_has_macros=_header_has_macros(hdr, self._sm),
            param_kinds=param_kinds,
            other_names=_other_names(syn),
            imports_end=hdr.imports[-1].getLastToken().range.end.offset if len(hdr.imports) else None,
            ports_directive=None if hdr.ports is None else _conditional_directive(hdr.ports),
            params_directive=None if hdr.parameters is None else _conditional_directive(hdr.parameters),
        )

    def _body_indent(self, syn: Any) -> str:
        """Indent of the line of the first body item (two spaces for an empty body)."""
        members = list(syn.members)
        if not members:
            return "  "
        loc = self._original(members[0].getFirstToken().location)
        text = str(self._sm.getSourceText(loc.buffer)).encode()
        off = loc.offset
        start = max(text.rfind(b"\n", 0, off), text.rfind(b"\r", 0, off)) + 1
        line = text[start:off]
        return line[: len(line) - len(line.lstrip(b" \t"))].decode()

    def _body_items(
        self, syn: Any, param_kinds: dict[str, str], body_params_overridable: bool
    ) -> tuple[list[DeclInfo], list[AssignInfo]]:
        sk = pyslang.syntax.SyntaxKind
        decls: list[DeclInfo] = []
        assigns: list[AssignInfo] = []
        for mem in syn.members:
            if mem.kind in (sk.DataDeclaration, sk.NetDeclaration):
                is_net = mem.kind == sk.NetDeclaration
                base = _clean_text(mem.type)
                if is_net:
                    base = (mem.netType.valueText + " " + base).strip()
                for d in _items(mem.declarators):
                    dims = " ".join(_clean_text(x) for x in d.dimensions)
                    decls.append(
                        DeclInfo(
                            d.name.valueText,
                            "net" if is_net else "var",
                            (base + " " + dims).strip(),
                            d.getFirstToken().location.offset,
                            d.getLastToken().range.end.offset,
                        )
                    )
            elif mem.kind == sk.ContinuousAssign:
                for a in _items(mem.assignments):
                    if a.kind != sk.AssignmentExpression:
                        continue
                    assigns.append(
                        AssignInfo(
                            _clean_text(a.left),
                            _clean_text(a.right),
                            a.getFirstToken().location.offset,
                            a.getLastToken().range.end.offset,
                        )
                    )
            elif mem.kind == sk.ParameterDeclarationStatement:
                decl = mem.parameter
                kind = _param_kind(decl)
                if kind != "local" and not body_params_overridable:
                    kind = "local"
                for d in _items(decl.declarators):
                    param_kinds.setdefault(d.name.valueText, kind)
        return decls, assigns

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
            last_type = ""
            for p in _items(plist.ports):
                header = getattr(p, "header", None)
                tok = getattr(header if header is not None else p, "direction", None)
                if header is not None and header.kind != sk.InterfacePortHeader:
                    dtype = _clean_text(header.dataType)
                    if (tok is not None and tok.kind in dirs) or dtype or getattr(header, "netType", None):
                        last_type = _port_type_text(dtype)
                    own_type = last_type
                else:
                    own_type = ""
                dims = getattr(getattr(p, "declarator", None), "dimensions", None)
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
                        own_type,
                        dims is not None and len(dims) > 0,
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
        params_open: int | None = None
        if node.parameters is not None:
            params_close = node.parameters.closeParen.location.offset
            params_open = node.parameters.hash.location.offset
            for a in _items(node.parameters.parameters):
                expr = getattr(a, "expr", None)
                overrides.append(
                    ParamOverride(
                        name=a.name.valueText if a.kind == sk.NamedParamAssignment else None,
                        value="" if expr is None else _text(expr),
                        start=a.getFirstToken().location.offset,
                        end=a.getLastToken().range.end.offset,
                        open=a.openParen.location.offset
                        if a.kind == sk.NamedParamAssignment and a.openParen
                        else None,
                    )
                )
        out: list[InstanceInfo] = []
        first = _first_token(node)
        in_generate = _in_generate(node)
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
                    ports_open=hi.openParen.location.offset,
                    params_open=params_open,
                    in_generate=in_generate,
                    is_array=len(hi.decl.dimensions) > 0,
                    conn_directive=_conditional_directive(hi, skip_first=False),
                    override_directive=None
                    if node.parameters is None
                    else _conditional_directive(node.parameters),
                )
            )
        return out


_DIRECTIVES = {
    "IfDefDirective": "`ifdef",
    "IfNDefDirective": "`ifndef",
    "ElsIfDirective": "`elsif",
    "ElseDirective": "`else",
    "EndIfDirective": "`endif",
}
_GENERATE_KINDS = ("GenerateBlock", "IfGenerate", "LoopGenerate", "CaseGenerate")


def _conditional_directive(node: Any, skip_first: bool = True) -> str | None:
    """The first preprocessor conditional directive inside a parenthesised list.

    The trivia of the opening token lies before the list, so it does not count
    (`skip_first`); for an instance node the first token is the instance name.
    """
    found: list[str] = []
    seen_first = not skip_first

    def scan(n: Any) -> None:
        nonlocal seen_first
        if found or not isinstance(n, pyslang.parsing.Token):
            return
        if not seen_first:
            seen_first = True
            return
        for t in n.trivia:
            if t.kind == pyslang.parsing.TriviaKind.Directive:
                syn = t.syntax()
                name = _DIRECTIVES.get(str(syn.kind).rsplit(".", 1)[-1]) if syn is not None else None
                if name is not None:
                    found.append(name)
                    return

    node.visit(scan)
    return found[0] if found else None


def _in_generate(node: Any) -> bool:
    p = node.parent
    while p is not None:
        kind = str(p.kind).rsplit(".", 1)[-1]
        if kind in _GENERATE_KINDS:
            return True
        if kind in ("ModuleDeclaration", "InterfaceDeclaration", "ProgramDeclaration"):
            return False
        p = p.parent
    return False


def _other_names(syn: Any) -> frozenset[str]:
    """Names in the module's own scope besides ports, parameters, nets and instances."""
    sk = pyslang.syntax.SyntaxKind
    tk = pyslang.parsing.TokenKind
    names: set[str] = set()

    def block_names(block: Any) -> None:
        if block is None:
            return
        if block.kind == sk.IfGenerate:
            block_names(block.block)
            clause = block.elseClause
            block_names(None if clause is None else clause.clause)
        elif block.kind == sk.LoopGenerate:
            block_names(block.block)
        elif block.kind == sk.GenerateBlock:
            if block.beginName is not None:
                names.add(block.beginName.name.valueText)
            elif block.label is not None:
                names.add(block.label.name.valueText)

    def members(items: Any) -> None:
        for mem in items:
            if mem.kind == sk.TypedefDeclaration:
                names.add(mem.name.valueText)
            elif mem.kind in (sk.FunctionDeclaration, sk.TaskDeclaration):
                last = mem.prototype.name.getLastToken()
                names.add(last.valueText)
            elif mem.kind == sk.GenvarDeclaration:
                names.update(
                    t.valueText
                    for ident in _items(mem.identifiers)
                    for t in _tokens_of(ident)
                    if t.kind == tk.Identifier
                )
            elif mem.kind in (sk.IfGenerate, sk.LoopGenerate):
                block_names(mem)
            elif mem.kind == sk.GenerateRegion:
                members(mem.members)

    members(syn.members)
    return frozenset(names)


def _tokens_of(node: Any) -> list[Any]:
    out: list[Any] = []

    def scan(n: Any) -> None:
        if isinstance(n, pyslang.parsing.Token):
            out.append(n)

    node.visit(scan)
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


def _header_has_macros(hdr: Any, sm: Any) -> bool:
    """Macro tokens anywhere from the module name through the ";"."""
    found = False

    def scan(n: Any) -> None:
        nonlocal found
        if not found and isinstance(n, pyslang.parsing.Token) and sm.isMacroLoc(n.location):
            found = True

    for part in (hdr.name, hdr.imports, hdr.parameters, hdr.ports, hdr.semi):
        if part is None:
            continue
        if isinstance(part, pyslang.parsing.Token):
            scan(part)
        elif hasattr(part, 'visit'):
            part.visit(scan)
        else:
            for item in part:
                item.visit(scan)
    return found


def _port_type_text(dtype: str) -> str:
    """`logic` plus the packed dimensions for an implicit or net-typed ANSI port header."""
    toks = tokens(dtype)
    if not toks or toks[0] in ("[", "signed", "unsigned"):
        return ("logic " + dtype).strip()
    return dtype


def _param_type_text(decl: Any) -> str:
    typ = getattr(decl, "type", None)
    return "" if typ is None else _clean_text(typ)


def _param_kind(decl: Any) -> str:
    if decl.keyword.kind == pyslang.parsing.TokenKind.LocalParamKeyword:
        return "local"
    return "type" if decl.kind == pyslang.syntax.SyntaxKind.TypeParameterDeclaration else "value"


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
            return ConnInfo(
                port,
                expr,
                _first_token(c).location.offset,
                c.getLastToken().range.end.offset,
                c.openParen.location.offset,
            )
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
