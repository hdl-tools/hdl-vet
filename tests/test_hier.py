"""Phase 4: hierarchical operations on `Vet` (`hdl_vet.ops`).

Both operations are built from the phase 3 operations (add_port, connect,
add_assign, add_param, set_param) and inherit their rules: skip / apply / drift,
derived expectations, post-conditions, layout of inserted text, and the refusals
(macro instances, positional or `.*` lists, conditional compilation in a list,
instances inside generate blocks or instance arrays, non-ANSI or macro headers).
Each underlying operation appears in `vet.log`.

Paths
    `to` is a hierarchical instance path from the top module, e.g.
    "top.u_mid.u_l0": the first segment is the top module's name, each further
    segment an instance in the module reached so far. The modules along it are
    M0 (the top) ... Mn (the destination). VetError for a path that is only the
    top, does not start at the top, or names an unknown instance (with
    suggestions); for a segment written with an index ("g[0]") the message says
    generate blocks and instance arrays are not supported.

All or nothing
    The whole path is validated before anything is queued. When an operation
    raises (VetError or DriftError) nothing has been queued or logged.

Re-loads
    The operation knows what it has just added, so on a design with nothing
    queued it causes no re-load of its own.

vet.punch_port(name, direction, to, *, signal=None, width=None, type="logic",
               all_instances=False)
    direction "input"
        A signal enters at the top and arrives in Mn as input port `name`.
        M0 gets input port `name` (add_port) unless `signal` is given, in which
        case `signal`, a plain identifier naming an existing port or
        module-level net/variable of M0, is the source and M0 gets no new port.
        Each of M1..Mn gets input port `name`, and its instance in the module
        above is connected: `.name(name)`, or `.name(signal)` in M0 when
        `signal` is given.
    direction "output"
        `signal`, a plain identifier naming a port or module-level net/variable
        of Mn (default: `name`), leaves at the top as output port `name`.
        Mn gets output port `name` and `assign name = signal;`. Each of
        M(n-1)..M0 gets output port `name`, and the instance below is connected
        `.name(name)`. An interface port cannot be punched (VetError).
    Type and width
        `type` defaults to None. When `type` or `width` is given, the port is
        declared as in add_port (type "logic" if only `width` is given).
        When both are None and there is a `signal` (always for an output, and for
        an input with `signal=`), the port takes the data type of the signal's
        declaration as written: "logic [3:0]", "logic [2:0][31:0]",
        "logic [p_pkg::W-1:0]", "logic signed [7:0]", "p_pkg::mode_t". A port
        that shares a declaration (`output logic [3:0] y0, y1`) takes the shared
        type; a net keyword (`wire [2:0] n`) or an implicit type becomes `logic`
        with the same dimensions. An input without `signal` and without `type`
        or `width` is a 1-bit `logic`.
        The type must mean the same in every module on the path: every
        identifier in it must be package-scoped (`pkg::NAME`); keywords, system
        functions and literals are fine. A type that uses a bare identifier (a
        parameter, localparam or typedef of one module) is refused with a
        VetError naming it and asking for an explicit `width=`. The same rule
        applies to a `width` or `type` given by the caller. A signal with
        unpacked dimensions is refused ("unpacked").
    Existing pieces
        A module on the path that already has the port, declared identically,
        is left as it is (logged as a skip), and likewise an instance already
        connected `.name(name)`: a second identical call is all skips, and a
        partly punched path is completed. For an output whose `signal` is `name`
        and `name` is already an output port of Mn, Mn is not edited at all (no
        port, no assign) and M(n-1)..M0 are punched as usual.
    Log
        One record per underlying operation; their order within one call is
        not specified.
    Shared modules
        Adding a port to a module changes every instance of it. By default each
        of M1..Mn must be elaborated exactly once (VetError otherwise, naming the
        count and `all_instances`). With all_instances=True and direction
        "input", every instance of each of M1..Mn is connected, in whichever
        module it sits; that module must itself be one of M0..Mn (it then has
        the signal), else VetError naming the stray instance. Instances sitting
        in M0 are connected to `signal` when one is given. An output cannot
        go through a shared module (several copies would drive one net):
        VetError whatever `all_instances` says.

vet.propagate_param(name, to, *, default=None)
    Makes an existing parameter of the destination settable from the top without
    changing its value. `name` must be an overridable value parameter of Mn.
    Each of M0..M(n-1) gets `parameter <type> name = <default>` (add_param) with
    the type as declared in Mn, and the instance below is overridden with
    `.name(name)` (set_param, replacing an existing override).
    <default> is `default` if given; else, when the destination instance is
    already overridden with `.name(name)` and M(n-1) has a value parameter
    `name` (an earlier propagation), that parameter's default in M(n-1); else
    the destination instance's current override text; else the default declared
    in Mn. It, and the type, must mean the same in every module on the path
    (same package-scoped rule as port types); otherwise VetError asking for an
    explicit `default=`. An implicit type stays implicit: `parameter K = 5`.
    Each of M0..M(n-1) must be elaborated exactly once (the destination module
    itself may be shared: it is not edited).
    The value in use must not change. The operation does NOT permit a
    parameter change at the destination path, so a different value is an
    unexpected change at sign-off; and a post-condition checks that the
    parameter's resolved value at the destination path is the same before and
    after (a failed check starts with "propagate_param").
"""

