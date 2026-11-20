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

import base64
import copy
import os
import re
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hdl_vet.archive import need_bool, need_dict, need_int, need_list, need_str
from hdl_vet.enables import Enable
from hdl_vet.filelist import Filelist, read_filelist
from hdl_vet.paths import same_file
from hdl_vet.prove import Proof, Record
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
    equivalence: list[Record] = field(default_factory=list)  # one record for each comparison of the proof
    enables: list[Enable] = field(default_factory=list)  # one record for each pin of an enable that was looked at
    notes: list[str] = field(default_factory=list)  # the notes of the operations, set by `Vet.finish` and `Vet.check`

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
        lines.extend(f"  note: {note}" for note in self.notes)
        lines.extend(self.equivalence_lines())
        return "\n".join(lines) + "\n"

    def equivalence_lines(self) -> list[str]:
        """The report lines of the equivalence records and of the pins of the enables (each line once)."""
        lines = [line for record in self.equivalence for line in record.lines()]
        lines += [line for enable in self.enables if (line := enable.line()) is not None]
        return list(dict.fromkeys(lines))


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
        self.scripts: list[tuple[str, str]] = []  # (name, sha256) of the scripts applied so far, in order
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
                raise SessionError(f"{p}: already carries the hdl-vet header; the input must be an unmodified tree")
        errors = [d for d in self._baseline.diagnostics() if d.severity == "error"]
        if errors:
            d = errors[0]
            where = f"{d.path}:{d.line}: " if d.path is not None else ""
            raise SessionError(f"the baseline design has {len(errors)} error(s); first: {where}{d.code}: {d.message}")
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
            raise SessionError(f"{path}: bytes [{start}, {end}) are {current[start:end]!r}, expected {old!r}")
        queue = self._queue.get(key)
        if queue is None:
            queue = self._queue[key] = SpliceQueue(len(current))
        queue.add(Splice(key, start, end, bytes(text)))

    def is_editable(self, path: Path) -> bool:
        """True when `splice` would accept `path`: a source or included file that is editable."""
        key = self._key(path)
        return key in self._text and (self._editable is None or key in self._editable)

    def expect(self, kind: str, path: str = "*", name: str = "*", *, exact: bool = False, below: bool = False) -> None:
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

    def permit(self, kind: str, path: str = "*", name: str = "*", *, exact: bool = False, below: bool = False) -> None:
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

    @property
    def baseline(self) -> Design:
        """The design as it was loaded from the input files, before any edit."""
        return self._baseline

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

    def expected_at(self, kind: str, name: str) -> list[str]:
        """The paths where `kind` of `name` is required: what an operation of this run registered.

        `Vet._added` cannot answer this. It holds only what is waiting for the next load,
        and it is cleared by one, so an operation that re-loads before it looks sees
        nothing there. An expectation lives for the whole run, which is the question.
        """
        return [e.path for e in self._expects if e.required and e.kind == kind and e.name == name]

    def drop_expect(self, kind: str, paths: Collection[str], name: str) -> None:
        """Forget one expectation: an operation of this run undid what registered it.

        Narrower than `drop_expects`, which forgets everything at a path and below it
        because the instance there is going. Here the instance stays and only this one
        expectation is moot, so the rest must still be met.
        """
        mine = set(paths)
        self._expects = [e for e in self._expects if not (e.kind == kind and e.path in mine and e.name == name)]

    def compare_since(self, cp: object, drove: Sequence[tuple[str, str]] = ()) -> Comparison | None:
        """What the loaded design has that the design at `cp` did not; None if it is that design.

        The diagnostics are compared by location through the edits made since `cp`, as
        sign-off does against the baseline; warnings inside an added file never count.
        The dangling (see `dangling_set`) and unused entries are compared as multisets of
        (code, module, file, message); dangling entries inside an added file never count. `drove` are the
        (module, signal) pairs that something removed used to drive: the bits driven afterwards must cover
        the bits driven before.
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
        if _unwritable(Path(path).name):
            raise SessionError(
                f"its name {Path(path).name!r} cannot be written on every platform "
                f"(a Windows device name, or a trailing dot or blank); added files are written under their own name"
            )
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
        proof: Callable[[Session, Design], Proof] | None = None,
    ) -> Result:
        """`proof` makes the records of the equivalence check from this session and the edited design."""
        if self._finished:
            raise SessionError("the session is already finished")
        self._finished = True
        scripts = list(scripts)
        checks = list(checks)
        queued = any(len(q) for q in self._queue.values())
        self._apply_queue()
        changed = sorted((p for p in self._text if self._text[p] != self._orig[p]), key=lambda p: p.as_posix())
        for p in changed:
            hs = _header_splice(p, self._text[p], scripts)
            self._text[p] = apply_splices(self._text[p], [hs])
            self._maps.setdefault(p, []).append(OffsetMap([hs]))
        if not changed:
            unexpected, unmatched = check_changes([], self._expects)
            if queued:  # the loaded design is behind the text (the edits may have cancelled out)
                try:
                    self._reload()
                except AdapterError as e:
                    return Result(False, [], [], [], [], {}, "", self.layout_root, [], str(e), added=self.added)
                design = self._design
            else:
                design = self._design if self._added else self._baseline
            failed = self._run_checks(checks, design)
            found = self._run_proof(proof, design, failed)
            records = found.records
            return Result(
                not unmatched
                and not failed
                and all(r.passed for r in records)
                and all(e.passed for e in found.enables),
                [],
                [],
                unexpected,
                unmatched,
                {},
                "",
                self.layout_root,
                failed_checks=failed,
                added=self.added,
                equivalence=records,
                enables=found.enables,
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
        changes += diff_snapshots(self._baseline.gate_snapshot(), self._design.gate_snapshot())
        changes.sort(key=lambda c: (c.path, c.kind, c.name))
        unexpected, unmatched = check_changes(changes, self._expects)
        failed = self._run_checks(checks, self._design)
        found = self._run_proof(proof, self._design, failed)
        records = found.records
        added_paths = {path for path, _ in self._added.values()}
        dangling = _grown(dangling_set(self._baseline, added_paths), dangling_set(self._design, added_paths))
        ok = not new and not unexpected and not unmatched and not new_files and not failed and not dangling
        ok = ok and all(r.passed for r in records) and all(e.passed for e in found.enables)
        return Result(
            ok,
            new,
            changes,
            unexpected,
            unmatched,
            files,
            diff,
            self.layout_root,
            new_files,
            failed_checks=failed,
            added=self.added,
            dangling=dangling,
            equivalence=records,
            enables=found.enables,
        )

    def _run_proof(self, proof: Callable[[Session, Design], Proof] | None, design: Design, failed: list[str]) -> Proof:
        """The records of the equivalence check. An exception of the check is a failed check, not an escape."""
        if proof is None:
            return Proof()
        try:
            return proof(self, design)
        except Exception as e:
            failed.append(f"the equivalence check raised {type(e).__name__}: {e}")
            return Proof()

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

    @property
    def finished(self) -> bool:
        return self._finished

    def check(
        self,
        scripts: Iterable[tuple[str, str]] = (),
        checks: Iterable[Callable[[Design], str | None]] = (),
        proof: Callable[[Session, Design], Proof] | None = None,
    ) -> Result:
        """What `finish` would return now; afterwards the session is as it was (`reloads` keeps counting)."""
        if self._finished:
            raise SessionError("the session is already finished")
        cp = self.checkpoint()
        try:
            return self.finish(scripts, checks, proof)
        finally:
            self.rollback(cp)
            self._finished = False

    # --- saved sessions ------------------------------------------------------

    def owned_paths(self) -> dict[Path, str]:
        """Files of the user's that saving must never replace, each with what it is."""
        out = {self._filelist_path: "the file list"}
        for path in self._fl.lists:
            out.setdefault(path, "a file list the design reads")
        for name, (path, _) in self._added.items():
            out.setdefault(path, f"the source of the added file {name.rsplit('/', 1)[-1]}")
        return out

    def to_state(
        self, scripts: Iterable[tuple[str, str]], extra: Mapping[str, Any] | None = None
    ) -> tuple[dict[str, Any], dict[str, bytes]]:
        """The session as plain data: an index of JSON values and the file entries of an archive.

        Entry names are `original/<path>`, `current/<path>` (files that differ from the original;
        `<path>` is relative to the layout root) and `added/<name>`. `scripts` is what the index
        lists as applied; `extra` are further fields of the index (the Vet's own). Nothing queued
        may be left: call `refresh` first. No slang object is stored; `from_state` re-loads the designs.
        """
        if any(len(q) for q in self._queue.values()):
            raise SessionError("splices are queued; apply them before saving")
        root = self.layout_root
        files: dict[str, bytes] = {}
        changed = [p for p in self._files if self._text[p] != self._orig[p]]
        for p in self._files:
            files[f"original/{p.relative_to(root).as_posix()}"] = self._orig[p]
        for p in changed:
            files[f"current/{p.relative_to(root).as_posix()}"] = self._text[p]
        added: list[dict[str, Any]] = []
        for out, (path, data) in self._added.items():
            name = out.rsplit("/", 1)[-1]
            files[f"added/{name}"] = data
            seen = self._added_base.get(path, Counter())
            diags = sorted([code, message, n] for (code, message), n in seen.items())
            added.append({"name": name, "path": str(path), "diagnostics": diags})
        fl = self._fl
        index: dict[str, Any] = {
            **(extra or {}),
            "top": self._top,
            "layout_root": str(root),
            "filelist_path": str(self._filelist_path),
            "filelist": {
                "files": [str(p) for p in fl.files],
                "incdirs": [str(p) for p in fl.incdirs],
                "defines": [[name, value] for name, value in fl.defines.items()],
                "root": str(fl.root),
                "lists": [str(p) for p in fl.lists],
            },
            "files": [str(p) for p in self._files],
            "changed": [str(p) for p in changed],
            "editable": None if self._editable is None else sorted(str(p) for p in self._editable),
            "maps": {
                str(p): [
                    [[s.start, s.end, base64.b64encode(s.text).decode("ascii")] for s in m.splices] for m in stages
                ]
                for p, stages in sorted(self._maps.items(), key=lambda kv: kv[0].as_posix())
            },
            "expects": [
                {
                    "kind": e.kind,
                    "path": e.path,
                    "name": e.name,
                    "required": e.required,
                    "exact": e.exact,
                    "below": e.below,
                }
                for e in self._expects
            ],
            "added": added,
            "scripts": [[n, h] for n, h in scripts],
        }
        return index, files

    @classmethod
    def from_state(cls, index: dict[str, Any], files: dict[str, bytes]) -> Session:
        """A session in the state `to_state` described. The file system is not read.

        Both designs are loaded again from the saved bytes: the baseline from the originals, the
        current one from the current text and the added files (the baseline itself when nothing
        differs, so that `reloads` is 0 exactly then, else 1). Every value of the index is
        type-checked and nothing is coerced. SessionError for a path outside the layout root, a
        name that cannot be written on every platform, entries the index does not match, or
        stored edits that do not lead from each original to its current text; TypeError,
        ValueError, KeyError, SpliceError or AdapterError for other damage (the caller makes them
        one SessionError naming the file).
        """
        root = _stored_path(index["layout_root"])

        def under(value: object) -> Path:
            p = _stored_path(value)
            if p != root and root not in p.parents:
                raise SessionError(f"the session archive has a path outside its layout root {root}: {value}")
            if os.name == "nt" and any(_unwritable(part) for part in p.relative_to(root).parts):
                raise SessionError(f"the session archive has a path with a name Windows cannot write: {value}")
            return p

        top = index["top"]
        if top is not None:
            top = need_str(top)
        fl_in = need_dict(index["filelist"])
        defines: dict[str, str | None] = {}
        for pair in need_list(fl_in["defines"]):
            name, value = need_list(pair, 2)
            defines[need_str(name)] = None if value is None else need_str(value)
        fl = Filelist(
            [under(v) for v in need_list(fl_in["files"])],
            [under(v) for v in need_list(fl_in["incdirs"])],
            defines,
            under(fl_in["root"]),
            # The file lists lie anywhere; an archive from before this record has none.
            [_stored_path(v) for v in need_list(fl_in["lists"])] if "lists" in fl_in else [],
        )
        paths = [under(v) for v in need_list(index["files"])]
        changed = [under(v) for v in need_list(index["changed"])]
        if len(set(paths)) != len(paths) or len(set(changed)) != len(changed):
            raise ValueError("a file is listed twice")
        _refuse_one_file_under_two_names(paths)
        if not set(fl.files) <= set(paths) or not set(changed) <= set(paths):
            raise ValueError("the file lists disagree")
        added_in = [need_dict(a) for a in need_list(index["added"])]
        names = [_plain_name(a["name"]) for a in added_in]
        if len({n.casefold() for n in names}) != len(names):
            raise SessionError("the session archive has two added files whose names differ only in case")
        expected = {f"original/{p.relative_to(root).as_posix()}" for p in paths}
        expected |= {f"current/{p.relative_to(root).as_posix()}" for p in changed}
        expected |= {f"added/{n}" for n in names}
        if set(files) != expected:
            extra, missing = sorted(set(files) - expected), sorted(expected - set(files))
            raise SessionError(
                "the session archive does not match its index"
                + (f"; entries the index does not know: {', '.join(extra[:3])}" if extra else "")
                + (f"; entries missing: {', '.join(missing[:3])}" if missing else "")
            )

        self = object.__new__(cls)
        self._filelist_path = _stored_path(index["filelist_path"])
        self._top = top
        self._fl = fl
        self._orig = {p: files[f"original/{p.relative_to(root).as_posix()}"] for p in paths}
        self._text = dict(self._orig)
        for p in changed:
            self._text[p] = files[f"current/{p.relative_to(root).as_posix()}"]
        self._files = list(paths)
        sources = set(fl.files)
        self._headers = [p for p in paths if p not in sources]
        editable = index["editable"]
        self._editable = None if editable is None else {under(v) for v in need_list(editable)}
        self._added = {}
        self._added_base = {}
        for a, name in zip(added_in, names, strict=True):
            path = _stored_path(a["path"])
            self._added[f"{ADDED_DIR}/{name}"] = (path, files[f"added/{name}"])
            counts: Counter[tuple[str, str]] = Counter()
            for item in need_list(a["diagnostics"]):
                code, message, n = need_list(item, 3)
                if need_int(n) < 0:
                    raise ValueError("a negative count")
                counts[(need_str(code), need_str(message))] = n
            self._added_base[path] = counts
        self._load_fl = (
            Filelist([*fl.files, *(path for path, _ in self._added.values())], fl.incdirs, fl.defines, fl.root)
            if self._added
            else fl
        )
        self._queue = {}
        self._maps = {}
        for key, stages in need_dict(index["maps"]).items():
            p = under(key)
            if p not in self._text:
                raise ValueError(f"stages for {key}, which is not a file of the design")
            self._maps[p] = [
                OffsetMap(
                    [
                        Splice(
                            p, need_int(s), need_int(e), base64.b64decode(need_str(t).encode("ascii"), validate=True)
                        )
                        for s, e, t in (need_list(item, 3) for item in need_list(stage))
                    ]
                )
                for stage in need_list(stages)
            ]
        for p in paths:  # the stored edits must lead from each original to its current text
            data = self._orig[p]
            for m in self._maps.get(p, ()):
                data = apply_splices(data, m.splices)  # SpliceError for a splice outside its file
            if data != self._text[p]:
                raise SessionError(
                    f"the session archive is damaged: the stored edits of {p.relative_to(root).as_posix()} "
                    f"do not lead from its original to its current text"
                )
        self._expects = []
        for e in (need_dict(x) for x in need_list(index["expects"])):
            self._expects.append(
                Expect(
                    need_str(e["kind"]),
                    need_str(e["path"]),
                    need_str(e["name"]),
                    need_bool(e["required"]),
                    need_bool(e["exact"]),
                    need_bool(e["below"]),
                )
            )
        self.scripts = [(need_str(n), need_str(h)) for n, h in (need_list(x, 2) for x in need_list(index["scripts"]))]
        self._finished = False
        self._load_cache = LoadCache()
        self.layout_root = self._compute_layout_root()
        if self.layout_root != root:
            raise SessionError(f"the session archive's layout root {root} is not the root of its files")
        self._baseline = load(fl, top=top, overrides=dict(self._orig), check_overrides=False, cache=self._load_cache)
        if not changed and not self._added:
            self._design = self._baseline
            self.reloads = 0
        else:
            self._design = load(
                self._load_fl, top=top, overrides=self._overrides(), check_overrides=False, cache=self._load_cache
            )
            self.reloads = 1
        return self


