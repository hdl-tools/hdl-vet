"""Byte-range edits to comma separated lists (ports, parameters, connections, overrides).

Pure text: the callers know where the items are (from the source model) and pass their
byte ranges. Nothing here reformats existing bytes; it only says which ranges go and
where new items are inserted. Splices are `(start, end, text)` in the coordinates of
the text the ranges were read from.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

__all__ = ["append_items", "edit_list", "line_rule", "remove_all"]

Splice = tuple[int, int, bytes]


def line_rule(data: bytes, start: int, end: int, comment: bool = False) -> tuple[int, int]:
    """Widen `[start, end)` to whole lines, with one line break, when only blanks are left on them.

    With `comment`, a `//` comment after the range does not count as something left: it goes
    with the line (an item of a list takes its trailing comment along).
    """
    ls = data.rfind(b"\n", 0, start) + 1
    if data[ls:start].strip(b" \t") != b"":
        return start, end
    nl = data.find(b"\n", end)
    le = len(data) if nl < 0 else nl
    rest = data[end:le].strip(b" \t\r")
    if rest != b"" and not (comment and rest.startswith(b"//")):
        return start, end
    return ls, (le if nl < 0 else nl + 1)


def _comma_after(data: bytes, pos: int) -> int | None:
    """Offset of the first comma at or after `pos`, skipping blanks and comments; None if something else is first."""
    n = len(data)
    while pos < n:
        c = data[pos : pos + 1]
        if c in (b" ", b"\t", b"\r", b"\n"):
            pos += 1
        elif data.startswith(b"//", pos):
            nl = data.find(b"\n", pos)
            pos = n if nl < 0 else nl + 1
        elif data.startswith(b"/*", pos):
            close = data.find(b"*/", pos + 2)
            if close < 0:
                return None
            pos = close + 2
        else:
            return pos if c == b"," else None
    return None


def _indent_of(data: bytes, offset: int) -> bytes:
    start = data.rfind(b"\n", 0, offset) + 1
    line = data[start:offset]
    return line[: len(line) - len(line.lstrip(b" \t"))]


def append_items(
    data: bytes, last_start: int, last_end: int, close: int, items: Sequence[bytes], eol: bytes
) -> list[Splice]:
    """Splices that append `items`, in order, after the element `[last_start, last_end)` of a list.

    The list ends at `close` (its ")"). When the ")" opens its own line, each item gets a line of
    its own, indented like the element; otherwise the items follow on the same line.
    """
    if not items:
        return []
    ls = data.rfind(b"\n", 0, close) + 1
    if data[ls:close].strip() == b"":
        indent = _indent_of(data, last_start)
        text = b"".join(indent + item + (b"," if k < len(items) - 1 else b"") + eol for k, item in enumerate(items))
        return [(last_end, last_end, b","), (ls, ls, text)]
    return [(last_end, last_end, b", " + b", ".join(items))]


def edit_list(
    data: bytes,
    items: Sequence[tuple[int, int]],
    drop: Collection[int],
    close: int,
    eol: bytes,
    new: Sequence[bytes] = (),
) -> list[Splice] | None:
    """Splices that remove the items at the indexes `drop` and append `new` after the last one kept.

    List rule: an item that is not the last of the list goes with the text up to the start of the
    next item. The items after the last one kept go with the comma that follows it, and nothing
    else: a comment after that comma stays, and the items go with the line (and a comment after
    them) when they have it to themselves. Returns None when no item is kept: what that means
    (the whole list goes, or a new one is written) is the caller's to decide.
    """
    n = len(items)
    kept = [i for i in range(n) if i not in drop]
    if not kept:
        return None
    last = kept[-1]
    out: list[Splice] = []
    for i in sorted(drop):
        if i < last:
            out.append((items[i][0], items[i + 1][0], b""))
    if last < n - 1:
        first, stop = items[last + 1][0], items[n - 1][1]
        comma = _comma_after(data, items[last][1])
        a, b = line_rule(data, first, stop, comment=True)
        if comma is not None and (a, b) != (first, stop):
            out.append((comma, comma + 1, b""))
            out.append((a, b, b""))
        else:
            out.append((items[last][1], stop, b""))
    out.extend(append_items(data, items[last][0], items[last][1], close, new, eol))
    return out


def remove_all(data: bytes, items: Sequence[tuple[int, int]]) -> Splice:
    """The splice removing every item of a list (line rule, a trailing comment goes with its line)."""
    start, end = line_rule(data, items[0][0], items[-1][1], comment=True)
    return (start, end, b"")
