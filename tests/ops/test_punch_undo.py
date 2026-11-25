"""`punch_port` undone with `remove_port` in each module (#57, the fourth part).

`Vet.punch_port` is `add_port` in each module of the path and `connect` at each instance, as its
log shows. `remove_port` in each module, from the bottom up, takes all of it out again: it removes
the port and, at every instance of the module, the connection of that port. The port parts cancel
since #100. The connection parts did not. Measured on `main` at 27dbb6b:

    vet.punch_port("pp", "input", to="top.u_m.u_l")
    leaf.remove_port("pp"); mid.remove_port("pp"); top.remove_port("pp")
    ok=False, files={}
      expected change not seen: connection-changed path=top.u_m.u_l name=pp
      expected change not seen: connection-changed path=top.u_m name=pp
      failed check: connect mid.u_l pp: expected 'pp', found None
      failed check: connect top.u_m pp: expected 'pp', found None

RULES

U1  `remove_port` of a module takes the connection of that port away at every instance of the
    module. A check of a `connect` to that port at such an instance is retired with it: the
    port is gone, so the check can say nothing.
U2  At such an instance, a `connection-changed` expectation for that port that an operation
    registered is forgotten when the design the session started from has no connection of
    that port there. Then the connection is back to what it was: none. When the first design
    has one, the removal is a change, and the expectation stays and is met.
U3  So `punch_port` and then `remove_port` in each module of the path, from the bottom up,
    signs off with no file changed. For an output with `signal=`, the assignment that
    `punch_port` wrote is removed first, with `remove_assign`, and the removals are in one
    group: alone, the removal of the assignment leaves an output with no driver and is refused.
U4  U1 and U2 are for that port at those instances only. A `connect` to another port of the
    same instance keeps its check and its expectation.
U5  The order from the top down is refused as before: the connection would name a net that
    is not declared.
U6  A removal of the port in one module only leaves the ports above it. The run signs off
    with those changes, and the connection that is gone is no failure.
U7  An expectation that the script registered stays (rule B9 of tests/ops/test_replace_back.py).
U8  A group that is rolled back gives the check and the expectation back. A save between
    the calls changes nothing.
U9  A `remove_port` that is refused retires nothing and forgets nothing, also inside a group
    where the script catches the refusal. The check and the expectation go only when the
    removal is committed (found by the review: the check went before the refusal).

Not part of this file: the status `cancelled` of the records (#57), and `propagate_param`
undone with `remove_param`, which was not measured.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session

LEAF = (
    "module leaf (input logic a, input logic spare, output logic y);\n"
    "  logic n;\n"
    "  assign n = ~a;\n"
    "  assign y = n;\n"
    "endmodule\n"
)
MID = "module mid (input logic a, input logic b, output logic y);\n  leaf u_l (.a(a), .spare(b), .y(y));\nendmodule\n"
TOP = "module top (input logic a, input logic b, output logic y);\n  mid u_m (.a(a), .b(b), .y(y));\nendmodule\n"
PATH = ("leaf", "mid", "top")


@pytest.fixture
def filelist(tmp_path: Path) -> Path:
    root = tmp_path / "design"
    (root / "rtl").mkdir(parents=True)
    for name, text in (("leaf", LEAF), ("mid", MID), ("top", TOP)):
        (root / "rtl" / f"{name}.sv").write_bytes(text.encode())
    path = root / "design.f"
    path.write_bytes(b"rtl/leaf.sv\nrtl/mid.sv\nrtl/top.sv\n")
    return path


@pytest.fixture
def vet(filelist: Path) -> Vet:
    return Vet(Session(filelist, top="top"))


def punch(vet: Vet) -> None:
    vet.punch_port("pp", "input", to="top.u_m.u_l")


def unpunch(vet: Vet, modules: tuple[str, ...] = PATH) -> None:
    for module in modules:
        vet.module(module).remove_port("pp")


def kinds(result) -> list[tuple[str, str, str]]:
    return sorted((c.kind, c.path, c.name) for c in result.changes)


def clean(result) -> None:
    assert [str(c) for c in result.failed_checks] == []
    assert [(e.kind, e.path, e.name) for e in result.unmatched_expects] == []
    assert result.ok, result.report()


def test_the_design_is_what_the_tests_need(vet):
    assert vet.design.diagnostics() == []
    punch(vet)
    result = vet.finish()
    clean(result)
    assert ("port-added", "top", "pp") in kinds(result)


# --- U1, U2, U3: the whole way back --------------------------------------------------------


def test_a_punched_input_that_is_removed_bottom_up_signs_off(vet):
    punch(vet)
    unpunch(vet)
    result = vet.finish()
    clean(result)
    assert result.files == {}
    assert result.changes == []


def test_a_punched_output_that_is_removed_bottom_up_signs_off(vet):
    vet.punch_port("pp", "output", to="top.u_m.u_l", signal="n")
    with vet.group():
        vet.module("leaf").remove_assign("pp")
        unpunch(vet)
    result = vet.finish()
    clean(result)
    assert result.files == {}


def test_punched_removed_and_punched_again_is_the_punch(vet):
    punch(vet)
    unpunch(vet)
    punch(vet)
    result = vet.finish()
    clean(result)
    assert ("port-added", "top", "pp") in kinds(result)
    assert ("connection-changed", "top.u_m.u_l", "pp") in kinds(result)


# --- U2: a connection that the first design has --------------------------------------------


def test_a_connection_of_the_first_design_that_goes_with_its_port_is_a_change(vet):
    """`spare` of `leaf` is connected to `b` in the first design. The script moves it, then removes the port."""
    vet.module("mid").instance("u_l").connect("spare", "a", was="b")
    vet.module("leaf").remove_port("spare")
    result = vet.finish()
    clean(result)
    assert ("port-removed", "top.u_m.u_l", "spare") in kinds(result)


# --- U4: only that port --------------------------------------------------------------------


def test_another_connection_of_the_instance_keeps_its_check(vet):
    vet.module("mid").instance("u_l").connect("spare", "a", was="b")
    punch(vet)
    unpunch(vet)
    result = vet.finish()
    clean(result)
    assert kinds(result) == [("connection-changed", "top.u_m.u_l", "spare")]


def test_another_connection_that_a_raw_edit_puts_back_fails(vet):
    vet.module("mid").instance("u_l").connect("spare", "a", was="b")
    punch(vet)
    unpunch(vet)
    mid = next(p for p in vet.session.files if p.name == "mid.sv")
    text = vet.text(mid)
    start = text.index(b".spare(a)")
    vet.splice(mid, start, start + len(b".spare(a)"), b".spare(b)", waiver="a test")
    result = vet.finish()
    assert not result.ok
    assert any("spare" in str(c) for c in result.failed_checks), result.report()


# --- U5: the order from the top down -------------------------------------------------------


def test_the_order_from_the_top_down_is_refused(vet):
    punch(vet)
    with pytest.raises(VetError) as e:
        vet.module("top").remove_port("pp")
    assert str(e.value).startswith("remove_port top: "), str(e.value)


# --- U6: one module only -------------------------------------------------------------------


def test_a_removal_in_the_lowest_module_only_signs_off_with_the_rest(vet):
    punch(vet)
    vet.module("leaf").remove_port("pp")
    result = vet.finish()
    clean(result)
    assert kinds(result) == [
        ("connection-changed", "top.u_m", "pp"),
        ("port-added", "top", "pp"),
        ("port-added", "top.u_m", "pp"),
    ]


# --- U7: the expectation of the script -----------------------------------------------------


def test_the_expectation_of_the_script_stays(vet):
    vet.expect("connection-changed", "top.u_m.u_l", "pp")
    punch(vet)
    unpunch(vet)
    result = vet.finish()
    assert not result.ok
    assert [(e.kind, e.path, e.name) for e in result.unmatched_expects] == [("connection-changed", "top.u_m.u_l", "pp")]


# --- U8: a group and a save ----------------------------------------------------------------


def test_a_rolled_back_group_gives_the_check_and_the_expectation_back(vet):
    punch(vet)
    with pytest.raises(RuntimeError), vet.group():
        vet.module("leaf").remove_port("pp")
        raise RuntimeError("stop the group")
    result = vet.finish()
    clean(result)
    assert ("connection-changed", "top.u_m.u_l", "pp") in kinds(result)
    mid = next(p for p in vet.session.files if p.name == "mid.sv")
    assert b".pp(pp)" in vet.text(mid)


def test_a_rolled_back_removal_and_a_raw_edit_of_the_connection_fails(vet):
    """After the roll back the check of the `connect` is there again, so it still guards the connection."""
    punch(vet)
    with pytest.raises(RuntimeError), vet.group():
        vet.module("leaf").remove_port("pp")
        raise RuntimeError("stop the group")
    mid = next(p for p in vet.session.files if p.name == "mid.sv")
    text = vet.text(mid)
    start = text.index(b".pp(pp)")
    vet.splice(mid, start, start + len(b".pp(pp)"), b".pp(a)", waiver="a test")
    result = vet.finish()
    assert not result.ok
    assert any("pp" in str(c) for c in result.failed_checks), result.report()


def test_a_save_between_the_punch_and_the_removal_changes_nothing(vet, tmp_path):
    punch(vet)
    vet.save(tmp_path / "s.zip")
    back = Vet.restore(tmp_path / "s.zip")
    unpunch(back)
    result = back.finish()
    clean(result)
    assert result.files == {}


# --- U9: a refusal retires nothing ---------------------------------------------------------

# A second parent of `leaf`, with a directive in its connection list: `remove_port` refuses there.
ZZ = (
    "module zz (input logic a, output logic y);\n"
    "  leaf u_q (.a(a),\n"
    "`ifdef FOO\n"
    "  .spare(a),\n"
    "`endif\n"
    "  .y(y));\n"
    "endmodule\n"
)


@pytest.fixture
def held(filelist: Path) -> Vet:
    (filelist.parent / "rtl" / "zz.sv").write_bytes(ZZ.encode())
    filelist.write_bytes(b"rtl/leaf.sv\nrtl/mid.sv\nrtl/zz.sv\nrtl/top.sv\n")
    return Vet(Session(filelist, top="top"))


def rewire(vet: Vet, new: bytes) -> None:
    mid = next(p for p in vet.session.files if p.name == "mid.sv")
    start = vet.text(mid).index(b".pp(pp)")
    vet.splice(mid, start, start + len(b".pp(pp)"), new, waiver="a test")


def test_a_refused_removal_in_a_group_keeps_the_check(held):
    punch(held)
    with held.group():
        with pytest.raises(VetError) as e:
            held.module("leaf").remove_port("pp")
        assert str(e.value).startswith("remove_port zz.u_q"), str(e.value)
        rewire(held, b".pp(a)")
    result = held.finish()
    assert not result.ok
    assert any("connect mid.u_l pp" in str(c) for c in result.failed_checks), result.report()


def test_a_refused_removal_keeps_the_check_after_the_group_and_after_a_save(held, tmp_path):
    punch(held)
    with held.group(), pytest.raises(VetError):
        held.module("leaf").remove_port("pp")
    held.save(tmp_path / "s.zip")
    back = Vet.restore(tmp_path / "s.zip")
    rewire(back, b".pp(b)")
    result = back.finish()
    assert not result.ok
    assert any("connect mid.u_l pp" in str(c) for c in result.failed_checks), result.report()


def test_a_refused_removal_keeps_the_expectation(held):
    punch(held)
    with held.group(), pytest.raises(VetError):
        held.module("leaf").remove_port("pp")
    assert ("connection-changed", "top.u_m.u_l", "pp") in [
        (e.kind, e.path, e.name) for e in held.session._expects if e.required
    ]
    result = held.finish()
    clean(result)
