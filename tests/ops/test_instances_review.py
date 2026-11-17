"""Phase 5 review fixes (spec: tests/ops/test_instances.py; this file refines it).

Found by the adversarial review of the first implementation. Where a rule
below differs from the phase 5 spec, this file wins.

replace_module
    R1  The value check covers kept and renamed overrides too: only
        parameters named in `set_params` are exempt. Values are compared as
        integers when both are integral, whatever their type or width
        (`4` equals `32'd4`); otherwise as text.
    R2  A kept or renamed port must also have the same data type on both
        modules (the snapshot's port type: signedness, struct, unpacked
        dimensions). The refusal names both ports and both types. An unpacked
        port is never reported as "0 bits wide".
    R3  An override list removed whole leaves one blank when nothing but the
        instance name would follow the module name (`m#(.D(3))u(...)` becomes
        `n u(...)`). An instance whose path is gone in the tried design is a
        refusal, not a skipped check.

Dangling check
    D1  Entries are compared with the name of the module they are in, so an
        entry that disappears in one module cannot hide a new one of the same
        name in another module of the same file.
    D2  Partly driven signals. slang says nothing when only some bits of a
        signal lose their driver. For every signal that a removed connection
        or a removed assignment drove (the base name of each target, through
        selects and concatenations), the bits driven after the operation (or
        the group) must cover the bits driven before, unless the signal is
        gone or is reported unused. Otherwise the refusal names the signal.
    D3  Dangling entries located inside an added file never count (an added
        cell may have an output it does not drive).

Added files
    A1  Two added files whose names differ only in case are refused.
    A2  An added file that carries the hdl-vet header is refused, like a
        source of the design.
    A3  The exemption for warnings inside an added file covers only what the
        file showed when it was added: the diagnostics located in it right
        after `add_file` are its baseline. A warning that appears in it later
        (the script instantiated it with a parameter that makes it truncate)
        is new.
    A4  `add_file` is a checked operation like the others (dangling
        comparison, notes). Its messages name the file once.

Removals
    X1  The last item of a list: only the comma after the previous item is
        removed, and the item with the line rule; a comment after that comma
        stays where it is, and a comment after the removed item goes with it
        when the line goes.
    X2  A name that appears as an identifier in text disabled by a
        conditional directive (`ifdef ... `endif) inside the module is refused
        by remove_port, remove_param and remove_wire: the other configuration
        would no longer compile.
    X3  remove_wire and remove_assign refuse a name declared or assigned
        inside a generate block (they used to report "skipped").
    X4  remove_instance drops the post-conditions and the required
        expectations that earlier operations registered for that instance
        (replace_module, connect, disconnect, set_param), so that swapping an
        instance and later removing it signs off.

Groups
    G1  A group inside a group is taken back when an exception leaves it,
        also when the outer group catches the exception and carries on. The
        comparison is still made once, at the end of the outermost group.
    G2  Step 4 of the spec, precisely: edits queued before a refused checked
        operation stay (they are applied, as step 1 says); everything the
        refused operation did is gone.

Known and left open: two operations that cancel each other (add_wire then
remove_wire; a swap and a swap back) still fail sign-off on their stale
expectations; a parameter change that removes a driver in another generate
branch is not seen by the dangling check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.ops import Vet, VetError
from hdl_vet.output import write_tree
from hdl_vet.session import Session
from vetkit import make_design


def design(tmp_path: Path, files: dict[str, str], top: str = "top") -> tuple[Vet, dict[str, Path]]:
    root = tmp_path / "d"
    root.mkdir()
    paths = {}
    for name, text in files.items():
        paths[name] = (root / name).resolve()
        paths[name].write_bytes(text.encode())
    (root / "d.f").write_bytes("".join(f"{n}\n" for n in files if n.endswith(".sv")).encode())
    return Vet(Session(root / "d.f", top=top)), paths


def cell(tmp_path: Path, name: str, text: str, sub: str = "cells") -> Path:
    d = tmp_path / sub
    d.mkdir(exist_ok=True)
    (d / name).write_bytes(text.encode())
    return d / name


def edits(diff: str) -> list[str]:
    return [
        line
        for line in diff.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---")) and "Modified by hdl-vet" not in line
    ]


def untouched(vet: Vet, *paths: Path) -> bool:
    return all(vet.text(p) == p.read_bytes() for p in paths)


OLD_NEW = (
    "module old_m #(parameter int N = 4) (input logic clk, output logic [7:0] q);\n"
    "  assign q = 8'(N);\n"
    "endmodule\n"
    "module narrow_m #(parameter logic [7:0] N = 4) (input logic clk, output logic [7:0] q);\n"
    "  assign q = N;\n"
    "endmodule\n"
    "module unsigned_m #(parameter int unsigned N = 4) (input logic clk, output logic [7:0] q);\n"
    "  assign q = 8'(N);\n"
    "endmodule\n"
)


# --- R1 ------------------------------------------------------------------------------


def test_a_carried_override_that_changes_value_is_refused(tmp_path: Path):
    vet, p = design(
        tmp_path,
        {
            "m.sv": OLD_NEW,
            "top.sv": "module top (input logic clk, output logic [7:0] q);\n"
            "  old_m #(.N(300)) u (.clk(clk), .q(q));\nendmodule\n",
        },
    )
    with pytest.raises(VetError, match="'N'"):
        vet.module("top").instance("u").replace_module("narrow_m")  # 300 does not fit eight bits
    assert vet.log == [] and untouched(vet, p["top.sv"])
    vet.module("top").instance("u").replace_module("narrow_m", set_params={"N": 44})
    r = vet.finish()
    assert r.ok, r.report()
    assert "+  narrow_m #(.N(44)) u (.clk(clk), .q(q));" in edits(r.diff)


def test_the_same_value_in_another_integer_type_is_accepted(tmp_path: Path):
    vet, _ = design(
        tmp_path,
        {
            "m.sv": OLD_NEW,
            "top.sv": "module top (input logic clk, output logic [7:0] q, output logic [7:0] r);\n"
            "  old_m #(.N(3)) u (.clk(clk), .q(q));\n"
            "  old_m u_default (.clk(clk), .q(r));\nendmodule\n",
        },
    )
    vet.module("top").instance("u").replace_module("unsigned_m")  # override 3 -> 32'd3
    vet.module("top").instance("u_default").replace_module("unsigned_m")  # default 4 -> 32'd4
    r = vet.finish()
    assert r.ok, r.report()


# --- R2 ------------------------------------------------------------------------------


TYPES = (
    "module signed_m (input logic signed [7:0] a, output logic [7:0] y);\n  assign y = a;\nendmodule\n"
    "module plain_m (input logic [7:0] a, output logic [7:0] y);\n  assign y = a;\nendmodule\n"
    "module unpacked_m (input logic [7:0] a, output logic y [8]);\n  assign y = '{default: a[0]};\nendmodule\n"
)


def test_a_port_of_another_type_is_refused(tmp_path: Path):
    vet, p = design(
        tmp_path,
        {
            "m.sv": TYPES,
            "top.sv": "module top (input logic signed [7:0] a, output logic [7:0] y);\n"
            "  signed_m u (.a(a), .y(y));\nendmodule\n",
        },
    )
    inst = vet.module("top").instance("u")
    with pytest.raises(VetError, match=r"'a'.*signed") as e:
        inst.replace_module("plain_m")
    assert str(e.value).startswith("replace_module top.u")
    with pytest.raises(VetError, match="'y'") as e:
        inst.replace_module("unpacked_m")
    assert "0 bits" not in str(e.value)
    assert vet.log == [] and untouched(vet, p["top.sv"])
    assert vet.finish().ok


def test_reg_and_logic_are_the_same_port_type(tmp_path: Path):
    """R2 compares data types, and `reg` is `logic` under another keyword: a Verilog-style
    cell with `output reg` must swap in for a module with `output logic`. `bit` stays different."""
    kit = make_design(tmp_path / "in")
    vet = Vet(Session(kit.filelist, top="top"))
    vet.add_file(
        cell(
            tmp_path,
            "reg_leaf.v.sv",
            "module reg_leaf (input clk, input [3:0] d, output reg [3:0] q);\n"
            "  always @(posedge clk) q <= d;\nendmodule\n"
            "module bit_leaf (input clk, input [3:0] d, output bit [3:0] q);\n"
            "  always @(posedge clk) q <= d;\nendmodule\n",
        )
    )
    inst = vet.module("mid").instance("u_l0")
    with pytest.raises(VetError, match=r"'q'.*bit"):
        inst.replace_module("bit_leaf", drop_params=["WIDTH"])
    inst.replace_module("reg_leaf", drop_params=["WIDTH"])
    r = vet.finish()
    assert r.ok, r.report()
    assert "+  reg_leaf u_l0 (.clk(clk), .d(a), .q(y0));" in edits(r.diff)


# --- R3 ------------------------------------------------------------------------------


def test_removing_an_override_list_does_not_glue_names(tmp_path: Path):
    vet, _ = design(
        tmp_path,
        {
            "m.sv": "module oldm #(parameter int D = 1) (input logic clk);\nendmodule\n"
            "module newm (input logic clk);\nendmodule\n",
            "top.sv": "module top (input logic clk);\n  logic spare;\n  oldm#(.D(3))u(.clk(clk));\nendmodule\n",
        },
    )
    with vet.group():
        vet.module("top").instance("u").replace_module("newm", drop_params=["D"])
        vet.module("top").remove_wire("spare")
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == ["-  logic spare;", "-  oldm#(.D(3))u(.clk(clk));", "+  newm u(.clk(clk));"]


# --- D1, D2, D3 ----------------------------------------------------------------------


def test_a_dangling_signal_cannot_hide_behind_its_namesake_in_another_module(tmp_path: Path):
    vet, p = design(
        tmp_path,
        {
            "two.sv": "module m1 (input logic i, output logic o);\n  logic x;\n  assign o = x;\nendmodule\n"
            "module m2 (input logic i, output logic o);\n  logic x;\n  assign x = i;\n  assign o = x;\nendmodule\n",
            "top.sv": "module top (input logic i, output logic a, output logic b);\n"
            "  m1 u1 (.i(i), .o(a));\n  m2 u2 (.i(i), .o(b));\nendmodule\n",
        },
    )
    with pytest.raises(VetError, match="'x'") as e:
        with vet.group():
            vet.module("m1").add_assign("x", "i")
            vet.module("m2").remove_assign("x")
    assert str(e.value).startswith("group:") and "m2" in str(e.value)
    assert vet.log == [] and untouched(vet, p["two.sv"])


BUS = (
    "module nib (input logic clk, input logic [3:0] d, output logic [3:0] q);\n"
    "  always_ff @(posedge clk) q <= d;\n"
    "endmodule\n"
    "module top (input logic clk, input logic [7:0] a, output logic [7:0] y, output logic [3:0] z);\n"
    "  nib u_lo (.clk(clk), .d(a[3:0]), .q(y[3:0]));\n"
    "  nib u_hi (.clk(clk), .d(a[7:4]), .q(y[7:4]));\n"
    "  assign z[1:0] = a[1:0];\n"
    "  assign z[3:2] = a[3:2];\n"
    "endmodule\n"
)


def test_a_partly_undriven_bus_is_refused(tmp_path: Path):
    vet, p = design(tmp_path, {"bus.sv": BUS})
    top = vet.module("top")
    with pytest.raises(VetError, match="'y'") as e:
        top.remove_instance("u_hi")
    assert str(e.value).startswith("remove_instance top.u_hi")
    with pytest.raises(VetError, match="'z'"):
        top.remove_assign("z[1:0]")
    assert vet.log == [] and untouched(vet, p["bus.sv"])
    with vet.group():  # both halves go and the whole bus gets a new driver: covered again
        top.remove_instance("u_hi")
        top.remove_instance("u_lo")
        top.add_assign("y", "a")
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        "-  nib u_lo (.clk(clk), .d(a[3:0]), .q(y[3:0]));",
        "-  nib u_hi (.clk(clk), .d(a[7:4]), .q(y[7:4]));",
        "+  assign y = a;",
    ]


def test_an_undriven_output_inside_an_added_cell_does_not_block_a_swap(tmp_path: Path):
    kit = make_design(tmp_path / "in")
    vet = Vet(Session(kit.filelist, top="top"))
    spare = cell(
        tmp_path,
        "spare_cell.sv",
        "module spare_cell (\n  input  logic       clk,\n  input  logic [3:0] d,\n"
        "  output logic [3:0] q,\n  output logic       spare_o\n);\n"
        "  always_ff @(posedge clk) q <= d;\nendmodule\n",
    )
    vet.add_file(spare)
    vet.module("mid").instance("u_l0").replace_module("spare_cell", drop_params=["WIDTH"], unconnected=["spare_o"])
    r = vet.finish()
    assert r.ok, r.report()


# --- A1 .. A4 ------------------------------------------------------------------------


def test_added_files_differing_only_in_case_are_refused(tmp_path: Path):
    kit = make_design(tmp_path / "in")
    vet = Vet(Session(kit.filelist, top="top"))
    first = cell(tmp_path, "Cell.sv", "module cell_a (input logic clk);\nendmodule\n", sub="x")
    second = cell(tmp_path, "cell.sv", "module cell_b (input logic clk);\nendmodule\n", sub="y")
    vet.add_file(first)
    with pytest.raises(VetError, match="cell.sv") as e:
        vet.add_file(second)
    assert "cell.sv: cell.sv" not in str(e.value)  # the file is named once
    assert list(vet.session.added) == ["hdl_vet_added/Cell.sv"]


def test_an_added_file_with_the_hdl_vet_header_is_refused(tmp_path: Path):
    kit = make_design(tmp_path / "in")
    vet = Vet(Session(kit.filelist, top="top"))
    path = cell(tmp_path, "used.sv", "// Modified by hdl-vet\nmodule used (input logic clk);\nendmodule\n")
    with pytest.raises(VetError, match="header"):
        vet.add_file(path)
    assert vet.session.added == {}


PCELL = (
    "module pcell #(parameter int W = 4) (\n"
    "  input  logic       clk,\n"
    "  input  logic [3:0] d,\n"
    "  output logic [3:0] q\n"
    ");\n"
    "  logic [W-1:0] t;\n"
    "  assign t = d;\n"
    "  always_ff @(posedge clk) q <= 4'(t);\n"
    "endmodule\n"
)


def test_a_warning_the_script_causes_inside_an_added_file_is_new(tmp_path: Path):
    kit = make_design(tmp_path / "in")
    vet = Vet(Session(kit.filelist, top="top"))
    vet.add_file(cell(tmp_path, "pcell.sv", PCELL))
    inst = vet.module("mid").instance("u_l0")
    with pytest.raises(VetError, match="[Tt]runcat"):
        inst.replace_module("pcell", drop_params=["WIDTH"], set_params={"W": 2})  # `t` now drops two bits
    assert untouched(vet, kit.mid)
    inst.replace_module("pcell", drop_params=["WIDTH"])
    r = vet.finish()
    assert r.ok, r.report()


def test_the_written_tree_has_every_added_file(tmp_path: Path):
    kit = make_design(tmp_path / "in")
    vet = Vet(Session(kit.filelist, top="top"))
    vet.add_file(cell(tmp_path, "a_cell.sv", PCELL.replace("pcell", "a_cell")))
    vet.add_file(cell(tmp_path, "b_cell.sv", PCELL.replace("pcell", "b_cell")))
    mid = vet.module("mid")
    mid.instance("u_l0").replace_module("a_cell", drop_params=["WIDTH"])
    mid.instance("u_l1").replace_module("b_cell", drop_params=["WIDTH", "DEPTH"])
    r = vet.finish()
    assert r.ok, r.report()
    out = write_tree(vet.session, r, tmp_path / "out")
    assert sorted(p.name for p in (out / "hdl_vet_added").iterdir()) == ["a_cell.sv", "b_cell.sv"]
    assert (out / "design.f").read_text(encoding="utf-8").splitlines()[-2:] == [
        "hdl_vet_added/a_cell.sv",
        "hdl_vet_added/b_cell.sv",
    ]


# --- X1 .. X4 ------------------------------------------------------------------------


COMMENTS = (
    "module c #(\n"
    "  parameter int A = 1, // first\n"
    "  parameter int B = 2  // second\n"
    ") (\n"
    "  input  logic clk, // clock\n"
    "  input  logic spare // not used\n"
    ");\n"
    "endmodule\n"
    "module top (input logic clk);\n"
    "  c #(\n"
    "    .A(1), // a comment\n"
    "    .B(2) // b comment\n"
    "  ) u (\n"
    "    .clk  (clk),  // the clock\n"
    "    .spare(1'b0)  // tied\n"
    "  );\n"
    "endmodule\n"
)


def test_removing_the_last_item_leaves_the_other_comments_alone(tmp_path: Path):
    vet, _ = design(tmp_path, {"c.sv": COMMENTS})
    c = vet.module("c")
    c.remove_param("B")
    c.remove_port("spare")
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        "-  parameter int A = 1, // first",
        "-  parameter int B = 2  // second",
        "+  parameter int A = 1 // first",
        "-  input  logic clk, // clock",
        "-  input  logic spare // not used",
        "+  input  logic clk // clock",
        "-    .A(1), // a comment",
        "-    .B(2) // b comment",
        "+    .A(1) // a comment",
        "-    .clk  (clk),  // the clock",
        "-    .spare(1'b0)  // tied",
        "+    .clk  (clk)  // the clock",
    ]


IFDEF = (
    "module g (input logic x, input logic spare, output logic y);\n"
    "  logic helper;\n"
    "`ifdef USE_SPARE\n"
    "  assign y = x & spare & helper;\n"
    "`else\n"
    "  assign y = x;\n"
    "`endif\n"
    "endmodule\n"
    "module top (input logic x, output logic y);\n"
    "  g u (.x(x), .spare(1'b1), .y(y));\n"
    "endmodule\n"
)


def test_a_name_used_only_in_disabled_text_is_not_removed(tmp_path: Path):
    vet, p = design(tmp_path, {"g.sv": IFDEF})
    g = vet.module("g")
    with pytest.raises(VetError, match="'spare'") as e:
        g.remove_port("spare")
    assert "USE_SPARE" in str(e.value) or "ifdef" in str(e.value) or "disabled" in str(e.value)
    with pytest.raises(VetError, match="'helper'"):
        g.remove_wire("helper")
    assert vet.log == [] and vet.session.reloads == 0 and untouched(vet, p["g.sv"])


GEN = (
    "module top #(parameter bit ON = 1) (input logic a, output logic y);\n"
    "  if (ON) begin : g\n"
    "    logic inner;\n"
    "    assign inner = a;\n"
    "    assign y = inner;\n"
    "  end\n"
    "endmodule\n"
)


def test_names_inside_a_generate_block_are_refused_not_skipped(tmp_path: Path):
    vet, p = design(tmp_path, {"gen.sv": GEN})
    top = vet.module("top")
    with pytest.raises(VetError, match="generate"):
        top.remove_wire("inner")
    with pytest.raises(VetError, match="generate"):
        top.remove_assign("y")
    assert vet.log == [] and untouched(vet, p["gen.sv"])


def test_an_instance_can_be_edited_and_then_removed(tmp_path: Path):
    kit = make_design(tmp_path / "in")
    vet = Vet(Session(kit.filelist, top="top"))
    top = vet.module("top")
    spare = top.instance("u_spare")
    spare.replace_module("alt_leaf", ports={"d": "din"})
    spare.connect("din", "4'd0", was="spare_d")
    top.remove_instance("u_spare")
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == ["-  leaf u_spare (.clk(clk), .d(spare_d), .q(spare_q));"]
    assert {(c.kind, c.path) for c in r.changes} == {("instance-removed", "top.u_spare")}


# --- G1, G2 --------------------------------------------------------------------------


def test_an_inner_group_is_taken_back_when_the_outer_one_catches(tmp_path: Path):
    kit = make_design(tmp_path / "in")
    vet = Vet(Session(kit.filelist, top="top"))
    with vet.group():
        vet.module("top").add_wire("w_outer")
        try:
            with vet.group():
                vet.module("mid").add_wire("w_inner")
                raise RuntimeError("stop")
        except RuntimeError:
            pass
        vet.module("top").add_wire("w_after")
    assert [(r.op, r.target) for r in vet.log] == [("add_wire", "top"), ("add_wire", "top")]
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == ["+  logic w_after;", "+  logic w_outer;"]
    assert list(r.files) == [kit.top]


def test_edits_queued_before_a_refused_operation_survive(tmp_path: Path):
    kit = make_design(tmp_path / "in")
    vet = Vet(Session(kit.filelist, top="top"))
    vet.module("mid").add_wire("kept")
    with pytest.raises(VetError, match="y0"):
        vet.module("mid").remove_instance("u_l0")
    vet.module("mid").instance("u_l0").connect("d", "~a", was="a")  # fresh offsets after the refusal
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        "-  leaf #(.WIDTH(4)) u_l0 (.clk(clk), .d(a), .q(y0));",
        "+  logic kept;",
        "+  leaf #(.WIDTH(4)) u_l0 (.clk(clk), .d(~a), .q(y0));",
    ]
