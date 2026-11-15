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

import copy
import os
import re
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hdl_vet.filelist import Filelist, read_filelist
from hdl_vet.signoff import Change, Expect, check_changes, diff_snapshots, new_diagnostics
from hdl_vet.slang_adapter import AdapterError, Design, Diag, InstanceSnap, LoadCache, ModuleInfo, load
from hdl_vet.splice import OffsetMap, Splice, SpliceQueue, apply_splices, unified_diff

__all__ = ["ADDED_DIR", "HEADER_MARK", "Comparison", "Result", "Session", "SessionError", "describe"]

ADDED_DIR = "hdl_vet_added"  # where the output tree keeps the files a script adds

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
    added: dict[str, bytes] = field(default_factory=dict)  # output-relative POSIX path -> bytes
    dangling: list[Diag] = field(default_factory=list)  # dangling entries the baseline did not have

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
        for d in self.dangling:
            where = self._rel(d.path) + (f":{d.line}" if d.line is not None else "")
            lines.append(f"  left dangling: {d.message} in module {d.module} ({where})")
        for c in self.unexpected_changes:
            name = f" {c.name}" if c.name else ""
            lines.append(f"  unexpected change: {c.kind} {c.path}{name}: {c.before!r} -> {c.after!r}")
        for e in self.unmatched_expects:
            lines.append(f"  expected change not seen: {e.kind} path={e.path} name={e.name}")
        for msg in self.failed_checks:
            lines.append(f"  failed check: {msg}")
        return "\n".join(lines) + "\n"


def describe(d: Diag) -> str:
    """One diagnostic as a refusal or a note quotes it: slang's message, its module and the file's name."""
    bits: list[str] = []
    if d.module is not None:
        bits.append(f"module {d.module}")
    if d.path is not None:
        bits.append(d.path.name + (f":{d.line}" if d.line is not None else ""))
    return f"{d.message} [{', '.join(bits)}]" if bits else d.message


