"""A reset pulse between two clock edges (#137).

The proof stepped every flop at every step. An asynchronous reset in a step also changed what the
other flops sampled in that step. Real hardware has a second kind of step: a reset pulse with no
clock edge. The flops with that reset change, and every other flop holds. The model had no such
step, so a state that only such a step reaches was never compared, and an edit that differs only
there was "proved". Measured on `main` at 4f52366, each of these is "proved" and differs:

    one plain asynchronous reset
        shared: A has the reset r and takes 1 at each edge; S samples A at each edge
        gold:   assign qa = A;
        gate:   assign qa = A | (S & ~A & ~r);
        With A and S at 1, r pulses between two edges: A is 0, S stays 1. Gold 0, gate 1.

    an asynchronous reset made synchronous, with the output masked
        gold:   always_ff @(posedge clk or negedge r) if (!r) q <= 1'b0; else q <= d;
        gate:   a flop s with a synchronous reset, and assign q = r & s;

    an asynchronous load, and two flops with different resets (the pairs further down)

A first try refused shapes from the netlist (an input that is a reset and also data; a load of a
value that is no constant). Three reviews each found a pair that those refusals let through, the
last with one plain reset and nothing else. So the hole is in the model, and no refusal of a
shape closes it.

Owner decision of 2026-11-27: change the model.

RULES

Z1  The proof has a step that is no clock edge. In such a step every flop holds its value, and an
    asynchronous reset, set or load still acts. The inputs can change in every step.
Z2  So a pair that differs only in what a pulse between two clock edges does is never "proved".
Z3  Nothing is refused for its shape. A module that samples its own asynchronous reset, and a
    register with an asynchronous preset to an input, are compared: the same text is "proved",
    and a changed function is never "proved".
Z4  What was compared before is compared as before: a reset through logic, a reset to an output
    port, a reset into a child that stays a black box, a reset synchroniser, synchronous resets
    only, and a child that is flattened into the comparison.
Z5  A clock through an inverter is the other edge of the input and is compared. The same edge is
    "proved". Another edge is never "proved".
Z6  Flops with different asynchronous resets are compared. The same resets are "proved". Resets
    that are swapped between two flops are never "proved".

The review of the new model found the one thing it cannot judge:

Z7  A flop that reads its clock as data. The model takes the data of a flop from the step before
    the edge, where the clock still has its old value. In hardware the data changes with the
    edge itself, so the value is not defined. When the clock input reaches a data pin of a flop
    (data, enable or synchronous reset, through any logic), on either side, the answer is
    "error", and the detail says that the clock is read as data. The clock in logic that only
    reaches an output port, and the clock as the asynchronous reset of a flop, are compared.

STATED LIMITS

- Z7 does not ask what the edit touches. A module with a flop that samples its own clock gives
  "error" for every logic edit. Such an edit needs a waiver.
- The proof is slower for a memory with an asynchronous reset. 16 words of 8 bits take about
  10 seconds, 64 words about two minutes, and more can reach the time limit, which is "error".
- The first state of a flop was not compared when this was written. Since #139 the proof has a
  first step for it: see tests/equiv/test_equiv_first.py.

- A step of the proof is now half a clock period or a pulse, and the proof looks a fixed number
  of steps back. A rewrite that needs more of them than before can be "refuted" where it was
  "proved". The limit on a renamed chain of registers in tests/equiv/test_equiv_review.py says
  how far that goes.
- The clock is an input like the others in the proof. A glitch on the clock itself is not what
  this is about: the clock check before the proof still asks for one clock from an input port.
"""

from __future__ import annotations

import pytest

from equiv_designs import run
from hdl_vet import equiv

pytestmark = [pytest.mark.equiv]

needs_yosys = pytest.mark.skipif(not equiv.available(), reason="Yosys (hdl-vet[equiv]) is not installed")

PORTS = "input logic clk, input logic r, input logic r2, input logic d, input logic d2, output logic q, output logic q2"


def module(body: str, ports: str = PORTS) -> str:
    return f"module m ({ports});\n{body}endmodule\n"


def answer(tmp_path, gold: str, gate: str, **options):
    return run(tmp_path, (gold,), (gate,), "m", **options)


# --- Z1, Z2: a pulse between two edges ------------------------------------------------------------

