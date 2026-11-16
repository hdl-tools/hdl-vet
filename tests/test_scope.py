"""Phase 5d: a declared scope. Specification and acceptance tests.

The owner's request: a script names a module, changes that module and what is
below it, and any change outside is an error.

    def apply(vet):
        with vet.scope("blk_a"):
            a = vet.module("blk_a")
            a.add_wire("dbg")
            ...
        # after the block the whole design can be edited again

The design is still loaded from the real top and sign-off still judges the whole
design at the end of the run. A scope adds refusals; it removes no check.

Owner decisions (2026-11-16)

- The scope is declared with a with-block, like `vet.group()`.
- It covers the module and everything instantiated beneath it.
- The scope module's own ports and parameters may be changed. The parent's
  instantiation is outside the scope, so a new input stays unconnected there and
  sign-off fails unless a later script, outside the scope, connects it.
  (Consequence, found in review: removing a port or parameter of the scope
  module also edits its instantiation in the parent, so it is refused by S3.)
- Inside a scope, `punch_port` and `propagate_param` start at the scope module:
  the path begins with its name, and the port or parameter ends in its own list.

RULES

S1  `Vet.scope(module)` is a context manager. On entry what is queued is applied
    and loaded, as for a group. `module` must be a module with at least one
    instance under the top in the design as loaded on entry; the errors are
    those of `vet.module` ("no module ...", "... is not elaborated under the
    top ..."). The top module is allowed (the scope then covers everything).
S2  Inside. An instance path is inside when it is the path of an instance of the
    scope module or lies below one, in the design as loaded on entry. A module is
    inside when it has at least one instance and every instance of it is inside.
    The design on entry decides, not the baseline: an instance that an earlier
    operation of the run added, replaced or removed counts as it is now
    (`Vet.module_paths` and `.paths` report the baseline and do not decide). A module that is also
    instantiated elsewhere is not inside, although some of its instances are.
S3  Refused at the call. Inside the block, an operation that would edit the text
    of a module that is not inside is refused with a VetError that names the
    operation, the module, the scope and one instance path outside the scope.
    The scope is checked before anything else about the operation, so the
    message is the same whether or not the operation would otherwise succeed.
    An operation that would otherwise be skipped is refused too.
    Nothing is queued; the block goes on if the script catches the error. This
    holds for every operation of `Module` and `Instance` that edits (an
    `Instance` operation edits the module the instance is written in;
    `remove_port` and `remove_param` also edit every module that instantiates
    the module, and the message names that module), also on an object obtained
    before the block. Reading is not restricted: `vet.module`,
    `.info`, `.paths`, `.instance` work for any module.
S4  `vet.splice` inside the block is refused at the call unless the whole range
    lies in the text of one module that is inside, from its `module` keyword to
    the end of its `endmodule`. Text outside every module (a comment above the
    module, a package, an include file) is outside. A splice whose new text
    contains a `define, `undef or `undefineall directive is refused as well: a
    macro reaches every file that is read later.
S5  `vet.add_file`, `vet.expect` and `vet.permit` are not restricted.
S6  `punch_port` and `propagate_param` inside the block take a path that starts
    with the scope module's name: `to="blk_a.u_c0"`. They do what they do from
    the top, with the scope module in the place of the top: the port or parameter
    is added down the path and ends in the scope module's own list; nothing above
    it is edited. A path that does not start with the scope module's name is
    refused, and the message names the scope module as the start; the scope
    module alone is refused with "... must name an instance below ...". Both
    operations are refused when the scope module is elaborated more than once;
    the message says "elaborated N times" and names the scope.
S7  At the end of the block the design is re-loaded and compared with the design
    on entry. If any byte outside the text of the modules that are inside has
    changed, or the structure has changed at an instance path that is not inside
    (also a path that did not exist on entry), a VetError that says "outside the
    scope" is raised and everything done in the block is taken back: text, queue,
    log, notes, expectations. This is the backstop for what S3 and S4 cannot see.
S8  An exception that leaves the block takes back everything done in the block,
    as for a group.
S9  A scope cannot be opened inside a scope or inside a group ("scope: not
    allowed inside a scope", "scope: not allowed inside a group"). A group can
    be opened inside a scope; the scope itself is not a group, so operations in
    it are compared one by one as outside it. `vet.check`, `vet.save` and
    `vet.finish` are refused inside a scope ("check: not allowed inside a
    scope", and so on).
S10 After the block there is no scope. Operations inside the block are recorded
    in `vet.log` as they are outside it. Nothing about a scope is kept in a saved
    session.

LEFT OPEN

- Hierarchical operations in a scope whose module is elaborated more than once
  are refused (S6); making them work is left open.
- A hierarchical reference written inside the scope that reaches outside it
  (`force top.u_b.m = ...` in a splice) is not detected. `defparam` and `bind`
  aimed outside are caught by S7.
- A scope named by an instance path instead of a module.
- The starter script written by `vet init` does not mention scopes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.cli import main
from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session

CELL = (
    "module {name} #(\n"
    "  parameter int W = 1\n"
    ") (\n"
    "  input  logic clk,\n"
    "  input  logic d,\n"
    "  output logic q\n"
    ");\n"
    "  always_ff @(posedge clk) q <= d;\n"
    "endmodule\n"
)
CELLS = "// cells\n" + CELL.format(name="cell_a") + "\n" + CELL.format(name="cell_b")
BUF = "module buf_cell (\n  input  logic a,\n  output logic y\n);\n  assign y = a;\nendmodule\n"
SHARED = (
    "module shared (\n"
    "  input  logic clk,\n"
    "  input  logic d,\n"
    "  output logic q\n"
    ");\n"
    "  logic r;\n"
    "  always_ff @(posedge clk) r <= d;\n"
    "  buf_cell u_buf (.a(r), .y(q));\n"
    "endmodule\n"
)
BLK = (
    "// block\n"
    "module {name} (\n"
    "  input  logic clk,\n"
    "  input  logic d,\n"
    "  output logic q\n"
    ");\n"
    "  logic m;\n"
    "  {cell} {inst} (.clk(clk), .d(d), .q(m));\n"
    "  shared u_s (.clk(clk), .d(m), .q(q));\n"
    "endmodule\n"
)
TOP = (
    "module top (\n"
    "  input  logic clk,\n"
    "  input  logic a,\n"
    "  input  logic b,\n"
    "  output logic ya,\n"
    "  output logic yb\n"
    ");\n"
    "  blk_a u_a (.clk(clk), .d(a), .q(ya));\n"
    "  blk_b u_b (.clk(clk), .d(b), .q(yb));\n"
    "endmodule\n"
)
FILES = {
    "cells": CELLS,
    "buf_cell": BUF,
    "shared": SHARED,
    "blk_a": BLK.format(name="blk_a", cell="cell_a", inst="u_c0"),
    "blk_b": BLK.format(name="blk_b", cell="cell_b", inst="u_c"),
    "top": TOP,
}


class Design:
    """top -> u_a (blk_a) -> u_c0 (cell_a), u_s (shared -> u_buf);  top -> u_b (blk_b) -> u_c (cell_b), u_s (shared).

    `cell_a` and `cell_b` are in one file, `rtl/cells.sv`.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        (self.root / "rtl").mkdir(parents=True)
        for name, text in FILES.items():
            (self.root / "rtl" / f"{name}.sv").write_bytes(text.encode())
        self.filelist = self.root / "design.f"
        self.filelist.write_bytes("".join(f"rtl/{name}.sv\n" for name in FILES).encode())

    def path(self, name: str) -> Path:
        return self.root / "rtl" / f"{name}.sv"

    def changed(self, vet: Vet) -> set[str]:
        """The files whose text differs from the input, with everything queued applied."""
        vet.session.refresh()
        return {n for n in FILES if vet.text(self.path(n)) != self.path(n).read_bytes()}


