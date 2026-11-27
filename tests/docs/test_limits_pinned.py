"""Rows of the chapter on limits that did not say what the tool does (#106).

Every item of `docs/manual/chapters/limits.tex` was run on 2026-11-26. Eight rows did not match.
Two were corrected with #137 (a clock through an inverter, two asynchronous resets; their tests
are in tests/equiv/test_equiv_reset.py). This file pins the other six, so that the text and the
tool cannot drift apart again without a test that fails.

RULES

P1  A `specify` block and a covergroup do not stop the comparison: a harmless edit beside one is
    "proved". A property with an assertion and a `fork` do: the answer is "error". (The chapter
    said "error" for all four.)
P2  A change in the text of a `$display` is not seen: it is "proved". A change of a delay is
    "error", because the reader refuses the delay. A change of the value that an `initial` block
    gives is never "proved". (The chapter said that none of the three is seen.)
P3  The dangling check sees an inout port: the removal of its only driver is refused. (The
    chapter listed it as not covered.)
P4  The dangling check sees a new reader of a signal that had no driver in the input design: the
    sign-off fails and names the signal. (The chapter listed it as not covered.)
P5  `Vet.punch_port` refuses to take an interface port as its signal. It does punch a plain new
    port into a module that has an interface port. (The chapter said that it refuses interface
    ports.)
P6  A hierarchical path through an instance with an escaped name works when the path is written
    as the design prints it, with the backslash and the blank after the name. The spelling
    without them is refused. (The chapter said that such a path is refused.)

Pinned in another file: what `Instance.replace_module` does with a module that has an interface
port. The chapter says it refuses one. When this file was written, a port that is an interface
with no modport was not refused (#141). It is refused since #148, and the rules F1 to F10 of
tests/ops/test_replace_interface.py and tests/ops/test_replace_interface_review.py pin the
sentence of the chapter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet import equiv
from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session

needs_yosys = pytest.mark.skipif(not equiv.available(), reason="Yosys (hdl-vet[equiv]) is not installed")


def vet_of(tmp_path: Path, files: dict[str, str]) -> Vet:
    root = tmp_path / "design"
    (root / "rtl").mkdir(parents=True)
    for name, text in files.items():
        (root / "rtl" / name).write_bytes(text.encode())
    (root / "design.f").write_bytes("".join(f"rtl/{name}\n" for name in files).encode())
    return Vet(Session(root / "design.f", top="top"))


def splice(vet: Vet, name: str, old: str, new: str) -> None:
    path = next(p for p in vet.session.files if p.name == name)
    at = vet.text(path).index(old.encode())
    vet.splice(path, at, at + len(old), new.encode())


def status(result) -> str:
    (record,) = result.equivalence
    return record.status


# --- P1: constructs that the reader of the proof does not take --------------------------------------

BASE = "module top (input logic clk, input logic a, output logic y);\n  assign y = a;\n{extra}\nendmodule\n"
CONSTRUCTS = {
    "specify": ("  specify\n    (a => y) = 1;\n  endspecify", "proved"),
    "covergroup": ("  covergroup cg @(posedge clk);\n    coverpoint a;\n  endgroup\n  cg c = new;", "proved"),
    "property": ("  property p; @(posedge clk) a; endproperty\n  assert property (p);", "error"),
    "fork": ("  initial begin\n    fork\n      #1;\n    join\n  end", "error"),
}


@needs_yosys
@pytest.mark.equiv
@pytest.mark.parametrize("construct", CONSTRUCTS)
def test_a_harmless_edit_beside_a_construct(tmp_path, construct):
    extra, expected = CONSTRUCTS[construct]
    vet = vet_of(tmp_path, {"top.sv": BASE.format(extra=extra)})
    splice(vet, "top.sv", "assign y = a;", "assign y = a & 1'b1;")
    result = vet.finish()
    assert status(result) == expected, result.report()
    assert result.ok == (expected == "proved")


# --- P2: what only a simulation runs ----------------------------------------------------------------


@needs_yosys
@pytest.mark.equiv
def test_a_changed_display_text_is_not_seen(tmp_path):
    vet = vet_of(tmp_path, {"top.sv": BASE.format(extra='  always @(posedge clk) $display("one");')})
    splice(vet, "top.sv", '"one"', '"two"')
    result = vet.finish()
    assert status(result) == "proved", result.report()
    assert result.ok


@needs_yosys
@pytest.mark.equiv
def test_a_changed_delay_is_an_error(tmp_path):
    vet = vet_of(tmp_path, {"top.sv": "module top (input logic a, output logic y);\n  assign #1 y = a;\nendmodule\n"})
    splice(vet, "top.sv", "#1", "#5")
    result = vet.finish()
    assert status(result) == "error", result.report()
    assert not result.ok


@needs_yosys
@pytest.mark.equiv
def test_a_changed_value_of_an_initial_block_is_never_proved(tmp_path):
    text = "module top (input logic a, output logic y);\n  logic d;\n  initial d = 1'b0;\n  assign y = a;\nendmodule\n"
    vet = vet_of(tmp_path, {"top.sv": text})
    splice(vet, "top.sv", "d = 1'b0", "d = 1'b1")
    result = vet.finish()
    assert status(result) in ("refuted", "error"), result.report()
    assert not result.ok


# --- P3, P4: the dangling check ---------------------------------------------------------------------


def test_the_removal_of_the_only_driver_of_an_inout_port_is_refused(tmp_path):
    text = (
        "module top (input logic a, input logic en, inout wire io, output logic y);\n"
        "  assign io = en ? a : 1'bz;\n"
        "  assign y = io;\n"
        "endmodule\n"
    )
    vet = vet_of(tmp_path, {"top.sv": text})
    with pytest.raises(VetError) as e:
        vet.module("top").remove_assign("io", was="en ? a : 1'bz")
    assert str(e.value).startswith("remove_assign top: "), str(e.value)
    assert "io" in str(e.value)
    assert vet.log == []


def test_a_new_reader_of_a_signal_that_nothing_drove_fails_the_sign_off(tmp_path):
    text = (
        "module top (input logic a, output logic y, output logic z);\n  logic floating;\n  assign y = a;\nendmodule\n"
    )
    vet = vet_of(tmp_path, {"top.sv": text})
    vet.module("top").add_assign("z", "floating")
    result = vet.finish()
    assert not result.ok
    assert any("floating" in d.message for d in result.dangling), result.report()


# --- P5: interface ports and punch_port -------------------------------------------------------------

INTERFACE = {
    "ifc.sv": "interface ifc;\n  logic v;\nendinterface\n",
    "leaf.sv": "module leaf (ifc i, input logic a, output logic y);\n  assign y = a & i.v;\nendmodule\n",
    "top.sv": "module top (input logic a, output logic y);\n  ifc b ();\n  leaf u (.i(b), .a(a), .y(y));\nendmodule\n",
}


def test_an_interface_port_cannot_be_the_signal_of_a_punch(tmp_path):
    vet = vet_of(tmp_path, INTERFACE)
    with pytest.raises(VetError) as e:
        vet.punch_port("pp", "output", to="top.u", signal="i")
    assert str(e.value).startswith("punch_port"), str(e.value)
    assert "interface" in str(e.value)
    assert vet.log == []


def test_a_plain_port_is_punched_into_a_module_that_has_an_interface_port(tmp_path):
    vet = vet_of(tmp_path, INTERFACE)
    vet.punch_port("pp", "input", to="top.u")
    result = vet.finish()
    assert result.ok, result.report()
    assert ("port-added", "top.u", "pp") in [(c.kind, c.path, c.name) for c in result.changes]


# --- P6: a path through an escaped name -------------------------------------------------------------

ESCAPED = {
    "leaf.sv": "module leaf (input logic a, output logic y);\n  assign y = a;\nendmodule\n",
    "top.sv": "module top (input logic a, output logic y);\n  leaf \\u/esc (.a(a), .y(y));\nendmodule\n",
}


def test_a_path_through_an_escaped_name_works_as_the_design_prints_it(tmp_path):
    vet = vet_of(tmp_path, ESCAPED)
    vet.punch_port("pp", "input", to="top.\\u/esc ")
    result = vet.finish()
    assert result.ok, result.report()
    assert ("port-added", "top.\\u/esc ", "pp") in [(c.kind, c.path, c.name) for c in result.changes]


def test_the_spelling_without_the_backslash_is_refused(tmp_path):
    vet = vet_of(tmp_path, ESCAPED)
    with pytest.raises(VetError) as e:
        vet.punch_port("pp", "input", to="top.u/esc")
    assert str(e.value).startswith("punch_port"), str(e.value)
    assert vet.log == []