TAIL = "  assign q2 = d2;\n"
ASYNC = module("  always_ff @(posedge clk or negedge r) if (!r) q <= 1'b0; else q <= d;\n" + TAIL)
MASKED = module("  logic s;\n  always_ff @(posedge clk) if (!r) s <= 1'b0; else s <= d;\n  assign q = r & s;\n" + TAIL)
ASYNC_HIGH = module("  always_ff @(posedge clk or posedge r) if (r) q <= 1'b0; else q <= d;\n" + TAIL)
MASKED_HIGH = module(
    "  logic s;\n  always_ff @(posedge clk) if (r) s <= 1'b0; else s <= d;\n  assign q = ~r & s;\n" + TAIL
)
ASYNC_SET = module("  always_ff @(posedge clk or negedge r) if (!r) q <= 1'b1; else q <= d;\n" + TAIL)
MASKED_SET = module(
    "  logic s;\n  always_ff @(posedge clk) if (!r) s <= 1'b1; else s <= d;\n  assign q = ~r | s;\n" + TAIL
)
OTHER = "  always_ff @(posedge clk or negedge r2) if (!r2) q2 <= 1'b0; else q2 <= d2;\n"
ASYNC_TWO = module("  always_ff @(posedge clk or negedge r) if (!r) q <= 1'b0; else q <= d;\n" + OTHER)
MASKED_TWO = module(
    "  logic s;\n  always_ff @(posedge clk) if (!r) s <= 1'b0; else s <= d;\n  assign q = r & s;\n" + OTHER
)

# One plain reset. With A and S at 1, a pulse of r clears A and leaves S.
APORTS = "input logic clk, input logic r, output logic qa, output logic qs"
ONE = (
    "  logic A, S;\n"
    "  always_ff @(posedge clk or posedge r) if (r) A <= 1'b0; else A <= 1'b1;\n"
    "  always_ff @(posedge clk) S <= A;\n"
    "  assign qs = S;\n"
)
ONE_GOLD = module(ONE + "  assign qa = A;\n", APORTS)
ONE_GATE = module(ONE + "  assign qa = A | (S & ~A & ~r);\n", APORTS)

# Two flops with different resets of the same inputs. x pulses with l low, then l pulses with x low.
XPORTS = "input logic clk, input logic x, input logic l, output logic qa, output logic qb"
TWO = (
    "  logic A, B, ra;\n  assign ra = x & ~l;\n"
    "  always_ff @(posedge clk or posedge ra) if (ra) A <= 1'b1; else A <= 1'b0;\n"
    "  always_ff @(posedge clk or posedge l) if (l) B <= 1'b0; else B <= 1'b1;\n"
    "  assign qb = B;\n"
)
TWO_GOLD = module(TWO + "  assign qa = A;\n", XPORTS)
TWO_GATE = module(TWO + "  assign qa = A & (B | ra | l);\n", XPORTS)

# An asynchronous load. The pulse of l loads the new d in the gold. In the gate s keeps the old one.
LPORTS = "input logic clk, input logic l, input logic d, input logic a, output logic q"
LOAD = module("  always_ff @(posedge clk or posedge l) if (l) q <= d; else q <= d;\n", LPORTS)
LOAD_GATE = module("  logic s;\n  always_ff @(posedge clk) s <= d;\n  assign q = l ? d : s;\n", LPORTS)
LOAD_XOR = module("  always_ff @(posedge clk or posedge l) if (l) q <= d ^ a; else q <= a ^ d;\n", LPORTS)
LOAD_XOR_GATE = module("  logic s;\n  always_ff @(posedge clk) s <= d ^ a;\n  assign q = l ? (a ^ d) : s;\n", LPORTS)
# The input x is the set of A and the load value of B.
SET_AND_VALUE = (
    "  logic A, B;\n"
    "  always_ff @(posedge clk or posedge x) if (x) A <= 1'b1; else A <= 1'b0;\n"
    "  always_ff @(posedge clk or posedge l) if (l) B <= x; else B <= 1'b1;\n"
    "  assign qb = B;\n"
)
SET_GOLD = module(SET_AND_VALUE + "  assign qa = A;\n", XPORTS)
SET_GATE = module(SET_AND_VALUE + "  assign qa = A & (B | x | l);\n", XPORTS)
# The load value comes from a flop. After an edge p is new and s is old.
FLOP_VALUE = module(
    "  logic p;\n  always_ff @(posedge clk) p <= d;\n"
    "  always_ff @(posedge clk or posedge l) if (l) q <= p; else q <= p;\n",
    LPORTS,
)
FLOP_VALUE_GATE = module(
    "  logic p, s;\n  always_ff @(posedge clk) p <= d;\n  always_ff @(posedge clk) s <= p;\n  assign q = l ? p : s;\n",
    LPORTS,
)

