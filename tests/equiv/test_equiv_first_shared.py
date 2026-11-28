"""The first state of a flop: flops of the same name start the same (#139).

The first step (rules I1 to I8) started every flop of both sides undefined. Where the gold is
undefined the gate may have any value, so a difference counted only where the gold was defined
without a flop in between. A gate that adds a flop of its own to an output that the gold takes
from a flop was "proved" although it differs:

    gold:  always_ff @(posedge clk) g <= d;   assign q = g;
    gate:  the same g, and  always_ff @(posedge clk) a <= a;   assign q = g ^ a;

More steps do not close this. Measured on 2026-11-28: with the gold output behind one flop two
steps refute it, behind two flops in a row three steps do not, and every fixed number of steps
leaves a pipeline that is one stage deeper.

Owner decision of 2026-11-28, after a prototype was measured on the whole suite: flops of the
same name start with one shared value.

RULES

I9   In the first step a flop that both sides have starts with one shared, defined value: any
     value, the same on both sides. Both sides have a flop when a net of the same name and width
     is the output of state cells on both sides, in the flattened netlists.
I10  A flop that only the gold has starts undefined: the gate may have any value where it shows.
     A flop that only the gate has starts undefined too, and where it shows at an output that the
     gold defines, that is a difference (as I1 had it).
I11  So a gate that adds a flop of its own to an output is "refuted" at every depth of the
     pipeline in front of that output, with "first state" in the detail.
I12  Right pairs stay "proved": the same text, a renamed flop, logic behind a shared flop written
     in another way. A mux of gates with equal arms, selected by a flop that both sides have, is
     "proved" now, because that flop is defined in the first step.
I13  The first step never passes with nothing checked. The shared values are constraints, and
     constraints that cannot all be met would make every comparison pass. The engine shows that
     a first state exists that meets them. When none exists, or when that cannot be shown, the
     answer is "error" and the detail starts with "the first state could not be checked: ".
I14  A name is one flop however many names its net has. An output port that is the same net as a
     flop of the gold is that flop and not a second one.
I15  A flop in a flattened child, in a generate block and a word of a memory are flops like any
     other: they are shared when both sides have them.

STATED LIMITS

- Flops are matched by name. A flop that is renamed between the two sides is not shared, and the
  gold is undefined there in the first step. A gate that renames a flop and adds a flop of its own
  to the same output in one edit is not seen by the first step.
- A flop of the same name that carries another value on the two sides is "refuted" (it was so
  before, by the induction).
- An undefined value of a flop that only the gate has does not cancel: `(a ^ a) | d` is "refuted"
  against `d` when the gold has no flop `a`.
"""

from __future__ import annotations

import pytest

from equiv_designs import run
from hdl_vet import equiv

pytestmark = [pytest.mark.equiv]

needs_yosys = pytest.mark.skipif(not equiv.available(), reason="Yosys (hdl-vet[equiv]) is not installed")

PORTS = "input logic clk, input logic rst_n, input logic d, input logic e, output logic q, output logic o"
OWN = "  logic a;\n  always_ff @(posedge clk) a <= a;\n"
TAIL = "  assign o = d;\n"


def module(body: str, name: str = "m") -> str:
    return f"module {name} ({PORTS});\n{body}endmodule\n"


def answer(tmp_path, gold: str, gate: str, **options):
    return run(tmp_path, (gold,), (gate,), "m", **options)


def refuted_in_the_first_state(result) -> None:
    assert result.status == "refuted", result
    assert "first state" in result.detail, result


# --- I9, I11: every depth ---------------------------------------------------------------------------

PIPES = {
    1: ("  logic g;\n  always_ff @(posedge clk) g <= d;\n", "g"),
    2: ("  logic g, h;\n  always_ff @(posedge clk) begin g <= d; h <= g; end\n", "h"),
    3: ("  logic g, h, i;\n  always_ff @(posedge clk) begin g <= d; h <= g; i <= h; end\n", "i"),
    5: (
        "  logic [4:0] p;\n  always_ff @(posedge clk) p <= {p[3:0], d};\n  logic last;\n  assign last = p[4];\n",
        "last",
    ),
}


@needs_yosys
@pytest.mark.parametrize("depth", PIPES)
def test_a_flop_of_the_gate_added_to_an_output_is_refuted_at_every_depth(tmp_path, depth):
    pipe, last = PIPES[depth]
    gold = module(pipe + f"  assign q = {last};\n" + TAIL)
    gate = module(pipe + OWN + f"  assign q = {last} ^ a;\n" + TAIL)
    refuted_in_the_first_state(answer(tmp_path, gold, gate))


