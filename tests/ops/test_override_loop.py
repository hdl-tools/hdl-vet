"""`override_net` and a combinational loop through other nets (#53).

`override_net` writes `assign net = enable ? value : keep;`. When the value reads a net
that the overridden net drives, the edited design has a combinational loop while the
enable is 1. Before this part hdl-vet refused only the direct case, a value that reads the
net itself. Everything further was accepted, and the sign-off said `ok`, because the
comparison ties the enable to 0 and never sees the loop. A wrong `ok` is the worst result
this tool can give, so the walk below closes the reachable part of that hole.

Measured on the design of this file before the part (pyslang 12.1.0):

    value 'n' -> refused        the direct case, already refused
    value 't' -> accepted       assign t = n;                       one hop
    value 'u' -> accepted       assign u = t;                       two hops
    value 'c' -> accepted       always_comb c = t & a;
    value 'q' -> accepted       always_ff @(posedge clk) q <= t;    correct, a flop
    value 'z' -> accepted       k u_k (.i(t), .o(z));               a child, not seen

Owner decision (2026-11-20)

The walk follows the module-level combinational drivers inside the one module and refuses
on reaching the net. An over-refusal is acceptable here and a missed loop is not. Two
routes stay open, and they are stated limits below rather than rules.

Rules
    L1  A value that reads the net itself is refused, as before.
    L2  A value that reads a net which the net drives through `assign` statements is
        refused, at any depth.
    L3  A combinational procedural block is part of such a path: `always_comb`,
        `always_latch`, `always @(*)` and `always @(a or b)`.
    L4  A clocked procedural block ends the path, because a flop is not a combinational
        loop. A block counts as clocked only when every event of it carries an edge. In
        doubt the block counts as combinational, which refuses rather than accepts.
    L5  The walk stays inside the one module. A net that the output pin of a child
        instance drives ends the path, and so does a name that the module does not own.
    L6  A value that reads no net on such a path is accepted. The operation applies.
    L7  The refusal starts with `override_net <module>: `, and names the net, the name in
        the value that closes the loop, and the word loop.
    L8  The refusal happens when the operation is called. Nothing is queued in `vet.log`
        and no file changes.
    L9  The walk covers every elaborated body of the module. A loop at one
        parameterisation is enough to refuse the whole operation.

Limits, stated
    - A loop that closes through the output pin of a child instance is not refused (L5).
      The walk does not enter the child, because the names inside it belong to another
      namespace. Rule L5 pins this boundary, and `test_a_loop_through_a_child_is_a_limit`
      is the test that says the limit is still a limit.
    - A loop that closes through a hierarchical reference from another module is not
      refused, for the same reason.
    - A branch of a generate that the elaboration does not take cannot close a loop here,
      because slang reports no driver for it. This is not a limit of the walk. A parameter
      change that takes that branch is a change of configuration, which the manual already
      covers under "One configuration".

Not checked
    Whether the edited design with the enable at 1 is free of loops that the design
    already had. The walk answers only about the net that this operation overrides.
    Every test here passes a waiver, so the file needs no Yosys, and no refusal of this
    file can be the one for a missing Yosys.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.ops import Vet, VetError
from override_design import Design

WAIVER = "a test of the loop walk, no proof wanted"

K = "module k (input logic i, output logic o);\n  assign o = i;\nendmodule\n"

M = (
    "module m (input logic a, input logic sel, input logic clk, output logic y);\n"
    "  logic n, t, u, c, e, w, q, r, z;\n"
    "  assign n = a;\n"
    "  assign t = n;\n"
    "  assign u = t;\n"
    "  always_comb c = t & a;\n"
    "  always @(*) e = t;\n"
    "  always @(a or t) w = t & a;\n"
    "  always_ff @(posedge clk) q <= t;\n"
    "  always @(posedge clk) r <= t;\n"
    "  k u_k (.i(t), .o(z));\n"
    "  assign y = u | sel | c | e | w | q | r | z;\n"
    "endmodule\n"
)

TOP = (
    "module top (input logic a, input logic sel, input logic clk, output logic y);\n"
    "  m u_m (.a(a), .sel(sel), .clk(clk), .y(y));\n"
    "endmodule\n"
)

# L9: `m2` is elaborated twice. The generate branch puts `n` on the path of `t` at K=1 only.
M2 = (
    "module m2 #(parameter int KP = 0) (input logic a, input logic sel, output logic y);\n"
    "  logic n, t;\n"
    "  assign n = a;\n"
    "  if (KP) begin : g\n"
    "    assign t = n;\n"
    "  end else begin : g\n"
    "    assign t = sel;\n"
    "  end\n"
    "  assign y = t;\n"
    "endmodule\n"
)

TOP2 = (
    "module top (input logic a, input logic sel, output logic y0, output logic y1);\n"
    "  m2 #(.KP(0)) u_a (.a(a), .sel(sel), .y(y0));\n"
    "  m2 #(.KP(1)) u_b (.a(a), .sel(sel), .y(y1));\n"
    "endmodule\n"
)


@pytest.fixture
def design(tmp_path: Path) -> Design:
    return Design(tmp_path / "design", files={"k": K, "m": M, "top": TOP})


@pytest.fixture
def mod(design: Design):
    vet = design.vet()
    vet.punch_port("bypass_en_i", "input", to="top.u_m")
    return vet.module("m")


def over(mod, value: str, net: str = "n"):
    return mod.override_net(net, value, enable="bypass_en_i", waiver=WAIVER)


# --- L1, L2: the net itself and the assign chain --------------------------------------


def test_the_net_itself_is_refused(mod):
    with pytest.raises(VetError, match="loop"):
        over(mod, "n")


@pytest.mark.parametrize("value", ["t", "u", "y"])
def test_a_net_the_override_drives_through_assigns_is_refused(mod, value):
    """One hop, two hops and three hops. Each was a wrong `ok` before this part."""
    with pytest.raises(VetError, match="loop"):
        over(mod, value)


def test_the_loop_is_refused_inside_a_larger_expression(mod):
    with pytest.raises(VetError, match="loop"):
        over(mod, "(t & sel) | 1'b0")


# --- L3: a combinational block is on the path ----------------------------------------


@pytest.mark.parametrize("value", ["c", "e", "w"])
def test_a_combinational_block_is_part_of_the_path(mod, value):
    """`always_comb`, `always @(*)` and `always @(a or t)` all carry the loop."""
    with pytest.raises(VetError, match="loop"):
        over(mod, value)


# --- L4: a clocked block ends the path -----------------------------------------------


@pytest.mark.parametrize("value", ["q", "r"])
def test_a_clocked_block_ends_the_path(mod, value):
    """`always_ff @(posedge clk)` and `always @(posedge clk)`: a flop is not a loop."""
    over(mod, value)
    assert [(r.op, r.target, r.status) for r in mod.vet.log][-1] == ("override_net", "m.n", "applied")


# --- L5: the walk stays in the module ------------------------------------------------


def test_a_loop_through_a_child_is_a_limit(mod):
    """`z` is driven by the pin of `u_k`, which reads `t`, which `n` drives.

    That is a real loop and hdl-vet does not refuse it. The limit is stated in the
    docstring and in the manual. This test says the limit is still a limit, so that
    widening the walk cannot leave the manual behind.
    """
    over(mod, "z")
    assert [(r.op, r.target, r.status) for r in mod.vet.log][-1] == ("override_net", "m.n", "applied")


# --- L6: no loop, so the operation applies -------------------------------------------


@pytest.mark.parametrize("value", ["a", "sel", "1'b0"])
def test_a_value_with_no_loop_is_accepted(mod, value):
    over(mod, value)
    assert [(r.op, r.target, r.status) for r in mod.vet.log][-1] == ("override_net", "m.n", "applied")


# --- L7: the message -----------------------------------------------------------------


def test_the_message_names_the_operation_the_net_and_the_name(mod):
    with pytest.raises(VetError) as e:
        over(mod, "u")
    text = str(e.value)
    assert text.startswith("override_net m: "), text
    assert "'n'" in text and "'u'" in text, text
    assert "loop" in text, text


# --- L8: nothing is queued and nothing is written ------------------------------------


def test_nothing_is_queued_or_written_when_the_loop_is_refused(mod, design):
    """The log must not grow and the text must not change.

    The assertion compares the whole log against itself before the call, rather than
    naming what the fixture put there. `punch_port` writes three records, not one, and an
    earlier version of this test named one. It is the refused operation that must leave
    no trace, so the log before the call is the only right thing to compare against.
    """
    before_log = [(r.op, r.target, r.status) for r in mod.vet.log]
    before_text = design.text(mod.vet, "m")
    with pytest.raises(VetError):
        over(mod, "t")
    assert [(r.op, r.target, r.status) for r in mod.vet.log] == before_log
    assert "override_net" not in [r.op for r in mod.vet.log]
    assert design.text(mod.vet, "m") == before_text


# --- L9: every elaborated body -------------------------------------------------------


def test_a_loop_at_one_parameterisation_refuses_the_whole_operation(tmp_path: Path):
    """`m2` is elaborated with KP=0, where there is no loop, and with KP=1, where there is."""
    design = Design(tmp_path / "design", files={"m2": M2, "top": TOP2})
    vet: Vet = design.vet()
    vet.punch_port("bypass_en_i", "input", to="top.u_a", all_instances=True)
    with pytest.raises(VetError, match="loop"):
        vet.module("m2", instances=2).override_net("n", "t", enable="bypass_en_i", waiver=WAIVER)
