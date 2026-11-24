"""Review of `accept_types` (#59, part 2).

The waiver itself held: the review could not make it accept a difference of direction or of
bit width, and found no wrong `ok`. What it found is that the waiver is judged **per
elaborated path**, and the target of a swap can be elaborated at several paths.

`compared`, the set of ports the waiver actually covered, was built inside the loop over
the paths, and the "covers nothing" refusal was raised inside that loop too. A module whose
type parameter comes from its parent has different port types at different paths. The
waiver is then needed at one path and covers nothing at another, and the swap cannot be
expressed at all: with the waiver it is refused for covering nothing, without it for the
type difference. The note had the same shape of defect and appeared once per path.

Rules
    W1  The waiver is judged over the whole swap, not one path at a time. A waiver that
        covers something at any path is not stale.
    W2  A waived port leaves one note, whatever the number of paths. `Result.report()`
        prints each note once, as it already does for the equivalence lines.
    W3  A waiver that covers nothing at any path is still refused.
    W4  A port whose bit width is reported as zero is still guarded. The width clause is
        skipped for it, so the waiver leaves the type clause as the only check in hdl-vet,
        and the refusal then comes from the compiler. The swap does not go through.

Not checked
    A `group` whose later edit retypes the connected net could mask the diagnostic that
    rule W4 rests on, because the new diagnostics are compared once at the end of a group.
    That needs a raw edit of the enclosing declaration, so it is deliberate, and it is a
    stated limit rather than a rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session
from no_comparison import no_comparison  # noqa: F401

HEADER = "typedef logic [7:0] byte_t;\ntypedef bit [7:0] two_state_t;\n"

CELLS = {
    "old_c": "module old_c #(parameter type T = logic) (input logic clk, input T d, output T q);\n"
    "  always_ff @(posedge clk) q <= d;\nendmodule\n",
    "two_c": "module two_c (input logic clk, input two_state_t d, output two_state_t q);\n"
    "  always_ff @(posedge clk) q <= d;\nendmodule\n",
    # the type parameter comes from the parent, so the port types differ per path
    "mid": "module mid #(parameter type T = logic) (input logic clk, input T d, output T q);\n"
    "  old_c #(.T(T)) u_c (.clk(clk), .d(d), .q(q));\nendmodule\n",
    # unpacked arrays: slang reports the bit width of such a port as zero
    "arr2_c": "module arr2_c (input logic clk, input logic d [2], output logic q [2]);\n"
    "  always_ff @(posedge clk) q <= d;\nendmodule\n",
    "arr8_c": "module arr8_c (input logic clk, input logic d [8], output logic q [8]);\n"
    "  always_ff @(posedge clk) q <= d;\nendmodule\n",
}

TOP = (
    "module top (input logic clk, input byte_t da, output byte_t qa,\n"
    "            input two_state_t db, output two_state_t qb);\n"
    "  mid #(.T(byte_t)) u_a (.clk(clk), .d(da), .q(qa));\n"
    "  mid #(.T(two_state_t)) u_b (.clk(clk), .d(db), .q(qb));\n"
    "endmodule\n"
)

ARRAY_TOP = (
    "module top (input logic clk, input logic da [2], output logic qa [2]);\n"
    "  arr2_c u_c (.clk(clk), .d(da), .q(qa));\n"
    "endmodule\n"
)


def build(root: Path, top_text: str) -> Path:
    (root / "rtl").mkdir(parents=True)
    (root / "rtl" / "types.svh").write_bytes(HEADER.encode())
    for name, text in CELLS.items():
        (root / "rtl" / f"{name}.sv").write_bytes(f'`include "types.svh"\n{text}'.encode())
    (root / "rtl" / "top.sv").write_bytes(f'`include "types.svh"\n{top_text}'.encode())
    lines = "+incdir+rtl\n" + "".join(f"rtl/{n}.sv\n" for n in CELLS) + "rtl/top.sv\n"
    (root / "design.f").write_bytes(lines.encode())
    return root / "design.f"


@pytest.fixture
def two_paths(tmp_path: Path):
    """`mid` is elaborated twice, with a four-state type at one path and two-state at the other."""
    return Vet(Session(build(tmp_path / "in", TOP), top="top"))


def test_the_baseline_has_two_types_for_one_port(two_paths):
    """What makes the rest of this file possible: one port, two resolved types."""
    snap = two_paths.session.baseline_snapshot()
    assert snap["top.u_a.u_c"].port_resolved["d"] == "logic[7:0]"
    assert snap["top.u_b.u_c"].port_resolved["d"] == "bit[7:0] (two-state)"


# --- W1 and W2 -----------------------------------------------------------------------


def test_a_waiver_needed_at_one_path_is_not_stale(two_paths):
    swap = two_paths.module("mid", instances=2).instance("u_c")
    swap.replace_module("two_c", was="old_c", drop_params=["T"], accept_types=["d", "q"])
    r = two_paths.finish()
    assert r.ok, r.report()


def test_a_waived_port_leaves_one_note_for_the_whole_swap(two_paths):
    swap = two_paths.module("mid", instances=2).instance("u_c")
    swap.replace_module("two_c", was="old_c", drop_params=["T"], accept_types=["d", "q"])
    assert len(two_paths.notes) == 2, two_paths.notes
    report = two_paths.finish().report()
    assert report.count("accept_types accepts the type of port 'd'") == 1, report


# --- W3 ------------------------------------------------------------------------------


def test_a_waiver_that_covers_nothing_at_any_path_is_still_refused(two_paths):
    swap = two_paths.module("mid", instances=2).instance("u_c")
    with pytest.raises(VetError, match="covers nothing") as e:
        swap.replace_module("two_c", was="old_c", drop_params=["T"], accept_types=["d", "q", "clk"])
    assert "'clk'" in str(e.value)


# --- W4 ------------------------------------------------------------------------------


def test_a_port_of_width_zero_is_still_guarded(tmp_path: Path):
    """The width clause is skipped for an unpacked array, so the waiver removes the only
    check hdl-vet makes. The compiler refuses the edited design, so nothing passes."""
    v = Vet(Session(build(tmp_path / "in", ARRAY_TOP), top="top"))
    assert v.session.baseline_snapshot()["top.u_c"].ports["d"] == ("input", 0)
    with pytest.raises(VetError) as e:
        v.module("top").instance("u_c").replace_module("arr8_c", was="arr2_c", accept_types=["d", "q"])
    assert "new error" in str(e.value), str(e.value)
    assert v.log == []