@pytest.fixture
def design(tmp_path: Path) -> Design:
    return Design(tmp_path / "design")


@pytest.fixture
def vet(design: Design) -> Vet:
    return Vet(Session(design.filelist, top="top"))


def statuses(vet: Vet) -> list[tuple[str, str, str]]:
    return [(r.op, r.target, r.status) for r in vet.log]


# --- S1, S2, S10: what a scope allows -----------------------------------------------------


def test_the_design_is_clean(vet):
    assert vet.finish().ok


def test_edits_inside_the_scope_are_applied(vet, design):
    with vet.scope("blk_a"):
        vet.module("blk_a").add_wire("dbg")
        vet.module("cell_a").add_wire("t")
    assert design.changed(vet) == {"blk_a", "cells"}
    assert statuses(vet) == [("add_wire", "blk_a", "applied"), ("add_wire", "cell_a", "applied")]
    result = vet.finish()
    assert result.ok
    assert "logic dbg;" in result.diff


def test_an_instance_operation_inside_the_scope_is_applied(vet, design):
    with vet.scope("blk_a"):
        vet.module("blk_a").instance("u_c0").set_param("W", 2)
    assert design.changed(vet) == {"blk_a"}
    assert vet.finish().ok


def test_after_the_block_the_whole_design_can_be_edited(vet, design):
    with vet.scope("blk_a"):
        vet.module("blk_a").add_wire("dbg")
    vet.module("blk_b").add_wire("dbg")
    vet.module("top").add_wire("dbg")
    assert design.changed(vet) == {"blk_a", "blk_b", "top"}
    assert vet.finish().ok


