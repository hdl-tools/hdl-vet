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
    "AssignInfo",
    "ConnInfo",
    "DeclInfo",
    "Design",
    "Diag",
    "Driver",
    "InstanceInfo",
    "InstanceSite",
    "InstanceSnap",
    "LoadCache",
    "ModuleInfo",
    "ParamInfo",
    "ParamOverride",
    "PortInfo",
    "driven_bases",
    "is_simple_identifier",
    "load",
    "syntax_equivalent",
    "tokens",
]

# Warnings slang leaves off by default but that a port-level edit can trigger:
# an undeclared name in a port connection becomes an implicit net, and a wrong
# width is only truncated or extended. Deliberately not -Wextra / -Wconversion.
_ALWAYS_WARN = ("ImplicitNet", "PortWidthTruncate", "PortWidthExpand", "WidthTruncate", "EmptyInputPortConn")
_DIRECTIONS = {
    "In": "input",
    "Out": "output",
    "InOut": "inout",
    "Ref": "ref",
}
_WS_RE = re.compile(r"\s+")
_IDENT_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
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

_BOM = b"\xef\xbb\xbf"
_SEVERITY = {
    pyslang.DiagnosticSeverity.Note: "note",
    pyslang.DiagnosticSeverity.Warning: "warning",
    pyslang.DiagnosticSeverity.Error: "error",
    pyslang.DiagnosticSeverity.Fatal: "error",
}
_CODE_NAMES: dict[Any, str] = {getattr(pyslang.Diags, n): n for n in dir(pyslang.Diags) if not n.startswith("_")}
_INCLUDE_RE = re.compile(r'`include\s+"([^"\r\n]+)"')
_MAX_INCLUDE_DEPTH = 64
_GROWTH_FACTOR = 4  # start over when the buffers exceed this multiple of the first load's...
_GROWTH_FLOOR = 2000  # ...plus this many
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
    module: str | None = None  # the module the diagnostic is located in (set for dangling / unused entries)
    # the name of the signal the entry is about, slang's first argument (set for dangling / unused entries);
    # not part of equality
    symbol: str | None = field(default=None, compare=False, repr=False)
    # UndrivenPort entries: the port's external name and whether its module reads it; None / False when
    # the symbol could not be resolved (not part of equality)
    port: str | None = field(default=None, compare=False, repr=False)
    read: bool = field(default=False, compare=False, repr=False)


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
    decl_start: int = -1  # the whole declaration (`parameter int X = 1`), which may hold several names
    decl_end: int = -1
    decl_count: int = 1  # names the declaration declares


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
    stmt_start: int = -1  # the whole declaration statement; -1 when it cannot be edited as text
    stmt_end: int = -1
    stmt_count: int = 1  # names the statement declares


@dataclass(frozen=True)
class AssignInfo:
    """One continuous assignment `left = right`."""

    left: str
    right: str
    start: int
    end: int
    stmt_start: int = -1  # the whole `assign ...;` statement; -1 when it cannot be edited as text
    stmt_end: int = -1
    stmt_count: int = 1  # assignments the statement holds


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
    disabled_names: dict[str, str] = field(default_factory=dict)  # identifier in directive-disabled text -> directive
    generate_decls: frozenset[str] = frozenset()  # nets and variables declared inside generate constructs
    generate_assigned: frozenset[str] = frozenset()  # base names assigned inside generate constructs

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
    """What stays valid across the loads of one session.

    File-system answers: a session reads every file once and then re-loads from
    its own bytes, so which real file an include name resolves to, and what a
    path resolves to, cannot change between its loads. Without this, every re-load
    asked the file system again for each include of each source (over a third of a
    re-load on the demo_ss sample).

    Parse trees: unless `reuse` is False, the cache also keeps one slang driver /
    SourceManager and the tree of every source it parsed, so a later load parses
    only the sources whose bytes changed. Whenever reuse is not provably safe (a
    header's bytes changed, an operation failed half-way) the cache starts over
    with a new SourceManager and parses everything; correctness never depends on a
    tree being reused. A cache is bound to one file list and one `top`.

    Pass one instance to every `load` of a session; a load without a cache
    behaves as before.
    """

    def __init__(self, reuse: bool = True) -> None:
        self.includes: dict[tuple[str, Path], Path | None] = {}
        self.paths: dict[Path, Path] = {}
        self.headers: dict[Path, bytes] = {}
        self.reuse = reuse
        self.parsed = 0  # source files parsed by loads using this cache, in total
        self._filelist: Filelist | None = None
        self._top: Any = _UNBOUND
        self._shared: _Shared | None = None

    def bind(self, fl: Filelist) -> None:
        """Tie the cache to one file list: its answers depend on the include directories.

        What the cache keeps depends on the include directories, the defines and the
        root, not on which sources there are: a session adds a source file (or takes
        it back), and the parse trees of the others stay valid.
        """
        if self._filelist is None:
            self._filelist = fl
        elif self._filelist is not fl and (
            (self._filelist.incdirs, self._filelist.defines, self._filelist.root) != (fl.incdirs, fl.defines, fl.root)
        ):
            raise AdapterError("a LoadCache belongs to one file list and cannot be shared with another")

    def bind_top(self, top: str | None) -> None:
        """Tie the cache to one top module: kept trees are parsed with it in their options."""
        if self._top is _UNBOUND:
            self._top = top
        elif self._top != top:
            raise AdapterError(f"a LoadCache belongs to one top module ({self._top!r}); it cannot load top {top!r}")

    def resolved(self, path: Path) -> Path:
        # A relative path means something else after a change of directory.
        if not path.is_absolute():
            return path.resolve()
        out = self.paths.get(path)
        if out is None:
            out = self.paths[path] = path.resolve()
        return out