@needs_yosys
@pytest.mark.parametrize("depth", PIPES)
def test_the_same_pipeline_is_proved(tmp_path, depth):
    pipe, last = PIPES[depth]
    text = module(pipe + f"  assign q = {last};\n" + TAIL)
    assert answer(tmp_path, text, text).status == "proved"


KINDS = {
    "a-synchronous-reset": "  always_ff @(posedge clk) if (!rst_n) g <= 1'b0; else g <= d;\n",
    "an-asynchronous-reset": "  always_ff @(posedge clk or negedge rst_n) if (!rst_n) g <= 1'b0; else g <= d;\n",
    "an-enable": "  always_ff @(posedge clk) if (e) g <= d;\n",
    "a-hold": "  always_ff @(posedge clk) g <= g;\n",
}


@needs_yosys
@pytest.mark.parametrize("kind", KINDS)
def test_it_is_refuted_for_every_kind_of_shared_flop(tmp_path, kind):
    shared = "  logic g;\n" + KINDS[kind]
    gold = module(shared + "  assign q = g;\n" + TAIL)
    gate = module(shared + OWN + "  assign q = g ^ a;\n" + TAIL)
    refuted_in_the_first_state(answer(tmp_path, gold, gate))


@needs_yosys
@pytest.mark.parametrize("how", ["g | a", "g & ~a", "a ? ~g : g", "g ^ (a & e)"])
def test_it_is_refuted_however_the_flop_is_added(tmp_path, how):
    shared = "  logic g;\n  always_ff @(posedge clk) g <= d;\n"
    gold = module(shared + "  assign q = g;\n" + TAIL)
    gate = module(shared + OWN + f"  assign q = {how};\n" + TAIL)
    # The induction refutes some of these before the first step is run, with another detail.
    assert answer(tmp_path, gold, gate).status == "refuted"


# --- I10: a flop on one side only -------------------------------------------------------------------


@needs_yosys
def test_a_renamed_flop_is_proved(tmp_path):
    gold = module("  logic g;\n  always_ff @(posedge clk) g <= d;\n  assign q = g;\n" + TAIL)
    gate = module("  logic b;\n  always_ff @(posedge clk) b <= d;\n  assign q = b;\n" + TAIL)
    assert answer(tmp_path, gold, gate).status == "proved"


@needs_yosys
def test_a_flop_of_the_gold_alone_leaves_the_gate_free(tmp_path):
    gold = module("  logic g;\n  always_ff @(posedge clk) g <= 1'b0;\n  assign q = g;\n" + TAIL)
    gate = module("  assign q = 1'b0;\n" + TAIL)
    assert answer(tmp_path, gold, gate).status == "proved"


@needs_yosys
def test_a_flop_of_the_gate_alone_is_refuted_against_a_constant(tmp_path):
    gold = module("  assign q = 1'b0;\n" + TAIL)
    gate = module("  logic g;\n  always_ff @(posedge clk) g <= 1'b0;\n  assign q = g;\n" + TAIL)
    refuted_in_the_first_state(answer(tmp_path, gold, gate))


# --- I12: right pairs -------------------------------------------------------------------------------

SHARED = "  logic s;\n  always_ff @(posedge clk) s <= e;\n"


@needs_yosys
def test_logic_behind_a_shared_flop_written_in_another_way_is_proved(tmp_path):
    gold = module(SHARED + "  assign q = s & rst_n;\n" + TAIL)
    gate = module(SHARED + "  assign q = ~(~s | ~rst_n);\n" + TAIL)
    assert answer(tmp_path, gold, gate).status == "proved"


@needs_yosys
def test_a_mux_of_gates_selected_by_a_shared_flop_is_proved(tmp_path):
    gold = module(SHARED + "  assign q = d;\n  assign o = s;\n")
    gate = module(SHARED + "  assign q = (s & d) | (~s & d);\n  assign o = s;\n")
    assert answer(tmp_path, gold, gate).status == "proved"


@needs_yosys
def test_a_changed_function_behind_a_shared_flop_is_refuted(tmp_path):
    gold = module(SHARED + "  assign q = s & d;\n" + TAIL)
    gate = module(SHARED + "  assign q = s | d;\n" + TAIL)
    assert answer(tmp_path, gold, gate).status == "refuted"


# --- I13, I14: never a pass with nothing checked ----------------------------------------------------


@needs_yosys
def test_an_output_port_that_is_a_flop_is_one_flop(tmp_path):
    """The port `q` is the flop itself on the gold side, and logic on the gate side."""
    gold = module("  always_ff @(posedge clk) q <= d;\n" + TAIL)
    gate = module("  logic r;\n  always_ff @(posedge clk) r <= d;\n" + OWN + "  assign q = r ^ a;\n" + TAIL)
    result = answer(tmp_path, gold, gate)
    assert result.status in ("refuted", "error"), result


