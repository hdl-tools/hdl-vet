"""Phase 6, part 2: `override_net`, the equivalence check at sign-off, and the waiver.

`override_net` is the first operation that changes logic. It puts a mux on a net:
a new enable selects an override value, for example a DFT bypass of a reset
synchroniser. Because it changes logic, the sign-off proves that the edited
module behaves as before when the enable is 0, with the equivalence engine of
part 1 (`hdl_vet.equiv`, tests/equiv/test_equiv.py).

    def apply(vet):
        vet.punch_port("test_en_i", "input", to="top.u_core")
        core = vet.module("core")
        core.override_net("rst_sync_n", "rst_ni", enable="test_en_i")

    // before                                   // after
    logic rst_sync_n;                           logic rst_sync_n;
    rst_sync u_sync (.rst_no(rst_sync_n), ...); logic rst_sync_n_pre;
                                                rst_sync u_sync (.rst_no(rst_sync_n_pre), ...);
                                                assign rst_sync_n = test_en_i ? rst_ni : rst_sync_n_pre;

Owner decisions (2026-11-17)

- The first version overrides a net that one `assign` statement drives, or the
  output pin of one instance. A net driven in an `always` block is refused.
- The sign-off proves the edited module. How the enable is wired above it is
  shown by the structure comparison (the new port and its connection), not
  proved.
- A waiver is an argument of the operation, printed in the sign-off report and
  stored in `hdl-vet.json`. With a waiver the check still runs when Yosys is
  there; its answer is reported, and only the waiver decides.
- Yosys is the optional extra `hdl-vet[equiv]`. Without it a logic operation is
  refused unless it carries a waiver.

THE OPERATION

O1  `Module.override_net(net, value, *, enable, keep=None, waiver=None)`.
    `net` is a signal of the module (a variable, a net or an output port) whose
    whole width has exactly one driver, and that driver is an `assign net = ...;`
    statement or the connection `.pin(net)` of one instance output.
    `value` is the text of the override, an expression over signals of the
    module. `enable` is the name of an input port of the module, one bit wide;
    the script adds it before (`add_port`, `punch_port`). `keep` names the new
    signal that carries the original value; the default is `<net>_pre`.
O2  What it writes. A declaration of `keep` with the type and width of `net`;
    the driver now drives `keep` (the left side of the assign, or the pin
    connection); and `assign net = enable ? value : keep;`. Nothing else changes.
O3  Refused when called, with a VetError that starts with
    "override_net <module>: " and nothing queued: no such signal; `net` is an
    input port; no driver, more than one driver, a driver for a part of the net,
    a driver in an `always` block, in a generate block or an instance array;
    `enable` is not an input port of the module, or is wider than one bit, or is
    `net`; `keep` or the default name exists already or is not a simple
    identifier; `value` is empty; the module is elaborated more than once and the
    count was not confirmed (`vet.module(name, instances=N)`, as for every
    operation on a module); Yosys is missing and there is no waiver (the message
    names `hdl-vet[equiv]`).
O4  A second run is skipped: the net is already driven by
    `assign net = enable ? value : keep;`. If that assign is there with another
    enable, value or kept signal, the operation stops with a DriftError.
O5  It is an operation like the others: recorded in `vet.log` as
    ("override_net", "<module>.<net>", applied or skipped); allowed in a group
    and in a scope (the module must be inside the scope); kept in a saved
    session.

THE SIGN-OFF

O6  For every module that a logic operation edited, the sign-off compares the
    module of the input design (gold) with the edited module (gate), once for
    each set of parameter values the module is elaborated with. The enable of
    every override in the module is tied to 0. A child module whose text, and
    the text of everything below it, did not change in the run is not looked
    into (`same`). All other edits of the run in that module are part of the
    gate: a new input must have no effect, and a changed connection is a change
    of behaviour.
O7  The result has a record for each comparison: `Result.equivalence`, a list of
    records with `module`, `params` (name to value, as text), `operations` (the
    logic operations of the module, as "override_net core.rst_sync_n"),
    `status`, `detail`, `waiver` and `check`.
      status "proved"   the engine proved it
      status "refuted"  the engine did not; `detail` names the signals
      status "error"    the engine could not judge (its reason is in `detail`)
      status "waived"   every logic operation of the module has a waiver;
                        `waiver` is the text (several texts joined by "; "),
                        and `check` is what the engine answered ("proved",
                        "refuted", "error"), or None when Yosys is missing
    `Result.ok` is false when a record is "refuted" or "error". One waiver does
    not cover another operation: a module with two overrides of which one has no
    waiver is judged by the engine.
O8  The report (`Result.report()`) has a line for every record, after the lines
    of the other checks:
      "  equivalence proved: core (override_net core.rst_sync_n; equal when test_en_i is 0)"
      "  not equivalent: core: <detail> (override_net core.rst_sync_n)"
      "  equivalence not checked: core: <detail> (override_net core.rst_sync_n)"
      "  waived: override_net core.rst_sync_n: \\"<waiver>\\" (the check says: refuted, <detail>)"
    `vet apply` prints these lines on stderr also when the sign-off is ok. The
    manifest `hdl-vet.json` gains "equivalence": a list of
    {"module", "params", "operations", "status", "detail"}, and "waivers": a
    list of {"operation", "waiver", "check"}; both are there (empty) when no
    logic operation ran.
O9  `vet.check()` and `--dry-run` run the same comparison as `finish`.

LIMITS, STATED

- The proof is of the edited module only. An enable that a parent ties to 1 is
  not seen by the proof (the structure comparison shows the connection).
- What the engine cannot judge needs a waiver: a module with flops on more than
  one clock of its own, latches, initial values (tests/equiv/test_equiv_review.py).
- A raw edit (`vet.splice`) of logic is still not proved. Whether every raw edit
  inside a module should need a proof or a waiver is a question for the owner.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hdl_vet import equiv
from hdl_vet.cli import main
from hdl_vet.ops import DriftError, Vet, VetError
from override_design import CORE, FILES, TOP, Design, needs_yosys, records, statuses, with_enable

pytestmark = [pytest.mark.equiv]


@pytest.fixture
def design(tmp_path: Path) -> Design:
    return Design(tmp_path / "design")


@pytest.fixture
def vet(design: Design) -> Vet:
    return design.vet()


# --- O1, O2: what it writes -----------------------------------------------------------------------


def test_the_design_is_clean(vet):
    result = vet.finish()
    assert result.ok, result.report()
    assert result.equivalence == []


@needs_yosys
def test_an_override_on_the_output_pin_of_an_instance(vet, design):
    core = with_enable(vet)
    core.override_net("rst_sync_n", "rst_ni", enable="test_en_i")
    text = design.text(vet, "core")
    assert "  logic       rst_sync_n;\n" in text
    assert "logic rst_sync_n_pre;" in text
    assert ".rst_no(rst_sync_n_pre));" in text
    assert "assign rst_sync_n = test_en_i ? rst_ni : rst_sync_n_pre;" in text
    assert ".rst_ni(rst_sync_n)," in text  # the readers of the net are untouched
    assert "assign rst_o    = rst_sync_n;" in text
    assert statuses(vet)[-1] == ("override_net", "core.rst_sync_n", "applied")
    result = vet.finish()
    assert result.ok, result.report()
    assert records(result) == [("core", "proved")]


@needs_yosys
def test_an_override_on_a_net_that_an_assign_drives(vet, design):
    core = with_enable(vet)
    core.override_net("irq_o", "1'b0", enable="test_en_i")
    text = design.text(vet, "core")
    assert "logic irq_o_pre;" in text
    assert "assign irq_o_pre    = irq_raw & en_i[1] & seen & (split == en_i);" in text or (
        "assign irq_o_pre = irq_raw & en_i[1] & seen & (split == en_i);" in text
    )
    assert "assign irq_o = test_en_i ? 1'b0 : irq_o_pre;" in text
    assert "  output logic       irq_o,\n" in text
    result = vet.finish()
    assert result.ok, result.report()
    assert records(result) == [("core", "proved")]


@needs_yosys
def test_the_kept_signal_has_the_width_of_the_net(vet, design):
    core = with_enable(vet)
    core.override_net("count_o", "8'hFF", enable="test_en_i", keep="count_raw")
    core.override_net("cnt", "8'h00", enable="test_en_i")
    text = design.text(vet, "core")
    assert "logic [7:0] count_raw;" in text
    assert "assign count_o = test_en_i ? 8'hFF : count_raw;" in text
    assert "logic [7:0] cnt_pre;" in text
    assert ".count_o(cnt_pre)," in text
    assert "assign cnt = test_en_i ? 8'h00 : cnt_pre;" in text
    result = vet.finish()
    assert result.ok, result.report()
    assert records(result) == [("core", "proved")]
    (record,) = result.equivalence
    assert record.operations == ["override_net core.count_o", "override_net core.cnt"]
    assert record.waiver is None


def test_only_the_module_of_the_net_is_edited(vet, design, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    core = with_enable(vet)
    core.override_net("rst_sync_n", "rst_ni", enable="test_en_i", waiver="see the tests of the reset")
    assert design.changed(vet) == {"core", "top"}  # top only through punch_port
    top = design.text(vet, "top")
    assert "rst_sync_n" not in top


# --- O3: refusals ---------------------------------------------------------------------------------

REFUSALS = [
    ({"net": "nope"}, "nope"),
    ({"net": "clk_i"}, "input"),
    ({"net": "floating"}, "no driver|not driven|undriven"),
    ({"net": "split"}, "part|more than one driver|two drivers"),
    ({"net": "seen"}, "always"),
    ({"enable": "rst_sync_n"}, "input port"),
    ({"enable": "irq_o"}, "input port"),
    ({"enable": "en_i"}, "one bit|1 bit"),
    ({"enable": "nope_i"}, "nope_i"),
    ({"net": "test_en_i"}, "input"),
    ({"keep": "cnt"}, "cnt"),
    ({"keep": "a b"}, "simple identifier"),
    ({"value": ""}, "value"),
    ({"value": "   "}, "value"),
]


@pytest.mark.parametrize("change, needle", REFUSALS, ids=[str(sorted(c.items())) for c, _ in REFUSALS])
def test_what_cannot_be_overridden_is_refused(design, change, needle):
    vet = design.vet()
    core = with_enable(vet)
    before = statuses(vet)
    call = {"net": "rst_sync_n", "value": "rst_ni", "enable": "test_en_i", **change}
    net, value = call.pop("net"), call.pop("value")
    with pytest.raises(VetError, match=needle) as e:
        core.override_net(net, value, **call)
    assert str(e.value).startswith("override_net core: ")
    assert not isinstance(e.value, DriftError)
    assert statuses(vet) == before
    assert "_pre" not in design.text(vet, "core")


def test_the_default_name_of_the_kept_signal_must_be_free(tmp_path: Path):
    files = dict(FILES)
    files["core"] = CORE.replace(
        "  logic       floating;\n", "  logic       floating;\n  logic       rst_sync_n_pre;\n"
    )
    design = Design(tmp_path / "design", files)
    vet = design.vet()
    core = with_enable(vet)
    with pytest.raises(VetError, match="rst_sync_n_pre"):
        core.override_net("rst_sync_n", "rst_ni", enable="test_en_i")


def test_without_yosys_a_logic_operation_needs_a_waiver(vet, design, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    core = with_enable(vet)
    with pytest.raises(VetError, match=r"hdl-vet\[equiv\]") as e:
        core.override_net("rst_sync_n", "rst_ni", enable="test_en_i")
    assert "waiver" in str(e.value)
    assert "_pre" not in design.text(vet, "core")
    core.override_net("rst_sync_n", "rst_ni", enable="test_en_i", waiver="simulation tb_reset, case 4")
    result = vet.finish()
    assert result.ok, result.report()
    (record,) = result.equivalence
    assert (record.status, record.check) == ("waived", None)
    assert record.waiver == "simulation tb_reset, case 4"
    assert 'waived: override_net core.rst_sync_n: "simulation tb_reset, case 4"' in result.report()


@pytest.mark.parametrize("waiver", ["", "   ", 3, ["x"]])
def test_a_waiver_is_a_text_that_says_something(vet, waiver):
    core = with_enable(vet)
    with pytest.raises((VetError, TypeError), match="waiver"):
        core.override_net("rst_sync_n", "rst_ni", enable="test_en_i", waiver=waiver)


def test_a_module_elaborated_twice_needs_its_count(vet, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    vet.punch_port("test_en_i", "input", to="top.u_core.u_sync", all_instances=True)
    with pytest.raises(VetError, match="instances=2"):
        vet.module("rst_sync").override_net("rst_no", "rst_ni", enable="test_en_i", waiver="w")


# --- O4, O5 ---------------------------------------------------------------------------------------


@needs_yosys
def test_a_second_run_is_skipped_and_another_value_is_drift(vet, design):
    core = with_enable(vet)
    vet.punch_port("other_en_i", "input", to="top.u_core")
    core.override_net("rst_sync_n", "rst_ni", enable="test_en_i")
    once = design.text(vet, "core")
    core.override_net("rst_sync_n", "rst_ni", enable="test_en_i")
    assert design.text(vet, "core") == once
    assert [s for s in statuses(vet) if s[0] == "override_net"] == [
        ("override_net", "core.rst_sync_n", "applied"),
        ("override_net", "core.rst_sync_n", "skipped"),
    ]
    for other in ({"value": "1'b1"}, {"enable": "other_en_i"}, {"keep": "other_pre"}):
        call = {"value": "rst_ni", "enable": "test_en_i", **other}
        with pytest.raises(DriftError, match="override_net core"):
            core.override_net("rst_sync_n", call.pop("value"), **call)
    assert design.text(vet, "core") == once
    result = vet.finish()
    assert result.ok, result.report()
    assert records(result) == [("core", "proved")]


@needs_yosys
def test_an_override_in_a_group_and_in_a_scope(vet, design):
    vet.punch_port("test_en_i", "input", to="top.u_core")
    with vet.scope("core"):
        core = vet.module("core")
        with vet.group():
            core.override_net("rst_sync_n", "rst_ni", enable="test_en_i")
            core.override_net("irq_o", "1'b0", enable="test_en_i")
    assert vet.finish().ok


@needs_yosys
def test_an_override_survives_a_saved_session(vet, design, tmp_path):
    core = with_enable(vet)
    core.override_net("rst_sync_n", "rst_ni", enable="test_en_i")
    core.override_net("irq_o", "1'b0", enable="test_en_i", waiver="irq is masked in test mode by design")
    vet.save(tmp_path / "s.zip")
    again = Vet.restore(tmp_path / "s.zip")
    again.module("core").override_net("rst_sync_n", "rst_ni", enable="test_en_i")  # skipped
    result = again.finish()
    assert result.ok, result.report()
    (record,) = result.equivalence
    assert record.module == "core"
    assert record.status == "proved"  # one of the two has no waiver, so the engine decides
    assert record.operations == ["override_net core.rst_sync_n", "override_net core.irq_o"]


# --- O6, O7, O8: the sign-off ---------------------------------------------------------------------


@needs_yosys
def test_a_miswired_override_fails_the_sign_off(vet, design):
    core = with_enable(vet)
    core.override_net("rst_sync_n", "rst_ni", enable="test_en_i")
    design.replace(vet, "core", b"test_en_i ? rst_ni : rst_sync_n_pre", b"test_en_i ? rst_sync_n_pre : rst_ni")
    result = vet.finish()
    assert not result.ok
    (record,) = result.equivalence
    assert (record.module, record.status) == ("core", "refuted")
    assert record.detail
    report = result.report()
    assert "hdl-vet sign-off: FAILED" in report
    assert "  not equivalent: core: " in report
    assert "(override_net core.rst_sync_n, splice core)" in report


@needs_yosys
def test_another_change_of_behaviour_in_the_module_is_part_of_the_proof(vet, design):
    """A connection changed by a structural operation is declared for the structure, not for the logic."""
    core = with_enable(vet)
    core.override_net("rst_sync_n", "rst_ni", enable="test_en_i")
    core.instance("u_timer").connect("en_i", "en_i[1]", was="en_i[0]")
    result = vet.finish()
    assert not result.ok
    assert records(result) == [("core", "refuted")]
    assert result.unexpected_changes == []


@needs_yosys
def test_the_report_names_what_was_proved(vet):
    core = with_enable(vet)
    core.override_net("rst_sync_n", "rst_ni", enable="test_en_i")
    result = vet.check()
    assert result.ok
    assert "  equivalence proved: core (override_net core.rst_sync_n; equal when test_en_i is 0)" in result.report()
    (record,) = result.equivalence
    assert record.params == {}
    assert vet.finish().ok


@needs_yosys
def test_a_waiver_decides_and_the_check_is_still_reported(vet, design):
    core = with_enable(vet)
    core.override_net("rst_sync_n", "rst_ni", enable="test_en_i", waiver="covered by tb_reset")
    design.replace(
        vet,
        "core",
        b"test_en_i ? rst_ni : rst_sync_n_pre",
        b"test_en_i ? rst_sync_n_pre : rst_ni",
        waiver="covered by tb_reset",
    )
    result = vet.finish()
    assert result.ok, result.report()
    (record,) = result.equivalence
    assert (record.status, record.check, record.waiver) == ("waived", "refuted", "covered by tb_reset")
    report = result.report()
    assert '  waived: override_net core.rst_sync_n: "covered by tb_reset" (the check says: refuted' in report
    assert "not equivalent" not in report


@needs_yosys
def test_a_waiver_on_an_edit_that_is_proved(vet):
    core = with_enable(vet)
    core.override_net("rst_sync_n", "rst_ni", enable="test_en_i", waiver="not needed, but given")
    result = vet.finish()
    assert result.ok
    (record,) = result.equivalence
    assert (record.status, record.check) == ("waived", "proved")
    assert "(the check says: proved)" in result.report()


@needs_yosys
def test_one_waiver_does_not_cover_another_operation(vet, design):
    core = with_enable(vet)
    core.override_net("rst_sync_n", "rst_ni", enable="test_en_i", waiver="covered by tb_reset")
    core.override_net("irq_o", "1'b0", enable="test_en_i")
    design.replace(vet, "core", b"test_en_i ? rst_ni : rst_sync_n_pre", b"test_en_i ? rst_sync_n_pre : rst_ni")
    result = vet.finish()
    assert not result.ok
    assert records(result) == [("core", "refuted")]


@needs_yosys
def test_each_set_of_parameter_values_is_proved(vet, design):
    """`rst_sync` is elaborated with 2 and with 3 stages; the override is in the module itself."""
    vet.punch_port("test_en_i", "input", to="top.u_core.u_sync", all_instances=True)
    sync = vet.module("rst_sync", instances=2)
    sync.override_net("rst_no", "rst_ni", enable="test_en_i")
    result = vet.finish()
    assert result.ok, result.report()
    found = sorted((r.module, r.params.get("STAGES"), r.status) for r in result.equivalence)
    assert found == [("rst_sync", "2", "proved"), ("rst_sync", "3", "proved")]
    assert "STAGES=3" in result.report()


@needs_yosys
def test_a_parent_and_a_child_that_both_have_an_override(vet, design):
    vet.punch_port("test_en_i", "input", to="top.u_core.u_sync", all_instances=True)
    vet.module("rst_sync", instances=2).override_net("rst_no", "rst_ni", enable="test_en_i")
    vet.module("core").override_net("irq_o", "1'b0", enable="test_en_i")
    result = vet.finish()
    assert result.ok, result.report()
    assert sorted(set(records(result))) == [("core", "proved"), ("rst_sync", "proved")]


@needs_yosys
def test_a_module_the_engine_cannot_judge_needs_a_waiver(tmp_path: Path):
    """`core` gets a second clock of its own: the engine answers "error", and the sign-off fails."""
    files = dict(FILES)
    two_clocks = CORE.replace(
        "  input  logic       rst_ni,\n", "  input  logic       rst_ni,\n  input  logic       clk2_i,\n"
    )
    two_clocks = two_clocks.replace(
        "  always_ff @(posedge clk_i or negedge rst_slow_n)", "  always_ff @(posedge clk2_i or negedge rst_slow_n)"
    )
    two_clocks = two_clocks.replace("  logic       floating;\n", "  logic       floating;\n  logic       rst_q;\n")
    two_clocks = two_clocks.replace(
        "  assign rst_o    = rst_sync_n;\n",
        "  always_ff @(posedge clk_i) rst_q <= rst_sync_n;\n  assign rst_o    = rst_q;\n",
    )
    assert two_clocks.count("clk2_i") == 2 and "rst_q <= rst_sync_n" in two_clocks
    files["core"] = two_clocks
    files["top"] = TOP.replace("    .rst_ni  (rst_ni),\n", "    .rst_ni  (rst_ni),\n    .clk2_i  (clk_i),\n")
    design = Design(tmp_path / "design", files)

    vet = design.vet()
    core = with_enable(vet)
    core.override_net("rst_sync_n", "rst_ni", enable="test_en_i")
    result = vet.finish()
    assert not result.ok
    (record,) = result.equivalence
    assert record.status == "error"
    assert "clock" in record.detail
    assert "  equivalence not checked: core: " in result.report()

    again = design.vet()
    core = with_enable(again)
    core.override_net("rst_sync_n", "rst_ni", enable="test_en_i", waiver="two clocks; covered by tb_cdc")
    result = again.finish()
    assert result.ok, result.report()
    assert (result.equivalence[0].status, result.equivalence[0].check) == ("waived", "error")


@needs_yosys
def test_the_proof_is_of_the_edited_module_only(vet):
    """The stated limit: a parent that ties the enable to 1 is not seen by the proof."""
    core = with_enable(vet)
    core.override_net("rst_sync_n", "rst_ni", enable="test_en_i")
    vet.module("top").instance("u_core").connect("test_en_i", "1'b1", was="test_en_i")
    result = vet.finish()
    assert records(result) == [("core", "proved")]


# --- O8, O9: the command line and the manifest ------------------------------------------------------

SCRIPT = (
    "def apply(vet):\n"
    '    vet.punch_port("test_en_i", "input", to="top.u_core")\n'
    '    core = vet.module("core")\n'
    '    core.override_net("rst_sync_n", "rst_ni", enable="test_en_i")\n'
    '    core.override_net("irq_o", "1\'b0", enable="test_en_i", waiver="irq is masked in test mode")\n'
)


def run(*args: object) -> int:
    return main([str(a) for a in args])


@needs_yosys
def test_the_command_line_prints_the_lines_and_the_manifest_keeps_them(design, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "dft.py").write_text(SCRIPT, encoding="utf-8")
    assert run("apply", "--no-project", "-f", design.filelist, "--top", "top", "-s", "dft.py", "--dry-run") == 0
    dry = capsys.readouterr()
    assert "equivalence proved: core (override_net core.rst_sync_n, override_net core.irq_o; " in dry.err + dry.out

    out = tmp_path / "out"
    assert run("apply", "--no-project", "-f", design.filelist, "--top", "top", "-s", "dft.py", "-o", out) == 0
    capsys.readouterr()
    manifest = json.loads((out / "hdl-vet.json").read_text(encoding="utf-8"))
    assert manifest["equivalence"] == [
        {
            "module": "core",
            "params": {},
            "operations": ["override_net core.rst_sync_n", "override_net core.irq_o"],
            "status": "proved",
            "detail": manifest["equivalence"][0]["detail"],
        }
    ]
    assert manifest["waivers"] == [
        {"operation": "override_net core.irq_o", "waiver": "irq is masked in test mode", "check": "proved"}
    ]


def test_a_run_without_logic_operations_has_empty_lists(design, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plain.py").write_text('def apply(vet):\n    vet.module("top").add_wire("spare")\n', encoding="utf-8")
    out = tmp_path / "out"
    assert run("apply", "--no-project", "-f", design.filelist, "--top", "top", "-s", "plain.py", "-o", out) == 0
    captured = capsys.readouterr()
    assert "equivalence" not in captured.err + captured.out
    manifest = json.loads((out / "hdl-vet.json").read_text(encoding="utf-8"))
    assert manifest["equivalence"] == []
    assert manifest["waivers"] == []


@needs_yosys
def test_a_refuted_override_stops_the_command(design, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    core_file = design.path("core").as_posix()
    (tmp_path / "bad.py").write_text(
        "from pathlib import Path\n\n\n"
        "def apply(vet):\n"
        '    vet.punch_port("test_en_i", "input", to="top.u_core")\n'
        '    vet.module("core").override_net("rst_sync_n", "rst_ni", enable="test_en_i")\n'
        f'    path = Path("{core_file}")\n'
        "    vet.design  # load the edits that are queued, so that the text shows them\n"
        "    data = vet.text(path)\n"
        '    old = b"test_en_i ? rst_ni : rst_sync_n_pre"\n'
        "    at = data.index(old)\n"
        '    vet.splice(path, at, at + len(old), b"test_en_i ? rst_sync_n_pre : rst_ni", old=old)\n',
        encoding="utf-8",
    )
    out = tmp_path / "out"
    assert run("apply", "--no-project", "-f", design.filelist, "--top", "top", "-s", "bad.py", "-o", out) == 1
    captured = capsys.readouterr()
    assert "not equivalent: core: " in captured.err
    assert not out.exists()