from __future__ import annotations

from pathlib import Path

import pytest
from vetkit import make_design

from hdl_vet.ops import DriftError, Vet, VetError
from hdl_vet.session import Session
from hdl_vet.signoff import Change


@pytest.fixture
def kit(tmp_path: Path):
    return make_design(tmp_path)


@pytest.fixture
def vet(kit):
    return Vet(Session(kit.filelist, top="top"))


def vet_for(tmp_path: Path, src: bytes, top: str = "t") -> Vet:
    (tmp_path / "t.sv").write_bytes(src)
    (tmp_path / "t.f").write_bytes(b"t.sv\n")
    return Vet(Session(tmp_path / "t.f", top=top))


def edits(diff: str) -> list[str]:
    return [
        line
        for line in diff.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---")) and "Modified by hdl-vet" not in line
    ]


def ops(vet: Vet) -> list[tuple[str, str, str]]:
    return [(r.op, r.target, r.status) for r in vet.log]


MID_PORT = ["-  output logic [3:0] y0, y1", "+  output logic [3:0] y0, y1,"]
TOP_PORT = ["-  output logic [3:0] y1", "+  output logic [3:0] y1,"]
U_MID = "  mid u_mid (.clk(clk), .a(a), .y0(y0), .y1(y1));"
U_SPARE = "  leaf u_spare (.clk(clk), .d(spare_d), .q(spare_q));"


# --- punch_port: input ---------------------------------------------------------------


def test_punch_input_two_levels(vet):
    vet.punch_port("dft_en", "input", to="top.u_mid")
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        # rtl/mid.sv
        *MID_PORT,
        "+  input  logic dft_en",
        # rtl/top.sv
        *TOP_PORT,
        "+  input  logic dft_en",
        "-" + U_MID,
        "+" + U_MID.replace("));", "), .dft_en(dft_en));"),
    ]
    assert r.changes == [
        Change("port-added", "top", "dft_en", None, "input 1"),
        Change("connection-changed", "top.u_mid", "dft_en", None, "dft_en"),
        Change("port-added", "top.u_mid", "dft_en", None, "input 1"),
    ]
    assert sorted(ops(vet)) == [
        ("add_port", "mid", "applied"),
        ("add_port", "top", "applied"),
        ("connect", "top.u_mid", "applied"),
    ]
    assert vet.session.reloads == 1  # only the sign-off re-load


def test_punch_input_through_a_shared_module_needs_all_instances(vet):
    with pytest.raises(VetError, match=r"leaf.*3.*all_instances"):
        vet.punch_port("dft_en", "input", to="top.u_mid.u_l0")
    assert vet.log == [] and vet.finish().files == {}


