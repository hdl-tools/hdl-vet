"""Phase 3: the script API and the first structural operations (`hdl_vet.ops`).

`hdl_vet.ops` imports no pyslang. Scripts receive a `Vet`; the command line
builds `Vet(Session(...))`, runs the scripts, and calls `vet.finish(scripts)`.

    VetError(Exception)        a request that cannot be carried out
    DriftError(VetError)       the design is in neither the "before" nor the
                               "after" state the operation knows

    Vet(session)
        .session
        .module(name, instances=None) -> Module
        .finish(scripts=()) -> Result
        .log -> list[OpRecord]     OpRecord(op, target, status), frozen;
                                   status "applied" | "skipped"; target e.g.
                                   "leaf", "mid.u_l0"
        .splice(...), .expect(...), .text(path), .design     as on Session (raw
                                   escape hatch; kept so phase 2 scripts still run)

Every operation follows the same contract:
    - If the design already shows exactly the requested result, nothing is
      queued and the operation is logged "skipped".
    - If the design is in the expected "before" state, the edit is queued (with
      the `old=` guard), its structural expectations are registered, and a
      post-condition is registered that `finish` evaluates on the final design.
    - Otherwise DriftError, naming what was found.
    - Handles are live: each operation and each model attribute looks the
      design up again, so offsets are never stale. A re-load happens only when
      the lookup depends on something edited since the last load:
        * the list the operation edits or reads: a module's port list,
          parameter list or declarations; an instance's connection list or
          override list (so `connect` after `add_port` on the instantiated
          module re-loads, two `connect`s on different instances do not);
        * for add_assign: anything queued at all (drivers depend on everything);
        * after a raw `vet.splice`: anything in the design (its effect is unknown).
      Session gains `.loaded` (the last loaded Design, never re-loading) and
      `.refresh()` (apply the queue and re-load if anything is queued) for this.
    - A failed post-condition is a string starting with the operation name and
      naming its target, e.g. "connect mid.u_l0 d: expected '~a', found 'y1'".
    - Unknown names raise VetError with up to three suggestions from
      `difflib.get_close_matches(name, candidates, n=3)`, closest first:
      "... 'u_mdi' ...; did you mean 'u_mid'?" or "...; did you mean 'a', 'b'
      or 'c'?". No suggestion text when nothing is close.
    - "Whitespace-insensitive" comparisons remove all whitespace from both sides.
      `was=` values and parameter values are compared as str(value).
    - Skipped operations register neither expectations nor post-conditions.
      Post-conditions are not evaluated when the edited design cannot be loaded.
    - Name-conflict checks (add_port, add_param, add_wire) use the last loaded
      model plus the names this Vet has added to that module since; they do not
      force a re-load on their own.
    - VetError and DriftError are hdl-vet's own errors: the command line reports
      them on stderr, naming the script, without a traceback, and exits with
      status 2. On success its stdout includes "<N> applied, <M> skipped".
    - Text is inserted using the file's own line ending; existing bytes are
      never reformatted.

Module handle  (attributes of the underlying ModuleInfo, e.g. `.path`,
`.body_end`, `.port(name)`, stay available through the handle; `.info` returns
it. Two names differ: `.instance(name)` returns an Instance handle and
`.instances(...)` is the selector below; the plain list is `.info.instances`.)
    vet.module(name, instances=None)
        VetError if unknown. The module must be elaborated at least once under
        the top; `.paths` lists its hierarchical paths (sorted). A module-level
        edit changes every instance, so when len(paths) > 1 the edit operations
        below raise VetError unless `instances` equals len(paths).
    .instance(name) -> Instance              exactly one, else VetError
    .instances(module=None, name=None, *, count) -> list[Instance]
        Filters: `module` exact, `name` glob. `count` is required: VetError
        listing the matches when the number differs. Source order.
    .add_port(name, direction, type="logic", width=None)
        direction "input" | "output" | "inout". Declaration text:
            f"{direction:<6} {type}{range} {name}"
        range: none for width None or 1; " [N-1:0]" computed for an int N > 1
        (" [3:0]"); " [EXPR-1:0]" for a string; a string starting with "[" is
        used as written. Placement, after the last port of an ANSI list:
          - if the closing ")" is the first non-blank character of its line:
            "," right after the last port, and a new line before the ")" line,
            indented like the last port's line;
          - otherwise ", <decl>" right after the last port;
          - an empty list "()" receives the declaration between the parentheses.
        Skipped when a port of that name exists whose own text equals the
        declaration (whitespace-insensitive); DriftError when it exists but
        differs or the name is taken by anything else. VetError for a non-ANSI
        or absent port list, or when the module header (name through its ";")
        contains macro tokens; macros in the body do not matter.
        Requires: port-added on every path. Permits: connection-changed for
        that port on every path.
    .add_param(name, default, type="int")
        Declaration `parameter {type} {name} = {default}`, placed after the last
        parameter by the same rule as ports. A module without a parameter port
        list gets ` #(<decl>)` directly after its name. Skip / drift as for
        ports, comparing the default text. Requires: param-changed on every path.
    .add_wire(name, width=None, type="logic")
        `{indent}{type}{range} {name};` on a new line directly below the line
        that ends the module header. indent is that of the first non-blank body
        line (two spaces if the body is empty). Skipped when a module-level
        net/variable of that name with the same type and range exists;
        DriftError when the name is used by anything else. No structural
        expectation (the snapshot does not see declarations).
    .add_assign(target, expr)
        `{indent}assign {target} = {expr};` on a new line directly above the
        `endmodule` line. `target` is a plain identifier naming a module-level
        net/variable or an output port. Allowed only when slang's driver
        analysis finds no driver for it in the module's analysed body (slang
        analyses one canonical body per shared module, so the adapter must look
        there, not in an arbitrary instance). Skipped when
        the module already has exactly that continuous assignment. VetError when
        the target is unknown, is an input, or already has a driver.

Instance handle  (attributes of the underlying InstanceInfo stay available)
    .paths    hierarchical paths of this instance under every path of its module
    VetError for instances that come from a macro, or that use positional or
    `.*` connections / positional parameter overrides when the operation needs
    to edit that list.
    .connect(port, expr, was=None)
        `port` must exist on the instantiated module. Missing or empty (`.p()`)
        connection: added / filled. Same expression (whitespace-insensitive):
        skipped. Different expression: replaced only when `was` equals it, else
        DriftError quoting the current expression.
        New connection text: `.{port:<W}({expr})`. A name field is a connection's
        name plus the blanks before its "(". W is the common width of the name
        fields of the existing connections when they all have the same width
        (an aligned list), else 0. Placed after the last connection by the same
        rule as ports.
        Filling or replacing keeps everything up to and including "(" and
        rewrites only the expression. An instance with no connections receives
        it between the parentheses.
        Requires: connection-changed for `port` on every path.
    .disconnect(port, was=None)
        Outputs and inouts only: VetError for an input port, because slang
        accepts `.p()` on an input silently and the input would float. The
        message names the instance and port and gives the two ways out, e.g.
            cannot disconnect input port 'd' of mid.u_l1: the input would be
            left floating. Either tie it to a value with
            connect('d', <value>, was='a'), or remove the port from module
            'leaf'.
        (`was=` shows the current expression; it is omitted when the port has
        no connection.)
        Leaves an explicit empty connection, keeping the name field. Skipped when
        already empty or absent. DriftError when `was` is given and differs.
        Requires: connection-changed.
    .set_param(name, value, was=None)
        `name` must be an overridable value parameter of the instantiated module
        (not a localparam or a type parameter). The post-condition compares the
        override's source text, not the resolved value. No override:
        `.{name}({value})` is added after the last override (same placement and
        name-field rule), or ` #(.{name}({value}))` directly after the module
        name when the instance has no `#(...)`. Same value text: skipped.
        Different: replaced only when `was` equals it, else DriftError.
        Permits (does not require): param-changed for `name` on every path,
        since an override equal to the default changes nothing. Knock-on
        changes (port widths) must be declared by the script with vet.expect.

Adapter addition
    `WidthTruncate` joins the always-reported warnings: an inserted assignment
    that silently drops bits (a script written for a 12-bit bus applied after
    the bus grew to 14) must fail sign-off. Baseline occurrences do not matter;
    only new ones fail.

Session / sign-off additions
    Session.permit(kind, path="*", name="*")     allows a change, does not require it
    Expect gains `required: bool = True`; check_changes never reports an
    unrequired Expect as unmatched.
    Session.finish(scripts=(), checks=())        checks: callables(Design) -> str | None,
                                                 evaluated on the final design
    Result gains failed_checks: list[str]; any entry makes ok False and appears
    in report(). When nothing changed, checks still run (on the baseline).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from vetkit import make_design, span

from hdl_vet.ops import DriftError, Vet, VetError
from hdl_vet.session import Session
from hdl_vet.signoff import Change


@pytest.fixture
def kit(tmp_path: Path):
    return make_design(tmp_path)


@pytest.fixture
def vet(kit):
    return Vet(Session(kit.filelist, top="top"))


def edits(diff: str) -> list[str]:
    """The added and removed lines of a diff, without file headers and the hdl-vet header."""
    return [
        line
        for line in diff.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---")) and "Modified by hdl-vet" not in line
    ]


def statuses(vet: Vet) -> list[tuple[str, str, str]]:
    return [(r.op, r.target, r.status) for r in vet.log]


# --- selectors -------------------------------------------------------------------


def test_unknown_names_suggest_a_close_match(vet):
    with pytest.raises(VetError, match=r"'mdi'.*did you mean 'mid'"):
        vet.module("mdi")
    with pytest.raises(VetError, match=r"'u_mdi'.*did you mean 'u_mid'"):
        vet.module("top").instance("u_mdi")
    with pytest.raises(VetError, match="zzz") as e:
        vet.module("zzz")
    assert "did you mean" not in str(e.value)


def test_paths(vet):
    assert vet.module("top").paths == ["top"]
    assert vet.module("leaf").paths == ["top.u_mid.u_l0", "top.u_mid.u_l1", "top.u_spare"]
    assert vet.module("mid").instance("u_l1").paths == ["top.u_mid.u_l1"]


def test_module_not_elaborated_under_the_top_is_refused(vet):
    with pytest.raises(VetError, match="alt_leaf"):
        vet.module("alt_leaf")


def test_instances_selector_needs_the_right_count(vet):
    top, mid = vet.module("top"), vet.module("mid")
    assert [i.name for i in mid.instances(module="leaf", count=2)] == ["u_l0", "u_l1"]
    assert [i.name for i in top.instances(module="leaf", count=1)] == ["u_spare"]
    assert [i.name for i in top.instances(name="u_*", count=2)] == ["u_mid", "u_spare"]
    assert [i.name for i in mid.instances(module="leaf", name="*1", count=1)] == ["u_l1"]
    assert top.instances(module="nope", count=0) == []
    with pytest.raises(VetError, match=r"u_l0.*u_l1"):
        mid.instances(module="leaf", count=4)
    with pytest.raises(TypeError):
        mid.instances(module="leaf")


def test_handles_expose_the_model(vet, kit):
    mid = vet.module("mid")
    assert mid.path == kit.mid and mid.info.name == "mid"
    assert mid.instance("u_l1").param_override("DEPTH").value == "3"


def test_module_level_edit_of_a_shared_module_needs_the_instance_count(vet):
    with pytest.raises(VetError, match="3"):
        vet.module("leaf").add_port("scan_en", "input")
    with pytest.raises(VetError, match="3"):
        vet.module("leaf", instances=2).add_port("scan_en", "input")
    assert vet.log == []


# --- add_port ----------------------------------------------------------------------


def test_add_port_to_a_multi_line_list(vet, kit):
    vet.module("top").add_port("dft_en", "input")
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        "-  output logic [3:0] y1",
        "+  output logic [3:0] y1,",
        "+  input  logic dft_en",
    ]
    assert r.changes == [Change("port-added", "top", "dft_en", None, "input 1")]
    assert statuses(vet) == [("add_port", "top", "applied")]


@pytest.mark.parametrize(
    ("kwargs", "decl"),
    [
        ({"direction": "input", "width": 4}, "input  logic [3:0] p"),
        ({"direction": "input", "width": 1}, "input  logic p"),
        ({"direction": "output", "width": "4"}, "output logic [4-1:0] p"),
        ({"direction": "input", "width": "[1:0]"}, "input  logic [1:0] p"),
        ({"direction": "inout", "type": "wire"}, "inout  wire p"),
    ],
)
def test_add_port_declaration_forms(vet, kwargs, decl):
    vet.module("top").add_port("p", **kwargs)
    r = vet.finish()
    assert edits(r.diff)[-1] == "+  " + decl


def test_add_port_to_single_line_and_empty_lists(tmp_path: Path):
    (tmp_path / "t.sv").write_bytes(
        b"module one(input logic a);\nendmodule\n"
        b"module none ();\nendmodule\n"
        b"module t(input logic a);\n  one u_o (.a(a));\n  none u_n ();\nendmodule\n"
    )
    (tmp_path / "t.f").write_bytes(b"t.sv\n")
    vet = Vet(Session(tmp_path / "t.f", top="t"))
    vet.module("one").add_port("b", "output")
    vet.module("none").add_port("c", "output", width=2)
    r = vet.finish()
    assert edits(r.diff) == [
        "-module one(input logic a);",
        "+module one(input logic a, output logic b);",
        "-module none ();",
        "+module none (output logic [1:0] c);",
    ]


def test_add_port_skip_and_drift(vet):
    top = vet.module("top")
    top.add_port("clk", "input")  # exists, same declaration apart from spacing
    assert statuses(vet) == [("add_port", "top", "skipped")]
    with pytest.raises(DriftError, match="clk"):
        top.add_port("clk", "output")
    with pytest.raises(DriftError, match="a"):
        top.add_port("a", "input")  # exists as a 4-bit port
    with pytest.raises(DriftError, match="spare_d"):
        top.add_port("spare_d", "input")  # name used by a net
    with pytest.raises(VetError, match="direction"):
        top.add_port("x", "in")
    r = vet.finish()
    assert r.ok and r.files == {}


def test_add_port_refuses_non_ansi_and_macro_headers(tmp_path: Path):
    (tmp_path / "t.sv").write_bytes(
        b"`define W 4\n"
        b"module old(a);\n  input a;\nendmodule\n"
        b"module mac(input logic [`W-1:0] a);\nendmodule\n"
        b"module bare;\nendmodule\n"
        b"module t(input logic [3:0] a);\n  old u_o (.a(a[0]));\n  mac u_m (.a(a));\n  bare u_b ();\nendmodule\n"
    )
    (tmp_path / "t.f").write_bytes(b"t.sv\n")
    vet = Vet(Session(tmp_path / "t.f", top="t"))
    for name in ("old", "mac", "bare"):
        with pytest.raises(VetError, match=name):
            vet.module(name).add_port("x", "input")


def test_add_port_keeps_crlf(tmp_path: Path):
    kit = make_design(tmp_path, newline="\r\n")
    vet = Vet(Session(kit.filelist, top="top"))
    vet.module("top").add_port("dft_en", "input")
    r = vet.finish()
    assert r.ok, r.report()
    out = r.files[kit.top]
    assert b"  output logic [3:0] y1,\r\n  input  logic dft_en\r\n);\r\n" in out
    assert out.count(b"\n") == out.count(b"\r\n")


# --- add_port + connect across the hierarchy ---------------------------------------------


def test_new_port_on_a_shared_module_connected_everywhere(vet, kit):
    vet.module("leaf", instances=3).add_port("scan_en", "input")
    for inst in vet.module("mid").instances(module="leaf", count=2):
        inst.connect("scan_en", "1'b0")
    vet.module("top").instance("u_spare").connect("scan_en", "1'b0")
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        "-  output logic [WIDTH-1:0] q",
        "+  output logic [WIDTH-1:0] q,",
        "+  input  logic scan_en",
        "-  leaf #(.WIDTH(4)) u_l0 (.clk(clk), .d(a), .q(y0));",
        "+  leaf #(.WIDTH(4)) u_l0 (.clk(clk), .d(a), .q(y0), .scan_en(1'b0));",
        "-    .q   (y1)",
        "+    .q   (y1),",
        "+    .scan_en(1'b0)",
        "-  leaf u_spare (.clk(clk), .d(spare_d), .q(spare_q));",
        "+  leaf u_spare (.clk(clk), .d(spare_d), .q(spare_q), .scan_en(1'b0));",
    ]
    assert [c.kind for c in r.changes].count("port-added") == 3
    assert vet.session.reloads == 2  # one to see the new port, one to sign off
    assert [s for _, _, s in statuses(vet)] == ["applied"] * 4


def test_new_input_left_unconnected_fails_sign_off(vet):
    vet.module("leaf", instances=3).add_port("scan_en", "input")
    r = vet.finish()
    assert not r.ok
    assert [d.code for d in r.new_diagnostics] == ["UnconnectedInputPort"] * 3
    assert r.unexpected_changes == [] and r.unmatched_expects == [] and r.failed_checks == []


# --- connect / disconnect ----------------------------------------------------------------


def test_connect_skip_drift_and_replace(vet, kit):
    u_l0 = vet.module("mid").instance("u_l0")
    u_l1 = vet.module("mid").instance("u_l1")
    u_l0.connect("d", " a ")
    assert statuses(vet) == [("connect", "mid.u_l0", "skipped")]
    with pytest.raises(DriftError, match="'a'"):
        u_l0.connect("d", "~a")
    with pytest.raises(DriftError):
        u_l0.connect("d", "~a", was="y1")
    u_l0.connect("d", "~a", was="a")
    u_l1.connect("d", "~a", was="a")
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        "-  leaf #(.WIDTH(4)) u_l0 (.clk(clk), .d(a), .q(y0));",
        "+  leaf #(.WIDTH(4)) u_l0 (.clk(clk), .d(~a), .q(y0));",
        "-    .d   (a),",
        "+    .d   (~a),",
    ]
    assert r.changes == [
        Change("connection-changed", "top.u_mid.u_l0", "d", "a", "~a"),
        Change("connection-changed", "top.u_mid.u_l1", "d", "a", "~a"),
    ]


def test_connect_unknown_port_and_bad_signal(vet):
    u_l0 = vet.module("mid").instance("u_l0")
    with pytest.raises(VetError, match=r"'dd'.*did you mean 'd'"):
        u_l0.connect("dd", "a")
    u_l0.connect("d", "a_typo", was="a")
    r = vet.finish()
    assert not r.ok
    assert "ImplicitNet" in [d.code for d in r.new_diagnostics]


def test_connect_fills_empty_and_missing_connections(tmp_path: Path):
    (tmp_path / "t.sv").write_bytes(
        b"module leaf(input logic a, input logic b, output logic q);\n  assign q = a & b;\nendmodule\n"
        b"module t(input logic x, input logic a, output logic q0, output logic q1, output logic q2);\n"
        b"  leaf u_e (.a (x), .b (), .q (q0));\n"
        b"  leaf u_m (\n      .a(x),\n      .q(q1)\n  );\n"
        b"  leaf u_i (.a, .b(x), .q(q2));\n"
        b"endmodule\n"
    )
    (tmp_path / "t.f").write_bytes(b"t.sv\n")
    kit_top = tmp_path / "t.sv"
    base = Session(tmp_path / "t.f", top="t")
    vet = Vet(base)
    t = vet.module("t")
    t.instance("u_e").connect("b", "x")
    t.instance("u_m").connect("b", "x")
    with pytest.raises(DriftError):
        t.instance("u_i").connect("a", "x")  # `.a` means a signal named a, which is not x
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        "-  leaf u_e (.a (x), .b (), .q (q0));",
        "+  leaf u_e (.a (x), .b (x), .q (q0));",
        "-      .q(q1)",
        "+      .q(q1),",
        "+      .b(x)",
    ]
    assert kit_top.read_bytes().count(b".b(x)") == 1  # input untouched


def test_connect_refuses_positional_wildcard_and_macro_instances(tmp_path: Path):
    (tmp_path / "t.sv").write_bytes(
        b"`define LEAF leaf\n"
        b"module leaf(input logic a, input logic b);\nendmodule\n"
        b"module t(input logic a, input logic b);\n"
        b"  leaf u_pos (a, b);\n  leaf u_star (.*);\n  `LEAF u_mac (.a(a), .b(b));\n"
        b"endmodule\n"
    )
    (tmp_path / "t.f").write_bytes(b"t.sv\n")
    vet = Vet(Session(tmp_path / "t.f", top="t"))
    for name in ("u_pos", "u_star", "u_mac"):
        with pytest.raises(VetError, match=name):
            vet.module("t").instance(name).connect("b", "a", was="b")


def test_disconnect(vet):
    u_l1 = vet.module("mid").instance("u_l1")
    with pytest.raises(DriftError):
        u_l1.disconnect("q", was="y0")
    u_l1.disconnect("q", was="y1")
    r = vet.finish()
    assert edits(r.diff) == ["-    .q   (y1)", "+    .q   ()"]
    assert r.changes == [Change("connection-changed", "top.u_mid.u_l1", "q", "y1", "")]
    # The operation did what was asked, but `y1` of mid is still read by top and now
    # has no driver: sign-off refuses that (tests/test_dangling_signoff.py).
    assert r.unexpected_changes == [] and r.new_diagnostics == [] and r.failed_checks == []
    assert [d.message for d in r.dangling] == ["undriven output port 'y1'"]
    assert not r.ok


def test_disconnect_of_an_output_nobody_reads_signs_off(vet):
    vet.module("top").instance("u_spare").disconnect("q", was="spare_q")
    r = vet.finish()
    assert edits(r.diff) == [
        "-  leaf u_spare (.clk(clk), .d(spare_d), .q(spare_q));",
        "+  leaf u_spare (.clk(clk), .d(spare_d), .q());",
    ]
    assert r.ok, r.report()


def test_disconnect_refuses_inputs(vet):
    # `.d()` on an input is legal and silent in slang, so it would pass sign-off
    # while leaving an input floating. Tie it off with connect(..., was=...) instead.
    with pytest.raises(VetError, match="input") as e:
        vet.module("mid").instance("u_l1").disconnect("d")
    message = str(e.value)
    # The error tells the author the two acceptable ways out.
    assert "'d'" in message and "u_l1" in message
    assert "tie it to a value" in message and "connect('d', <value>, was='a')" in message
    assert "remove the port" in message
    assert vet.log == []


def test_set_param_refuses_localparams(tmp_path: Path):
    (tmp_path / "t.sv").write_bytes(
        b"module leaf #(parameter int W = 2, localparam int L = 5) (input logic a);\nendmodule\n"
        b"module t(input logic a);\n  leaf u (.a(a));\nendmodule\n"
    )
    (tmp_path / "t.f").write_bytes(b"t.sv\n")
    vet = Vet(Session(tmp_path / "t.f", top="t"))
    with pytest.raises(VetError, match="L"):
        vet.module("t").instance("u").set_param("L", 8)
    vet.module("t").instance("u").set_param("W", 4)
    assert vet.finish().ok


def test_disconnect_already_empty_is_skipped(tmp_path: Path):
    (tmp_path / "t.sv").write_bytes(
        b"module leaf(input logic a, output logic q);\n  assign q = a;\nendmodule\n"
        b"module t(input logic a);\n  leaf u (.a(a), .q());\nendmodule\n"
    )
    (tmp_path / "t.f").write_bytes(b"t.sv\n")
    vet = Vet(Session(tmp_path / "t.f", top="t"))
    vet.module("t").instance("u").disconnect("q")
    assert statuses(vet) == [("disconnect", "t.u", "skipped")]
    assert vet.finish().files == {}


# --- set_param / add_param -----------------------------------------------------------------


def test_set_param_replace_add_and_new_list(vet):
    mid = vet.module("mid")
    u_l1 = mid.instance("u_l1")
    u_l1.set_param("DEPTH", 3)
    assert statuses(vet) == [("set_param", "mid.u_l1", "skipped")]
    with pytest.raises(DriftError, match="3"):
        u_l1.set_param("DEPTH", 5)
    with pytest.raises(VetError, match=r"'WIDHT'.*did you mean 'WIDTH'"):
        u_l1.set_param("WIDHT", 8)
    u_l1.set_param("DEPTH", 5, was=3)
    mid.instance("u_l0").set_param("DEPTH", 4)
    vet.module("top").instance("u_spare").set_param("DEPTH", 2)  # equal to the default
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        # adjacent lines share one diff hunk: removals first, then additions
        "-  leaf #(.WIDTH(4)) u_l0 (.clk(clk), .d(a), .q(y0));",
        "-  leaf #(.WIDTH(4), .DEPTH(3)) u_l1 (",
        "+  leaf #(.WIDTH(4), .DEPTH(4)) u_l0 (.clk(clk), .d(a), .q(y0));",
        "+  leaf #(.WIDTH(4), .DEPTH(5)) u_l1 (",
        "-  leaf u_spare (.clk(clk), .d(spare_d), .q(spare_q));",
        "+  leaf #(.DEPTH(2)) u_spare (.clk(clk), .d(spare_d), .q(spare_q));",
    ]
    assert r.changes == [
        Change("param-changed", "top.u_mid.u_l0", "DEPTH", "2", "4"),
        Change("param-changed", "top.u_mid.u_l1", "DEPTH", "3", "5"),
    ]


def test_set_param_knock_on_changes_must_be_declared(vet):
    vet.module("mid").instance("u_l0").set_param("WIDTH", 8, was=4)
    r = vet.finish()
    assert not r.ok
    assert {(c.kind, c.name) for c in r.unexpected_changes} == {("port-changed", "d"), ("port-changed", "q")}


def test_add_param(vet):
    vet.module("leaf", instances=3).add_param("EXTRA", 7)
    vet.module("mid").add_param("P", "1'b1", type="bit")
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        "-  parameter int DEPTH = 2",
        "+  parameter int DEPTH = 2,",
        "+  parameter int EXTRA = 7",
        "-module mid (",
        "+module mid #(parameter bit P = 1'b1) (",
    ]
    assert len([c for c in r.changes if c.kind == "param-changed" and c.name == "EXTRA"]) == 3


def test_add_param_skip_and_drift(vet):
    leaf = vet.module("leaf", instances=3)
    leaf.add_param("DEPTH", 2)
    assert statuses(vet) == [("add_param", "leaf", "skipped")]
    with pytest.raises(DriftError, match="DEPTH"):
        leaf.add_param("DEPTH", 9)
    with pytest.raises(DriftError, match="clk"):
        leaf.add_param("clk", 1)


# --- add_wire / add_assign --------------------------------------------------------------


def test_add_wire_and_assign(vet):
    top = vet.module("top")
    top.add_wire("dbg", width=4)
    top.add_wire("flag")
    top.add_assign("dbg", "a & spare_d")
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        "+  logic [3:0] dbg;",
        "+  logic flag;",
        "+  assign dbg = a & spare_d;",
    ]
    lines = r.files[top.path].decode().splitlines()
    assert lines[lines.index(");") + 1 : lines.index(");") + 4] == [
        "  logic [3:0] dbg;",
        "  logic flag;",
        "  logic [3:0] spare_d;",
    ]
    assert lines[-2:] == ["  assign dbg = a & spare_d;", "endmodule"]
    assert r.changes == []


def test_add_wire_skip_and_drift(vet):
    top = vet.module("top")
    top.add_wire("spare_d", width=4)
    assert statuses(vet) == [("add_wire", "top", "skipped")]
    for name in ("spare_d", "a", "u_mid"):
        with pytest.raises(DriftError, match=name):
            top.add_wire(name)


def test_truncating_assign_fails_sign_off(vet):
    top = vet.module("top")
    top.add_wire("narrow", width=2)
    top.add_assign("narrow", "a")  # a is 4 bits
    r = vet.finish()
    assert not r.ok
    assert [d.code for d in r.new_diagnostics] == ["WidthTruncate"]


def test_new_output_port_driven_by_an_assign(vet):
    top = vet.module("top")
    top.add_port("dbg_o", "output", width=4)
    top.add_assign("dbg_o", "spare_d")
    r = vet.finish()
    assert r.ok, r.report()
    assert r.changes == [Change("port-added", "top", "dbg_o", None, "output 4")]


def test_add_assign_only_on_undriven_targets(vet):
    top = vet.module("top")
    top.add_assign("spare_d", "a")  # exactly the existing assignment
    assert statuses(vet) == [("add_assign", "top", "skipped")]
    with pytest.raises(VetError, match="spare_d"):
        top.add_assign("spare_d", "~a")  # driven by the existing assign
    with pytest.raises(VetError, match="y0"):
        top.add_assign("y0", "a")  # driven by u_mid
    with pytest.raises(VetError, match="'a'"):
        top.add_assign("a", "y0")  # an input
    with pytest.raises(VetError, match="nope"):
        top.add_assign("nope", "a")
    with pytest.raises(VetError):
        top.add_assign("y0[0]", "a")  # not a plain identifier
    assert vet.finish().files == {}


# --- post-conditions fire on a wrong result -----------------------------------------------


def corrupt(vet: Vet, path: Path, old: bytes, new: bytes) -> None:
    vet.design  # apply what is queued so the text shows the operation's result
    start, end = span(vet.text(path), old)
    vet.splice(path, start, end, new, old=old)


def test_check_fires_when_a_connect_is_undone(vet, kit):
    vet.module("mid").instance("u_l0").connect("d", "~a", was="a")
    corrupt(vet, kit.mid, b".d(~a)", b".d(y1)")
    r = vet.finish()
    assert not r.ok
    assert len(r.failed_checks) == 1
    assert "connect" in r.failed_checks[0] and "u_l0" in r.failed_checks[0]
    assert r.failed_checks[0] in r.report()


def test_check_fires_when_a_new_port_changes_direction(vet, kit):
    vet.module("top").add_port("dft_en", "input")
    corrupt(vet, kit.top, b"input  logic dft_en", b"output logic dft_en")
    r = vet.finish()
    assert not r.ok
    assert len(r.failed_checks) == 1 and "add_port" in r.failed_checks[0]


def test_check_fires_when_a_wire_or_assign_disappears(vet, kit):
    top = vet.module("top")
    top.add_wire("dbg", width=4)
    top.add_assign("dbg", "a")
    corrupt(vet, kit.top, b"  assign dbg = a;\n", b"")
    r = vet.finish()
    assert [c.split()[0] for c in r.failed_checks] == ["add_assign"]

    vet2 = Vet(Session(kit.filelist, top="top"))
    vet2.module("top").add_wire("dbg", width=4)
    corrupt(vet2, kit.top, b"  logic [3:0] dbg;\n", b"")
    assert [c.split()[0] for c in vet2.finish().failed_checks] == ["add_wire"]


def test_check_fires_when_a_parameter_value_is_wrong(vet, kit):
    vet.module("mid").instance("u_l1").set_param("DEPTH", 5, was=3)
    corrupt(vet, kit.mid, b".DEPTH(5)", b".DEPTH(6)")
    r = vet.finish()
    assert not r.ok
    assert [c.split()[0] for c in r.failed_checks] == ["set_param"]


def test_command_line_runs_operations_and_reports_drift(tmp_path: Path, capsys):
    from hdl_vet.cli import main

    kit = make_design(tmp_path / "in")  # the output tree must lie outside the input tree

    good = tmp_path / "good.py"
    good.write_text(
        "def apply(vet):\n"
        "    vet.module('top').add_port('dft_en', 'input')\n"
        "    vet.module('mid').instance('u_l1').set_param('DEPTH', 3)\n"
    )
    out = tmp_path / "out"
    assert main(["apply", "-f", str(kit.filelist), "-s", str(good), "-o", str(out), "--top", "top"]) == 0
    assert b"  input  logic dft_en\n" in (out / "rtl" / "top.sv").read_bytes()
    stdout = capsys.readouterr().out
    assert "1 applied" in stdout and "1 skipped" in stdout

    drift = tmp_path / "drift.py"
    drift.write_text("def apply(vet):\n    vet.module('mid').instance('u_l1').set_param('DEPTH', 9)\n")
    out2 = tmp_path / "out2"
    assert main(["apply", "-f", str(kit.filelist), "-s", str(drift), "-o", str(out2), "--top", "top"]) == 2
    err = capsys.readouterr().err
    assert "DEPTH" in err and "drift.py" in err and "Traceback" not in err
    assert not out2.exists()


def test_unrequired_expectation_and_checks_on_session(kit):
    s = Session(kit.filelist, top="top")
    s.permit("param-changed", "top.*", "DEPTH")
    r = s.finish(checks=[lambda design: None, lambda design: "custom check failed: x"])
    assert not r.ok
    assert r.unmatched_expects == [] and r.failed_checks == ["custom check failed: x"]
    assert Session(kit.filelist, top="top").finish().failed_checks == []