_UNBOUND = object()


def _header_raw(header: Path, overrides: Mapping[Path, bytes], cache: LoadCache) -> bytes:
    """The bytes of a header: the caller's, else the file's (read once per cache)."""
    raw = overrides.get(header)
    if raw is None:
        raw = cache.headers.get(header)
    if raw is None:
        raw = cache.headers[header] = _read(header, overrides)
    return raw


@dataclass
class _Entry:
    """A source's parse tree and the bytes it was parsed from."""

    raw: bytes
    tree: Any
    deps: frozenset[Path]  # headers the adapter's scan of the text resolved (closure)
    headers: frozenset[Path]  # real paths of the headers slang included while parsing it


class _Unsafe(Exception):
    """slang bound an include the adapter did not predict, or a registered key clashed:
    what an earlier load registered may have leaked into this tree, so the history matters."""


class _Shared:
    """One slang driver and SourceManager, and what was registered on it.

    All trees of a compilation must come from the same SourceManager, and slang
    refuses a second buffer under a path it already holds; so this state only
    grows (new buffers, new synthetic names) and is thrown away as a whole when
    reuse is not provably safe. Designs hold on to the instance they were built
    from, so discarding it never affects them. Nothing a Design reads is ever
    changed after the fact: `real` and `hbytes` only gain keys whose values stay
    the same, `entries` is read only while loading.
    """

    def __init__(self, fl: Filelist, top: str | None) -> None:
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
        self.driver = driver  # keeps the SourceManager and engine alive
        self.sm = driver.sourceManager
        self.engine = driver.diagEngine
        self.fl = fl
        self._names: dict[str, str] = {}
        self._root = Path(fl.root.anchor) / "__hdlvet__"
        self.real: dict[str, Path] = {}  # synthetic buffer path -> real path
        self.hbytes: dict[Path, bytes] = {}  # raw bytes of every header registered
        self.bad: dict[Path, str] = {}  # headers that are not valid UTF-8
        self.bodies: dict[Path, str] = {}
        self.entries: dict[Path, _Entry] = {}
        self.versions = 0
        self.unsafe = False  # reuse was found unsafe: every later load starts over
        self.initial: int | None = None  # buffers after the first complete load
        self.nbuf = 0  # buffers the SourceManager holds, as of the last parse
        self.dirty = False  # a registration is under way; if it fails the state is unusable
        bag = driver.createOptionBag()
        pre = bag.preprocessorOptions
        pre.predefines = [n if v is None else f"{n}={v}" for n, v in fl.defines.items()]
        pre.additionalIncludePaths = [str(self.mirror(d)) for d in fl.incdirs]
        bag.preprocessorOptions = pre
        if top is not None:
            comp_opts = bag.compilationOptions
            comp_opts.topModules = {top}
            bag.compilationOptions = comp_opts
        self.bag = bag

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

    def renamed(self, key: Path) -> Path:
        """A fresh name for the buffer of a source: `mirror()` can never produce it.

        '@' is not in _SAFE_NAME and `_component` never emits it, so no include of a
        real file can resolve to a source's buffer, and slang never holds two
        buffers under one name when a source is parsed again.
        """
        while True:
            self.versions += 1
            cand = key.with_name(f"{key.name}@{self.versions}")
            if _key(cand) not in self.real:
                return cand

    def grown(self) -> bool:
        """True once the SourceManager holds far more buffers than after the first load.

        Every re-parse leaves its buffers (and those of its macro expansions) behind;
        a session that edits one file a thousand times would otherwise carry them all.
        """
        return self.initial is not None and self.nbuf > _GROWTH_FACTOR * self.initial + _GROWTH_FLOOR

    def headers_unchanged(self, overrides: Mapping[Path, bytes], cache: LoadCache) -> bool:
        """True if every registered header still has the bytes it was registered with."""
        for header, raw in self.hbytes.items():
            try:
                if _header_raw(header, overrides, cache) != raw:
                    return False
            except AdapterError:
                return False
        return True

    def included(self, sources: Sequence[Path]) -> set[Path]:
        """Real paths of the headers slang included while parsing exactly `sources`."""
        out: set[Path] = set()
        for src in sources:
            out |= self.entries[src].headers
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

    One `_Loader` serves one load; what outlives it lives in `_Shared`.
    """

    def __init__(
        self,
        fl: Filelist,
        overrides: Mapping[Path, bytes],
        shared: _Shared,
        cache: LoadCache,
        strict: bool = False,
    ) -> None:
        self.strict = strict  # a reused state: anything unexpected means "start over"
        self.fl = fl
        self.overrides = overrides
        self.shared = shared
        self.sm = shared.sm
        self.cache = cache
        self.boms: dict[Path, bool] = {}  # per load: a Design must keep its own

    @property
    def real(self) -> dict[str, Path]:
        return self.shared.real

    def mirror(self, real: Path) -> Path:
        return self.shared.mirror(real)

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
        sh = self.shared
        body = sh.bodies.get(header)
        if body is None:
            # A header the caller did not supply (one named only in an inactive
            # `ifdef branch) is read from disk once per cache, not once per load.
            raw = _header_raw(header, self.overrides, self.cache)
            try:
                body = raw.decode("utf-8")
            except UnicodeDecodeError as e:
                sh.bad[header] = str(e)
                body = raw.decode("utf-8", errors="replace")
            sh.hbytes[header] = raw
            sh.bodies[header] = body
        return body

    def _register_includes(self, text: str, real_dir: Path, depth: int = 0, raw_dir: Path | None = None) -> None:
        """Hand every header `text` includes (recursively) to slang as text.

        `raw_dir` is set when the includer was registered under a raw real path (an
        include named by an absolute path): slang then looks its relative includes up
        next to that raw path, not in the mirror, so they are registered there too.
        """
        if depth > _MAX_INCLUDE_DEPTH:
            return
        sh = self.shared
        for name in _INCLUDE_RE.findall(text):
            header = self._resolve(name, real_dir)
            if header is None:
                continue
            body = self._body(header)
            child_raw: Path | None = None
            if Path(name).is_absolute():
                keys = [Path(name)] if name.isascii() else []
                child_raw = Path(os.path.normpath(name)).parent if keys else None
            else:
                # Where slang looks first: next to the includer. Register that key only
                # when the file really resolves there: a header found through an include
                # directory must not appear next to its includer, or its own relative
                # includes would resolve from the wrong directory.
                keys = []
                if self.cache.resolved(real_dir / name) == header:
                    keys.append(self.mirror(Path(os.path.normpath(real_dir / name))))
                    if raw_dir is not None and name.isascii():
                        raw = Path(os.path.normpath(raw_dir / name))
                        keys.append(raw)
                        child_raw = raw.parent
            keys.append(self.mirror(header))
            fresh = False
            for key in keys:
                k = _key(key)
                known = sh.real.get(k)
                if known is not None:
                    if known != header and self.strict:
                        raise _Unsafe()
                    continue
                sh.real[k] = header
                self.sm.assignText(str(key), body)
                fresh = True
            if fresh:
                self._register_includes(body, header.parent, depth + 1, child_raw)

    def deps(self, text: str, real_dir: Path, seen: set[Path] | None = None, depth: int = 0) -> set[Path]:
        """The closure of headers the scan of `text` resolves (what slang may include)."""
        seen = set() if seen is None else seen
        if depth > _MAX_INCLUDE_DEPTH:
            return seen
        for name in _INCLUDE_RE.findall(text):
            header = self._resolve(name, real_dir)
            if header is None or header in seen:
                continue
            seen.add(header)
            self.deps(self._body(header), header.parent, seen, depth + 1)
        return seen

    def reusable(self, src: Path) -> Any | None:
        """The kept tree of `src` if it was parsed from exactly the bytes now given."""
        entry = self.shared.entries.get(src)
        if entry is None or not self.cache.reuse:
            return None
        raw = _read(src, self.overrides)
        if entry.raw != raw:
            return None
        self.boms[src] = raw.startswith(_BOM)
        return entry.tree

    def tree(self, src: Path, bag: Any) -> Any:
        raw = _read(src, self.overrides)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise AdapterError(f"{src}: not valid UTF-8, refusing to load ({e})") from e
        sh = self.shared
        self.boms[src] = raw.startswith(_BOM)
        key = sh.renamed(self.mirror(src))
        # From here until the tree is stored the state is half-registered.
        sh.dirty = True
        sh.real[_key(key)] = src
        self._register_includes(text, src.parent)
        deps = frozenset(self.deps(text, src.parent))
        before = len(self.sm.getAllBuffers())
        tree = pyslang.syntax.SyntaxTree.fromText(text, self.sm, src.name, str(key), bag)
        buffers = self.sm.getAllBuffers()
        sh.nbuf = len(buffers)
        headers: set[Path] = set()
        unexpected = False
        for buf in buffers[before:]:
            if self.sm.getIncludedFrom(buf).buffer.id == 0:
                continue  # not an include
            path = sh.real.get(_key(Path(str(self.sm.getFullPath(buf)))))
            if path is None:
                unexpected = True
            else:
                headers.add(path)
        if not headers <= deps:
            unexpected = True
        if unexpected and self.cache.reuse:
            # slang bound an include the scan did not predict (a name built by a macro):
            # it may have found a header an earlier load registered. This state's
            # history matters from now on, so it is not kept.
            sh.unsafe = True
            if self.strict:
                raise _Unsafe()
        sh.entries[src] = _Entry(raw, tree, deps, frozenset(headers))
        sh.dirty = False
        self.cache.parsed += 1
        return tree


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
    included file is an error. `cache` carries what stays valid from one load
    of a session to the next (see LoadCache): file-system answers and parse trees.
    """
    fl = filelist if isinstance(filelist, Filelist) else read_filelist(filelist, base=base)
    if cache is None:
        ov = {Path(k).resolve(): bytes(v) for k, v in (overrides or {}).items()}
        # Even one load asks for the same header many times; a private cache
        # covers that. Nothing of it outlives the load.
        use = LoadCache(reuse=False)
    else:
        cache.bind(fl)
        cache.bind_top(top)
        ov = {cache.resolved(Path(k)): bytes(v) for k, v in (overrides or {}).items()}
        use = cache

    shared = use._shared if use.reuse else None
    if shared is not None and (shared.unsafe or shared.grown() or not shared.headers_unchanged(ov, use)):
        shared = None  # not provably safe (or too big) to reuse: start over
    for _attempt in (0, 1):
        reused = shared is not None
        if shared is None:
            shared = _Shared(fl, top)
            if use.reuse:
                use._shared = shared
        try:
            return _load(fl, top, ov, check_overrides, use, shared, strict=reused)
        except _Unsafe:
            shared = use._shared = None  # redo the load on a fresh state
        except BaseException as e:
            # Entries are stored only once whole, so a refusal before any registration
            # (bad UTF-8, top not found) leaves the state valid; anything else may
            # have left a half-registered header or version behind.
            if shared.dirty or not isinstance(e, AdapterError):
                use._shared = None
                if reused and not isinstance(e, AdapterError):
                    shared = None  # the reused state may be why (slang remembers failed lookups)
                    continue
            raise
    raise AssertionError("a fresh state cannot be unsafe")  # pragma: no cover


