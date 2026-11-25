"""An operation that writes logic needs a proof or a waiver (phase 6, part 5; issue #75).

THE PROBLEM

Since the rule for raw splices (tests/ops/test_raw_splice.py), `vet.splice` of
`assign x = a & b;` needs a proof or a waiver. `add_assign("x", "a & b")` wrote
the same text and no comparison judged it. The same held for an expression on a
pin (`connect`), for another value of a parameter of a child (`set_param`) and
for another module in the place of a child (`replace_module`).

OWNER DECISIONS (2026-11-24)

- `add_assign`, `connect` with an expression, `set_param` and `replace_module`
  need a proof or a waiver, by the rule of a raw splice.
- For `add_assign` and `connect`, an expression with an operator, a conditional
  or a call is logic. A name, a select, a concatenation and a constant are
  wiring: they stay structural, as before.
- `add_file` alone needs nothing: its modules are used through `replace_module`.
- A saved session of an older format is refused, as before.

RULES

L1  The logic calls. A call counts when it changes the text and is one of:
    - `Module.add_assign(target, expr)` with an expression that is logic,
    - `Instance.connect(port, expr)` with an expression that is logic,
    - `Instance.set_param(name, value)`, whatever the value,
    - `Instance.replace_module(module, ...)`.
    A call that is skipped (the text is there already) or refused counts for
    nothing.
L2  Wiring and logic. An expression is wiring when it is made only of names,
    literals, selects, concatenations, replications and parentheses. The index
    and the bounds of a select and the count of a replication may be any
    constant expression (`d[W-1:0]`). All else is logic: an operator, a
    conditional, a call, a cast, a select with an index that is a signal (it is
    a mux), and anything that the reader cannot sort. The syntax tree decides,
    through the adapter, not a search in the text.
L3  The mark. A logic call marks the module whose text it changes: the module
    of `add_assign`, the module that holds the instance for the three others.
    The sign-off compares each marked module with the module of the input
    design, by rule S3 of tests/ops/test_raw_splice.py. The label of the
    operation in the record is
      add_assign <module>.<target>
      connect <module>.<instance>.<port>
      set_param <module>.<instance>.<name>
      replace_module <module>.<instance>
    The labels of a record are in the order of the calls. The lines of the
    report are the ones of rule S4, with these labels.
L4  The waiver. Each of the four operations takes `waiver=None`, by keyword. A
    waiver is one line of text that is not empty. A marked module is "waived"
    only when every logic operation and every raw splice in it has a waiver
    (rule S5). The comparison still runs with a waiver. A waiver on a call that
    does not count (wiring, or a skipped call) is accepted and leaves no
    record.
L5  Without Yosys. A logic call with no waiver is refused when called. The
    message starts with the operation and its target, as the other refusals of
    the operation do, and names `hdl-vet[equiv]`. Nothing is written. With a
    waiver the call is applied, and the record is "waived" with "the check did
    not run".
L6  Taking back. A call that fails, and a group that is left by an exception,
    leave no mark.
L7  Operations that are built from the same parts do not count on their own:
    `punch_port` and `propagate_param` write names only. The `connect=` and
    `set_params=` of `replace_module` are part of its one label.
L8  A saved session keeps the marks and the waivers. The format of the archive
    is 4. An archive of format 3 can hold a logic call with no mark, so it is
    refused, as the formats 1 and 2 are.
L9  The other checks do not change: the post-condition of each operation, the
    structure, the diagnostics. A waiver silences none of them.
L10 The exit status of `vet apply` is 1 for refuted and for not checked, 0 for
    proved and for waived (rule S12).

STATED LIMITS

- Wiring is not compared. `connect("en_i", "en_i[1]")` and a tie to `1'b0`
  change the function and pass with the structural checks only. This is the
  owner's line between wiring and logic.
- `add_assign` drives a net that had no driver, and `set_param` asks for
  another child. Such a call nearly always changes the function, so it nearly
  always needs a waiver that names the check that covers it. The comparison
  then documents what changed.
- `disconnect`, the removals, `punch_port` and `propagate_param` are not
  compared.
- The limits of rule S3 hold: a marked module is judged as a whole.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hdl_vet import archive, equiv
from hdl_vet.cli import main
from hdl_vet.ops import DriftError, VetError
from override_design import CORE, FILES, RST_SYNC, Design, needs_yosys, records, with_enable

pytestmark = [pytest.mark.equiv]

TECH = RST_SYNC.replace("module rst_sync ", "module rst_sync_tech ")
TECH_WRONG = TECH.replace("assign rst_no = q[STAGES-1];", "assign rst_no = ~q[STAGES-1];")
READ = CORE.replace("assign rst_o    = rst_sync_n;", "assign rst_o    = rst_sync_n & floating;")

LOGIC = [
    "en_i[0] & en_i[1]",
    "~en_i[0]",
    "en_i[1] ? en_i[0] : 1'b0",
    "en_i[seen]",
    "$signed(en_i[0])",
    "1'(en_i[0])",
    "(en_i[0] == 1'b1)",
]
WIRING = ["en_i[1]", "(en_i[1])", "{en_i[1]}", "{1{en_i[1]}}", "en_i[2-1]", "en_i[1:1]", "1'b1", "seen"]


@pytest.fixture
def design(tmp_path: Path) -> Design:
    return Design(tmp_path / "design")


def no_yosys(monkeypatch) -> None:
    monkeypatch.setattr(equiv, "available", lambda: False)


def lines(result, word: str) -> list[str]:
    return [line for line in result.report().splitlines() if word in line]


def timer(vet):
    return vet.module("core").instance("u_timer")


def sync(vet):
    return vet.module("core").instance("u_sync")


def tech(design: Design, vet, text: str = TECH) -> None:
    path = design.root / "cells" / "rst_sync_tech.sv"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(text.encode())
    vet.add_file(path)


# --- L1, L2, L5: what counts, seen without Yosys ---------------------------------------------------


@pytest.mark.parametrize("expr", LOGIC)
def test_connect_with_logic_is_refused_without_yosys(design, monkeypatch, expr):
    no_yosys(monkeypatch)
    vet = design.vet()
    with pytest.raises(VetError) as e:
        timer(vet).connect("en_i", expr, was="en_i[0]")
    assert str(e.value).startswith("connect core.u_timer")
    assert "hdl-vet[equiv]" in str(e.value) and "waiver" in str(e.value)
    assert design.changed(vet) == set()
    assert vet.finish().equivalence == []


@pytest.mark.parametrize("expr", WIRING)
def test_connect_with_wiring_needs_nothing(design, monkeypatch, expr):
    no_yosys(monkeypatch)
    vet = design.vet()
    timer(vet).connect("en_i", expr, was="en_i[0]")
    result = vet.finish()
    assert result.equivalence == []
    assert result.ok, result.report()


@pytest.mark.parametrize("expr", LOGIC)
def test_add_assign_with_logic_is_refused_without_yosys(design, monkeypatch, expr):
    no_yosys(monkeypatch)
    vet = design.vet()
    with pytest.raises(VetError) as e:
        vet.module("core").add_assign("floating", expr)
    assert str(e.value).startswith("add_assign core")
    assert "hdl-vet[equiv]" in str(e.value)
    assert design.changed(vet) == set()


@pytest.mark.parametrize("expr", WIRING)
def test_add_assign_with_wiring_needs_nothing(design, monkeypatch, expr):
    no_yosys(monkeypatch)
    vet = design.vet()
    vet.module("core").add_assign("floating", expr)
    result = vet.finish()
    assert result.equivalence == []
    assert result.ok, result.report()


def test_set_param_is_refused_without_yosys(design, monkeypatch):
    no_yosys(monkeypatch)
    vet = design.vet()
    with pytest.raises(VetError) as e:
        sync(vet).set_param("STAGES", "3", was="2")
    assert str(e.value).startswith("set_param core.u_sync")
    assert "hdl-vet[equiv]" in str(e.value)
    assert design.changed(vet) == set()


def test_replace_module_is_refused_without_yosys(design, monkeypatch):
    no_yosys(monkeypatch)
    vet = design.vet()
    tech(design, vet)
    with pytest.raises(VetError) as e:
        sync(vet).replace_module("rst_sync_tech", was="rst_sync")
    assert str(e.value).startswith("replace_module core.u_sync")
    assert "hdl-vet[equiv]" in str(e.value)
    assert design.changed(vet) == set()


def test_a_waiver_lets_the_call_through_without_yosys(design, monkeypatch):
    no_yosys(monkeypatch)
    vet = design.vet()
    sync(vet).set_param("STAGES", "3", was="2", waiver="see TB-9")
    result = vet.finish()
    (record,) = result.equivalence
    assert (record.module, record.status, record.check, record.waiver) == ("core", "waived", None, "see TB-9")
    assert record.operations == ["set_param core.u_sync.STAGES"]
    assert lines(result, "waived") == [
        '  waived: set_param core.u_sync.STAGES: "see TB-9" (the check did not run: Yosys is missing)'
    ]
    assert result.ok, result.report()


def test_a_skipped_call_counts_for_nothing(design, monkeypatch):
    no_yosys(monkeypatch)
    vet = design.vet()
    sync(vet).set_param("STAGES", "2")
    timer(vet).connect("en_i", "en_i[0]")
    assert vet.finish().equivalence == []


def test_a_waiver_on_wiring_leaves_no_record(design, monkeypatch):
    no_yosys(monkeypatch)
    vet = design.vet()
    timer(vet).connect("en_i", "en_i[1]", was="en_i[0]", waiver="see TB-9")
    vet.module("core").add_assign("floating", "1'b0", waiver="see TB-9")
    sync(vet).set_param("STAGES", "2", waiver="see TB-9")
    result = vet.finish()
    assert result.equivalence == []
    assert lines(result, "waived") == []


@pytest.mark.parametrize("waiver", ["", "  ", "one\ntwo", 7])
def test_a_waiver_is_one_line_of_text(design, waiver):
    vet = design.vet()
    with pytest.raises(VetError, match="waiver"):
        sync(vet).set_param("STAGES", "3", was="2", waiver=waiver)
    with pytest.raises(VetError, match="waiver"):
        timer(vet).connect("en_i", "~en_i[0]", was="en_i[0]", waiver=waiver)
    with pytest.raises(VetError, match="waiver"):
        vet.module("core").add_assign("floating", "~en_i[0]", waiver=waiver)
    assert design.changed(vet) == set()


# --- L3: the comparison ---------------------------------------------------------------------------


@needs_yosys
def test_connect_that_changes_the_function_fails(design):
    vet = design.vet()
    timer(vet).connect("en_i", "en_i[0] & en_i[1]", was="en_i[0]")
    result = vet.finish()
    assert records(result) == [("core", "refuted")]
    (record,) = result.equivalence
    assert record.operations == ["connect core.u_timer.en_i"]
    assert lines(result, "not equivalent") == [f"  not equivalent: core: {record.detail} (connect core.u_timer.en_i)"]
    assert not result.ok


@needs_yosys
def test_connect_that_keeps_the_function_is_proved(design):
    vet = design.vet()
    timer(vet).connect("en_i", "en_i[0] & 1'b1", was="en_i[0]")
    result = vet.finish()
    assert records(result) == [("core", "proved")]
    assert lines(result, "equivalence proved") == ["  equivalence proved: core (connect core.u_timer.en_i)"]
    assert result.ok, result.report()


@needs_yosys
def test_add_assign_to_a_net_that_nothing_reads_is_proved(design):
    vet = design.vet()
    vet.module("core").add_assign("floating", "en_i[0] ^ en_i[1]")
    result = vet.finish()
    assert records(result) == [("core", "proved")]
    assert result.equivalence[0].operations == ["add_assign core.floating"]
    assert result.ok, result.report()


@needs_yosys
def test_add_assign_to_a_net_that_an_output_reads_is_never_proved(tmp_path):
    design = Design(tmp_path / "design", dict(FILES, core=READ))
    vet = design.vet()
    vet.module("core").add_assign("floating", "en_i[0] | en_i[1]")
    result = vet.finish()
    (record,) = result.equivalence
    assert record.status in ("refuted", "error"), record
    assert not result.ok


@needs_yosys
def test_set_param_that_changes_the_child_is_never_proved(design):
    vet = design.vet()
    sync(vet).set_param("STAGES", "3", was="2")
    result = vet.finish()
    (record,) = result.equivalence
    assert record.module == "core" and record.status in ("refuted", "error"), record
    assert record.operations == ["set_param core.u_sync.STAGES"]
    assert not result.ok


@needs_yosys
def test_set_param_to_the_same_value_in_other_words_is_proved(design):
    vet = design.vet()
    sync(vet).set_param("STAGES", "1 + 1", was="2")
    result = vet.finish()
    assert records(result) == [("core", "proved")]
    assert result.ok, result.report()


@needs_yosys
def test_a_waiver_keeps_the_answer_of_the_check(design):
    vet = design.vet()
    sync(vet).set_param("STAGES", "3", was="2", waiver="see TB-9")
    result = vet.finish()
    (record,) = result.equivalence
    assert (record.status, record.waiver) == ("waived", "see TB-9")
    assert record.check in ("refuted", "error")
    (line,) = lines(result, "waived")
    assert line.startswith('  waived: set_param core.u_sync.STAGES: "see TB-9" (the check says: ')
    assert result.ok, result.report()


@needs_yosys
def test_replace_module_by_an_equal_cell_is_proved(design):
    vet = design.vet()
    tech(design, vet)
    sync(vet).replace_module("rst_sync_tech", was="rst_sync")
    result = vet.finish()
    assert records(result) == [("core", "proved")]
    assert result.equivalence[0].operations == ["replace_module core.u_sync"]
    assert result.ok, result.report()


@needs_yosys
def test_replace_module_by_another_cell_fails(design):
    vet = design.vet()
    tech(design, vet, TECH_WRONG)
    sync(vet).replace_module("rst_sync_tech", was="rst_sync")
    result = vet.finish()
    (record,) = result.equivalence
    assert record.status in ("refuted", "error"), record
    assert record.operations == ["replace_module core.u_sync"]
    assert not result.ok


@needs_yosys
def test_the_parts_of_replace_module_are_one_label(design):
    vet = design.vet()
    tech(design, vet)
    sync(vet).replace_module("rst_sync_tech", was="rst_sync", set_params={"STAGES": "3"}, waiver="see TB-9")
    result = vet.finish()
    (record,) = result.equivalence
    assert record.operations == ["replace_module core.u_sync"]
    assert record.status == "waived"


# --- L3, L4: several operations in one module -----------------------------------------------------


@needs_yosys
def test_the_labels_are_in_the_order_of_the_calls(design):
    vet = design.vet()
    core = vet.module("core")
    core.add_assign("floating", "en_i[0] ^ en_i[1]")
    timer(vet).connect("en_i", "en_i[0] & 1'b1", was="en_i[0]")
    sync(vet).set_param("STAGES", "1 + 1", was="2")
    result = vet.finish()
    (record,) = result.equivalence
    assert record.operations == [
        "add_assign core.floating",
        "connect core.u_timer.en_i",
        "set_param core.u_sync.STAGES",
    ]
    assert record.status == "proved"


@needs_yosys
def test_one_waiver_does_not_cover_the_other_operation(design):
    vet = design.vet()
    vet.module("core").add_assign("floating", "en_i[0] ^ en_i[1]", waiver="see TB-9")
    timer(vet).connect("en_i", "en_i[0] & en_i[1]", was="en_i[0]")
    result = vet.finish()
    assert records(result) == [("core", "refuted")]
    assert not result.ok


@needs_yosys
def test_a_raw_splice_and_a_logic_call_share_the_record(design):
    vet = design.vet()
    design.replace(vet, "core", b"assign count_o  = cnt;", b"assign count_o  = cnt | 8'd0;")
    timer(vet).connect("en_i", "en_i[0] & 1'b1", was="en_i[0]")
    result = vet.finish()
    (record,) = result.equivalence
    assert record.operations == ["splice core", "connect core.u_timer.en_i"]
    assert record.status == "proved"


# --- L6: taking back ------------------------------------------------------------------------------


def test_a_call_that_fails_leaves_no_mark(design, monkeypatch):
    no_yosys(monkeypatch)
    vet = design.vet()
    with pytest.raises(DriftError):
        timer(vet).connect("en_i", "~en_i[0]", was="en_i[1]", waiver="see TB-9")
    with pytest.raises(DriftError):
        sync(vet).set_param("STAGES", "3", was="4", waiver="see TB-9")
    result = vet.finish()
    assert result.equivalence == []
    assert result.ok, result.report()


def test_a_group_that_fails_takes_its_marks_back(design, monkeypatch):
    no_yosys(monkeypatch)
    vet = design.vet()
    with pytest.raises(RuntimeError), vet.group():
        sync(vet).set_param("STAGES", "3", was="2", waiver="see TB-9")
        timer(vet).connect("en_i", "~en_i[0]", was="en_i[0]", waiver="see TB-9")
        raise RuntimeError("stop")
    result = vet.finish()
    assert result.equivalence == []
    assert design.changed(vet) == set()
    assert result.ok, result.report()


# --- L7: operations built from the same parts -----------------------------------------------------


def test_punch_port_and_propagate_param_are_not_logic_calls(design, monkeypatch):
    no_yosys(monkeypatch)
    vet = design.vet()
    with_enable(vet)
    vet.propagate_param("STAGES", to="top.u_core.u_sync")
    result = vet.finish()
    assert result.equivalence == []
    assert result.ok, result.report()


# --- L8: a saved session --------------------------------------------------------------------------


def test_a_resumed_session_keeps_the_marks(design, tmp_path, monkeypatch):
    no_yosys(monkeypatch)
    vet = design.vet()
    sync(vet).set_param("STAGES", "3", was="2", waiver="see TB-9")
    timer(vet).connect("en_i", "~en_i[0]", was="en_i[0]", waiver="see TB-10")
    saved = tmp_path / "session.zip"
    vet.save(saved)
    result = type(vet).restore(saved).finish()
    (record,) = result.equivalence
    assert record.operations == ["set_param core.u_sync.STAGES", "connect core.u_timer.en_i"]
    assert (record.status, record.waiver) == ("waived", "see TB-9; see TB-10")


def test_the_format_of_the_archive_is_4():
    assert archive.FORMAT == 5


# --- L9: the other checks -------------------------------------------------------------------------


def test_a_waiver_does_not_silence_the_post_condition(design, monkeypatch):
    no_yosys(monkeypatch)
    vet = design.vet()
    timer(vet).connect("en_i", "~en_i[0]", was="en_i[0]", waiver="see TB-9")
    design.replace(vet, "core", b".en_i(~en_i[0])", b".en_i(~en_i[1])", waiver="set-up of the test")
    result = vet.finish()
    assert not result.ok


# --- L10: the command line ------------------------------------------------------------------------

SCRIPT = 'def apply(vet):\n    vet.module("core").instance("u_sync").set_param("STAGES", "3", was="2"{waiver})\n'


@needs_yosys
@pytest.mark.parametrize("waiver, status", [("", 1), (', waiver="see TB-9"', 0)])
def test_the_exit_status(design, tmp_path, capsys, monkeypatch, waiver, status):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "edit.py").write_text(SCRIPT.format(waiver=waiver), encoding="utf-8")
    out = tmp_path / "out"
    args = ["apply", "--no-project", "-f", design.filelist, "--top", "top", "-s", "edit.py", "-o", out]
    code = main([str(a) for a in args])
    err = capsys.readouterr().err
    assert code == status, err
    if status == 0:
        assert 'waived: set_param core.u_sync.STAGES: "see TB-9"' in err
        manifest = json.loads((out / "hdl-vet.json").read_text(encoding="utf-8"))
        assert [w["operation"] for w in manifest["waivers"]] == ["set_param core.u_sync.STAGES"]