def test_punch_input_to_every_instance(vet):
    vet.punch_port("dft_en", "input", to="top.u_mid.u_l0", all_instances=True)
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        # rtl/leaf.sv
        "-  output logic [WIDTH-1:0] q",
        "+  output logic [WIDTH-1:0] q,",
        "+  input  logic dft_en",
        # rtl/mid.sv
        *MID_PORT,
        "+  input  logic dft_en",
        "-  leaf #(.WIDTH(4)) u_l0 (.clk(clk), .d(a), .q(y0));",
        "+  leaf #(.WIDTH(4)) u_l0 (.clk(clk), .d(a), .q(y0), .dft_en(dft_en));",
        "-    .q   (y1)",
        "+    .q   (y1),",
        "+    .dft_en(dft_en)",
        # rtl/top.sv: u_mid and u_spare are adjacent lines, one diff hunk
        *TOP_PORT,
        "+  input  logic dft_en",
        "-" + U_MID,
        "-" + U_SPARE,
        "+" + U_MID.replace("));", "), .dft_en(dft_en));"),
        "+" + U_SPARE.replace("));", "), .dft_en(dft_en));"),
    ]
    assert len([c for c in r.changes if c.kind == "port-added"]) == 5
    assert len([c for c in r.changes if c.kind == "connection-changed"]) == 4
    assert vet.session.reloads == 1


def test_punch_input_from_an_existing_signal(vet, kit):
    vet.punch_port("spare_i", "input", to="top.u_mid", signal="spare_d")  # type taken from spare_d
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        *MID_PORT,
        "+  input  logic [3:0] spare_i",
        "-" + U_MID,
        "+" + U_MID.replace("));", "), .spare_i(spare_d));"),
    ]
    assert Change("connection-changed", "top.u_mid", "spare_i", None, "spare_d") in r.changes
    with pytest.raises(VetError, match="nope"):
        Vet(Session(kit.filelist, top="top")).punch_port("x", "input", to="top.u_mid", signal="nope")


def test_stray_instance_of_a_shared_module_is_refused(tmp_path: Path):
    src = (
        b"module leaf(input logic a);\nendmodule\n"
        b"module side(input logic a);\n  leaf u_stray (.a(a));\nendmodule\n"
        b"module mid(input logic a);\n  leaf u_leaf (.a(a));\nendmodule\n"
        b"module t(input logic a);\n  mid u_mid (.a(a));\n  side u_side (.a(a));\nendmodule\n"
    )
    vet = vet_for(tmp_path, src)
    with pytest.raises(VetError, match=r"t\.u_side\.u_stray"):
        vet.punch_port("dft", "input", to="t.u_mid.u_leaf", all_instances=True)
    assert vet.log == [] and vet.finish().files == {}


# --- punch_port: output ---------------------------------------------------------------


def test_punch_output_two_levels_takes_the_signal_width(vet):
    vet.punch_port("dbg_o", "output", to="top.u_mid", signal="y1")  # y1 shares `output logic [3:0] y0, y1`
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        # rtl/mid.sv
        *MID_PORT,
        "+  output logic [3:0] dbg_o",
        "+  assign dbg_o = y1;",
        # rtl/top.sv
        *TOP_PORT,
        "+  output logic [3:0] dbg_o",
        "-" + U_MID,
        "+" + U_MID.replace("));", "), .dbg_o(dbg_o));"),
    ]
    assert r.changes == [
        Change("port-added", "top", "dbg_o", None, "output 4"),
        Change("connection-changed", "top.u_mid", "dbg_o", None, "dbg_o"),
        Change("port-added", "top.u_mid", "dbg_o", None, "output 4"),
    ]
    assert vet.session.reloads == 1


def test_punch_output_through_a_shared_module_is_always_refused(vet):
    for flag in (False, True):
        with pytest.raises(VetError, match=r"leaf.*output"):
            vet.punch_port("q_dbg", "output", to="top.u_mid.u_l0", signal="q", width=4, all_instances=flag)
    assert vet.log == []


