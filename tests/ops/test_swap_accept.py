"""`Instance.replace_module` and the argument `accept_types` (#59, part 2).

A swap refuses a carried port whose type differs from the port it replaces. Part 1 of #59
made that comparison read the type the port resolves to, so a typedef or a type parameter
that resolves to the same type is no longer a difference. What is left is a difference that
is real, `bit` against `logic` above all, and there was no way to accept one on purpose.

`accept_types` is that way. It names ports, not the whole swap, so the waiver is visible in
the script and in a review, and so that a port which starts differing after a later
generator run is still refused.

The direction and the bit width are not waivable. A width difference is silent truncation
or extension and a direction difference leaves a net undriven, and the structural layer
cannot see through either of them afterwards.

Rules
    V1  `accept_types` names a port of the **new** module, as `connect` and `unconnected`
        do. A type difference on a port it names is accepted and the swap applies.
    V2  A port that `ports` renames is named by the name it lands on, not by the
        connection it comes from.
    V3  A name that is not a port of the new module is refused, before any edit.
    V4  The waiver covers the ports it names and no others. Another port whose type
        differs is still refused.
    V5  The direction and the bit width are never waivable. `accept_types` does not cover
        them.
    V6  A waiver that covers nothing is refused. The types of that port agree, so the
        waiver is stale, and a stale waiver would cover a difference that appears later.
    V7  Each waived port leaves a note in the sign-off report, naming the port and both
        types. A waiver inside a script is otherwise invisible to a reader of the output.

Not checked
    Whether a waived swap is equivalent. The equivalence check works on what Yosys reads,
    and Yosys has no four-state value, so it cannot see a `bit` against `logic` difference.
    That is why the waiver is deliberate and per port.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session
from no_comparison import no_comparison  # noqa: F401

HEADER = "typedef logic [7:0] byte_t;\ntypedef bit [7:0] two_state_t;\n"

CELLS = {
    # the instantiated cell: its port types come from the type parameter
    "old_c": "module old_c #(parameter type T = logic) (input logic clk, input T d, output T q);\n"
    "  always_ff @(posedge clk) q <= d;\nendmodule\n",
    # two-state, the same eight bits: a real type difference, no width difference
    "two_c": "module two_c (input logic clk, input two_state_t d, output two_state_t q);\n"
    "  always_ff @(posedge clk) q <= d;\nendmodule\n",
    # two-state on `q` only, so one port differs and one does not
    "half_c": "module half_c (input logic clk, input byte_t d, output two_state_t q);\n"
    "  always_ff @(posedge clk) q <= d;\nendmodule\n",
    # the same types: nothing for a waiver to cover
    "same_c": "module same_c (input logic clk, input byte_t d, output byte_t q);\n"
    "  always_ff @(posedge clk) q <= d;\nendmodule\n",
    # four bits: a width difference, which no waiver covers
    "thin_c": "module thin_c (input logic clk, input logic [3:0] d, output logic [3:0] q);\n"
    "  always_ff @(posedge clk) q <= d;\nendmodule\n",
    # the ports renamed, and two-state
    "cap_c": "module cap_c (input logic clk, input two_state_t D, output two_state_t Q);\n"
    "  always_ff @(posedge clk) Q <= D;\nendmodule\n",
}

TOP = (
    "module top (input logic clk, input byte_t d, output byte_t q);\n"
    "  old_c #(.T(byte_t)) u_c (.clk(clk), .d(d), .q(q));\n"
    "endmodule\n"
)


@pytest.fixture
def kit(tmp_path: Path):
    root = tmp_path / "in"
    (root / "rtl").mkdir(parents=True)
    (root / "rtl" / "types.svh").write_bytes(HEADER.encode())
    for name, text in CELLS.items():
        (root / "rtl" / f"{name}.sv").write_bytes(f'`include "types.svh"\n{text}'.encode())
    (root / "rtl" / "top.sv").write_bytes(f'`include "types.svh"\n{TOP}'.encode())
    lines = "+incdir+rtl\n" + "".join(f"rtl/{n}.sv\n" for n in CELLS) + "rtl/top.sv\n"
    (root / "design.f").write_bytes(lines.encode())
    return root / "design.f"


@pytest.fixture
def vet(kit):
    return Vet(Session(kit, top="top"))


def swap(vet: Vet, module: str, **kw):
    return vet.module("top").instance("u_c").replace_module(module, was="old_c", **kw)


# --- V1 and V2: the waiver works, and names a port of the new module -----------------


def test_a_waived_port_is_accepted(vet):
    swap(vet, "two_c", drop_params=["T"], accept_types=["d", "q"])
    r = vet.finish()
    assert r.ok, r.report()


def test_the_same_swap_without_the_waiver_is_refused(vet):
    """The counterpart: the waiver is what makes the difference, not the design."""
    with pytest.raises(VetError, match="two-state"):
        swap(vet, "two_c", drop_params=["T"])


def test_a_renamed_port_is_waived_under_the_name_it_lands_on(vet):
    swap(vet, "cap_c", drop_params=["T"], ports={"d": "D", "q": "Q"}, accept_types=["D", "Q"])
    r = vet.finish()
    assert r.ok, r.report()


def test_a_renamed_port_is_not_waived_under_the_old_name(vet):
    with pytest.raises(VetError, match="accept_types") as e:
        swap(vet, "cap_c", drop_params=["T"], ports={"d": "D", "q": "Q"}, accept_types=["d", "q"])
    assert "'d'" in str(e.value)


# --- V3: a name that is not a port -------------------------------------------------


def test_a_name_that_is_not_a_port_is_refused(vet):
    with pytest.raises(VetError, match="accept_types") as e:
        swap(vet, "two_c", drop_params=["T"], accept_types=["nope"])
    assert "'nope'" in str(e.value)


def test_nothing_is_written_when_the_waiver_is_refused(vet):
    before = vet.session.text(vet.module("top").path)
    with pytest.raises(VetError):
        swap(vet, "two_c", drop_params=["T"], accept_types=["nope"])
    assert vet.session.text(vet.module("top").path) == before
    assert vet.log == []


# --- V4: only the ports it names -----------------------------------------------------


def test_another_port_with_a_type_difference_is_still_refused(vet):
    """`half_c` differs on `q` only, so a waiver for `d` leaves `q` refused."""
    with pytest.raises(VetError, match="'q'"):
        swap(vet, "half_c", drop_params=["T"], accept_types=["d"])


def test_the_port_that_differs_can_be_waived_alone(vet):
    swap(vet, "half_c", drop_params=["T"], accept_types=["q"])
    r = vet.finish()
    assert r.ok, r.report()


# --- V5: the width and the direction are not waivable ---------------------------------


def test_a_width_difference_is_not_waivable(vet):
    with pytest.raises(VetError, match="bits wide"):
        swap(vet, "thin_c", drop_params=["T"], accept_types=["d", "q"])


# --- V6: a waiver that covers nothing -------------------------------------------------


def test_a_waiver_that_covers_nothing_is_refused(vet):
    with pytest.raises(VetError, match="accept_types") as e:
        swap(vet, "same_c", drop_params=["T"], accept_types=["d"])
    assert "'d'" in str(e.value)


def test_a_waiver_for_a_port_that_is_not_carried_is_refused(vet):
    """`q` is dropped, so no comparison happens on it and the waiver covers nothing."""
    with pytest.raises(VetError, match="accept_types"):
        swap(vet, "two_c", drop_params=["T"], drop_ports=["q"], connect={"q": "q"}, accept_types=["q"])


# --- V7: the note ---------------------------------------------------------------------


def test_each_waived_port_leaves_a_note(vet):
    swap(vet, "two_c", drop_params=["T"], accept_types=["d", "q"])
    notes = "\n".join(vet.notes)
    assert "'d'" in notes and "'q'" in notes, notes
    assert "two-state" in notes, notes
    assert "accept_types" in notes, notes
    r = vet.finish()
    assert "two-state" in r.report(), r.report()
