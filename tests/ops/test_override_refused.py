"""An `override_net` that is refused changes nothing.

`Module.override_net` committed its plan and after that moved the check of an earlier
`add_assign` of the net to the keep name (`_retarget`). That step can refuse: the keep name
already has a post-condition of this run. Outside a group the refusal rolls the commit back.
Inside a group there is no checkpoint for one call. When the script caught the refusal, the
override was in the design, its record was in the log as `applied`, and its check was live.
Measured on `main` at a3e04a6:

    top.add_assign("n", "a")
    with vet.group():
        top.remove_assign("k"); top.remove_wire("k")
        try: top.override_net("n", "b", enable="en", keep="k", waiver=...)   # VetError
        except VetError: pass
    text:  logic k;  assign k = a;  assign n = en ? b : k;
    log:   ... ('override_net', 'top.n', 'applied')
    ok=False, three failed checks

No continuation gave a wrong "ok": the checks that were left exclude each other. But the run
could never sign off again, also after the script put the design back by hand. It is the shape
of rule U9 of tests/ops/test_punch_undo.py and of rule V1 of
tests/ops/test_remove_instance_refused.py.

RULES

R1  An `override_net` that is refused changes nothing: no text, no record in the log, no check,
    no expectation. This holds inside a group where the script catches the refusal.
R2  After such a refusal the run signs off as it does without the call.
R3  An `override_net` that is not refused does what it did before, and the check of an earlier
    `add_assign` of the net still follows the net to the keep name.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet import equiv
from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session

TOP = (
    "module top (input logic a, input logic b, output logic y, output logic z);\n"
    "  logic k;\n"
    "  {kind} n;\n"
    "  assign k = b;\n"
    "  assign y = a;\n"
    "  assign z = n;\n"
    "endmodule\n"
)
WHY = "a test"


@pytest.fixture(params=["logic", "wire"])
def vet(request, tmp_path: Path, monkeypatch) -> Vet:
    monkeypatch.setattr(equiv, "available", lambda: False)
    root = tmp_path / "design"
    (root / "rtl").mkdir(parents=True)
    (root / "rtl" / "top.sv").write_bytes(TOP.format(kind=request.param).encode())
    (root / "design.f").write_bytes(b"rtl/top.sv\n")
    return Vet(Session(root / "design.f", top="top"))


def top(vet: Vet):
    return vet.module("top")


def prepare(vet: Vet) -> None:
    """`n` gets a driver, and the name `k` is freed: its check of the removal is what `_retarget` meets."""
    top(vet).add_port("en", "input")
    top(vet).add_assign("n", "a", waiver=WHY)
    with vet.group():
        top(vet).remove_assign("k")
        top(vet).remove_wire("k")


def state(vet: Vet):
    vet.session.refresh()
    vet._sync()
    path = next(p for p in vet.session.files if p.name == "top.sv")
    return (
        vet.text(path),
        [(r.op, r.target, r.status) for r in vet.log],
        sorted(c.key for c in vet._checks),
        sorted((e.kind, e.path, e.name, e.required) for e in vet.session._expects),
    )


def override(vet: Vet) -> None:
    top(vet).override_net("n", "b", enable="en", keep="k", waiver=WHY)


# --- R1, R2 ----------------------------------------------------------------------------------


def test_a_refusal_in_a_group_that_the_script_catches_changes_nothing(vet):
    prepare(vet)
    before = state(vet)
    with vet.group():
        with pytest.raises(VetError) as e:
            override(vet)
        assert str(e.value).startswith("override_net top: "), str(e.value)
    assert state(vet) == before


def test_the_run_signs_off_after_the_refusal_as_without_the_call(vet):
    prepare(vet)
    with vet.group(), pytest.raises(VetError):
        override(vet)
    result = vet.finish()
    assert [str(c) for c in result.failed_checks] == []
    assert result.ok, result.report()
    assert all(r.op != "override_net" for r in vet.log)


def test_a_refusal_outside_a_group_changes_nothing(vet):
    prepare(vet)
    before = state(vet)
    with pytest.raises(VetError):
        override(vet)
    assert state(vet) == before
    assert vet.finish().ok


def test_a_refusal_in_a_group_that_the_script_does_not_catch_rolls_the_group_back(vet):
    prepare(vet)
    before = state(vet)
    with pytest.raises(VetError), vet.group():
        override(vet)
    assert state(vet) == before


# --- R3 --------------------------------------------------------------------------------------


def test_an_override_that_is_taken_still_moves_the_check_of_the_assignment(vet):
    top(vet).add_port("en", "input")
    top(vet).add_assign("n", "a", waiver=WHY)
    top(vet).override_net("n", "b", enable="en", keep="n_pre", waiver=WHY)
    keys = sorted(c.key for c in vet._checks)
    assert ("assign", "top", "n_pre") in keys
    assert ("assign", "top", "n") not in keys
    result = vet.finish()
    assert [str(c) for c in result.failed_checks] == []
    assert result.ok, result.report()
