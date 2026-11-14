"""Phase 2: sign-off comparisons (`hdl_vet.signoff`). Pure Python, no pyslang.

    new_diagnostics(before, after, maps) -> list[Diag]
        before, after: list[Diag] of the baseline and the modified design.
        maps: Mapping[Path, Sequence[OffsetMap]], for each changed file the
              offset maps of every re-load stage, in order (baseline -> final).
        Notes (severity "note") are ignored on both sides.
        Each diagnostic is keyed on (code, path, canonical offset):
          - after:  its offset mapped backward through every stage, last first;
          - before: its offset mapped forward through every stage, then backward
                    again (so a location inside replaced text lands on the start
                    of that replacement, exactly where an `after` diagnostic
                    inside the new text lands).
        Line and severity are never part of the key. Matching is in two passes,
        each a multiset match that consumes one baseline diagnostic per match:
          1. exact: (code, path, canonical offset, message);
          2. location only: (code, path, canonical offset), allowed only when
             BOTH diagnostics are untouched and neither has in_macro set. This
             pass exists because an edit elsewhere (a rename) may change the
             message of a diagnostic whose own text was not edited.
        A baseline diagnostic is "touched" if, at some stage of its forward
        mapping, it lies inside a replaced range. An `after` diagnostic is
        touched if, at some stage of its backward mapping, it lies inside
        replacement or inserted text. Inside edited text a vanished warning must
        never pay for a new one, so there the message has to match exactly;
        the same holds for macro expansions, whose content can change through an
        edit in another file.
        The result is the unmatched `after` diagnostics, sorted by
        (path, offset, code). Diagnostics without a location use offset None;
        a None path or offset sorts before any value.

    Diag has a seventh field, `in_macro: bool = False`: True when the
    diagnostic's original location was inside a macro expansion.

    Change (frozen): kind, path, name, before, after
        kind is one of
          "instance-added"      name "",  before None,   after module
          "instance-removed"    name "",  before module, after None
          "module-changed"      name "",  before/after module
          "param-changed"       name = parameter; None on the missing side
          "port-added"          name = port; after  = "<direction> <width>"
          "port-removed"        name = port; before = "<direction> <width>"
          "port-changed"        name = port; both sides "<direction> <width>"
          "connection-changed"  name = port; expression text, None when the port
                                does not exist on that side
        `path` is the instance's hierarchical path.

    diff_snapshots(before, after) -> list[Change]
        Sorted by (path, kind, name). An added or removed instance produces
        only its instance-added / instance-removed change (plus the same for
        each instance below it), never port or parameter changes. An instance
        whose module changed produces "module-changed" and, in addition, any
        parameter, port and connection changes between the two.

    Expect (frozen): kind, path = "*", name = "*"
        All three are case-sensitive glob patterns (fnmatchcase rules).

    check_changes(changes, expects) -> tuple[list[Change], list[Expect]]
        (changes matched by no Expect, Expects that matched no change).
"""

from __future__ import annotations

from pathlib import Path

from hdl_vet.signoff import Change, Expect, check_changes, diff_snapshots, new_diagnostics
from hdl_vet.slang_adapter import Diag, InstanceSnap
from hdl_vet.splice import OffsetMap, Splice

F = Path("a.sv")
G = Path("b.sv")


def D(code: str, offset: int | None, path: Path | None = F, sev: str = "warning", msg: str = "m") -> Diag:
    return Diag(sev, code, msg, path, None if offset is None else 1, offset)


def test_identical_lists_have_nothing_new():
    diags = [D("X", 5), D("Y", 9), D("Z", None, path=None)]
    assert new_diagnostics(diags, list(diags), {}) == []


def test_shifted_location_is_not_new():
    maps = {F: [OffsetMap([Splice(F, 2, 2, b"12345")])]}
    assert new_diagnostics([D("X", 10)], [D("X", 15, msg="other text")], maps) == []


def test_unshifted_location_after_an_insertion_is_new():
    maps = {F: [OffsetMap([Splice(F, 2, 2, b"12345")])]}
    extra = new_diagnostics([D("X", 10)], [D("X", 15), D("X", 10)], maps)
    assert len(extra) == 1


def test_same_diagnostic_inside_a_replaced_span_is_not_new():
    # bytes [10, 17) replaced by 12 bytes; the warning sat at 12, now sits at 13
    maps = {F: [OffsetMap([Splice(F, 10, 17, b"renamed_long")])]}
    assert new_diagnostics([D("ImplicitNet", 12, msg="same")], [D("ImplicitNet", 13, msg="same")], maps) == []


def test_vanished_diagnostic_does_not_pay_for_a_different_one_in_the_same_span():
    maps = {F: [OffsetMap([Splice(F, 10, 17, b"renamed_long")])]}
    out = new_diagnostics([D("ImplicitNet", 12, msg="net 'a'")], [D("ImplicitNet", 13, msg="net 'typo'")], maps)
    assert [d.message for d in out] == ["net 'typo'"]


def test_diagnostic_in_inserted_text_is_not_masked_by_one_that_vanished_at_the_insertion_point():
    maps = {F: [OffsetMap([Splice(F, 10, 10, b"12345")])]}
    out = new_diagnostics([D("X", 10, msg="old")], [D("X", 12, msg="other")], maps)
    assert len(out) == 1
    assert new_diagnostics([D("X", 10, msg="old")], [D("X", 15, msg="old")], maps) == []


