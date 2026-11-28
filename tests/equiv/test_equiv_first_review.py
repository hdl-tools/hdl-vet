"""The first state of a flop: what the review found (#139).

The review of the first step (rules I1 to I6 of test_equiv_first.py) found no new wrong "proved".
It found a new "error" on right pairs, and it found pairs for a limit that was only a guess.

The new "error": the first step compares the two sides as one problem, with no matched nets in
between. The induction matches the nets of the same name and proves piece by piece. A module
with an adder in front of a multiplier of 12 bits, the same text on both sides, was "proved" in
0.6 seconds and ran into the time limit in the first step.

RULES

I7  The first step is not run when neither side has a flop or another cell that keeps state.
    There is no first state then, and the answer of the induction stands.
I8  Before the comparison of the first step, logic that is the same on both sides and reads the
    same nets is shared. A pair with wide arithmetic that the edit does not touch costs about as
    much as before the first step existed. The holes of I2 are still "refuted".

A LIMIT THAT IS GONE

- With every flop undefined in the first step, a gate `q = g ^ a` against the gold `q = g`, with a
  flop `g` and a flop `a <= a` that has no reset, was a wrong "proved": the gold was undefined in
  the first step, so the gate could have any value, and the induction never asks about a state in
  which the two differ at every step. Since rule I9 (test_equiv_first_shared.py) the flop `g`
  starts with one shared value, and the pair is "refuted". The three pairs that pinned the limit
  here assert that now.

STATED LIMITS (the first is shown by a test)

- An undefined value does not cancel in the first step, also in a mux written with gates:
  `(s & d) | (~s & d)` with a flop `s` is "refuted" against `d`, and that is wrong.
- I8 shares only what is the same. A pair with flops, whose edit rewrites arithmetic that feeds
  a wide multiplier on a path with no flop to an output, can still reach the time limit. The
  answer is then "error", and the detail says "timed out".
"""

from __future__ import annotations

import pytest

from equiv_designs import run
from hdl_vet import equiv

pytestmark = [pytest.mark.equiv]

needs_yosys = pytest.mark.skipif(not equiv.available(), reason="Yosys (hdl-vet[equiv]) is not installed")

WIDE = "input logic clk, input logic [11:0] a, b, c, input logic d, output logic [23:0] q, output logic o"
SUM = "  logic [11:0] t;\n  assign t = a + b;\n"
REWRITTEN = "  logic [11:0] t;\n  assign t = (a ^ b) + ((a & b) << 1);\n"
PRODUCT = "  assign q = t * c;\n"
STAGE = "  always_ff @(posedge clk) q <= t * c;\n"
HOLE = "  logic h;\n  always_ff @(posedge clk) h <= h;\n  assign o = h;\n"


def wide(body: str) -> str:
    return f"module m ({WIDE});\n{body}endmodule\n"


def answer(tmp_path, gold: str, gate: str):
    # The bound shows the rule: without it each of these pairs ran for more than 120 seconds.
    return run(tmp_path, (gold,), (gate,), "m", timeout=60)


# --- I7: no flop, no first state ---------------------------------------------------------------------

NO_FLOP = {
    "the-same-text": (SUM + PRODUCT, SUM + PRODUCT),
    "the-product-rewritten": (SUM + PRODUCT, SUM + "  assign q = (t * c) | 24'd0;\n"),
    "the-sum-rewritten": (SUM + PRODUCT, REWRITTEN + PRODUCT),
}


@needs_yosys
@pytest.mark.parametrize("pair", NO_FLOP)
def test_a_pair_with_no_flop_is_answered_by_the_induction(tmp_path, pair):
    gold, gate = NO_FLOP[pair]
    tail = "  assign o = d;\n"
    result = answer(tmp_path, wide(gold + tail), wide(gate + tail))
    assert result.status == "proved", result
    assert result.seconds < 30, result


# --- I8: what is the same on both sides is shared ----------------------------------------------------


