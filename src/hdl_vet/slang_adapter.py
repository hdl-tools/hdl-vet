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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyslang

from hdl_vet.filelist import Filelist, read_filelist

__all__ = [
    "AdapterError",
    "Design",
    "Diag",
    "Driver",
    "InstanceSite",
    "load",
    "syntax_equivalent",
]

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


class AdapterError(Exception):
    """Raised when a design cannot be loaded or a query cannot be answered."""


@dataclass(frozen=True)
class Diag:
    severity: str
    code: str
    message: str
    path: Path | None
    line: int | None


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
    includes is handed over as text under a synthetic ASCII path (`fromText` for
    sources, `assignText` for headers), which sidesteps its narrow-string path
    handling. slang resolves an include next to the including buffer, so each
    header is registered next to its includer; Python mirrors slang's search
    order (the includer's real directory, then the include directories) to find
    the bytes, applying overrides.
    """

    def __init__(self, fl: Filelist, overrides: Mapping[Path, bytes], sm: Any) -> None:
        self.fl = fl
        self.overrides = overrides
        self.sm = sm
        self.real: dict[str, Path] = {}  # synthetic buffer path -> real path
        self.boms: dict[Path, bool] = {}
        self.bad: dict[Path, str] = {}  # headers that are not valid UTF-8
        self._vdirs: dict[Path, Path] = {}
        self._root = Path(Path.cwd().anchor) / "__hdlvet__"

    def _vdir(self, real_dir: Path) -> Path:
        if real_dir not in self._vdirs:
            self._vdirs[real_dir] = self._root / f"d{len(self._vdirs)}"
        return self._vdirs[real_dir]

    def _resolve(self, name: str, real_dir: Path) -> Path | None:
        for base in (real_dir, *self.fl.incdirs):
            cand = (base / name).resolve()
            if cand in self.overrides or cand.is_file():
                return cand
        return None

    def _register_includes(self, text: str, vdir: Path, real_dir: Path, depth: int = 0) -> None:
        if depth > _MAX_INCLUDE_DEPTH:
            return
        for name in _INCLUDE_RE.findall(text):
            key = vdir / name
            if _key(key) in self.real:
                continue
            header = self._resolve(name, real_dir)
            if header is None:
                continue
            raw = _read(header, self.overrides)
            try:
                body = raw.decode("utf-8")
            except UnicodeDecodeError as e:
                self.bad[header] = str(e)
                body = raw.decode("utf-8", errors="replace")
            self.real[_key(key)] = header
            self.sm.assignText(str(key), body)
            self._register_includes(body, key.parent, header.parent, depth + 1)

    def tree(self, src: Path, index: int, bag: Any) -> Any:
        raw = _read(src, self.overrides)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise AdapterError(f"{src}: not valid UTF-8, refusing to load ({e})") from e
        self.boms[src] = raw.startswith(_BOM)
        vdir = self._vdir(src.parent)
        key = vdir / f"s{index}.sv"
        self.real[_key(key)] = src
        self._register_includes(text, vdir, src.parent)
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
    filelist: Path, top: str | None = None, overrides: Mapping[Path, bytes] | None = None
) -> Design:
    fl = read_filelist(filelist)
    ov = {Path(k).resolve(): bytes(v) for k, v in (overrides or {}).items()}

    # A driver is used only for its configured diagnostic engine, option defaults
    # and SourceManager; "-" is a placeholder input that is never read.
    driver = pyslang.driver.Driver()
    driver.addStandardArgs()
    if not driver.parseCommandLine("slang -", pyslang.driver.CommandLineOptions()):
        raise AdapterError("slang rejected its default command line")
    if not driver.processOptions():
        raise AdapterError("slang rejected its default options")
    bag = driver.createOptionBag()
    pre = bag.preprocessorOptions
    pre.predefines = [n if v is None else f"{n}={v}" for n, v in fl.defines.items()]
    bag.preprocessorOptions = pre
    if top is not None:
        comp_opts = bag.compilationOptions
        comp_opts.topModules = {top}
        bag.compilationOptions = comp_opts
    loader = _Loader(fl, ov, driver.sourceManager)

    trees = [loader.tree(src, i, bag) for i, src in enumerate(fl.files)]
    used = loader.included()
    for header in sorted(used & loader.bad.keys()):
        raise AdapterError(f"{header}: not valid UTF-8, refusing to load ({loader.bad[header]})")
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
        self._real = loader.real
        self._boms = loader.boms
        self._sm = loader.sm
        self._trees = trees
        self._comp = comp
        self._engine = engine
        self._diags: list[Diag] | None = None
        self._analysis: Any = None

    @property
    def files(self) -> list[Path]:
        return list(self._fl.files)

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
            loc = self._original(diag.location)
            path: Path | None = None
            line: int | None = None
            offset = 0
            if loc.buffer.id != 0 and loc != pyslang.SourceLocation.NoLocation:
                path = self._buffer_path(loc.buffer)
                line = self._sm.getLineNumber(loc)
                offset = loc.offset
            code = _CODE_NAMES.get(diag.code, str(diag.code))
            message = engine.formatMessage(diag)
            out.append(((str(path or ""), offset, code, message), Diag(severity, code, message, path, line)))
        out.sort(key=lambda item: item[0])
        return [d for _, d in out]

    # --- elaborated structure ----------------------------------------------

    def elaborated_instance_count(self) -> int:
        count = 0

        def visit(node: Any) -> None:
            nonlocal count
            if node.kind == pyslang.ast.SymbolKind.Instance:
                count += 1

        self._comp.getRoot().visit(visit)
        return count

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
        first, last = inst.getFirstToken(), inst.getLastToken()
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


def syntax_equivalent(a: str, b: str) -> bool:
    trees = []
    for text in (a, b):
        tree = pyslang.syntax.SyntaxTree.fromText(text)
        if any(d.isError() for d in tree.diagnostics):
            raise AdapterError(f"text does not parse: {text!r}")
        trees.append(tree)
    return bool(trees[0].root.isEquivalentTo(trees[1].root))
