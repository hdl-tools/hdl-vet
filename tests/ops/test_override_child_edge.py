"""The loop walk and a route through a child instance (#53, the last hole).

The walk follows the drivers of a module's own members. When a net is driven by the output
pin of a child instance, the walk stopped there, so this loop was accepted and the sign-off
reported `proved`:

    kid u_k (.i(t), .o(z));      // z comes out of the child
    assign t = n;                // and t comes from the overridden net
    assign y = z;                // override_net("n", "z") closes the loop at enable 1

Owner decision (2026-11-21): close it with a conservative edge, without entering the child.
When a net is driven by an instance, every name that the instance's non-output pins read
counts as driving that net. For the example, the pin `.i(t)` gives the edge `t -> z`, so `z`
is in the closure of `n` and the value is refused.

Measured before this part, on the design of this file:

    value 'z'  -> accepted      driven by u_k, whose input pin reads t, which n drives
    value 'v'  -> accepted      two hops: assign v = z
    value 'w'  -> accepted      driven by u_k's other output
    value 'q'  -> accepted      driven by u_q, whose pins read only sel

WHY THE EDGE IS DELIBERATELY TOO WIDE

It assumes every output of a child depends on every one of its inputs. In `kid` below that
is false: `o` follows `i` and `p` follows `j`. So the value `w`, which comes from `p` and
therefore from `sel` alone, is refused although no loop runs through it. That is the price
the owner accepted, and rule C4 pins it so it is not mistaken for a defect.

What the edge must NOT become is a blanket refusal of every net a child drives. Rule C5 is
the guard: `u_q` reads only `sel`, so the nets it drives stay acceptable.

Rules
    C1  A loop that closes through the output pin of a child instance is refused.
    C2  The same holds at any depth: a net that reads a net the child drives is refused too.
    C3  A pin that is not an output contributes what it reads. An `inout` pin carries a
        value inward, so it counts.
    C4  The edge is wider than the design demands, on purpose. A net driven by an output
        that does not in fact depend on the loop-carrying input is refused as well.
    C5  It is not a blanket refusal. A net driven by a child whose pins read nothing the
        overridden net drives is still accepted.
    C6  The refusal is the loop one, with the same message, because this is a loop and not
        a value hdl-vet cannot follow.
    C7  A child port that slang models as a `MultiPort`, which a non-ANSI explicit port
        declaration produces, contributes like any other non-output pin. Measured: taking
        the connections from the instance rather than from the child's port list flattens
        such a port into its constituent ports, so each carries its own direction and
        expression. `getPortConnection` raises a TypeError for a `MultiPort`, so the port
        list route cannot reach it at all.
    C8  A child inside an instance array contributes. Measured as working already; the
        test pins it against a regression.
    C9  A child inside a generate loop contributes. Measured as working already; the test
        pins it against a regression.

Limits, stated
    None left for this route. Issue #53 closes with this part. The over-refusal of rule C4
    is the cost, and `limits.tex` says so.

    The value-scope limit is unchanged and lives in `tests/ops/test_override_value_scope.py`:
    a package constant such as `pk::W` is refused because its first name is not a member of
    the module.

Not checked
    A `ref` port, which needs an automatic variable to be useful. An interface port of a
    child: such a port is skipped, and a module with an interface port cannot reach
    `proved` anyway, because the equivalence check reports `error` for one
    (`docs/manual/chapters/limits.tex`, "What the check cannot model").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.ops import VetError
from override_design import Design

WAIVER = "a test of the child edge, no proof wanted"

KID = (
    "module kid (input logic i, input logic j, output logic o, output logic p);\n"
    "  assign o = i;\n"
    "  assign p = j;\n"
    "endmodule\n"
)

BI = "module bi (inout logic io, output logic ob);\n  assign ob = io;\nendmodule\n"

# a non-ANSI explicit port: slang models `p` as a MultiPortSymbol, not a PortSymbol
MPK = "module mpk (.p({pa, pb}), om);\n  input pa, pb;\n  output om;\n  assign om = pa ^ pb;\nendmodule\n"

MX = (
    "module mx (input logic a, input logic sel, output logic y);\n"
    "  logic n, t, z, w, v, q, r, zb, zgen, zmp;\n"
    "  logic [1:0] zarr, parr;\n"
    "  wire  tw;\n"
    "  assign n = a;\n"
    "  assign t = n;\n"
    "  assign tw = t;\n"
    "  kid u_k (.i(t), .j(sel), .o(z), .p(w));\n"
    "  kid u_q (.i(sel), .j(sel), .o(q), .p(r));\n"
    "  bi  u_b (.io(tw), .ob(zb));\n"
    "  kid u_arr [1:0] (.i(t), .j(sel), .o(zarr), .p(parr));\n"
    "  logic pgen;\n"
    "  for (genvar g = 0; g < 1; g++) begin : blk\n"
    "    kid u_in (.i(t), .j(sel), .o(zgen), .p(pgen));\n"
    "  end\n"
    "  mpk u_mp (.p({t, sel}), .om(zmp));\n"
    "  assign v = z;\n"
    "  assign y = z | w | v | q | r | zb | zgen | zmp | zarr[0] | zarr[1]\n"
    "           | parr[0] | parr[1] | pgen;\n"
    "endmodule\n"
)

TOP = "module top (input logic a, input logic sel, output logic y);\n  mx u_m (.a(a), .sel(sel), .y(y));\nendmodule\n"


@pytest.fixture
def design(tmp_path: Path) -> Design:
    return Design(tmp_path / "design", files={"kid": KID, "bi": BI, "mpk": MPK, "mx": MX, "top": TOP})


@pytest.fixture
def mod(design: Design):
    vet = design.vet()
    vet.punch_port("bypass_en_i", "input", to="top.u_m")
    return vet.module("mx")


def over(mod, value: str):
    return mod.override_net("n", value, enable="bypass_en_i", waiver=WAIVER)


def applied(mod) -> tuple[str, str, str]:
    return [(r.op, r.target, r.status) for r in mod.vet.log][-1]


def test_the_design_is_clean(design):
    assert design.vet().design.diagnostics() == []


# --- C1, C2, C6: the route through a child is refused --------------------------------


def test_a_loop_through_a_child_output_is_refused(mod):
    """`z` comes out of `u_k`, whose pin `.i(t)` reads `t`, which `n` drives."""
    with pytest.raises(VetError, match="loop") as e:
        over(mod, "z")
    assert "'z'" in str(e.value), str(e.value)


def test_the_same_route_at_a_further_hop_is_refused(mod):
    """`v` reads `z`, so the route is `n -> t -> (u_k) -> z -> v`."""
    with pytest.raises(VetError, match="loop"):
        over(mod, "v")


def test_the_refusal_is_the_loop_one(mod):
    """Not the "cannot follow" refusal: `z` is a variable of the module, so it is followed."""
    with pytest.raises(VetError) as e:
        over(mod, "z")
    text = str(e.value)
    assert "drives inside the module" in text, text
    assert "cannot follow" not in text, text


# --- C3: a pin that is not an output contributes -------------------------------------


def test_an_inout_pin_contributes_what_it_reads(mod):
    """`zb` comes from `u_b`, whose only non-output pin is the `inout` `.io(tw)`.

    An `inout` port takes a net, not a variable, so `tw` is a wire driven from `t`.
    """
    with pytest.raises(VetError, match="loop"):
        over(mod, "zb")


# --- C4: the accepted over-refusal ---------------------------------------------------


def test_an_output_that_does_not_depend_on_the_loop_is_refused_too(mod):
    """`w` comes from `p`, and in `kid` `p` follows `j`, which reads `sel`, not `t`.

    No loop runs through `w`. The edge refuses it anyway, because it assumes every output
    depends on every input. That is the stated cost of not entering the child.
    """
    with pytest.raises(VetError, match="loop"):
        over(mod, "w")


# --- C5: not a blanket refusal -------------------------------------------------------


@pytest.mark.parametrize("value", ["q", "r"])
def test_a_net_from_a_child_that_reads_nothing_relevant_is_accepted(mod, value):
    """`u_q` reads only `sel`. If the edge were a blanket refusal of child-driven nets,
    this would fail, and the operation would be useless on any design with submodules."""
    over(mod, value)
    assert applied(mod) == ("override_net", "mx.n", "applied")


def test_a_plain_unrelated_signal_is_still_accepted(mod):
    over(mod, "sel")
    assert applied(mod) == ("override_net", "mx.n", "applied")


# --- C7, C8, C9: the other shapes a child can take ----------------------------------


def test_a_loop_through_a_multiport_child_is_refused(mod):
    """`zmp` comes from `u_mp`, whose `.p({t, sel})` is a non-ANSI explicit port.

    slang models that port as a `MultiPort`, and `getPortConnection` raises a TypeError
    for one, so reading the child's port list cannot reach it. Taking the connections
    from the instance flattens it into `pa` and `pb`, and `pa` reads `t`.
    """
    with pytest.raises(VetError, match="loop"):
        over(mod, "zmp")


def test_a_loop_through_a_child_in_an_instance_array_is_refused(mod):
    with pytest.raises(VetError, match="loop"):
        over(mod, "zarr[0]")


def test_a_loop_through_a_child_in_a_generate_loop_is_refused(mod):
    with pytest.raises(VetError, match="loop"):
        over(mod, "zgen")