PULSE_PAIRS = {
    "one-reset": (ONE_GOLD, ONE_GATE),
    "masked-low": (ASYNC, MASKED),
    "masked-high": (ASYNC_HIGH, MASKED_HIGH),
    "masked-set": (ASYNC_SET, MASKED_SET),
    "masked-beside-another-reset": (ASYNC_TWO, MASKED_TWO),
    "masked-reversed": (MASKED, ASYNC),
    "two-resets-of-the-same-inputs": (TWO_GOLD, TWO_GATE),
    "load": (LOAD, LOAD_GATE),
    "load-xor": (LOAD_XOR, LOAD_XOR_GATE),
    "load-reversed": (LOAD_GATE, LOAD),
    "set-and-load-value": (SET_GOLD, SET_GATE),
    "load-value-from-a-flop": (FLOP_VALUE, FLOP_VALUE_GATE),
    "load-value-from-a-flop-reversed": (FLOP_VALUE_GATE, FLOP_VALUE),
}


@needs_yosys
@pytest.mark.parametrize("pair", PULSE_PAIRS)
def test_a_pair_that_differs_for_a_pulse_between_two_edges_is_never_proved(tmp_path, pair):
    gold, gate = PULSE_PAIRS[pair]
    result = answer(tmp_path, gold, gate)
    assert result.status in ("refuted", "error"), result


@needs_yosys
@pytest.mark.parametrize("pair", ["one-reset", "masked-low", "two-resets-of-the-same-inputs"])
def test_such_a_pair_is_refuted_and_not_refused(tmp_path, pair):
    """The engine can judge these shapes now, so the answer is the difference and no refusal."""
    gold, gate = PULSE_PAIRS[pair]
    result = answer(tmp_path, gold, gate)
    assert result.status == "refuted", result


# --- Z3: nothing is refused for its shape ---------------------------------------------------------

USES = {
    "data": "  logic e;\n  always_ff @(posedge clk) e <= r;\n  assign q2 = e ^ d2;\n",
    "enable": "  logic e;\n  always_ff @(posedge clk) if (r) e <= d2;\n  assign q2 = e;\n",
    "synchronous-reset": "  logic e;\n  always_ff @(posedge clk) if (!r) e <= 1'b0; else e <= d2;\n  assign q2 = e;\n",
    "through-logic": "  logic e;\n  always_ff @(posedge clk) e <= d2 & ~(r ^ d);\n  assign q2 = e;\n",
}
# The load value a reaches no data pin: a register with a preset to a strap.
LOAD_OTHER = module("  always_ff @(posedge clk or posedge l) if (l) q <= a; else q <= d;\n", LPORTS)


@needs_yosys
@pytest.mark.parametrize("use", USES)
def test_a_module_that_samples_its_asynchronous_reset_is_compared(tmp_path, use):
    text = module("  always_ff @(posedge clk or negedge r) if (!r) q <= 1'b0; else q <= d;\n" + USES[use])
    assert answer(tmp_path / "same", text, text).status == "proved"
    other = answer(tmp_path / "other", text, text.replace("q <= d;", "q <= ~d;"))
    assert other.status == "refuted", other


@needs_yosys
def test_a_register_with_an_asynchronous_preset_to_an_input_is_compared(tmp_path):
    assert answer(tmp_path / "same", LOAD_OTHER, LOAD_OTHER).status == "proved"
    other = answer(tmp_path / "other", LOAD_OTHER, LOAD_OTHER.replace("else q <= d;", "else q <= ~d;"))
    assert other.status == "refuted", other
    value = answer(tmp_path / "value", LOAD_OTHER, LOAD_OTHER.replace("if (l) q <= a;", "if (l) q <= ~a;"))
    assert value.status == "refuted", value


# --- Z4: what was compared before -----------------------------------------------------------------

