"""The first state of a flop (#139).

The proof is an induction. It starts from any state in which the two sides agree so far, so a
state of the gate that the gold cannot be in was never asked about. A flop has no defined value
before its first clock edge or its first reset. A gate that differs from the gold only there
was "proved". Measured on `main` at 4f52366, with the gold `assign q = 1'b0;`:

    always_ff @(posedge clk) a <= a;      assign q = a;    a flop that powers up at 1 gives 1 for ever
    always_ff @(posedge clk) a <= a & d;  assign q = a;    it gives 1 until d is 0 at an edge
    always_ff @(posedge clk) a <= 1'b0;   assign q = a;    it can give 1 until the first edge
    the same with an asynchronous reset of a to 0          it can give 1 until the first reset

Owner decision of 2026-11-27: the proof gets a first step, with one step, after a measurement
(on the issue). A cell that the first step has no model for: `$specify2` and `$print` are taken
out, any other is an "error".

RULES

I1  After a proof that is "proved", the engine runs a first step on the same prepared pair. Every
    flop of both sides starts with an undefined value. Every input is defined. The outputs are
    compared in that one step by the rule the engine has for undefined values: where the gold is
    undefined the gate may have any value, and where the gold is defined the gate must have the
    same value. An undefined value of the gate there is a difference.
I2  A pair that fails the first step is "refuted", and the detail has the words "first state".
    The four pairs above are "refuted".
I3  A flop with no reset is normal, and it does not fail the first step when the gold has the
    same undefined value at the same output: the same flop on both sides, a flop with another
    name, the logic in front of a flop written in another way, a flop with an asynchronous reset.
    A gate that masks a flop away (`(a & 1'b0) | d`, `a ? d : d`) is "proved" against `d`.
I4  A pair that the induction refutes keeps its answer and its detail. The first step is not run
    for it.
I5  A specify block and a `$display` are not logic. They are taken out for the first step, and a
    harmless edit beside one stays "proved".
I6  When the first step cannot be run for another reason (a cell that it has no model for, a
    failed run), the answer is "error", and the detail starts with "the first state could not be
    checked: ". It is never "proved".

STATED LIMITS

- The first step is one step. A gate that differs from the gold only in a later step, from a
  state that the induction does not ask about, can still be a wrong "proved". No such pair is
  known: the one that was tried (the last test of this file) is refuted by the induction.
- An undefined value does not cancel in the first step. `a ^ a` with a flop `a` that has no reset
  is 0 in hardware and undefined in the model. A gate `(a ^ a) | d` against the gold `d` is
  "refuted", and that is wrong. Write the edit without the flop, or give a waiver.
- I6 is not shown by a test. No construct was found that passes every check before the proof,
  is "proved" by the induction, and has a cell with no model other than the two of I5.
- The first step costs time: about 4 seconds on the large generated design, under 0.1 second
  on a small module. The times are from one Windows machine.
- A design with a memory was not measured.
"""

from __future__ import annotations

import pytest

from equiv_designs import run
from hdl_vet import equiv

pytestmark = [pytest.mark.equiv]

needs_yosys = pytest.mark.skipif(not equiv.available(), reason="Yosys (hdl-vet[equiv]) is not installed")

PORTS = "input logic clk, input logic rst_n, input logic d, output logic q"
FF = "  logic a;\n"


def module(body: str) -> str:
    return f"module m ({PORTS});\n{body}endmodule\n"


def answer(tmp_path, gold: str, gate: str):
    return run(tmp_path, (gold,), (gate,), "m")


ZERO = module("  assign q = 1'b0;\n")
PLAIN = module(FF + "  always_ff @(posedge clk) a <= d;\n  assign q = a;\n")
WIRE = module(FF + "  always_ff @(posedge clk) a <= d;\n  assign q = d;\n")
ARST = "  always_ff @(posedge clk or negedge rst_n) if (!rst_n) a <= 1'b0; else a <= {value};\n  assign q = a;\n"

# --- I1, I2: the four pairs of the issue -------------------------------------------------------------

HOLES = {
    "holds-for-ever": FF + "  always_ff @(posedge clk) a <= a;\n  assign q = a;\n",
    "holds-until-d-is-0": FF + "  always_ff @(posedge clk) a <= a & d;\n  assign q = a;\n",
    "until-the-first-edge": FF + "  always_ff @(posedge clk) a <= 1'b0;\n  assign q = a;\n",
    "until-the-first-reset": FF + ARST.format(value="1'b0"),
}