@needs_yosys
def test_wide_arithmetic_beside_a_flop_is_proved_in_time(tmp_path):
    """A flop elsewhere in the module, so I7 does not apply: the product goes to an output with no flop."""
    body = SUM + PRODUCT + "  logic s;\n  always_ff @(posedge clk) s <= d;\n  assign o = s;\n"
    result = answer(tmp_path, wide(body), wide(body))
    assert result.status == "proved", result
    assert result.seconds < 30, result


@needs_yosys
def test_wide_arithmetic_in_front_of_a_flop_is_proved_in_time(tmp_path):
    result = answer(tmp_path, wide(SUM + STAGE + "  assign o = d;\n"), wide(REWRITTEN + STAGE + "  assign o = d;\n"))
    assert result.status == "proved", result
    assert result.seconds < 30, result


@needs_yosys
def test_a_hole_beside_wide_arithmetic_is_refuted(tmp_path):
    """Sharing must not take the check out."""
    result = answer(tmp_path, wide(SUM + PRODUCT + "  assign o = 1'b0;\n"), wide(SUM + PRODUCT + HOLE))
    assert result.status == "refuted", result
    assert "first state" in result.detail, result


@needs_yosys
def test_a_hole_in_a_flop_that_both_sides_have_is_refuted(tmp_path):
    """The two sides have the same flop `h`. The gold masks it, the gate does not."""
    flop = "  logic h;\n  always_ff @(posedge clk) h <= h;\n"
    gold = wide(SUM + PRODUCT + flop + "  assign o = h & 1'b0;\n")
    gate = wide(SUM + PRODUCT + flop + "  assign o = h;\n")
    result = answer(tmp_path, gold, gate)
    assert result.status == "refuted", result


HOLDS = (
    "module holds (input logic clk, output logic y);\n"
    "  logic a;\n  always_ff @(posedge clk) a <= a;\n  assign y = a;\nendmodule\n"
)


@needs_yosys
def test_a_hole_in_a_flattened_child_is_refuted(tmp_path):
    """I7 looks at the flattened netlist: the parent has no flop of its own."""
    ports = "input logic clk, input logic d, output logic q"
    gold = HOLDS + f"module m ({ports});\n  assign q = 1'b0;\nendmodule\n"
    gate = HOLDS + f"module m ({ports});\n  holds u (.clk(clk), .y(q));\nendmodule\n"
    result = run(tmp_path, (gold,), (gate,), "m")
    assert result.status == "refuted", result
    assert "first state" in result.detail, result


# --- the stated limits -------------------------------------------------------------------------------

PORTS = "input logic clk, input logic rst_n, input logic d, input logic e, output logic q"
LATER = {
    "a-flop-with-no-reset": "  always_ff @(posedge clk) g <= d;\n",
    "a-synchronous-reset": "  always_ff @(posedge clk) if (!rst_n) g <= 1'b0; else g <= d;\n",
    "an-enable": "  always_ff @(posedge clk) if (e) g <= d;\n",
}


@needs_yosys
@pytest.mark.parametrize("flop", LATER)
def test_a_gate_that_differs_from_the_second_step_on_is_refuted(tmp_path, flop):
    """This was a stated limit, a wrong "proved", until rule I9."""
    shared = "  logic g;\n" + LATER[flop]
    gold = f"module m ({PORTS});\n{shared}  assign q = g;\nendmodule\n"
    gate = (
        f"module m ({PORTS});\n{shared}  logic a;\n  always_ff @(posedge clk) a <= a;\n  assign q = g ^ a;\nendmodule\n"
    )
    result = run(tmp_path, (gold,), (gate,), "m")
    assert result.status == "refuted", result
    assert "first state" in result.detail, result


@needs_yosys
def test_a_mux_of_gates_with_equal_arms_is_refuted_although_it_is_right(tmp_path):
    """Stated limit."""
    gold = f"module m ({PORTS});\n  assign q = d;\nendmodule\n"
    gate = (
        f"module m ({PORTS});\n  logic s;\n  always_ff @(posedge clk) s <= e;\n"
        "  assign q = (s & d) | (~s & d);\nendmodule\n"
    )
    result = run(tmp_path, (gold,), (gate,), "m")
    assert result.status == "refuted", result
    assert "first state" in result.detail, result