ANDED = module(
    "  logic rr;\n  assign rr = r & r2;\n"
    "  always_ff @(posedge clk or negedge rr) if (!rr) q <= 1'b0; else q <= d;\n" + TAIL
)
TO_PORT = module("  always_ff @(posedge clk or negedge r) if (!r) q <= 1'b0; else q <= d;\n  assign q2 = r;\n")
SYNCHRONISER = module(
    "  logic [1:0] s;\n"
    "  always_ff @(posedge clk or negedge r) if (!r) s <= 2'b00; else s <= {s[0], 1'b1};\n"
    "  assign q = s[1];\n" + TAIL
)
SYNC_ONLY = module(
    "  always_ff @(posedge clk) if (!r) q <= 1'b0; else q <= d;\n  always_ff @(posedge clk) q2 <= r & d2;\n"
)


@needs_yosys
@pytest.mark.parametrize(
    "text",
    [ASYNC, ASYNC_TWO, ANDED, TO_PORT, SYNCHRONISER, SYNC_ONLY],
    ids=["one-reset", "two-resets", "reset-through-logic", "reset-to-a-port", "synchroniser", "synchronous-only"],
)
def test_a_reset_is_compared_as_before(tmp_path, text):
    same = answer(tmp_path / "same", text, text)
    assert same.status == "proved", same
    changed = text.replace("q <= d;", "q <= ~d;") if "q <= d;" in text else text.replace("1'b1}", "1'b0}")
    assert changed != text
    other = answer(tmp_path / "other", text, changed)
    assert other.status == "refuted", other


@needs_yosys
@pytest.mark.parametrize(
    ("text", "old", "new"),
    [
        (ASYNC, "if (!r) q <= 1'b0;", "if (!r) q <= 1'b1;"),
        (ASYNC, "posedge clk or negedge r) if (!r)", "posedge clk or posedge r) if (r)"),
        (ASYNC, "always_ff @(posedge clk or negedge r) if (!r)", "always_ff @(posedge clk) if (!r)"),
        (
            ASYNC,
            "always_ff @(posedge clk or negedge r) if (!r) q <= 1'b0; else q <= d;",
            "always_ff @(posedge clk) q <= d;",
        ),
    ],
    ids=["reset-value", "polarity", "made-synchronous", "removed"],
)
def test_a_changed_reset_is_never_proved(tmp_path, text, old, new):
    assert old in text
    result = answer(tmp_path, text, text.replace(old, new))
    assert result.status in ("refuted", "error"), result


CHILD = (
    "module child (input logic clk, input logic rst_n, input logic d, output logic q);\n"
    "  always_ff @(posedge clk or negedge rst_n) if (!rst_n) q <= 1'b0; else q <= d;\n"
    "endmodule\n"
)
PARENT = module("  child u (.clk(clk), .rst_n(r), .d(d), .q(q));\n" + TAIL)
PARENT_EDITED = PARENT.replace(TAIL, "  assign q2 = d2 & 1'b1;\n")
PARENT_MASKED = module(
    "  logic s;\n  child u (.clk(clk), .rst_n(1'b1), .d(d & r), .q(s));\n  assign q = r & s;\n" + TAIL
)


@needs_yosys
def test_a_reset_into_a_child_that_stays_a_black_box_is_compared_as_before(tmp_path):
    result = run(tmp_path, (CHILD, PARENT), (CHILD, PARENT_EDITED), "m", same=["child"])
    assert result.status == "proved", result


@needs_yosys
def test_a_flattened_child_with_an_asynchronous_reset_is_compared_as_before(tmp_path):
    result = run(tmp_path, (CHILD, PARENT), (CHILD, PARENT_EDITED), "m")
    assert result.status == "proved", result


@needs_yosys
def test_a_reset_that_is_masked_around_a_flattened_child_is_never_proved(tmp_path):
    result = run(tmp_path, (CHILD, PARENT), (CHILD, PARENT_MASKED), "m")
    assert result.status in ("error", "refuted"), result


# --- Z5: a clock through an inverter --------------------------------------------------------------

CPORTS = "input logic clk, input logic d, output logic q"
INVERTED = module("  logic cn;\n  assign cn = ~clk;\n  always_ff @(posedge cn) q <= d;\n", CPORTS)
NEGEDGE = module("  always_ff @(negedge clk) q <= d;\n", CPORTS)
POSEDGE = module("  always_ff @(posedge clk) q <= d;\n", CPORTS)


