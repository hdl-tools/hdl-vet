"""The status `cancelled` of two operations that undo each other (#57, the last part).

Two operations of one run can undo each other: `add_port` and `remove_port` of the same port,
`replace_module` and the replacement back. Their expectations and checks cancel since #98, #100,
#132 and #134, so the run signs off. The log still said `applied` for both, and the summary of
`vet apply` counted both as applied, while the design had not changed.

Owner decisions

2026-11-20
- A third status of `OpRecord`: `cancelled`. Both records of a pair get it.

2026-11-27
- `cancelled` means that the pair left no change in the design. A way back that leaves another
  parameter value or another connection keeps both records `applied`. It is decided at the
  sign-off, from the changes that the two operations leave.
- Three calls on one thing: the records are paired in order. The first two are `cancelled`, the
  third is `applied`. Four calls are two pairs.

RULES

C1  `OpRecord.status` is "applied", "skipped" or "cancelled". Before a sign-off a record is
    "applied" or "skipped". `Vet.finish` and `Vet.check` decide which records are "cancelled",
    and `vet.log` shows it after them.
C2  A pair is two "applied" records of one thing, of which the second undoes the first:
        add_wire / remove_wire          of one name in one module
        add_assign / remove_assign      of one target in one module
        add_port / remove_port          of one name in one module
        add_param / remove_param        of one name in one module
        replace_module / replace_module of one instance, the second back to the first module
    The other way round is no pair: a removal of a thing of the input design, and then an
    addition that puts it back. The addition writes the thing anew, and what hung on the first
    one does not come back: the connections of a port at the instances, the overrides of a
    parameter, the place of a port in the list, an initial value. A first form of this rule
    took it as a pair, and the review showed four runs whose log said "cancelled" for a design
    that had changed. Both records stay "applied".
C3  An addition and its removal are "cancelled": the thing was not there before the first and
    is not there after the second. A replacement and the replacement back are "cancelled" when
    the instance has, in the final design, the module, the parameter overrides and the
    connections that it has in the input design. Otherwise both records stay "applied".
C4  The records of one thing are paired in order. An addition takes the next removal as its
    partner, and a replacement takes the next replacement. A removal that no addition of the
    run stands before has no partner, and so has the last record of an odd number. A record
    with no partner stays "applied".
C5  It is decided again at every sign-off. A `check` can show a pair as "cancelled", and a later
    operation on the same thing makes a new record; the `finish` then pairs in order again.
C6  A "skipped" record is in no pair. Records of other things are not touched.
C7  `Result.ok` does not depend on the status.
C8  A saved session holds no "cancelled". It is decided again at the next sign-off, so the format
    of the archive does not change.
C9  `vet apply` counts the three: "N applied, M skipped", and ", K cancelled" behind it when K is
    not 0. So the summary of a run with no pair reads as before.

STATED LIMITS

- A pair that more calls on the same thing follow is "cancelled" with no look at the design:
  the design between the calls is not kept. `replace_module` there, back with another
  parameter value, and there again shows the first two as "cancelled".
- After a resume, a record of the saved part is paired only when its thing can be told from the
  checks of the session: one thing of that kind in that module. With two, the records stay
  "applied". A record is never "cancelled" by a guess.
- A raw splice is no record. A pair stays "cancelled" when a raw splice afterwards writes the
  same thing again. The second review showed it; the sign-off of that run fails.

- `punch_port` is `add_port` in each module and `connect` at each instance. When `remove_port`
  undoes it, the port records are pairs. A `connect` record has no partner in the log: it stays
  "applied", although its connection went with the port.
- `set_param` and `connect` back to the first value, and `override_net` or `insert_cell_on_net`,
  have no operation that undoes them as a pair. Their records stay "applied".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet import equiv
from hdl_vet.cli import main
from hdl_vet.ops import Vet
from hdl_vet.session import Session

LEAF = (
    "module {name} #(parameter int W = 1, parameter int K = 0)\n"
    "  (input logic a, input logic spare, output logic y);\n"
    "  logic helper;\n"
    "  assign helper = a;\n"
    "  assign y = helper;\n"
    "endmodule\n"
)
TOP = (
    "module top (input logic a, input logic b, output logic y, output logic z);\n"
    "  leaf u0 (.a(a), .spare(b), .y(y));\n"
    "  leaf u1 (.a(a), .spare(b), .y(z));\n"
    "endmodule\n"
)
WHY = "the two cells are the same"


@pytest.fixture(autouse=True)
def no_yosys(monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)


@pytest.fixture
def filelist(tmp_path: Path) -> Path:
    root = tmp_path / "design"
    (root / "rtl").mkdir(parents=True)
    for name in ("leaf", "leaf2"):
        (root / "rtl" / f"{name}.sv").write_bytes(LEAF.format(name=name).encode())
    (root / "rtl" / "top.sv").write_bytes(TOP.encode())
    path = root / "design.f"
    path.write_bytes(b"rtl/leaf.sv\nrtl/leaf2.sv\nrtl/top.sv\n")
    return path


@pytest.fixture
def vet(filelist: Path) -> Vet:
    return Vet(Session(filelist, top="top"))


def top(vet: Vet):
    return vet.module("top")


def log(vet: Vet) -> list[tuple[str, str]]:
    return [(r.op, r.status) for r in vet.log]


def swap(vet: Vet, module: str, **more: object) -> None:
    top(vet).instance("u0").replace_module(module, waiver=WHY, **more)


# --- C1, C2, C3: a pair that leaves nothing --------------------------------------------------------

ADDED = {
    "wire": (lambda t: t.add_wire("w2"), lambda t: t.remove_wire("w2")),
    "assign": (lambda t: t.add_assign("w2", "a"), lambda t: t.remove_assign("w2")),
    "port": (lambda t: t.add_port("np", "output"), lambda t: t.remove_port("np")),
    "param": (lambda t: t.add_param("NP", 1), lambda t: t.remove_param("NP")),
}


@pytest.mark.parametrize("kind", ["wire", "port", "param"])
def test_an_addition_and_its_removal_are_cancelled(vet, kind):
    add, remove = ADDED[kind]
    add(top(vet))
    assert log(vet)[-1][1] == "applied"
    remove(top(vet))
    assert [s for _, s in log(vet)] == ["applied", "applied"]  # before the sign-off
    result = vet.finish()
    assert result.ok, result.report()
    assert result.files == {}
    assert [s for _, s in log(vet)] == ["cancelled", "cancelled"]


def test_an_assignment_and_its_removal_are_cancelled_inside_a_pair_of_the_wire(vet):
    top(vet).add_wire("w2")
    top(vet).add_assign("w2", "a")
    top(vet).remove_assign("w2")
    top(vet).remove_wire("w2")
    result = vet.finish()
    assert result.ok, result.report()
    assert log(vet) == [
        ("add_wire", "cancelled"),
        ("add_assign", "cancelled"),
        ("remove_assign", "cancelled"),
        ("remove_wire", "cancelled"),
    ]


def test_a_replacement_and_the_replacement_back_are_cancelled(vet):
    swap(vet, "leaf2")
    swap(vet, "leaf")
    result = vet.finish()
    assert result.ok, result.report()
    assert log(vet) == [("replace_module", "cancelled"), ("replace_module", "cancelled")]


def test_a_removal_and_the_addition_that_puts_the_thing_back_are_no_pair(vet):
    """`helper` is in the input design. It is removed and put back as it was: every record stays applied."""
    leaf = vet.module("leaf", instances=2)
    with vet.group():
        leaf.remove_assign("y")
        leaf.remove_assign("helper")
        leaf.remove_wire("helper")
        leaf.add_wire("helper")
        leaf.add_assign("helper", "a")
        leaf.add_assign("y", "helper")
    result = vet.finish()
    assert result.ok, result.report()
    assert {s for _, s in log(vet)} == {"applied"}, log(vet)


def test_a_port_that_is_removed_and_added_again_loses_its_connections_and_is_no_pair(vet):
    """The removal takes `.spare(b)` away at both instances, and the addition does not put it back."""
    leaf = vet.module("leaf", instances=2)
    leaf.remove_port("spare")
    leaf.add_port("spare", "input")
    vet.finish()
    assert log(vet) == [("remove_port", "applied"), ("add_port", "applied")]


def test_a_parameter_that_is_removed_and_added_again_is_no_pair(vet):
    leaf = vet.module("leaf", instances=2)
    leaf.remove_param("K")
    leaf.add_param("K", 0)
    vet.finish()
    assert log(vet) == [("remove_param", "applied"), ("add_param", "applied")]


def test_a_removal_before_a_pair_stays_applied(vet):
    """Removed, added, removed: the last two are a pair, and the first removal is what the run did."""
    leaf = vet.module("leaf", instances=2)
    leaf.remove_param("K")
    leaf.add_param("K", 0)
    leaf.remove_param("K")
    vet.finish()
    assert log(vet) == [("remove_param", "applied"), ("add_param", "cancelled"), ("remove_param", "cancelled")]


# --- C3: something is left -------------------------------------------------------------------------


def test_a_way_back_that_leaves_another_parameter_value_is_not_cancelled(vet):
    swap(vet, "leaf2")
    swap(vet, "leaf", set_params={"K": 1})
    result = vet.finish()
    assert result.ok, result.report()
    assert log(vet) == [("replace_module", "applied"), ("replace_module", "applied")]


def test_a_thing_that_comes_back_in_another_form_is_not_cancelled(vet):
    leaf = vet.module("leaf", instances=2)
    with vet.group():
        leaf.remove_assign("y")
        leaf.remove_assign("helper")
        leaf.remove_wire("helper")
        leaf.add_wire("helper", type="wire")
        leaf.add_assign("helper", "a")
        leaf.add_assign("y", "helper")
    vet.finish()
    statuses = dict(log(vet))
    assert statuses["remove_wire"] == "applied"
    assert statuses["add_wire"] == "applied"


def test_one_addition_alone_is_applied(vet):
    top(vet).add_wire("w2")
    vet.finish()
    assert log(vet) == [("add_wire", "applied")]


# --- C4: pairs in order ----------------------------------------------------------------------------


def test_three_calls_are_one_pair_and_one_applied(vet):
    top(vet).add_port("np", "output")
    top(vet).remove_port("np")
    top(vet).add_port("np", "output")
    result = vet.finish()
    assert result.ok, result.report()
    assert log(vet) == [("add_port", "cancelled"), ("remove_port", "cancelled"), ("add_port", "applied")]


def test_four_calls_are_two_pairs(vet):
    for _ in range(2):
        top(vet).add_wire("w2")
        top(vet).remove_wire("w2")
    vet.finish()
    assert [s for _, s in log(vet)] == ["cancelled"] * 4


def test_there_back_and_there_again(vet):
    swap(vet, "leaf2")
    swap(vet, "leaf")
    swap(vet, "leaf2")
    result = vet.finish()
    assert result.ok, result.report()
    assert [s for _, s in log(vet)] == ["cancelled", "cancelled", "applied"]


# --- C5: decided again at every sign-off -----------------------------------------------------------


def test_a_check_shows_it_and_a_later_call_is_paired_again(vet):
    top(vet).add_wire("w2")
    top(vet).remove_wire("w2")
    vet.check()
    assert [s for _, s in log(vet)] == ["cancelled", "cancelled"]
    top(vet).add_wire("w2")
    vet.finish()
    assert [s for _, s in log(vet)] == ["cancelled", "cancelled", "applied"]


# --- C6: other records -----------------------------------------------------------------------------


def test_a_skipped_record_and_other_things_are_not_touched(vet):
    top(vet).remove_wire("nosuch")
    top(vet).add_wire("keep")
    top(vet).add_wire("w2")
    top(vet).remove_wire("w2")
    vet.module("leaf", instances=2).add_wire("w2")
    vet.finish()
    assert log(vet) == [
        ("remove_wire", "skipped"),
        ("add_wire", "applied"),
        ("add_wire", "cancelled"),
        ("remove_wire", "cancelled"),
        ("add_wire", "applied"),
    ]


def test_the_same_name_in_two_modules_is_two_things(vet):
    top(vet).add_wire("w2")
    vet.module("leaf", instances=2).add_wire("w2")
    top(vet).remove_wire("w2")
    vet.finish()
    assert log(vet) == [("add_wire", "cancelled"), ("add_wire", "applied"), ("remove_wire", "cancelled")]


# --- C8: a saved session ---------------------------------------------------------------------------


def test_a_saved_session_holds_no_cancelled_and_decides_again(vet, tmp_path):
    top(vet).add_wire("w2")
    top(vet).remove_wire("w2")
    vet.check()
    vet.save(tmp_path / "s.zip")
    back = Vet.restore(tmp_path / "s.zip")
    assert [s for _, s in log(back)] == ["applied", "applied"]
    back.finish()
    assert [s for _, s in log(back)] == ["cancelled", "cancelled"]


def test_the_format_of_the_archive_is_still_6():
    from hdl_vet import archive

    assert archive.FORMAT == 6


# --- C9: the summary of the command line -----------------------------------------------------------

SCRIPT = "def apply(vet):\n    top = vet.module('top')\n    top.add_wire('keep')\n{more}"
PAIR = "    top.add_wire('w2')\n    top.remove_wire('w2')\n"


def run_script(filelist: Path, tmp_path: Path, capsys, more: str) -> str:
    script = tmp_path / "apply.py"
    script.write_text(SCRIPT.format(more=more), encoding="utf-8")
    code = main(["apply", "--no-project", "-f", str(filelist), "--top", "top", "-s", str(script), "--dry-run"])
    assert code == 0
    captured = capsys.readouterr()
    return captured.out + captured.err


def test_the_summary_counts_the_cancelled_records(filelist, tmp_path, capsys):
    out = run_script(filelist, tmp_path, capsys, PAIR)
    assert "1 applied, 0 skipped, 2 cancelled" in out, out


def test_the_summary_of_a_run_with_no_pair_reads_as_before(filelist, tmp_path, capsys):
    out = run_script(filelist, tmp_path, capsys, "")
    assert "1 applied, 0 skipped" in out, out
    assert "cancelled" not in out