@needs_yosys
def test_two_names_of_one_flop_do_not_hide_a_hole(tmp_path):
    shared = "  logic g, alias_g;\n  always_ff @(posedge clk) g <= d;\n  assign alias_g = g;\n"
    gold = module(shared + "  assign q = alias_g;\n" + TAIL)
    gate = module(shared + OWN + "  assign q = alias_g ^ a;\n" + TAIL)
    refuted_in_the_first_state(answer(tmp_path, gold, gate))


RESETS = {
    "another-reset-value": (
        "  always_ff @(posedge clk or negedge rst_n) if (!rst_n) g <= 1'b0; else g <= d;\n",
        "  always_ff @(posedge clk or negedge rst_n) if (!rst_n) g <= 1'b1; else g <= d;\n",
    ),
    "a-reset-on-the-gold-only": (
        "  always_ff @(posedge clk or negedge rst_n) if (!rst_n) g <= 1'b0; else g <= d;\n",
        "  always_ff @(posedge clk) g <= d;\n",
    ),
    "a-reset-on-the-gate-only": (
        "  always_ff @(posedge clk) g <= d;\n",
        "  always_ff @(posedge clk or negedge rst_n) if (!rst_n) g <= 1'b0; else g <= d;\n",
    ),
}


@needs_yosys
@pytest.mark.parametrize("pair", RESETS)
def test_a_shared_flop_with_another_reset_is_never_proved(tmp_path, pair):
    """The shared value cannot be met while the reset is active. That must not pass the pair."""
    gold_flop, gate_flop = RESETS[pair]
    gold = module("  logic g;\n" + gold_flop + "  assign q = g;\n" + TAIL)
    gate = module("  logic g;\n" + gate_flop + "  assign q = g;\n" + TAIL)
    result = answer(tmp_path, gold, gate)
    assert result.status in ("refuted", "error"), result


@needs_yosys
def test_a_hole_that_shows_only_under_reset_is_never_proved(tmp_path):
    """The two sides have the flop g with another reset value, and the gate adds a flop of its own under reset."""
    gold = module(
        "  logic g;\n  always_ff @(posedge clk or negedge rst_n) if (!rst_n) g <= 1'b0; else g <= d;\n"
        "  assign q = 1'b0;\n  assign o = g;\n"
    )
    gate = module(
        "  logic g;\n  always_ff @(posedge clk or negedge rst_n) if (!rst_n) g <= 1'b1; else g <= d;\n"
        + OWN
        + "  assign q = a & ~rst_n;\n  assign o = g;\n"
    )
    result = answer(tmp_path, gold, gate)
    assert result.status in ("refuted", "error"), result


# --- I15: flops with names that are not plain -------------------------------------------------------

CHILD = (
    "module c (input logic clk, input logic d, output logic y);\n"
    "  logic r;\n  always_ff @(posedge clk) r <= d;\n  assign y = r;\nendmodule\n"
)


@needs_yosys
def test_a_flop_in_a_flattened_child_is_shared(tmp_path):
    use = "  logic w;\n  c u (.clk(clk), .d(d), .y(w));\n"
    gold = CHILD + module(use + "  assign q = w;\n" + TAIL)
    gate = CHILD + module(use + OWN + "  assign q = w ^ a;\n" + TAIL)
    refuted_in_the_first_state(answer(tmp_path, gold, gate))


@needs_yosys
def test_a_flop_in_a_generate_block_is_shared(tmp_path):
    block = (
        "  logic [1:0] w;\n"
        "  for (genvar i = 0; i < 2; i++) begin : stage\n"
        "    logic r;\n    always_ff @(posedge clk) r <= d;\n    assign w[i] = r;\n  end\n"
    )
    gold = module(block + "  assign q = w[1];\n" + TAIL)
    gate = module(block + OWN + "  assign q = w[1] ^ a;\n" + TAIL)
    refuted_in_the_first_state(answer(tmp_path, gold, gate))


@needs_yosys
def test_a_word_of_a_memory_is_shared(tmp_path):
    memory = "  logic mem [0:1];\n  always_ff @(posedge clk) mem[e] <= d;\n"
    gold = module(memory + "  assign q = mem[0];\n" + TAIL)
    gate = module(memory + OWN + "  assign q = mem[0] ^ a;\n" + TAIL)
    refuted_in_the_first_state(answer(tmp_path, gold, gate))


# --- the stated limits ------------------------------------------------------------------------------


@needs_yosys
def test_an_undefined_value_of_a_flop_of_the_gate_alone_does_not_cancel(tmp_path):
    """Stated limit: right, and refuted."""
    gold = module("  assign q = d;\n" + TAIL)
    gate = module(OWN + "  assign q = (a ^ a) | d;\n" + TAIL)
    refuted_in_the_first_state(answer(tmp_path, gold, gate))
