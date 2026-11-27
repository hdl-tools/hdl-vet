"""Phase 6: `insert_cell_on_net`, a cell between the driver of a net and its readers (#41).

The second logic pattern after `override_net`. It puts an instance of a cell on a net: a buffer
with a bypass, an isolation cell, a scan cell. The cell goes at the driver, so every reader of the
net is behind it.

    def apply(vet):
        vet.punch_port("test_en_i", "input", to="top.u_core")
        core = vet.module("core")
        core.insert_cell_on_net(
            "rst_sync_n", "iso_cell", name="u_iso",
            data_in="d_i", data_out="q_o", enable_pin="iso_i", enable="test_en_i",
        )

    // before                                   // after
    logic rst_sync_n;                           logic rst_sync_n;
    rst_sync u_sync (.rst_no(rst_sync_n), ...); logic rst_sync_n_pre;
                                                rst_sync u_sync (.rst_no(rst_sync_n_pre), ...);
                                                iso_cell u_iso (.d_i(rst_sync_n_pre),
                                                                .iso_i(test_en_i), .q_o(rst_sync_n));

Owner decisions

2026-11-24
- The cell must have an enable or bypass pin. The proof ties it off and compares the module with
  the input, as `override_net` does. A cell without such a pin is refused unless waived.
- The cell goes at the driver: the old driver feeds the cell, the cell drives the net. The driver
  rules of `override_net` apply.
- The script names the data input, the data output and the enable pin of the cell. Nothing is
  guessed. A missing or wrong pin is refused.

2026-11-27
- Other pins of the cell: a `connect=` mapping, as `replace_module` has it. Every input pin of
  the cell that is not the data input or the enable must be in the mapping. An output other
  than the data output is left open by naming it in `unconnected=`.
- The enable: as `override_net`. `enable=` names a one-bit input of the module that the input
  design does not have. The way of the enable to the top is checked as for an override.

THE OPERATION

I1  `Module.insert_cell_on_net(net, cell, *, name, data_in, data_out, enable_pin, enable,
    connect=None, unconnected=(), params=None, keep=None, waiver=None)`.
    `net` is as for `override_net`: a signal of the module whose whole width has exactly one
    driver, an `assign` or the output pin of one instance. `cell` is a module of the design.
    `name` is the name of the new instance. `data_in`, `data_out` and `enable_pin` are ports of
    the cell. `enable` is a one-bit input port of the module; the script adds it before.
    `params` gives parameter overrides of the instance, name to value. `keep` names the signal
    that carries what the driver gives; the default is `<net>_pre`.
I2  What it writes. A declaration of `keep` with the type and width of `net`. The driver now
    drives `keep`. One instance of `cell`, named `name`, after the driver: `data_in` is
    connected to `keep`, `data_out` to `net`, `enable_pin` to `enable`, each pin of `connect`
    to its expression, and each pin of `unconnected` to nothing (`.pin()`). Named connections
    only. Nothing else changes.
I3  Refused when called, with a `VetError` that starts with "insert_cell_on_net <module>: " and
    with nothing queued:
    - what `override_net` refuses for the net, the enable and the keep name;
    - `cell` is no module of the design, or is the module itself;
    - `name` is no simple identifier, or the module has that name already;
    - `data_in`, `data_out` or `enable_pin` is no port of the cell, or two of them are the same
      pin, or `data_in` is no input, `data_out` is no output, `enable_pin` is no input of one bit;
    - the width of `data_in` or of `data_out` is not the width of the net;
    - `connect` names a pin that the cell does not have, an output, or one of the three pins;
    - an input pin of the cell is in none of `data_in`, `enable_pin` and `connect`;
    - an output pin of the cell is neither `data_out` nor in `unconnected`; `unconnected` names
      a pin that is no output, or `data_out`;
    - `params` names a parameter that the cell does not have;
    - Yosys is missing and there is no waiver (the message names `hdl-vet[equiv]`).
    The arguments are checked as rule A1 to A11 of tests/ops/test_argument_checks.py say.
I4  A second run is skipped: the instance is there with that cell and those connections. If an
    instance of that name is there with another cell or other connections, the call stops
    with a `DriftError`.
I5  It is an operation like the others: recorded in `vet.log` as ("insert_cell_on_net",
    "<module>.<net>", applied or skipped), allowed in a group and in a scope, kept in a saved
    session, and refused with nothing changed when it is refused inside a group.

THE SIGN-OFF

I6  The call is a logic operation with the label "insert_cell_on_net <module>.<net>". The module
    is compared as for `override_net`: the enable is tied to 0, and the edited module must do
    what the module of the input design does. So a cell that lets its data through when its
    enable pin is 0 is "proved", and any other cell is "refuted" or "error". The line of the
    report is the one of an override:
      "  equivalence proved: core (insert_cell_on_net core.rst_sync_n; equal when test_en_i is 0)"
I7  The structure comparison expects the new instance (`instance-added`) at each path of the
    module. The way of the enable to the top is in `Result.enables`, as for an override.
I8  A waiver is as for every logic operation: the check still runs when Yosys is there, its
    answer is reported, and only the waiver decides.
I9  A check of the operation fails the sign-off when the instance, one of its connections or
    the declaration of `keep` is not in the final design as the call wrote it.

WHAT THE REVIEW FOUND

I10 An expression of `connect=` must not read the net, the output of the cell. That is a loop
    through the cell (`.x_i(rst_sync_n), .q_o(rst_sync_n)`), and the engine says "proved" for a
    loop that can hold more than one value. The call is refused, as `override_net` refuses a
    value that reads its net, also when the net is read through other nets of the module.
    An expression that the tool cannot follow to the nets it reads (a call of a function,
    a hierarchical name) is refused too, as `override_net` refuses such a value: a function
    with no argument that returns the net is the same loop (found by the second review).
I11 The parameters are part of what the call wrote. A second call with other `params=` is a
    `DriftError`, and the check of I9 fails when a parameter override of the instance is not
    as the call wrote it. A value of `params=` is one expression with no comment, as an
    expression of `connect=` is.
I12 One logic pattern on a net in a run. `insert_cell_on_net` and `override_net` refuse a net
    that a cell or an override of this run already drives. The message names the earlier
    operation. Before this rule the second call was taken and the sign-off then failed on
    the check of the first.
I13 The declaration of `keep` is checked with its type, as the declaration of `add_wire` is.
I14 A `name` that another instance of the module has is refused with a plain `VetError`. A
    `DriftError` is only for an instance of that name that this operation could have written.
I15 Every refusal of the operation starts with "insert_cell_on_net <module>: ", also one that
    comes from code it shares with `override_net`.

STATED LIMITS

- The engine itself says "proved" for a combinational loop that can hold more than one value.
  I10 keeps the operation from writing one. A raw splice that writes one is not caught.
- A value of `params=` that is a type (for a type parameter) is refused, because it is no
  expression.
- An expression of `connect=` that names a constant of a package (`pk::K`, or `K` from a
  wildcard import) is refused as one that cannot be followed, as `override_net` refuses it.
  Give it through a `localparam` of the module.
- An operation on the new instance (`connect`, `disconnect`, `set_param`, `replace_module`) is
  refused for the rest of the run: the instance is not known under the top.

- The cell is off when its enable pin is 0. A cell whose bypass is active at 1 is "refuted".
  Wrap it in a module, or give a waiver.
- A cell that the engine cannot judge (a clock gate with a latch) is "error" and needs a waiver.
- The cell goes at the driver. A cell in front of one reader only is not this operation.
- A cell with an interface port, a non-ANSI port list or a port list from a macro is refused.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hdl_vet import equiv
from hdl_vet.ops import DriftError, Vet, VetError
from override_design import FILES, Design, needs_yosys, records, statuses, with_enable

ISO = (
    "module iso_cell (input logic d_i, input logic iso_i, output logic q_o);\n"
    "  assign q_o = iso_i ? 1'b0 : d_i;\n"
    "endmodule\n"
)
# Not off at 0: it inverts.
INV = (
    "module inv_cell (input logic d_i, input logic en_i, output logic q_o);\n"
    "  assign q_o = en_i ? 1'b0 : ~d_i;\n"
    "endmodule\n"
)
SCAN = (
    "module scan_cell (input logic d_i, input logic se_i, input logic si_i, output logic q_o, output logic so_o);\n"
    "  assign q_o = se_i ? si_i : d_i;\n"
    "  assign so_o = q_o;\n"
    "endmodule\n"
)
BUS = (
    "module iso_bus #(parameter int W = 1) (input logic [W-1:0] d_i, input logic iso_i, output logic [W-1:0] q_o);\n"
    "  assign q_o = iso_i ? '0 : d_i;\n"
    "endmodule\n"
)
CELLS = {"iso_cell": ISO, "inv_cell": INV, "scan_cell": SCAN, "iso_bus": BUS}
WHY = "the cell is checked in its own bench"


@pytest.fixture
def design(tmp_path: Path) -> Design:
    return Design(tmp_path / "design", {**CELLS, **FILES})


@pytest.fixture
def vet(design: Design) -> Vet:
    return design.vet()


def iso(core, net: str = "rst_sync_n", **more: object) -> None:
    given: dict[str, object] = {
        "name": "u_iso",
        "data_in": "d_i",
        "data_out": "q_o",
        "enable_pin": "iso_i",
        "enable": "test_en_i",
    }
    given.update(more)
    core.insert_cell_on_net(net, given.pop("cell", "iso_cell"), **given)


def squeezed(text: str) -> str:
    return re.sub(r"\s+", "", text)


def test_the_design_is_what_the_tests_need(vet):
    assert vet.design.diagnostics() == []


# --- I1, I2: what it writes -------------------------------------------------------------------------


@needs_yosys
def test_a_cell_on_a_net_that_an_instance_drives(vet, design):
    core = with_enable(vet)
    iso(core)
    text = design.text(vet, "core")
    flat = squeezed(text)
    assert "  logic       rst_sync_n;\n" in text
    assert "logic rst_sync_n_pre;" in text
    assert ".rst_no(rst_sync_n_pre));" in flat
    assert "iso_cellu_iso(" in flat
    for connection in (".d_i(rst_sync_n_pre)", ".iso_i(test_en_i)", ".q_o(rst_sync_n)"):
        assert connection in flat, connection
    assert ".rst_ni(rst_sync_n)," in flat  # the readers of the net are untouched
    assert "assign rst_o    = rst_sync_n;" in text
    assert statuses(vet)[-1] == ("insert_cell_on_net", "core.rst_sync_n", "applied")
    result = vet.finish()
    assert result.ok, result.report()
    assert records(result) == [("core", "proved")]


@needs_yosys
def test_a_cell_on_a_net_that_an_assign_drives(vet, design):
    core = with_enable(vet)
    iso(core, "irq_o")
    flat = squeezed(design.text(vet, "core"))
    assert "logicirq_o_pre;" in flat
    assert "assignirq_o_pre=irq_raw&en_i[1]&seen&(split==en_i);" in flat
    assert ".d_i(irq_o_pre)" in flat
    assert ".q_o(irq_o)" in flat
    result = vet.finish()
    assert result.ok, result.report()
    assert records(result) == [("core", "proved")]


@needs_yosys
def test_a_cell_with_a_parameter_on_a_bus(vet, design):
    core = with_enable(vet)
    iso(core, "cnt", cell="iso_bus", name="u_bus", params={"W": 8}, keep="cnt_raw")
    text = design.text(vet, "core")
    flat = squeezed(text)
    assert "logic [7:0] cnt_raw;" in text
    assert ".count_o(cnt_raw)" in flat
    assert "iso_bus#(.W(8))u_bus(" in flat
    assert ".d_i(cnt_raw)" in flat
    assert ".q_o(cnt)" in flat
    result = vet.finish()
    assert result.ok, result.report()
    assert records(result) == [("core", "proved")]


@needs_yosys
def test_the_other_pins_of_the_cell(vet, design):
    core = with_enable(vet)
    core.insert_cell_on_net(
        "rst_sync_n",
        "scan_cell",
        name="u_scan",
        data_in="d_i",
        data_out="q_o",
        enable_pin="se_i",
        enable="test_en_i",
        connect={"si_i": "en_i[0]"},
        unconnected=["so_o"],
    )
    flat = squeezed(design.text(vet, "core"))
    for connection in (".d_i(rst_sync_n_pre)", ".se_i(test_en_i)", ".si_i(en_i[0])", ".q_o(rst_sync_n)", ".so_o()"):
        assert connection in flat, connection
    result = vet.finish()
    assert result.ok, result.report()
    assert records(result) == [("core", "proved")]


def test_only_the_module_of_the_net_is_edited(vet, design, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    core = with_enable(vet)
    iso(core, waiver=WHY)
    assert design.changed(vet) == {"top", "core"}  # `top` by the punched enable
    assert design.text(vet, "iso_cell") == ISO


# --- I3: refusals -----------------------------------------------------------------------------------

REFUSED = {
    "no such net": ({"net": "nosuch"}, "nosuch"),
    "an input port": ({"net": "rst_ni"}, "rst_ni"),
    "a net with two drivers": ({"net": "split"}, "split"),
    "a net of an always block": ({"net": "seen"}, "seen"),
    "a net with no driver": ({"net": "floating"}, "floating"),
    "no such cell": ({"cell": "nocell"}, "nocell"),
    "the module itself": ({"cell": "core"}, "core"),
    "a name that is no identifier": ({"name": "u iso"}, "u iso"),
    "a name that is taken": ({"name": "u_sync"}, "u_sync"),
    "no such data input": ({"data_in": "x_i"}, "x_i"),
    "no such data output": ({"data_out": "x_o"}, "x_o"),
    "no such enable pin": ({"enable_pin": "x_i"}, "x_i"),
    "the data input is an output": ({"data_in": "q_o", "data_out": "d_i"}, "q_o"),
    "the data output is an input": ({"data_out": "d_i", "data_in": "q_o", "enable_pin": "iso_i"}, "d_i"),
    "one pin twice": ({"enable_pin": "d_i"}, "d_i"),
    "the enable is no port": ({"enable": "nosuch_en"}, "nosuch_en"),
    "the enable is a port of the input design": ({"enable": "rst_ni"}, "rst_ni"),
    "the enable is two bits": ({"enable": "en_i"}, "en_i"),
    "the keep name is taken": ({"keep": "rst_slow_n"}, "rst_slow_n"),
    "the width of the cell": ({"net": "cnt"}, "width"),
    "a pin that the cell does not have": ({"connect": {"x_i": "1'b0"}}, "x_i"),
    "one of the three pins in connect": ({"connect": {"d_i": "1'b0"}}, "d_i"),
    "an output in connect": ({"connect": {"q_o": "rst_o"}}, "q_o"),
    "unconnected names an input": ({"unconnected": ["iso_i"]}, "iso_i"),
    "unconnected names the data output": ({"unconnected": ["q_o"]}, "q_o"),
    "a parameter that the cell does not have": ({"params": {"W": 8}}, "W"),
}


@pytest.mark.parametrize("case", REFUSED)
def test_what_cannot_be_done_is_refused(vet, design, monkeypatch, case):
    monkeypatch.setattr(equiv, "available", lambda: False)
    change, needle = REFUSED[case]
    core = with_enable(vet)
    before = statuses(vet)
    with pytest.raises(VetError) as e:
        iso(core, waiver=WHY, **change)
    assert str(e.value).startswith("insert_cell_on_net core: "), str(e.value)
    assert needle in str(e.value), str(e.value)
    assert statuses(vet) == before
    assert design.changed(vet) == {"top", "core"}
    assert "u_iso" not in design.text(vet, "core")


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        ({}, "si_i"),
        ({"connect": {"si_i": "en_i[0]"}}, "so_o"),
    ],
    ids=["an input pin that nothing names", "an output pin that nothing names"],
)
def test_every_pin_of_the_cell_is_named(vet, monkeypatch, change, needle):
    monkeypatch.setattr(equiv, "available", lambda: False)
    core = with_enable(vet)
    with pytest.raises(VetError) as e:
        iso(core, cell="scan_cell", enable_pin="se_i", waiver=WHY, **change)
    assert str(e.value).startswith("insert_cell_on_net core: "), str(e.value)
    assert needle in str(e.value), str(e.value)


def test_without_yosys_and_without_a_waiver_it_is_refused(vet, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    core = with_enable(vet)
    with pytest.raises(VetError, match=r"hdl-vet\[equiv\]") as e:
        iso(core)
    assert str(e.value).startswith("insert_cell_on_net core: ")


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("name", 5),
        ("data_in", None),
        ("data_out", ["q_o"]),
        ("enable_pin", b"iso_i"),
        ("enable", 1),
        ("connect", [("si_i", "1'b0")]),
        ("connect", {"si_i": None}),
        ("unconnected", "so_o"),
        ("params", "W"),
        ("keep", 5),
        ("waiver", ""),
    ],
    ids=repr,
)
def test_an_argument_of_a_wrong_kind_is_refused(vet, monkeypatch, argument, value):
    monkeypatch.setattr(equiv, "available", lambda: False)
    core = with_enable(vet)
    given = {"waiver": WHY, argument: value}
    with pytest.raises(VetError) as e:
        iso(core, **given)
    assert str(e.value).startswith("insert_cell_on_net core"), str(e.value)


# --- I4, I5: a second run, the log, a group, a save --------------------------------------------------


def test_a_second_run_is_skipped(vet, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    core = with_enable(vet)
    iso(core, waiver=WHY)
    iso(vet.module("core"), waiver=WHY)
    assert statuses(vet)[-2:] == [
        ("insert_cell_on_net", "core.rst_sync_n", "applied"),
        ("insert_cell_on_net", "core.rst_sync_n", "skipped"),
    ]


def test_another_cell_under_the_same_name_is_a_drift(vet, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    core = with_enable(vet)
    iso(core, waiver=WHY)
    with pytest.raises(DriftError) as e:
        iso(vet.module("core"), cell="inv_cell", enable_pin="en_i", waiver=WHY)
    assert str(e.value).startswith("insert_cell_on_net core"), str(e.value)


def test_a_refusal_in_a_group_that_the_script_catches_changes_nothing(vet, design, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    core = with_enable(vet)
    before = (statuses(vet), design.text(vet, "core"), sorted(c.key for c in vet._checks))
    with vet.group():
        with pytest.raises(VetError):
            iso(core, keep="rst_slow_n", waiver=WHY)
    assert (statuses(vet), design.text(vet, "core"), sorted(c.key for c in vet._checks)) == before


def test_a_saved_session_keeps_the_operation(vet, tmp_path, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    core = with_enable(vet)
    iso(core, waiver=WHY)
    vet.save(tmp_path / "s.zip")
    back = Vet.restore(tmp_path / "s.zip")
    result = back.finish()
    assert result.ok, result.report()
    assert [(r.operations, r.status) for r in result.equivalence] == [
        (["insert_cell_on_net core.rst_sync_n"], "waived")
    ]


# --- I6 to I8: the sign-off -------------------------------------------------------------------------


@needs_yosys
def test_the_record_and_the_report(vet):
    core = with_enable(vet)
    iso(core)
    result = vet.finish()
    assert result.ok, result.report()
    (record,) = result.equivalence
    assert record.module == "core"
    assert record.operations == ["insert_cell_on_net core.rst_sync_n"]
    assert record.waiver is None
    line = "  equivalence proved: core (insert_cell_on_net core.rst_sync_n; equal when test_en_i is 0)"
    assert line in result.report().splitlines()
    assert ("instance-added", "top.u_core.u_iso") in [(c.kind, c.path) for c in result.changes]
    assert [(e.module, e.instance, e.pin, e.status) for e in result.enables] == [
        ("top", "u_core", "test_en_i", "carried")
    ]


@needs_yosys
def test_a_cell_that_is_not_off_at_0_is_never_proved(vet):
    core = with_enable(vet)
    iso(core, cell="inv_cell", enable_pin="en_i")
    result = vet.finish()
    assert records(result) == [("core", "refuted")]
    assert not result.ok


@needs_yosys
def test_a_cell_wired_the_wrong_way_round_is_never_proved(vet, design):
    """The cell is right, and a raw edit then feeds it from another net."""
    core = with_enable(vet)
    iso(core)
    design.replace(vet, "core", b".d_i(rst_sync_n_pre)", b".d_i(rst_slow_n)")
    result = vet.finish()
    assert not result.ok


@needs_yosys
def test_an_enable_that_the_top_ties_to_1_fails_the_sign_off(vet, design):
    vet.module("core").add_port("test_en_i", "input")
    vet.module("top").instance("u_core").connect("test_en_i", "1'b1")
    iso(vet.module("core"))
    result = vet.finish()
    assert not result.ok
    assert [e.status for e in result.enables] == ["not off"]


@needs_yosys
def test_a_waiver_keeps_the_answer_of_the_check(vet):
    core = with_enable(vet)
    iso(core, cell="inv_cell", enable_pin="en_i", waiver=WHY)
    result = vet.finish()
    assert result.ok, result.report()
    (record,) = result.equivalence
    assert (record.status, record.check, record.waiver) == ("waived", "refuted", WHY)


@needs_yosys
def test_a_cell_and_an_override_in_one_module_are_one_comparison(vet):
    core = with_enable(vet)
    iso(core)
    core.override_net("irq_o", "1'b0", enable="test_en_i")
    result = vet.finish()
    assert result.ok, result.report()
    (record,) = result.equivalence
    assert record.operations == ["insert_cell_on_net core.rst_sync_n", "override_net core.irq_o"]


# --- I9: the check of the operation -----------------------------------------------------------------


def test_a_cell_that_a_raw_edit_takes_out_again_fails_the_check(vet, design, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    core = with_enable(vet)
    iso(core, waiver=WHY)
    text = design.text(vet, "core")
    start = text.index("iso_cell")
    end = text.index(";", start) + 1
    design.replace(vet, "core", text[start:end].encode(), b"assign rst_sync_n = rst_sync_n_pre;", waiver=WHY)
    result = vet.finish()
    assert not result.ok
    assert any("insert_cell_on_net core" in str(c) for c in result.failed_checks), result.report()


# --- I10: no loop through the cell ------------------------------------------------------------------

XCELL = (
    "module xcell (input logic d_i, input logic iso_i, input logic x_i, output logic q_o);\n"
    "  assign q_o = iso_i ? 1'b0 : (d_i & x_i);\n"
    "endmodule\n"
)


@pytest.fixture
def xvet(tmp_path: Path) -> Vet:
    return Design(tmp_path / "design", {"xcell": XCELL, **CELLS, **FILES}).vet()


def xcell(core, expr: str, **more: object) -> None:
    iso(core, cell="xcell", connect={"x_i": expr}, waiver=WHY, **more)


@pytest.mark.parametrize("expr", ["rst_sync_n", "rst_sync_n & en_i[0]", "rst_o", "~rst_o | en_i[1]"])
def test_a_connection_that_reads_the_net_is_refused(xvet, monkeypatch, expr):
    """`rst_o` is `assign rst_o = rst_sync_n;`: the net read through another net of the module."""
    monkeypatch.setattr(equiv, "available", lambda: False)
    core = with_enable(xvet)
    before = statuses(xvet)
    with pytest.raises(VetError) as e:
        xcell(core, expr)
    assert str(e.value).startswith("insert_cell_on_net core: "), str(e.value)
    assert "loop" in str(e.value), str(e.value)
    assert statuses(xvet) == before


FUNC = FILES["core"].replace(
    "  logic       floating;\n",
    "  logic       floating;\n  function automatic logic f();\n    return rst_sync_n;\n  endfunction\n",
)


@pytest.mark.parametrize("expr", ["f()", "f() & en_i[0]", "u_timer.irq_o"])
def test_a_connection_that_the_tool_cannot_follow_is_refused(tmp_path, monkeypatch, expr):
    monkeypatch.setattr(equiv, "available", lambda: False)
    assert FILES["core"] != FUNC
    vet = Design(tmp_path / "design", {"xcell": XCELL, **CELLS, **FILES, "core": FUNC}).vet()
    core = with_enable(vet)
    before = statuses(vet)
    with pytest.raises(VetError) as e:
        xcell(core, expr)
    assert str(e.value).startswith("insert_cell_on_net core: "), str(e.value)
    assert "cannot follow" in str(e.value), str(e.value)
    assert statuses(vet) == before


@pytest.mark.parametrize("expr", ["rst_sync_n_pre", "rst_slow_n", "en_i[0]", "1'b1"])
def test_a_connection_that_does_not_read_the_net_is_taken(xvet, monkeypatch, expr):
    """The keep signal is in front of the cell, so reading it is no loop."""
    monkeypatch.setattr(equiv, "available", lambda: False)
    core = with_enable(xvet)
    xcell(core, expr)
    assert statuses(xvet)[-1] == ("insert_cell_on_net", "core.rst_sync_n", "applied")


# --- I11: the parameters ----------------------------------------------------------------------------


def bus(core, **more: object) -> None:
    iso(core, "cnt", cell="iso_bus", name="u_bus", waiver=WHY, **more)


def test_a_second_run_with_another_parameter_value_is_a_drift(vet, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    core = with_enable(vet)
    bus(core, params={"W": 8})
    bus(vet.module("core"), params={"W": 8})
    assert statuses(vet)[-1] == ("insert_cell_on_net", "core.cnt", "skipped")
    for other in ({"W": 4}, {"W": "4 + 4"}, None):
        with pytest.raises(DriftError) as e:
            bus(vet.module("core"), params=other)
        assert str(e.value).startswith("insert_cell_on_net core"), str(e.value)


def test_a_parameter_that_a_raw_edit_changes_fails_the_check(vet, design, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    core = with_enable(vet)
    iso(core, cell="iso_bus", name="u_bus", params={"W": 1}, waiver=WHY)
    design.replace(vet, "core", b".W(1)", b".W(1 + 0)", waiver=WHY)
    result = vet.finish()
    assert not result.ok
    assert any("insert_cell_on_net core" in str(c) for c in result.failed_checks), result.report()


@pytest.mark.parametrize("value", ["8 // x", "8), .Q(2", "8\n", "8;", "8)) u_x (); iso_bus #(.W(8", "", None, True])
def test_a_parameter_value_is_one_expression(vet, design, monkeypatch, value):
    monkeypatch.setattr(equiv, "available", lambda: False)
    core = with_enable(vet)
    before = statuses(vet)
    with pytest.raises(VetError) as e:
        bus(core, params={"W": value})
    assert str(e.value).startswith("insert_cell_on_net core: "), str(e.value)
    assert statuses(vet) == before
    assert "u_bus" not in design.text(vet, "core")


# --- I12: one logic pattern on a net ----------------------------------------------------------------


def test_a_second_cell_on_the_net_is_refused(vet, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    core = with_enable(vet)
    iso(core, waiver=WHY)
    with pytest.raises(VetError) as e:
        iso(vet.module("core"), name="u_iso2", keep="pre2", waiver=WHY)
    assert str(e.value).startswith("insert_cell_on_net core: "), str(e.value)
    assert "insert_cell_on_net core.rst_sync_n" in str(e.value)
    assert not isinstance(e.value, DriftError)


def test_an_override_on_the_net_of_a_cell_is_refused(vet, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    core = with_enable(vet)
    iso(core, waiver=WHY)
    with pytest.raises(VetError) as e:
        vet.module("core").override_net("rst_sync_n", "rst_ni", enable="test_en_i", keep="pre2", waiver=WHY)
    assert str(e.value).startswith("override_net core: "), str(e.value)
    assert "insert_cell_on_net core.rst_sync_n" in str(e.value)


def test_a_cell_on_the_net_of_an_override_is_refused(vet, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    core = with_enable(vet)
    core.override_net("rst_sync_n", "rst_ni", enable="test_en_i", waiver=WHY)
    with pytest.raises(VetError) as e:
        iso(vet.module("core"), keep="pre2", waiver=WHY)
    assert str(e.value).startswith("insert_cell_on_net core: "), str(e.value)
    assert "override_net core.rst_sync_n" in str(e.value)


def test_cells_on_two_nets_of_one_module_are_taken(vet, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    core = with_enable(vet)
    iso(core, waiver=WHY)
    iso(vet.module("core"), "rst_slow_n", name="u_iso2", waiver=WHY)
    result = vet.finish()
    assert result.ok, result.report()
    (record,) = result.equivalence
    assert record.operations == ["insert_cell_on_net core.rst_sync_n", "insert_cell_on_net core.rst_slow_n"]


# --- I13: the declaration of the kept signal --------------------------------------------------------


def test_another_type_of_the_kept_signal_fails_the_check(vet, design, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    core = with_enable(vet)
    iso(core, waiver=WHY)
    design.replace(vet, "core", b"logic rst_sync_n_pre;", b"wire rst_sync_n_pre;", waiver=WHY)
    result = vet.finish()
    assert not result.ok
    assert any("insert_cell_on_net core" in str(c) for c in result.failed_checks), result.report()


# --- I14, I15: the kind and the start of a refusal --------------------------------------------------


def test_a_name_that_another_instance_has_is_no_drift(vet, monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)
    core = with_enable(vet)
    with pytest.raises(VetError) as e:
        iso(core, name="u_sync", waiver=WHY)
    assert not isinstance(e.value, DriftError), str(e.value)
    assert str(e.value).startswith("insert_cell_on_net core: "), str(e.value)


def test_a_refusal_from_the_shared_code_names_this_operation(vet, monkeypatch):
    """The keep name has a post-condition of this run: the refusal that `override_net` gives for it."""
    monkeypatch.setattr(equiv, "available", lambda: False)
    core = with_enable(vet)
    core.add_wire("kk")
    core.add_assign("kk", "en_i[0]")
    core.add_assign("floating", "en_i[1]")
    with vet.group():
        vet.module("core").remove_assign("kk")
        vet.module("core").remove_wire("kk")
    with pytest.raises(VetError) as e:
        iso(vet.module("core"), "floating", keep="kk", waiver=WHY)
    assert str(e.value).startswith("insert_cell_on_net core: "), str(e.value)
    assert "override_net" not in str(e.value), str(e.value)
