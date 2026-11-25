"""A `remove_instance` that is refused forgets nothing (#133).

`Module.remove_instance` forgot the expectations at the instance and below it, dropped the checks
of the instance and recorded what the instance drove, all before its plan was committed. Outside a
group a refusal rolls that back. Inside a group there is no checkpoint for one call: when the
script catches the refusal, the instance is still in the design and what was forgotten stays
forgotten. Measured on `main` at 3c4ba91, with `top.sv` not editable:

    leaf.add_port("pp", "input")
    with vet.group():
        try: top.remove_instance("u_m")          # VetError: cannot edit ...top.sv
        except VetError: pass
    ok=False
      unexpected change: port-added top.u_m.u_l pp: None -> 'input 1'

The `add_port` is right, and the sign-off refuses it. `remove_port` had the same shape with a
check, which gave a wrong "ok" (rule U9 of tests/ops/test_punch_undo.py).

RULES

V1  A `remove_instance` that is refused changes nothing of the session and nothing of the
    checks, also inside a group where the script catches the refusal. What it forgets and
    drops goes only when its plan is committed.
V2  A `remove_instance` that is committed forgets and drops what it did before this part.

Not measured: whether an expectation below an instance can be the only guard of a change. Where
one is, the shape that V1 closes was a wrong "ok".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session

LEAF = "module leaf (input logic a, input logic spare, output logic y);\n  assign y = ~a;\nendmodule\n"
MID = "module mid (input logic a, input logic b, output logic y);\n  leaf u_l (.a(a), .spare(b), .y(y));\nendmodule\n"
TOP = "module top (input logic a, input logic b, output logic y);\n  mid u_m (.a(a), .b(b), .y(y));\nendmodule\n"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "design"
    (root / "rtl").mkdir(parents=True)
    for name, text in (("leaf", LEAF), ("mid", MID), ("top", TOP)):
        (root / "rtl" / f"{name}.sv").write_bytes(text.encode())
    (root / "design.f").write_bytes(b"rtl/leaf.sv\nrtl/mid.sv\nrtl/top.sv\n")
    return root


@pytest.fixture
def held(root: Path) -> Vet:
    """`top.sv` cannot be edited, so `remove_instance` in `top` is refused when it commits."""
    return Vet(Session(root / "design.f", top="top", editable=[root / "rtl" / "leaf.sv", root / "rtl" / "mid.sv"]))


@pytest.fixture
def vet(root: Path) -> Vet:
    return Vet(Session(root / "design.f", top="top"))


def required(vet: Vet) -> list[tuple[str, str, str]]:
    return sorted((e.kind, e.path, e.name) for e in vet.session._expects if e.required)


def refuse(vet: Vet) -> None:
    with vet.group():
        with pytest.raises(VetError) as e:
            vet.module("top").remove_instance("u_m")
        assert str(e.value).startswith("remove_instance"), str(e.value)


# --- V1 ------------------------------------------------------------------------------------


def test_a_refused_removal_in_a_group_keeps_the_expectations_below_the_instance(held):
    held.module("leaf").add_param("K", 1)
    before = required(held)
    refuse(held)
    assert required(held) == before
    result = held.finish()
    assert [str(c) for c in result.unexpected_changes] == []
    assert result.ok, result.report()


def test_a_refused_removal_in_a_group_keeps_the_checks(held):
    held.module("mid").instance("u_l").connect("spare", "a", was="b")
    before = sorted(c.key for c in held._checks)
    refuse(held)
    assert sorted(c.key for c in held._checks) == before
    result = held.finish()
    assert result.ok, result.report()


def test_a_refused_removal_keeps_what_the_session_knows_after_a_save(held, tmp_path):
    held.module("leaf").add_param("K", 1)
    refuse(held)
    held.save(tmp_path / "s.zip")
    back = Vet.restore(tmp_path / "s.zip")
    result = back.finish()
    assert result.ok, result.report()


def test_a_refused_removal_outside_a_group_keeps_everything(held):
    held.module("leaf").add_param("K", 1)
    before = required(held)
    with pytest.raises(VetError):
        held.module("top").remove_instance("u_m")
    assert required(held) == before
    assert held.finish().ok


# --- V2 ------------------------------------------------------------------------------------


def test_a_committed_removal_still_forgets_what_was_expected_below_it(vet):
    vet.module("mid").instance("u_l").connect("spare", "a", was="b")
    with vet.group():
        vet.module("top").remove_instance("u_m")
        vet.module("top").add_assign("y", "a")
    assert [e for e in required(vet) if e[0] == "connection-changed"] == []
    result = vet.finish()
    assert [str(e) for e in result.unmatched_expects] == []
    assert [str(c) for c in result.failed_checks] == []
