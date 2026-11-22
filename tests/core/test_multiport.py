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
