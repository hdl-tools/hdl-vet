"""Stated limits of the comparison that had no test (#106, the third batch).

The item "What the check cannot model" of the chapter on limits lists cases that give "error".
The survey of 2026-11-27 found no test for five of them. Here each one is in a module of which
the two sides differ by a harmless rewrite (`a` against `a & 1'b1`), so a comparison that could
judge the module would answer "proved". A test that fails means that the limit is gone or is
stated wrongly, and the chapter has to change with it.

A review of the tests asked for two things. Each "error" is tied to its construct by a part of
its detail, because the comparison has other ways to an "error". And the body of V7 is `a - b`,
which is not the same when two ports change places, so "proved" shows that the ports are matched
by name.

V1  A latch gives "error".
V2  Flops on both edges of one clock give "error".
V3  An asynchronous set together with an asynchronous reset on one flop gives "error".
V4  A clock that reaches a flop through a multiplexer that is not tied gives "error".
V5  A clock that reaches a flop through a divider gives "error".

From the items "Parameters" and "Changes that the proof does not see":

V6  A module with a parameter that has a string value gives "error" at the sign-off, also when
    the parameter is at its default.
V7  A change of the order of the ports, and a change of the signedness of a port, are "proved":
    the module computes the same.
"""

from __future__ import annotations

import pytest

from equiv_designs import run
from hdl_vet import equiv
from hdl_vet.ops import Vet
from hdl_vet.session import Session

pytestmark = [pytest.mark.equiv]

needs_yosys = pytest.mark.skipif(not equiv.available(), reason="Yosys (hdl-vet[equiv]) is not installed")

PORTS = (
    "input logic clk, input logic clk2, input logic rst_n, input logic set_n, input logic a, input logic en, "
    "output logic o, output logic y"
)

CASES = {
    "latch": "  logic q;\n  always_latch if (en) q = a;\n  assign y = q;\n",
    "both-edges": (
        "  logic r, s;\n  always_ff @(posedge clk) r <= a;\n  always_ff @(negedge clk) s <= a;\n  assign y = r ^ s;\n"
    ),
    "set-and-reset": (
        "  logic r;\n"
        "  always_ff @(posedge clk or negedge rst_n or negedge set_n)\n"
        "    if (!rst_n) r <= 1'b0; else if (!set_n) r <= 1'b1; else r <= a;\n"
        "  assign y = r;\n"
    ),
    "clock-mux": (
        "  logic cm;\n  assign cm = en ? clk : clk2;\n  logic r;\n  always_ff @(posedge cm) r <= a;\n  assign y = r;\n"
    ),
    "clock-divider": (
        "  logic d, r;\n  always_ff @(posedge clk) d <= ~d;\n  always_ff @(posedge d) r <= a;\n  assign y = r;\n"
    ),
}


# The answer must come from the construct, not from another way to an "error".
DETAIL = {
    "latch": "only flops are supported",
    "both-edges": "more than one clock",
    "set-and-reset": "multiple asynchronous loads unsupported",
    "clock-mux": "clocked by something other than an input port",
    "clock-divider": "clocked by something other than an input port",
}


def module(body: str, o: str) -> str:
    return f"module m ({PORTS});\n  assign o = {o};\n{body}endmodule\n"


@needs_yosys
@pytest.mark.parametrize("case", sorted(CASES))
def test_a_module_that_the_check_cannot_model_is_an_error(tmp_path, case):
    result = run(tmp_path, (module(CASES[case], "a"),), (module(CASES[case], "a & 1'b1"),), "m")
    assert result.status == "error", (result.status, result.detail)
    assert DETAIL[case] in result.detail, result.detail


@needs_yosys
def test_the_same_rewrite_with_a_plain_flop_is_proved(tmp_path):
    """The other side: the rewrite itself is harmless, so the errors above come from the construct."""
    body = "  logic r;\n  always_ff @(posedge clk) r <= a;\n  assign y = r;\n"
    result = run(tmp_path, (module(body, "a"),), (module(body, "a & 1'b1"),), "m")
    assert result.status == "proved", (result.status, result.detail)


STRING_LEAF = (
    'module leaf #(parameter string S = "x") (input logic a, output logic o, output logic y);\n'
    "  assign o = a;\n"
    '  assign y = (S == "x") ? a : 1\'b0;\n'
    "endmodule\n"
)


@needs_yosys
@pytest.mark.parametrize("inst", ["leaf u", 'leaf #(.S("abc")) u'], ids=["default", "set"])
def test_a_parameter_with_a_string_value_is_an_error_at_the_sign_off(tmp_path, inst):
    """The sign-off gives the check the values of the elaborated instance, so the default counts too."""
    root = tmp_path / "design"
    (root / "rtl").mkdir(parents=True)
    top = f"module top (input logic a, output logic o, output logic y);\n  {inst} (.a(a), .o(o), .y(y));\nendmodule\n"
    (root / "rtl" / "leaf.sv").write_bytes(STRING_LEAF.encode())
    (root / "rtl" / "top.sv").write_bytes(top.encode())
    (root / "design.f").write_bytes(b"rtl/leaf.sv\nrtl/top.sv\n")
    vet = Vet(Session(root / "design.f", top="top"))
    path = root / "rtl" / "leaf.sv"
    old = b"assign o = a;"
    at = vet.text(path).index(old)
    vet.splice(path, at, at + len(old), b"assign o = a & 1'b1;", old=old)
    result = vet.finish()
    assert not result.ok
    assert [(r.module, r.status) for r in result.equivalence] == [("leaf", "error")]
    assert "parameter S" in result.equivalence[0].detail, result.equivalence[0].detail


BODY = "  assign y = a - b;\nendmodule\n"
CHANGES = {
    "port-order": (
        "module m (input logic [3:0] a, input logic [3:0] b, output logic [4:0] y);\n",
        "module m (input logic [3:0] b, input logic [3:0] a, output logic [4:0] y);\n",
    ),
    "signedness": (
        "module m (input logic [3:0] a, input logic [3:0] b, output logic [4:0] y);\n",
        "module m (input logic [3:0] a, input logic [3:0] b, output logic signed [4:0] y);\n",
    ),
}


@needs_yosys
@pytest.mark.parametrize("change", sorted(CHANGES))
def test_a_change_of_a_port_that_the_proof_does_not_see_is_proved(tmp_path, change):
    gold, gate = CHANGES[change]
    result = run(tmp_path, (gold + BODY,), (gate + BODY,), "m")
    assert result.status == "proved", (result.status, result.detail)
