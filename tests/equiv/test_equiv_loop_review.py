"""A combinational loop: what the review found (#143).

The review of the loop check (rules Q1 to Q6 of test_equiv_loop.py) found one loop that the
check does not see, one wrong "error", and one weak message.

RULES

Q7  A flop with an asynchronous load is transparent while the load is active: its output is its
    load value then. So the load value and the load condition of such a flop lead to its output
    in the walk. A flop that loads `~r` or `r` into itself, r being its own output, has a
    combinational loop, and the answer is "error". Measured before this rule: the gold loads
    `~r`, the gate loads `r`, and the answer was "proved" although the gold oscillates and the
    gate holds. A flop with an asynchronous reset to a constant has no such path.
Q8  A `case` in `always_comb` is a mux and is followed bit by bit, as `? :` is. A vector whose
    bit i is made from its bit i-1 in one arm of a case is no loop.
Q9  The detail names a net of the source when one is on the loop, and an internal net (a name
    that starts with `$`) only when none is.

STATED LIMITS

- Q7 does not ask whether the load can be active. A flop that loads a function of its own
  output is an "error" for every edit of its module.
"""

from __future__ import annotations

import pytest

from equiv_designs import run
from hdl_vet import equiv

pytestmark = [pytest.mark.equiv]

needs_yosys = pytest.mark.skipif(not equiv.available(), reason="Yosys (hdl-vet[equiv]) is not installed")

PORTS = "input logic clk, input logic d, input logic e, input logic t, output logic q"


def module(body: str, ports: str = PORTS) -> str:
    return f"module m ({ports});\n{body}endmodule\n"


LOAD = "  logic r;\n  always_ff @(posedge clk or posedge e) if (e) r <= {value}; else r <= d;\n  assign q = r;\n"

# --- Q7 ---------------------------------------------------------------------------------------------


@needs_yosys
@pytest.mark.parametrize(("gold", "gate"), [("~r", "r"), ("~r", "~r"), ("r", "r"), ("~r", "~(r ^ 1'b0)")])
def test_a_flop_that_loads_its_own_output_is_an_error(tmp_path, gold, gate):
    result = run(tmp_path, (module(LOAD.format(value=gold)),), (module(LOAD.format(value=gate)),), "m")
    assert result.status == "error", result
    assert "combinational loop" in result.detail, result


@needs_yosys
def test_a_flop_with_an_asynchronous_reset_to_a_constant_has_no_loop(tmp_path):
    text = module(LOAD.format(value="1'b0"))
    result = run(tmp_path, (text,), (text,), "m")
    assert result.status == "proved", result


@needs_yosys
def test_a_flop_that_loads_an_input_has_no_loop(tmp_path):
    text = module(LOAD.format(value="t"))
    result = run(tmp_path, (text,), (text,), "m")
    assert result.status in ("proved", "error"), result
    assert "combinational loop" not in result.detail, result


# --- Q8 ---------------------------------------------------------------------------------------------

CASE_PORTS = "input logic [3:0] a, input logic [3:0] b, input logic [1:0] s, input logic v, output logic [3:0] y"
CASE = (
    "  logic [3:0] w;\n"
    "  always_comb case (s) 2'd0: w = a; 2'd1: w = {w[2:0], v}; default: w = b; endcase\n"
    "  assign y = w;\n"
)
CASE_LOOP = (
    "  logic [3:0] w;\n"
    "  always_comb case (s) 2'd0: w = a; 2'd1: w = {w[0], w[3:1]}; default: w = b; endcase\n"
    "  assign y = w;\n"
)


@needs_yosys
def test_a_chain_in_one_arm_of_a_case_is_no_loop(tmp_path):
    text = module(CASE, CASE_PORTS)
    result = run(tmp_path, (text,), (text,), "m")
    assert result.status == "proved", result


@needs_yosys
def test_a_ring_in_one_arm_of_a_case_is_an_error(tmp_path):
    text = module(CASE_LOOP, CASE_PORTS)
    result = run(tmp_path, (text,), (text,), "m")
    assert result.status == "error", result
    assert "combinational loop" in result.detail, result


# --- Q9 ---------------------------------------------------------------------------------------------


@needs_yosys
def test_the_detail_names_a_net_of_the_source(tmp_path):
    gate = module("  logic lp;\n  assign lp = t ? (d & lp) : d;\n  assign q = lp;\n")
    result = run(tmp_path, (module("  assign q = d;\n"),), (gate,), "m")
    assert result.status == "error", result
    assert "combinational loop through" in result.detail and "$" not in result.detail, result