def test_the_top_module_as_scope_covers_everything(vet, design):
    with vet.scope("top"):
        vet.module("top").add_wire("t0")
        vet.module("shared", instances=2).add_wire("t1")
        vet.module("cell_b").add_wire("t2")
    assert design.changed(vet) == {"top", "shared", "cells"}
    assert vet.finish().ok


def test_a_scope_on_a_module_with_several_instances(vet, design):
    """`shared` is elaborated twice; both instances, and `buf_cell` below them, are inside."""
    with vet.scope("shared"):
        vet.module("shared", instances=2).add_wire("t")
        vet.module("buf_cell", instances=2).add_wire("t")
        with pytest.raises(VetError, match="scope"):
            vet.module("blk_a").add_wire("t")
    assert design.changed(vet) == {"shared", "buf_cell"}
    assert vet.finish().ok


@pytest.mark.parametrize("name, needle", [("nope", "no module 'nope'"), ("blk_x", "no module 'blk_x'")])
def test_the_scope_module_must_exist(vet, name, needle):
    with pytest.raises(VetError, match=needle):
        with vet.scope(name):
            pass


def test_the_scope_module_must_be_elaborated(design, tmp_path):
    lone = design.root / "rtl" / "lone.sv"
    lone.write_bytes(b"module lone (input logic a, output logic y);\n  assign y = a;\nendmodule\n")
    design.filelist.write_bytes(design.filelist.read_bytes() + b"rtl/lone.sv\n")
    vet = Vet(Session(design.filelist, top="top"))
    with pytest.raises(VetError, match="not elaborated under the top"):
        with vet.scope("lone"):
            pass


def test_edits_queued_before_the_block_are_kept(vet, design):
    vet.module("blk_b").add_wire("pre")
    with vet.scope("blk_a"):
        vet.module("blk_a").add_wire("dbg")
    assert design.changed(vet) == {"blk_a", "blk_b"}
    assert vet.finish().ok


# --- S3: refused at the call ----------------------------------------------------------------