_DEVICE = re.compile(r"(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?", re.IGNORECASE)


def _refuse_one_file_under_two_names(paths: Sequence[Path]) -> None:
    """Refuse an index that names one file twice, under spellings that differ only in case.

    Where the archive was saved the two names were two files, and the index is honest. Here
    they are one file, and the session would hold it twice: the output tree would write it
    twice to one place, and the manifest would record a hash of bytes that are not on disk.

    The file system is asked only about names that could collide, so a design whose names
    are all distinct costs no syscall. Two names that really are two files are kept, which
    is why this asks rather than folding the case and refusing.
    """
    groups: dict[str, list[Path]] = {}
    for path in paths:
        groups.setdefault(str(path).casefold(), []).append(path)
    for same in groups.values():
        first = same[0]
        for other in same[1:]:
            if same_file(first, other):
                raise ValueError(f"{first.name} and {other.name} are one file here, and both are listed")


def _unwritable(part: str) -> bool:
    """True for a file name that some platform cannot write: a Windows device name, a trailing dot or blank."""
    return part.endswith((".", " ")) or _DEVICE.fullmatch(part) is not None


def _stored_path(value: object) -> Path:
    """A path of a saved session: absolute and normalised (no `..`), as `Path` spells it."""
    p = Path(need_str(value))
    if not p.is_absolute() or ".." in p.parts or str(p) != value:
        raise SessionError(f"the session archive has a path that is not absolute and normalised: {value!r}")
    return p


def _plain_name(value: object) -> str:
    """The name of an added file in an archive: a plain file name that every platform can write."""
    if (
        not isinstance(value, str)
        or value in ("", ".", "..")
        or any(c in value for c in '/\\:*?"<>|\x00')
        or Path(value).name != value
        or _unwritable(value)
    ):
        raise SessionError(f"the session archive has an added file whose name is not a plain file name: {value!r}")
    return value