def _load(
    fl: Filelist,
    top: str | None,
    ov: dict[Path, bytes],
    check_overrides: bool,
    cache: LoadCache,
    shared: _Shared,
    strict: bool = False,
) -> Design:
    loader = _Loader(fl, ov, shared, cache, strict)
    bag = shared.bag
    trees = []
    for src in fl.files:
        tree = loader.reusable(src)
        trees.append(tree if tree is not None else loader.tree(src, bag))
    if shared.initial is None:
        shared.initial = shared.nbuf
    used = shared.included(fl.files)
    for header in sorted(used & shared.bad.keys()):
        raise AdapterError(f"{header}: not valid UTF-8, refusing to load ({shared.bad[header]})")
    if check_overrides:
        for key in ov:
            if key not in fl.files and key not in used:
                raise AdapterError(f"{key}: override is neither a source nor an included file")

    comp = pyslang.ast.Compilation(bag)
    for tree in trees:
        comp.addSyntaxTree(tree)
    if top is not None and not comp.getRoot().topInstances:
        raise AdapterError(f"top module {top!r} not found")
    return Design(fl, loader, shared, dict(zip(fl.files, trees, strict=True)), comp, used)


class Design:
    def __init__(
        self,
        fl: Filelist,
        loader: _Loader,
        shared: _Shared,
        trees: dict[Path, Any],
        comp: Any,
        included: set[Path],
    ) -> None:
        self._fl = fl
        self._real = shared.real  # only ever gains keys
        self._hbytes = shared.hbytes  # a header's bytes never change once registered
        self._boms = loader.boms  # this load's own
        self._sm = loader.sm
        self._trees = trees
        self._comp = comp
        self._engine = shared.engine
        self._included = included
        self._diags: list[Diag] | None = None
        self._analysis: Any = None
        self._unused: list[Diag] | None = None
        self._spans: list[tuple[Path, int, int, str]] | None = None
        self._snap: dict[str, InstanceSnap] | None = None
        self._instance_cache: tuple[Any, ...] | None = None
        self._modules: dict[str, ModuleInfo] = {}
        self._buffer_paths: dict[int, Path | None] = {}
        self._buffer_texts: dict[int, bytes] = {}

    @property
    def files(self) -> list[Path]:
        return list(self._fl.files)

    @property
    def filelist(self) -> Filelist:
        return self._fl

    @property
    def included(self) -> list[Path]:
        """Headers slang actually included, sorted."""
        return sorted(self._included)

    @property
    def included_bytes(self) -> dict[Path, bytes]:
        """The bytes this load read for each header it actually included."""
        return {p: self._hbytes[p] for p in self.included if p in self._hbytes}

    # --- helpers -----------------------------------------------------------

    def _buffer_path(self, buffer: Any) -> Path | None:
        # Asked once per token while a module's model is built; the answer for a
        # buffer never changes.
        known = self._buffer_paths
        if buffer.id not in known:
            known[buffer.id] = self._real.get(_key(Path(str(self._sm.getFullPath(buffer)))))
        return known[buffer.id]

    def _buffer_bytes(self, buffer: Any) -> bytes:
        data = self._buffer_texts.get(buffer.id)
        if data is None:
            data = self._buffer_texts[buffer.id] = str(self._sm.getSourceText(buffer)).encode()
        return data

    def _plain(self, node: Any) -> bool:
        """True when `node` certainly has no macro-generated token and no directive inside.

        Both need a backtick somewhere in the node's own source text: a macro use,
        an `include, or an `ifdef between two of its tokens. When the node starts
        and ends in the same file buffer and that stretch of text has no backtick,
        the per-token scans can be skipped. They cost a Python call per token and
        were most of the time of building a large module's model. A backtick in a
        comment or string only means the scans run as before.
        """
        first, last = node.getFirstToken(), node.getLastToken()
        if not first or not last:
            return False
        a, b = first.location, last.location
        sm = self._sm
        if sm.isMacroLoc(a) or sm.isMacroLoc(b) or a.buffer != b.buffer:
            return False
        return b"`" not in self._buffer_bytes(a.buffer)[a.offset : last.range.end.offset]

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
        out = str(pyslang.syntax.SyntaxPrinter.printFile(tree)).encode()
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
                if d.definitionKind not in kinds or d.syntax is None:
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

        self._visit_instances(one)
        analysis = self._analyze()
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

    def defined_in(self, path: Path) -> list[str]:
        """Names of the modules, interfaces and programs whose definition lies in the file `path`."""
        dk = pyslang.ast.DefinitionKind
        kinds = (dk.Module, dk.Interface, dk.Program)
        out: set[str] = set()
        for d in self._comp.getDefinitions():
            if d.definitionKind not in kinds or d.syntax is None:
                continue
            first = d.syntax.getFirstToken()
            if first and self._buffer_path(self._original(first.location).buffer) == path:
                out.add(d.name)
        return sorted(out)

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
        conns: dict[str, str] = {}
        for port in body.portList:
            if port.kind == sk.Port:
                ports[port.name] = (_DIRECTIONS[port.direction.name], port.type.bitWidth)
                port_types[port.name] = str(port.type)
            else:
                ports[port.name] = ("interface", 0)
            if not is_top:
                conns[port.name] = self._connection_text(port, sym.getPortConnection(port))
        return InstanceSnap(body.definition.name, params, ports, conns, port_types)

    def instance_snap(self, path: str) -> InstanceSnap | None:
        """The snapshot of the one instance at `path`, without building the whole snapshot.

        None when there is no instance at that path. Equal to `snapshot().get(path)`.
        """
        if self._snap is not None:
            return self._snap.get(path)
        sym = self._comp.getRoot().lookupName(path)
        if sym is None or sym.kind != pyslang.ast.SymbolKind.Instance:
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

    def _sites_of(self, inst: Any, plain: bool | None = None) -> list[InstanceSite]:
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
                declarators = _items(decl.declarators)
                for d in declarators:
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
                            decl_start=decl.getFirstToken().location.offset,
                            decl_end=decl.getLastToken().range.end.offset,
                            decl_count=len(declarators),
                        )
                    )
        instances: list[InstanceInfo] = []
        plain = self._plain(syn)
        has_macros = False if plain else _has_macro_tokens(syn, self._sm)

        def visit(node: Any) -> None:
            if node.kind == sk.HierarchyInstantiation:
                check(_first_token(node), f"instantiation of {node.type.valueText!r}")
                instances.extend(self._instances_of(node, path))

        syn.visit(visit)
        decls, assigns = self._body_items(syn, param_kinds, hdr.parameters is None, path)
        gen_decls, gen_assigned = _generate_names(syn)
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
            port_list_kind="none" if hdr.ports is None else "ansi" if hdr.ports.kind == sk.AnsiPortList else "nonansi",
            header_end=hdr.semi.range.end.offset,
            body_indent=self._body_indent(syn),
            decls=decls,
            assigns=assigns,
            header_has_macros=False if plain else _header_has_macros(hdr, self._sm),
            param_kinds=param_kinds,
            other_names=_other_names(syn),
            imports_end=hdr.imports[-1].getLastToken().range.end.offset if len(hdr.imports) else None,
            ports_directive=None if plain or hdr.ports is None else _conditional_directive(hdr.ports),
            params_directive=None if plain or hdr.parameters is None else _conditional_directive(hdr.parameters),
            disabled_names={} if plain else _disabled_names(syn),
            generate_decls=gen_decls,
            generate_assigned=gen_assigned,
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

    def _stmt_range(self, node: Any, path: Path) -> tuple[int, int]:
        """Byte range of a whole statement in `path`, or (-1, -1) when it is not plain text of that file."""
        first, last = node.getFirstToken(), node.getLastToken()
        if not first or not last:
            return -1, -1
        a, b = first.location, last.location
        if self._sm.isMacroLoc(a) or self._sm.isMacroLoc(b) or a.buffer != b.buffer:
            return -1, -1
        if self._buffer_path(a.buffer) != path:
            return -1, -1
        return a.offset, last.range.end.offset

    def _body_items(
        self, syn: Any, param_kinds: dict[str, str], body_params_overridable: bool, path: Path
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
                s0, s1 = self._stmt_range(mem, path)
                names = _items(mem.declarators)
                for d in names:
                    dims = " ".join(_clean_text(x) for x in d.dimensions)
                    decls.append(
                        DeclInfo(
                            d.name.valueText,
                            "net" if is_net else "var",
                            (base + " " + dims).strip(),
                            d.getFirstToken().location.offset,
                            d.getLastToken().range.end.offset,
                            s0,
                            s1,
                            len(names),
                        )
                    )
            elif mem.kind == sk.ContinuousAssign:
                s0, s1 = self._stmt_range(mem, path)
                items = _items(mem.assignments)
                for a in items:
                    if a.kind != sk.AssignmentExpression:
                        continue
                    assigns.append(
                        AssignInfo(
                            _clean_text(a.left),
                            _clean_text(a.right),
                            a.getFirstToken().location.offset,
                            a.getLastToken().range.end.offset,
                            s0,
                            s1,
                            len(items),
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
        dirs: dict[Any, str] = {
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
                direction = (
                    "interface"
                    if mem.header.kind == sk.InterfacePortHeader
                    else dirs.get(getattr(tok, "kind", None), "inout")
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
        plain = self._plain(node)
        from_macro = False if plain else _has_macro_tokens(node, self._sm)
        module = node.type.valueText
        # An instantiation whose instance has no name (text an edit glued together) is not an instance.
        hier = [i for i in node.instances if i.kind == sk.HierarchicalInstance and i.decl is not None]
        try:
            sites: list[InstanceSite | None] = list(self._sites_of(node, plain))
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
                        open=a.openParen.location.offset if a.kind == sk.NamedParamAssignment and a.openParen else None,
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
                    conn_directive=None if plain else _conditional_directive(hi, skip_first=False),
                    override_directive=None
                    if plain or node.parameters is None
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


def _merge_ranges(spans: Sequence[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    """Inclusive integer ranges, sorted, with overlapping and adjacent ones joined."""
    out: list[list[int]] = []
    for lo, hi in sorted((min(a, b), max(a, b)) for a, b in spans):
        if out and lo <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return tuple((lo, hi) for lo, hi in out)


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


def _disabled_names(syn: Any) -> dict[str, str]:
    """Identifiers in text a conditional directive disables inside `syn`, with the directive."""
    out: dict[str, str] = {}
    ident = pyslang.parsing.TokenKind.Identifier

    first = True

    def scan(n: Any) -> None:
        nonlocal first
        if not isinstance(n, pyslang.parsing.Token):
            return
        if first:  # the trivia of the first token lies before the module
            first = False
            return
        for t in n.trivia:
            if t.kind != pyslang.parsing.TriviaKind.Directive:
                continue
            node = t.syntax()
            disabled = getattr(node, "disabledTokens", None)
            if node is None or not disabled:
                continue
            kind = _DIRECTIVES.get(str(node.kind).rsplit(".", 1)[-1], "a conditional directive")
            expr = getattr(node, "expr", None)
            label = kind if expr is None else f"{kind} {str(expr).strip()}"
            for tok in disabled:
                if tok.kind == ident:
                    out.setdefault(tok.rawText, label)

    syn.visit(scan)
    return out


def _generate_names(syn: Any) -> tuple[frozenset[str], frozenset[str]]:
    """Names declared, and base names assigned, inside the generate constructs of a module."""
    sk = pyslang.syntax.SyntaxKind
    decls: set[str] = set()
    assigned: set[str] = set()

    def scan(n: Any) -> None:
        if isinstance(n, pyslang.parsing.Token):
            return
        if n.kind in (sk.DataDeclaration, sk.NetDeclaration):
            decls.update(d.name.valueText for d in _items(n.declarators))
        elif n.kind == sk.ContinuousAssign:
            for a in _items(n.assignments):
                if a.kind == sk.AssignmentExpression:
                    assigned.update(driven_bases(_clean_text(a.left)))

    for mem in syn.members:
        if mem.kind in (sk.IfGenerate, sk.LoopGenerate, sk.CaseGenerate, sk.GenerateRegion):
            mem.visit(scan)
    return frozenset(decls), frozenset(assigned)


def _tokens_of(node: Any) -> list[Any]:
    out: list[Any] = []

    def scan(n: Any) -> None:
        if isinstance(n, pyslang.parsing.Token):
            out.append(n)

    node.visit(scan)
    return out


def _reads_symbol(body: Any, symbol: Any) -> bool:
    """True when an expression in `body` names `symbol`."""
    hits: list[int] = []

    def visit(node: Any) -> None:
        if isinstance(node, pyslang.ast.NamedValueExpression) and node.symbol == symbol:
            hits.append(1)

    body.visit(visit)
    return bool(hits)


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
        elif hasattr(part, "visit"):
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
        if not found and isinstance(n, pyslang.parsing.Token) and n.location.buffer == buf and n.location.offset >= cut:
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
