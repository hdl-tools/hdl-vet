"""A combinational loop (#143).

The engine answered "proved" for an edit that writes a combinational loop which can hold more
than one value. Measured on `main` at the time of #41, with the gold `assign q = d;`:

    logic lp;
    assign lp = d & lp;
    assign q = lp;

`lp = d & lp` has two answers when `d` is 1. Hardware that settles at 0 keeps `q` at 0 for ever.
The proof takes an answer of the loop that agrees with the gold.

Owner decision of 2026-11-27: a check of the netlist before the proof. A loop on either side is
an "error".

RULES

Q1  Before the proof the engine looks for a combinational loop in the netlist of each side, after
    the ties and the clean-up. A loop is a path from an output bit of a cell back to an input bit
    of the same cell that passes no flop.
Q2  A loop on either side is an "error". The detail says "combinational loop", says which side
    has it (gold or gate), and names a net of the loop when the netlist has a name for one.
Q3  It is an "error" also when both sides have the same loop, and when only the gold has it.
Q4  The check follows bits, not whole cells, through the cells that work bit by bit (`&`, `|`,
    `^`, `~`, a mux). A vector whose bit i is made from its bit i-1 is no loop.
Q5  Feedback through a flop is no loop.
Q6  A child that is flattened into the comparison is looked through. A child that stays a black
    box (`same=`) cuts the path: its output does not count as made from its inputs.

STATED LIMITS

- A design that has a combinational loop on purpose (a ring oscillator, a latch made of gates)
  cannot be proved. Every edit of such a module is an "error" and needs a waiver.
- A loop through a child that is a black box is not seen. The child itself is compared on its
  own, and the loop shows when the parent is compared with the child flattened.
- Through a cell that does not work bit by bit (an adder, a comparison, a shift) the check
  takes every output bit as made from every input bit. A vector that feeds its own lower bits
  into such a cell is an "error" although it is no loop.
- A loop that the clean-up removes, because nothing reads it, is not reported.
"""

from __future__ import annotations

import pytest

from equiv_designs import run
from hdl_vet import equiv

pytestmark = [pytest.mark.equiv]

needs_yosys = pytest.mark.skipif(not equiv.available(), reason="Yosys (hdl-vet[equiv]) is not installed")

PORTS = "input logic clk, input logic d, input logic e, output logic q"
CHILD = "module c (input logic a, input logic b, output logic y);\n  assign y = a & b;\nendmodule\n"
REG = "module r (input logic clk, input logic a, output logic y);\n  always_ff @(posedge clk) y <= a;\nendmodule\n"


def module(body: str) -> str:
    return f"module m ({PORTS});\n{body}endmodule\n"


def answer(tmp_path, gold: str, gate: str, **options):
    return run(tmp_path, (gold,), (gate,), "m", **options)


WIRE = module("  assign q = d;\n")

# --- Q1, Q2: a loop in the gate ---------------------------------------------------------------------

LOOPS = {
    "the-issue": ("  logic lp;\n  assign lp = d & lp;\n  assign q = lp;\n", "lp"),
    "two-nets": ("  logic x, y;\n  assign x = d & y;\n  assign y = x | e;\n  assign q = x;\n", None),
    "a-mux": ("  logic mx;\n  assign mx = e ? d : mx;\n  assign q = mx;\n", "mx"),
    "bits-of-a-vector": (
        "  logic [1:0] v;\n  assign v[0] = d & v[1];\n  assign v[1] = v[0] | e;\n  assign q = v[0];\n",
        "v",
    ),
    "no-answer": ("  logic lp;\n  assign lp = d ^ lp;\n  assign q = lp;\n", "lp"),
}


@needs_yosys
@pytest.mark.parametrize("loop", LOOPS)
def test_a_loop_in_the_gate_is_an_error(tmp_path, loop):
    body, net = LOOPS[loop]
    result = answer(tmp_path, WIRE, module(body))
    assert result.status == "error", result
    assert "combinational loop" in result.detail and "gate" in result.detail, result
    if net is not None:
        assert net in result.detail, result


@needs_yosys
def test_a_loop_through_a_flattened_child_is_an_error(tmp_path):
    gate = CHILD + module("  logic w;\n  c u (.a(d), .b(w), .y(w));\n  assign q = w;\n")
    result = answer(tmp_path, CHILD + WIRE, gate)
    assert result.status == "error", result
    assert "combinational loop" in result.detail and "gate" in result.detail, result


# --- Q3 ---------------------------------------------------------------------------------------------


@needs_yosys
def test_the_same_loop_on_both_sides_is_an_error(tmp_path):
    text = module(LOOPS["the-issue"][0])
    result = answer(tmp_path, text, text)
    assert result.status == "error", result
    assert "combinational loop" in result.detail, result


@needs_yosys
def test_a_loop_in_the_gold_alone_is_an_error(tmp_path):
    result = answer(tmp_path, module(LOOPS["the-issue"][0]), WIRE)
    assert result.status == "error", result
    assert "combinational loop" in result.detail and "gold" in result.detail, result


# --- Q4, Q5, Q6: what is no loop --------------------------------------------------------------------

NO_LOOP = {
    "feedback-through-a-flop": "  logic a;\n  always_ff @(posedge clk) a <= a ^ d;\n  assign q = a;\n",
    "a-chain-in-one-vector": "  logic [3:0] v;\n  assign v = {v[2:0] & {3{e}}, d};\n  assign q = v[3];\n",
    "a-chain-through-a-mux": "  logic [3:0] v;\n  assign v = e ? {v[2:0], d} : 4'b0;\n  assign q = v[3];\n",
    "gray-to-binary": (
        "  logic [3:0] g, b;\n  assign g = {d, e, d ^ e, d};\n"
        "  always_comb begin\n    b[3] = g[3];\n    for (int i = 2; i >= 0; i--) b[i] = b[i+1] ^ g[i];\n  end\n"
        "  assign q = b[0];\n"
    ),
}


@needs_yosys
@pytest.mark.parametrize("shape", NO_LOOP)
def test_what_is_no_loop_is_proved(tmp_path, shape):
    text = module(NO_LOOP[shape])
    result = answer(tmp_path, text, text)
    assert result.status == "proved", result


BOXED = REG + module("  logic w;\n  r u (.clk(clk), .a(w ^ d), .y(w));\n  assign q = w;\n")


@needs_yosys
@pytest.mark.parametrize("same", [(), ("r",)])
def test_feedback_around_a_child_with_a_flop_is_no_loop(tmp_path, same):
    result = answer(tmp_path, BOXED, BOXED, same=same)
    assert result.status == "proved", result


# --- the stated limits ------------------------------------------------------------------------------


@needs_yosys
def test_a_loop_through_a_black_box_is_not_seen(tmp_path):
    """Stated limit. The child is the same on both sides, and the parent is the same text."""
    text = CHILD + module("  logic w;\n  c u (.a(d), .b(w), .y(w));\n  assign q = w;\n")
    result = answer(tmp_path, text, text, same=("c",))
    assert result.status == "proved", result


@needs_yosys
def test_a_vector_through_an_adder_is_an_error_although_it_is_no_loop(tmp_path):
    """Stated limit: bit i of the sum is made from the bits below it only."""
    text = module("  logic [3:0] s;\n  assign s = {s[2:0], d} + 4'd1;\n  assign q = s[3];\n")
    result = answer(tmp_path, text, text)
    assert result.status == "error", result
    assert "combinational loop" in result.detail, result
