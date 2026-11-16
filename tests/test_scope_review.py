"""Phase 5d, declared scope: tests from the adversarial review of the implementation.

The base specification is the docstring of `tests/test_scope.py`. The review
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
from test_scope import CELL, FILES, Design, statuses


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
            vet.splice(cells, at, at, b'  `include "alt.svh"\n')
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
            vet.splice(cells, at, at, text + b"\n")
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
        vet.splice(cells, at, at, text + b"\n")
    assert design.changed(vet) == {"cells"}
    assert vet.finish().ok


def test_a_macro_use_is_accepted(tmp_path: Path):
    design = Design(tmp_path / "design")
    cells = design.path("cells")
    cells.write_bytes(b"`define ONE 1'b1\n" + cells.read_bytes())
    vet = Vet(Session(design.filelist, top="top"))
    at = cells.read_bytes().index(b"  always_ff")
    with vet.scope("blk_a"):
        vet.splice(cells, at, at, b"  logic t;\n  assign t = `ONE;\n")
    assert vet.finish().ok


def test_outside_a_scope_a_splice_may_hold_any_directive(vet, design):
    """R1 is a rule of the scope; the raw splice is as free as before without one."""
    cells, at = in_cell_a(design)
    vet.splice(cells, at, at, b"`define X 1\n")
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
