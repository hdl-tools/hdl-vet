"""The equivalence engine: tests from the adversarial review.

The base specification is the docstring of `tests/test_equiv.py`. The review
(about 130 checks) rejected the engine as first built: it found five kinds of a
wrong "proved". These rules close them, and three wrong "error" answers on equal
designs that a user of generated code will meet.

A WRONG "PROVED" (each was reproduced)

V1  Undefined values. A gate output that is x, or that reads a net nobody
    drives, where the gold has a value, is refuted. (Without Yosys's `-undef`,
    x and an undriven net count as 0.) The other direction is the usual meaning
    of an equivalence check and is part of E1: where the GOLD is x, the gate may
    have any value.
V2  Initial values. The proof is by induction and has no start state, so two
    flops that differ only in their initial value were proved. A module in which
    a signal has an initial value (`logic r = 1'b1;`, an `initial` block), on
    either side, is never "proved": it is an "error" whose detail says "initial
    value". (A flop that is set by a reset has none and is not affected.)
V3  A child named in `same` must be the same on both sides. For every instance
    that is not looked into, the engine compares what it stands for on the two
    sides: the module and its parameter values (and so its text). If they
    differ, the answer is not "proved": an "error" whose detail names the
    instance. This holds when the caller is wrong about `same`, and when the
    caller is right about the text but the parent gives the instance other
    parameter values.
V4  The module of an instance. An instance that is of one `same` module in the
    gold and of another `same` module in the gate is not "proved".
V5  Parameter names. A name in `params` that is not a parameter of `module` on
    both sides is an "error" that names it. (The reader drops an unknown name
    without a word, and the check then runs at the defaults.)

A WRONG "ERROR" OR "REFUTED" ON EQUAL DESIGNS

V6  Unpacked arrays. A module with an unpacked array of signals
    (`logic [7:0] s [3]`), used or unused, is compared like any other logic.
V7  Ties come first. The ties are applied before the engine looks at the clocks,
    so a clock mux whose select is tied is the clock it selects. With the other
    value of the tie the answer is not "proved".

ROBUSTNESS

V8  `check` answers "error" when its temporary folder cannot be made. A boolean
    is refused as a tie value, as it is as a parameter value. A flop whose clock
    edge the netlist does not state is an "error", not a rising edge. Two calls
    of `check` from two threads do not run Yosys at the same time.
V9  Each side is read from its files once per check (the first build read each
    four times: a design of 500 files took 40 seconds for one small module).
V10 Without the extra, `import hdl_vet.equiv` works, `available()` is False and
    `check` answers "error" with the name of the extra. The wheel smoke check
    (`tests/smoke_wheel.py`) covers it.

LIMITS, STATED (each gives "error" or "refuted", never a wrong "proved", except
the last one, which the caller must know)

- A module compared with its children flattened and flops on more than one
  clock: "error". Name the children of each clock domain in `same`.
- A clock that reaches a flop through an inverter, a mux that is not tied or a
  divider; flops on both edges of one clock; latches; two asynchronous resets;
  interface ports: "error".
- A renamed chain of more than five registers, and a rewrite that keeps the name
  of an internal signal but changes what it carries: "refuted".
- A wide multiplier written another way can reach the timeout.
- A change of the signedness of a port, of the order of the fields of a struct
  port or of the order of the ports is "proved": the module computes the same,
  but a parent that connects by position or by field sees a change. The
  structure comparison of the sign-off reports a changed port.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from hdl_vet import equiv
from test_equiv import RST_SYNC, TIMER, WRAP, run

REQUIRED = os.environ.get("HDL_VET_REQUIRE_EQUIV") == "1"
needs_yosys = pytest.mark.skipif(not equiv.available() and not REQUIRED, reason="Yosys (hdl-vet[equiv]) is missing")
pytestmark = [pytest.mark.equiv]


def module(body: str, ports: str = "input logic a, output logic o") -> str:
    return f"module m ({ports});\n{body}endmodule\n"


# --- V1 -----------------------------------------------------------------------------------------

X_PAIRS = {
    "constant-x": (module("  assign o = 1'b0;\n"), module("  assign o = 1'bx;\n")),
    "undriven-or": (module("  assign o = a;\n"), module("  logic w;\n  assign o = a | w;\n")),
    "undriven-xor": (module("  assign o = a;\n"), module("  logic w;\n  assign o = a ^ w;\n")),
    "undriven-output": (module("  assign o = 1'b0;\n"), module("")),
    "mux-arm-x": (
        module("  assign o = s ? a : 1'b0;\n", "input logic a, input logic s, output logic o"),
        module("  assign o = s ? a : 1'bx;\n", "input logic a, input logic s, output logic o"),
    ),
    "flop-from-undriven": (
        module("  always_ff @(posedge c) o <= a & 1'b0;\n", "input logic c, input logic a, output logic o"),
        module("  logic nd;\n  always_ff @(posedge c) o <= nd;\n", "input logic c, input logic a, output logic o"),
    ),
}


@pytest.mark.parametrize("name", sorted(X_PAIRS))
@needs_yosys
def test_an_undefined_value_in_the_gate_is_refuted(tmp_path, name):
    gold, gate = X_PAIRS[name]
    result = run(tmp_path, (gold,), (gate,), "m")
    assert result.status == "refuted", (name, result.detail)
    assert "o" in result.detail


@needs_yosys
def test_an_undefined_value_in_the_gold_lets_the_gate_choose(tmp_path):
    gold = module("  assign o = s ? a : 1'bx;\n", "input logic a, input logic s, output logic o")
    gate = module("  assign o = s ? a : 1'b1;\n", "input logic a, input logic s, output logic o")
    assert run(tmp_path, (gold,), (gate,), "m").status == "proved"


@needs_yosys
def test_the_earlier_cases_still_hold_with_undefined_values_taken_seriously(tmp_path):
    assert run(tmp_path / "a", (RST_SYNC,), (RST_SYNC,), "rst_sync").status == "proved"
    assert run(tmp_path / "b", (TIMER,), (TIMER,), "timer").status == "proved"
    assert run(tmp_path / "c", (TIMER, WRAP), (TIMER, WRAP), "wrap", same=["timer"]).status == "proved"


# --- V2 -----------------------------------------------------------------------------------------

HOLD = (
    "module m (input logic clk, output logic q);\n"
    "  logic r = 1'b{v};\n"
    "  always_ff @(posedge clk) r <= r;\n"
    "  assign q = r;\n"
    "endmodule\n"
)
COUNT = (
    "module m (input logic clk, output logic [3:0] q);\n"
    "  logic [3:0] r;\n"
    "  initial r = 4'd{v};\n"
    "  always_ff @(posedge clk) r <= r + 4'd1;\n"
    "  assign q = r;\n"
    "endmodule\n"
)


@pytest.mark.parametrize("text, a, b", [(HOLD, 0, 1), (COUNT, 0, 5)], ids=["declaration", "initial-block"])
@needs_yosys
def test_a_difference_in_an_initial_value_is_not_proved(tmp_path, text, a, b):
    result = run(tmp_path / "a", (text.format(v=a),), (text.format(v=b),), "m")
    assert result.status == "error", result.detail
    assert "initial value" in result.detail
    same = run(tmp_path / "b", (text.format(v=a),), (text.format(v=a),), "m")
    assert same.status == "error"  # stated plainly: such a module is not proved, also against itself


@needs_yosys
def test_an_initial_value_on_one_side_only_is_not_proved(tmp_path):
    plain = HOLD.replace(" = 1'b{v}", "")
    assert run(tmp_path, (plain,), (HOLD.format(v=1),), "m").status == "error"


# --- V3, V4 -------------------------------------------------------------------------------------

STEP_TIMER = TIMER.replace(
    "parameter int WIDTH = 8", "parameter int WIDTH = 8,\n  parameter logic [7:0] STEP = 8'd1"
).replace("count_o + 1'b1", "count_o + STEP")


@needs_yosys
def test_a_same_child_with_other_parameter_values_is_not_proved(tmp_path):
    assert STEP_TIMER != TIMER
    assert run(tmp_path / "a", (STEP_TIMER, WRAP), (STEP_TIMER, WRAP), "wrap", same=["timer"]).status == "proved"
    gate = WRAP.replace("timer u_t1", "timer #(.STEP(8'd2)) u_t1")
    result = run(tmp_path / "b", (STEP_TIMER, WRAP), (STEP_TIMER, gate), "wrap", same=["timer"])
    assert result.status == "error", result.detail
    assert "u_t1" in result.detail


@needs_yosys
def test_a_same_child_whose_text_differs_is_not_proved(tmp_path):
    """The caller is wrong about `same`; the engine does not take its word."""
    changed = TIMER.replace("assign irq_o = &count_o;", "assign irq_o = |count_o;")
    result = run(tmp_path / "a", (TIMER, WRAP), (changed, WRAP), "wrap", same=["timer"])
    assert result.status == "error", result.detail
    assert "u_t0" in result.detail or "u_t1" in result.detail
    rewritten = TIMER.replace("assign irq_o = &count_o;", "assign irq_o = (count_o == '1);")
    assert run(tmp_path / "b", (TIMER, WRAP), (rewritten, WRAP), "wrap", same=["timer"]).status != "proved"


@needs_yosys
def test_an_instance_of_another_same_module_is_not_proved(tmp_path):
    broken = RST_SYNC.replace("module rst_sync", "module rst_sync_b").replace("1'b1}", "1'b0}")
    parent = (
        "module top (\n  input  logic clk_i,\n  input  logic rst_ni,\n  output logic rst_o\n);\n"
        "  rst_sync u_sync (.clk_i(clk_i), .rst_ni(rst_ni), .rst_no(rst_o));\nendmodule\n"
    )
    swapped = parent.replace("rst_sync u_sync", "rst_sync_b u_sync")
    files = (RST_SYNC, broken)
    result = run(tmp_path / "a", (*files, parent), (*files, swapped), "top", same=["rst_sync", "rst_sync_b"])
    assert result.status in ("error", "refuted"), result.detail
    assert "u_sync" in result.detail
    assert (
        run(tmp_path / "b", (*files, parent), (*files, parent), "top", same=["rst_sync", "rst_sync_b"]).status
        == "proved"
    )


# --- V5 -----------------------------------------------------------------------------------------


@needs_yosys
def test_an_unknown_parameter_name_is_an_error(tmp_path):
    gate = RST_SYNC.replace("{q[STAGES-2:0], 1'b1}", "(STAGES > 2) ? {q[STAGES-2:0], 1'b0} : {q[STAGES-2:0], 1'b1}")
    result = run(tmp_path / "a", (RST_SYNC,), (gate,), "rst_sync", params={"STAGE": "3"})
    assert result.status == "error", result.detail
    assert "STAGE" in result.detail
    assert run(tmp_path / "b", (RST_SYNC,), (gate,), "rst_sync", params={"STAGES": "3"}).status == "refuted"
    only_gate = RST_SYNC.replace("parameter int STAGES = 2", "parameter int STAGES = 2,\n  parameter int EXTRA = 0")
    assert run(tmp_path / "c", (RST_SYNC,), (only_gate,), "rst_sync", params={"EXTRA": 1}).status == "error"


# --- V6 -----------------------------------------------------------------------------------------

ARRAY = (
    "module m (\n"
    "  input  logic       clk,\n"
    "  input  logic [7:0] d,\n"
    "  output logic [7:0] q\n"
    ");\n"
    "  logic [7:0] s [3];\n"
    "  always_ff @(posedge clk) begin\n"
    "    s[0] <= d;\n"
    "    s[1] <= s[0];\n"
    "    s[2] <= s[1];\n"
    "  end\n"
    "  assign q = s[2];\n"
    "endmodule\n"
)


@needs_yosys
def test_an_unpacked_array_is_compared(tmp_path):
    assert run(tmp_path / "a", (ARRAY,), (ARRAY,), "m").status == "proved"
    wrong = ARRAY.replace("s[2] <= s[1];", "s[2] <= s[0];")
    assert run(tmp_path / "b", (ARRAY,), (wrong,), "m").status == "refuted"


@needs_yosys
def test_an_unused_unpacked_array_does_not_stop_the_check(tmp_path):
    gold = module("  logic [3:0] spare [2];\n  assign o = a;\n")
    assert run(tmp_path / "a", (gold,), (gold,), "m").status == "proved"
    assert (
        run(tmp_path / "b", (gold,), (module("  logic [3:0] spare [2];\n  assign o = ~a;\n"),), "m").status == "refuted"
    )


# --- V7 -----------------------------------------------------------------------------------------

FLOP = "module m (input logic clk, input logic d, output logic q);\n  always_ff @(posedge clk) q <= d;\nendmodule\n"
CLOCK_MUX = (
    "module m (input logic clk, input logic d, input logic test_en_i, input logic tclk_i, output logic q);\n"
    "  logic ck;\n"
    "  assign ck = test_en_i ? tclk_i : clk;\n"
    "  always_ff @(posedge ck) q <= d;\n"
    "endmodule\n"
)


@needs_yosys
def test_a_tied_clock_mux_is_the_clock_it_selects(tmp_path):
    assert run(tmp_path / "a", (FLOP,), (CLOCK_MUX,), "m", tie={"test_en_i": 0}).status == "proved"
    assert run(tmp_path / "b", (FLOP,), (CLOCK_MUX,), "m", tie={"test_en_i": 1}).status in ("refuted", "error")
    assert run(tmp_path / "c", (FLOP,), (CLOCK_MUX,), "m").status in ("refuted", "error")


# --- V8, V10 ------------------------------------------------------------------------------------


def test_a_temporary_folder_that_cannot_be_made_is_an_error(tmp_path, monkeypatch):
    import tempfile

    def fails(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(tempfile, "mkdtemp", fails)
    monkeypatch.setattr(tempfile, "TemporaryDirectory", fails)
    if not equiv.available():
        pytest.skip("Yosys is missing: the answer is already an error")
    result = run(tmp_path, (FLOP,), (FLOP,), "m")
    assert result.status == "error"
    assert result.detail


@pytest.mark.parametrize("tie", [{"test_en_i": True}, {"test_en_i": False}, {"test_en_i": -1}, {"test_en_i": None}])
def test_a_tie_value_is_a_plain_number(tmp_path, tie):
    with pytest.raises((TypeError, ValueError)):
        run(tmp_path, (FLOP,), (CLOCK_MUX,), "m", tie=tie)


@needs_yosys
def test_two_threads_do_not_run_yosys_at_the_same_time(tmp_path):
    """Each check is timed inside the engine; one at a time, the whole takes the sum of the parts."""
    results = []

    def one(name: str) -> None:
        results.append(run(tmp_path / name, (TIMER,), (TIMER,), "timer"))

    threads = [threading.Thread(target=one, args=(f"t{i}",)) for i in range(3)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    total = time.monotonic() - started
    assert [r.status for r in results] == ["proved"] * 3
    # three checks one after the other take at least as long as their own times added up
    assert total >= 0.9 * sum(r.seconds for r in results)


def test_the_module_can_be_used_without_the_extra(tmp_path, monkeypatch):
    """V10, in this environment: hide the package from the import system."""
    import importlib.util

    real = importlib.util.find_spec

    def hidden(name, *args, **kwargs):
        return None if name.split(".")[0] == "yowasp_yosys" else real(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", hidden)
    assert equiv.available() is False
    result = run(tmp_path, (FLOP,), (FLOP,), "m")
    assert result.status == "error"
    assert "hdl-vet[equiv]" in result.detail


def test_the_smoke_check_covers_the_engine_without_the_extra():
    text = (Path(__file__).resolve().parent / "smoke_wheel.py").read_text(encoding="utf-8")
    assert "hdl_vet.equiv" in text
    assert "hdl-vet[equiv]" in text


# --- second review round ------------------------------------------------------------------------
#
# The second review found every wrong "proved" of the first round closed, and three new ones in
# the code that closed them.
#
# V11 Defines. A define whose value holds a quote character (a sized number such as 1'b1 or
#     32'd4) reaches the reader as it was given. Before, the value was written unquoted into the
#     reader's file, the reader took the quote as the start of a string, and the define, with the
#     ones after it, was lost without a word: the configuration that was asked for was never
#     compared.
# V12 The comparison of a `same` child (V3) covers its memories (depth and offset) and the
#     attributes of its signals (an initial value, signedness), not only its cells and ports.
#     Before, a child whose memory depth or whose initial value differed through a parameter was
#     proved.
# V13 A parameter that is declared in an included file can be named in `params`.
# V14 Undefined values in the gold. Where the gold is x the gate may choose (V1). When a check is
#     "proved" and an x of the gold can reach an output of the gold, the detail says so and names
#     the outputs: "the gold has undefined values on ...". The status stays "proved".
# V15 The clocks are checked before the proof is run, so that a module the engine cannot judge
#     costs no proof.
#
# More limits, stated: a module with flops on two clocks of its own (a clock-domain bridge) is
# always an "error"; a memory of 1000 words of 32 bits reaches the timeout; a parameter value
# that is a string cannot be given; the lock holds for the threads of one process, not for two
# processes.


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())
    return path


IFDEF = (
    "module m (input logic a, output logic y);\n"
    "`ifdef Y\n"
    "  assign y = {expr};\n"
    "`else\n"
    "  assign y = a;\n"
    "`endif\n"
    "endmodule\n"
)


@pytest.mark.parametrize(
    "defines",
    [{"X": "1'b0", "Y": "1'b1"}, {"Y": "1'b1"}, {"WD": "32'd4", "Y": None}, {"Y": "8'hFF"}, {"S": '"text"', "Y": "1"}],
    ids=["two-sized", "one-sized", "sized-then-plain", "hex", "string"],
)
@needs_yosys
def test_a_define_with_a_quote_in_its_value_is_not_lost(tmp_path, defines):
    gold = equiv.Source([_write(tmp_path / "g.sv", IFDEF.format(expr="a"))], defines=defines)
    gate = equiv.Source([_write(tmp_path / "t.sv", IFDEF.format(expr="~a"))], defines=defines)
    result = equiv.check(gold, gate, "m")
    assert result.status == "refuted", result.detail  # Y is defined, so the two differ
    assert equiv.check(gold, gold, "m").status == "proved"


@needs_yosys
def test_the_value_of_a_define_is_used(tmp_path):
    text = "module m (input logic [7:0] a, output logic [7:0] y);\n  assign y = a + `STEP;\nendmodule\n"
    one = equiv.Source([_write(tmp_path / "m.sv", text)], defines={"STEP": "8'd1"})
    two = equiv.Source([_write(tmp_path / "m.sv", text)], defines={"STEP": "8'd2"})
    assert equiv.check(one, one, "m").status == "proved"
    assert equiv.check(one, two, "m").status == "refuted"


RAM = (
    "module ram #(\n"
    "  parameter int DEPTH = 16\n"
    ") (\n"
    "  input  logic       clk,\n"
    "  input  logic       we,\n"
    "  input  logic [3:0] wa,\n"
    "  input  logic [3:0] ra,\n"
    "  input  logic [7:0] wd,\n"
    "  output logic [7:0] rd\n"
    ");\n"
    "  logic [7:0] mem [DEPTH];\n"
    "  always_ff @(posedge clk) if (we) mem[wa] <= wd;\n"
    "  assign rd = mem[ra];\n"
    "endmodule\n"
)
RAM_TOP = (
    "module top (\n"
    "  input  logic       clk,\n"
    "  input  logic       we,\n"
    "  input  logic [3:0] wa,\n"
    "  input  logic [3:0] ra,\n"
    "  input  logic [7:0] wd,\n"
    "  output logic [7:0] rd\n"
    ");\n"
    "  ram {params}u_r (.clk(clk), .we(we), .wa(wa), .ra(ra), .wd(wd), .rd(rd));\n"
    "endmodule\n"
)


@needs_yosys
def test_the_memory_of_a_same_child_is_compared(tmp_path):
    plain = RAM_TOP.format(params="")
    smaller = RAM_TOP.format(params="#(.DEPTH(12)) ")
    assert run(tmp_path / "a", (RAM, plain), (RAM, plain), "top", same=["ram"]).status == "proved"
    result = run(tmp_path / "b", (RAM, plain), (RAM, smaller), "top", same=["ram"])
    assert result.status != "proved", result.detail
    assert "u_r" in result.detail


INIT_CHILD = (
    "module ch #(\n"
    "  parameter bit INIT = 1'b1\n"
    ") (\n"
    "  input  logic clk,\n"
    "  output logic q\n"
    ");\n"
    "  logic r = INIT;\n"
    "  always_ff @(posedge clk) r <= r;\n"
    "  assign q = r;\n"
    "endmodule\n"
)
INIT_TOP = "module top (input logic clk, output logic q);\n  ch {params}u_c (.clk(clk), .q(q));\nendmodule\n"


@needs_yosys
def test_the_initial_value_in_a_same_child_is_compared(tmp_path):
    gold = INIT_TOP.format(params="")
    gate = INIT_TOP.format(params="#(.INIT(1'b0)) ")
    result = run(tmp_path / "a", (INIT_CHILD, gold), (INIT_CHILD, gate), "top", same=["ch"])
    assert result.status != "proved", result.detail
    assert "u_c" in result.detail
    other_text = INIT_CHILD.replace("parameter bit INIT = 1'b1", "parameter bit INIT = 1'b0")
    assert run(tmp_path / "b", (INIT_CHILD, gold), (other_text, gold), "top", same=["ch"]).status != "proved"
    assert run(tmp_path / "c", (INIT_CHILD, gold), (INIT_CHILD, gold), "top", same=["ch"]).status == "proved"


@needs_yosys
def test_a_same_child_is_still_the_same_when_other_text_changes(tmp_path):
    """No new wrong error: other modules before the child, a comment in it, the parent rewritten."""
    extra = "module unused (input logic a, output logic o);\n  assign o = a;\nendmodule\n"
    commented = TIMER.replace("  assign irq_o", "  // the interrupt\n  assign irq_o")
    rewritten = WRAP.replace("assign irq_o = |irq;", "assign irq_o = irq[0] | irq[1];")
    assert run(tmp_path / "a", (TIMER, WRAP), (extra, TIMER, WRAP), "wrap", same=["timer"]).status == "proved"
    assert run(tmp_path / "b", (TIMER, WRAP), (commented, WRAP), "wrap", same=["timer"]).status == "proved"
    assert run(tmp_path / "c", (TIMER, WRAP), (TIMER, rewritten), "wrap", same=["timer"]).status == "proved"


@needs_yosys
def test_a_parameter_from_an_included_file_can_be_named(tmp_path):
    inc = tmp_path / "inc"
    _write(inc / "params.svh", "  parameter int STEP = 1,\n")
    text = (
        "module m #(\n"
        '`include "params.svh"\n'
        "  parameter int WIDTH = 8\n"
        ") (input logic [WIDTH-1:0] a, output logic [WIDTH-1:0] y);\n"
        "  assign y = a + WIDTH'(STEP);\n"
        "endmodule\n"
    )
    gate = text.replace("a + WIDTH'(STEP)", "(STEP == 3) ? a : a + WIDTH'(STEP)")
    gold_src = equiv.Source([_write(tmp_path / "g.sv", text)], incdirs=[inc])
    gate_src = equiv.Source([_write(tmp_path / "t.sv", gate)], incdirs=[inc])
    assert equiv.check(gold_src, gate_src, "m").status == "proved"
    assert equiv.check(gold_src, gate_src, "m", params={"STEP": 3}).status == "refuted"


@needs_yosys
def test_undefined_values_of_the_gold_are_named_when_a_check_is_proved(tmp_path):
    ports = "input logic a, input logic s, output logic o"
    gold = module("  assign o = s ? a : 1'bx;\n", ports)
    gate = module("  assign o = s ? a : 1'b1;\n", ports)
    result = run(tmp_path / "a", (gold,), (gate,), "m")
    assert result.status == "proved"
    assert "the gold has undefined values" in result.detail
    assert "o" in result.detail.split("undefined values")[1]
    clean = run(tmp_path / "b", (FLOP,), (FLOP,), "m")
    assert clean.status == "proved"
    assert "undefined" not in clean.detail


@needs_yosys
def test_an_undefined_value_that_reaches_no_output_is_not_reported(tmp_path):
    ports = "input logic a, input logic s, output logic o"
    gold = module("  logic t;\n  assign t = s ? a : 1'bx;\n  assign o = s & t;\n", ports)
    result = run(tmp_path, (gold,), (gold,), "m")
    assert result.status == "proved"
    assert "undefined" not in result.detail


@needs_yosys
def test_a_module_the_engine_cannot_judge_costs_no_proof(tmp_path):
    """V15: two clocks in one module is an error; the answer comes without the proof being run."""
    bridge = (
        "module m (input logic ca, input logic cb, input logic [15:0] d, output logic [15:0] q);\n"
        "  logic [15:0] r;\n"
        "  always_ff @(posedge ca) r <= d * d;\n"
        "  always_ff @(posedge cb) q <= r * r;\n"
        "endmodule\n"
    )
    result = run(tmp_path, (bridge,), (bridge,), "m")
    assert result.status == "error"
    assert "clock" in result.detail
    assert result.seconds < 10


# --- third review round -------------------------------------------------------------------------
#
# The third review re-ran about 230 earlier checks: no answer had moved in the wrong direction,
# and the three wrong "proved" of the second round were closed. It found one more, of the same
# family as V11, and an exception where the rules ask for an answer.
#
# V16 The name of a define. The reader's file expands `$NAME` as a variable of the environment,
#     so the define `A$B` reached the reader as `A`, and a design that tests `A$B` was compared
#     in another configuration: a wrong "proved". A define name, a define value, a file or an
#     include folder that holds a character the reader's file cannot carry (`$`, a backslash in
#     a define, a line break) is an "error" that names it. Nothing is passed on changed.
# V17 A define value is text. Every other character is passed on as it is: brackets, blanks,
#     operators, braces. A value the engine cannot pass on is an "error" answer, never an
#     exception: the defines come from the user's file list, and `check` raises only for an
#     argument of a wrong type (E7).
# V18 The report of undefined values (V14) has a time budget of its own. When it runs out, the
#     answer stays "proved" and the detail says that the report could not be made.
#
# Limit, stated: the report of V14 does not see an undefined value of the gold that cancels
# (`u ^ u`) or one that a division by zero makes.

DEFINE_GOLD = (
    "module m (input logic a, output logic y);\n"
    "`ifdef A$B\n"
    "  assign y = ~a;\n"
    "`else\n"
    "  assign y = a;\n"
    "`endif\n"
    "endmodule\n"
)
DEFINE_GATE = "module m (input logic a, output logic y);\n  assign y = a;\nendmodule\n"


@needs_yosys
def test_a_define_name_with_a_dollar_is_not_proved(tmp_path):
    defines = {"A$B": None}
    gold = equiv.Source([_write(tmp_path / "g.sv", DEFINE_GOLD)], defines=defines)
    gate = equiv.Source([_write(tmp_path / "t.sv", DEFINE_GATE)], defines=defines)
    result = equiv.check(gold, gate, "m")
    assert result.status == "error", result.detail
    assert "A$B" in result.detail


VALUE = "module m (output logic [7:0] y);\n  assign y = 8'(`V);\nendmodule\n"


@pytest.mark.parametrize(
    "one, two, equal",
    [
        ("(1+2)", "(1+2)", True),
        ("(1+2)", "3", True),
        ("1 + 2", "2*3", False),
        ("{2{1'b1}}", "2'b11", True),
        ("1<<3", "8", True),
        ("(4 > 3) ? 8'd7 : 8'd9", "7", True),
        ("8'hFF", "8'hFE", False),
    ],
)
@needs_yosys
def test_a_define_value_is_passed_on_as_it_is(tmp_path, one, two, equal):
    path = _write(tmp_path / "m.sv", VALUE)
    result = equiv.check(equiv.Source([path], defines={"V": one}), equiv.Source([path], defines={"V": two}), "m")
    assert result.status == ("proved" if equal else "refuted"), result.detail


@pytest.mark.parametrize(
    "value",
    ["$HOME", "a$b", "a\\b", "1\n2", "x\\"],
    ids=["variable", "dollar", "backslash", "line-break", "ends-in-backslash"],
)
def test_a_define_value_that_cannot_be_passed_on_is_an_error_answer(tmp_path, value):
    path = _write(tmp_path / "m.sv", VALUE)
    source = equiv.Source([path], defines={"V": value})
    if not equiv.available():
        pytest.skip("Yosys is missing: the answer is already an error")
    result = equiv.check(source, source, "m")  # no exception
    assert result.status == "error", result.detail
    assert "V" in result.detail


@pytest.mark.parametrize("defines", [{"V": 3}, {"V": b"3"}, {3: "x"}, ["V"]], ids=["int", "bytes", "int-name", "list"])
def test_a_define_of_a_wrong_type_raises(tmp_path, defines):
    path = _write(tmp_path / "m.sv", VALUE)
    with pytest.raises((TypeError, ValueError)):
        equiv.check(equiv.Source([path], defines=defines), equiv.Source([path], defines=defines), "m")