@pytest.mark.parametrize(
    "module, outside",
    [
        ("blk_b", "top.u_b"),
        ("top", "top"),
        ("cell_b", "top.u_b.u_c"),
        ("shared", "top.u_b.u_s"),
        ("buf_cell", "top.u_b.u_s.u_buf"),
    ],
)
def test_editing_a_module_outside_the_scope_is_refused(vet, design, module, outside):
    with vet.scope("blk_a"):
        with pytest.raises(VetError) as e:
            vet.module(module).add_wire("x")
    message = str(e.value)
    assert "add_wire" in message
    assert repr(module) in message
    assert "scope 'blk_a'" in message
    assert outside in message
    assert design.changed(vet) == set()
    assert statuses(vet) == []


def test_every_kind_of_edit_outside_the_scope_is_refused(vet, design):
    with vet.scope("blk_a"):
        top = vet.module("top")
        blk_b = vet.module("blk_b")
        for edit in (
            lambda: top.add_port("p", "input"),
            lambda: top.add_param("P", 1),
            lambda: top.add_assign("ya", "a"),
            lambda: top.remove_port("ya"),
            lambda: top.remove_instance("u_b"),
            lambda: top.instance("u_a").connect("d", "b", was="a"),
            lambda: top.instance("u_a").disconnect("q"),
            lambda: blk_b.remove_wire("m"),
            lambda: blk_b.instance("u_c").set_param("W", 2),
            lambda: blk_b.instance("u_c").replace_module("cell_a"),
            lambda: blk_b.add_param("Q", 1),
            lambda: blk_b.add_wire("m"),  # would be skipped: the wire is there
            lambda: blk_b.remove_wire("nope"),  # would be skipped: there is no such wire
            lambda: blk_b.remove_assign("q"),
            lambda: vet.module("cell_b").remove_param("W"),
        ):
            with pytest.raises(VetError, match="scope 'blk_a'"):
                edit()
    assert design.changed(vet) == set()
    assert vet.finish().ok


def test_the_instance_of_the_scope_module_is_written_outside(vet, design):
    """`u_a` is an instance of blk_a, but its text is in `top`: connecting it is an edit of `top`."""
    with vet.scope("blk_a"):
        with pytest.raises(VetError, match="scope 'blk_a'") as e:
            vet.module("top").instance("u_a").connect("d", "b", was="a")
        assert "'top'" in str(e.value)
    assert design.changed(vet) == set()


def test_reading_outside_the_scope_is_allowed(vet):
    with vet.scope("blk_a"):
        blk_b = vet.module("blk_b")
        assert blk_b.paths == ["top.u_b"]
        assert [i.name for i in blk_b.info.instances] == ["u_c", "u_s"]
        assert blk_b.instance("u_c").target == "blk_b.u_c"
        assert vet.module("top", instances=1).name == "top"


def test_an_object_from_before_the_block_is_held_to_the_scope(vet, design):
    blk_b = vet.module("blk_b")
    u_c = blk_b.instance("u_c")
    blk_a = vet.module("blk_a")
    with vet.scope("blk_a"):
        with pytest.raises(VetError, match="scope 'blk_a'"):
            blk_b.add_wire("x")
        with pytest.raises(VetError, match="scope 'blk_a'"):
            u_c.set_param("W", 2)
        blk_a.add_wire("ok")
    blk_b.add_wire("x")
    assert design.changed(vet) == {"blk_a", "blk_b"}


def test_the_block_goes_on_after_a_refusal_the_script_catches(vet, design):
    with vet.scope("blk_a"):
        vet.module("blk_a").add_wire("first")
        with pytest.raises(VetError):
            vet.module("top").add_wire("x")
        vet.module("blk_a").add_wire("second")
    assert design.changed(vet) == {"blk_a"}
    assert [s for s in statuses(vet) if s[2] == "applied"] == [
        ("add_wire", "blk_a", "applied"),
        ("add_wire", "blk_a", "applied"),
    ]
    assert vet.finish().ok


