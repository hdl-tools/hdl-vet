"""The equivalence engine: a child that is proved inert stays a black box (phase 6, part 3).

THE PROBLEM

A module with an override is compared with its children as black boxes when they
did not change (`same`). A child that has an override of its own did change, so
it was flattened into the comparison of the parent. On demo_ss the top then had
flops on more than one clock and the answer was "error", although the child was
proved on its own.

THE ARGUMENT

The child C of the edited design has inputs E that the child of the input design
does not have. A comparison of C alone has proved: with every E at 0, C is the
child of the input design. So an instance of C whose pins E are 0 can be replaced
by the child of the input design. If, in the comparison of the parent, every
instance of C has its pins E at 0, the child is a black box on both sides.

RULES

I1  `check(..., inert={"child": ["en_i", ...]})` names child modules and, for
    each, the inputs that only the gate's child has. Such a child is a black box
    on both sides. The engine does not look into it and does not compare it: the
    caller says that it is proved.
I2  The engine checks the pins. In the gate, after the ties and after children
    that are not boxes are flattened, each named input of each instance of the
    child is the constant 0. If one is not (the constant 1, a free input, a
    signal, a flop, not connected), the answer is "error" and the detail names
    the instance and the pin. Never "proved".
I3  The rest of the interface is the same. Apart from the named inputs, the
    child has the same ports on both sides (name, direction, width), and each
    instance has the same parameter values on both sides. If not: "error".
    The gold's child has none of the named inputs and the gate's child has each
    of them as an input. If not: "error".
I4  The other connections of the child are compared as for a `same` child: a
    changed connection is "refuted" and the detail names the instance.
I5  A module named in `same` and in `inert` is a wrong request: "error".
    `inert` with a value of a wrong type raises, as `tie` does.
I6  An inert child inside a child that is flattened is found and checked in the
    same way.
I7  What is in the inert child does not matter to the comparison of the parent:
    a child with flops on another clock than the parent's is accepted.
"""

from __future__ import annotations

import os

import pytest

from equiv_designs import equiv, run

REQUIRED = os.environ.get("HDL_VET_REQUIRE_EQUIV") == "1"
needs_yosys = pytest.mark.skipif(not equiv.available() and not REQUIRED, reason="Yosys (hdl-vet[equiv]) is missing")
pytestmark = [pytest.mark.equiv]

LEAF = (
    "module leaf #(\n"
    "  parameter int W = 1\n"
    ") (\n"
    "  input  logic         clk_b,\n"
    "  input  logic         rst_ni,\n"
    "  input  logic [W-1:0] d,\n"
    "  output logic [W-1:0] q\n"
    ");\n"
    "  logic [W-1:0] q_r;\n"
    "\n"
    "  always_ff @(posedge clk_b or negedge rst_ni)\n"
    "    if (!rst_ni) q_r <= '0;\n"
    "    else         q_r <= d;\n"
    "\n"
    "  assign q = q_r;\n"
    "endmodule\n"
)
RST = "  input  logic         rst_ni,\n"
LEAF_EN = LEAF.replace(RST, RST + "  input  logic         en_i,\n").replace(
    "assign q = q_r;", "assign q = en_i ? '0 : q_r;"
)
MID = (
    "module mid (\n"
    "  input  logic clk_a,\n"
    "  input  logic clk_b,\n"
    "  input  logic rst_ni,\n"
    "  input  logic d,\n"
    "  output logic y\n"
    ");\n"
    "  logic lq, r;\n"
    "\n"
    "  leaf u_leaf (.clk_b(clk_b), .rst_ni(rst_ni), .d(d), .q(lq));\n"
    "\n"
    "  always_ff @(posedge clk_a or negedge rst_ni)\n"
    "    if (!rst_ni) r <= 1'b0;\n"
    "    else         r <= lq;\n"
    "\n"
    "  assign y = r;\n"
    "endmodule\n"
)
MID_EN = (
    MID.replace("  input  logic rst_ni,\n", "  input  logic rst_ni,\n  input  logic en_i,\n")
    .replace(".rst_ni(rst_ni), .d(d)", ".rst_ni(rst_ni), .en_i(en_i), .d(d)")
    .replace("  logic lq, r;\n", "  logic lq, r, y_pre;\n")
    .replace("assign y = r;", "assign y_pre = r;\n  assign y = en_i ? 1'b1 : y_pre;")
)
OUTER = (
    "module outer (\n"
    "  input  logic clk_a,\n"
    "  input  logic clk_b,\n"
    "  input  logic rst_ni,\n"
    "  input  logic d,\n"
    "  output logic y\n"
    ");\n"
    "  mid u_mid (.clk_a(clk_a), .clk_b(clk_b), .rst_ni(rst_ni), .d(d), .y(y));\n"
    "endmodule\n"
)
OUTER_EN = OUTER.replace("  input  logic rst_ni,\n", "  input  logic rst_ni,\n  input  logic en_i,\n").replace(
    ".rst_ni(rst_ni), .d(d)", ".rst_ni(rst_ni), .en_i(en_i), .d(d)"
)
INERT = {"leaf": ["en_i"]}
TIE = {"en_i": 0}


