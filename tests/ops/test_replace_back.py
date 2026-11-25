"""`replace_module` and the replacement back (#57, the third part).

`Instance.replace_module` expects the change `module-changed` at each path of the instance. A
second call that puts the first module back registers the same expectation again. The design is
then the one the run started from, so no such change is seen, and the sign-off fails on both
expectations. Measured on `main` at 8582b67:

    u0.replace_module("child2"); u0.replace_module("child")
    ok=False, files={}
      expected change not seen: module-changed path=top.u0 name=*
      expected change not seen: module-changed path=top.u0 name=*

The wires, assignments, ports and parameters cancel since #98 and #100. This is the same family,
and it uses the same signal: the design that the session started from.

RULES

B1  `replace_module` expects `module-changed` at a path only when the new module is not the
    module that the instance at that path has in the design the session started from.
B2  When the new module is that first module, the call forgets the `module-changed`
    expectations of this run at that path. So "there and back" signs off, with no file changed.
B3  "There, back and there again" expects the change once more, and it is seen.
B4  B1 and B2 are decided for each path. Another instance keeps its expectation.
B5  B2 looks at the module only. The replacement back can leave another parameter value or
    another connection than the first design had. That change is permitted at the instance,
    as it is for every `replace_module`, and it is in `result.changes`.
B6  A group that is rolled back gives the expectations back.
B7  A save between the two calls changes nothing: the pair still cancels after the resume.
B8  The proof or the waiver of each call is asked for as before. Both calls are in the log.

The review found that B2 forgot too much:

B9  An operation forgets only an expectation that an operation registered. An expectation that
    the script registered with `Vet.expect` stays required, whatever its kind, path and name.
    This holds for every pair that cancels: `replace_module` and back, `add_port` and
    `remove_port`, `add_param` and `remove_param`. Before this rule, the port and parameter
    pairs forgot the expectation of the script too.
    `remove_instance` is such an operation too: it forgets what operations expected at the
    instance and below it, and it keeps what the script expected there (found by the second
    review; `main` forgot it).
B10 A saved session keeps which expectations the script registered. The format of the archive
    is 5. An archive of format 4 does not say it, so it is refused, as the older formats are.

Not part of this file: the status `cancelled` of the two records, and `punch_port` undone with
`remove_port` (the other open parts of #57).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.ops import Vet
from hdl_vet.session import Session

CHILD = (
    "module {name} #(parameter int W = 1, parameter int K = 0)\n"
    "  (input logic clk, input logic [W-1:0] d, output logic [W-1:0] q);\n"
    "  always_ff @(posedge clk) q <= d;\n"
    "endmodule\n"
)
TOP = (
    "module top (input logic a, input logic [3:0] b, output logic [3:0] y, output logic [3:0] z);\n"
    "  child #(.W(4)) u0 (.clk(a), .d(b), .q(y));\n"
    "  child #(.W(4)) u1 (.clk(a), .d(b), .q(z));\n"
    "endmodule\n"
)
WHY = "the cells are the same flop"


@pytest.fixture
def filelist(tmp_path: Path) -> Path:
    root = tmp_path / "design"
    (root / "rtl").mkdir(parents=True)
    for name in ("child", "child2", "child3"):
        (root / "rtl" / f"{name}.sv").write_bytes(CHILD.format(name=name).encode())
    (root / "rtl" / "top.sv").write_bytes(TOP.encode())
    path = root / "design.f"
    path.write_bytes(b"rtl/child.sv\nrtl/child2.sv\nrtl/child3.sv\nrtl/top.sv\n")
    return path


@pytest.fixture
def vet(filelist: Path) -> Vet:
    return Vet(Session(filelist, top="top"))


def swap(vet: Vet, instance: str, module: str, **more: object) -> None:
    vet.module("top").instance(instance).replace_module(module, waiver=WHY, **more)


def kinds(result) -> list[tuple[str, str]]:
    return sorted((c.kind, c.path) for c in result.changes)


def test_the_design_is_what_the_tests_need(vet):
    assert vet.design.diagnostics() == []
    swap(vet, "u0", "child2")
    result = vet.finish()
    assert result.ok, result.report()
    assert ("module-changed", "top.u0") in kinds(result)


# --- B1, B2: there and back ----------------------------------------------------------------


def test_there_and_back_signs_off(vet):
    swap(vet, "u0", "child2")
    swap(vet, "u0", "child")
    result = vet.finish()
    assert result.unmatched_expects == [], [str(e) for e in result.unmatched_expects]
    assert result.failed_checks == []
    assert result.ok, result.report()
    assert result.files == {}
    assert result.changes == []


def test_there_and_back_through_a_third_module_signs_off(vet):
    swap(vet, "u0", "child2")
    swap(vet, "u0", "child3")
    swap(vet, "u0", "child")
    result = vet.finish()
    assert result.ok, result.report()
    assert result.files == {}


def test_a_replacement_by_the_module_it_has_is_no_change(vet):
    """The first module asked for at once: nothing is expected, and nothing is seen."""
    swap(vet, "u0", "child")
    result = vet.finish()
    assert result.ok, result.report()
    assert result.files == {}


# --- B3: there, back and there again -------------------------------------------------------


def test_there_back_and_there_again_expects_the_change(vet):
    swap(vet, "u0", "child2")
    swap(vet, "u0", "child")
    swap(vet, "u0", "child2")
    result = vet.finish()
    assert result.ok, result.report()
    assert ("module-changed", "top.u0") in kinds(result)
    assert [str(e) for e in result.unmatched_expects] == []


def test_a_replacement_that_does_not_go_back_is_still_expected(vet):
    """The guard against forgetting too much: `child3` is not the first module of `u0`."""
    swap(vet, "u0", "child2")
    swap(vet, "u0", "child3")
    result = vet.finish()
    assert result.ok, result.report()
    assert ("module-changed", "top.u0") in kinds(result)


# --- B4: each path for itself --------------------------------------------------------------


def test_another_instance_keeps_its_expectation(vet):
    swap(vet, "u1", "child2")
    swap(vet, "u0", "child2")
    swap(vet, "u0", "child")
    result = vet.finish()
    assert result.ok, result.report()
    assert kinds(result) == [("module-changed", "top.u1")]


def test_the_expectation_of_another_instance_is_not_forgotten(vet):
    """`u1` is expected to change. A raw edit puts its module back, and the run must fail."""
    swap(vet, "u1", "child2")
    swap(vet, "u0", "child2")
    swap(vet, "u0", "child")
    top = next(p for p in vet.session.files if p.name == "top.sv")
    text = vet.text(top)
    start = text.index(b"child2 #(.W(4)) u1")
    vet.splice(top, start, start + len(b"child2"), b"child", waiver=WHY)
    result = vet.finish()
    assert not result.ok
    assert any(e.kind == "module-changed" and e.path == "top.u1" for e in result.unmatched_expects)


# --- B5: only the module is looked at ------------------------------------------------------


def test_the_replacement_back_can_leave_another_parameter_value(vet):
    swap(vet, "u0", "child2")
    swap(vet, "u0", "child", set_params={"K": 1})
    result = vet.finish()
    assert [str(e) for e in result.unmatched_expects] == []
    assert result.ok, result.report()
    assert kinds(result) == [("param-changed", "top.u0")]


# --- B6: a group that is rolled back -------------------------------------------------------


def test_a_rolled_back_group_gives_the_expectation_back(vet):
    swap(vet, "u0", "child2")
    with pytest.raises(RuntimeError), vet.group():
        swap(vet, "u0", "child")
        raise RuntimeError("stop the group")
    result = vet.finish()
    assert result.ok, result.report()
    assert ("module-changed", "top.u0") in kinds(result)
    assert any(e.kind == "module-changed" and e.path == "top.u0" for e in vet.session._expects if e.required)


# --- B7: a save between the two calls ------------------------------------------------------


def test_a_pair_that_a_save_splits_still_cancels(vet, tmp_path):
    swap(vet, "u0", "child2")
    vet.save(tmp_path / "s.zip")
    back = Vet.restore(tmp_path / "s.zip")
    swap(back, "u0", "child")
    result = back.finish()
    assert [str(e) for e in result.unmatched_expects] == []
    assert result.ok, result.report()
    assert result.files == {}


# --- B8: the log and the waiver ------------------------------------------------------------


def test_both_calls_are_in_the_log_and_each_needs_its_waiver(vet):
    swap(vet, "u0", "child2")
    swap(vet, "u0", "child")
    assert [(r.op, r.status) for r in vet.log] == [("replace_module", "applied"), ("replace_module", "applied")]


def test_the_replacement_back_without_a_waiver_is_judged_as_before(vet, monkeypatch):
    """Without Yosys a logic call with no waiver is refused. The way back is such a call."""
    from hdl_vet import equiv

    monkeypatch.setattr(equiv, "available", lambda: False)
    swap(vet, "u0", "child2")
    with pytest.raises(Exception) as e:
        vet.module("top").instance("u0").replace_module("child")
    assert str(e.value).startswith("replace_module top.u0"), str(e.value)


# --- B9: the expectation of the script stays -------------------------------------------------


def own(result, kind: str) -> list[str]:
    return [e.path for e in result.unmatched_expects if e.kind == kind]


@pytest.mark.parametrize("when", ["before", "between", "after"])
@pytest.mark.parametrize("exact", [False, True])
def test_the_expectation_of_the_script_is_not_forgotten(vet, when, exact):
    if when == "before":
        vet.expect("module-changed", "top.u0", exact=exact)
    swap(vet, "u0", "child2")
    if when == "between":
        vet.expect("module-changed", "top.u0", exact=exact)
    swap(vet, "u0", "child")
    if when == "after":
        vet.expect("module-changed", "top.u0", exact=exact)
    result = vet.finish()
    assert not result.ok
    assert own(result, "module-changed") == ["top.u0"]


def test_the_expectation_of_the_script_is_met_by_a_real_change(vet):
    vet.expect("module-changed", "top.u0")
    swap(vet, "u0", "child2")
    result = vet.finish()
    assert result.ok, result.report()


def test_a_port_pair_keeps_the_expectation_of_the_script(vet):
    vet.expect("port-added", "top", "np")
    vet.module("top").add_port("np", "input")
    vet.module("top").remove_port("np")
    result = vet.finish()
    assert not result.ok
    assert own(result, "port-added") == ["top"]


def test_a_parameter_pair_keeps_the_expectation_of_the_script(vet):
    vet.expect("param-changed", "top", "NP")
    vet.module("top").add_param("NP", 1)
    vet.module("top").remove_param("NP")
    result = vet.finish()
    assert not result.ok
    assert own(result, "param-changed") == ["top"]


@pytest.mark.parametrize(("kind", "name"), [("module-changed", "*"), ("connection-changed", "d")])
def test_a_removed_instance_keeps_the_expectation_of_the_script(vet, kind, name):
    vet.expect(kind, "top.u0", name)
    vet.module("top").remove_instance("u0")
    result = vet.finish()
    assert not result.ok
    assert own(result, kind) == ["top.u0"]


def test_a_removed_instance_still_forgets_what_an_operation_expected(vet):
    swap(vet, "u0", "child2")
    vet.module("top").remove_instance("u0")
    result = vet.finish()
    assert own(result, "module-changed") == []


def test_the_pairs_still_cancel_with_no_expectation_of_the_script(vet):
    vet.module("top").add_port("np", "input")
    vet.module("top").remove_port("np")
    vet.module("top").add_param("NP", 1)
    vet.module("top").remove_param("NP")
    result = vet.finish()
    assert result.ok, result.report()
    assert result.files == {}


# --- B10: the saved session ------------------------------------------------------------------


def test_a_save_keeps_whose_expectation_it_is(vet, tmp_path):
    vet.expect("module-changed", "top.u0")
    swap(vet, "u0", "child2")
    vet.save(tmp_path / "s.zip")
    back = Vet.restore(tmp_path / "s.zip")
    swap(back, "u0", "child")
    result = back.finish()
    assert not result.ok
    assert own(result, "module-changed") == ["top.u0"]


def test_the_format_of_the_archive_is_5():
    from hdl_vet import archive

    assert archive.FORMAT == 5