def test_the_scope_module_s_own_ports_may_change(vet, design):
    """The owner's decision: allowed; sign-off fails until something outside the scope connects the port."""
    with vet.scope("blk_a"):
        vet.module("blk_a").add_port("scan_i", "input")
    assert design.changed(vet) == {"blk_a"}
    assert [d.code for d in vet.check().new_diagnostics] == ["UnconnectedInputPort"]
    vet.module("top").instance("u_a").connect("scan_i", "1'b0")
    assert vet.finish().ok


def test_inside_is_judged_on_the_design_at_entry(vet, design):
    """An earlier operation put a second `cell_a` under blk_b; the baseline does not know it."""
    vet.module("blk_b").instance("u_c").replace_module("cell_a")
    with vet.scope("blk_a"):
        with pytest.raises(VetError, match="scope 'blk_a'") as e:
            vet.module("cell_a").add_wire("t")
        assert "top.u_b.u_c" in str(e.value)
        cells = design.path("cells")
        at = vet.text(cells).index(b"  always_ff")
        with pytest.raises(VetError, match="scope 'blk_a'"):
            vet.splice(cells, at, at, b"  // x\n")
    assert design.changed(vet) == {"blk_b"}


def test_a_module_that_lost_its_last_instance_cannot_be_a_scope(vet, design):
    with vet.group():
        vet.module("top").remove_instance("u_b")
        vet.module("top").remove_port("yb")
        vet.module("top").remove_port("b")
    with pytest.raises(VetError, match="not elaborated under the top"):
        with vet.scope("blk_b"):
            pass
    with vet.scope("blk_a"):
        with pytest.raises(VetError, match="scope 'blk_a'"):
            vet.module("cell_b").add_wire("t")


def test_removing_a_port_of_the_scope_module_edits_its_parent(vet, design):
    with vet.scope("blk_a"):
        with pytest.raises(VetError, match="scope 'blk_a'") as e:
            vet.module("blk_a").remove_port("d")
        assert "remove_port" in str(e.value)
        assert "'top'" in str(e.value)
    assert design.changed(vet) == set()
    assert statuses(vet) == []


# --- S4, S5: splice, add_file ---------------------------------------------------------------


def test_a_splice_inside_a_module_of_the_scope(vet, design):
    cells = design.path("cells")
    data = cells.read_bytes()
    in_a = data.index(b"  always_ff")
    in_b = data.index(b"  always_ff", in_a + 1)
    start_b = data.index(b"module cell_b")
    with vet.scope("blk_a"):
        vet.splice(cells, in_a, in_a, b"  // inside cell_a\n")
        for start, end in ((in_b, in_b), (0, 0), (start_b - 1, start_b - 1), (in_a, in_b)):
            with pytest.raises(VetError, match="scope 'blk_a'"):
                vet.splice(cells, start, end, b"// x\n")
        with pytest.raises(VetError, match="scope 'blk_a'"):
            vet.splice(design.path("top"), 0, 0, b"// x\n")
        with pytest.raises(VetError, match="scope 'blk_a'"):
            vet.splice(design.path("blk_a"), 0, 0, b"// above the module\n")
    assert design.changed(vet) == {"cells"}
    assert vet.text(cells).count(b"// inside cell_a") == 1
    assert vet.finish().ok


@pytest.mark.parametrize("directive", [b"`define X 1", b"  `undef X", b"`undefineall"])
def test_a_splice_that_defines_a_macro_is_refused(vet, design, directive):
    cells = design.path("cells")
    at = cells.read_bytes().index(b"  always_ff")
    with vet.scope("blk_a"):
        with pytest.raises(VetError, match="scope 'blk_a'") as e:
            vet.splice(cells, at, at, directive + b"\n")
        assert "`" in str(e.value)
        vet.splice(cells, at, at, b"  // `define in a comment is text\n")
    assert design.changed(vet) == {"cells"}
    assert vet.finish().ok