def mid(tmp_path, gate_mid: str = MID_EN, gate_leaf: str = LEAF_EN, gold_leaf: str = LEAF, **options):
    options.setdefault("tie", TIE)
    options.setdefault("inert", INERT)
    return run(tmp_path, (gold_leaf, MID), (gate_leaf, gate_mid), "mid", **options)


def test_the_set_up_is_what_the_tests_need():
    assert LEAF_EN != LEAF and "en_i ? '0 : q_r" in LEAF_EN
    assert MID_EN.count("en_i") == 4 and "y_pre = r" in MID_EN
    assert OUTER_EN.count("en_i") == 3


@needs_yosys
def test_without_inert_the_two_clocks_are_an_error(tmp_path):
    """The case this part is for: the child is flattened and the parent has two clocks."""
    result = mid(tmp_path, inert=None)
    assert result.status == "error"
    assert "clock" in result.detail


@needs_yosys
def test_an_inert_child_with_its_enable_tied_is_a_black_box(tmp_path):
    result = mid(tmp_path)
    assert result.status == "proved", result.detail


@needs_yosys
def test_a_constant_zero_on_the_pin_is_accepted(tmp_path):
    gate = MID_EN.replace(".en_i(en_i), .d(d)", ".en_i(1'b0), .d(d)")
    assert mid(tmp_path, gate_mid=gate).status == "proved"


@needs_yosys
@pytest.mark.parametrize(
    "pin",
    [".en_i(1'b1)", ".en_i(d)", ".en_i(r)", ".en_i(~en_i)", ".en_i(en_i | d)", ".en_i()"],
)
def test_a_pin_that_is_not_zero_is_never_proved(tmp_path, pin):
    gate = MID_EN.replace(".en_i(en_i), .d(d)", f"{pin}, .d(d)")
    assert gate != MID_EN
    result = mid(tmp_path, gate_mid=gate)
    assert result.status == "error", result.detail
    assert "u_leaf" in result.detail and "en_i" in result.detail


@needs_yosys
def test_a_pin_left_out_is_never_proved(tmp_path):
    gate = MID_EN.replace(".en_i(en_i), .d(d)", ".d(d)")
    result = mid(tmp_path, gate_mid=gate)
    assert result.status == "error", result.detail
    assert "u_leaf" in result.detail and "en_i" in result.detail


@needs_yosys
def test_an_enable_that_is_not_tied_is_never_proved(tmp_path):
    """The parent's enable is free: the pin of the child is not 0."""
    gate = MID_EN.replace("assign y = en_i ? 1'b1 : y_pre;", "assign y = y_pre;")
    result = mid(tmp_path, gate_mid=gate, tie={})
    assert result.status == "error", result.detail
    assert "u_leaf" in result.detail


@needs_yosys
def test_a_wrong_mux_in_the_parent_is_refuted(tmp_path):
    gate = MID_EN.replace("en_i ? 1'b1 : y_pre", "en_i ? y_pre : 1'b1")
    result = mid(tmp_path, gate_mid=gate)
    assert result.status == "refuted", result.detail
    assert "y" in result.detail


@needs_yosys
def test_a_changed_connection_of_the_inert_child_is_refuted(tmp_path):
    gate = MID_EN.replace(".d(d), .q(lq)", ".d(~d), .q(lq)")
    assert gate != MID_EN
    result = mid(tmp_path, gate_mid=gate)
    assert result.status == "refuted", result.detail
    assert "u_leaf" in result.detail