WIDTHS = (
    b"package p_pkg;\n  parameter int W = 4;\n  typedef logic [1:0] mode_t;\nendpackage\n"
    b"interface bus_if;\n  logic v;\nendinterface\n"
    b"module leaf #(parameter int N = 2) (input logic clk, bus_if b);\n"
    b"  typedef logic [2:0] local_t;\n"
    b"  logic [p_pkg::W-1:0] bus;\n  logic [1:0][7:0] grid;\n  logic [N-1:0] loc;\n  logic arr [2];\n  logic one;\n"
    b"  p_pkg::mode_t mode;\n  logic signed [7:0] sgn;\n  wire [2:0] wnet;\n  local_t lt;\n"
    b"  assign bus = '0;\n  assign grid = '0;\n  assign loc = '0;\n  assign one = clk;\n"
    b"endmodule\n"
    b"module mid(input logic clk);\n  bus_if u_if ();\n  leaf u_leaf (.clk(clk), .b(u_if));\nendmodule\n"
    b"module t(input logic clk);\n  mid u_mid (.clk(clk));\nendmodule\n"
)


@pytest.mark.parametrize(
    ("signal", "kwargs", "decl", "width"),
    [
        ("bus", {}, "output logic [p_pkg::W-1:0] o", 4),
        ("grid", {}, "output logic [1:0][7:0] o", 16),
        ("one", {}, "output logic o", 1),
        ("loc", {"width": 2}, "output logic [1:0] o", 2),
        ("bus", {"width": "p_pkg::W"}, "output logic [p_pkg::W-1:0] o", 4),
        ("mode", {}, "output p_pkg::mode_t o", 2),
        ("sgn", {}, "output logic signed [7:0] o", 8),
        ("wnet", {}, "output logic [2:0] o", 3),
        ("lt", {"type": "logic [2:0]"}, "output logic [2:0] o", 3),
    ],
)
def test_punch_output_three_levels_width_forms(tmp_path: Path, signal, kwargs, decl, width):
    vet = vet_for(tmp_path, WIDTHS)
    vet.punch_port("o", "output", to="t.u_mid.u_leaf", signal=signal, **kwargs)
    r = vet.finish()
    assert r.ok, r.report()
    # all three headers are single-line port lists, so the port is appended in place
    headers = [line for line in edits(r.diff) if line.startswith("+module ")]
    assert len(headers) == 3 and all(line.endswith(f", {decl});") for line in headers), headers
    assert f"+  assign o = {signal};" in edits(r.diff)
    assert sorted((c.path, c.after) for c in r.changes if c.kind == "port-added") == [
        ("t", f"output {width}"),
        ("t.u_mid", f"output {width}"),
        ("t.u_mid.u_leaf", f"output {width}"),
    ]
    assert vet.session.reloads == 1


def test_width_that_is_local_to_one_module_is_refused(tmp_path: Path):
    vet = vet_for(tmp_path, WIDTHS)
    with pytest.raises(VetError, match=r"\bN\b.*width="):
        vet.punch_port("o", "output", to="t.u_mid.u_leaf", signal="loc")
    with pytest.raises(VetError, match=r"\bN\b"):
        vet.punch_port("i", "input", to="t.u_mid.u_leaf", width="N")
    with pytest.raises(VetError, match=r"local_t.*width="):
        vet.punch_port("o", "output", to="t.u_mid.u_leaf", signal="lt")
    with pytest.raises(VetError, match="local_t"):
        vet.punch_port("i", "input", to="t.u_mid.u_leaf", type="local_t")
    with pytest.raises(DriftError, match="u_if"):
        # free in t, but taken by an instance name in the intermediate module
        vet.punch_port("u_if", "input", to="t.u_mid.u_leaf")
    with pytest.raises(VetError, match="unpacked"):
        vet.punch_port("o", "output", to="t.u_mid.u_leaf", signal="arr")
    with pytest.raises(VetError, match="interface"):
        vet.punch_port("o", "output", to="t.u_mid.u_leaf", signal="b")
    with pytest.raises(VetError, match=r"'bsu'.*did you mean 'bus'"):
        vet.punch_port("o", "output", to="t.u_mid.u_leaf", signal="bsu")
    assert vet.log == [] and vet.finish().files == {}


def test_punch_output_defaults_the_signal_to_the_name(tmp_path: Path):
    vet = vet_for(tmp_path, WIDTHS)
    with pytest.raises(DriftError, match="bus"):
        # `bus` exists in leaf as a net, so it cannot also become a port there
        vet.punch_port("bus", "output", to="t.u_mid.u_leaf")
    assert vet.log == []