def test_a_splice_may_cover_the_whole_module_text(vet, design):
    blk_a = design.path("blk_a")
    data = blk_a.read_bytes()
    start = data.index(b"module blk_a")
    end = data.index(b"endmodule") + len(b"endmodule")
    with vet.scope("blk_a"):
        with pytest.raises(VetError, match="scope 'blk_a'"):
            vet.splice(blk_a, start, end + 1, data[start : end + 1])
        vet.splice(blk_a, start, end, data[start:end].replace(b"logic m;", b"logic m; // note"))
    assert design.changed(vet) == {"blk_a"}
    assert vet.finish().ok


def test_add_file_and_a_swap_inside_the_scope(vet, design, tmp_path):
    cell = tmp_path / "cells" / "new_cell.sv"
    cell.parent.mkdir()
    cell.write_bytes(CELL.format(name="new_cell").encode())
    with vet.scope("blk_a"):
        vet.add_file(cell)
        vet.module("blk_a").instance("u_c0").replace_module("new_cell")
    assert design.changed(vet) == {"blk_a"}
    result = vet.finish()
    assert result.ok
    assert "new_cell u_c0" in result.diff


# --- S6: hierarchical operations start at the scope module ------------------------------------


def test_punch_port_starts_at_the_scope_module(vet, design):
    with vet.scope("blk_a"):
        vet.punch_port("scan_i", "input", to="blk_a.u_c0")
    assert design.changed(vet) == {"blk_a", "cells"}
    assert statuses(vet) == [
        ("add_port", "blk_a", "applied"),
        ("add_port", "cell_a", "applied"),
        ("connect", "blk_a.u_c0", "applied"),
    ]
    blk_a = vet.text(design.path("blk_a")).decode()
    assert "scan_i" in blk_a.split(");")[0]
    assert ".scan_i(scan_i)" in blk_a
    assert [d.code for d in vet.check().new_diagnostics] == ["UnconnectedInputPort"]
    vet.module("top").instance("u_a").connect("scan_i", "1'b0")
    assert vet.finish().ok


def test_punch_port_two_levels_below_the_scope_module(vet, design):
    """`shared` is also used under blk_b, so a path through it leaves the scope."""
    with vet.scope("blk_a"):
        with pytest.raises(VetError, match="scope 'blk_a'"):
            vet.punch_port("scan_i", "input", to="blk_a.u_s.u_buf", all_instances=True)
    assert design.changed(vet) == set()


def test_propagate_param_starts_at_the_scope_module(vet, design):
    with vet.scope("blk_a"):
        vet.propagate_param("W", to="blk_a.u_c0")
    assert design.changed(vet) == {"blk_a"}
    assert statuses(vet) == [("add_param", "blk_a", "applied"), ("set_param", "blk_a.u_c0", "applied")]
    blk_a = vet.text(design.path("blk_a")).decode()
    assert "module blk_a #(parameter int W = 1) (" in blk_a
    assert "cell_a #(.W(W)) u_c0" in blk_a
    assert vet.finish().ok


@pytest.mark.parametrize("to", ["top.u_a.u_c0", "u_c0", "blk_b.u_c"])
def test_a_path_that_does_not_start_at_the_scope_module_is_refused(vet, design, to):
    with vet.scope("blk_a"):
        with pytest.raises(VetError, match="'blk_a'") as e:
            vet.punch_port("scan_i", "input", to=to)
        assert "punch_port" in str(e.value)
        with pytest.raises(VetError, match="'blk_a'"):
            vet.propagate_param("W", to=to)
    assert design.changed(vet) == set()
    assert statuses(vet) == []


def test_the_scope_module_itself_is_not_a_path(vet):
    with vet.scope("blk_a"):
        with pytest.raises(VetError, match="must name an instance below"):
            vet.punch_port("scan_i", "input", to="blk_a")


