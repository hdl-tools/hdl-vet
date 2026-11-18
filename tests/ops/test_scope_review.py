"""Phase 5d, declared scope: tests from the adversarial review of the implementation.

The base specification is the docstring of `tests/ops/test_scope.py`. The review
(about 120 probes) found one escape and one traceback; these rules close them and
settle three smaller points.

R1  Directives in a splice (replaces the last sentence of S4). Inside a scope, a
    splice is refused when its new text contains one of these compiler
    directives outside a comment and outside a string literal:
        `include `define `undef `undefineall `celldefine `endcelldefine `line
        `default_nettype `unconnected_drive `nounconnected_drive `timescale
        `resetall `pragma `begin_keywords `end_keywords
    Each of them reaches text outside the module: an `include can pull in a
    header that redefines a macro an outside module uses, and neither the byte
    nor the structure comparison of S7 sees that. A macro use (`WIDTH) and
    `ifdef `ifndef `elsif `else `endif are not refused. The message names the
    directive and the scope.
R2  A script must close what it opens. When `apply` returns with a scope or a
    group still open, `vet apply` stops with exit status 2 and a message that
    names the script and says it left a scope (with its module) or a group
    open. Nothing is written, there is no traceback, and the next script does
    not run.
R3  (Wording of S1.) The scope module must be one `vet.module` accepts: a module
    of the input design. A module that entered the design during the run, from
    an added file, cannot be a scope; its text is never edited anyway.
R4  The comparison at the end of the block takes the block back whatever goes
    wrong in it, not only when the design cannot be loaded.
R5  When the module that is outside is the top module, the message says "the
    top module" instead of giving its path as an instance.

R6  References that reach outside (asked for by the owner after the review). At
    the end of the block, a module that is inside must not name, by a
    hierarchical path, a signal, task or function of an instance that is outside
    the scope, unless the design on entry already had that reference in that
    module. It does not matter how the text got there (a splice, the expression
    of `connect` or `add_assign`) or what it does with the name (`force`,
    `release`, an assignment, a read, a call). The error says "outside the
    scope" and names the module and the path as the design resolves it; the
    block is taken back, as in S7. A hierarchical path that ends at or below an
    instance of the scope module is fine. Outside a scope nothing is checked.

From the adversarial review of R6 (about 100 probes; 40 of 46 escape attempts and
all 23 legitimate uses were handled correctly before these rules):

R7  A name that goes through the module's own interface port (`bus.sig`,
    `bus.get()`, also when the port is passed down to a module below) is a use
    of the port, not a reference that reaches outside, although the interface
    instance is in the parent.
R8  References are counted in the text of a module: for a module and a target,
    the largest count found in one instance of the module. One more instance
    of a module that already had the reference is not a new reference.
R9  At the end of the block the modules that are inside are determined again:
    a module that came into the scope during the block (from an added file, or
    one nobody instantiated before) is held to R6 with a count of zero on
    entry.
R10 A reference belongs to the module whose text holds it. A name inside a
    generate block belongs to the module that has the block, and a name in an
    instance below a generate block belongs to that instance's module, once.
    The port connections of an instance array are seen like those of an
    instance.

Limits of R6, stated: a name slang does not elaborate (an unused sequence or
property) is not seen; a name that is illegal anyway (unresolved, or in a
constant such as a parameter override) is left to sign-off; the message names
one reference when a block adds several.

LEFT OPEN (added to the list of test_scope.py)

- After an earlier operation of the run replaced an instance, a hierarchical
  operation still resolves its path against the input design, inside a scope as
  outside one; sign-off catches the result. A refusal message can name an
  instance path of the input design that no longer exists.
- A script that drops an open `with` object without leaving the block (possible
  only by calling `__enter__` by hand) is taken back silently when Python
  collects it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.cli import main
from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session
from no_comparison import no_comparison  # noqa: F401
from scope_design import CELL, FILES, Design, statuses


@pytest.fixture
def design(tmp_path: Path) -> Design:
    return Design(tmp_path / "design")


@pytest.fixture
def vet(design: Design) -> Vet:
    return Vet(Session(design.filelist, top="top"))


def in_cell_a(design: Design) -> tuple[Path, int]:
    cells = design.path("cells")
    return cells, cells.read_bytes().index(b"  always_ff")


# --- R1 ---------------------------------------------------------------------------------------


def test_an_include_spliced_into_the_scope_cannot_change_logic_outside(tmp_path: Path):
    """The escape the review found: the header is already read by the design, so sign-off sees no new file."""
    design = Design(tmp_path / "design")
    rtl = design.root / "rtl"
    (rtl / "defs.svh").write_bytes(b"`define CELL_B_EXPR d\n")
    (rtl / "alt.svh").write_bytes(b"`undef CELL_B_EXPR\n`define CELL_B_EXPR ~d\n")
    cells = design.path("cells")
    text = cells.read_bytes().decode()
    head, tail = text.split("module cell_b")
    cells.write_bytes(
        ('`include "defs.svh"\n' + head + "module cell_b" + tail.replace("q <= d;", "q <= `CELL_B_EXPR;")).encode()
    )
    top = design.path("top")
    top.write_bytes(top.read_bytes() + b'`include "alt.svh"\n')
    design.filelist.write_bytes(b"+incdir+rtl\n" + design.filelist.read_bytes())

    vet = Vet(Session(design.filelist, top="top"))
    assert vet.check().ok
    at = cells.read_bytes().index(b"  always_ff")
    with vet.scope("blk_a"):
        with pytest.raises(VetError, match="scope 'blk_a'") as e:
            vet.splice(cells, at, at, b'  `include "alt.svh"\n', waiver="set-up of the test")
        assert "`include" in str(e.value)
    assert vet.text(cells) == cells.read_bytes()
    assert vet.finish().ok


@pytest.mark.parametrize(
    "text",
    [
        b'`include "x.svh"',
        b"`define X 1",
        b"  `undef X",
        b"`undefineall",
        b"`celldefine",
        b"`endcelldefine",
        b'`line 1 "x.sv" 0',
        b"`default_nettype none",
        b"`unconnected_drive pull1",
        b"`nounconnected_drive",
        b"`timescale 1ns/1ps",
        b"`resetall",
        b"`pragma protect",
        b'`begin_keywords "1800-2017"',
        b"`end_keywords",
        b"  logic t; `define LATER 1",
    ],
)
def test_a_directive_that_reaches_outside_the_module_is_refused(vet, design, text):
    cells, at = in_cell_a(design)
    directive = text[text.index(b"`") :].split()[0].decode()
    with vet.scope("blk_a"):
        with pytest.raises(VetError, match="scope 'blk_a'") as e:
            vet.splice(cells, at, at, text + b"\n", waiver="set-up of the test")
        assert directive in str(e.value)
    assert design.changed(vet) == set()


@pytest.mark.parametrize(
    "text",
    [
        b"  // `define in a line comment",
        b"  /* `include in a\n     block comment */",
        b'  initial $display("`define in a string");',
        b"  `ifdef SOME_MACRO\n  logic t;\n  `else\n  logic u;\n  `endif",
        b"  `ifndef SOME_MACRO\n  logic t;\n  `elsif OTHER\n  logic u;\n  `endif",
        b"  logic defined_x, include_y;",
    ],
)
def test_what_is_not_such_a_directive_is_accepted(vet, design, text):
    cells, at = in_cell_a(design)
    with vet.scope("blk_a"):
        vet.splice(cells, at, at, text + b"\n", waiver="set-up of the test")
    assert design.changed(vet) == {"cells"}
    assert vet.finish().ok


def test_a_macro_use_is_accepted(tmp_path: Path):
    design = Design(tmp_path / "design")
    cells = design.path("cells")
    cells.write_bytes(b"`define ONE 1'b1\n" + cells.read_bytes())
    vet = Vet(Session(design.filelist, top="top"))
    at = cells.read_bytes().index(b"  always_ff")
    with vet.scope("blk_a"):
        vet.splice(cells, at, at, b"  logic t;\n  assign t = `ONE;\n", waiver="set-up of the test")
    assert vet.finish().ok


def test_outside_a_scope_a_splice_may_hold_any_directive(vet, design):
    """R1 is a rule of the scope; the raw splice is as free as before without one."""
    cells, at = in_cell_a(design)
    vet.splice(cells, at, at, b"`define X 1\n", waiver="set-up of the test")
    assert b"`define X 1" in vet.check().files[cells]


# --- R2 ---------------------------------------------------------------------------------------

OPEN_SCOPE = (
    "KEEP = []\n"
    "\n"
    "\n"
    "def apply(vet):\n"
    '    block = vet.scope("blk_a")\n'
    "    KEEP.append(block)\n"
    "    block.__enter__()\n"
    '    vet.module("blk_a").add_wire("dbg")\n'
)
OPEN_GROUP = OPEN_SCOPE.replace('vet.scope("blk_a")', "vet.group()")
NEXT = 'def apply(vet):\n    vet.module("blk_b").add_wire("after")\n'


def run(*args: object) -> int:
    return main([str(a) for a in args])


@pytest.mark.parametrize(
    "script, needles",
    [(OPEN_SCOPE, ["scope 'blk_a'", "open"]), (OPEN_GROUP, ["group", "open"])],
    ids=["scope", "group"],
)
@pytest.mark.parametrize("mode", ["-o", "--dry-run", "--save"])
def test_a_script_that_leaves_a_block_open_stops_the_run(design, tmp_path, capsys, monkeypatch, script, needles, mode):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "open.py").write_text(script, encoding="utf-8")
    (tmp_path / "next.py").write_text(NEXT.replace("after", "later"), encoding="utf-8")
    out = tmp_path / "out"
    target = {"-o": ["-o", out], "--dry-run": ["--dry-run"], "--save": ["--save", tmp_path / "s.zip"]}[mode]
    status = run(
        "apply", "--no-project", "-f", design.filelist, "--top", "top", "-s", "open.py", "-s", "next.py", *target
    )
    captured = capsys.readouterr()
    assert status == 2, captured
    assert "open.py" in captured.err
    for needle in needles:
        assert needle in captured.err
    assert "Traceback" not in captured.err
    assert "later" not in captured.out
    assert not out.exists()
    assert not (tmp_path / "s.zip").exists()


def test_a_script_that_closes_its_blocks_is_not_affected(design, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ok.py").write_text(
        "def apply(vet):\n"
        '    with vet.scope("blk_a"):\n'
        "        with vet.group():\n"
        '            vet.module("blk_a").add_wire("dbg")\n'
        "            return\n",
        encoding="utf-8",
    )
    (tmp_path / "next.py").write_text(NEXT.replace("after", "later"), encoding="utf-8")
    status = run(
        "apply", "--no-project", "-f", design.filelist, "--top", "top", "-s", "ok.py", "-s", "next.py", "--dry-run"
    )
    captured = capsys.readouterr()
    assert status == 0, captured.err
    assert "2 applied, 0 skipped" in captured.out + captured.err
    assert "logic dbg;" in captured.out
    assert "logic later;" in captured.out


# --- R3 ---------------------------------------------------------------------------------------


def test_a_module_from_an_added_file_cannot_be_a_scope(vet, design, tmp_path):
    new = tmp_path / "cells" / "blk_c.sv"
    new.parent.mkdir()
    new.write_bytes(FILES["blk_b"].replace("module blk_b", "module blk_c").encode())
    vet.add_file(new)
    vet.module("top").instance("u_b").replace_module("blk_c")
    assert vet.check().ok
    with pytest.raises(VetError, match="blk_c"):
        with vet.scope("blk_c"):
            pass
    with pytest.raises(VetError, match="not elaborated under the top"):
        with vet.scope("blk_b"):
            pass
    assert vet.finish().ok
    assert CELL  # the cell template is shared with test_scope.py


# --- R4 ---------------------------------------------------------------------------------------


def test_any_failure_at_the_end_of_the_block_takes_the_block_back(vet, design):
    refresh = vet.session.refresh

    def fails_once() -> None:
        vet.session.refresh = refresh  # type: ignore[method-assign]
        raise RuntimeError("the load broke")

    with pytest.raises(RuntimeError, match="the load broke"):
        with vet.scope("blk_a"):
            vet.module("blk_a").add_wire("dbg")
            vet.module("cell_a").add_wire("t")
            vet.session.refresh()  # the operations are loaded; the next load is the one at the end
            vet.session.refresh = fails_once  # type: ignore[method-assign]
    assert design.changed(vet) == set()
    assert statuses(vet) == []
    vet.module("blk_b").add_wire("dbg")  # no scope is left behind
    with vet.scope("blk_a"):
        vet.module("blk_a").add_wire("dbg")
    assert design.changed(vet) == {"blk_a", "blk_b"}
    assert vet.finish().ok


# --- R5 ---------------------------------------------------------------------------------------


def test_the_top_module_is_called_the_top_module(vet):
    with vet.scope("blk_a"):
        with pytest.raises(VetError, match="scope 'blk_a'") as e:
            vet.module("top").add_wire("x")
    assert "the top module" in str(e.value)
    assert "'top'" in str(e.value)


# --- R6 ---------------------------------------------------------------------------------------

EXTRA_B = (
    b"  task automatic poke;\n"
    b"  endtask\n"
    b"  function automatic logic twice(input logic x);\n"
    b"    return x;\n"
    b"  endfunction\n"
    b"endmodule"
)


def into_blk_a(design: Design, vet: Vet, text: bytes) -> None:
    blk_a = design.path("blk_a")
    at = vet.text(blk_a).index(b"endmodule")
    vet.splice(blk_a, at, at, text, waiver="set-up of the test")


@pytest.mark.parametrize(
    "text, target",
    [
        (b"  initial force top.u_b.m = 1'b0;\n", "top.u_b.m"),
        (b"  logic peek;\n  assign peek = top.u_b.m;\n", "top.u_b.m"),
        (b"  initial begin\n    force top.u_b.u_s.r = 1'b1;\n    release top.u_b.u_s.r;\n  end\n", "top.u_b.u_s.r"),
        (b"  initial top.u_b.poke();\n", "top.u_b.poke"),
        (b"  logic peek;\n  assign peek = top.u_b.twice(d);\n", "top.u_b.twice"),
    ],
    ids=["force", "read", "release", "task", "function"],
)
def test_a_reference_that_reaches_outside_the_scope_is_refused(tmp_path: Path, text, target):
    design = Design(tmp_path / "design")
    blk_b = design.path("blk_b")
    blk_b.write_bytes(blk_b.read_bytes().replace(b"endmodule", EXTRA_B))
    vet = Vet(Session(design.filelist, top="top"))
    into_blk_a(design, vet, text)
    assert vet.check().ok  # legal SystemVerilog: without a scope nothing objects
    vet = Vet(Session(design.filelist, top="top"))
    with pytest.raises(VetError, match="outside the scope") as e:
        with vet.scope("blk_a"):
            vet.module("blk_a").add_wire("dbg")
            into_blk_a(design, vet, text)
    assert target in str(e.value)
    assert "'blk_a'" in str(e.value)
    assert design.changed(vet) == set()
    assert statuses(vet) == []
    assert vet.finish().ok


def test_an_operation_s_expression_that_reaches_outside_is_refused(vet, design):
    with pytest.raises(VetError, match="outside the scope") as e:
        with vet.scope("blk_a"):
            blk_a = vet.module("blk_a")
            blk_a.add_wire("peek")
            blk_a.add_assign("peek", "top.u_b.m")
    assert "top.u_b.m" in str(e.value)
    with pytest.raises(VetError, match="outside the scope"):
        with vet.scope("blk_a"):
            vet.module("blk_a").instance("u_c0").connect("d", "top.u_b.m", was="d")
    assert design.changed(vet) == set()
    assert statuses(vet) == []
    assert vet.finish().ok


@pytest.mark.parametrize(
    "text",
    [
        b"  logic peek;\n  assign peek = u_c0.q;\n",
        b"  logic peek;\n  assign peek = top.u_a.u_s.r;\n",
        b"  logic peek;\n  assign peek = blk_a.m;\n",
        b"  initial force u_s.r = 1'b0;\n",
    ],
    ids=["downward", "from-the-top", "by-module-name", "force-inside"],
)
def test_a_reference_that_stays_inside_the_scope_is_accepted(vet, design, text):
    with vet.scope("blk_a"):
        into_blk_a(design, vet, text)
    assert design.changed(vet) == {"blk_a"}
    assert vet.finish().ok


def test_a_reference_the_design_already_had_is_not_counted(tmp_path: Path):
    design = Design(tmp_path / "design")
    blk_a = design.path("blk_a")
    blk_a.write_bytes(blk_a.read_bytes().replace(b"endmodule", b"  logic old;\n  assign old = top.u_b.m;\nendmodule"))
    vet = Vet(Session(design.filelist, top="top"))
    with vet.scope("blk_a"):
        vet.module("blk_a").add_wire("dbg")
    with pytest.raises(VetError, match="outside the scope") as e:
        with vet.scope("blk_a"):
            into_blk_a(design, vet, b"  logic more;\n  assign more = top.u_b.m;\n")
    assert "top.u_b.m" in str(e.value)
    with pytest.raises(VetError, match="outside the scope"):
        with vet.scope("blk_a"):
            into_blk_a(design, vet, b"  logic other;\n  assign other = top.u_b.u_s.r;\n")
    assert b"logic dbg;" in vet.text(blk_a)
    assert b"logic more;" not in vet.text(blk_a)
    assert vet.finish().ok


def test_a_scope_with_two_instances_and_a_reference_between_them(vet, design):
    """`shared` is at top.u_a.u_s and top.u_b.u_s; `top.u_a.m` is in blk_a, which is outside the scope."""
    shared = design.path("shared")
    at = shared.read_bytes().index(b"endmodule")
    with pytest.raises(VetError, match="outside the scope") as e:
        with vet.scope("shared"):
            vet.splice(shared, at, at, b"  logic peek;\n  assign peek = top.u_a.m;\n", waiver="set-up of the test")
    assert "top.u_a.m" in str(e.value)
    with vet.scope("shared"):
        vet.splice(shared, at, at, b"  logic peek;\n  assign peek = top.u_a.u_s.r;\n", waiver="set-up of the test")
    assert design.changed(vet) == {"shared"}
    assert vet.finish().ok


def test_outside_a_scope_references_are_not_checked(vet, design):
    blk_a = vet.module("blk_a")
    blk_a.add_wire("peek")
    blk_a.add_assign("peek", "top.u_b.m")
    into_blk_a(design, vet, b"  initial force top.u_b.m = 1'b0;\n")
    assert vet.finish().ok


def test_a_later_block_is_not_blamed_for_a_reference_made_before_it(vet, design):
    """The reference was written outside a scope, so it is part of the design the next block starts from."""
    into_blk_a(design, vet, b"  initial force top.u_b.m = 1'b0;\n")
    with vet.scope("blk_a"):
        vet.module("blk_a").add_wire("dbg")
    assert vet.finish().ok


# --- R7 ---------------------------------------------------------------------------------------

MY_IF = (
    "interface my_if;\n"
    "  logic sig;\n"
    "  assign sig = 1'b0;\n"
    "  function automatic logic get();\n"
    "    return sig;\n"
    "  endfunction\n"
    "  task automatic do_it();\n"
    "  endtask\n"
    "  modport slave (input sig, import get, import do_it);\n"
    "endinterface\n"
)


def with_interface(root: Path) -> Design:
    """blk_a has an interface port `bus`, passed down to cell_a as `cb`; the interface instance is in `top`."""
    design = Design(root)
    (design.root / "rtl" / "my_if.sv").write_bytes(MY_IF.encode())
    design.filelist.write_bytes(b"rtl/my_if.sv\n" + design.filelist.read_bytes())
    for name, old, new in (
        ("cells", "  output logic q\n);", "  output logic q,\n  my_if.slave cb\n);"),
        ("blk_a", "  output logic q\n);", "  output logic q,\n  my_if.slave bus\n);"),
        ("blk_a", ".q(m));", ".q(m), .cb(bus));"),
        (
            "top",
            "  blk_a u_a (.clk(clk), .d(a), .q(ya));",
            "  my_if u_if ();\n  blk_a u_a (.clk(clk), .d(a), .q(ya), .bus(u_if));",
        ),
    ):
        path = design.path(name)
        text = path.read_bytes().decode()
        assert old in text, (name, old)
        path.write_bytes(text.replace(old, new, 1).encode())
    return design


@pytest.mark.parametrize(
    "module, text",
    [
        ("blk_a", b"  logic p;\n  assign p = bus.sig;\n"),
        ("blk_a", b"  logic p;\n  assign p = bus.get();\n"),
        ("blk_a", b"  initial bus.do_it();\n"),
        ("cells", b"  logic p;\n  assign p = cb.sig;\n"),
    ],
    ids=["signal", "function", "task", "passed-down"],
)
def test_a_use_of_an_interface_port_is_accepted(tmp_path: Path, module, text):
    design = with_interface(tmp_path / "design")
    vet = Vet(Session(design.filelist, top="top"))
    assert vet.check().ok
    path = design.path(module)
    at = vet.text(path).index(b"endmodule")
    with vet.scope("blk_a"):
        vet.splice(path, at, at, text, waiver="set-up of the test")
    assert design.changed(vet) == {module}
    assert vet.finish().ok


def test_a_reference_outside_is_still_refused_in_a_design_with_interfaces(tmp_path: Path):
    design = with_interface(tmp_path / "design")
    vet = Vet(Session(design.filelist, top="top"))
    for text, target in (
        (b"  logic p;\n  assign p = top.u_if.sig;\n", "top.u_if.sig"),
        (b"  logic p;\n  assign p = top.u_b.m;\n", "top.u_b.m"),
    ):
        with pytest.raises(VetError, match="outside the scope") as e:
            with vet.scope("blk_a"):
                into_blk_a(design, vet, text)
        assert target in str(e.value)
    assert design.changed(vet) == set()


# --- R8, R9, R10 --------------------------------------------------------------------------------

SPY = b"  logic spy;\n  assign spy = top.u_b.m;\nendmodule"


def spying_cell_a(root: Path, blk_a_cell: str | None = None) -> Design:
    """`cell_a` reads `top.u_b.m` in the input design. `blk_a_cell`: other text for its instance in blk_a."""
    design = Design(root)
    cells = design.path("cells")
    cells.write_bytes(cells.read_bytes().replace(b"endmodule", SPY, 1))
    if blk_a_cell is not None:
        blk_a = design.path("blk_a")
        old = b"  cell_a u_c0 (.clk(clk), .d(d), .q(m));\n"
        assert old in blk_a.read_bytes()
        blk_a.write_bytes(blk_a.read_bytes().replace(old, blk_a_cell.encode()))
    return design


def test_another_instance_of_a_module_with_an_old_reference_is_accepted(tmp_path: Path):
    design = spying_cell_a(tmp_path / "design")
    vet = Vet(Session(design.filelist, top="top"))
    with vet.scope("blk_a"):
        vet.module("blk_a").add_wire("q1")
        into_blk_a(design, vet, b"  cell_a u_c1 (.clk(clk), .d(d), .q(q1));\n")
    assert design.changed(vet) == {"blk_a"}
    cells = design.path("cells")
    at = vet.text(cells).index(b"endmodule")
    with pytest.raises(VetError, match="outside the scope") as e:
        with vet.scope("blk_a"):
            vet.splice(cells, at, at, b"  logic more;\n  assign more = top.u_b.m;\n", waiver="set-up of the test")
    assert "'cell_a'" in str(e.value)
    assert design.changed(vet) == {"blk_a"}


SPY_MODULE = "module {name} (\n  output logic o\n);\n  assign o = top.u_b.m;\nendmodule\n"


@pytest.mark.parametrize("how", ["added-file", "not-instantiated-before"])
def test_a_new_module_inside_that_reaches_outside_is_refused(tmp_path: Path, how):
    design = Design(tmp_path / "design")
    if how == "not-instantiated-before":
        (design.root / "rtl" / "spy.sv").write_bytes(SPY_MODULE.format(name="spy").encode())
        design.filelist.write_bytes(design.filelist.read_bytes() + b"rtl/spy.sv\n")
    vet = Vet(Session(design.filelist, top="top"))
    extra = tmp_path / "extra" / "spy.sv"
    extra.parent.mkdir()
    extra.write_bytes(SPY_MODULE.format(name="spy").encode())
    with pytest.raises(VetError, match="outside the scope") as e:
        with vet.scope("blk_a"):
            if how == "added-file":
                vet.add_file(extra)
            into_blk_a(design, vet, b"  logic ao;\n  spy u_spy (.o(ao));\n")
    assert "'spy'" in str(e.value)
    assert "top.u_b.m" in str(e.value)
    assert design.changed(vet) == set()
    assert statuses(vet) == []


GEN_CELL = "  if (1) begin : g\n    cell_a u_c0 (.clk(clk), .d(d), .q(m));\n  end\n"


def test_a_reference_hidden_by_a_generate_block_is_refused(tmp_path: Path):
    """The block's instance of cell_a goes, and blk_a gets a reference of its own to the same target."""
    design = spying_cell_a(tmp_path / "design", GEN_CELL)
    vet = Vet(Session(design.filelist, top="top"))
    blk_a = design.path("blk_a")
    start = vet.text(blk_a).index(GEN_CELL.encode())
    new = b"  assign m = d;\n  logic own_spy;\n  assign own_spy = top.u_b.m;\n"
    with pytest.raises(VetError, match="outside the scope") as e:
        with vet.scope("blk_a"):
            vet.splice(blk_a, start, start + len(GEN_CELL), new, waiver="set-up of the test")
    assert "'blk_a'" in str(e.value)
    assert "top.u_b.m" in str(e.value)
    assert design.changed(vet) == set()