def test_punch_output_of_an_existing_output_port_is_all_skips(vet):
    # y0 is already an output of mid, wired to top's y0: nothing to do anywhere
    vet.punch_port("y0", "output", to="top.u_mid")
    assert sorted(ops(vet)) == [("add_port", "top", "skipped"), ("connect", "top.u_mid", "skipped")]
    assert vet.finish().files == {}


LEAF_Q = b"module leaf (input logic a, output logic [1:0] q);\n  assign q = {a, a};\nendmodule\n"
T_MID = b"module t (input logic a);\n  mid u_mid (.a(a));\nendmodule\n"


def test_existing_output_port_is_carried_up_without_touching_its_module(tmp_path: Path):
    # leaf already has output q, left open in mid: only mid and t are edited
    vet = vet_for(
        tmp_path, LEAF_Q + b"module mid (input logic a);\n  leaf u_leaf (.a(a), .q());\nendmodule\n" + T_MID
    )
    vet.punch_port("q", "output", to="t.u_mid.u_leaf")
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        "-module mid (input logic a);",
        "-  leaf u_leaf (.a(a), .q());",
        "+module mid (input logic a, output logic [1:0] q);",
        "+  leaf u_leaf (.a(a), .q(q));",
        "-module t (input logic a);",
        "-  mid u_mid (.a(a));",
        "+module t (input logic a, output logic [1:0] q);",
        "+  mid u_mid (.a(a), .q(q));",
    ]
    assert ("add_port", "leaf", "applied") not in ops(vet)
    assert vet.session.reloads == 1


def test_existing_output_port_connected_elsewhere_is_drift(tmp_path: Path):
    vet = vet_for(
        tmp_path,
        LEAF_Q
        + b"module mid (input logic a);\n  logic [1:0] q_other;\n  leaf u_leaf (.a(a), .q(q_other));\nendmodule\n"
        + T_MID,
    )
    with pytest.raises(DriftError, match="q_other"):
        vet.punch_port("q", "output", to="t.u_mid.u_leaf")
    assert vet.log == [] and vet.finish().files == {}


def test_positional_connections_on_the_path_are_refused(tmp_path: Path):
    src = (
        b"module leaf (input logic a);\nendmodule\n"
        b"module mid (input logic a);\n  leaf u_leaf (a);\nendmodule\n"
        b"module t (input logic a);\n  mid u_mid (.a(a));\nendmodule\n"
    )
    vet = vet_for(tmp_path, src)
    with pytest.raises(VetError, match="u_leaf"):
        vet.punch_port("x", "input", to="t.u_mid.u_leaf")
    assert vet.log == [] and vet.finish().files == {}


# --- paths, refusals, all or nothing ------------------------------------------------------


def test_path_errors(vet):
    with pytest.raises(VetError, match="instance below the top"):
        vet.punch_port("x", "input", to="top")
    with pytest.raises(VetError, match=r"'mid'.*top"):
        vet.punch_port("x", "input", to="mid.u_l0")
    with pytest.raises(VetError, match=r"'u_mdi'.*did you mean 'u_mid'"):
        vet.punch_port("x", "input", to="top.u_mdi")
    with pytest.raises(VetError, match=r"'u_l9'.*did you mean"):
        vet.punch_port("x", "input", to="top.u_mid.u_l9")
    with pytest.raises(VetError, match="direction"):
        vet.punch_port("x", "inout", to="top.u_mid")
    assert vet.log == []


def test_generate_blocks_and_arrays_on_the_path_are_refused(tmp_path: Path):
    src = (
        b"module leaf(input logic a);\nendmodule\n"
        b"module t(input logic a);\n"
        b"  for (genvar i = 0; i < 2; i++) begin : g\n    leaf u (.a(a));\n  end\n"
        b"  leaf u_arr [1:0] (.a(a));\n"
        b"endmodule\n"
    )
    vet = vet_for(tmp_path, src)
    for to, word in (("t.g[0].u", "generate"), ("t.u", "generate block"), ("t.u_arr", "instance array"), ("t.u_arr[0]", "array")):
        with pytest.raises(VetError, match=word):
            vet.punch_port("x", "input", to=to, all_instances=True)
    assert vet.log == [] and vet.finish().files == {}