@needs_yosys
def test_an_inverted_clock_is_the_other_edge(tmp_path):
    assert answer(tmp_path / "a", INVERTED, INVERTED).status == "proved"
    assert answer(tmp_path / "b", INVERTED, NEGEDGE).status == "proved"


@needs_yosys
def test_an_inverted_clock_against_the_plain_clock_is_never_proved(tmp_path):
    result = answer(tmp_path, INVERTED, POSEDGE)
    assert result.status in ("refuted", "error"), result


# --- Z6: flops with different asynchronous resets -------------------------------------------------

SWAPPED = module(
    "  always_ff @(posedge clk or negedge r2) if (!r2) q <= 1'b0; else q <= d;\n"
    "  always_ff @(posedge clk or negedge r) if (!r) q2 <= 1'b0; else q2 <= d2;\n"
)


@needs_yosys
def test_resets_that_are_swapped_between_two_flops_are_never_proved(tmp_path):
    result = answer(tmp_path, ASYNC_TWO, SWAPPED)
    assert result.status in ("refuted", "error"), result


# --- Z7: the clock read as data -------------------------------------------------------------------

PLAIN = module("  always_ff @(posedge clk) q <= d;\n" + TAIL)
ZERO = module("  always_ff @(posedge clk) q <= 1'b0;\n" + TAIL)
CLOCK_PAIRS = {
    "the-clock-itself": (ZERO, module("  always_ff @(posedge clk) q <= clk;\n" + TAIL)),
    "masked-by-the-clock": (PLAIN, module("  always_ff @(posedge clk) q <= d & ~clk;\n" + TAIL)),
    "selected-by-the-clock": (PLAIN, module("  always_ff @(posedge clk) q <= clk ? d2 : d;\n" + TAIL)),
    "enabled-by-the-clock": (PLAIN, module("  always_ff @(posedge clk) if (!clk) q <= d;\n" + TAIL)),
    "through-a-net": (
        PLAIN,
        module("  logic c;\n  assign c = clk | r;\n  always_ff @(posedge clk) q <= d & ~c;\n" + TAIL),
    ),
    "reversed": (module("  always_ff @(posedge clk) q <= d & ~clk;\n" + TAIL), PLAIN),
}


@needs_yosys
@pytest.mark.parametrize("pair", CLOCK_PAIRS)
def test_a_flop_that_reads_its_clock_as_data_is_an_error(tmp_path, pair):
    gold, gate = CLOCK_PAIRS[pair]
    result = answer(tmp_path, gold, gate)
    assert result.status == "error", result
    assert "clock" in result.detail, result.detail
    assert "data" in result.detail, result.detail


@needs_yosys
def test_a_module_with_such_a_flop_is_an_error_for_every_edit(tmp_path):
    """The stated limit of Z7: both sides are the same module."""
    text = CLOCK_PAIRS["masked-by-the-clock"][1]
    assert answer(tmp_path, text, text).status == "error"


CLOCK_OUT = module("  always_ff @(posedge clk) q <= d;\n  assign q2 = d2 & clk;\n")
CLOCK_RESET = module("  always_ff @(posedge r or posedge clk) if (clk) q <= 1'b0; else q <= d;\n" + TAIL)


@needs_yosys
def test_the_clock_in_logic_that_reaches_only_a_port_is_compared(tmp_path):
    assert answer(tmp_path / "same", CLOCK_OUT, CLOCK_OUT).status == "proved"
    other = answer(tmp_path / "other", CLOCK_OUT, CLOCK_OUT.replace("d2 & clk", "d2 | clk"))
    assert other.status == "refuted", other
    assert answer(tmp_path / "gone", CLOCK_OUT, CLOCK_OUT.replace("d2 & clk", "d2")).status == "refuted"


@needs_yosys
def test_an_input_that_is_the_reset_of_one_flop_and_no_clock_is_compared(tmp_path):
    """`clk` is no clock here: the flop is clocked by `r`, and `clk` is its asynchronous reset."""
    assert answer(tmp_path / "same", CLOCK_RESET, CLOCK_RESET).status == "proved"
    other = answer(tmp_path / "other", CLOCK_RESET, CLOCK_RESET.replace("q <= d;", "q <= ~d;"))
    assert other.status == "refuted", other
