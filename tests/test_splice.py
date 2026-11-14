"""Phase 2: the splice engine (`hdl_vet.splice`). No pyslang involved.

    Splice(path: Path, start: int, end: int, text: bytes)
        Frozen, hashable. Replace bytes [start, end) of `path` with `text`.
        start == end is a pure insertion before the byte at `start`.

    apply_splices(data: bytes, splices: Iterable[Splice]) -> bytes
        All splices belong to one file and are expressed in the coordinates of
        `data`. Rules:
          - Exact duplicates (same range and text) count once.
          - Ranges must not overlap. An insertion strictly inside a replaced
            range overlaps it. An insertion at either boundary of a replaced
            range does not.
          - Several different insertions at the same offset are kept in the
            order given.
          - 0 <= start <= end <= len(data), else SpliceError.
        Violations raise SpliceError; the message contains the offsets.

    OffsetMap(splices: Iterable[Splice])     (same validation as apply_splices)
        .forward(old: int) -> int
            Position in the output of input offset `old`. An offset inside a
            replaced range [start, end) maps to the first byte of its
            replacement. An offset at a pure insertion point maps after the
            inserted text (the byte that was there moved).
        .backward(new: int) -> int
            Input offset for output offset `new`. An offset inside replacement
            or inserted text maps to the `start` of that splice.

    unified_diff(path: Path, before: bytes, after: bytes, label: str | None = None) -> str
        Standard unified diff with 3 lines of context, headers
        `--- a/<label>` and `+++ b/<label>` (label defaults to path.as_posix()).
        Empty string when before == after. Line endings inside the diff body
        are shown as "\\n"; CRLF content does not produce doubled blank lines.
        Bytes that are not UTF-8 are shown with the replacement character. A
        missing final newline gets the usual "\\ No newline at end of file" line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.splice import OffsetMap, Splice, SpliceError, apply_splices, unified_diff

P = Path("x.sv")
DATA = b"0123456789"


def S(start: int, end: int, text: bytes) -> Splice:
    return Splice(P, start, end, text)


def test_no_splices_is_identity():
    assert apply_splices(DATA, []) == DATA


def test_replace_insert_delete():
    out = apply_splices(DATA, [S(2, 4, b"AB!"), S(6, 6, b"<>"), S(8, 9, b"")])
    assert out == b"01AB!45<>679"


def test_order_of_submission_does_not_matter_for_disjoint_splices():
    a = [S(2, 4, b"AB!"), S(6, 6, b"<>"), S(8, 9, b"")]
    assert apply_splices(DATA, a) == apply_splices(DATA, list(reversed(a)))


def test_exact_duplicates_count_once():
    assert apply_splices(DATA, [S(3, 3, b"x"), S(3, 3, b"x")]) == b"012x3456789"
    assert apply_splices(DATA, [S(3, 5, b"x"), S(3, 5, b"x")]) == b"012x56789"


def test_insertions_at_same_offset_keep_submission_order():
    assert apply_splices(DATA, [S(3, 3, b"a"), S(3, 3, b"b")]) == b"012ab3456789"
    assert apply_splices(DATA, [S(3, 3, b"b"), S(3, 3, b"a")]) == b"012ba3456789"


def test_insertion_at_boundaries_of_a_replacement_is_allowed():
    out = apply_splices(DATA, [S(3, 5, b"XY"), S(3, 3, b"<"), S(5, 5, b">")])
    assert out == b"012<XY>56789"


def test_insert_at_start_and_end_of_file():
    assert apply_splices(DATA, [S(0, 0, b"^"), S(10, 10, b"$")]) == b"^0123456789$"


@pytest.mark.parametrize(
    "bad",
    [
        [S(2, 5, b"a"), S(4, 6, b"b")],  # partial overlap
        [S(2, 6, b"a"), S(3, 4, b"b")],  # nested
        [S(2, 6, b"a"), S(4, 4, b"b")],  # insertion strictly inside a replacement
        [S(2, 5, b"a"), S(2, 5, b"b")],  # same range, different text
    ],
)
def test_overlaps_are_rejected(bad):
    with pytest.raises(SpliceError, match=r"\d"):
        apply_splices(DATA, bad)
    with pytest.raises(SpliceError):
        OffsetMap(bad)


@pytest.mark.parametrize("bad", [S(-1, 2, b""), S(5, 4, b""), S(9, 11, b""), S(11, 11, b"x")])
def test_out_of_range_is_rejected(bad):
    with pytest.raises(SpliceError):
        apply_splices(DATA, [bad])


def test_offset_map_forward_and_backward():
    splices = [S(2, 4, b"AB!"), S(6, 6, b"<>"), S(8, 9, b"")]
    out = apply_splices(DATA, splices)  # 01AB!45<>679
    m = OffsetMap(splices)
    # untouched bytes map to where the same byte now is, and back
    for old in (0, 1, 4, 5, 6, 7, 9):
        new = m.forward(old)
        assert out[new] == DATA[old], old
        assert m.backward(new) == old
    # inside the replaced range: first byte of the replacement
    assert m.forward(2) == 2 and m.forward(3) == 2
    # inside replacement text: start of that splice
    assert m.backward(2) == 2 and m.backward(3) == 2 and m.backward(4) == 2
    # inside inserted text: the insertion point
    assert m.backward(7) == 6 and m.backward(8) == 6
    # deleted byte maps to what follows it
    assert m.forward(8) == m.forward(9)
    # end of file
    assert m.forward(10) == len(out) and m.backward(len(out)) == 10


def test_offset_map_identity_when_empty():
    m = OffsetMap([])
    assert [m.forward(i) for i in range(11)] == list(range(11))
    assert [m.backward(i) for i in range(11)] == list(range(11))


def test_unified_diff_basic_and_empty():
    before = b"a\nb\nc\n"
    after = b"a\nB\nc\n"
    diff = unified_diff(Path("rtl/x.sv"), before, after)
    assert diff.splitlines()[:2] == ["--- a/rtl/x.sv", "+++ b/rtl/x.sv"]
    assert "-b" in diff.splitlines() and "+B" in diff.splitlines()
    assert unified_diff(Path("rtl/x.sv"), before, before) == ""
    assert unified_diff(Path("ignored"), before, after, label="top.sv").startswith("--- a/top.sv\n")


def test_unified_diff_crlf_has_no_blank_filler_lines():
    diff = unified_diff(P, b"a\r\nb\r\n", b"a\r\nB\r\n")
    assert "" not in diff.splitlines()
    assert "\r" not in diff
