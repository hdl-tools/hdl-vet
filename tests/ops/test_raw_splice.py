"""A raw splice needs a proof or a waiver (phase 6, part 4; issue #40).

THE PROBLEM

`vet.splice` writes any text. A splice that changes logic (`1'b1` to `1'b0` in
an assign) passed the sign-off when the module had no logic operation: the
design compiles, there is no new diagnostic, the structure is the same. The
plan's rule "a logic edit with neither equivalence nor waiver fails the run"
held for `override_net` only.

OWNER DECISIONS (2026-11-17, 2026-11-18)

- Every module whose text a raw splice changed is compared with its original.
  It must be equal, or the splice has a waiver.
- Without Yosys, a raw splice inside a module is refused when called, unless it
  has a waiver. The same rule as `override_net`.
- A raw splice that changes text outside every module (a package, a macro, a
  typedef at file level) always needs a waiver. No comparison is run for it.

RULES

S1  `vet.splice(path, start, end, text, old=None, *, waiver=None)`. A raw splice
    is a call of `vet.splice` by a script. A splice that an operation makes is
    not raw.
S2  Blanks and comments. A splice that leaves the file the same but for blanks
    and comments needs nothing: no comparison, no waiver, no record.
S3  Inside a module. A splice whose range lies inside one module marks that
    module. The sign-off compares each marked module with the module of the
    input design, for each set of parameter values, exactly as for a module
    with a logic operation: the enables of its logic operations (and the ones
    it carries) are tied to 0, children that did not change are black boxes.
    A module with a raw splice and no logic operation has no tie: it must be
    equal as it is.
S4  The record. The label of the operation is `splice <module>`; it is in
    `operations` once, however many splices the module has. The lines of the
    report are the ones of `override_net`:
      equivalence proved: core (splice core)
      not equivalent: core: <detail> (splice core)
      equivalence not checked: core: <detail> (splice core)
      waived: splice core: "<waiver>" (the check says: refuted, <detail>)
    A proved line has `; equal when <enable> is 0` only when the module has an
    enable.
S5  The waiver. A marked module is "waived" only when every logic operation in
    it and every raw splice in it has a waiver. A waiver is one line (as P6 of
    tests/ops/test_override_review.py). The comparison still runs with a
    waiver, and its answer is in the line.
S6  Without Yosys. A raw splice inside a module with no waiver is refused when
    called: the message starts with `splice <file name>: ` and names
    `hdl-vet[equiv]`. Nothing is written. With a waiver it is applied, and the
    record is "waived" with "the check did not run".
S7  Outside the modules. A splice whose range is not inside one module (a
    package, a macro, a typedef at file level, a range that spans two modules,
    a file that holds no module) is refused when called unless it has a
    waiver, with or without Yosys: the message says "outside". With a waiver
    it is applied and no comparison is run for it. The result has it in
    `Result.outside`: a list of records with `file` (the path in the design,
    with slashes) and `waiver`. Its line of the report:
      waived: splice rtl/p.sv: "<waiver>" (outside the modules: not compared)
    The manifest has it under "waivers" as {operation: "splice rtl/p.sv",
    waiver, check: null}.
S8  A splice in a module that is not elaborated under the top is refused as
    before (nothing changes there).
S9  A group that is left by an exception takes its raw splices back: no mark, no
    record. A scope keeps its own rules for a splice; a splice that the scope
    allows is raw like any other.
S10 A saved session keeps the marks and the waivers. A run that resumes it
    judges them again.
S11 The other checks of the sign-off do not change: a raw splice that changes
    the structure still needs `vet.expect`, and a new diagnostic still fails.
    A waiver on a splice silences none of them.
S12 The exit status of `vet apply` is 1 for refuted and for not checked, 0 for
    proved and for waived.

STATED LIMITS

- A module with a raw splice is judged as a whole: a structural operation in
  the same module that changes behaviour (another connection of a pin) is then
  "not equivalent" too, as it is for a module with `override_net`.
- An intended change of function cannot be proved equal. It needs a waiver that
  names the check that covers it.
- The limits of the engine hold (tests/equiv, issue #50): for example a module
  with flops on more than one clock is "not checked".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hdl_vet import equiv
from hdl_vet.cli import main
from hdl_vet.ops import VetError
from override_design import CORE, FILES, Design, needs_yosys, records, statuses, with_enable

pytestmark = [pytest.mark.equiv]

PKG = "package p;\n  localparam int INC = 1;\nendpackage\n"
BUG = (b"assign count_o  = cnt;", b"assign count_o  = cnt + 8'd1;")
SAME = (b"assign rst_o    = rst_sync_n;", b"assign rst_o    = rst_sync_n & 1'b1;")
COMMENT = (b"assign rst_o    = rst_sync_n;", b"assign rst_o = rst_sync_n;  // the reset out")


@pytest.fixture
def design(tmp_path: Path) -> Design:
    return Design(tmp_path / "design")


def no_yosys(monkeypatch) -> None:
    monkeypatch.setattr(equiv, "available", lambda: False)


def lines(result, word: str) -> list[str]:
    return [line for line in result.report().splitlines() if word in line]


# --- S3, S4: inside a module ----------------------------------------------------------------------


@needs_yosys
def test_the_silent_bug_fails_the_sign_off(design):
    vet = design.vet()
    design.replace(vet, "core", *BUG)
    result = vet.finish()
    assert records(result) == [("core", "refuted")]
    (record,) = result.equivalence
    assert record.operations == ["splice core"]
    assert "count_o" in record.detail
    assert lines(result, "not equivalent") == [f"  not equivalent: core: {record.detail} (splice core)"]
    assert not result.ok


@needs_yosys
def test_a_splice_that_keeps_the_function_is_proved(design):
    vet = design.vet()
    design.replace(vet, "core", *SAME)
    result = vet.finish()
    assert records(result) == [("core", "proved")]
    assert lines(result, "equivalence proved") == ["  equivalence proved: core (splice core)"]
    assert result.ok, result.report()


@needs_yosys
def test_several_splices_in_a_module_are_one_operation(design):
    vet = design.vet()
    design.replace(vet, "core", *SAME)
    design.replace(vet, "core", b"assign count_o  = cnt;", b"assign count_o  = cnt | 8'd0;")
    result = vet.finish()
    (record,) = result.equivalence
    assert (record.status, record.operations) == ("proved", ["splice core"])


@needs_yosys
def test_two_modules_are_two_records(design):
    vet = design.vet()
    design.replace(vet, "rst_sync", b"assign rst_no = q[STAGES-1];", b"assign rst_no = q[STAGES-1] & 1'b1;")
    design.replace(vet, "timer", b"assign irq_o = &count_o;", b"assign irq_o = |count_o;")
    result = vet.finish()
    assert {r.module: r.status for r in result.equivalence} == {"rst_sync": "proved", "timer": "refuted"}
    assert not result.ok


@needs_yosys
def test_a_splice_beside_an_override_is_compared_with_the_enable_tied(design):
    vet = design.vet()
    core = with_enable(vet)
    core.override_net("rst_sync_n", "rst_ni", enable="test_en_i")
    design.replace(vet, "core", b"assign count_o  = cnt;", b"assign count_o  = cnt ^ 8'd0;")
    result = vet.check()
    (record,) = result.equivalence
    assert record.status == "proved", record.detail
    assert record.operations == ["override_net core.rst_sync_n", "splice core"]
    assert "equal when test_en_i is 0" in lines(result, "equivalence proved")[0]
    design.replace(vet, "core", b"cnt ^ 8'd0", b"cnt ^ 8'd1")
    assert records(vet.finish()) == [("core", "refuted")]


# --- S2: blanks and comments ----------------------------------------------------------------------


@pytest.mark.parametrize("yosys", [True, False])
def test_blanks_and_comments_need_nothing(design, monkeypatch, yosys):
    if not yosys:
        no_yosys(monkeypatch)
    calls = []
    monkeypatch.setattr(equiv, "check", lambda *a, **k: calls.append(a) or pytest.fail("no comparison is expected"))
    vet = design.vet()
    design.replace(vet, "core", *COMMENT)
    design.replace(vet, "top", b"module top (", b"// the top\nmodule top (")
    result = vet.finish()
    assert result.equivalence == [] and result.outside == []
    assert result.ok, result.report()
    assert "the reset out" in design.text(vet, "core")
    assert calls == []


# --- S5, S6: the waiver, and without Yosys --------------------------------------------------------


def test_without_yosys_a_splice_in_a_module_is_refused(design, monkeypatch):
    no_yosys(monkeypatch)
    vet = design.vet()
    before = statuses(vet)
    with pytest.raises(VetError, match=r"hdl-vet\[equiv\]") as e:
        design.replace(vet, "core", *BUG)
    assert str(e.value).startswith("splice core.sv: ")
    assert design.changed(vet) == set()
    assert statuses(vet) == before
    assert vet.finish().ok


def test_without_yosys_a_waived_splice_is_applied(design, monkeypatch):
    no_yosys(monkeypatch)
    vet = design.vet()
    design.replace(vet, "core", *BUG, waiver="count from one: see TB-9")
    result = vet.finish()
    (record,) = result.equivalence
    assert (record.module, record.status, record.check) == ("core", "waived", None)
    assert lines(result, "waived") == [
        '  waived: splice core: "count from one: see TB-9" (the check did not run: Yosys is missing)'
    ]
    assert result.ok


@needs_yosys
def test_a_waived_splice_is_still_compared(design):
    vet = design.vet()
    design.replace(vet, "core", *BUG, waiver="count from one: see TB-9")
    result = vet.finish()
    (record,) = result.equivalence
    assert (record.status, record.check) == ("waived", "refuted")
    (line,) = lines(result, "waived")
    assert line.startswith('  waived: splice core: "count from one: see TB-9" (the check says: refuted, ')
    assert result.ok


@needs_yosys
def test_one_splice_without_a_waiver_keeps_the_module_from_waived(design):
    vet = design.vet()
    design.replace(vet, "core", *BUG, waiver="w")
    design.replace(vet, "core", *SAME)
    result = vet.finish()
    assert records(result) == [("core", "refuted")]
    assert not result.ok


@needs_yosys
def test_a_waiver_on_the_override_does_not_cover_the_splice(design):
    vet = design.vet()
    core = with_enable(vet)
    core.override_net("rst_sync_n", "rst_ni", enable="test_en_i", waiver="w")
    design.replace(vet, "core", *BUG)
    result = vet.finish()
    assert records(result) == [("core", "refuted")]
    assert not result.ok


@pytest.mark.parametrize("waiver", ["first\nsecond", "", 3])
def test_a_wrong_waiver_is_refused(design, monkeypatch, waiver):
    no_yosys(monkeypatch)
    vet = design.vet()
    with pytest.raises(VetError, match="waiver"):
        design.replace(vet, "core", *BUG, waiver=waiver)
    assert design.changed(vet) == set()


# --- S7: outside the modules ----------------------------------------------------------------------


def with_package(tmp_path: Path) -> Design:
    files = {"p": PKG, **FILES}
    files["core"] = CORE.replace("assign count_o  = cnt;", "assign count_o  = cnt + 8'(p::INC) - 8'd1;")
    assert files["core"] != CORE
    return Design(tmp_path / "pkg", files)


@pytest.mark.parametrize("yosys", [True, False])
def test_a_splice_outside_the_modules_is_refused_without_a_waiver(tmp_path, monkeypatch, yosys):
    if not yosys:
        no_yosys(monkeypatch)
    design = with_package(tmp_path)
    vet = design.vet()
    with pytest.raises(VetError, match="outside") as e:
        design.replace(vet, "p", b"INC = 1", b"INC = 2")
    assert str(e.value).startswith("splice p.sv: ")
    assert design.changed(vet) == set()


def test_a_waived_splice_outside_the_modules_is_not_compared(tmp_path, monkeypatch):
    monkeypatch.setattr(equiv, "check", lambda *a, **k: pytest.fail("no comparison is expected"))
    design = with_package(tmp_path)
    vet = design.vet()
    design.replace(vet, "p", b"INC = 1", b"INC = 2", waiver="the step is 2 now: simulated in TB-3")
    result = vet.finish()
    assert result.equivalence == []
    assert [(o.file, o.waiver) for o in result.outside] == [("rtl/p.sv", "the step is 2 now: simulated in TB-3")]
    assert lines(result, "waived") == [
        '  waived: splice rtl/p.sv: "the step is 2 now: simulated in TB-3" (outside the modules: not compared)'
    ]
    assert result.ok, result.report()


def test_a_comment_outside_the_modules_needs_nothing(tmp_path):
    design = with_package(tmp_path)
    vet = design.vet()
    design.replace(vet, "p", b"package p;", b"// constants\npackage p;")
    result = vet.finish()
    assert result.outside == [] and result.ok


def test_a_range_over_two_modules_is_outside(tmp_path, monkeypatch):
    no_yosys(monkeypatch)
    both = FILES["rst_sync"] + FILES["timer"]
    files = {"cells": both, "core": FILES["core"], "top": FILES["top"]}
    design = Design(tmp_path / "two", files)
    vet = design.vet()
    old = b"endmodule\nmodule timer"
    assert old in both.encode()
    with pytest.raises(VetError, match="outside"):
        design.replace(vet, "cells", old, b"endmodule\n\n`define X 1\nmodule timer")


# --- S9: groups -----------------------------------------------------------------------------------


@needs_yosys
def test_a_group_that_fails_takes_the_splice_back(design):
    vet = design.vet()
    with pytest.raises(RuntimeError), vet.group():
        design.replace(vet, "core", *BUG)
        raise RuntimeError("stop")
    result = vet.finish()
    assert result.equivalence == []
    assert design.changed(vet) == set()
    assert result.ok


# --- S10: a saved session -------------------------------------------------------------------------


@needs_yosys
def test_a_resumed_session_judges_the_splice_again(design, tmp_path):
    vet = design.vet()
    design.replace(vet, "core", *BUG, waiver="see TB-9")
    saved = tmp_path / "session.zip"
    vet.save(saved)
    again = type(vet).restore(saved)
    result = again.finish()
    (record,) = result.equivalence
    assert (record.status, record.check, record.waiver) == ("waived", "refuted", "see TB-9")
    assert record.operations == ["splice core"]


# --- S11: the other checks ------------------------------------------------------------------------


@needs_yosys
def test_a_waiver_does_not_hide_an_unexpected_change(design):
    vet = design.vet()
    design.replace(vet, "core", b".en_i(en_i[0]), .count_o(cnt)", b".en_i(en_i[1]), .count_o(cnt)", waiver="w")
    result = vet.finish()
    assert not result.ok
    assert "unexpected change" in result.report()


# --- S12: the command line ------------------------------------------------------------------------


def run(*args: object) -> int:
    return main([str(a) for a in args])


SCRIPT = (
    "def apply(vet):\n"
    '    path = vet.module("core").path\n'
    "    old = b'assign count_o  = cnt;'\n"
    "    at = vet.text(path).index(old)\n"
    "    vet.splice(path, at, at + len(old), b'assign count_o  = cnt + 8\\'d1;', old=old{waiver})\n"
)


@needs_yosys
@pytest.mark.parametrize("waiver, status", [("", 1), (', waiver="see TB-9"', 0)])
def test_the_exit_status(design, tmp_path, capsys, monkeypatch, waiver, status):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bug.py").write_text(SCRIPT.format(waiver=waiver), encoding="utf-8")
    out = tmp_path / "out"
    code = run("apply", "--no-project", "-f", design.filelist, "--top", "top", "-s", "bug.py", "-o", out)
    err = capsys.readouterr().err
    assert code == status, err
    if status == 0:
        assert 'waived: splice core: "see TB-9"' in err
        manifest = json.loads((out / "hdl-vet.json").read_text(encoding="utf-8"))
        assert manifest["waivers"] == [
            {"operation": "splice core", "waiver": "see TB-9", "check": manifest["waivers"][0]["check"]}
        ]
        assert manifest["waivers"][0]["check"].startswith("refuted")
        assert [e["status"] for e in manifest["equivalence"]] == ["waived"]
    else:
        assert "not equivalent: core:" in err and "(splice core)" in err
