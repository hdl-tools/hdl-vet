"""`override_net` and its sign-off: tests from the adversarial review.

The base specification is the docstring of tests/ops/test_override.py. The review
(about 80 cases on small designs and eight on a copy of demo_ss) found no ok
sign-off and no "proved" for a module that differs with the enable at 0. These
rules close what it did find.

P1  One override for a net. A net that already has an override (its driver is a
    mux `assign net = PORT ? ... : ...;` on an input port that the input design
    does not have) cannot get a second one: the operation stops with a
    DriftError unless the call is that override again (O4). Before, a second
    override with another `keep` replaced the record of the first, so the waiver
    of the second covered both.
P2  The value has the width of the net. A value whose own width differs from the
    net's is refused when called, with "width" in the message; the fill literals
    ('0, '1, 'x, 'z) take the width of the net and are accepted. Before, a
    narrow signed value was written into the mux, where it is extended with
    zeros, not with its sign as in `assign net = value;`.
P3  The value is one expression. A value that cannot be read as one expression
    by itself (a comma at the top level, an unbalanced bracket, a second
    statement) is refused when called.
P4  An inout pin is not an output pin: a net that an inout pin drives is refused.
    A net that is an unpacked array is refused, and the message says "unpacked".
P5  The enable is new. An input port that the module of the input design has
    already is refused as the enable when called: with it tied to 0 the module
    is not "as before". (Before: an "error" record at the sign-off.)
P6  A waiver is one line. A waiver with a line break is refused. The report
    shows the waiver as a quoted string in which a quote is escaped, as JSON
    does, so that a line of the report is always one line.
P7  The waiver of the latest call counts. A call that is skipped (the override
    is there) sets the waiver of that override to what the call gives: a waiver
    added later counts, and a call without one takes it away.
P8  `same` follows the parameter values. A child instance whose parameter values
    differ between the input design and the edited design is not named in
    `same`, although its text is unchanged; the comparison then shows the change
    of behaviour ("refuted"), not an "error".
P9  The detail of a record names files by their path in the design, never by a
    path of the temporary folder of the proof. The manifest is the same on every
    run.
P10 (The engine, src/hdl_vet/equiv.py.) The comparison of a child named in
    `same` does not depend on the numbers the reader gives to the bits of a
    netlist: two identical children compare equal. On demo_ss, two of three
    parameter sets of `bag_axi_dw` were answered "the child i_dw is named in
    same but is not the same", although nothing below had changed.

FROM THE SECOND REVIEW (of the fixes; it could not get two different children
taken for the same)

P11 The value does not read the net: a value that names the net is refused, with
    "loop" in the message. (It was written and "proved": a combinational loop
    when the enable is 1.) A loop through other nets is a stated limit.
P12 P1 is decided by what the run knows, not by the look of the text. A net that
    has an override of this run cannot get another one (DriftError), also after
    an edit of the line of the first. A mux that the script or the input design
    wrote is not an override: the net can be overridden.
P13 `same` is empty when text outside the module bodies changed (a package, the
    compilation unit): a child that reads a changed package constant is
    compared, and the answer is "refuted", not an "error".
P14 A value with an escaped name is accepted. A value with a line break is
    refused.
P15 (The engine.) The detail of a result never names the engine's own work
    folder.

LIMITS, STATED (in the manual too)

- A loop through other nets: a value that reads a net which the overridden net
  drives is not refused, and the comparison (enable at 0) does not see it.
- The comparison of a child named in `same` can answer "not the same" for two
  identical children that have symmetric logic without names in another order:
  an "error", never a wrong "proved".

- Overrides at two levels. A module that has an override and a child that has
  one too is compared with that child flattened. If the two then have flops on
  more than one clock, the engine answers "error" and the upper override needs a
  waiver. (On demo_ss: the bypass of the reset synchronisers in the top together
  with an override in `aon_wrapper`.)
- A waiver covers the comparison of the module: with a waiver on every logic
  operation of a module, a raw edit of logic in the same module is not judged.
- A large module needs memory: the comparison of the top of demo_ss took about
  2.3 GB in the Yosys process.
- A driver with a delay (`assign #1 n = ...`) makes the comparison an "error".
- A parameter with a string value makes the comparison an "error"; a type
  parameter is compared at its default only.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from hdl_vet import equiv
from hdl_vet.cli import main
from hdl_vet.ops import DriftError, VetError
from override_design import CORE, FILES, TOP, Design, needs_yosys, records, statuses, with_enable

pytestmark = [pytest.mark.equiv]

WIDE = (
    "module wide (\n"
    "  input  logic              clk_i,\n"
    "  input  logic        [7:0] a,\n"
    "  input  logic signed [3:0] s,\n"
    "  inout  wire               pad,\n"
    "  output logic        [7:0] n,\n"
    "  output logic        [7:0] m,\n"
    "  output logic        [3:0] k\n"
    ");\n"
    "  logic [7:0] arr [2];\n"
    "  wire        io_w;\n"
    "\n"
    "  iocell u_io (.io(io_w), .pad(pad));\n"
    "  assign n      = a;\n"
    "  assign m      = a + 8'd1;\n"
    "  assign k      = a[3:0];\n"
    "  assign arr[0] = a;\n"
    "  assign arr[1] = a;\n"
    "endmodule\n"
)
IOCELL = "module iocell (\n  inout wire io,\n  inout wire pad\n);\n  tran t (io, pad);\nendmodule\n"
WIDE_TOP = (
    "module top (\n"
    "  input  logic              clk_i,\n"
    "  input  logic        [7:0] a,\n"
    "  input  logic signed [3:0] s,\n"
    "  inout  wire               pad,\n"
    "  output logic        [7:0] n,\n"
    "  output logic        [7:0] m,\n"
    "  output logic        [3:0] k\n"
    ");\n"
    "  wide u_w (.clk_i(clk_i), .a(a), .s(s), .pad(pad), .n(n), .m(m), .k(k));\n"
    "endmodule\n"
)


@pytest.fixture
def design(tmp_path: Path) -> Design:
    return Design(tmp_path / "design")


@pytest.fixture
def wide(tmp_path: Path) -> Design:
    return Design(tmp_path / "wide", {"iocell": IOCELL, "wide": WIDE, "top": WIDE_TOP})


def wide_module(design: Design, monkeypatch, yosys: bool = False):
    if not yosys:
        monkeypatch.setattr(equiv, "available", lambda: False)
    vet = design.vet()
    vet.punch_port("test_en_i", "input", to="top.u_w")
    return vet, vet.module("wide")


# --- P1 -------------------------------------------------------------------------------------------


def test_a_net_gets_one_override(wide, monkeypatch):
    vet, mod = wide_module(wide, monkeypatch)
    mod.add_port("other_en_i", "input")
    mod.override_net("n", "8'hAA", enable="test_en_i", keep="n_k1", waiver="first")
    once = wide.text(vet, "wide")
    for call in (
        {"value": "8'h55", "enable": "other_en_i", "keep": "n_k2", "waiver": "second"},
        {"value": "8'h55", "enable": "other_en_i", "waiver": "second"},
        {"value": "8'hAA", "enable": "test_en_i", "keep": "n_k2"},
    ):
        with pytest.raises(DriftError, match="override_net wide"):
            mod.override_net("n", call.pop("value"), **call)
    assert wide.text(vet, "wide") == once
    assert [s for s in statuses(vet) if s[0] == "override_net"] == [("override_net", "wide.n", "applied")]
    result = vet.finish()
    assert [r.operations for r in result.equivalence] == [["override_net wide.n"]]


# --- P2, P3, P4 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "net, value",
    [("n", "s"), ("n", "4'd3"), ("n", "a[3:0]"), ("n", "1'b0"), ("k", "a"), ("n", "{a, a}"), ("n", "16'd1")],
)
def test_a_value_of_another_width_is_refused(wide, monkeypatch, net, value):
    vet, mod = wide_module(wide, monkeypatch)
    before = statuses(vet)
    with pytest.raises(VetError, match="width") as e:
        mod.override_net(net, value, enable="test_en_i", waiver="w")
    assert str(e.value).startswith("override_net wide: ")
    assert statuses(vet) == before
    assert "_pre" not in wide.text(vet, "wide")


@pytest.mark.parametrize(
    "net, value",
    [("n", "8'hAA"), ("n", "'0"), ("n", "'1"), ("n", "a"), ("n", "8'(s)"), ("n", "{4'd0, s}"), ("k", "s"), ("n", "m")],
)
def test_a_value_of_the_width_of_the_net_is_accepted(wide, monkeypatch, net, value):
    vet, mod = wide_module(wide, monkeypatch)
    mod.override_net(net, value, enable="test_en_i", waiver="w")
    assert f"assign {net} = test_en_i ? {value} : {net}_pre;" in wide.text(vet, "wide")
    assert vet.finish().ok


@needs_yosys
def test_a_signed_value_at_full_width_is_proved(tmp_path: Path, monkeypatch):
    """Without the pad cell: the reader of the engine cannot take its `tran` primitive."""
    no_pad = WIDE.replace("  inout  wire               pad,\n", "").replace("  wire        io_w;\n", "")
    no_pad = no_pad.replace("  iocell u_io (.io(io_w), .pad(pad));\n", "")
    top = WIDE_TOP.replace("  inout  wire               pad,\n", "").replace(".pad(pad), ", "")
    assert "pad" not in no_pad + top
    design = Design(tmp_path / "wide", {"wide": no_pad, "top": top})
    vet, mod = wide_module(design, monkeypatch, yosys=True)
    mod.override_net("n", "8'(s)", enable="test_en_i")
    result = vet.finish()
    assert result.ok, result.report()
    assert records(result) == [("wide", "proved")]


@pytest.mark.parametrize(
    "value",
    ["a : n_pre, k = a[3:0] ? 4'd0", "a, a", "a)", "(a", "a; assign k = 4'd0", "a // note", "a /* note */ + 8'd1 :"],
)
def test_a_value_that_is_not_one_expression_is_refused(wide, monkeypatch, value):
    vet, mod = wide_module(wide, monkeypatch)
    with pytest.raises(VetError, match="value") as e:
        mod.override_net("n", value, enable="test_en_i", waiver="w")
    assert str(e.value).startswith("override_net wide: ")
    assert "_pre" not in wide.text(vet, "wide")


def test_an_inout_pin_and_an_unpacked_array_are_refused(wide, monkeypatch):
    vet, mod = wide_module(wide, monkeypatch)
    with pytest.raises(VetError, match="inout"):
        mod.override_net("io_w", "1'b0", enable="test_en_i", waiver="w")
    with pytest.raises(VetError, match="unpacked"):
        mod.override_net("arr", "'0", enable="test_en_i", waiver="w")
    assert "_pre" not in wide.text(vet, "wide")
    assert vet.finish().ok


# --- P5 -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("enable", ["clk_i", "rst_ni"])
def test_an_enable_that_the_input_design_has_is_refused(design, monkeypatch, enable):
    monkeypatch.setattr(equiv, "available", lambda: False)
    vet = design.vet()
    core = with_enable(vet)
    with pytest.raises(VetError, match="enable") as e:
        core.override_net("rst_sync_n", "rst_ni", enable=enable, waiver="w")
    assert str(e.value).startswith("override_net core: ")
    assert not isinstance(e.value, DriftError)
    assert enable in str(e.value)
    assert "_pre" not in design.text(vet, "core")


def test_an_enable_added_by_the_run_is_new(design, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    vet = design.vet()
    vet.module("core").add_port("scan_en_i", "input")
    vet.module("top").instance("u_core").connect("scan_en_i", "1'b0")
    vet.module("core").override_net("rst_sync_n", "rst_ni", enable="scan_en_i", waiver="w")
    assert vet.finish().ok


# --- P6, P7 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("waiver", ["first\nsecond", "first\r\nsecond", "ends with a break\n"])
def test_a_waiver_is_one_line(design, monkeypatch, waiver):
    monkeypatch.setattr(equiv, "available", lambda: False)
    vet = design.vet()
    core = with_enable(vet)
    with pytest.raises(VetError, match="waiver"):
        core.override_net("rst_sync_n", "rst_ni", enable="test_en_i", waiver=waiver)
    assert "_pre" not in design.text(vet, "core")


def test_a_waiver_with_quotes_is_one_report_line(design, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    vet = design.vet()
    core = with_enable(vet)
    waiver = 'say "no" to reset in test mode (see TB-12)'
    core.override_net("rst_sync_n", "rst_ni", enable="test_en_i", waiver=waiver)
    result = vet.finish()
    lines = [line for line in result.report().splitlines() if "waived:" in line]
    assert len(lines) == 1
    assert f"waived: override_net core.rst_sync_n: {json.dumps(waiver)}" in lines[0]
    assert result.equivalence[0].waiver == waiver


@needs_yosys
def test_a_waiver_given_on_a_later_call_counts(design):
    vet = design.vet()
    core = with_enable(vet)
    core.override_net("rst_sync_n", "rst_ni", enable="test_en_i")
    design.replace(vet, "core", b"test_en_i ? rst_ni : rst_sync_n_pre", b"test_en_i ? rst_sync_n_pre : rst_ni")
    assert not vet.check().ok
    design.replace(vet, "core", b"test_en_i ? rst_sync_n_pre : rst_ni", b"test_en_i ? rst_ni : rst_sync_n_pre")
    core.override_net("rst_sync_n", "rst_ni", enable="test_en_i", waiver="added on the second run")
    assert statuses(vet)[-1] == ("override_net", "core.rst_sync_n", "skipped")
    result = vet.check()
    assert result.ok
    assert (result.equivalence[0].status, result.equivalence[0].waiver) == ("waived", "added on the second run")
    core.override_net("rst_sync_n", "rst_ni", enable="test_en_i")
    result = vet.finish()
    assert (result.equivalence[0].status, result.equivalence[0].waiver) == ("proved", None)


# --- P8 -------------------------------------------------------------------------------------------


@needs_yosys
def test_a_child_with_another_parameter_value_is_compared_not_trusted(design):
    """`u_slow` gets 4 stages: its text is unchanged, but it is not the same child any more."""
    vet = design.vet()
    core = with_enable(vet)
    core.override_net("irq_o", "1'b0", enable="test_en_i")
    core.instance("u_slow").set_param("STAGES", 4, was=3)
    result = vet.finish()
    (record,) = result.equivalence
    assert (record.module, record.status) == ("core", "refuted"), record.detail
    assert "named in same" not in record.detail
    assert not result.ok


# --- P9 -------------------------------------------------------------------------------------------


@needs_yosys
def test_the_detail_names_no_temporary_path(tmp_path: Path, capsys, monkeypatch):
    """A driver with a delay: the reader of the engine refuses it and names the file."""
    files = dict(FILES)
    files["core"] = CORE.replace("  assign rst_o    = rst_sync_n;\n", "  assign #1 rst_o = rst_sync_n;\n")
    assert files["core"] != CORE
    design = Design(tmp_path / "design", files)
    vet = design.vet()
    core = with_enable(vet)
    core.override_net("irq_o", "1'b0", enable="test_en_i")
    first = vet.check()
    second = vet.finish()
    (record,) = second.equivalence
    assert record.status == "error"
    assert first.equivalence[0].detail == record.detail
    assert "hdl_vet_proof" not in record.detail
    assert Path(tempfile.gettempdir()).as_posix().lower() not in record.detail.replace(chr(92), "/").lower()
    assert "rtl/core.sv" in record.detail
    assert TOP  # the top of the shared design is used by the other tests of this file


def run(*args: object) -> int:
    return main([str(a) for a in args])


@needs_yosys
def test_the_manifest_is_the_same_on_every_run(design, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "dft.py").write_text(
        "def apply(vet):\n"
        '    vet.punch_port("test_en_i", "input", to="top.u_core")\n'
        '    vet.module("core").override_net("rst_sync_n", "rst_ni", enable="test_en_i")\n',
        encoding="utf-8",
    )
    manifests = []
    for name in ("one", "two"):
        out = tmp_path / name
        assert run("apply", "--no-project", "-f", design.filelist, "--top", "top", "-s", "dft.py", "-o", out) == 0
        manifests.append((out / "hdl-vet.json").read_bytes())
    capsys.readouterr()
    assert manifests[0] == manifests[1]


# --- second review: P11 to P15 --------------------------------------------------------------------


@pytest.mark.parametrize("value", ["count_o + 8'd1", "count_o", "~count_o", "{count_o[6:0], 1'b0}"])
def test_a_value_that_reads_the_net_is_refused(design, monkeypatch, value):
    monkeypatch.setattr(equiv, "available", lambda: False)
    vet = design.vet()
    core = with_enable(vet)
    with pytest.raises(VetError, match="loop") as e:
        core.override_net("count_o", value, enable="test_en_i", waiver="w")
    assert str(e.value).startswith("override_net core: ")
    assert "_pre" not in design.text(vet, "core")


def test_a_mux_that_the_script_wrote_is_not_an_override(design, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    vet = design.vet()
    core = with_enable(vet)
    vet.punch_port("mode_i", "input", to="top.u_core")
    core.add_wire("sel_w")
    core.add_assign("sel_w", "mode_i ? rst_ni : en_i[0]")
    core.override_net("sel_w", "1'b0", enable="test_en_i", waiver="w")
    text = design.text(vet, "core")
    assert "assign sel_w_pre = mode_i ? rst_ni : en_i[0];" in text
    assert "assign sel_w = test_en_i ? 1'b0 : sel_w_pre;" in text
    assert vet.finish().ok


def test_a_second_override_after_an_edit_of_the_first_is_drift(design, monkeypatch):
    """The line of the first override is edited by hand: the run still knows that the net has one."""
    monkeypatch.setattr(equiv, "available", lambda: False)
    vet = design.vet()
    core = with_enable(vet)
    vet.punch_port("other_en_i", "input", to="top.u_core")
    core.override_net("rst_o", "1'b0", enable="test_en_i", waiver="first")
    design.replace(vet, "core", b"test_en_i ? 1'b0 : rst_o_pre", b"(test_en_i) ? 1'b0 : rst_o_pre")
    with pytest.raises(DriftError, match="override_net core"):
        core.override_net("rst_o", "1'b1", enable="other_en_i", keep="rst_o_k2", waiver="second")
    assert "rst_o_k2" not in design.text(vet, "core")
    result = vet.finish()
    assert [(r.operations, r.waiver) for r in result.equivalence] == [(["override_net core.rst_o"], "first")]


PKG = "package p;\n  localparam int INC = 1;\nendpackage\n"
TICK = (
    "module tick (\n"
    "  input  logic       clk_i,\n"
    "  input  logic [7:0] a,\n"
    "  output logic [7:0] q\n"
    ");\n"
    "  always_ff @(posedge clk_i) q <= a + 8'(p::INC);\n"
    "endmodule\n"
)
PKG_TOP = (
    "module top (\n"
    "  input  logic       clk_i,\n"
    "  input  logic [7:0] a,\n"
    "  output logic [7:0] q,\n"
    "  output logic [7:0] r\n"
    ");\n"
    "  tick u_t (.clk_i(clk_i), .a(a), .q(q));\n"
    "  assign r = a;\n"
    "endmodule\n"
)


@needs_yosys
def test_a_change_in_a_package_is_compared_not_trusted(tmp_path: Path):
    """`tick` reads `p::INC`: its text is unchanged, but it is not the same child any more."""
    design = Design(tmp_path / "pkg", {"p": PKG, "tick": TICK, "top": PKG_TOP})
    vet = design.vet()
    top = vet.module("top")
    top.add_port("test_en_i", "input")
    top.override_net("r", "8'h00", enable="test_en_i")
    design.replace(vet, "p", b"INC = 1", b"INC = 2")
    result = vet.finish()
    (record,) = result.equivalence
    assert (record.module, record.status) == ("top", "refuted"), record.detail
    assert not result.ok


def test_a_value_with_an_escaped_name_is_accepted(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    top = PKG_TOP.replace("logic [7:0] a,", "logic [7:0] \\a.b ,").replace(".a(a)", ".a(\\a.b )")
    top = top.replace("assign r = a;", "assign r = \\a.b ;")
    design = Design(tmp_path / "esc", {"p": PKG, "tick": TICK, "top": top})
    vet = design.vet()
    mod = vet.module("top")
    mod.add_port("test_en_i", "input")
    mod.override_net("q", "\\a.b ", enable="test_en_i", waiver="w")
    assert vet.finish().ok


@pytest.mark.parametrize("value", ["a\n| a", "a\r| a", "a\n"])
def test_a_value_with_a_line_break_is_refused(wide, monkeypatch, value):
    vet, mod = wide_module(wide, monkeypatch)
    with pytest.raises(VetError, match="value"):
        mod.override_net("n", value, enable="test_en_i", waiver="w")
    assert "_pre" not in wide.text(vet, "wide")


@needs_yosys
def test_the_detail_names_no_work_folder_of_the_engine(tmp_path: Path, monkeypatch):
    """The Yosys process ends with an error line that names a file of the engine's own folder."""
    import subprocess

    seen: list[Path] = []

    def spawn(work, args, env, timeout):
        seen.append(Path(work))
        line = f"{Path(work).as_posix()}/run.ys:3: ERROR: something went wrong"
        return subprocess.CompletedProcess(list(args), 1, stdout="", stderr=line + "\n")

    monkeypatch.setattr(equiv, "_spawn", spawn)
    design = Design(tmp_path / "pkg", {"p": PKG, "tick": TICK, "top": PKG_TOP})
    source = equiv.Source(files=[design.path(n) for n in ("p", "tick", "top")])
    result = equiv.check(source, source, "top")
    assert seen
    assert result.status == "error"
    flat = result.detail.replace(chr(92), "/").lower()
    for work in seen:
        assert work.as_posix().lower() not in flat
        assert work.name.lower() not in flat