@needs_yosys
@pytest.mark.parametrize(
    "old, new",
    [
        ("  input  logic [W-1:0] d,\n", "  input  logic [W-1:0] d,\n  input  logic         extra_i,\n"),
        ("  output logic [W-1:0] q\n", "  output logic [W:0]   q\n"),
        ("  input  logic         en_i,\n", "  output logic         en_i,\n"),
    ],
)
def test_another_interface_of_the_child_is_an_error(tmp_path, old, new):
    gate_leaf = LEAF_EN.replace(old, new)
    assert gate_leaf != LEAF_EN
    result = mid(tmp_path, gate_leaf=gate_leaf)
    assert result.status == "error", result.detail


@needs_yosys
def test_the_named_input_must_be_new_and_must_be_there(tmp_path):
    result = mid(tmp_path / "a", gold_leaf=LEAF_EN, gate_mid=MID_EN)
    assert result.status == "error", result.detail
    result = mid(tmp_path / "b", inert={"leaf": ["other_i"]})
    assert result.status == "error", result.detail
    result = mid(tmp_path / "c", inert={"nothing": ["en_i"]})
    assert result.status != "proved", result.detail


@needs_yosys
def test_another_parameter_value_on_the_instance_is_an_error(tmp_path):
    gate = MID_EN.replace("leaf u_leaf (", "leaf #(.W(1)) u_leaf (")
    assert mid(tmp_path / "a", gate_mid=gate).status == "proved"
    wide = (
        "logic [1:0] lq2;\n"
        "  leaf #(.W(2)) u_leaf (.clk_b(clk_b), .rst_ni(rst_ni), .en_i(en_i), .d({d, d}), .q(lq2));\n"
        "  assign lq = lq2[0];"
    )
    gate = MID_EN.replace("leaf u_leaf (.clk_b(clk_b), .rst_ni(rst_ni), .en_i(en_i), .d(d), .q(lq));", wide)
    assert gate != MID_EN
    result = mid(tmp_path / "b", gate_mid=gate)
    assert result.status in ("error", "refuted"), result.detail


@needs_yosys
def test_same_and_inert_together_is_an_error(tmp_path):
    result = mid(tmp_path, same=["leaf"])
    assert result.status == "error", result.detail
    assert "leaf" in result.detail


@pytest.mark.parametrize("inert", [["leaf"], {"leaf": "en_i"}, {"leaf": [1]}, {1: ["en_i"]}])
def test_a_wrong_inert_raises(tmp_path, inert):
    with pytest.raises((TypeError, ValueError)):
        mid(tmp_path, inert=inert)


@needs_yosys
def test_an_inert_child_below_a_flattened_child(tmp_path):
    """`mid` is not a box here: it is flattened into `outer`, and `leaf` inside it stays a box."""
    gold = (LEAF, MID, OUTER)
    result = run(tmp_path / "a", gold, (LEAF_EN, MID_EN, OUTER_EN), "outer", tie=TIE, inert=INERT)
    assert result.status == "proved", result.detail
    wrong = MID_EN.replace(".en_i(en_i), .d(d)", ".en_i(1'b1), .d(d)")
    result = run(tmp_path / "b", gold, (LEAF_EN, wrong, OUTER_EN), "outer", tie=TIE, inert=INERT)
    assert result.status == "error", result.detail
    assert "u_leaf" in result.detail
    wrong = OUTER_EN.replace(".en_i(en_i), .d(d)", ".en_i(d), .d(d)")
    result = run(tmp_path / "c", gold, (LEAF_EN, MID_EN, wrong), "outer", tie=TIE, inert=INERT)
    assert result.status != "proved", result.detail


@needs_yosys
def test_two_levels_of_inert_children(tmp_path):
    """`mid` is proved too: in `outer` it is a box with its own enable, and `leaf` is not looked at."""
    gold = (LEAF, MID, OUTER)
    result = run(tmp_path / "a", gold, (LEAF_EN, MID_EN, OUTER_EN), "outer", tie=TIE, inert={"mid": ["en_i"]})
    assert result.status == "proved", result.detail
    wrong = OUTER_EN.replace(".en_i(en_i), .d(d)", ".en_i(1'b1), .d(d)")
    result = run(tmp_path / "b", gold, (LEAF_EN, MID_EN, wrong), "outer", tie=TIE, inert={"mid": ["en_i"]})
    assert result.status == "error", result.detail
    assert "u_mid" in result.detail
