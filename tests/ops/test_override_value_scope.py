"""`override_net` and a value hdl-vet cannot analyse (#53).

The loop walk follows the drivers of a module's own members. It judges a value by the name
each path in it starts with, so a value that reads through something the walk cannot follow
slips past it, makes a real combinational loop, and reaches the sign-off as `proved`.

Owner decision (2026-11-21): refuse such a value. An error is always acceptable when in
doubt, and a wrong `proved` is the worst result this tool gives. It was accepted that this
stops scripts that work today.

WHAT DECIDES, MEASURED

A `ScopedName` in the value is the wrong signal. Measured on the design below:

    heads('t')        = ['t']     -> Variable       analysable
    heads('pr.hi')    = ['pr']    -> Variable       analysable, a struct member
    heads('$bits(t)') = ['t']     -> Variable       analysable, a system call
    heads('P')        = ['P']     -> Parameter      analysable, a constant
    heads('g.mid')    = ['g']     -> GenerateBlock  not analysable
    heads('f()')      = ['f']     -> Subroutine     not analysable
    heads('u_k.o')    = ['u_k']   -> Instance       not analysable
    heads('pk::W')    = ['pk']    -> None           not analysable

So `pr.hi` and `$bits(t)` are dotted or called and are fully covered by the existing walk,
because their head is a signal of this module. Refusing every dotted value would have been
a wide over-refusal of correct scripts. The kind of the symbol the head resolves to is what
decides.

Rules
    S1  A value whose every head resolves to a net, a variable or a parameter of the module
        is analysable. The loop walk judges it, exactly as before.
    S2  A value with a head that resolves to an instance, a generate block or a subprogram
        is refused. hdl-vet cannot tell what such a value reads.
    S3  A value with a head that resolves to nothing in the module is refused.
    S4  A struct member and a system call are not refused. Their head is a signal of the
        module, so rule S1 covers them.
    S5  The refusal happens when the operation is called, with nothing queued and no file
        changed. The message names the operation, the module and the head it cannot follow.

Limits, stated
    - A **package constant**, such as `pk::W`, is refused by rule S3, although it is a
      compile-time constant and can never be part of a loop. Telling it apart would mean
      resolving names outside the module, which is what the walk deliberately does not do.
      `test_a_package_constant_is_refused_as_a_limit` pins it.
    - A loop that closes through a **child module** in its common form is still not
      refused, and still reaches `proved`:

          kid u_k (.i(t), .o(z));
          assign y = z;

      The value `z` has the head `z`, a variable, so rule S1 judges it analysable, and the
      walk then stops because the driver of `z` is an instance. A conservative edge from
      every input pin of an instance to every net its output pins drive would close this.
      That widens refusals a long way and is a question for the owner, recorded on the
      issue. `test_a_loop_through_a_child_net_is_still_a_limit` pins the gap.

Not checked
    A class property or a static method reached with `::`. A value naming an interface
    signal. Both resolve to nothing in the module body, so rule S3 refuses them, but no
    test here says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.ops import VetError
from override_design import Design

WAIVER = "a test of the value scope, no proof wanted"

KID = "module kid (input logic i, output logic o);\n  assign o = i;\nendmodule\n"

MX = (
    "package pk;\n"
    "  localparam int W = 4;\n"
    "endpackage\n"
    "module mx #(parameter int P = 1) (input logic a, input logic sel, output logic y);\n"
    "  typedef struct packed { logic hi; logic lo; } pair_t;\n"
    "  logic n, t, z;\n"
    "  pair_t pr;\n"
    "  assign n = a;\n"
    "  assign t = n;\n"
    "  assign pr = '{hi: t, lo: sel};\n"
    "  kid u_k (.i(t), .o(z));\n"
    "  if (1) begin : g\n"
    "    logic mid;\n"
    "    assign mid = t;\n"
    "  end\n"
    "  function automatic logic f();\n"
    "    return t;\n"
    "  endfunction\n"
    "  assign y = z | pr.hi | g.mid | P[0] | pk::W[0];\n"
    "endmodule\n"
)

TOP = "module top (input logic a, input logic sel, output logic y);\n  mx u_m (.a(a), .sel(sel), .y(y));\nendmodule\n"


@pytest.fixture
def design(tmp_path: Path) -> Design:
    return Design(tmp_path / "design", files={"kid": KID, "mx": MX, "top": TOP})


@pytest.fixture
def mod(design: Design):
    vet = design.vet()
    vet.punch_port("bypass_en_i", "input", to="top.u_m")
    return vet.module("mx")


def over(mod, value: str):
    return mod.override_net("n", value, enable="bypass_en_i", waiver=WAIVER)


def test_the_design_is_clean(design):
    assert design.vet().design.diagnostics() == []


# --- S1 and S4: an analysable value is judged by the walk, as before -----------------


def test_a_plain_signal_is_still_judged_by_the_walk(mod):
    """`t` is driven by `n`, so this is the loop refusal, not the new one."""
    with pytest.raises(VetError, match="loop"):
        over(mod, "t")


def test_a_signal_with_no_loop_is_still_accepted(mod):
    over(mod, "sel")
    assert [(r.op, r.target, r.status) for r in mod.vet.log][-1] == ("override_net", "mx.n", "applied")


def test_a_parameter_is_analysable(mod):
    over(mod, "P[0]")
    assert [(r.op, r.target, r.status) for r in mod.vet.log][-1] == ("override_net", "mx.n", "applied")


def test_a_struct_member_is_not_refused_for_its_dot(mod):
    """`pr.hi` has the head `pr`, a variable of the module, so rule S1 covers it.

    `pr` is driven from `t`, which `n` drives, so the loop walk refuses it. What must not
    happen is a refusal for being dotted: the message has to be the loop one.
    """
    with pytest.raises(VetError, match="loop"):
        over(mod, "pr.hi")


def test_a_system_call_is_not_refused_for_its_parentheses(mod):
    """`$bits(sel)` has the head `sel`, so it is analysable and there is no loop."""
    over(mod, "$bits(sel) == 1")
    assert [(r.op, r.target, r.status) for r in mod.vet.log][-1] == ("override_net", "mx.n", "applied")


# --- S2: an instance, a generate block, a subprogram ---------------------------------


@pytest.mark.parametrize(
    ("value", "head"),
    [("u_k.o", "u_k"), ("g.mid", "g"), ("f()", "f")],
)
def test_a_value_hdl_vet_cannot_follow_is_refused(mod, value, head):
    with pytest.raises(VetError) as e:
        over(mod, value)
    text = str(e.value)
    assert text.startswith("override_net mx: "), text
    assert repr(head) in text, f"the message must name {head!r}: {text}"
    assert "loop" not in text, f"this is not a loop refusal: {text}"


# --- S3: a head that resolves to nothing ---------------------------------------------


def test_a_name_of_another_module_is_refused(mod):
    with pytest.raises(VetError) as e:
        over(mod, "top.u_m.t")
    assert "loop" not in str(e.value), str(e.value)
    assert "'top'" in str(e.value), str(e.value)


def test_a_package_constant_is_refused_as_a_limit(mod):
    """A stated limit. `pk::W` is a compile-time constant and can never be in a loop.

    Its head `pk` resolves to nothing in the module, so rule S3 refuses it. This test
    records that the over-refusal is still there, so closing it cannot leave the manual
    behind.
    """
    with pytest.raises(VetError) as e:
        over(mod, "pk::W[0]")
    assert "loop" not in str(e.value), str(e.value)


# --- S5: nothing queued, nothing written ---------------------------------------------


def test_nothing_is_queued_or_written_when_the_value_is_refused(mod, design):
    before_log = [(r.op, r.target, r.status) for r in mod.vet.log]
    before_text = design.text(mod.vet, "mx")
    with pytest.raises(VetError):
        over(mod, "f()")
    assert [(r.op, r.target, r.status) for r in mod.vet.log] == before_log
    assert design.text(mod.vet, "mx") == before_text


# --- the limit that stays open -------------------------------------------------------


def test_a_loop_through_a_child_net_is_still_a_limit(mod):
    """`z` is driven by the pin of `u_k`, which reads `t`, which `n` drives.

    That is a real loop and hdl-vet does not refuse it, because `z` is a variable of the
    module and the walk stops at the instance driver. The limit is in the docstring and in
    the manual. This test says it is still a limit.
    """
    over(mod, "z")
    assert [(r.op, r.target, r.status) for r in mod.vet.log][-1] == ("override_net", "mx.n", "applied")