@needs_yosys
@pytest.mark.parametrize("hole", HOLES)
def test_a_gate_that_differs_only_in_its_first_state_is_refuted(tmp_path, hole):
    result = answer(tmp_path, ZERO, module(HOLES[hole]))
    assert result.status == "refuted", result
    assert "first state" in result.detail, result


@needs_yosys
def test_a_gate_that_is_defined_where_the_gold_is_not_is_proved(tmp_path):
    """I1: the rule has a direction. The gold of a hole against the constant is the free choice of the gate."""
    result = answer(tmp_path, module(HOLES["until-the-first-edge"]), ZERO)
    assert result.status == "proved", result


# --- I3: a flop with no reset is normal --------------------------------------------------------------

RIGHT = {
    "the-same-flop": (PLAIN, PLAIN),
    "a-renamed-flop": (PLAIN, module("  logic b;\n  always_ff @(posedge clk) b <= d;\n  assign q = b;\n")),
    "the-logic-in-front-rewritten": (
        module(FF + "  always_ff @(posedge clk) a <= d & rst_n;\n  assign q = a;\n"),
        module(FF + "  always_ff @(posedge clk) a <= ~(~d | ~rst_n);\n  assign q = a;\n"),
    ),
    "an-asynchronous-reset": (module(FF + ARST.format(value="d")), module(FF + ARST.format(value="d"))),
    "a-masked-flop": (WIRE, module(FF + "  always_ff @(posedge clk) a <= d;\n  assign q = (a & 1'b0) | d;\n")),
    "a-mux-with-equal-arms": (WIRE, module(FF + "  always_ff @(posedge clk) a <= d;\n  assign q = a ? d : d;\n")),
}


@needs_yosys
@pytest.mark.parametrize("pair", RIGHT)
def test_a_right_pair_with_a_flop_that_has_no_reset_is_proved(tmp_path, pair):
    gold, gate = RIGHT[pair]
    result = answer(tmp_path, gold, gate)
    assert result.status == "proved", result


# --- I4: the induction answers first -----------------------------------------------------------------


@needs_yosys
def test_a_pair_that_the_induction_refutes_keeps_its_detail(tmp_path):
    result = answer(tmp_path, PLAIN, module(FF + "  always_ff @(posedge clk) a <= ~d;\n  assign q = a;\n"))
    assert result.status == "refuted", result
    assert "first state" not in result.detail, result
    assert "not proven equal" in result.detail, result


# --- I5: what is no logic ----------------------------------------------------------------------------

NO_LOGIC = {
    "specify": "  specify\n    (d => q) = 1;\n  endspecify\n",
    "display": '  always @(posedge clk) $display("one");\n',
}


@needs_yosys
@pytest.mark.parametrize("construct", NO_LOGIC)
def test_a_harmless_edit_beside_what_is_no_logic_is_proved(tmp_path, construct):
    gold = module("  assign q = d;\n" + NO_LOGIC[construct])
    gate = module("  assign q = d & 1'b1;\n" + NO_LOGIC[construct])
    result = answer(tmp_path, gold, gate)
    assert result.status == "proved", result


@needs_yosys
@pytest.mark.parametrize("construct", NO_LOGIC)
def test_a_hole_beside_what_is_no_logic_is_refuted(tmp_path, construct):
    """Taking the cell out must not take the check out."""
    gate = module(HOLES["holds-for-ever"] + NO_LOGIC[construct])
    result = answer(tmp_path, module("  assign q = 1'b0;\n" + NO_LOGIC[construct]), gate)
    assert result.status == "refuted", result
    assert "first state" in result.detail, result


# --- the stated limits -------------------------------------------------------------------------------


@needs_yosys
def test_an_undefined_value_that_cancels_is_refuted_although_it_is_right(tmp_path):
    """Stated limit: `a ^ a` is 0 in hardware and undefined in the model of the first step."""
    gate = module(FF + "  always_ff @(posedge clk) a <= d;\n  assign q = (a ^ a) | d;\n")
    result = answer(tmp_path, WIRE, gate)
    assert result.status == "refuted", result
    assert "first state" in result.detail, result


@needs_yosys
def test_a_gate_that_differs_after_a_reset_is_never_proved(tmp_path):
    """One step does not see this pair. The induction refutes it."""
    reset = "  logic g;\n  always_ff @(posedge clk) if (!rst_n) g <= 1'b0; else g <= d;\n"
    gold = module(reset + "  assign q = g;\n")
    gate = module(reset + FF + "  always_ff @(posedge clk) a <= a;\n  assign q = g | a;\n")
    result = answer(tmp_path, gold, gate)
    assert result.status in ("refuted", "error"), result
