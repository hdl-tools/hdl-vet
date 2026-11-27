"""A note about something that is gone (#96).

An operation leaves a note when the design has a new thing to read after it, for example a
variable that nothing uses any more. A later operation of the run can take that thing away again.
The note stayed, and it named a line of the design that no longer exists. Measured on `main` at
fb058b6, with `leaf` elaborated three times:

    leaf.add_wire("dbg"); leaf.add_assign("dbg", "a")
    leaf.remove_assign("dbg"); leaf.remove_wire("dbg")
    ok=True, files={}
      note: remove_assign leaf: unused variable 'dbg' [module leaf, leaf.sv:2]

The design is the one the run started from, and there is no `dbg` in it.

Owner decision of 2026-11-27: at the sign-off a note is kept only when the final design still gives
that diagnostic, by its code and the name it is about, in the same module. The line number is not
compared, because later edits move lines. The note keeps the operation that caused it.

RULES

N1  A note of a diagnostic that the final design no longer gives is not in `Result.notes`, for
    `finish` and for `check`.
N2  A note of a diagnostic that the final design still gives stays, with the operation that
    caused it first, also when a later edit moved the line.
N3  It is decided for each module: the same name in another module does not keep a note alive.
N4  A note that is no diagnostic of the compiler stays as it is. That is the note of
    `replace_module` on a type difference that `accept_types` accepted.
N5  A saved session keeps its notes, and they are judged in the same way after the resume.
N6  The notes of `vet.notes` are not changed by a `check`: a note that a `check` left out can come
    back at the `finish`, when the design gives the diagnostic again by then.

The review found that the first form read too much from the text of a note:

N7  A note is matched by what a diagnostic of the final design says: its message and its module,
    as the tool prints them at the end of the note. What is in front of that, the operation and
    its target, is not parsed. So a target with a colon and a blank in it (a file of
    `add_file` on Linux, `x: b.sv`) does not lose a note that is true, and a module whose
    name has a comma or a bracket (`\\m,x] `) does not keep a note that is gone.

STATED LIMITS

- Two signals with the same name in two scopes of one module (one at the level of the module,
  one in a generate block) give the same message. A note about the one that is gone stays
  while the other one is there.
- A signal that is removed, added again and made unused again leaves the note of each
  operation that made it unused. Both are true of the final design, and they read the same.
- When the final design cannot be loaded, every note stays.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet import equiv
from hdl_vet.ops import Vet
from hdl_vet.session import Session

LEAF = "module leaf (input logic a, output logic y);\n  assign y = a;\nendmodule\n"
OTHER = "module other (input logic a, output logic y);\n  assign y = a;\nendmodule\n"
TOP = (
    "module top (input logic a, output logic [2:0] y, output logic z);\n"
    "  leaf u0 (.a(a), .y(y[0]));\n"
    "  leaf u1 (.a(a), .y(y[1]));\n"
    "  leaf u2 (.a(a), .y(y[2]));\n"
    "  other u3 (.a(a), .y(z));\n"
    "endmodule\n"
)
NOTE = "remove_assign {module}: unused variable 'dbg'"


@pytest.fixture(autouse=True)
def no_yosys(monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)


@pytest.fixture
def vet(tmp_path: Path) -> Vet:
    root = tmp_path / "design"
    (root / "rtl").mkdir(parents=True)
    for name, text in (("leaf", LEAF), ("other", OTHER), ("top", TOP)):
        (root / "rtl" / f"{name}.sv").write_bytes(text.encode())
    (root / "design.f").write_bytes(b"rtl/leaf.sv\nrtl/other.sv\nrtl/top.sv\n")
    return Vet(Session(root / "design.f", top="top"))


def leaf(vet: Vet):
    return vet.module("leaf", instances=3)


def unused(module) -> None:
    """Leaves `dbg` declared and unused: the removal of its only reader makes the note."""
    module.add_wire("dbg")
    module.add_assign("dbg", "a")
    module.remove_assign("dbg")


def starts(notes: list[str], module: str) -> list[str]:
    return [n for n in notes if n.startswith(NOTE.format(module=module))]


# --- N1: a note about something that is gone ------------------------------------------------------


def test_a_pair_that_cancels_leaves_no_note(vet):
    unused(leaf(vet))
    assert starts(vet.notes, "leaf"), vet.notes  # the note was made
    leaf(vet).remove_wire("dbg")
    result = vet.finish()
    assert result.ok, result.report()
    assert result.files == {}
    assert result.notes == []
    assert "note:" not in result.report()


def test_a_check_leaves_it_out_too(vet):
    unused(leaf(vet))
    leaf(vet).remove_wire("dbg")
    assert vet.check().notes == []


# --- N2: a note that is still true ----------------------------------------------------------------


def test_a_note_that_is_still_true_stays(vet):
    unused(leaf(vet))
    result = vet.finish()
    assert result.ok, result.report()
    assert len(starts(result.notes, "leaf")) == 1, result.notes
    assert "  note: remove_assign leaf: unused variable 'dbg'" in result.report()


def test_a_note_stays_when_a_later_edit_moves_the_line(vet):
    unused(leaf(vet))
    leaf(vet).add_wire("first")
    leaf(vet).add_assign("first", "a")
    result = vet.finish()
    assert len(starts(result.notes, "leaf")) == 1, result.notes


# --- N3: each module for itself -------------------------------------------------------------------


def test_the_same_name_in_another_module_keeps_no_note_alive(vet):
    unused(leaf(vet))
    unused(vet.module("other"))
    leaf(vet).remove_wire("dbg")
    result = vet.finish()
    assert starts(result.notes, "leaf") == [], result.notes
    assert len(starts(result.notes, "other")) == 1, result.notes


def test_another_name_in_the_same_module_keeps_no_note_alive(vet):
    unused(leaf(vet))
    leaf(vet).remove_wire("dbg")
    leaf(vet).add_wire("dbg2")
    leaf(vet).add_assign("dbg2", "a")
    leaf(vet).remove_assign("dbg2")
    result = vet.finish()
    assert starts(result.notes, "leaf") == [], result.notes
    assert any("'dbg2'" in n for n in result.notes), result.notes


# --- N7: only the end of a note is read -----------------------------------------------------------

# What `add_file` would write for a file named `x: b.sv`, which only Linux can hold.
ODD = "add_file x: b.sv: unused variable 'dbg' [module leaf, leaf.sv:2]"


def test_a_true_note_with_a_colon_in_its_target_stays(vet):
    unused(leaf(vet))
    vet.notes.append(ODD)
    result = vet.finish()
    assert [n for n in result.notes if n.startswith("add_file x: b.sv: unused variable 'dbg' [module leaf, ")], (
        result.notes
    )
    assert len(starts(result.notes, "leaf")) == 1, result.notes


def test_a_gone_note_with_a_colon_in_its_target_goes(vet):
    unused(leaf(vet))
    vet.notes.append(ODD)
    leaf(vet).remove_wire("dbg")
    assert vet.finish().notes == []


ESCAPED = (
    "module \\m,x] (input logic a, output logic y);\n  logic dbg;\n  assign dbg = a;\n  assign y = a;\nendmodule\n"
)
ETOP = "module top (input logic a, output logic y);\n  \\m,x] u (.a(a), .y(y));\nendmodule\n"


def escaped(tmp_path: Path) -> Vet:
    root = tmp_path / "escaped"
    (root / "rtl").mkdir(parents=True)
    (root / "rtl" / "m.sv").write_bytes(ESCAPED.encode())
    (root / "rtl" / "top.sv").write_bytes(ETOP.encode())
    (root / "design.f").write_bytes(b"rtl/m.sv\nrtl/top.sv\n")
    return Vet(Session(root / "design.f", top="top"))


def test_a_gone_note_of_a_module_with_a_bracket_in_its_name_goes(tmp_path):
    vet = escaped(tmp_path)
    vet.module("m,x]").remove_assign("dbg")
    assert any("'dbg'" in n for n in vet.notes), vet.notes
    vet.module("m,x]").remove_wire("dbg")
    result = vet.finish()
    assert result.ok, result.report()
    assert result.notes == []


def test_a_true_note_of_such_a_module_stays(tmp_path):
    vet = escaped(tmp_path)
    vet.module("m,x]").remove_assign("dbg")
    result = vet.finish()
    assert len([n for n in result.notes if "'dbg'" in n]) == 1, result.notes


# --- N5: a saved session --------------------------------------------------------------------------


def test_a_note_of_a_saved_session_is_judged_after_the_resume(vet, tmp_path):
    unused(leaf(vet))
    vet.save(tmp_path / "s.zip")
    back = Vet.restore(tmp_path / "s.zip")
    assert len(starts(back.check().notes, "leaf")) == 1
    back.module("leaf", instances=3).remove_wire("dbg")
    result = back.finish()
    assert result.ok, result.report()
    assert result.notes == []


# --- N6: the notes of the run are not thrown away -------------------------------------------------


def test_a_check_does_not_change_the_notes_of_the_run(vet):
    unused(leaf(vet))
    before = list(vet.notes)
    leaf(vet).remove_wire("dbg")
    assert vet.check().notes == []
    assert vet.notes == before
