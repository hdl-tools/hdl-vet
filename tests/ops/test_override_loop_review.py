"""Review of the loop walk of `override_net` (#53).

The first version of the walk collected the names a driver reads from the right-hand side
of the assignments inside it, and it accepted a driver only when the containing symbol was
a `ContinuousAssign` or a `ProceduralBlock`. Both choices fail **open**: what the walk
cannot see, it accepts. The review found ten routes that make a genuine combinational loop
and reach `vet.finish()` with `ok = True` and the record `proved`, which is the result the
part exists to prevent. Three of them are one hop.

Confirmed by running, on commit 04eea6e:

    always_comb with `if (t) d = ...`   value 'd' -> ACCEPTED   closure(n) = {'t'}
    wire t = n; wire u = t;             value 't' -> ACCEPTED   closure(n) = {}
    and g1 (t, n, sel); buf g2 (u, t);  value 't' -> ACCEPTED   closure(n) = {}

The causes are separate and each is narrow.

A read that is not on the right-hand side of an assignment was invisible: the condition of
an `if`, the selector of a `case`, the bound of a `for`, and the index on the left side.
Reading the whole driver instead of the right-hand sides closes all four at once, and it
fails closed, because a target name of the driver then also counts as a source. That is an
over-refusal, which this part accepts by design.

A declaration that carries its own assignment, `wire t = n;`, has **no driver** in
`getDrivers` at all. `drivers_in` already knows this and adds `sym.initializer` on a
separate line. The walk has to do the same.

A gate primitive drives through a `PrimitiveInstance` containing symbol, which was in
neither accepted kind. The rule is inverted here: follow every driver except an `Instance`
(a child module, which is a stated limit) and a clocked procedural block.

The walk also refused a value it should not. `value_identifiers` returns every name of a
hierarchical path, so `top.u_o.t` yielded `t`, and a module with its own `t` refused the
value with a message claiming a loop that does not exist.

Rules
    R1  A read inside a combinational block that is not on the right of an assignment is
        on the path: the condition of an `if`, the selector of a `case`, the bound of a
        `for`, and the index on the left side of an assignment.
    R2  A net or variable declared with its own assignment is on the path, at any depth.
    R3  A gate primitive on the path is followed. Only a child module instance and a
        clocked procedural block end the walk.
    R4  A hierarchical value is judged by the name it starts with, not by every name of
        its path. A value that names a signal of another instance is not refused because
        this module reuses one of those names.
    R5  The walk is no reason for a new wrong `ok` elsewhere: the cases the first version
        got right stay right. `tests/ops/test_override_loop.py` holds those.
    R6  `value_head_identifiers` is the function rule R4 rests on, so it has a test of its
        own and not only the one through the operation.
    R7  The refusal says what is true. The walk follows an `initial` block, which is not
        combinational logic, so the message says the net drives the name inside the module
        and does not call the route combinational.

Limits, stated
    - A value that reads through a **function call** is not refused. `f()` yields the name
        `f`, and the walk does not enter the body of the function.
    - A value that reads through a **hierarchical name** is not refused, whether the name
        points into another module or into a generate block of this one. The walk follows
        the name the value starts with, and a path leaves this module's own members.
      Both leave a wrong `ok` reachable. Whether such a value should be refused outright
      is a question for the owner, recorded on the issue. Refusing it is the safe
      direction and would stop scripts that work today, so it is not decided here.

Not checked
    `bind`, `defparam`, `specify`, a user-defined primitive, `tranif` and `bufif`,
    `force` and `release`, and a clocking-block drive. The review did not reach them. Rule
    R3 follows every driver that is not a child instance or a clocked block, so a
    primitive of those kinds is expected to be followed, but no test here says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.ops import Vet, VetError
from hdl_vet.slang_adapter import value_head_identifiers, value_identifiers
from override_design import Design

WAIVER = "a test of the loop walk, no proof wanted"

WRAP = (
    "module top (input logic a, input logic sel, input logic clk, output logic y);\n"
    "  m u_m (.a(a), .sel(sel), .clk(clk), .y(y));\n"
    "endmodule\n"
)

# R1: the read is in the condition of an `if`, not on the right of an assignment
M_IF = (
    "module m (input logic a, input logic sel, input logic clk, output logic y);\n"
    "  logic n, t, d;\n"
    "  assign n = a;\n"
    "  assign t = n;\n"
    "  always_comb begin\n"
    "    if (t) d = 1'b1;\n"
    "    else   d = 1'b0;\n"
    "  end\n"
    "  assign y = d | sel | clk;\n"
    "endmodule\n"
)

# R1: the read is the selector of a `case`
M_CASE = (
    "module m (input logic a, input logic sel, input logic clk, output logic y);\n"
    "  logic n, t, d;\n"
    "  assign n = a;\n"
    "  assign t = n;\n"
    "  always_comb case (t)\n"
    "    1'b0: d = 1'b0;\n"
    "    default: d = 1'b1;\n"
    "  endcase\n"
    "  assign y = d | sel | clk;\n"
    "endmodule\n"
)

# R1: the read is the bound of a `for`
M_FOR = (
    "module m (input logic a, input logic sel, input logic clk, output logic y);\n"
    "  logic n; logic [3:0] t; logic [3:0] d;\n"
    "  assign n = a;\n"
    "  assign t = {3'b0, n};\n"
    "  always_comb begin\n"
    "    d = 4'b0;\n"
    "    for (int i = 0; i < t; i++) d[i] = 1'b1;\n"
    "  end\n"
    "  assign y = d[0] | sel | clk;\n"
    "endmodule\n"
)

# R1: the read is the index on the left side
M_INDEX = (
    "module m (input logic a, input logic sel, input logic clk, output logic y);\n"
    "  logic n; logic [3:0] d; logic [1:0] s;\n"
    "  assign n = a;\n"
    "  assign s = {n, sel};\n"
    "  always_comb begin\n"
    "    d = 4'b0;\n"
    "    d[s] = 1'b1;\n"
    "  end\n"
    "  assign y = d[0] | clk;\n"
    "endmodule\n"
)

# R2: the declaration carries its own assignment
M_WIRE = (
    "module m (input logic a, input logic sel, input logic clk, output logic y);\n"
    "  logic n;\n"
    "  wire t = n;\n"
    "  wire u = t;\n"
    "  assign n = a;\n"
    "  assign y = u | sel | clk;\n"
    "endmodule\n"
)

# R3: a gate primitive is on the path
M_GATE = (
    "module m (input logic a, input logic sel, input logic clk, output logic y);\n"
    "  logic n; wire t; wire u;\n"
    "  assign n = a;\n"
    "  and g1 (t, n, sel);\n"
    "  buf g2 (u, t);\n"
    "  assign y = u | clk;\n"
    "endmodule\n"
)

# R5 limit: the value reads through a function call
M_FUNC = (
    "module m (input logic a, input logic sel, input logic clk, output logic y);\n"
    "  logic n, t;\n"
    "  assign n = a;\n"
    "  assign t = n;\n"
    "  function automatic logic f();\n"
    "    return t;\n"
    "  endfunction\n"
    "  assign y = t | sel | clk;\n"
    "endmodule\n"
)

LOOPING = {
    "an if condition": (M_IF, "d"),
    "a case selector": (M_CASE, "d"),
    "a for bound": (M_FOR, "d[0]"),
    "a left side index": (M_INDEX, "d[0]"),
    "a declaration assignment, one hop": (M_WIRE, "t"),
    "a declaration assignment, two hops": (M_WIRE, "u"),
    "a gate primitive, one hop": (M_GATE, "t"),
    "a gate primitive, two hops": (M_GATE, "u"),
}


def build(tmp_path: Path, module_text: str) -> Design:
    return Design(tmp_path / "design", files={"m": module_text, "top": WRAP})


def swap_in(design: Design):
    vet = design.vet()
    vet.punch_port("bypass_en_i", "input", to="top.u_m")
    return vet.module("m")


# --- R1, R2, R3: every route the review found must be refused -------------------------


@pytest.mark.parametrize("label", sorted(LOOPING))
def test_a_loop_the_review_found_is_refused(tmp_path: Path, label):
    """Each of these was accepted before, and the sign-off then reported `proved`."""
    module_text, value = LOOPING[label]
    mod = swap_in(build(tmp_path, module_text))
    with pytest.raises(VetError, match="loop"):
        mod.override_net("n", value, enable="bypass_en_i", waiver=WAIVER)


# --- R4: a hierarchical value is judged by the name it starts with --------------------

OTH = "module oth (input logic i, output logic o);\n  logic t;\n  assign t = i;\n  assign o = t;\nendmodule\n"

M_PLAIN = (
    "module m (input logic a, input logic sel, input logic clk, input logic oz, output logic y);\n"
    "  logic n, t;\n"
    "  assign n = a;\n"
    "  assign t = n;\n"
    "  assign y = t | oz | sel | clk;\n"
    "endmodule\n"
)

TOP_TWO = (
    "module top (input logic a, input logic sel, input logic clk, output logic y);\n"
    "  logic oz;\n"
    "  oth u_o (.i(sel), .o(oz));\n"
    "  m u_m (.a(a), .sel(sel), .clk(clk), .oz(oz), .y(y));\n"
    "endmodule\n"
)


def test_a_name_of_another_instance_is_not_a_loop(tmp_path: Path):
    """`top.u_o.t` is a net of another module. `m` reuses the name `t`, which is no loop.

    Before the fix the walk intersected every name of the path, so the last component `t`
    matched `m`'s own `t` and the value was refused with a message that was simply untrue.
    """
    design = Design(tmp_path / "design", files={"oth": OTH, "m": M_PLAIN, "top": TOP_TWO})
    vet: Vet = design.vet()
    vet.punch_port("bypass_en_i", "input", to="top.u_m")
    try:
        vet.module("m").override_net("n", "top.u_o.t", enable="bypass_en_i", waiver=WAIVER)
    except VetError as e:
        assert "loop" not in str(e), f"refused as a loop, which it is not: {e}"


# --- R5: the stated limits, pinned so that closing one cannot leave the manual behind --


def test_a_loop_through_a_function_call_is_a_limit(tmp_path: Path):
    """`f()` returns `t`, which `n` drives. The walk does not enter the function body.

    This is a stated limit and a reachable wrong `ok`. The test records that it is still
    a limit, so that a later fix has to update the manual with it.
    """
    mod = swap_in(build(tmp_path, M_FUNC))
    mod.override_net("n", "f()", enable="bypass_en_i", waiver=WAIVER)
    assert [(r.op, r.target, r.status) for r in mod.vet.log][-1] == ("override_net", "m.n", "applied")


# --- R6: the head-only identifier function, directly ---------------------------------


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("top.u_o.t", {"top"}),
        ("g.mid", {"g"}),
        ("f()", {"f"}),
        ("a | sel", {"a", "sel"}),
        ("t[0]", {"t"}),
        ("top.u[i].t[j]", {"top", "i", "j"}),
        ("a.b(c)", {"a", "c"}),
        ("x.y[k] + z", {"x", "k", "z"}),
    ],
)
def test_only_the_name_a_path_starts_with_is_read(text, want):
    """Rule R4 rests on this, so it is tested here and not only through the operation.

    A name on the right of a dot belongs to another scope and must not match a member of
    this module. An index inside such a path is read in this scope and must stay.
    """
    assert value_head_identifiers(text) == want


def test_the_full_identifier_function_still_returns_every_name():
    """The counterpart: `value_identifiers` is unchanged, and other call sites rely on it."""
    assert value_identifiers("top.u_o.t") == {"t", "top", "u_o"}


# --- R7: the message does not call a route combinational when it is not ---------------

M_INITIAL = (
    "module m (input logic a, input logic sel, input logic clk, output logic y);\n"
    "  logic n, t, d;\n"
    "  assign n = a;\n"
    "  assign t = n;\n"
    "  initial d = t;\n"
    "  assign y = d | sel | clk;\n"
    "endmodule\n"
)


def test_the_message_does_not_claim_a_combinational_route(tmp_path: Path):
    """An `initial` block is followed, because the walk fails closed, but it is not
    combinational logic. The refusal must not say that it is."""
    mod = swap_in(build(tmp_path, M_INITIAL))
    with pytest.raises(VetError, match="loop") as e:
        mod.override_net("n", "d", enable="bypass_en_i", waiver=WAIVER)
    assert "combinational" not in str(e.value), str(e.value)
    assert "drives inside the module" in str(e.value), str(e.value)
