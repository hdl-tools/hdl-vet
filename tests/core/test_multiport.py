"""A child with a non-ANSI explicit port, which slang models as a `MultiPort` (#53).

A port declared in the non-ANSI explicit form is not a `PortSymbol`:

    module mpk (.p({pa, pb}), om);   // slang: p is a MultiPortSymbol
      input pa, pb;
      output om;
      assign om = pa ^ pb;
    endmodule

`_snap_of` in `src/hdl_vet/slang_adapter/snapshots.py` called
`sym.getPortConnection(port)` for every port of every instance, outside the
`port.kind == sk.Port` test above it. That binding accepts a `PortSymbol` or an
`InterfacePortSymbol` and raises for a `MultiPortSymbol`:

    TypeError: getPortConnection(): incompatible function arguments.
        Invoked with types: InstanceSymbol, MultiPortSymbol

So any design holding such a child crashed the structural snapshot with a traceback
instead of giving an error, which AGENTS.md forbids. Confirmed on `main` at 6b4e339 by
building the design below and asking for `baseline_snapshot()`.

The same root cause left a hole in the loop walk of `Module.override_net`, because a
multiport pin could not be read and so carried no dependency. That half is in
`tests/ops/test_override_child_edge.py`, rule C7.

Measured on a `MultiPortSymbol`, which is what makes an honest record possible:

    direction = ArgumentDirection.In
    type      = logic[1:0]
    ports     = ['pa', 'pb']

Rules
    P1  A design with such a child loads, and the baseline snapshot is built without an
        exception.
    P2  The multiport is recorded with its own direction and width. It is NOT recorded as
        an interface port, which is what the old `else` branch did to every port that was
        not a `PortSymbol`.
    P3  The connections of the instance are recorded. Taking them from the instance
        flattens a multiport into its constituent ports, so each carries its own name and
        expression.
    P4  A sign-off over such a design reaches a result rather than raising.
    P5  `pin_connections` asked for a multiport by its own name returns one entry per
        constituent, each with that constituent's connection. It used to raise, because
        `getPortConnection` refuses a `MultiPortSymbol` (#114).
    P6  `pin_connections` asked for a **constituent** by its own name returns that
        constituent's connection. It used to report an empty text, which means "no
        connection", because `portList` holds the multiport and not its constituents, so
        the lookup found nothing. That was a silent wrong answer and is not in #114's text.
    P7  A plain port is unaffected. Both routes give an output port an `Assignment` whose
        own syntax is None, which the existing unwrapping already resolves, so the answer
        for `om` does not move. A name that is no port of the module keeps its present
        answer too: one entry with an empty text.

Observed and deliberately not changed
    `pin_connections` cannot distinguish "the module has no such pin" from "the pin is
    there and nothing is connected to it". Both give one entry with the text "". That is
    what its docstring promises, and the enable trace in `src/hdl_vet/enables.py` reads
    the answer, so widening it is a separate decision and not part of #114.

Not checked
    An interface port, which is the other kind the `else` branch covers and which is
    recorded as `("interface", 0)` on purpose. A multiport whose constituents have
    different directions, which IEEE 1800 does not allow.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.ops import Vet
from hdl_vet.session import Session

MPK = "module mpk (.p({pa, pb}), om);\n  input pa, pb;\n  output om;\n  assign om = pa ^ pb;\nendmodule\n"

TOP = "module top (input logic a, input logic sel, output logic y);\n  mpk u_mp (.p({a, sel}), .om(y));\nendmodule\n"


@pytest.fixture
def vet(tmp_path: Path) -> Vet:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "mpk.sv").write_bytes(MPK.encode())
    (tmp_path / "rtl" / "top.sv").write_bytes(TOP.encode())
    fl = tmp_path / "design.f"
    fl.write_bytes(b"rtl/mpk.sv\nrtl/top.sv\n")
    return Vet(Session(fl, top="top"))


# --- P1: it does not crash ------------------------------------------------------------


def test_the_baseline_snapshot_is_built(vet):
    """Before the fix this raised TypeError from snapshots.py, not a VetError."""
    snap = vet.session.baseline_snapshot()
    assert "top.u_mp" in snap, sorted(snap)


def test_the_design_is_clean(vet):
    assert vet.design.diagnostics() == []


# --- P2: the multiport is recorded honestly -------------------------------------------


def test_the_multiport_keeps_its_direction_and_width(vet):
    """`p` is `input` and two bits wide. Recording it as an interface port loses both."""
    snap = vet.session.baseline_snapshot()
    ports = snap["top.u_mp"].ports
    assert ports["p"] == ("input", 2), ports
    assert ports["om"] == ("output", 1), ports


def test_the_multiport_is_not_called_an_interface(vet):
    snap = vet.session.baseline_snapshot()
    assert snap["top.u_mp"].ports["p"][0] != "interface", snap["top.u_mp"].ports


# --- P3: the connections are recorded -------------------------------------------------


def test_the_connections_of_the_instance_are_recorded(vet):
    """Taken from the instance, a multiport arrives as its constituent ports.

    So the keys are `pa` and `pb` rather than `p`. That is what slang reports, and it
    carries more than a single joined entry would.
    """
    conns = vet.session.baseline_snapshot()["top.u_mp"].connections
    assert conns, conns
    assert {"pa", "pb"} <= set(conns) or "p" in conns, conns
    assert "om" in conns, conns


# --- P4: a sign-off completes ---------------------------------------------------------


def test_a_sign_off_over_such_a_design_completes(vet):
    result = vet.finish()
    assert result.ok, result.report()


# --- P5, P6, P7: pin_connections and a multiport (#114) ------------------------------


def test_asking_for_the_multiport_returns_its_constituents(vet):
    """`.p({a, sel})` connects `pa` to `a` and `pb` to `sel`, so one pin gives two entries.

    Measured before the fix: this raised TypeError, because `getPortConnection` refuses a
    `MultiPortSymbol` and `pins.py` called it unconditionally.
    """
    conns = vet.session.loaded.pin_connections("mpk", "p")
    assert len(conns) == 2, conns
    assert {c.text for c in conns} == {"a", "sel"}, conns
    assert {c.parent for c in conns} == {"top"}, conns
    assert {c.instance for c in conns} == {"u_mp"}, conns


@pytest.mark.parametrize(("pin", "text"), [("pa", "a"), ("pb", "sel")])
def test_asking_for_a_constituent_gives_its_connection(vet, pin, text):
    """Measured before the fix: one entry with `text=""`, meaning "no connection".

    `portList` holds the multiport `p` and the plain port `om`, not `pa` and `pb`, so the
    name lookup found nothing and the code reported an absent connection rather than
    saying it could not tell. #114 did not know about this half.
    """
    conns = vet.session.loaded.pin_connections("mpk", pin)
    assert len(conns) == 1, conns
    assert conns[0].text == text, conns


def test_a_plain_port_is_unaffected(vet):
    """`om` is an output. Its connection arrives as an Assignment whose own syntax is
    None, by either route, and the existing unwrapping resolves it to the signal."""
    conns = vet.session.loaded.pin_connections("mpk", "om")
    assert len(conns) == 1, conns
    assert conns[0].text == "y", conns


def test_a_name_that_is_no_port_at_all_keeps_its_present_answer(vet):
    """One entry with an empty text, which is what the contract says and what it does.

    I first wrote this assertion as `== []` and was wrong: the docstring of
    `pin_connections` says "an instance with no connection for the pin has the text ''",
    and that applies to a name the module does not have at all. So the function cannot
    distinguish "no such pin" from "a pin that is there and unconnected". That conflation
    is real but it is not #114, and the enable trace reads this answer, so it is left
    alone here and recorded in the docstring above instead.
    """
    conns = vet.session.loaded.pin_connections("mpk", "nosuchpin")
    assert len(conns) == 1 and conns[0].text == "", conns