def test_hierarchical_operations_need_a_scope_module_elaborated_once(vet, design):
    with vet.scope("shared"):
        with pytest.raises(VetError, match="elaborated 2 times") as e:
            vet.punch_port("scan_i", "input", to="shared.u_buf")
        assert "scope 'shared'" in str(e.value)
        with pytest.raises(VetError, match="elaborated 2 times"):
            vet.propagate_param("W", to="shared.u_buf")
    assert design.changed(vet) == set()


def test_outside_a_scope_paths_start_at_the_top_as_before(vet, design):
    with vet.scope("blk_a"):
        vet.module("blk_a").add_wire("dbg")
    with pytest.raises(VetError, match="not at the top module"):
        vet.punch_port("scan_i", "input", to="blk_a.u_c0")
    vet.punch_port("scan_i", "input", to="top.u_a.u_c0")
    assert design.changed(vet) == {"top", "blk_a", "cells"}
    assert vet.finish().ok


# --- S7: the comparison at the end of the block -----------------------------------------------


def test_a_change_outside_made_behind_the_operations_is_caught_at_the_end(vet, design):
    """The session is edited directly, which S3 and S4 cannot see."""
    top = design.path("top")
    notes = len(vet.notes)
    with pytest.raises(VetError, match="outside the scope") as e:
        with vet.scope("blk_a"):
            vet.module("blk_a").add_wire("dbg")
            vet.session.splice(top, 0, 0, b"// sneaked in\n", old=b"")
    assert "blk_a" in str(e.value)
    assert "top.sv" in str(e.value)
    assert design.changed(vet) == set()
    assert statuses(vet) == []
    assert len(vet.notes) == notes
    assert vet.finish().ok


def test_a_comment_above_a_module_of_the_scope_is_outside_too(vet, design):
    with pytest.raises(VetError, match="outside the scope"):
        with vet.scope("blk_a"):
            vet.session.splice(design.path("blk_a"), 0, 0, b"// x\n", old=b"")
    assert design.changed(vet) == set()


def test_a_structural_change_outside_the_scope_is_caught_at_the_end(design):
    """With no top named, replacing the last `cell_a` makes `cell_a` a top-level module: a new path, outside."""
    vet = Vet(Session(design.filelist))
    with pytest.raises(VetError, match="outside the scope") as e:
        with vet.scope("blk_a"):
            vet.module("blk_a").instance("u_c0").replace_module("cell_b")
    assert "cell_a" in str(e.value)
    assert design.changed(vet) == set()
    assert statuses(vet) == []
    assert vet.finish().ok


def test_the_same_swap_with_the_top_named_is_inside(vet, design):
    with vet.scope("blk_a"):
        vet.module("blk_a").instance("u_c0").replace_module("cell_b")
    assert design.changed(vet) == {"blk_a"}
    assert vet.finish().ok


def test_expectations_made_in_a_refused_block_are_taken_back(vet, design):
    with pytest.raises(VetError, match="outside the scope"):
        with vet.scope("blk_a"):
            vet.expect("instance-removed", "top.u_b")
            vet.session.splice(design.path("top"), 0, 0, b"// x\n", old=b"")
    assert vet.finish().ok  # an expectation left behind would be reported as unmatched


# --- S8, S9 -------------------------------------------------------------------------------------


def test_an_exception_takes_the_block_back(vet, design):
    vet.module("blk_b").add_wire("kept")
    with pytest.raises(RuntimeError, match="stop"):
        with vet.scope("blk_a"):
            vet.module("blk_a").add_wire("dbg")
            vet.module("cell_a").add_port("p", "input")
            raise RuntimeError("stop")
    assert design.changed(vet) == {"blk_b"}
    assert statuses(vet) == [("add_wire", "blk_b", "applied")]
    assert vet.finish().ok


def test_a_refusal_that_is_not_caught_takes_the_block_back(vet, design):
    with pytest.raises(VetError, match="scope 'blk_a'"):
        with vet.scope("blk_a"):
            vet.module("blk_a").add_wire("dbg")
            vet.module("top").add_wire("x")
    assert design.changed(vet) == set()
    assert statuses(vet) == []


