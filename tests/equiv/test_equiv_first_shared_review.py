"""Flops of the same name start the same: what the review found (#139).

The review of rules I9 to I15 (test_equiv_first_shared.py) found a wrong "proved" that the change
itself made, and two wrong answers for a pair with the same text on both sides.

THE WRONG "PROVED"

The shared value is told to the solver as a constraint on the net of the flop: defined, and the
same on both sides. The net of a flop with an asynchronous load is its load value while the load
is active. In this gate the load value and the load condition of the shared flop `g` come from
two flops that only the gate has:

    gold:  always_ff @(posedge clk or posedge l) if (l) g <= d; else g <= e;     assign o = 1'b0;
    gate:  a has a reset and holds, c holds;  lt = l | a;  ad = a ? c : d;
           always_ff @(posedge clk or posedge lt) if (lt) g <= ad; else g <= e;  assign o = a;

When `a` is 1 the net of `g` is the undefined `c`, so "g is defined" cannot be met, and the solver
is left with `a` at 0 only. The guard finds a model, the proof passes, and `o = a` against 0 is
"proved". A constraint must never rule out a value that a flop can have in hardware.

RULES

I16  A flop is shared only when its net in the first step depends on nothing but its own state:
     a flop with no asynchronous pin, or a flop with an asynchronous reset to a constant, with the
     same polarity and the same constant on both sides. A flop with an asynchronous load of a net
     is never shared.
I17  Nothing is left to chance. A flop is shared, or it starts undefined (a flop with no
     asynchronous pin that one side has alone), or it is an unshared flop with an asynchronous
     reset to a constant, which the solver may start with any value. When a side has any other
     state, the first step runs for the whole pair as rule I1 had it: every flop undefined, no
     shared value, no constraint. Other state is: an asynchronous load of a net, a state cell of
     another kind, and a flop that has no name the solver can be given.
I18  A flop is one state cell. Its names are the nets that are exactly the output of that cell.
     A net that is made of bits of several flops, or of a part of one, names no flop and is
     ignored. It is no error.

STATED LIMITS

- In a pair that falls back to rule I1 (I17), a gate that adds a flop of its own to an output
  that the gold takes from a flop is a wrong "proved", as it was before rule I9. A module with a
  flop that loads a net asynchronously is such a pair for every edit. Pinned by a test.
"""

from __future__ import annotations

import pytest

from equiv_designs import run
from hdl_vet import equiv

pytestmark = [pytest.mark.equiv]

needs_yosys = pytest.mark.skipif(not equiv.available(), reason="Yosys (hdl-vet[equiv]) is not installed")

PORTS = (
    "input logic clk, input logic rst_n, input logic l, input logic d, input logic e, output logic q, output logic o"
)
OWN = "  logic a;\n  always_ff @(posedge clk) a <= a;\n"
LOAD = "  logic g;\n  always_ff @(posedge clk or posedge l) if (l) g <= d; else g <= e;\n"


def module(body: str) -> str:
    return f"module m ({PORTS});\n{body}endmodule\n"


def answer(tmp_path, gold: str, gate: str):
    return run(tmp_path, (gold,), (gate,), "m")


def refuted_in_the_first_state(result) -> None:
    assert result.status == "refuted", result
    assert "first state" in result.detail, result


# --- I16: a constraint must not rule out a value of another flop -------------------------------------

HELD = "  logic a;\n  always_ff @(posedge clk or negedge rst_n) if (!rst_n) a <= 1'b0; else a <= a;\n"
PINNED = (
    "  logic c, g, lt, ad;\n"
    + HELD
    + "  always_ff @(posedge clk) c <= c;\n"
    + "  assign lt = l | a;\n  assign ad = a ? c : d;\n"
    + "  always_ff @(posedge clk or posedge lt) if (lt) g <= ad; else g <= e;\n"
)


@needs_yosys
def test_a_flop_whose_start_value_a_shared_load_would_rule_out_is_refuted(tmp_path):
    gold = module(LOAD + "  assign q = g;\n  assign o = 1'b0;\n")
    gate = module(PINNED + "  assign q = g;\n  assign o = a;\n")
    result = answer(tmp_path, gold, gate)
    assert result.status in ("refuted", "error"), result


