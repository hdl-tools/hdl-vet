"""Phase 6, part 1: the equivalence engine. Specification and acceptance tests.

Phase 6 adds operations that change logic. Each such edit is checked by a formal
equivalence check of the module it edits, before and after. This part is the
engine alone, `hdl_vet.equiv`: no operation uses it yet (`override_net` and the
sign-off rule follow in part 2).

Owner decisions (2026-11-17)

- The engine is Yosys from PyPI (`yowasp-yosys`, pinned), an optional extra:
  `pip install hdl-vet[equiv]`. Without it everything of today works unchanged.

What a spike established on this machine (scratch folder `spike_equiv`, with the
working Yosys scripts): `read_slang` is built into yowasp-yosys 0.71 with slang
12.1.0; it needs `-j 1` and paths with forward slashes; every check runs in a
process of its own (a call in this process keeps about 100 MB); asynchronous
resets need `async2sync`; the flow `equiv_make` / `equiv_simple -seq 5` /
`equiv_induct -seq 5` / `equiv_status -assert` proved the equal cases and refuted
every planted bug, also one that shows only after 254 clock cycles, which a
bounded check passed and a plain miter with temporal induction did not finish;
`miter -ignore_gold_x` proved a wrong design and must not be used.

THE INTERFACE (`src/hdl_vet/equiv.py`; pyslang is not imported here)

    available() -> bool
        True when the Yosys package can be used.

    Source(files, incdirs=(), defines={})
        The files of one side, in file-list order, with its include folders and
        defines. A frozen dataclass.

    check(gold, gate, module, *, params=None, tie=None, same=(), timeout=300.0)
        -> Result(status, module, detail, seconds)
        Compare the module `module` of the Source `gold` (before) with the module
        of the same name of the Source `gate` (after).
        status is "proved", "refuted" or "error". detail is one line of text.

RULES

E1  What is compared. `module` is the top of each side. The two are equal when,
    for every value of the inputs over time, every output port of the gold
    module has the same value in the gate module. "proved" means Yosys proved
    it. "refuted" means it did not; `detail` then names the gold signals that
    were not proven equal (the first five). A check never answers "proved"
    for a design it could not read or a proof it could not finish.
E2  Ports. An input port only the gate has is free: the gate must then equal the
    gold for every value of it, so a new input that has an effect refutes. An
    output port only the gate has is ignored. A port of the gold that the gate
    does not have, or has with another width or direction, refutes, and `detail`
    names the port.
E3  `tie={"test_en_i": 0}` gives an input port that only the gate has a constant
    value for the proof: "equal when test_en_i is 0". A name in `tie` that is not
    a gate-only input port is an error.
E4  `params={"STAGES": "3"}` elaborates both sides with these parameter values
    instead of the defaults.
E5  Children. An instance of a module named in `same` is not looked into: the
    caller states that this module and all below it are the same text on both
    sides. Its input pins are compared like outputs of the parent and its output
    pins are free like inputs, matched by instance name and pin name. Every
    other child is part of the logic compared (flattened), on both sides.
E6  Sequential logic. Flops with an asynchronous reset are handled. A change of
    what clocks a flop is never "proved": it is "refuted", or an "error" whose
    detail says the clocks differ. (Yosys ignores the clock input of a flop in
    this flow unless the engine takes care of it.)
E7  Errors are answers, not exceptions. Yosys missing (the detail names
    `hdl-vet[equiv]`), a file that cannot be read, a syntax error, a module that
    does not exist, a crash of the tool, a timeout (the process is stopped):
    status "error" and a one-line detail. `check` raises only for a wrong call
    (a `tie` or `params` value of a wrong type).
E8  One process per check, started without a shell. Nothing is written beside
    the sources; what the engine writes goes to a temporary folder that it
    removes. The same call gives the same status and detail.
E9  Cost. A check of a small module takes a few seconds at most after the first
    use (the first use on a machine compiles Yosys once, about 15 seconds).

SET-UP THIS PART ADDS

- `pyproject.toml`: the extra `equiv` and the same pin in the `dev` group; a
  pytest marker `equiv` for the tests that need Yosys.
- These tests are skipped when Yosys is missing, unless the environment variable
  HDL_VET_REQUIRE_EQUIV is 1; CI sets it and installs the extra.
- The wheel alone, without the extra, still works (the `package` job).

LEFT OPEN

- Modules with more than one clock are not tested.
- A module too large to flatten (a spike run with 275 000 cells crashed).
- A trace of input values for a refuted check.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

equiv = pytest.importorskip("hdl_vet.equiv")

REQUIRED = os.environ.get("HDL_VET_REQUIRE_EQUIV") == "1"
needs_yosys = pytest.mark.skipif(not equiv.available() and not REQUIRED, reason="Yosys (hdl-vet[equiv]) is missing")
pytestmark = [pytest.mark.equiv]

RST_SYNC = (
    "module rst_sync #(\n"
    "  parameter int STAGES = 2\n"
    ") (\n"
    "  input  logic clk_i,\n"
    "  input  logic rst_ni,\n"
    "  output logic rst_no\n"
    ");\n"
    "  logic [STAGES-1:0] q;\n"
    "\n"
    "  always_ff @(posedge clk_i or negedge rst_ni)\n"
    "    if (!rst_ni) q <= '0;\n"
    "    else         q <= {q[STAGES-2:0], 1'b1};\n"
    "\n"
    "  assign rst_no = q[STAGES-1];\n"
    "endmodule\n"
)
TIMER = (
    "module timer #(\n"
    "  parameter int WIDTH = 8\n"
    ") (\n"
    "  input  logic             clk_i,\n"
    "  input  logic             rst_ni,\n"
    "  input  logic             en_i,\n"
    "  output logic [WIDTH-1:0] count_o,\n"
    "  output logic             irq_o\n"
    ");\n"
    "  always_ff @(posedge clk_i or negedge rst_ni)\n"
    "    if (!rst_ni)   count_o <= '0;\n"
    "    else if (en_i) count_o <= count_o + 1'b1;\n"
    "\n"
    "  assign irq_o = &count_o;\n"
    "endmodule\n"
)
WRAP = (
    "module wrap (\n"
    "  input  logic       clk_i,\n"
    "  input  logic       rst_ni,\n"
    "  input  logic [1:0] en_i,\n"
    "  output logic       irq_o\n"
    ");\n"
    "  logic [1:0] irq;\n"
    "  logic [7:0] c0, c1;\n"
    "  timer u_t0 (.clk_i(clk_i), .rst_ni(rst_ni), .en_i(en_i[0]), .count_o(c0), .irq_o(irq[0]));\n"
    "  timer u_t1 (.clk_i(clk_i), .rst_ni(rst_ni), .en_i(en_i[1]), .count_o(c1), .irq_o(irq[1]));\n"
    "  assign irq_o = |irq;\n"
    "endmodule\n"
)
OVERRIDE = RST_SYNC.replace("  output logic rst_no\n", "  input  logic test_en_i,\n  output logic rst_no\n").replace(
    "assign rst_no = q[STAGES-1];", "assign rst_no = test_en_i ? rst_ni : q[STAGES-1];"
)


def source(folder: Path, *texts: str) -> object:
    """A Source made of one file for each text, written into `folder`."""
    folder.mkdir(parents=True, exist_ok=True)
    files = []
    for number, text in enumerate(texts):
        path = folder / f"m{number}.sv"
        path.write_bytes(text.encode())
        files.append(path)
    return equiv.Source(files)


def run(tmp_path: Path, gold: tuple[str, ...], gate: tuple[str, ...], module: str, **options):
    result = equiv.check(source(tmp_path / "gold", *gold), source(tmp_path / "gate", *gate), module, **options)
    assert result.module == module
    assert "\n" not in result.detail
    return result


# --- the interface without Yosys ------------------------------------------------------------------


def test_the_interface():
    assert isinstance(equiv.available(), bool)
    src = equiv.Source([Path("a.sv")])
    assert list(src.files) == [Path("a.sv")]
    assert list(src.incdirs) == []
    assert dict(src.defines) == {}
    with pytest.raises((AttributeError, TypeError)):
        src.files = []  # frozen


def test_without_yosys_the_answer_is_an_error_that_names_the_extra(tmp_path, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    result = run(tmp_path, (RST_SYNC,), (RST_SYNC,), "rst_sync")
    assert result.status == "error"
    assert "hdl-vet[equiv]" in result.detail


def test_yosys_is_there_when_it_is_required():
    if REQUIRED:
        assert equiv.available()


@pytest.mark.parametrize(
    "options", [{"tie": {"x": "0"}}, {"tie": ["x"]}, {"params": {"STAGES": 3.5}}, {"params": "S=1"}]
)
def test_a_wrong_call_raises(tmp_path, options):
    with pytest.raises((TypeError, ValueError)):
        run(tmp_path, (RST_SYNC,), (RST_SYNC,), "rst_sync", **options)


# --- E1: equal and not equal ----------------------------------------------------------------------


@needs_yosys
def test_the_same_module_is_proved(tmp_path):
    result = run(tmp_path, (RST_SYNC,), (RST_SYNC,), "rst_sync")
    assert result.status == "proved", result.detail
    assert result.seconds > 0


@needs_yosys
def test_a_renamed_internal_signal_is_proved(tmp_path):
    gate = RST_SYNC.replace("q", "stage")
    assert gate != RST_SYNC
    assert run(tmp_path, (RST_SYNC,), (gate,), "rst_sync").status == "proved"


@needs_yosys
def test_the_silent_bug_is_refuted(tmp_path):
    """The mutation that every other check of hdl-vet passes: the shifted-in constant 1 becomes 0."""
    gate = RST_SYNC.replace("1'b1}", "1'b0}")
    assert gate != RST_SYNC
    result = run(tmp_path, (RST_SYNC,), (gate,), "rst_sync")
    assert result.status == "refuted"
    assert "rst_no" in result.detail or "q" in result.detail


@pytest.mark.parametrize(
    "old, new",
    [
        ("count_o <= '0;", "count_o <= 8'd1;"),
        ("count_o + 1'b1", "count_o + 2'd2"),
        ("else if (en_i)", "else if (!en_i)"),
        ("assign irq_o = &count_o;", "assign irq_o = |count_o;"),
        (" or negedge rst_ni)\n    if (!rst_ni)   count_o <= '0;\n    else if (en_i)", ")\n    if (en_i)"),
        ("count_o + 1'b1", "(count_o == 8'd254 ? 8'd0 : count_o + 1'b1)"),
    ],
    ids=["reset-value", "step", "enable", "output", "no-reset", "only-at-254"],
)
@needs_yosys
def test_a_changed_timer_is_refuted(tmp_path, old, new):
    gate = TIMER.replace(old, new)
    assert gate != TIMER
    result = run(tmp_path, (TIMER,), (gate,), "timer")
    assert result.status == "refuted", result.detail
    assert result.detail


@needs_yosys
def test_the_same_logic_written_another_way_is_proved(tmp_path):
    gate = TIMER.replace("assign irq_o = &count_o;", "assign irq_o = (count_o == '1);")
    assert run(tmp_path, (TIMER,), (gate,), "timer").status == "proved"


# --- E2, E3: ports and ties -----------------------------------------------------------------------


@needs_yosys
def test_an_override_is_proved_inert_when_its_enable_is_tied(tmp_path):
    assert run(tmp_path, (RST_SYNC,), (OVERRIDE,), "rst_sync", tie={"test_en_i": 0}).status == "proved"


@needs_yosys
def test_an_override_with_a_free_enable_is_refuted(tmp_path):
    result = run(tmp_path, (RST_SYNC,), (OVERRIDE,), "rst_sync")
    assert result.status == "refuted"
    assert "rst_no" in result.detail


@needs_yosys
def test_a_miswired_override_is_refuted(tmp_path):
    wrong = OVERRIDE.replace("test_en_i ? rst_ni", "!test_en_i ? rst_ni")
    assert run(tmp_path, (RST_SYNC,), (wrong,), "rst_sync", tie={"test_en_i": 0}).status == "refuted"
    assert run(tmp_path / "b", (RST_SYNC,), (OVERRIDE,), "rst_sync", tie={"test_en_i": 1}).status == "refuted"


@needs_yosys
def test_a_new_input_without_effect_and_a_new_output_are_accepted(tmp_path):
    gate = RST_SYNC.replace(
        "  output logic rst_no\n", "  input  logic spare_i,\n  output logic dbg_o,\n  output logic rst_no\n"
    )
    gate = gate.replace("assign rst_no", "assign dbg_o = q[0] ^ spare_i;\n  assign rst_no")
    assert run(tmp_path, (RST_SYNC,), (gate,), "rst_sync").status == "proved"


NO_RESET = (
    RST_SYNC.replace("  input  logic rst_ni,\n", "")
    .replace(" or negedge rst_ni", "")
    .replace("if (!rst_ni) q <= '0;\n    else         q <=", "q <=")
)
WIDER = RST_SYNC.replace("  output logic rst_no\n", "  output logic [1:0] rst_no\n")
RENAMED_PORT = RST_SYNC.replace("rst_no", "other_o")


@pytest.mark.parametrize(
    "gate, port",
    [(NO_RESET, "rst_ni"), (WIDER, "rst_no"), (RENAMED_PORT, "rst_no")],
    ids=["input-gone", "width", "output-gone"],
)
@needs_yosys
def test_a_port_of_the_gold_that_the_gate_lacks_refutes(tmp_path, gate, port):
    assert gate != RST_SYNC
    assert "rst_ni" not in NO_RESET
    result = run(tmp_path, (RST_SYNC,), (gate,), "rst_sync")
    assert result.status == "refuted", result.detail
    assert port in result.detail


@needs_yosys
def test_a_tie_must_name_a_new_input(tmp_path):
    for tie in ({"rst_ni": 1}, {"nope_i": 0}, {"rst_no": 0}):
        result = run(tmp_path / next(iter(tie)), (RST_SYNC,), (OVERRIDE,), "rst_sync", tie=tie)
        assert result.status == "error"
        assert next(iter(tie)) in result.detail


# --- E4: parameters ---------------------------------------------------------------------------------


@needs_yosys
def test_parameters_are_applied_to_both_sides(tmp_path):
    """With STAGES=2 both shift in one step; the changed gate differs only when there are three stages."""
    gate = RST_SYNC.replace("{q[STAGES-2:0], 1'b1}", "(STAGES > 2) ? {q[STAGES-2:0], 1'b0} : {q[STAGES-2:0], 1'b1}")
    assert run(tmp_path / "a", (RST_SYNC,), (gate,), "rst_sync").status == "proved"
    assert run(tmp_path / "b", (RST_SYNC,), (gate,), "rst_sync", params={"STAGES": "3"}).status == "refuted"
    assert run(tmp_path / "c", (RST_SYNC,), (RST_SYNC,), "rst_sync", params={"STAGES": 4}).status == "proved"


# --- E5: children -----------------------------------------------------------------------------------


@needs_yosys
def test_a_parent_with_children_that_are_the_same(tmp_path):
    assert run(tmp_path / "a", (TIMER, WRAP), (TIMER, WRAP), "wrap", same=["timer"]).status == "proved"
    wrong = WRAP.replace("assign irq_o = |irq;", "assign irq_o = &irq;")
    result = run(tmp_path / "b", (TIMER, WRAP), (TIMER, wrong), "wrap", same=["timer"])
    assert result.status == "refuted"
    assert "irq_o" in result.detail


@needs_yosys
def test_a_changed_connection_of_a_child_is_refuted(tmp_path):
    wrong = WRAP.replace(".en_i(en_i[1])", ".en_i(en_i[0])")
    result = run(tmp_path, (TIMER, WRAP), (TIMER, wrong), "wrap", same=["timer"])
    assert result.status == "refuted"
    assert "u_t1" in result.detail


@needs_yosys
def test_a_child_that_is_not_named_same_is_part_of_the_logic(tmp_path):
    changed = TIMER.replace("assign irq_o = &count_o;", "assign irq_o = |count_o;")
    assert run(tmp_path / "a", (TIMER, WRAP), (changed, WRAP), "wrap").status == "refuted"
    renamed = TIMER.replace("assign irq_o = &count_o;", "assign irq_o = (count_o == '1);")
    assert run(tmp_path / "b", (TIMER, WRAP), (renamed, WRAP), "wrap").status == "proved"
    assert run(tmp_path / "c", (TIMER, WRAP), (TIMER, WRAP), "wrap").status == "proved"


@needs_yosys
def test_a_swapped_cell_with_its_scan_pin_tied_in_the_parent(tmp_path):
    """The child differs on the two sides, so it is flattened; the tie is written in the parent."""
    cell = (
        "module tech_sync (\n  input  logic CK,\n  input  logic RN,\n  input  logic SE,\n  output logic Q\n);\n"
        "  logic [1:0] q;\n"
        "  always_ff @(posedge CK or negedge RN)\n    if (!RN) q <= '0;\n    else     q <= {q[0], 1'b1};\n"
        "  assign Q = SE ? RN : q[1];\nendmodule\n"
    )
    parent = (
        "module top (\n  input  logic clk_i,\n  input  logic rst_ni,\n  output logic rst_o\n);\n"
        "  rst_sync u_sync (.clk_i(clk_i), .rst_ni(rst_ni), .rst_no(rst_o));\nendmodule\n"
    )
    swapped = parent.replace(
        "rst_sync u_sync (.clk_i(clk_i), .rst_ni(rst_ni), .rst_no(rst_o));",
        "tech_sync u_sync (.CK(clk_i), .RN(rst_ni), .SE(1'b0), .Q(rst_o));",
    )
    assert run(tmp_path / "a", (RST_SYNC, parent), (cell, swapped), "top").status == "proved"
    live = swapped.replace(".SE(1'b0)", ".SE(1'b1)")
    assert run(tmp_path / "b", (RST_SYNC, parent), (cell, live), "top").status == "refuted"


# --- E6: clocks -------------------------------------------------------------------------------------

TWO_CLOCKS = (
    "module two (\n"
    "  input  logic clk_a,\n"
    "  input  logic clk_b,\n"
    "  input  logic d,\n"
    "  output logic q\n"
    ");\n"
    "  always_ff @(posedge clk_a) q <= d;\n"
    "endmodule\n"
)


@needs_yosys
def test_a_changed_clock_is_never_proved(tmp_path):
    gate = TWO_CLOCKS.replace("@(posedge clk_a)", "@(posedge clk_b)")
    result = run(tmp_path / "a", (TWO_CLOCKS,), (gate,), "two")
    assert result.status in ("refuted", "error"), result.detail
    edge = TWO_CLOCKS.replace("@(posedge clk_a)", "@(negedge clk_a)")
    assert run(tmp_path / "b", (TWO_CLOCKS,), (edge,), "two").status in ("refuted", "error")
    assert run(tmp_path / "c", (TWO_CLOCKS,), (TWO_CLOCKS,), "two").status == "proved"


@needs_yosys
def test_a_gated_clock_is_never_proved(tmp_path):
    gate = TWO_CLOCKS.replace("@(posedge clk_a)", "@(posedge clk_g)").replace(
        "  always_ff", "  logic clk_g;\n  assign clk_g = clk_a & clk_b;\n  always_ff"
    )
    assert run(tmp_path, (TWO_CLOCKS,), (gate,), "two").status in ("refuted", "error")


# --- E7: errors are answers -------------------------------------------------------------------------


@needs_yosys
def test_a_syntax_error_is_an_error(tmp_path):
    broken = RST_SYNC.replace("endmodule", "")
    result = run(tmp_path, (RST_SYNC,), (broken,), "rst_sync")
    assert result.status == "error"
    assert result.detail


@needs_yosys
def test_a_missing_module_and_a_missing_file_are_errors(tmp_path):
    assert run(tmp_path, (RST_SYNC,), (RST_SYNC,), "nope").status == "error"
    gone = equiv.Source([tmp_path / "gone.sv"])
    result = equiv.check(gone, gone, "rst_sync")
    assert result.status == "error"
    assert "gone.sv" in result.detail


@needs_yosys
def test_a_timeout_is_an_error_and_stops_the_process(tmp_path):
    started = time.monotonic()
    result = run(tmp_path, (TIMER,), (TIMER,), "timer", timeout=0.01)
    assert result.status == "error"
    assert "time" in result.detail.lower()
    assert time.monotonic() - started < 30


# --- E8, E9 -----------------------------------------------------------------------------------------


@needs_yosys
def test_nothing_is_left_behind_and_the_answer_is_the_same(tmp_path, monkeypatch):
    work = tmp_path / "work dir"  # a blank in the path
    work.mkdir()
    monkeypatch.chdir(work)
    gate = RST_SYNC.replace("1'b1}", "1'b0}")
    first = run(tmp_path / "with blank", (RST_SYNC,), (gate,), "rst_sync")
    second = equiv.check(
        equiv.Source([tmp_path / "with blank" / "gold" / "m0.sv"]),
        equiv.Source([tmp_path / "with blank" / "gate" / "m0.sv"]),
        "rst_sync",
    )
    assert (first.status, first.detail) == (second.status, second.detail) == ("refuted", first.detail)
    assert list(work.iterdir()) == []
    assert sorted(p.name for p in (tmp_path / "with blank" / "gold").iterdir()) == ["m0.sv"]
    assert sorted(p.name for p in (tmp_path / "with blank" / "gate").iterdir()) == ["m0.sv"]


@needs_yosys
def test_include_folders_and_defines(tmp_path):
    inc = tmp_path / "inc"
    inc.mkdir()
    (inc / "width.svh").write_bytes(b"`define STEP 1'b1\n")
    text = '`include "width.svh"\n' + TIMER.replace("count_o + 1'b1", "count_o + (`ifdef FAST 2'd2 `else `STEP `endif)")
    (tmp_path / "t.sv").write_bytes(text.encode())
    plain = equiv.Source([tmp_path / "t.sv"], incdirs=[inc])
    fast = equiv.Source([tmp_path / "t.sv"], incdirs=[inc], defines={"FAST": None})
    assert equiv.check(plain, plain, "timer").status == "proved"
    assert equiv.check(plain, fast, "timer").status == "refuted"
    assert equiv.check(equiv.Source([tmp_path / "t.sv"]), plain, "timer").status == "error"  # header not found


@needs_yosys
def test_a_small_check_is_fast(tmp_path):
    run(tmp_path / "warm", (RST_SYNC,), (RST_SYNC,), "rst_sync")  # the first use may compile Yosys
    result = run(tmp_path / "timed", (TIMER,), (TIMER,), "timer")
    assert result.status == "proved"
    assert result.seconds < 20
