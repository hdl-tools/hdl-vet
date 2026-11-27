"""Stated limits that had no test (#106).

A survey of the chapter on limits on 2026-11-27 listed the claims that no test asserts. This file
takes those that were written on that day, with the status `cancelled` (#147), the notes (#146)
and the interface ports (#148). Each test shows that the limit is still a limit. A test that fails
means that the limit is gone or is stated wrongly, and the docstring of its part and the chapter
on limits have to change with it.

The status `cancelled` (tests/ops/test_cancelled.py)

S1  `punch_port` undone by `remove_port` in each module: the port records are "cancelled", and
    the `connect` records stay "applied".
S2  `set_param` to a value and back to the first value: both records stay "applied".
S3  A replacement pair that another replacement of the instance follows is "cancelled" with no
    look at the design, also when the way back left another parameter value.
S4  A pair stays "cancelled" when a raw splice afterwards writes the same thing again. The
    sign-off of that run fails, by the check of the removal.
S5  After a resume, two things of one kind in one module: the records of the saved part stay
    "applied".

The notes (tests/ops/test_notes.py)

S6  A signal that is removed, added again and made unused again leaves the note of each
    operation that made it unused.
S7  A final design with an error is still judged: a note about a thing that is gone is left
    out, and a note that is true stays. (The limit of the notes is about a design that cannot
    be loaded at all. A design with a compile error is loaded.)

The interface ports (tests/ops/test_replace_interface_review.py)

S8  A module that is named `__hdlvet_probe` cannot be the module that the instance has.

The session by itself (the item "Raw logic edits" of the chapter)

S9  The session used with no `Vet` has no marks and no comparison: a splice that changes what an
    assignment computes signs off as "ok", with no proof and no waiver.

Not pinned here: two signals of one name in two scopes of one module keep the note of the one
that is gone. The tool refuses to remove a declaration of a generate block, so the run that shows
it needs a raw splice with a proof. And: when the final design cannot be loaded, every note
stays. No small edit was found that makes the load fail: a missing include, an unknown macro and
a string with no end all give a design with errors, which is loaded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet import equiv
from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session

LEAF = (
    "module {name} #(parameter int W = 1, parameter int K = 0)\n"
    "  (input logic a, input logic spare, output logic y);\n"
    "  assign y = a;\n"
    "endmodule\n"
)
TOP = (
    "module top (input logic a, input logic b, output logic y, output logic z);\n"
    "  leaf u0 (.a(a), .spare(b), .y(y));\n"
    "  leaf u1 (.a(a), .spare(b), .y(z));\n"
    "endmodule\n"
)
WHY = "the reviewer looked at it"


@pytest.fixture(autouse=True)
def no_yosys(monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)


def build(tmp_path: Path, files: dict[str, str]) -> Vet:
    root = tmp_path / "design"
    (root / "rtl").mkdir(parents=True)
    for name, text in files.items():
        (root / "rtl" / name).write_bytes(text.encode())
    (root / "design.f").write_bytes("".join(f"rtl/{name}\n" for name in files).encode())
    return Vet(Session(root / "design.f", top="top"))


@pytest.fixture
def vet(tmp_path: Path) -> Vet:
    files = {"leaf.sv": LEAF.format(name="leaf"), "leaf2.sv": LEAF.format(name="leaf2"), "top.sv": TOP}
    return build(tmp_path, files)


def log(vet: Vet) -> list[tuple[str, str, str]]:
    return [(r.op, r.target, r.status) for r in vet.log]


def top(vet: Vet):
    return vet.module("top")


def leaf(vet: Vet):
    return vet.module("leaf", instances=2)


# --- S1 -------------------------------------------------------------------------------------------


def test_the_connect_records_of_a_punch_that_is_undone_stay_applied(vet):
    vet.punch_port("np", "input", "top.u0", all_instances=True)
    leaf(vet).remove_port("np")
    top(vet).remove_port("np")
    result = vet.finish()
    assert result.ok, result.report()
    assert result.files == {}
    by_op: dict[str, set[str]] = {}
    for op, _, status in log(vet):
        by_op.setdefault(op, set()).add(status)
    assert by_op == {"add_port": {"cancelled"}, "remove_port": {"cancelled"}, "connect": {"applied"}}, log(vet)


# --- S2 -------------------------------------------------------------------------------------------


def test_a_parameter_set_and_set_back_is_applied_twice(vet):
    u0 = top(vet).instance("u0")
    u0.set_param("K", 1, waiver=WHY)
    u0.set_param("K", 0, was=1, waiver=WHY)
    vet.finish()
    assert [status for _, _, status in log(vet)] == ["applied", "applied"]


# --- S3 -------------------------------------------------------------------------------------------


def test_a_pair_that_another_replacement_follows_is_cancelled_with_no_look(vet):
    u0 = top(vet).instance("u0")
    u0.replace_module("leaf2", waiver=WHY)
    u0.replace_module("leaf", set_params={"K": 1}, waiver=WHY)
    u0.replace_module("leaf2", waiver=WHY)
    vet.finish()
    assert [status for _, _, status in log(vet)] == ["cancelled", "cancelled", "applied"]


# --- S4 -------------------------------------------------------------------------------------------


def test_a_pair_stays_cancelled_when_a_raw_splice_writes_the_thing_again(vet):
    top(vet).add_wire("w2")
    top(vet).remove_wire("w2")
    path = next(p for p in vet.session.files if p.name == "top.sv")
    at = vet.session.text(path).index(b"  leaf u0")
    vet.splice(path, at, at, b"  logic w2;\n", waiver=WHY)
    result = vet.finish()
    assert not result.ok
    assert "remove_wire top w2: the declaration is still there" in result.report(), result.report()
    assert [(op, status) for op, _, status in log(vet)][:2] == [("add_wire", "cancelled"), ("remove_wire", "cancelled")]


# --- S5 -------------------------------------------------------------------------------------------


def test_two_things_of_one_kind_are_not_paired_after_a_resume(vet, tmp_path):
    top(vet).add_wire("w2")
    top(vet).add_wire("w3")
    top(vet).remove_wire("w2")
    vet.save(tmp_path / "s.zip")
    back = Vet.restore(tmp_path / "s.zip")
    result = back.finish()
    assert result.ok, result.report()
    assert [status for _, _, status in log(back)] == ["applied", "applied", "applied"]


def test_the_same_run_with_no_resume_is_paired(vet):
    """The other side of S5: without the save, the pair is found."""
    top(vet).add_wire("w2")
    top(vet).add_wire("w3")
    top(vet).remove_wire("w2")
    vet.finish()
    assert [status for _, _, status in log(vet)] == ["cancelled", "applied", "cancelled"]


# --- S6, S7 ---------------------------------------------------------------------------------------


def unused(module) -> None:
    """Leaves `dbg` declared and unused: the removal of its only reader makes the note."""
    module.add_wire("dbg")
    module.add_assign("dbg", "a")
    module.remove_assign("dbg")


def about_dbg(notes: list[str]) -> list[str]:
    return [n for n in notes if "'dbg'" in n]


def test_a_signal_made_unused_twice_leaves_two_notes(vet):
    unused(leaf(vet))
    leaf(vet).remove_wire("dbg")
    unused(leaf(vet))
    result = vet.finish()
    assert result.ok, result.report()
    assert len(about_dbg(result.notes)) == 2, result.notes


@pytest.mark.parametrize("gone", [True, False], ids=["gone", "true"])
@pytest.mark.parametrize("bad", [b"  assign = ;\n", b'`include "nofile.svh"\n'], ids=["syntax", "include"])
def test_the_notes_of_a_final_design_with_an_error_are_still_judged(vet, gone, bad):
    unused(leaf(vet))
    if gone:
        leaf(vet).remove_wire("dbg")
    assert about_dbg(vet.notes), vet.notes
    path = next(p for p in vet.session.files if p.name == "top.sv")
    at = vet.session.text(path).index(b"  leaf u0")
    vet.splice(path, at, at, bad, waiver=WHY)
    result = vet.finish()
    assert not result.ok
    assert result.load_error is None
    assert len(about_dbg(result.notes)) == (0 if gone else 1), result.notes


# --- S8 -------------------------------------------------------------------------------------------


def test_the_module_that_the_instance_has_cannot_have_the_name_of_the_helper(tmp_path):
    probe = "__hdlvet_probe"
    files = {
        "probe.sv": LEAF.format(name=probe),
        "leaf2.sv": LEAF.format(name="leaf2"),
        "top.sv": TOP.replace("leaf u", f"{probe} u"),
    }
    vet = build(tmp_path, files)
    with pytest.raises(VetError) as caught:
        top(vet).instance("u0").replace_module("leaf2", waiver=WHY)
    message = str(caught.value)
    assert message.startswith("replace_module top.u0: "), message
    assert probe in message and "taken by" in message, message
    assert list(vet.log) == []


# --- S9 -------------------------------------------------------------------------------------------


def test_the_session_by_itself_compares_nothing(tmp_path):
    root = tmp_path / "design"
    (root / "rtl").mkdir(parents=True)
    path = root / "rtl" / "top.sv"
    path.write_bytes(b"module top (input logic [3:0] a, output logic [3:0] y);\n  assign y = a;\nendmodule" + b"\n")
    (root / "design.f").write_bytes(b"rtl/top.sv" + b"\n")
    session = Session(root / "design.f", top="top")
    old = b"assign y = a;"
    at = path.read_bytes().index(old)
    session.splice(path, at, at + len(old), b"assign y = ~a;", old=old)
    result = session.finish()
    assert result.ok, result.report()
    assert result.equivalence == []
    assert [p.name for p in result.files] == ["top.sv"]