def test_macro_diagnostics_need_an_exact_message_match():
    before = [Diag("warning", "ImplicitNet", "net 'spare_q'", F, 1, 40, True)]
    same = [Diag("warning", "ImplicitNet", "net 'spare_q'", F, 1, 40, True)]
    other = [Diag("warning", "ImplicitNet", "net 'other_typo'", F, 1, 40, True)]
    assert new_diagnostics(before, same, {}) == []
    assert new_diagnostics(before, other, {}) == other


def test_different_code_at_same_place_is_new():
    out = new_diagnostics([D("X", 5)], [D("Y", 5)], {})
    assert [d.code for d in out] == ["Y"]


def test_multiset_counts():
    before = [D("X", 5), D("X", 5)]
    assert new_diagnostics(before, [D("X", 5), D("X", 5)], {}) == []
    assert len(new_diagnostics(before, [D("X", 5), D("X", 5), D("X", 5)], {})) == 1
    assert new_diagnostics(before, [D("X", 5)], {}) == []


def test_other_file_is_not_shifted():
    maps = {F: [OffsetMap([Splice(F, 0, 0, b"xx")])]}
    assert new_diagnostics([D("X", 5, path=G)], [D("X", 5, path=G)], maps) == []
    assert len(new_diagnostics([D("X", 5, path=G)], [D("X", 7, path=G)], maps)) == 1


def test_several_stages_compose():
    # stage 1 inserts 3 bytes at 0; stage 2 (in stage-1 coordinates) inserts 2 bytes at 1
    maps = {F: [OffsetMap([Splice(F, 0, 0, b"abc")]), OffsetMap([Splice(F, 1, 1, b"zz")])]}
    assert new_diagnostics([D("X", 10)], [D("X", 15)], maps) == []
    assert len(new_diagnostics([D("X", 10)], [D("X", 13)], maps)) == 1


def test_notes_are_ignored_and_errors_count():
    out = new_diagnostics([], [D("N", 1, sev="note"), D("E", 2, sev="error")], {})
    assert [d.code for d in out] == ["E"]


def test_no_location_diagnostics():
    assert len(new_diagnostics([], [D("Z", None, path=None)], {})) == 1


def test_result_is_sorted():
    out = new_diagnostics([], [D("B", 9), D("A", 9), D("C", 1), D("A", 0, path=G)], {})
    assert [(d.path, d.offset, d.code) for d in out] == [(F, 1, "C"), (F, 9, "A"), (F, 9, "B"), (G, 0, "A")]


# --- snapshots ----------------------------------------------------------------


def snap(module="leaf", params=None, ports=None, conns=None) -> InstanceSnap:
    return InstanceSnap(
        module=module,
        params=dict(params or {"W": "4"}),
        ports=dict(ports or {"d": ("input", 4), "q": ("output", 4)}),
        connections=dict(conns or {"d": "a", "q": "y"}),
    )


BASE = {"top": snap("top", {}, {"a": ("input", 4)}, {}), "top.u_a": snap(), "top.u_b": snap()}


def test_equal_snapshots_have_no_changes():
    assert diff_snapshots(BASE, dict(BASE)) == []


def test_param_port_and_connection_changes():
    after = dict(BASE)
    after["top.u_a"] = snap(
        params={"W": "8", "NEW": "1"},
        ports={"d": ("input", 8), "en": ("input", 1)},
        conns={"d": "b", "en": "1'b1"},
    )
    assert diff_snapshots(BASE, after) == [
        Change("connection-changed", "top.u_a", "d", "a", "b"),
        Change("connection-changed", "top.u_a", "en", None, "1'b1"),
        Change("connection-changed", "top.u_a", "q", "y", None),
        Change("param-changed", "top.u_a", "NEW", None, "1"),
        Change("param-changed", "top.u_a", "W", "4", "8"),
        Change("port-added", "top.u_a", "en", None, "input 1"),
        Change("port-changed", "top.u_a", "d", "input 4", "input 8"),
        Change("port-removed", "top.u_a", "q", "output 4", None),
    ]


def test_instances_added_removed_and_swapped():
    after = dict(BASE)
    del after["top.u_b"]
    after["top.u_c"] = snap()
    after["top.u_c.u_x"] = snap("inner")
    after["top.u_a"] = snap("other")
    assert diff_snapshots(BASE, after) == [
        Change("module-changed", "top.u_a", "", "leaf", "other"),
        Change("instance-removed", "top.u_b", "", "leaf", None),
        Change("instance-added", "top.u_c", "", None, "leaf"),
        Change("instance-added", "top.u_c.u_x", "", None, "inner"),
    ]


def test_check_changes():
    changes = [
        Change("param-changed", "top.u_a", "W", "4", "8"),
        Change("port-changed", "top.u_a", "d", "input 4", "input 8"),
        Change("instance-added", "top.u_c", "", None, "leaf"),
    ]
    expects = [
        Expect("param-changed", "top.u_a", "W"),
        Expect("instance-added", "top.u_*"),
        Expect("port-added", "top", "dft_*"),
    ]
    unexpected, unmatched = check_changes(changes, expects)
    assert unexpected == [changes[1]]
    assert unmatched == [expects[2]]
    assert check_changes([], []) == ([], [])
    assert check_changes(changes, [Expect("*")]) == ([], [])


def test_expect_patterns_are_case_sensitive_and_kind_must_match():
    c = [Change("param-changed", "top.u_a", "W", "4", "8")]
    assert check_changes(c, [Expect("param-changed", "TOP.U_A", "W")])[0] == c
    assert check_changes(c, [Expect("port-changed", "top.u_a", "W")])[0] == c
    assert check_changes(c, [Expect("param-changed")]) == ([], [])