def test_a_reference_in_a_generate_block_is_refused(vet, design):
    with pytest.raises(VetError, match="outside the scope") as e:
        with vet.scope("blk_a"):
            into_blk_a(design, vet, b"  if (1) begin : g\n    logic p;\n    assign p = top.u_b.m;\n  end\n")
    assert "'blk_a'" in str(e.value)
    assert design.changed(vet) == set()


def test_an_instance_in_a_generate_block_is_not_blamed_on_its_host(tmp_path: Path):
    """`shared` is outside the scope (blk_b uses it too) and reads top.u_b.m; one more instance adds no reference."""
    design = Design(tmp_path / "design")
    shared = design.path("shared")
    shared.write_bytes(shared.read_bytes().replace(b"endmodule", SPY))
    vet = Vet(Session(design.filelist, top="top"))
    text = b"  logic g2q;\n  if (1) begin : g2\n    shared u_s2 (.clk(clk), .d(d), .q(g2q));\n  end\n"
    with vet.scope("blk_a"):
        into_blk_a(design, vet, text)
    assert design.changed(vet) == {"blk_a"}
    with vet.scope("blk_a"):
        into_blk_a(design, vet, b"  logic g3q;\n  shared u_s3 (.clk(clk), .d(d), .q(g3q));\n")
    assert b"u_s3" in vet.text(design.path("blk_a"))


@pytest.mark.parametrize(
    "text",
    [
        b"  cell_a u_ar [1:0] (.clk(clk), .d(top.u_b.m), .q());\n",
        b"  for (genvar i = 0; i < 2; i++) begin : gl\n    cell_a u_c (.clk(clk), .d(top.u_b.m), .q());\n  end\n",
        b"  cell_a u_one (.clk(clk), .d(top.u_b.m), .q());\n",
    ],
    ids=["instance-array", "generate-loop", "instance"],
)
def test_a_reference_in_a_port_connection_is_refused(vet, design, text):
    with pytest.raises(VetError, match="outside the scope") as e:
        with vet.scope("blk_a"):
            into_blk_a(design, vet, text)
    assert "'blk_a'" in str(e.value)
    assert "top.u_b.m" in str(e.value)
    assert design.changed(vet) == set()
