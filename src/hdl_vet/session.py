"""The edit session: queue byte-range splices, re-load on demand, sign off.

A `Session` loads the baseline design once. Scripts queue splices against the
text of the last (re-)load; the design is re-loaded only when a query needs the
edited text. `finish` adds the hdl-vet header to every changed file, re-loads, and
compares the result with the baseline: new diagnostics (located, so shifted or
renamed ones are not new) and structural changes against declared expectations.

Every (re-)load is built from the session's own state: the file list is parsed
once and every source and header is read once, at construction; later loads pass
all of those bytes as overrides and never consult the file system, the
environment or the working directory again (a header that an edit newly includes
is the one exception, and sign-off rejects it).

This module does not import pyslang; it talks to `hdl_vet.slang_adapter`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from hdl_vet.filelist import Filelist, read_filelist
from hdl_vet.signoff import Change, Expect, check_changes, diff_snapshots, new_diagnostics
from hdl_vet.slang_adapter import AdapterError, Design, Diag, InstanceSnap, ModuleInfo, load
from hdl_vet.splice import OffsetMap, Splice, SpliceQueue, apply_splices, unified_diff

__all__ = ["HEADER_MARK", "Result", "Session", "SessionError"]

HEADER_MARK = b"// Modified by hdl-vet"
_BOM = b"\xef\xbb\xbf"
# The marker counts only at the start of a line, as a whole: followed by the end of
# the line or the file, or by ";".
_MARK_RE = re.compile(re.escape(HEADER_MARK) + rb"(?=[;\r\n]|\Z)", re.MULTILINE)
_BREAK_RE = re.compile(rb"\r\n|\n|\r")


class SessionError(Exception):
    """Raised for unusable input or an invalid request to the session."""


@dataclass(frozen=True)
class Result:
    ok: bool
    new_diagnostics: list[Diag]
    changes: list[Change]
    unexpected_changes: list[Change]
    unmatched_expects: list[Expect]
    files: dict[Path, bytes]
    diff: str
    layout_root: Path | None = field(default=None, repr=False)
    new_files: list[Path] = field(default_factory=list)
    load_error: str | None = None
    failed_checks: list[str] = field(default_factory=list)

    def _rel(self, path: Path | None) -> str:
        if path is None:
            return "<no file>"
        if self.layout_root is not None:
            try:
                return path.relative_to(self.layout_root).as_posix()
            except ValueError:
                pass
        return path.as_posix()

    def report(self) -> str:
        lines = ["hdl-vet sign-off: " + ("ok" if self.ok else "FAILED")]
        if self.load_error is not None:
            lines.append(f"  the edited design cannot be loaded: {self.load_error}")
        for p in self.new_files:
            lines.append(f"  new file read by the edited design, not in the baseline: {self._rel(p)}")
        for d in self.new_diagnostics:
            where = self._rel(d.path) + (f":{d.line}" if d.line is not None else "")
            lines.append(f"  new diagnostic: {d.severity} {d.code} at {where}: {d.message}")
        for c in self.unexpected_changes:
            name = f" {c.name}" if c.name else ""
            lines.append(f"  unexpected change: {c.kind} {c.path}{name}: {c.before!r} -> {c.after!r}")
        for e in self.unmatched_expects:
            lines.append(f"  expected change not seen: {e.kind} path={e.path} name={e.name}")
        for msg in self.failed_checks:
            lines.append(f"  failed check: {msg}")
        return "\n".join(lines) + "\n"


def _has_header(data: bytes) -> bool:
    body = data[len(_BOM) :] if data.startswith(_BOM) else data
    return _MARK_RE.search(body) is not None


def _eol(data: bytes) -> bytes:
    """The file's line ending: CRLF if its first break is CRLF, else LF."""
    m = _BREAK_RE.search(data)
    return b"\r\n" if m is not None and m.group() == b"\r\n" else b"\n"


def _header_splice(path: Path, data: bytes, scripts: Sequence[tuple[str, str]]) -> Splice:
    """The insertion of the header line, directly after the leading comment block."""
    eol = _eol(data)
    line = HEADER_MARK
    if scripts:
        line += b"; scripts: " + ", ".join(f"{n} sha256:{h[:12]}" for n, h in scripts).encode()
    first = len(_BOM) if data.startswith(_BOM) else 0
    point = first
    need_break = False
    if data.startswith(b"/*", first):
        close = data.find(b"*/", first + 2)
        if close >= 0:
            end = close + 2
            m = _BREAK_RE.search(data, end)
            rest = data[end:] if m is None else data[end : m.start()]
            if not rest.strip(b" \t"):
                point = len(data) if m is None else m.end()
                need_break = m is None
    else:
        j = first
        while data.startswith(b"//", j):
            m = _BREAK_RE.search(data, j)
            if m is None:
                j, need_break = len(data), True
            else:
                j = m.end()
        point = j
    return Splice(path, point, point, (eol if need_break else b"") + line + eol)


