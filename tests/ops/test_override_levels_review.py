"""Overrides at more than one level: tests from the adversarial review.

The base specification is in tests/equiv/test_equiv_inert.py (I1 to I7) and
tests/ops/test_override_levels.py (H1 to H10). The review (about 60 cases on
small designs, five scenarios on a copy of demo_ss with up to four levels, all
proved there) found that the engine's check of the pin holds for 17 of 19
attacks. It found two wrong ok sign-offs through a module that only carries an
enable, one wrong "proved" for a parent, and a false failure. These rules close
them.

R1  A type parameter that is not at its default. A module is compared at the
    values of its value parameters; a type parameter was compared at its default
    only, and the parent then took the child for proved. Now: a module that is
    elaborated with a type parameter at another type than its default gets the
    record "error" for that module, and the detail names the parameter. It is
    never "proved" and so never inert. A module whose type parameters are all at
    their defaults is compared as before.
R2  (The engine.) A named pin of an inert child on a net with more than one
    driver is not 0: "error".
R3  The way of an enable is decided by what the name means, not by its text. A
    connection `test_en_i` that is a local signal of a generate block, with the
    name of the new port of the module, is "not off".
R4  Every elaboration is looked at. A module with two elaborations whose
    instances of the child have other connections gives a record for each
    connection, and one "not off" fails the sign-off. The `instance` of a record
    is the path of the instance inside the parent module, with the names of the
    generate blocks (`g.u_leaf`).
R5  No false failure for an inert child in an instance array or a generate loop:
    the parent is proved. When the engine cannot handle the interface of an
    inert child (a name that it cannot handle, a new output port of the child),
    the sign-off compares the parent again with the child inside, as before
    this part. The record is what that comparison says.
R6  "Off" is a literal with the value 0 and no x or z digit, of any base and
    size (`1'b0`, `'0`, `'b0`, `8'h00`, `1'o0`, `0`). A literal with a sign
    marker, a literal in brackets, a parameter and a macro are "not off".
R7  (The adapter.) `Design.driven_bits` on a fresh load gives the bits of every
    elaboration of a module, also when no other query ran before.

LIMITS, STATED (in the manual too)

- A module that only carries an enable gets no comparison: the sign-off trusts
  its connections as R3 and R4 read them.
- "The top" is the top of the session. If it is a wrapper inside a larger
  design, the way of an enable ends at its new port.
- Not checked by the review: inout and interface ports of an inert child,
  parents made by `add_file` or `replace_module`, `defparam`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from equiv_designs import run
from hdl_vet import equiv
from hdl_vet.slang_adapter import load
from override_design import Design, needs_yosys

pytestmark = [pytest.mark.equiv]

LEAF_OP = "override_net leaf.q"


def enables(result) -> list[tuple[str, str, str, str, str]]:
    return [(e.module, e.instance, e.pin, e.connection, e.status) for e in result.enables]


def records(result) -> dict[str, str]:
    return {r.module: r.status for r in result.equivalence}


def no_yosys(monkeypatch) -> None:
    monkeypatch.setattr(equiv, "available", lambda: False)


# --- R1: a type parameter --------------------------------------------------------------------------

T_LEAF = (
    "module leaf #(\n"
    "  parameter type T = logic\n"
    ") (\n"
    "  input  logic clk_i,\n"
    "  input  logic rst_ni,\n"
    "  input  logic d,\n"
    "  output logic q\n"
    ");\n"
    "  logic q_r, p;\n"
    "\n"
    "  always_ff @(posedge clk_i or negedge rst_ni)\n"
    "    if (!rst_ni) q_r <= 1'b0;\n"
    "    else         q_r <= d;\n"
    "\n"
    "  if ($bits(T) > 1) begin : g_wide\n"
    "    assign p = q_r;\n"
    "  end else begin : g_one\n"
    "    assign p = q_r;\n"
    "  end\n"
    "\n"
    "  assign q = p;\n"
    "endmodule\n"
)
T_MID = (
    "module mid (\n"
    "  input  logic clk_i,\n"
    "  input  logic rst_ni,\n"
    "  input  logic d,\n"
    "  output logic y\n"
    ");\n"
    "  logic lq;\n"
    "\n"
    "  leaf #(.T(logic [1:0])) u_leaf (.clk_i(clk_i), .rst_ni(rst_ni), .d(d), .q(lq));\n"
    "\n"
    "  assign y = lq;\n"
    "endmodule\n"
)
T_TOP = (
    "module top (\n"
    "  input  logic clk_i,\n"
    "  input  logic rst_ni,\n"
    "  input  logic d,\n"
    "  output logic y\n"
    ");\n"
    "  mid u_mid (.clk_i(clk_i), .rst_ni(rst_ni), .d(d), .y(y));\n"
    "endmodule\n"
)


def typed(tmp_path: Path, mid: str = T_MID) -> Design:
    return Design(tmp_path / "typed", {"leaf": T_LEAF, "mid": mid, "top": T_TOP})


def both_levels(vet) -> None:
    vet.punch_port("test_en_i", "input", to="top.u_mid.u_leaf")
    vet.module("leaf").override_net("q", "1'b0", enable="test_en_i")
    vet.module("mid").override_net("y", "1'b1", enable="test_en_i")


@needs_yosys
def test_a_type_parameter_at_another_type_is_never_proved(tmp_path):
    """The branch for the wide type is changed by hand: at the default type nothing differs."""
    design = typed(tmp_path)
    vet = design.vet()
    both_levels(vet)
    design.replace(vet, "leaf", b"begin : g_wide\n    assign p = q_r;", b"begin : g_wide\n    assign p = ~q_r;")
    result = vet.finish()
    leaf = [r for r in result.equivalence if r.module == "leaf"]
    assert [r.status for r in leaf] == ["error"], result.report()
    assert "T" in leaf[0].detail and "type" in leaf[0].detail
    assert records(result)["mid"] != "proved", result.report()
    assert not result.ok


@needs_yosys
def test_a_type_parameter_at_another_type_is_an_error_without_any_change_too(tmp_path):
    design = typed(tmp_path)
    vet = design.vet()
    both_levels(vet)
    result = vet.finish()
    assert records(result)["leaf"] == "error", result.report()
    assert not result.ok


@needs_yosys
def test_a_type_parameter_at_its_default_is_compared(tmp_path):
    design = typed(tmp_path, T_MID.replace("leaf #(.T(logic [1:0])) u_leaf", "leaf u_leaf"))
    vet = design.vet()
    both_levels(vet)
    result = vet.finish()
    assert records(result) == {"leaf": "proved", "mid": "proved"}, result.report()
    assert result.ok


# --- R2: the engine --------------------------------------------------------------------------------

E_LEAF = (
    "module leaf (\n"
    "  input  logic clk_i,\n"
    "  input  logic d,\n"
    "  output logic q\n"
    ");\n"
    "  always_ff @(posedge clk_i) q <= d;\n"
    "endmodule\n"
)
E_LEAF_EN = E_LEAF.replace("  input  logic d,\n", "  input  logic d,\n  input  logic en_i,\n").replace(
    "q <= d;", "q <= en_i ? 1'b0 : d;"
)
E_MID = (
    "module mid (\n"
    "  input  logic clk_i,\n"
    "  input  logic d,\n"
    "  output logic y\n"
    ");\n"
    "  leaf u_leaf (.clk_i(clk_i), .d(d), .q(y));\n"
    "endmodule\n"
)


def e_mid(pin: str, extra: str = "") -> str:
    text = E_MID.replace("  input  logic d,\n", "  input  logic d,\n  input  logic en_i,\n")
    text = text.replace("  leaf u_leaf (.clk_i(clk_i), .d(d),", f"{extra}  leaf u_leaf (.clk_i(clk_i), .d(d), {pin},")
    assert pin in text
    return text


@needs_yosys
def test_the_engine_set_up_is_proved(tmp_path):
    result = run(
        tmp_path, (E_LEAF, E_MID), (E_LEAF_EN, e_mid(".en_i(en_i)")), "mid", tie={"en_i": 0}, inert={"leaf": ["en_i"]}
    )
    assert result.status == "proved", result.detail


@needs_yosys
@pytest.mark.parametrize(
    "extra",
    [
        "  wire e;\n  assign e = 1'b0;\n  assign e = d;\n",
        "  wire e;\n  assign e = d;\n  assign e = 1'b0;\n",
        "  wire e;\n  assign e = en_i;\n  assign e = d;\n",
    ],
)
def test_a_pin_on_a_net_with_two_drivers_is_never_proved(tmp_path, extra):
    gate = e_mid(".en_i(e)", extra)
    result = run(tmp_path, (E_LEAF, E_MID), (E_LEAF_EN, gate), "mid", tie={"en_i": 0}, inert={"leaf": ["en_i"]})
    assert result.status in ("error", "refuted"), result.detail


@needs_yosys
def test_a_net_with_two_drivers_away_from_the_pin_is_not_this_check(tmp_path):
    """A tri-state net elsewhere in the parent: the check of the pin does not look at it."""
    extra = "  wire t;\n  assign t = d ? 1'b0 : 1'bz;\n  assign t = d ? 1'bz : 1'b1;\n"
    gate = e_mid(".en_i(en_i)", extra)
    gold = E_MID.replace("  leaf u_leaf", extra + "  leaf u_leaf")
    assert extra in gate and extra in gold
    result = run(tmp_path, (E_LEAF, gold), (E_LEAF_EN, gate), "mid", tie={"en_i": 0}, inert={"leaf": ["en_i"]})
    plain = run(tmp_path / "plain", (E_LEAF, gold), (E_LEAF, gold), "mid", same=["leaf"])
    # With a child as a black box the engine cannot take a tri-state buffer (an older limit): the answer
    # is the one that the unchanged design gets with the child named in `same`.
    assert (result.status, result.detail) == (plain.status, plain.detail)
    assert "en_i" not in result.detail and "driver" not in result.detail


# --- R3, R4: what the name means, and every elaboration -------------------------------------------

G_LEAF = E_LEAF.replace(
    "always_ff @(posedge clk_i) q <= d;", "logic q_r;\n  always_ff @(posedge clk_i) q_r <= d;\n  assign q = q_r;"
)
SHADOW_MID = (
    "module mid (\n"
    "  input  logic clk_i,\n"
    "  input  logic d,\n"
    "  output logic y\n"
    ");\n"
    "  if (1) begin : g\n"
    "    logic test_en_i;\n"
    "    assign test_en_i = d;\n"
    "    leaf u_leaf (.clk_i(clk_i), .d(d), .q(y));\n"
    "  end\n"
    "endmodule\n"
)
G_TOP = (
    "module top (\n"
    "  input  logic clk_i,\n"
    "  input  logic d,\n"
    "  output logic y,\n"
    "  output logic y1\n"
    ");\n"
    "  mid u_mid0 (.clk_i(clk_i), .d(d), .y(y));\n"
    "  assign y1 = d;\n"
    "endmodule\n"
)


def test_a_local_signal_with_the_name_of_the_port_is_not_off(tmp_path, monkeypatch):
    no_yosys(monkeypatch)
    design = Design(tmp_path / "shadow", {"leaf": G_LEAF, "mid": SHADOW_MID, "top": G_TOP})
    vet = design.vet()
    vet.module("leaf").add_port("test_en_i", "input")
    vet.punch_port("test_en_i", "input", to="top.u_mid0")
    design.replace(vet, "mid", b"leaf u_leaf (.clk_i(clk_i),", b"leaf u_leaf (.test_en_i(test_en_i), .clk_i(clk_i),")
    vet.module("leaf").override_net("q", "1'b0", enable="test_en_i", waiver="w")
    result = vet.check()
    assert ("mid", "g.u_leaf", "test_en_i", "test_en_i", "waived") in enables(result), enables(result)


TWO_MID = (
    "module mid #(\n"
    "  parameter bit X = 1'b1\n"
    ") (\n"
    "  input  logic clk_i,\n"
    "  input  logic d,\n"
    "  output logic y\n"
    ");\n"
    "  if (X) begin : g\n"
    "    leaf u_leaf (.clk_i(clk_i), .d(d), .q(y));\n"
    "  end else begin : g\n"
    "    leaf u_leaf (.clk_i(clk_i), .d(~d), .q(y));\n"
    "  end\n"
    "endmodule\n"
)
TWO_TOP = G_TOP.replace("  assign y1 = d;\n", "  mid #(.X(1'b0)) u_mid1 (.clk_i(clk_i), .d(d), .y(y1));\n")


def test_two_elaborations_with_other_connections_are_both_looked_at(tmp_path, monkeypatch):
    no_yosys(monkeypatch)
    design = Design(tmp_path / "two", {"leaf": G_LEAF, "mid": TWO_MID, "top": TWO_TOP})
    vet = design.vet()
    vet.module("leaf", instances=2).add_port("test_en_i", "input")
    vet.module("mid", instances=2).add_port("test_en_i", "input")
    vet.module("top").add_port("test_en_i", "input")
    vet.module("top").instance("u_mid0").connect("test_en_i", "test_en_i")
    vet.module("top").instance("u_mid1").connect("test_en_i", "test_en_i")
    design.replace(
        vet, "mid", b"leaf u_leaf (.clk_i(clk_i), .d(d),", b"leaf u_leaf (.test_en_i(test_en_i), .clk_i(clk_i), .d(d),"
    )
    design.replace(
        vet, "mid", b"leaf u_leaf (.clk_i(clk_i), .d(~d),", b"leaf u_leaf (.test_en_i(1'b1), .clk_i(clk_i), .d(~d),"
    )
    vet.module("leaf", instances=2).override_net("q", "1'b0", enable="test_en_i", waiver="w")
    result = vet.check()
    in_mid = {(e.instance, e.connection, e.status) for e in result.enables if e.module == "mid"}
    assert in_mid == {("g.u_leaf", "test_en_i", "carried"), ("g.u_leaf", "1'b1", "waived")}, enables(result)
    assert f"  waived: enable mid.g.u_leaf.test_en_i is connected to 1'b1 ({LEAF_OP})" in result.report()


# --- R5: arrays, loops, a new output ---------------------------------------------------------------

LOOP_MID = (
    "module mid (\n"
    "  input  logic       clk_i,\n"
    "  input  logic [1:0] d,\n"
    "  output logic       y\n"
    ");\n"
    "  logic [1:0] lq;\n"
    "\n"
    "  for (genvar i = 0; i < 2; i++) begin : g\n"
    "    leaf u_leaf (.clk_i(clk_i), .d(d[i]), .q(lq[i]));\n"
    "  end\n"
    "\n"
    "  assign y = ^lq;\n"
    "endmodule\n"
)
ARRAY_MID = LOOP_MID.replace(
    "  for (genvar i = 0; i < 2; i++) begin : g\n    leaf u_leaf (.clk_i(clk_i), .d(d[i]), .q(lq[i]));\n  end\n",
    "  leaf u_leaf [1:0] (.clk_i(clk_i), .d(d), .q(lq));\n",
)
WIDE_TOP = (
    "module top (\n"
    "  input  logic       clk_i,\n"
    "  input  logic [1:0] d,\n"
    "  output logic       y\n"
    ");\n"
    "  mid u_mid (.clk_i(clk_i), .d(d), .y(y));\n"
    "endmodule\n"
)


@needs_yosys
@pytest.mark.parametrize("mid", [LOOP_MID, ARRAY_MID], ids=["loop", "array"])
def test_an_inert_child_in_a_loop_or_an_array_gives_no_false_failure(tmp_path, mid):
    assert mid != LOOP_MID or "genvar" in mid
    design = Design(tmp_path / "many", {"leaf": G_LEAF, "mid": mid, "top": WIDE_TOP})
    vet = design.vet()
    vet.module("leaf", instances=2).add_port("test_en_i", "input")
    vet.punch_port("test_en_i", "input", to="top.u_mid")
    design.replace(vet, "mid", b"(.clk_i(clk_i),", b"(.test_en_i(test_en_i), .clk_i(clk_i),")
    vet.module("leaf", instances=2).override_net("q", "1'b0", enable="test_en_i")
    vet.module("mid").override_net("y", "1'b1", enable="test_en_i")
    result = vet.finish()
    assert records(result) == {"leaf": "proved", "mid": "proved"}, result.report()
    assert {e.status for e in result.enables} == {"carried"}, enables(result)
    assert result.ok, result.report()


@needs_yosys
def test_a_new_output_of_a_proved_child_gives_no_false_failure(tmp_path):
    design = Design(tmp_path / "out", {"leaf": G_LEAF, "mid": E_MID, "top": OFF_TOP})
    vet = design.vet()
    vet.punch_port("test_en_i", "input", to="top.u_mid.u_leaf")
    vet.module("leaf").add_port("tap_o", "output")
    vet.module("leaf").add_assign("tap_o", "q_r")
    vet.module("leaf").override_net("q", "1'b0", enable="test_en_i")
    vet.module("mid").override_net("y", "1'b1", enable="test_en_i")
    result = vet.finish()
    assert records(result) == {"leaf": "proved", "mid": "proved"}, result.report()


# --- R6: what "off" is ----------------------------------------------------------------------------

OFF_MID = E_MID
OFF_TOP = G_TOP.replace("u_mid0", "u_mid")


@pytest.mark.parametrize(
    "text, status",
    [
        ("'b0", "off"),
        ("8'h00", "off"),
        ("1'o0", "off"),
        ("0", "off"),
        ("1'b0", "off"),
        ("(1'b0)", "waived"),
        ("1'sb0", "waived"),
        ("1'bx", "waived"),
        ("'bz", "waived"),
        ("2'b0x", "waived"),
        ("1'b1", "waived"),
        ("ZERO", "waived"),
    ],
)
def test_what_off_is(tmp_path, monkeypatch, text, status):
    no_yosys(monkeypatch)
    mid = OFF_MID.replace(");\n  leaf", ");\n  localparam logic ZERO = 1'b0;\n  leaf")
    assert "ZERO" in mid
    design = Design(tmp_path / "off", {"leaf": G_LEAF, "mid": mid, "top": OFF_TOP})
    vet = design.vet()
    vet.module("leaf").add_port("test_en_i", "input")
    vet.module("mid").instance("u_leaf").connect("test_en_i", text)
    vet.module("leaf").override_net("q", "1'b0", enable="test_en_i", waiver="w")
    result = vet.check()
    assert enables(result) == [("mid", "u_leaf", "test_en_i", text, status)]


# --- R7: the adapter ------------------------------------------------------------------------------


def test_driven_bits_on_a_fresh_load_sees_every_elaboration(tmp_path: Path):
    (tmp_path / "m.sv").write_text(
        "module child (input logic a, output logic y);\n"
        "  logic w;\n"
        "  assign w = a;\n"
        "  assign y = w;\n"
        "endmodule\n"
        "module top (input logic a, output logic y1, output logic y2);\n"
        "  child u1 (.a(a), .y(y1));\n"
        "  child u2 (.a(a), .y(y2));\n"
        "endmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "m.f").write_text("m.sv\n", encoding="utf-8")
    found = load(tmp_path / "m.f", top="top").driven_bits([("child", "w")])[("child", "w")]
    assert found is not None
    assert all(bits == ((0, 0),) for bits in found.values()), found