@needs_yosys
def test_the_same_hole_with_nothing_around_it_is_refuted(tmp_path):
    gold = module(LOAD + "  assign q = g;\n  assign o = 1'b0;\n")
    gate = module(LOAD + HELD + "  assign q = g;\n  assign o = a;\n")
    refuted_in_the_first_state(answer(tmp_path, gold, gate))


RESET = "  logic g;\n  always_ff @(posedge clk or posedge {reset}) if ({reset}) g <= 1'b0; else g <= e;\n"


@needs_yosys
def test_a_reset_of_a_shared_flop_that_comes_from_a_flop_of_the_gate_does_not_hide_it(tmp_path):
    """The reset of the shared flop `g` comes from the flop `a` of the gate. `a` at 1 must still be looked at."""
    gold = module(RESET.format(reset="l") + "  assign q = g;\n  assign o = 1'b0;\n")
    gate = module(
        HELD + "  logic lt;\n  assign lt = l | a;\n" + RESET.format(reset="lt") + "  assign q = g;\n  assign o = a;\n"
    )
    result = answer(tmp_path, gold, gate)
    assert result.status in ("refuted", "error"), result


# --- I17: what cannot be shared or named ------------------------------------------------------------


@needs_yosys
def test_a_flop_with_an_asynchronous_load_of_a_net_is_proved_against_itself(tmp_path):
    text = module(LOAD + "  assign q = g;\n  assign o = d;\n")
    assert answer(tmp_path, text, text).status == "proved"


@needs_yosys
@pytest.mark.parametrize("name", ["f", "json", "il", "\\a+b "])
def test_a_flop_with_a_name_that_is_hard_to_give_is_proved_against_itself(tmp_path, name):
    text = module(
        f"  logic {name};\n  always_ff @(posedge clk) {name} <= d;\n  assign q = {name} ^ e;\n  assign o = d;\n"
    )
    assert answer(tmp_path, text, text).status == "proved"


@needs_yosys
@pytest.mark.parametrize("name", ["f", "\\a+b "])
def test_a_hole_beside_a_flop_with_such_a_name_is_refuted(tmp_path, name):
    flop = f"  logic {name};\n  always_ff @(posedge clk) {name} <= d;\n"
    gold = module(flop + f"  assign q = {name} ^ e;\n  assign o = 1'b0;\n")
    gate = module(flop + OWN + f"  assign q = {name} ^ e;\n  assign o = a;\n")
    refuted_in_the_first_state(answer(tmp_path, gold, gate))


@needs_yosys
def test_in_a_module_with_a_load_of_a_net_the_old_limit_is_back(tmp_path):
    """Stated limit, a wrong "proved". When this test fails because the pair is refuted, the limit is gone."""
    shared = LOAD + "  logic h;\n  always_ff @(posedge clk) h <= d;\n"
    gold = module(shared + "  assign q = g;\n  assign o = h;\n")
    gate = module(shared + OWN + "  assign q = g;\n  assign o = h ^ a;\n")
    assert answer(tmp_path, gold, gate).status == "proved"


# --- I18: a flop is one state cell ------------------------------------------------------------------

MIXED = (
    "  logic [1:0] p;\n  logic g;\n"
    "  always_ff @(posedge clk) begin p <= {p[0], d}; g <= e; end\n"
    "  logic [1:0] w;\n  assign w = {p[0], g};\n"
)


@needs_yosys
def test_a_net_made_of_bits_of_two_flops_is_no_error(tmp_path):
    text = module(MIXED + "  assign q = ^w ^ p[1];\n  assign o = d;\n")
    assert answer(tmp_path, text, text).status == "proved"


@needs_yosys
def test_a_hole_beside_such_a_net_is_refuted(tmp_path):
    gold = module(MIXED + "  assign q = ^w ^ p[1];\n  assign o = d;\n")
    gate = module(MIXED + OWN + "  assign q = ^w ^ p[1] ^ a;\n  assign o = d;\n")
    refuted_in_the_first_state(answer(tmp_path, gold, gate))