def test_nothing_is_queued_when_a_lower_level_conflicts(vet):
    # top and mid could take a port `d`; leaf already has `d` with another type
    with pytest.raises(DriftError, match=r"leaf.*'d'"):
        vet.punch_port("d", "input", to="top.u_mid.u_l0", all_instances=True)
    assert vet.log == []
    r = vet.finish()
    assert r.ok and r.files == {}


def test_punch_is_idempotent_and_completes_a_partial_one(vet):
    vet.module("top").add_port("dft_en", "input")
    vet.punch_port("dft_en", "input", to="top.u_mid")
    assert sorted(ops(vet)) == [
        ("add_port", "mid", "applied"),
        ("add_port", "top", "applied"),
        ("add_port", "top", "skipped"),
        ("connect", "top.u_mid", "applied"),
    ]
    vet.punch_port("dft_en", "input", to="top.u_mid")
    assert [s for _, _, s in ops(vet)][-3:] == ["skipped"] * 3
    r = vet.finish()
    assert r.ok, r.report()
    assert len([line for line in edits(r.diff) if line.startswith("+") and "dft_en" in line]) == 3


def test_two_punches_through_the_same_modules(vet):
    vet.punch_port("dft_en", "input", to="top.u_mid")
    vet.punch_port("dbg_o", "output", to="top.u_mid", signal="y0")
    r = vet.finish()
    assert r.ok, r.report()
    assert "+" + U_MID.replace("));", "), .dft_en(dft_en), .dbg_o(dbg_o));") in edits(r.diff)
    assert "+  input  logic dft_en," in edits(r.diff) and "+  output logic [3:0] dbg_o" in edits(r.diff)


def test_post_condition_fires_when_a_level_is_undone(vet, kit):
    from vetkit import span

    vet.punch_port("dft_en", "input", to="top.u_mid")
    vet.design  # apply the queue
    start, end = span(vet.text(kit.top), b", .dft_en(dft_en)")
    vet.splice(kit.top, start, end, b"")
    r = vet.finish()
    assert not r.ok
    assert any(c.startswith("connect") and "u_mid" in c for c in r.failed_checks)


# --- propagate_param ------------------------------------------------------------------------


def test_propagate_param_from_an_overridden_instance(vet):
    vet.propagate_param("DEPTH", to="top.u_mid.u_l1")
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        # rtl/mid.sv
        "-module mid (",
        "+module mid #(parameter int DEPTH = 3) (",
        "-  leaf #(.WIDTH(4), .DEPTH(3)) u_l1 (",
        "+  leaf #(.WIDTH(4), .DEPTH(DEPTH)) u_l1 (",
        # rtl/top.sv
        "-module top (",
        "+module top #(parameter int DEPTH = 3) (",
        "-" + U_MID,
        "+" + U_MID.replace("mid u_mid", "mid #(.DEPTH(DEPTH)) u_mid"),
    ]
    assert r.changes == [
        Change("param-changed", "top", "DEPTH", None, "3"),
        Change("param-changed", "top.u_mid", "DEPTH", None, "3"),
    ]
    assert vet.session.reloads == 1


def test_propagate_param_uses_the_declared_default_without_an_override(vet):
    vet.propagate_param("DEPTH", to="top.u_mid.u_l0")
    r = vet.finish()
    assert r.ok, r.report()
    assert "+module mid #(parameter int DEPTH = 2) (" in edits(r.diff)
    assert "+  leaf #(.WIDTH(4), .DEPTH(DEPTH)) u_l0 (.clk(clk), .d(a), .q(y0));" in edits(r.diff)
    assert {(c.path, c.after) for c in r.changes} == {("top", "2"), ("top.u_mid", "2")}


def test_propagate_param_one_level(vet):
    vet.propagate_param("DEPTH", to="top.u_spare")
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        "-module top (",
        "+module top #(parameter int DEPTH = 2) (",
        "-" + U_SPARE,
        "+" + U_SPARE.replace("leaf u_spare", "leaf #(.DEPTH(DEPTH)) u_spare"),
    ]


