"""What the adversarial review of the logic calls found (phase 6, part 5; issue #75).

The rules L1 to L10 are in tests/ops/test_logic_ops.py. The review found one wrong "proved" and one
expression that was sorted as wiring and is logic.

RULES

M1  An undefined value in the input design. The engine can answer "proved" when a net of the input design
    has no driver: the edited side may take any value for it. Such an answer counts only when nothing
    outside the module can see the undefined value. It is "error" when the undefined value can reach a
    port of the module, directly, through a flop, or through a child. A child that stays a black box
    hides the way from its input pin to its outputs, so a pin of such a child counts as a port does.
    A net that nothing outside can see (it feeds nothing, or a flop that no port reads) does not count,
    and neither does a member of a variable of the module (`oldest.id`): its first name is no instance.
    No second comparison is run for this question: on a large module it does not end in time (corpus
    test C5 of tests/ops/test_override_corpus.py has such a module, and stays "proved").
M2  M1 holds for every comparison of the sign-off: a logic call, a raw splice and `override_net`. The
    same edit gets the same answer, whatever wrote it.
M3  A name is wiring only when the module resolves it to a net, a variable, a port or a parameter. A name
    of a `let` declaration is a call with no parentheses, so it is logic. A name that the reader cannot
    resolve is logic.

The second, narrow review found two more:

M4  Two undefined values of the input design are independent of each other. An output that reads two
    undriven bits is undefined also when it comes out the same with all of them 0 and with all of them 1
    (`n[0] ^ n[1]`, `u1 & ~u2`). M1 holds for it: a port or a pin of a boxed child that such an
    expression reaches makes the answer "error".
M5  A name that is declared only inside a generate block does not make the same name wiring at the level
    of the module. With `let f = ...;` in the module and `logic f;` in a generate block, `f` is logic.

The third review found that the engine names only the first nets where an undefined value shows:

M6  The report of undefined values names the first nets that differ. What lies behind them is taken as
    equal, so a port behind a named net is not named itself. A "proved" is therefore "error" when, in the
    netlist of the input design, a named net leads to an output of the module or to an input pin of a
    child that stayed a black box, through any number of cells and flops. This is decided from the
    netlist, not from the names of instances, so a child in a generate block counts. A named net that
    cannot be found in the netlist makes the answer "error". A named net that leads to nothing outside
    does not count. This replaces the comparison of names that M1 began with.
M7  A select with a variable index that can leave the range of its vector (`v[i +: 1]`, `v` of 3 bits,
    `i` of 2 bits) gives an undefined value that the report cannot see. Its result counts as a named net
    does in M6. A select whose index cannot leave the range does not count.

STATED LIMITS

- M6 follows the netlist and not the logic: an undefined value that a net leads to but that can never
  pass (it is masked by other logic) still gives "error". M7 looks at the widths only, so a select whose
  index stays in range by the logic that drives it still gives "error" when its width allows more.

- A member of a struct (`s.v`) is sorted as logic, as every name with a `.` is. So is a select whose
  index is a constant of a package or a name of an enum. Such a call costs a proof, or a waiver when
  the engine cannot prove the module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet import equiv
from hdl_vet.ops import VetError
from override_design import Design, needs_yosys

pytestmark = [pytest.mark.equiv]

CHILD = (
    "module child (input logic clk_i, input logic en_i, output logic q_o);\n"
    "  always_ff @(posedge clk_i) q_o <= en_i;\n"
    "endmodule\n"
)
TOP = (
    "module top (input logic clk_i, input logic a_i, output logic q_o, output logic y_o);\n"
    "  mid u_m (.clk_i(clk_i), .a_i(a_i), .q_o(q_o), .y_o(y_o));\n"
    "endmodule\n"
)
HEAD = "module mid (input logic clk_i, input logic a_i, output logic q_o, output logic y_o);\n"
# The undriven net `n` feeds a pin of a child that does not change.
BOX = HEAD + ("  logic n;\n  child u_c (.clk_i(clk_i), .en_i(n), .q_o(q_o));\n  assign y_o = a_i;\nendmodule\n")
# The undriven net `n` feeds a flop of the module that drives a port.
FLOP = HEAD + (
    "  logic n;\n"
    "  child u_c (.clk_i(clk_i), .en_i(a_i), .q_o(q_o));\n"
    "  always_ff @(posedge clk_i) y_o <= n;\n"
    "endmodule\n"
)
# The undriven net `n` feeds a flop that nothing reads.
DEAD = HEAD + (
    "  logic n;\n"
    "  logic d;\n"
    "  child u_c (.clk_i(clk_i), .en_i(a_i), .q_o(q_o));\n"
    "  always_ff @(posedge clk_i) d <= n;\n"
    "  assign y_o = a_i;\n"
    "endmodule\n"
)
LET = HEAD + (
    "  let f = ~a_i;\n  logic n;\n  child u_c (.clk_i(clk_i), .en_i(a_i), .q_o(q_o));\n  assign y_o = a_i;\nendmodule\n"
)
# Two undriven bits reach a port, and the port is the same with both 0 and with both 1.
XOR = HEAD + (
    "  logic [1:0] n;\n  child u_c (.clk_i(clk_i), .en_i(a_i), .q_o(q_o));\n  assign y_o = n[0] ^ n[1];\nendmodule\n"
)
ANDNOT = HEAD + (
    "  logic u1;\n"
    "  logic u2;\n"
    "  child u_c (.clk_i(clk_i), .en_i(a_i), .q_o(q_o));\n"
    "  assign y_o = u1 & ~u2;\n"
    "endmodule\n"
)
# The same two nets reach a pin of a child that does not change.
BOXPAIR = HEAD + (
    "  logic u1;\n"
    "  logic u2;\n"
    "  child u_c (.clk_i(clk_i), .en_i(u1 & ~u2), .q_o(q_o));\n"
    "  assign y_o = a_i;\n"
    "endmodule\n"
)
LETGEN = HEAD + (
    "  let f = ~a_i;\n"
    "  logic n;\n"
    "  if (1) begin : g\n"
    "    logic f;\n"
    "  end\n"
    "  child u_c (.clk_i(clk_i), .en_i(a_i), .q_o(q_o));\n"
    "  assign y_o = a_i;\n"
    "endmodule\n"
)


def build(tmp_path: Path, mid: str) -> Design:
    return Design(tmp_path / "design", {"child": CHILD, "mid": mid, "top": TOP})


def one(result):
    (record,) = result.equivalence
    return record


# --- M1: an undefined value that the outside can see ----------------------------------------------


@needs_yosys
def test_add_assign_to_a_net_that_a_child_reads_is_never_proved(tmp_path):
    vet = build(tmp_path, BOX).vet()
    vet.module("mid").add_assign("n", "~a_i")
    result = vet.finish()
    assert one(result).status in ("refuted", "error"), one(result)
    assert not result.ok


@needs_yosys
def test_connect_in_the_place_of_an_undriven_net_is_never_proved(tmp_path):
    vet = build(tmp_path, BOX).vet()
    vet.module("mid").instance("u_c").connect("en_i", "~a_i", was="n")
    result = vet.finish()
    assert one(result).status in ("refuted", "error"), one(result)
    assert not result.ok


@needs_yosys
def test_add_assign_to_a_net_that_a_flop_of_a_port_reads_is_never_proved(tmp_path):
    vet = build(tmp_path, FLOP).vet()
    vet.module("mid").add_assign("n", "~a_i")
    result = vet.finish()
    assert one(result).status in ("refuted", "error"), one(result)
    assert not result.ok


@needs_yosys
def test_an_undefined_net_that_nothing_outside_sees_does_not_count(tmp_path):
    vet = build(tmp_path, DEAD).vet()
    vet.module("mid").add_assign("n", "~a_i")
    result = vet.finish()
    assert one(result).status == "proved", one(result)
    assert result.ok, result.report()


# --- M2: the same answer for a raw splice ---------------------------------------------------------


@needs_yosys
@pytest.mark.parametrize("mid", [BOX, FLOP], ids=["child", "flop"])
def test_a_raw_splice_that_drives_an_undriven_net_is_never_proved(tmp_path, mid):
    design = build(tmp_path, mid)
    vet = design.vet()
    design.replace(vet, "mid", b"  logic n;\n", b"  logic n;\n  assign n = ~a_i;\n")
    result = vet.finish()
    assert one(result).status in ("refuted", "error"), one(result)
    assert not result.ok


@needs_yosys
def test_a_raw_splice_beside_an_undefined_net_that_nothing_sees_is_proved(tmp_path):
    design = build(tmp_path, DEAD)
    vet = design.vet()
    design.replace(vet, "mid", b"assign y_o = a_i;", b"assign y_o = a_i & 1'b1;")
    result = vet.finish()
    assert one(result).status == "proved", one(result)
    assert result.ok, result.report()


# --- M3: a name that is a call --------------------------------------------------------------------


def test_the_name_of_a_let_is_logic(tmp_path, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    design = build(tmp_path, LET)
    vet = design.vet()
    with pytest.raises(VetError, match=r"hdl-vet\[equiv\]"):
        vet.module("mid").instance("u_c").connect("en_i", "f", was="a_i")
    with pytest.raises(VetError, match=r"hdl-vet\[equiv\]"):
        vet.module("mid").add_assign("n", "{f, f}")
    with pytest.raises(VetError, match=r"hdl-vet\[equiv\]"):
        vet.module("mid").add_assign("n", "a_i[f]")
    assert design.changed(vet) == set()


@needs_yosys
def test_the_name_of_a_let_is_compared(tmp_path):
    vet = build(tmp_path, LET).vet()
    vet.module("mid").instance("u_c").connect("en_i", "f", was="a_i")
    result = vet.finish()
    assert one(result).status == "refuted", one(result)
    assert one(result).operations == ["connect mid.u_c.en_i"]
    assert not result.ok


# --- M4: two undefined values are independent -----------------------------------------------------


@needs_yosys
def test_two_undriven_bits_that_cancel_when_equal_are_never_proved(tmp_path):
    vet = build(tmp_path, XOR).vet()
    vet.module("mid").add_assign("n", "{a_i, 1'b0} | 2'b00")
    result = vet.finish()
    assert one(result).status in ("refuted", "error"), one(result)
    assert not result.ok


@needs_yosys
def test_two_undriven_nets_that_cancel_when_equal_are_never_proved(tmp_path):
    vet = build(tmp_path, ANDNOT).vet()
    vet.module("mid").add_assign("u1", "a_i & 1'b1")
    vet.module("mid").add_assign("u2", "a_i")
    result = vet.finish()
    assert one(result).status in ("refuted", "error"), one(result)
    assert not result.ok


@needs_yosys
@pytest.mark.parametrize(
    ("mid", "old", "new"),
    [
        (XOR, b"  logic [1:0] n;\n", b"  logic [1:0] n;\n  assign n = {a_i, 1'b0};\n"),
        (ANDNOT, b"  logic u2;\n", b"  logic u2;\n  assign u1 = a_i;\n  assign u2 = a_i;\n"),
        (BOXPAIR, b"  logic u2;\n", b"  logic u2;\n  assign u1 = a_i;\n  assign u2 = a_i;\n"),
    ],
    ids=["xor", "andnot", "pin-of-a-child"],
)
def test_a_raw_splice_that_drives_two_undriven_nets_is_never_proved(tmp_path, mid, old, new):
    design = build(tmp_path, mid)
    vet = design.vet()
    design.replace(vet, "mid", old, new)
    result = vet.finish()
    assert one(result).status in ("refuted", "error"), one(result)
    assert not result.ok


# --- M5: a name of a generate block ---------------------------------------------------------------


def test_a_variable_of_a_generate_block_does_not_hide_a_let(tmp_path, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    design = build(tmp_path, LETGEN)
    vet = design.vet()
    with pytest.raises(VetError, match=r"hdl-vet\[equiv\]"):
        vet.module("mid").instance("u_c").connect("en_i", "f", was="a_i")
    with pytest.raises(VetError, match=r"hdl-vet\[equiv\]"):
        vet.module("mid").add_assign("n", "f")
    assert design.changed(vet) == set()


# --- M6: what lies behind the first undefined net -------------------------------------------------

U_C = "  child u_c (.clk_i(clk_i), .en_i(a_i), .q_o(q_o));\n"
# The undriven net `n` reaches the port through a second named net.
WIRE = HEAD + ("  logic n;\n  logic m;\n" + U_C + "  assign m = ~n;\n  assign y_o = m & a_i;\nendmodule\n")
# ... through two flops.
SHIFT = HEAD + (
    "  logic n;\n"
    "  logic [1:0] s;\n" + U_C + "  always_ff @(posedge clk_i) s <= {s[0], n};\n"
    "  assign y_o = s[1];\n"
    "endmodule\n"
)
# ... through two flops to a pin of a child that does not change.
SHIFTBOX = HEAD + (
    "  logic n;\n"
    "  logic [1:0] s;\n"
    "  child u_c (.clk_i(clk_i), .en_i(s[1]), .q_o(q_o));\n"
    "  always_ff @(posedge clk_i) s <= {s[0], n};\n"
    "  assign y_o = a_i;\n"
    "endmodule\n"
)
# The second named net feeds nothing that the outside sees.
WIREDEAD = HEAD + ("  logic n;\n  logic m;\n" + U_C + "  assign m = ~n;\n  assign y_o = a_i;\nendmodule\n")
# The child that reads the undriven net is in a generate block.
GENBOX = HEAD + (
    "  logic n;\n"
    "  if (1) begin : g\n"
    "    child u_c (.clk_i(clk_i), .en_i(n), .q_o(q_o));\n"
    "  end\n"
    "  assign y_o = a_i;\n"
    "endmodule\n"
)


@needs_yosys
@pytest.mark.parametrize("mid", [WIRE, SHIFT, SHIFTBOX, GENBOX], ids=["net", "flops", "flops-to-child", "generate"])
def test_an_undefined_value_behind_other_nets_is_never_proved(tmp_path, mid):
    vet = build(tmp_path, mid).vet()
    vet.module("mid").add_assign("n", "~a_i")
    result = vet.finish()
    assert one(result).status in ("refuted", "error"), one(result)
    assert not result.ok


@needs_yosys
def test_an_undefined_value_behind_a_net_that_nothing_sees_does_not_count(tmp_path):
    vet = build(tmp_path, WIREDEAD).vet()
    vet.module("mid").add_assign("n", "~a_i")
    result = vet.finish()
    assert one(result).status == "proved", one(result)
    assert result.ok, result.report()


# --- M7: a select that can leave the range --------------------------------------------------------

SELECT = "  logic [2:0] v;\n  logic [1:0] i;\n  assign v = {a_i, a_i, ~a_i};\n  assign i = {a_i, a_i};\n"
# `i` is 0 or 3, and `v` has no bit 3.
RANGE = HEAD + SELECT + U_C + "  assign y_o = v[i +: 1];\nendmodule\n"
RANGEFLOP = HEAD + (
    SELECT + "  logic r;\n" + U_C + "  always_ff @(posedge clk_i) r <= v[i +: 1];\n  assign y_o = r;\nendmodule\n"
)
RANGEBOX = HEAD + (
    SELECT + "  child u_c (.clk_i(clk_i), .en_i(v[i +: 1]), .q_o(q_o));\n  assign y_o = a_i;\nendmodule\n"
)
# Every value of `i` is inside `w`.
INSIDE = HEAD + (
    "  logic [3:0] w;\n"
    "  logic [1:0] i;\n"
    "  assign w = {a_i, a_i, ~a_i, a_i};\n"
    "  assign i = {a_i, a_i};\n" + U_C + "  assign y_o = w[i +: 1];\n"
    "endmodule\n"
)


@needs_yosys
@pytest.mark.parametrize(
    ("mid", "old", "new"),
    [
        (RANGE, b"assign y_o = v[i +: 1];", b"assign y_o = 1'b1;"),
        (RANGEFLOP, b"r <= v[i +: 1];", b"r <= 1'b1;"),
        (RANGEBOX, b".en_i(v[i +: 1])", b".en_i(a_i | 1'b1)"),
    ],
    ids=["port", "flop", "pin-of-a-child"],
)
def test_a_select_that_can_leave_the_range_is_never_proved(tmp_path, mid, old, new):
    design = build(tmp_path, mid)
    vet = design.vet()
    design.replace(vet, "mid", old, new)
    result = vet.finish()
    assert one(result).status in ("refuted", "error"), one(result)
    assert not result.ok


@needs_yosys
def test_a_select_that_stays_in_the_range_is_proved(tmp_path):
    design = build(tmp_path, INSIDE)
    vet = design.vet()
    design.replace(vet, "mid", b"assign y_o = w[i +: 1];", b"assign y_o = w[i +: 1] & 1'b1;")
    result = vet.finish()
    assert one(result).status == "proved", one(result)
    assert result.ok, result.report()
