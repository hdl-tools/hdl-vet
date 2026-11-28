"""A memory that is only written with a constant (#159).

The read step ran `memory -nomap`, and the pass `opt_mem` inside it removes a bit of a memory
that every write sets to the same constant: the bit becomes that constant. The word is undefined
until its first write, so this is not the same design. No check after the read step could see it.
Measured on `main` at 4e84822, each "proved":

    gold:  assign q = 1'b0;
    gate:  logic mem [0:1];
           always_ff @(posedge clk) if (d) mem[rst_n] <= 1'b0;
           assign q = mem[0];

    the same with 1; and a memory of two bits a word, written with {d, 1'b0}, read as mem[0][0]

The same shape with a flop is "refuted" by the first step (#139).

Owner decision of 2026-11-28: the read step leaves that pass out.

RULES

Y1  A memory keeps its flops in the comparison, also when every write sets a bit to the same
    constant. A gate that reads such a word is "refuted" against the constant, and the detail has
    the words "first state".
Y2  The same memory on both sides is "proved". A gold with such a memory against a gate with the
    constant is "proved": the gate may have any value where the gold is undefined.
Y3  A memory that is written with data is compared as before.

STATED LIMITS

- A memory with an `initial` block that is also written is an "error" now, by the rule for
  initial values. It was "proved" against its constant while the memory was folded away.
- An undefined value does not cancel in the first step, for a word of a memory as for a flop:
  `mem[0] ^ mem[0]` is "refuted" against 0, and that is wrong. It was "proved" before.
- The time of a proof on a wide memory was not measured again. The tests on a memory with a
  reset and on the time limit of a large memory (tests/equiv/test_equiv_review.py) passed in the
  same time.
"""

from __future__ import annotations

import pytest

from equiv_designs import run
from hdl_vet import equiv

pytestmark = [pytest.mark.equiv]

needs_yosys = pytest.mark.skipif(not equiv.available(), reason="Yosys (hdl-vet[equiv]) is not installed")

PORTS = "input logic clk, input logic rst_n, input logic d, output logic q"


def module(body: str) -> str:
    return f"module m ({PORTS});\n{body}endmodule\n"


def answer(tmp_path, gold: str, gate: str):
    return run(tmp_path, (gold,), (gate,), "m")


def constant(value: str) -> str:
    return module(f"  assign q = 1'b{value};\n")


def written_with(value: str) -> str:
    return module(
        f"  logic mem [0:1];\n  always_ff @(posedge clk) if (d) mem[rst_n] <= 1'b{value};\n  assign q = mem[0];\n"
    )


LANE = module(
    "  logic [1:0] mem [0:1];\n  always_ff @(posedge clk) mem[rst_n] <= {d, 1'b0};\n  assign q = mem[0][0];\n"
)
DATA = "  logic mem [0:1];\n  always_ff @(posedge clk) mem[rst_n] <= {value};\n  assign q = mem[0];\n"

# --- Y1 ---------------------------------------------------------------------------------------------


@needs_yosys
@pytest.mark.parametrize("value", ["0", "1"])
def test_a_word_that_is_only_written_with_a_constant_is_not_that_constant(tmp_path, value):
    result = answer(tmp_path, constant(value), written_with(value))
    assert result.status == "refuted", result
    assert "first state" in result.detail, result


@needs_yosys
def test_a_bit_of_a_word_that_is_only_written_with_a_constant_is_not_that_constant(tmp_path):
    result = answer(tmp_path, constant("0"), LANE)
    assert result.status == "refuted", result
    assert "first state" in result.detail, result


# --- Y2 ---------------------------------------------------------------------------------------------


@needs_yosys
@pytest.mark.parametrize("text", [written_with("0"), written_with("1"), LANE])
def test_the_same_memory_on_both_sides_is_proved(tmp_path, text):
    result = answer(tmp_path, text, text)
    assert result.status == "proved", result


@needs_yosys
def test_a_gate_with_the_constant_is_proved_against_the_memory(tmp_path):
    result = answer(tmp_path, written_with("0"), constant("0"))
    assert result.status == "proved", result


# --- Y3 ---------------------------------------------------------------------------------------------


@needs_yosys
def test_a_memory_written_with_data_is_compared_as_before(tmp_path):
    text = module(DATA.format(value="d"))
    assert answer(tmp_path, text, text).status == "proved"
    result = answer(tmp_path, text, module(DATA.format(value="~d")))
    assert result.status in ("refuted", "error"), result


# --- the stated limits ------------------------------------------------------------------------------

BODY = "  logic mem [0:1];\n{init}  always_ff @(posedge clk) if (d) mem[rst_n] <= 1'b0;\n  assign q = {read};\n"


@needs_yosys
def test_a_written_memory_with_an_initial_block_is_an_error(tmp_path):
    gate = module(BODY.format(init="  initial begin mem[0] = 1'b0; mem[1] = 1'b0; end\n", read="mem[0]"))
    result = answer(tmp_path, constant("0"), gate)
    assert result.status == "error", result
    assert "initial value" in result.detail, result


@needs_yosys
def test_a_word_that_cancels_is_refuted_although_it_is_right(tmp_path):
    result = answer(tmp_path, constant("0"), module(BODY.format(init="", read="mem[0] ^ mem[0]")))
    assert result.status == "refuted", result
    assert "first state" in result.detail, result
