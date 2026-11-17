"""`Design`: one load of a file list, its files, round trip and cache of what it computed."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyslang

from hdl_vet.filelist import Filelist
from hdl_vet.slang_adapter.diagnostics import _DiagnosticsOps
from hdl_vet.slang_adapter.hierrefs import _HierRefOps
from hdl_vet.slang_adapter.model import _ModelOps
from hdl_vet.slang_adapter.records import AdapterError, Diag, InstanceSnap, ModuleInfo
from hdl_vet.slang_adapter.snapshots import _SnapshotOps
from hdl_vet.slang_adapter.text import _BOM, _key

if TYPE_CHECKING:
    from hdl_vet.slang_adapter.loading import _Loader, _Shared


class Design(_DiagnosticsOps, _SnapshotOps, _HierRefOps, _ModelOps):
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
        self._gate_snap: dict[str, InstanceSnap] | None = None
        self._udps: frozenset[str] | None = None
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