class Session:
    def __init__(
        self,
        filelist: Path,
        top: str | None = None,
        editable: Collection[Path] | None = None,
        base: Path | None = None,
    ) -> None:
        self._filelist_path = Path(filelist).resolve()
        self._top = top
        self._fl: Filelist = read_filelist(self._filelist_path, base=base)
        self._orig: dict[Path, bytes] = {}
        for p in self._fl.files:
            self._orig[p] = self._read(p)
        self._design: Design = load(self._fl, top=top, overrides=dict(self._orig))
        self._baseline = self._design
        for p, data in self._baseline.included_bytes.items():
            self._orig.setdefault(p, data)
        self.reloads = 0
        self._finished = False
        self._expects: list[Expect] = []
        self._files = list(self._orig)
        sources = set(self._fl.files)
        self._headers = [p for p in self._files if p not in sources]
        self._text = dict(self._orig)
        self._editable = None if editable is None else {Path(p).resolve() for p in editable}
        self._queue: dict[Path, SpliceQueue] = {}
        self._maps: dict[Path, list[OffsetMap]] = {}

        for p in self._files:
            if _has_header(self._orig[p]):
                raise SessionError(
                    f"{p}: already carries the hdl-vet header; the input must be an unmodified tree"
                )
        errors = [d for d in self._baseline.diagnostics() if d.severity == "error"]
        if errors:
            d = errors[0]
            where = f"{d.path}:{d.line}: " if d.path is not None else ""
            raise SessionError(
                f"the baseline design has {len(errors)} error(s); first: {where}{d.code}: {d.message}"
            )
        self.layout_root = self._compute_layout_root()

    @staticmethod
    def _read(path: Path) -> bytes:
        try:
            return path.read_bytes()
        except OSError as e:
            raise SessionError(f"{path}: cannot read ({e})") from e

    # --- inputs --------------------------------------------------------------

    @property
    def top(self) -> str | None:
        return self._top

    @property
    def filelist(self) -> Filelist:
        return self._fl

    @property
    def filelist_path(self) -> Path:
        return self._filelist_path

    @property
    def files(self) -> list[Path]:
        """Every source and included file of the design."""
        return list(self._files)

    def original(self, path: Path) -> bytes:
        """The bytes of `path` as they were when the session started."""
        try:
            return self._orig[Path(path).resolve()]
        except KeyError:
            raise SessionError(f"{path}: not a source or included file of this design") from None

    def _compute_layout_root(self) -> Path:
        # `fl.root` is `base` when one was given, else the directory of the file list.
        paths = [*self._files, *self._fl.incdirs, self._fl.root]
        try:
            return Path(os.path.commonpath([str(p) for p in paths]))
        except ValueError as e:
            raise SessionError(f"the design's files have no common directory ({e})") from e

    def text(self, path: Path) -> bytes:
        try:
            return self._text[Path(path).resolve()]
        except KeyError:
            raise SessionError(f"{path}: not a source or included file of this design") from None

    # --- edits ---------------------------------------------------------------

    def splice(self, path: Path, start: int, end: int, text: bytes, old: bytes | None = None) -> None:
        key = Path(path).resolve()
        if key not in self._text:
            raise SessionError(f"{path}: not a source or included file of this design")
        if self._editable is not None and key not in self._editable:
            raise SessionError(f"{path}: is not editable in this session")
        current = self._text[key]
        if old is not None and current[start:end] != old:
            raise SessionError(
                f"{path}: bytes [{start}, {end}) are {current[start:end]!r}, expected {old!r}"
            )
        queue = self._queue.get(key)
        if queue is None:
            queue = self._queue[key] = SpliceQueue(len(current))
        queue.add(Splice(key, start, end, bytes(text)))

    def is_editable(self, path: Path) -> bool:
        """True when `splice` would accept `path`: a source or included file that is editable."""
        key = Path(path).resolve()
        return key in self._text and (self._editable is None or key in self._editable)

    def expect(
        self, kind: str, path: str = "*", name: str = "*", *, exact: bool = False, below: bool = False
    ) -> None:
        self._expects.append(Expect(kind, path, name, exact=exact, below=below))

    def _apply_queue(self) -> None:
        """Apply the queued splices to the text and record the stage."""
        for key in sorted(self._queue, key=lambda p: p.as_posix()):
            splices = self._queue[key].splices()
            if not splices:
                continue
            self._text[key] = apply_splices(self._text[key], splices)
            self._maps.setdefault(key, []).append(OffsetMap(splices))
        self._queue = {}

    def permit(
        self, kind: str, path: str = "*", name: str = "*", *, exact: bool = False, below: bool = False
    ) -> None:
        """Allow a change without requiring it."""
        self._expects.append(Expect(kind, path, name, required=False, exact=exact, below=below))

    @property
    def loaded(self) -> Design:
        """The last loaded design; never re-loads."""
        return self._design

    def refresh(self) -> bool:
        """Apply the queue and re-load when anything is queued; True if it re-loaded."""
        before = self.reloads
        self._refresh()
        return self.reloads != before

    def baseline_snapshot(self) -> dict[str, InstanceSnap]:
        """The structural snapshot of the baseline design (cached; sign-off needs it too)."""
        return self._baseline.snapshot()

    def _reload(self) -> None:
        self._design = load(self._fl, top=self._top, overrides=dict(self._text), check_overrides=False)
        self.reloads += 1

    def _refresh(self) -> None:
        if any(len(q) for q in self._queue.values()):
            self._apply_queue()
            self._reload()

    def _dirty(self, path: Path) -> bool:
        q = self._queue.get(path)
        return q is not None and len(q) > 0

    # --- queries -------------------------------------------------------------

    def module(self, name: str) -> ModuleInfo:
        try:
            info = self._design.module(name)
        except AdapterError:
            if not any(len(q) for q in self._queue.values()):
                raise
            self._refresh()
            return self._design.module(name)
        if self._dirty(info.path) or any(self._dirty(p) for p in self._headers):
            self._refresh()
            return self._design.module(name)
        return info

    @property
    def design(self) -> Design:
        self._refresh()
        return self._design

    # --- sign-off ------------------------------------------------------------

    def finish(
        self,
        scripts: Iterable[tuple[str, str]] = (),
        checks: Iterable[Callable[[Design], str | None]] = (),
    ) -> Result:
        if self._finished:
            raise SessionError("the session is already finished")
        self._finished = True
        scripts = list(scripts)
        checks = list(checks)
        self._apply_queue()
        changed = sorted((p for p in self._text if self._text[p] != self._orig[p]), key=lambda p: p.as_posix())
        for p in changed:
            hs = _header_splice(p, self._text[p], scripts)
            self._text[p] = apply_splices(self._text[p], [hs])
            self._maps.setdefault(p, []).append(OffsetMap([hs]))
        if not changed:
            unexpected, unmatched = check_changes([], self._expects)
            failed = self._run_checks(checks, self._baseline)
            return Result(
                not unmatched and not failed, [], [], unexpected, unmatched, {}, "", self.layout_root,
                failed_checks=failed,
            )

        files = {p: self._text[p] for p in changed}
        diff = "".join(
            unified_diff(p, self._orig[p], self._text[p], label=p.relative_to(self.layout_root).as_posix())
            for p in changed
        )
        try:
            self._reload()
        except AdapterError as e:
            return Result(False, [], [], [], [], files, diff, self.layout_root, [], str(e))
        new_files = sorted((p for p in self._design.included if p not in self._text), key=lambda p: p.as_posix())
        new = new_diagnostics(self._baseline.diagnostics(), self._design.diagnostics(), self._maps)
        changes = diff_snapshots(self._baseline.snapshot(), self._design.snapshot())
        unexpected, unmatched = check_changes(changes, self._expects)
        failed = self._run_checks(checks, self._design)
        ok = not new and not unexpected and not unmatched and not new_files and not failed
        return Result(
            ok, new, changes, unexpected, unmatched, files, diff, self.layout_root, new_files,
            failed_checks=failed,
        )

    def _run_checks(self, checks: Sequence[Callable[[Design], str | None]], design: Design) -> list[str]:
        out: list[str] = []
        for check in checks:
            try:
                msg = check(design)
            except Exception as e:
                msg = f"check raised {type(e).__name__}: {e}"
            if msg is not None:
                out.append(msg)
        return out
