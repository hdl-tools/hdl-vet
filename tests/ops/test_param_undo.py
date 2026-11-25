"""`set_param` and then `remove_param` of that parameter (#99).

`Instance.set_param` registers a check that the override is in the final design.
`Module.remove_param` removes the parameter and, at every instance of the module, its override.
Nothing retired the check, so it failed on a design that was right. Measured on `main` at 3c4ba91:

    mid.u_l1.set_param("DEPTH", 8, was=4)
    leaf.remove_param("DEPTH")
    ok=False
      failed check: set_param mid.u_l1 DEPTH: override not found in the final design

It fails loudly, so it was no wrong "ok". It is the same shape as the check of a `connect` when
`remove_port` takes the connection away (rule U1 of tests/ops/test_punch_undo.py), and it gets
the same answer.

RULES

S1  `remove_param` of a module takes the override of that parameter away at every instance of
    the module. A check of a `set_param` of that parameter at such an instance is retired with
    it: the parameter is gone, so the check can say nothing.
S2  So `set_param` and then `remove_param` signs off, with the changes that the removal makes.
    `add_param`, `set_param` and `remove_param` signs off with no file changed.
S3  S1 is for that parameter at those instances only. A `set_param` of another parameter keeps
    its check.
S4  A parameter that is removed, added again and set again has a live check.
S5  A `remove_param` that is refused retires nothing, also inside a group where the script
    catches the refusal (the retirement is data on the plan, as in rule U9).
S6  A group that is rolled back gives the check back. A save between the calls changes nothing.

The expectations need no rule: `remove_param` forgets the `param-changed` expectations of the
pair since #100, and it keeps an expectation of the script (rule B9).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session

LEAF = (
    "module leaf #(parameter int DEPTH = 2, parameter int K = 0) (input logic a, output logic y);\n"
    "  assign y = a;\n"
    "endmodule\n"
)
MID = (
    "module mid (input logic a, output logic y, output logic z);\n"
    "  leaf #(.DEPTH(4)) u_l1 (.a(a), .y(y));\n"
    "  leaf u_l2 (.a(a), .y(z));\n"
    "endmodule\n"
)
TOP = "module top (input logic a, output logic y, output logic z);\n  mid u_m (.a(a), .y(y), .z(z));\nendmodule\n"
WHY = "the parameter is not used"


@pytest.fixture
def filelist(tmp_path: Path) -> Path:
    root = tmp_path / "design"
    (root / "rtl").mkdir(parents=True)
    for name, text in (("leaf", LEAF), ("mid", MID), ("top", TOP)):
        (root / "rtl" / f"{name}.sv").write_bytes(text.encode())
    path = root / "design.f"
    path.write_bytes(b"rtl/leaf.sv\nrtl/mid.sv\nrtl/top.sv\n")
    return path


@pytest.fixture
def vet(filelist: Path) -> Vet:
    return Vet(Session(filelist, top="top"))


def leaf(vet: Vet):
    return vet.module("leaf", instances=2)


def inst(vet: Vet, name: str):
    return vet.module("mid").instance(name)


def clean(result) -> None:
    assert [str(c) for c in result.failed_checks] == []
    assert [(e.kind, e.path, e.name) for e in result.unmatched_expects] == []
    assert result.ok, result.report()


def kinds(result) -> list[tuple[str, str, str]]:
    return sorted((c.kind, c.path, c.name) for c in result.changes)


def resplice(vet: Vet, name: bytes, old: bytes, new: bytes) -> None:
    """Change the value of the override `name` in `mid` by a raw edit (the queued edits are applied first)."""
    vet.session.refresh()
    vet._sync()
    mid = next(p for p in vet.session.files if p.name == "mid.sv")
    text = vet.text(mid)
    start = text.index(b"(" + old + b")", text.index(b"." + name)) + 1
    vet.splice(mid, start, start + len(old), new, waiver=WHY)


def test_the_design_is_what_the_tests_need(vet):
    assert vet.design.diagnostics() == []
    inst(vet, "u_l1").set_param("DEPTH", 8, was=4, waiver=WHY)
    result = vet.finish()
    clean(result)
    assert kinds(result) == [("param-changed", "top.u_m.u_l1", "DEPTH")]


# --- S1, S2 ----------------------------------------------------------------------------------


def test_an_override_that_is_changed_and_then_goes_with_its_parameter_signs_off(vet):
    inst(vet, "u_l1").set_param("DEPTH", 8, was=4, waiver=WHY)
    leaf(vet).remove_param("DEPTH")
    result = vet.finish()
    clean(result)
    assert kinds(result) == [
        ("param-changed", "top.u_m.u_l1", "DEPTH"),
        ("param-changed", "top.u_m.u_l2", "DEPTH"),
    ]


def test_an_override_that_is_added_and_then_goes_with_its_parameter_signs_off(vet):
    inst(vet, "u_l2").set_param("K", 3, waiver=WHY)
    leaf(vet).remove_param("K")
    result = vet.finish()
    clean(result)
    assert kinds(result) == [("param-changed", "top.u_m.u_l1", "K"), ("param-changed", "top.u_m.u_l2", "K")]


def test_a_parameter_that_is_added_set_and_removed_leaves_no_file_changed(vet):
    leaf(vet).add_param("NP", 1)
    inst(vet, "u_l2").set_param("NP", 3, waiver=WHY)
    leaf(vet).remove_param("NP")
    result = vet.finish()
    clean(result)
    assert result.files == {}
    assert result.changes == []


# --- S3: only that parameter -----------------------------------------------------------------


def test_another_parameter_keeps_its_check(vet):
    inst(vet, "u_l1").set_param("K", 3, waiver=WHY)
    inst(vet, "u_l1").set_param("DEPTH", 8, was=4, waiver=WHY)
    leaf(vet).remove_param("DEPTH")
    result = vet.finish()
    clean(result)
    assert ("param-changed", "top.u_m.u_l1", "K") in kinds(result)


def test_another_parameter_that_a_raw_edit_changes_fails(vet):
    inst(vet, "u_l1").set_param("K", 3, waiver=WHY)
    inst(vet, "u_l1").set_param("DEPTH", 8, was=4, waiver=WHY)
    leaf(vet).remove_param("DEPTH")
    resplice(vet, b"K", b"3", b"5")
    result = vet.finish()
    assert not result.ok
    assert any("set_param mid.u_l1 K" in str(c) for c in result.failed_checks), result.report()


def test_the_same_parameter_at_another_instance_that_was_not_set_needs_nothing(vet):
    inst(vet, "u_l1").set_param("DEPTH", 8, was=4, waiver=WHY)
    leaf(vet).remove_param("DEPTH")
    assert [c.key for c in vet._checks if c.key[0] == "param" and len(c.key) == 4] == []


# --- S4: removed, added again, set again -----------------------------------------------------


def test_a_parameter_that_comes_back_has_a_live_check(vet):
    leaf(vet).add_param("NP", 1)
    inst(vet, "u_l2").set_param("NP", 3, waiver=WHY)
    leaf(vet).remove_param("NP")
    leaf(vet).add_param("NP", 1)
    inst(vet, "u_l2").set_param("NP", 3, waiver=WHY)
    clean(vet.check())
    resplice(vet, b"NP", b"3", b"4")
    result = vet.finish()
    assert not result.ok
    assert any("set_param mid.u_l2 NP" in str(c) for c in result.failed_checks), result.report()


# --- S5: a refusal retires nothing -----------------------------------------------------------

# A second parent of `leaf`, with a directive in its parameter list.
ZZ = (
    "module zz (input logic a, output logic y);\n"
    "  leaf #(\n"
    "`ifdef FOO\n"
    "    .K(1),\n"
    "`endif\n"
    "    .DEPTH(4)) u_q (.a(a), .y(y));\n"
    "endmodule\n"
)


@pytest.fixture
def held(filelist: Path) -> Vet:
    (filelist.parent / "rtl" / "zz.sv").write_bytes(ZZ.encode())
    filelist.write_bytes(b"rtl/leaf.sv\nrtl/mid.sv\nrtl/zz.sv\nrtl/top.sv\n")
    return Vet(Session(filelist, top="top"))


def test_a_refused_removal_in_a_group_keeps_the_check(held):
    inst(held, "u_l1").set_param("DEPTH", 8, was=4, waiver=WHY)
    before = sorted(c.key for c in held._checks)
    with held.group():
        with pytest.raises(VetError) as e:
            leaf(held).remove_param("DEPTH")
        assert str(e.value).startswith("remove_param"), str(e.value)
        assert sorted(c.key for c in held._checks) == before
    resplice(held, b"DEPTH", b"8", b"6")
    result = held.finish()
    assert not result.ok
    assert any("set_param mid.u_l1 DEPTH" in str(c) for c in result.failed_checks), result.report()


# --- S6: a group and a save ------------------------------------------------------------------


def test_a_rolled_back_group_gives_the_check_back(vet):
    inst(vet, "u_l1").set_param("DEPTH", 8, was=4, waiver=WHY)
    with pytest.raises(RuntimeError), vet.group():
        leaf(vet).remove_param("DEPTH")
        raise RuntimeError("stop the group")
    resplice(vet, b"DEPTH", b"8", b"6")
    result = vet.finish()
    assert not result.ok
    assert any("set_param mid.u_l1 DEPTH" in str(c) for c in result.failed_checks), result.report()


def test_a_save_between_the_calls_changes_nothing(vet, tmp_path):
    leaf(vet).add_param("NP", 1)
    inst(vet, "u_l2").set_param("NP", 3, waiver=WHY)
    vet.save(tmp_path / "s.zip")
    back = Vet.restore(tmp_path / "s.zip")
    back.module("leaf", instances=2).remove_param("NP")
    result = back.finish()
    clean(result)
    assert result.files == {}