PARAMS = (
    b"package p_pkg;\n  parameter int W = 4;\n  typedef logic [1:0] mode_t;\nendpackage\n"
    b"module leaf #(parameter int unsigned N = 2, parameter p_pkg::mode_t M = 2'd1, parameter K = 5,\n"
    b"              localparam int L = 3, parameter type T = logic) (input logic a);\nendmodule\n"
    b"module mid (input logic a);\n  localparam int LOCAL = 4;\n"
    b"  leaf #(.N(LOCAL * 2), .M(2'd2)) u_leaf (.a(a));\nendmodule\n"
    b"module t (input logic a);\n  mid u_mid (.a(a));\nendmodule\n"
)


def test_propagate_param_copies_the_declared_type(tmp_path: Path):
    vet = vet_for(tmp_path, PARAMS)
    vet.propagate_param("M", to="t.u_mid.u_leaf")
    r = vet.finish()
    assert r.ok, r.report()
    assert "+module mid #(parameter p_pkg::mode_t M = 2'd2) (input logic a);" in edits(r.diff)
    assert "+module t #(parameter p_pkg::mode_t M = 2'd2) (input logic a);" in edits(r.diff)
    assert "+  leaf #(.N(LOCAL * 2), .M(M)) u_leaf (.a(a));" in edits(r.diff)


def test_propagate_param_needs_a_value_valid_everywhere(tmp_path: Path):
    vet = vet_for(tmp_path, PARAMS)
    with pytest.raises(VetError, match=r"LOCAL.*default="):
        vet.propagate_param("N", to="t.u_mid.u_leaf")
    assert vet.log == []

    vet.propagate_param("N", to="t.u_mid.u_leaf", default=8)  # same value as LOCAL * 2
    r = vet.finish()
    assert r.ok, r.report()
    assert "+module t #(parameter int unsigned N = 8) (input logic a);" in edits(r.diff)

    vet = vet_for(tmp_path, PARAMS)
    vet.propagate_param("N", to="t.u_mid.u_leaf", default=9)  # would change the value in use
    r = vet.finish()
    assert not r.ok
    assert any(c.startswith("propagate_param") and "N" in c for c in r.failed_checks)
    assert [(c.kind, c.path, c.name) for c in r.unexpected_changes] == [("param-changed", "t.u_mid.u_leaf", "N")]


def test_propagate_param_refusals(tmp_path: Path, vet):
    with pytest.raises(VetError, match=r"'DETPH'.*did you mean 'DEPTH'"):
        vet.propagate_param("DETPH", to="top.u_mid.u_l1")
    with pytest.raises(VetError, match="instance below the top"):
        vet.propagate_param("DEPTH", to="top")
    assert vet.log == []

    v2 = vet_for(tmp_path, PARAMS)
    for name, kind in (("L", "localparam"), ("T", "type parameter")):
        with pytest.raises(VetError, match=rf"'{name}'.*{kind}"):
            v2.propagate_param(name, to="t.u_mid.u_leaf")
    v2.propagate_param("K", to="t.u_mid.u_leaf")  # implicit type stays implicit
    r = v2.finish()
    assert r.ok, r.report()
    assert "+module t #(parameter K = 5) (input logic a);" in edits(r.diff)


def test_propagate_param_refuses_a_shared_ancestor(tmp_path: Path):
    src = (
        b"module leaf #(parameter int N = 2) (input logic a);\nendmodule\n"
        b"module pair(input logic a);\n  leaf u (.a(a));\nendmodule\n"
        b"module t(input logic a);\n  pair u_p0 (.a(a));\n  pair u_p1 (.a(a));\nendmodule\n"
    )
    vet = vet_for(tmp_path, src)
    with pytest.raises(VetError, match=r"pair.*2"):
        vet.propagate_param("N", to="t.u_p0.u")
    assert vet.log == [] and vet.finish().files == {}


def test_propagate_param_is_idempotent(vet):
    vet.propagate_param("DEPTH", to="top.u_mid.u_l1")
    vet.propagate_param("DEPTH", to="top.u_mid.u_l1")
    assert [s for _, _, s in ops(vet)].count("skipped") == 4
    assert vet.finish().ok