def test_a_scope_cannot_be_nested_or_opened_in_a_group(vet, design):
    with vet.scope("blk_a"):
        with pytest.raises(VetError, match="scope: not allowed inside a scope"):
            with vet.scope("cell_a"):
                pass
        vet.module("blk_a").add_wire("dbg")
    with vet.group():
        with pytest.raises(VetError, match="scope: not allowed inside a group"):
            with vet.scope("blk_a"):
                pass
    assert design.changed(vet) == {"blk_a"}
    assert vet.finish().ok


def test_a_group_inside_a_scope(vet, design):
    with vet.scope("blk_a"):
        with vet.group():
            vet.module("cell_a").add_port("scan_i", "input")
            vet.module("blk_a").instance("u_c0").connect("scan_i", "1'b0")
            with pytest.raises(VetError, match="scope 'blk_a'"):
                vet.module("top").add_wire("x")
    assert design.changed(vet) == {"blk_a", "cells"}
    assert vet.finish().ok


def test_check_save_and_finish_are_refused_inside_a_scope(vet, tmp_path):
    with vet.scope("blk_a"):
        with pytest.raises(VetError, match="check: not allowed inside a scope"):
            vet.check()
        with pytest.raises(VetError, match="save: not allowed inside a scope"):
            vet.save(tmp_path / "s.zip")
        with pytest.raises(VetError, match="finish: not allowed inside a scope"):
            vet.finish()
    assert vet.check().ok
    vet.save(tmp_path / "s.zip")
    assert Vet.restore(tmp_path / "s.zip").finish().ok


def test_a_saved_session_knows_nothing_of_a_scope(vet, design, tmp_path):
    with vet.scope("blk_a"):
        vet.module("blk_a").add_wire("dbg")
    vet.save(tmp_path / "s.zip")
    again = Vet.restore(tmp_path / "s.zip")
    again.module("blk_b").add_wire("dbg")
    assert again.finish().ok


# --- the command line ---------------------------------------------------------------------------

INSIDE = (
    "def apply(vet):\n"
    '    with vet.scope("blk_a"):\n'
    '        vet.module("blk_a").add_wire("dbg")\n'
    '        vet.module("cell_a").add_wire("t")\n'
)
OUTSIDE = 'def apply(vet):\n    with vet.scope("blk_a"):\n        vet.module("blk_b").add_wire("dbg")\n'


def run(*args: object) -> int:
    return main([str(a) for a in args])


def test_two_scripts_each_with_its_own_scope(design, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_text(INSIDE, encoding="utf-8")
    (tmp_path / "b.py").write_text(
        'def apply(vet):\n    with vet.scope("blk_b"):\n        vet.module("blk_b").add_wire("dbg")\n', encoding="utf-8"
    )
    out = tmp_path / "out"
    status = run("apply", "--no-project", "-f", design.filelist, "--top", "top", "-s", "a.py", "-s", "b.py", "-o", out)
    captured = capsys.readouterr()
    assert status == 0, captured.err
    assert "3 applied, 0 skipped" in captured.out
    assert b"logic dbg;" in (out / "rtl" / "blk_a.sv").read_bytes()
    assert b"logic dbg;" in (out / "rtl" / "blk_b.sv").read_bytes()
    assert (out / "rtl" / "top.sv").read_bytes() == design.path("top").read_bytes()


def test_a_script_that_leaves_its_scope_stops_the_run(design, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bad.py").write_text(OUTSIDE, encoding="utf-8")
    out = tmp_path / "out"
    status = run("apply", "--no-project", "-f", design.filelist, "--top", "top", "-s", "bad.py", "-o", out)
    captured = capsys.readouterr()
    assert status == 2
    assert "bad.py" in captured.err
    assert "scope 'blk_a'" in captured.err
    assert "'blk_b'" in captured.err
    assert "Traceback" not in captured.err
    assert not out.exists()
