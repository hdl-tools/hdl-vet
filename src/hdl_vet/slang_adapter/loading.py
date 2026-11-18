"""Loading: the load cache, and the shared state and the loader that build syntax trees from bytes."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyslang

from hdl_vet.filelist import Filelist
from hdl_vet.slang_adapter.records import AdapterError
from hdl_vet.slang_adapter.text import _BOM, _key, _read

# Warnings slang leaves off by default but that a port-level edit can trigger:
# an undeclared name in a port connection becomes an implicit net, and a wrong
# width is only truncated or extended. Deliberately not -Wextra / -Wconversion.
_ALWAYS_WARN = ("ImplicitNet", "PortWidthTruncate", "PortWidthExpand", "WidthTruncate", "EmptyInputPortConn")


_INCLUDE_RE = re.compile(r'`include\s+"([^"\r\n]+)"')
_MAX_INCLUDE_DEPTH = 64
_GROWTH_FACTOR = 4  # start over when the buffers exceed this multiple of the first load's...
_GROWTH_FLOOR = 2000  # ...plus this many
_SAFE_NAME = re.compile(r"[A-Za-z0-9._-]+")


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

    def __init__(self, fl: Filelist, top: str | None, overrides: Mapping[str, str] | None = None) -> None:
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
            if overrides:  # the values of the parameters of the top module, as the tool option -G gives them
                comp_opts.paramOverrides = [f"{name}={value}" for name, value in overrides.items()]
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