def _subtract(a: Sequence[tuple[int, int]], b: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """The inclusive integer ranges of `a` that no range of `b` covers (both sorted and merged)."""
    out: list[tuple[int, int]] = []
    for lo, hi in a:
        cur = lo
        for blo, bhi in b:
            if bhi < cur or blo > hi:
                continue
            if blo > cur:
                out.append((cur, blo - 1))
            cur = max(cur, bhi + 1)
            if cur > hi:
                break
        if cur <= hi:
            out.append((cur, hi))
    return out


@dataclass(frozen=True)
class Comparison:
    """What an edited design has that the design at a checkpoint did not."""

    diagnostics: list[Diag]  # new errors and warnings, as sign-off counts them
    dangling: list[Diag]
    unused: list[Diag]
    partial: list[str] = field(default_factory=list)  # signals that lost the driver of some of their bits


@dataclass(frozen=True)
class _Checkpoint:
    text: dict[Path, bytes]
    queue: dict[Path, Any]
    maps: dict[Path, list[OffsetMap]]
    expects: list[Expect]
    added: dict[str, tuple[Path, bytes]]
    added_base: dict[Path, Counter[tuple[str, str]]]
    load_fl: Filelist
    design: Design


def _grown(before: Sequence[Diag], after: Sequence[Diag]) -> list[Diag]:
    """The entries of `after` that `before` does not have, as multisets of (code, module, file, message)."""
    pool = Counter((d.code, d.module, d.path, d.message) for d in before)
    out: list[Diag] = []
    for d in after:
        key = (d.code, d.module, d.path, d.message)
        if pool[key] > 0:
            pool[key] -= 1
        else:
            out.append(d)
    return out


def dangling_set(design: Design, added: Collection[Path] = ()) -> list[Diag]:
    """What counts as dangling in `design`: its driverless nets and variables, and its driverless
    output ports that some instance connects to a non-empty expression or that their own module reads
    (an output nobody reads, such as one every instance leaves open or one of a top module, is allowed;
    an entry whose module or port cannot be identified counts). Entries inside the `added` files never count."""
    raw = [d for d in design.dangling() if d.path not in added]
    if not any(d.code == "UndrivenPort" for d in raw):
        return raw
    connected: set[tuple[str, str]] = set()
    for snap in design.snapshot().values():
        connected.update((snap.module, port) for port, text in snap.connections.items() if text)
    return [d for d in raw if d.code != "UndrivenPort" or d.port is None or d.read or (d.module, d.port) in connected]


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
        # File-system answers (which file an include resolves to) cannot change
        # between this session's loads: it re-loads from its own bytes.
        self._load_cache = LoadCache()
        self._load_fl: Filelist = self._fl  # what the design is loaded from: `_fl` plus the added files
        self._added: dict[str, tuple[Path, bytes]] = {}  # output name -> (real path, bytes), in the order added
        # what each added file showed right after it was added: (code, message) of its diagnostics
        self._added_base: dict[Path, Counter[tuple[str, str]]] = {}
        self._design: Design = load(self._fl, top=top, overrides=dict(self._orig), cache=self._load_cache)
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

    def _key(self, path: Path) -> Path:
        """`path` as the session keys its files. Callers mostly pass a resolved path
        already, so the file system is asked only when the path is not known as given."""
        path = Path(path)
        return path if path in self._text else path.resolve()

    def text(self, path: Path) -> bytes:
        key = self._key(path)
        try:
            return self._text[key]
        except KeyError:
            for added, data in self._added.values():
                if added == key:
                    return data
            raise SessionError(f"{path}: not a source or included file of this design") from None

    @property
    def added(self) -> dict[str, bytes]:
        """The files scripts have added: output-relative POSIX path -> bytes, in the order added."""
        return {name: data for name, (_, data) in self._added.items()}

    def is_added(self, path: Path) -> bool:
        key = self._key(path)
        return any(added == key for added, _ in self._added.values())

    # --- edits ---------------------------------------------------------------

    def splice(self, path: Path, start: int, end: int, text: bytes, old: bytes | None = None) -> None:
        key = Path(path).resolve()
        if key not in self._text:
            if self.is_added(key):
                raise SessionError(f"{path}: is an added file; hdl-vet never edits added files")
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
        key = self._key(path)
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

    def _overrides(self) -> dict[Path, bytes]:
        """Every byte the design is built from: the text of each file, and each added file."""
        out = dict(self._text)
        for path, data in self._added.values():
            out[path] = data
        return out

    def _reload(self) -> None:
        self._design = load(
            self._load_fl, top=self._top, overrides=self._overrides(), check_overrides=False, cache=self._load_cache
        )
        self.reloads += 1

    def _refresh(self) -> None:
        if not any(len(q) for q in self._queue.values()):
            return
        # If the edited text cannot be loaded, put everything back: otherwise the
        # text would be ahead of the loaded design, and a script that caught the
        # error would plan its next edit on a model that no longer matches it.
        text, queue = dict(self._text), self._queue
        maps = {p: list(stages) for p, stages in self._maps.items()}
        self._apply_queue()
        try:
            self._reload()
        except AdapterError:
            self._text, self._queue, self._maps = text, queue, maps
            raise

    def _dirty(self, path: Path) -> bool:
        q = self._queue.get(path)
        return q is not None and len(q) > 0

    # --- checkpoint and rollback ----------------------------------------------

    def checkpoint(self) -> object:
        """Everything a rollback restores: text, queue, stages, expectations, added files, the loaded design."""
        return _Checkpoint(
            dict(self._text),
            copy.deepcopy(self._queue),
            {p: list(stages) for p, stages in self._maps.items()},
            list(self._expects),
            dict(self._added),
            dict(self._added_base),
            self._load_fl,
            self._design,
        )

    def rollback(self, cp: object) -> None:
        """Go back to `cp`. `reloads` keeps counting. A checkpoint may be rolled back to more than once."""
        if not isinstance(cp, _Checkpoint):
            raise SessionError("not a checkpoint of this session")
        self._text = dict(cp.text)
        self._queue = copy.deepcopy(cp.queue)
        self._maps = {p: list(stages) for p, stages in cp.maps.items()}
        self._expects = list(cp.expects)
        self._added = dict(cp.added)
        self._added_base = dict(cp.added_base)
        self._load_fl = cp.load_fl
        self._design = cp.design

    def drop_expects(self, paths: Collection[str]) -> None:
        """Forget the required expectations at `paths` and below them (their instance is being removed)."""
        mine = tuple(paths)
        self._expects = [
            e
            for e in self._expects
            if not (e.required and (e.path in mine or any(e.path.startswith(p + ".") for p in mine)))
        ]

    def compare_since(self, cp: object, drove: Sequence[tuple[str, str]] = ()) -> Comparison | None:
        """What the loaded design has that the design at `cp` did not; None if it is that design.

        The diagnostics are compared by location through the edits made since `cp`, as
        sign-off does against the baseline; warnings inside an added file never count.
        The dangling (see `dangling_set`) and unused entries are compared as multisets of
        (code, module, file, message); dangling entries inside an added file never count. `drove` are the (module, signal) pairs that
        something removed used to drive: the bits driven afterwards must cover the bits driven before.
        """
        if not isinstance(cp, _Checkpoint):
            raise SessionError("not a checkpoint of this session")
        before, after = cp.design, self._design
        if before is after:
            return None
        maps = {p: stages[len(cp.maps.get(p, ())) :] for p, stages in self._maps.items()}
        new = new_diagnostics(before.diagnostics(), after.diagnostics(), {p: m for p, m in maps.items() if m})
        paths = {path for path, _ in self._added.values()}
        dangling = _grown(dangling_set(before, paths), dangling_set(after, paths))
        return Comparison(
            self._without_added_warnings(new),
            dangling,
            _grown(before.unused(), after.unused()),
            self._lost_bits(before, after, drove),
        )

    @staticmethod
    def _lost_bits(before: Design, after: Design, drove: Sequence[tuple[str, str]]) -> list[str]:
        pairs = list(dict.fromkeys(drove))
        if not pairs:
            return []
        was, now = before.driven_bits(pairs), after.driven_bits(pairs)
        unused = after.unused()
        out: list[str] = []
        for module, signal in pairs:
            old, new = was[(module, signal)], now[(module, signal)]
            if old is None or new is None:
                continue  # the signal or its module is gone
            if any(u.module == module and f"'{signal}'" in u.message for u in unused):
                continue  # nothing reads it any more
            for key, ranges in old.items():
                after_ranges = new.get(key)
                if not after_ranges:
                    continue  # no driver left at all: slang reports that itself
                lost = _subtract(ranges, after_ranges)
                if lost:
                    bits = ", ".join(str(lo) if lo == hi else f"{lo}-{hi}" for lo, hi in lost)
                    out.append(f"signal {signal!r} of module {module} loses the driver of bit(s) {bits}")
        return out

    def _without_added_warnings(self, diags: list[Diag]) -> list[Diag]:
        """Drop what an added file showed when it was added; anything else located in it counts."""
        if not self._added_base:
            return diags
        left = {p: Counter(c) for p, c in self._added_base.items()}
        out: list[Diag] = []
        for d in diags:
            pool = left.get(d.path) if d.path is not None else None
            if pool is not None and d.severity != "error" and pool[(d.code, d.message)] > 0:
                pool[(d.code, d.message)] -= 1
                continue
            out.append(d)
        return out

    # --- added files ---------------------------------------------------------

    def add_file(self, path: Path | str) -> bool:
        """Add a source file outside the design: it is loaded with the design, last in the file list.

        Returns False (nothing done) when the file is already part of the design. Raises
        SessionError, leaving the session as it was, when the file cannot be read, when
        its name is taken by another added file, when the input tree already has an
        `hdl_vet_added` directory, when it includes a header that is not a file of the
        design, or when the design with the file has a diagnostic the design did not have.
        """
        key = Path(path).resolve()
        if key in self._load_fl.files:
            return False
        out = f"{ADDED_DIR}/{key.name}"
        for name, (other, _) in self._added.items():
            if other == key:
                return False
            if name.casefold() == out.casefold():
                raise SessionError(
                    f"another added file ({other}) has the same name, which would collide in the output tree"
                    + ("" if name == out else f" ({name.rsplit('/', 1)[-1]!r} differs only in case)")
                )
        if (self.layout_root / ADDED_DIR).exists():
            raise SessionError(
                f"the input tree already has a {ADDED_DIR!r} at {self.layout_root}, where added files are written"
            )
        try:
            data = key.read_bytes()
        except OSError as e:
            raise SessionError(f"cannot read it ({e})") from e
        if _has_header(data):
            raise SessionError("it already carries the hdl-vet header; an added file must be an unmodified input")
        fl = self._load_fl
        trial = Filelist([*fl.files, key], fl.incdirs, fl.defines, fl.root)
        added = dict(self._added)
        self._added = {**added, out: (key, data)}
        try:
            design = load(
                trial, top=self._top, overrides=self._overrides(), check_overrides=False, cache=self._load_cache
            )
            self.reloads += 1
            known = set(self._text) | {p for p, _ in self._added.values()}
            extra = [h for h in design.included if h not in known]
            if extra:
                raise SessionError(
                    f"it includes {extra[0]}, which is not a file of the design; "
                    f"an added file may include only headers the design already reads"
                )
            new = self._without_added_warnings(
                [
                    d
                    for d in new_diagnostics(self._design.diagnostics(), design.diagnostics(), {})
                    if not (d.path == key and d.severity != "error")  # the file's own warnings are its baseline
                ]
            )
            if new:
                more = f" (and {len(new) - 3} more)" if len(new) > 3 else ""
                raise SessionError(
                    "the design with it has " + "; ".join(f"{d.severity} {describe(d)}" for d in new[:3]) + more
                )
        except AdapterError as e:
            self._added = added
            raise SessionError(f"the design with it cannot be loaded: {e}") from e
        except SessionError:
            self._added = added
            raise
        self._added_base = {
            **self._added_base,
            key: Counter((d.code, d.message) for d in design.diagnostics() if d.path == key),
        }
        self._load_fl = trial
        self._design = design
        return True

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
            failed = self._run_checks(checks, self._design if self._added else self._baseline)
            return Result(
                not unmatched and not failed, [], [], unexpected, unmatched, {}, "", self.layout_root,
                failed_checks=failed, added=self.added,
            )

        files = {p: self._text[p] for p in changed}
        diff = "".join(
            unified_diff(p, self._orig[p], self._text[p], label=p.relative_to(self.layout_root).as_posix())
            for p in changed
        )
        try:
            self._reload()
        except AdapterError as e:
            return Result(False, [], [], [], [], files, diff, self.layout_root, [], str(e), added=self.added)
        new_files = sorted((p for p in self._design.included if p not in self._text), key=lambda p: p.as_posix())
        new = self._without_added_warnings(
            new_diagnostics(self._baseline.diagnostics(), self._design.diagnostics(), self._maps)
        )
        changes = diff_snapshots(self._baseline.snapshot(), self._design.snapshot())
        unexpected, unmatched = check_changes(changes, self._expects)
        failed = self._run_checks(checks, self._design)
        added_paths = {path for path, _ in self._added.values()}
        dangling = _grown(dangling_set(self._baseline, added_paths), dangling_set(self._design, added_paths))
        ok = not new and not unexpected and not unmatched and not new_files and not failed and not dangling
        return Result(
            ok, new, changes, unexpected, unmatched, files, diff, self.layout_root, new_files,
            failed_checks=failed, added=self.added, dangling=dangling,
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
