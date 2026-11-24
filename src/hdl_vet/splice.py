"""Byte-range splices: the only way hdl-vet changes a file.

A `Splice` replaces bytes `[start, end)` of one file; `apply_splices` applies a set
of them expressed in the coordinates of the original bytes, and `OffsetMap` maps
offsets between the original and the result so that locations (for example
diagnostics) can be compared across an edit. No pyslang here.
"""

from __future__ import annotations

import bisect
import difflib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["OffsetMap", "RawMark", "RawSplice", "Splice", "SpliceError", "SpliceQueue", "apply_splices", "unified_diff"]


class SpliceError(Exception):
    """Raised for an out-of-range or overlapping splice; the message has the offsets."""


@dataclass
class RawSplice:
    """A raw splice (`Vet.splice`) that waits in the queue of the session. Its range is in the text of the last load."""

    start: int
    end: int
    text: bytes
    waiver: str | None
    modules: tuple[str, ...]  # the modules whose text holds the range (none or two: not one)
    marked: bool = False


@dataclass(frozen=True)
class RawMark:
    """What the sign-off must judge: `kind` "splice" or "logic" (`target` is a module) or "outside" (a file).

    `label` names a logic call in the record, for example `set_param core.u_sync.STAGES`.
    """

    kind: str
    target: str
    waiver: str | None
    label: str = ""


@dataclass(frozen=True)
class Splice:
    path: Path
    start: int
    end: int
    text: bytes


def _prepare(splices: Iterable[Splice], size: int | None = None) -> list[Splice]:
    """Validate, drop exact duplicates and order by position (stable for insertions)."""
    seen: set[tuple[int, int, bytes]] = set()
    kept: list[Splice] = []
    for s in splices:
        if s.start < 0 or s.start > s.end or (size is not None and s.end > size):
            limit = "" if size is None else f" in a file of {size} bytes"
            raise SpliceError(f"splice [{s.start}, {s.end}) is out of range{limit}")
        key = (s.start, s.end, s.text)
        if key in seen:
            continue
        seen.add(key)
        kept.append(s)
    # Insertions sort before a replacement that starts at the same offset; sorted()
    # is stable, so insertions at one offset keep their submission order.
    kept.sort(key=lambda s: (s.start, s.end != s.start))
    prev_end = 0  # end of the furthest replacement so far
    prev: Splice | None = None
    for s in kept:
        if s.start < prev_end and prev is not None:
            raise SpliceError(f"[{s.start}, {s.end}) overlaps [{prev.start}, {prev.end})")
        if s.end > s.start:
            prev_end, prev = s.end, s
    return kept


class SpliceQueue:
    """Splices of one file, validated as they are added (O(log n) per splice).

    Same rules as `apply_splices`; `splices()` returns them in submission order.
    """

    def __init__(self, size: int) -> None:
        self._size = size
        self._all: list[Splice] = []
        self._seen: set[tuple[int, int, bytes]] = set()
        self._repl: list[Splice] = []  # replacements, disjoint, sorted by start
        self._repl_starts: list[int] = []
        self._ins: list[int] = []  # sorted insertion points

    def __len__(self) -> int:
        return len(self._all)

    def add(self, s: Splice) -> None:
        a, b = s.start, s.end
        if a < 0 or a > b or b > self._size:
            raise SpliceError(f"splice [{a}, {b}) is out of range in a file of {self._size} bytes")
        key = (a, b, s.text)
        if key in self._seen:
            return
        if a == b:
            i = bisect.bisect_left(self._repl_starts, a) - 1  # last replacement starting before a
            if i >= 0 and self._repl[i].end > a:
                r = self._repl[i]
                raise SpliceError(f"insertion at {a} is inside the replaced range [{r.start}, {r.end})")
            bisect.insort(self._ins, a)
        else:
            i = bisect.bisect_left(self._repl_starts, b)  # replacements starting before b
            if i > 0 and self._repl[i - 1].end > a:
                r = self._repl[i - 1]
                raise SpliceError(f"[{a}, {b}) overlaps [{r.start}, {r.end})")
            j = bisect.bisect_right(self._ins, a)  # first insertion point after a
            if j < len(self._ins) and self._ins[j] < b:
                raise SpliceError(f"[{a}, {b}) has an insertion at {self._ins[j]} inside it")
            self._repl.insert(i, s)
            self._repl_starts.insert(i, a)
        self._seen.add(key)
        self._all.append(s)

    def splices(self) -> list[Splice]:
        return list(self._all)


def apply_splices(data: bytes, splices: Iterable[Splice]) -> bytes:
    out: list[bytes] = []
    pos = 0
    for s in _prepare(splices, len(data)):
        out.append(data[pos : s.start])
        out.append(s.text)
        pos = s.end
    out.append(data[pos:])
    return b"".join(out)


class OffsetMap:
    """Offset translation for one set of splices."""

    def __init__(self, splices: Iterable[Splice]) -> None:
        self._s = _prepare(splices)
        # Output start of each splice, and the cumulative size delta before it.
        self._new_start: list[int] = []
        shift = 0
        for s in self._s:
            self._new_start.append(s.start + shift)
            shift += len(s.text) - (s.end - s.start)
        self._starts = [s.start for s in self._s]

    @property
    def splices(self) -> list[Splice]:
        """The splices as validated and ordered; `OffsetMap(m.splices)` is `m` again."""
        return list(self._s)

    def forward(self, old: int) -> int:
        shift = 0
        for s in self._s:
            if s.start > old:
                break
            if s.start == s.end:
                shift += len(s.text)  # the byte at `old` moved after the insertion
            elif old < s.end:
                return s.start + shift  # inside a replaced range
            else:
                shift += len(s.text) - (s.end - s.start)
        return old + shift

    def replaces(self, old: int) -> bool:
        """True when `old` lies inside a replaced (or deleted) range of the original."""
        for s in self._s:
            if s.start > old:
                break
            if s.end > s.start and old < s.end:
                return True
        return False

    def in_new_text(self, new: int) -> bool:
        """True when `new` lies inside replacement or inserted text of the result."""
        i = bisect.bisect_right(self._new_start, new)
        return any(new < self._new_start[k] + len(self._s[k].text) for k in range(i))

    def backward(self, new: int) -> int:
        i = bisect.bisect_right(self._new_start, new)
        shift = 0
        for k in range(i):
            s = self._s[k]
            ns = self._new_start[k]
            if new < ns + len(s.text):
                return s.start
            shift += len(s.text) - (s.end - s.start)
        return new - shift


def _lines(data: bytes) -> list[str]:
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def unified_diff(path: Path, before: bytes, after: bytes, label: str | None = None) -> str:
    if before == after:
        return ""
    name = label if label is not None else Path(path).as_posix()
    out: list[str] = []
    for line in difflib.unified_diff(_lines(before), _lines(after), f"a/{name}", f"b/{name}", n=3, lineterm="\n"):
        out.append(line)
        if not line.endswith("\n"):
            out.append("\n\\ No newline at end of file\n")
    return "".join(out)
