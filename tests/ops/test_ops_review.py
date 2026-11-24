"""Phase 3 review fixes: cases the reviewer broke with probes.

Spec changes and additions (they extend the docstring of test_ops.py; where
they differ, this file wins):

Comparing text
    Every comparison of expressions, values and declarations (skip detection,
    `was=`, post-conditions) compares token sequences with comments ignored,
    not characters: `x & &y` differs from `x && y`, `"a b"` differs from
    `"ab"`, `/* tie */ x` equals `x`, `logic[3:0]` equals `logic [ 3 : 0 ]`.
    The tokenising lives in the adapter.

connect
    An expression that is empty, blank or only a comment is refused with
    VetError: for an input port the same "tie it to a value ... or remove the
    port" message as disconnect; otherwise a message pointing to disconnect.

add_assign: what counts as a driver
    A target is driven if ANY elaborated instance of the module has a driver
    for it (parameterisations can differ), or if its declaration has an
    initialiser (`wire w = a;`). An undriven inout port is an allowed target.

add_param
    Skipped only when the existing declaration has the same type, name and
    default; otherwise DriftError. In a header with package imports and no
    parameter list, ` #(...)` goes after the last import.

Conditional compilation
    An operation that edits a list (ports, parameters, connections, overrides)
    raises VetError naming the directive when a preprocessor conditional
    (`ifdef, `ifndef, `elsif, `else, `endif) lies inside that list. Sign-off
    only sees the current defines, so the other configuration cannot be checked.

Paths
    Expectations registered by operations match hierarchical paths exactly; a
    path such as `t.g[0].u` is not treated as a glob pattern.
    Instances inside generate blocks or instance arrays are not supported by
    instance operations yet: VetError saying so ("generate block" / "instance
    array").

Confirming multi-instance edits
    `instances=` given to vet.module must equal len(paths), always (VetError
    otherwise, also when the module is elaborated once). Instance operations
    edit the text of the enclosing module, so they need the same confirmation
    as module-level edits when that module is elaborated more than once.

Superseding
    When a later applied operation targets the same thing (same instance and
    port, same instance and parameter, same module and name), its
    post-condition replaces the earlier one.

Knock-on changes
    Instance.allow_changes_below(): permits (does not require) any structural
    change at paths strictly below this instance, for parameters that reshape
    a subtree.

Declarations
    DeclInfo.type_text spells unpacked dimensions as written: "logic [3:0] [2]".
    add_wire raises VetError for a name that is not a simple identifier or is a
    SystemVerilog keyword, and DriftError when the name is used by a typedef,
    function, task, genvar or named block of the module.
    add_wire raises VetError when the header line ends inside an unterminated
    block comment. body_indent is computed on bytes (non-ASCII text earlier in
    the file must not shift it).

Checks and the command line
    A check that raises is reported as a failed check ("check raised ..."),
    not as a traceback. With --dry-run the "<N> applied, <M> skipped" line goes
    to stderr so stdout stays a pure diff.

The removal check after a cancelled addition (#57)
    Once `remove_wire` or `remove_assign` retires the post-condition of the
    addition that it undid, its own post-condition is the only one left. It has
    to be at least as strong as the one it retired.
    C2  `remove_assign` fails when an assignment drives the target under
        another spelling, so `assign t[3:0] = ...` counts as an assignment to
        `t`. The comparison is the text of the target OR a base name of it
        (`driven_bases`). The text test stays because a target can be given as
        the design spells it, such as `w[3:0]` or an escaped name, and
        `driven_bases` answers only for a plain identifier. Matching on base
        names alone made the check vacuous for every such target.
    Stated limit, not fixed: a declaration that comes back inside a generate
    construct is not seen. `ModuleInfo.generate_decls` holds bare names with no
    scope and includes branches the elaboration does not take, so a check on it
    fails correct runs: a module-level `tmp` that was removed is reported as
    still there when any generate block declares its own local `tmp`. A
    name-only set cannot tell the two apart. The same holds for
    `generate_assigned` and `remove_assign`.

One check for a pair (#57, part 2)
    An addition and its removal share one key, so the later operation replaces
    the post-condition of the earlier one. See rules W6 to W11 in `test_ops.py`.
    C3  Exactly one post-condition is left for the pair, and it is the one of
        the last operation, with its own evaluator and its own arguments. It
        fires when the design does not show what that operation did. Sharing
        the key must not lose the check, and must not run the evaluator of the
        other operation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.ops import DriftError, Vet, VetError
from hdl_vet.session import Session
from no_comparison import no_comparison  # noqa: F401
from vetkit import make_design

LEAF = b"module leaf(input logic a, output logic q);\n  assign q = a;\nendmodule\n"


def vet_for(tmp_path: Path, src: bytes, top: str = "t") -> Vet:
    (tmp_path / "t.sv").write_bytes(src)
    (tmp_path / "t.f").write_bytes(b"t.sv\n")
    return Vet(Session(tmp_path / "t.f", top=top))


@pytest.fixture
def vet(tmp_path: Path):
    kit = make_design(tmp_path)
    return Vet(Session(kit.filelist, top="top"))


# --- wrong designs that passed ---------------------------------------------------------


def test_connect_refuses_an_empty_expression(tmp_path: Path):
    vet = vet_for(
        tmp_path,
        LEAF + b"module t(input logic x, output logic o);\n  leaf u (.a(x), .q(o));\nendmodule\n",
    )
    u = vet.module("t").instance("u")
    for expr in ("", " ", "/* nothing */"):
        with pytest.raises(VetError, match="tie it to a value"):
            u.connect("a", expr, was="x")
        with pytest.raises(VetError, match="disconnect"):
            u.connect("q", expr, was="o")
    assert vet.log == [] and vet.finish().files == {}


def test_add_assign_refuses_a_net_with_an_initialiser(tmp_path: Path):
    vet = vet_for(
        tmp_path,
        b"module t(input logic a, input logic b, output logic o);\n  wire w = a;\n  assign o = w;\nendmodule\n",
    )
    with pytest.raises(VetError, match="driven"):
        vet.module("t").add_assign("w", "b")


def test_add_assign_checks_every_parameterisation(tmp_path: Path):
    vet = vet_for(
        tmp_path,
        b"module sub #(parameter bit P = 0) (input logic a, output wire q);\n"
        b"  wire w;\n  if (P) begin : g\n    assign w = a;\n  end\n  assign q = w;\nendmodule\n"
        b"module t(input logic a, output wire o0, output wire o1);\n"
        b"  sub #(.P(0)) u0 (.a(a), .q(o0));\n  sub #(.P(1)) u1 (.a(a), .q(o1));\nendmodule\n",
    )
    with pytest.raises(VetError, match="driven"):
        vet.module("sub", instances=2).add_assign("w", "~a")


def test_add_assign_accepts_an_undriven_inout(tmp_path: Path):
    vet = vet_for(tmp_path, b"module t(input logic a, inout wire pad);\nendmodule\n")
    vet.module("t").add_assign("pad", "a")
    r = vet.finish()
    assert r.ok, r.report()


def test_comparisons_use_tokens_not_characters(tmp_path: Path):
    vet = vet_for(
        tmp_path,
        b'module leaf #(parameter string S = "a b") (input logic a, input logic b, output logic q);\n'
        b"  assign q = a & b;\nendmodule\n"
        b"module t(input logic x, input logic [1:0] y, output logic o);\n"
        b'  leaf #(.S("a b")) u (.a(x & &y), .b(/* tie */ x), .q(o));\nendmodule\n',
    )
    u = vet.module("t").instance("u")
    with pytest.raises(DriftError):
        u.connect("a", "x && y")
    with pytest.raises(DriftError):
        u.set_param("S", '"ab"')
    u.connect("a", "x&&y", was="x & & y")  # same tokens as the current text
    u.connect("b", "x")  # the comment does not count: already connected to x
    u.set_param("S", '"a b"')
    assert [rec.status for rec in vet.log] == ["applied", "skipped", "skipped"]


def test_comment_inside_a_connection_does_not_block_replacing_it(tmp_path: Path):
    vet = vet_for(
        tmp_path,
        LEAF + b"module t(input logic x, input logic y, output logic o);\n"
        b"  leaf u (.a(/* tie */ x), .q(o));\nendmodule\n",
    )
    vet.module("t").instance("u").connect("a", "y", was="x")
    r = vet.finish()
    assert r.ok, r.report()
    assert b".a(y)" in next(iter(r.files.values()))


def test_add_port_skip_ignores_spacing_but_not_tokens(tmp_path: Path):
    vet = vet_for(tmp_path, b"module t(input logic[ 3 : 0 ] a, input logic b);\nendmodule\n")
    t = vet.module("t")
    t.add_port("a", "input", width=4)
    assert [rec.status for rec in vet.log] == ["skipped"]
    with pytest.raises(DriftError):
        t.add_port("b", "input", type="logic signed")


def test_add_param_skip_needs_the_same_type(tmp_path: Path):
    vet = vet_for(tmp_path, b"module t #(parameter bit [2:0] W = 8) (input logic a);\nendmodule\n")
    with pytest.raises(DriftError, match="W"):
        vet.module("t").add_param("W", 8)
    vet.module("t").add_param("W", 8, type="bit [2:0]")
    assert [rec.status for rec in vet.log] == ["skipped"]


@pytest.mark.parametrize(
    ("src", "op"),
    [
        (
            LEAF.replace(b"output logic q", b"input logic b, output logic q")
            + b"module t(input logic x, input logic y, output logic o);\n"
            b"  leaf u (\n    .a(x)\n`ifdef DFT\n    , .b(y)\n`endif\n    , .q(o)\n  );\nendmodule\n",
            lambda v: v.module("t").instance("u").connect("b", "y"),
        ),
        (
            b"module t(\n  input logic a\n`ifdef DFT\n  , input logic dft\n`endif\n);\nendmodule\n",
            lambda v: v.module("t").add_port("x", "input"),
        ),
        (
            b"module t #(\n  parameter int A = 1\n`ifndef SYNTH\n  , parameter int B = 2\n`endif\n"
            b") (input logic a);\nendmodule\n",
            lambda v: v.module("t").add_param("C", 3),
        ),
    ],
)
def test_lists_with_conditional_compilation_are_refused(tmp_path: Path, src, op):
    vet = vet_for(tmp_path, src)
    with pytest.raises(VetError, match="ifn?def"):
        op(vet)
    assert vet.finish().files == {}


# --- paths ---------------------------------------------------------------------------------

GEN = (
    LEAF + b"module t(input logic a, output logic [1:0] o, output logic [1:0] p);\n"
    b"  for (genvar i = 0; i < 2; i++) begin : g\n    leaf u (.a(a), .q(o[i]));\n  end\n"
    b"  leaf u_arr [1:0] (.a(a), .q(p));\n"
    b"endmodule\n"
)


def test_expectations_match_generate_and_array_paths(tmp_path: Path):
    vet = vet_for(tmp_path, GEN)
    leaf = vet.module("leaf", instances=4)
    assert leaf.paths == ["t.g[0].u", "t.g[1].u", "t.u_arr[0]", "t.u_arr[1]"]
    leaf.add_param("EXTRA", 1)
    r = vet.finish()
    assert r.ok, r.report()
    assert len(r.changes) == 4


def test_instance_operations_under_generate_or_arrays_say_so(tmp_path: Path):
    vet = vet_for(tmp_path, GEN)
    t = vet.module("t")
    with pytest.raises(VetError, match="generate block"):
        t.instance("u").connect("a", "~a", was="a")
    with pytest.raises(VetError, match="instance array"):
        t.instance("u_arr").connect("a", "~a", was="a")


def test_instances_count_must_always_match(vet):
    with pytest.raises(VetError, match="1"):
        vet.module("mid", instances=5)
    assert vet.module("mid", instances=1).paths == ["top.u_mid"]


def test_instance_edit_in_a_shared_module_needs_confirmation(tmp_path: Path):
    src = (
        LEAF + b"module pair(input logic a, output logic q);\n  leaf u (.a(a), .q(q));\nendmodule\n"
        b"module t(input logic a, output logic o0, output logic o1);\n"
        b"  pair u_p0 (.a(a), .q(o0));\n  pair u_p1 (.a(a), .q(o1));\nendmodule\n"
    )
    vet = vet_for(tmp_path, src)
    with pytest.raises(VetError, match="2"):
        vet.module("pair").instance("u").connect("a", "~a", was="a")
    u = vet.module("pair", instances=2).instance("u")
    assert u.paths == ["t.u_p0.u", "t.u_p1.u"]
    u.connect("a", "~a", was="a")
    r = vet.finish()
    assert r.ok, r.report()
    assert len(r.changes) == 2


# --- sequences --------------------------------------------------------------------------------


def test_a_later_operation_supersedes_an_earlier_one(vet):
    u = vet.module("mid").instance("u_l0")
    u.connect("d", "~a", was="a")
    u.connect("d", "a & a", was="~a")
    u.set_param("DEPTH", 4)
    u.set_param("DEPTH", 6, was=4)
    r = vet.finish()
    assert r.ok, r.report()
    assert r.failed_checks == []
    text = next(iter(r.files.values()))
    assert b".d(a & a)" in text and b".DEPTH(6)" in text


def test_allow_changes_below(tmp_path: Path):
    src = (
        b"module leaf #(parameter int W = 2) (input logic [W-1:0] a, output logic [W-1:0] q);\n"
        b"  assign q = a;\nendmodule\n"
        b"module mid #(parameter int W = 2) (input logic [1:0] a, output logic [1:0] q);\n"
        b"  logic [W-1:0] x, y;\n  assign x = W'(a);\n  assign q = 2'(y);\n"
        b"  leaf #(.W(W)) u_leaf (.a(x), .q(y));\nendmodule\n"
        b"module t(input logic [1:0] a, output logic [1:0] q);\n  mid #(.W(2)) u_mid (.a(a), .q(q));\nendmodule\n"
    )
    vet = vet_for(tmp_path, src)
    vet.module("t").instance("u_mid").set_param("W", 4, was=2)
    r = vet.finish()
    assert not r.ok and {c.path for c in r.unexpected_changes} == {"t.u_mid.u_leaf"}

    vet = vet_for(tmp_path, src)
    u_mid = vet.module("t").instance("u_mid")
    u_mid.set_param("W", 4, was=2)
    u_mid.allow_changes_below()
    r = vet.finish()
    assert r.ok, r.report()


# --- text placement ----------------------------------------------------------------------------


def test_add_param_goes_after_header_imports(tmp_path: Path):
    vet = vet_for(
        tmp_path,
        b"package pkg;\n  typedef logic [3:0] nib_t;\nendpackage\n"
        b"module t import pkg::*; (input nib_t a);\nendmodule\n",
    )
    vet.module("t").add_param("P", 3)
    r = vet.finish()
    assert r.ok, r.report()
    assert b"module t import pkg::*; #(parameter int P = 3) (input nib_t a);" in next(iter(r.files.values()))


def test_add_wire_refuses_a_header_line_ending_in_an_open_comment(tmp_path: Path):
    vet = vet_for(tmp_path, b"module t (input logic a); /* start\n  still */\n  logic b;\nendmodule\n")
    with pytest.raises(VetError, match="comment"):
        vet.module("t").add_wire("w")


def test_body_indent_with_non_ascii_text_before_the_module(tmp_path: Path):
    banner = ("// " + "─" * 40 + " → banner\n").encode()
    vet = vet_for(tmp_path, banner + b"module t (input logic a);\n  logic b;\nendmodule\n")
    assert vet.module("t").info.body_indent == "  "
    vet.module("t").add_wire("w")
    assert b"\n  logic w;\n  logic b;\n" in next(iter(vet.finish().files.values()))


def test_decl_type_text_with_unpacked_dimensions(tmp_path: Path):
    vet = vet_for(
        tmp_path,
        b"module t (input logic a);\n  logic [3:0] w [2];\n  logic [3:0] z [8];\n  logic signed [7:0] s;\nendmodule\n",
    )
    assert [(d.name, d.type_text) for d in vet.module("t").info.decls] == [
        ("w", "logic [3:0] [2]"),
        ("z", "logic [3:0] [8]"),
        ("s", "logic signed [7:0]"),
    ]
    with pytest.raises(DriftError, match="w"):
        vet.module("t").add_wire("w", width=4)


def test_add_wire_name_checks(tmp_path: Path):
    vet = vet_for(
        tmp_path,
        b"module t (input logic a);\n  typedef logic [1:0] pair_t;\n"
        b"  function automatic logic f(input logic x);\n    return x;\n  endfunction\n"
        b"  genvar gi;\n  if (1) begin : blk\n    logic inner;\n  end\nendmodule\n",
    )
    t = vet.module("t")
    for bad in ("module", "foo bar", "9lives", ""):
        with pytest.raises(VetError):
            t.add_wire(bad)
    for used in ("pair_t", "f", "gi", "blk"):
        with pytest.raises(DriftError, match=used):
            t.add_wire(used)
    t.add_wire("inner")  # only declared inside the block: free at module level
    assert vet.finish().ok


# --- checks and the command line -------------------------------------------------------------------


def test_a_check_that_raises_is_a_failed_check(tmp_path: Path):
    kit = make_design(tmp_path)

    def boom(design):
        raise RuntimeError("kaboom")

    r = Session(kit.filelist, top="top").finish(checks=[boom])
    assert not r.ok
    assert len(r.failed_checks) == 1
    assert "check raised" in r.failed_checks[0] and "kaboom" in r.failed_checks[0]


def test_dry_run_keeps_stdout_a_pure_diff(tmp_path: Path, capsys):
    from hdl_vet.cli import main

    kit = make_design(tmp_path / "in")
    script = tmp_path / "s.py"
    script.write_text("def apply(vet):\n    vet.module('top').add_port('dft_en', 'input')\n")
    assert main(["apply", "-f", str(kit.filelist), "-s", str(script), "--top", "top", "--dry-run"]) == 0
    out, err = capsys.readouterr()
    assert out.startswith("--- a/rtl/top.sv\n")
    assert "applied" not in out and "1 applied, 0 skipped" in err


def test_disconnect_message_for_an_interface_port(tmp_path: Path):
    vet = vet_for(
        tmp_path,
        b"interface bus_if;\n  logic v;\nendinterface\n"
        b"module leaf(bus_if b);\nendmodule\n"
        b"module t(input logic a);\n  bus_if u_if ();\n  leaf u (.b(u_if));\nendmodule\n",
    )
    with pytest.raises(VetError, match="an interface port"):
        vet.module("t").instance("u").disconnect("b")


# --- the removal check after a cancelled addition (#57) --------------------------------

PLAIN = b"module t (input logic [3:0] a, output logic [3:0] y);\n  assign y = a;\nendmodule\n"


def _behind_the_back(vet: Vet, path: Path, old: bytes, new: bytes) -> None:
    """Change the text the way the reviewer's probes did, so a post-condition has to catch it."""
    _ = vet.design  # apply what is queued, so the text shows the result of the operations
    data = vet.text(path)
    at = data.find(old)
    assert at >= 0, old
    vet.splice(path, at, at + len(old), new, old=old)


def test_a_target_that_is_not_a_plain_identifier_is_still_compared_as_text(tmp_path: Path):
    """C2. The guard against making the check vacuous.

    `remove_assign` takes the target as the design spells it, so it can be `w[3:0]`.
    `driven_bases` answers only for a plain identifier, so a comparison on base names alone
    could never match such a target and the check would pass whatever the design holds.
    """
    vet = vet_for(
        tmp_path,
        b"module t (input logic [3:0] a, output logic [3:0] y);\n"
        b"  wire [7:0] w;\n"
        b"  assign w = {a, a};\n"
        b"  assign w[3:0] = a;\n"
        b"  assign y = w[7:4];\n"
        b"endmodule\n",
    )
    vet.module("t").remove_assign("w[3:0]")
    _behind_the_back(vet, tmp_path / "t.sv", b"  assign y = w[7:4];\n", b"  assign w[3:0] = a;\n  assign y = w[7:4];\n")
    r = vet.finish()
    assert not r.ok
    assert r.failed_checks == ["remove_assign t w[3:0]: the assignment is still there in the final design"]


def test_an_assignment_back_as_a_part_select_fails_the_removal(tmp_path: Path):
    """C2. The assignment is added and removed again, and then comes back as a part select.

    The check of the `add_assign` is retired. The check of the `remove_assign` used to
    compare the target as text, so `dbg[3:0]` did not count as an assignment to `dbg` and
    this design signed off with a net the script declared and a driver it never wrote.
    """
    vet = vet_for(tmp_path, PLAIN)
    top = vet.module("t")
    top.add_wire("dbg", width=4)
    top.add_assign("dbg", "a")
    top.remove_assign("dbg")
    _behind_the_back(vet, tmp_path / "t.sv", b"  assign y = a;\n", b"  assign dbg[3:0] = 4'b0;\n  assign y = a;\n")
    r = vet.finish()
    assert not r.ok
    assert r.failed_checks == ["remove_assign t dbg: the assignment is still there in the final design"]


# --- C3: one check for a pair, the one of the last operation ---------------------------------

CHILD = (
    b"module child (\n  input  logic a,\n  input  logic spare,\n  output logic q\n);\n  assign q = a;\nendmodule\n"
    b"module t (input logic a, output logic q);\n  child u (.a(a), .spare(1'b0), .q(q));\nendmodule\n"
)


def test_the_check_of_the_last_addition_is_the_one_left_for_a_wire_and_an_assignment(tmp_path: Path) -> None:
    """C3. Added, removed and added again, and then the assignment goes behind the back.

    The checks of the two removals were replaced, so they cannot fire on the wire that is
    there. The check of the second `add_assign` holds the second expression and fires.
    """
    vet = vet_for(tmp_path, PLAIN)
    top = vet.module("t")
    top.add_wire("dbg", width=4)
    top.add_assign("dbg", "a")
    top.remove_assign("dbg")
    top.remove_wire("dbg")
    top.add_wire("dbg", width=4)
    top.add_assign("dbg", "~a")
    _behind_the_back(vet, tmp_path / "t.sv", b"  assign dbg = ~a;\n", b"")
    r = vet.finish()
    assert not r.ok
    assert r.failed_checks == ["add_assign t dbg: the assignment 'assign dbg = ~a;' is missing"]


def test_the_check_of_the_last_addition_is_the_one_left_for_a_parameter(tmp_path: Path) -> None:
    """C3. Added, removed and added again, and then the default changes behind the back."""
    vet = vet_for(tmp_path, PLAIN)
    top = vet.module("t")
    top.add_param("DFT", 0)
    top.remove_param("DFT")
    top.add_param("DFT", 0)
    _behind_the_back(vet, tmp_path / "t.sv", b"DFT = 0", b"DFT = 1")
    r = vet.finish()
    assert not r.ok
    assert r.failed_checks == ["add_param t DFT: expected default '0', found '1'"]


def test_the_check_of_the_last_removal_is_the_one_left_for_a_port(tmp_path: Path) -> None:
    """C3. A port of the input design is removed, added and removed, and then comes back.

    The check of the `add_port` was replaced. The check of the last `remove_port` fires.
    """
    vet = vet_for(tmp_path, CHILD)
    child = vet.module("child")
    child.remove_port("spare")
    child.add_port("spare", "input")
    child.remove_port("spare")
    _behind_the_back(vet, tmp_path / "t.sv", b"  output logic q\n);", b"  output logic q,\n  input  logic spare\n);")
    r = vet.finish()
    assert not r.ok
    assert r.failed_checks == ["remove_port child spare: the port is still there in the final design"]


# --- an addition after a removal inside a group (#57) ----------------------------------------
#
# C4  Inside a group, `add_wire` used to see the declaration a `remove_wire` had queued
#     away, because it loads no fresh module info of its own. It recorded "skipped", and the
#     sign-off passed on a design that did not hold the wire the script asked for. A wrong
#     "ok", and it also made the next operation fail with "no net, variable or port".
#     It now reads the registered check of the removal, which says the declaration is on its
#     way out without a re-load. A re-load would place a second declaration above the first
#     instead of below it, so consecutive calls would appear in reverse order.


def test_an_addition_after_a_removal_inside_a_group_is_applied(vet: Vet) -> None:
    """C4. The same three operations as rule W7, inside a group this time."""
    mid = vet.module("mid")
    with vet.group():
        mid.add_wire("w", width=4)
        mid.remove_wire("w")
        mid.add_wire("w", width=4)
    assert [(e.op, e.status) for e in vet.log] == [
        ("add_wire", "applied"),
        ("remove_wire", "applied"),
        ("add_wire", "applied"),
    ]
    r = vet.finish()
    assert r.failed_checks == []
    assert r.ok, r.report()
    assert b"logic [3:0] w;" in r.files[mid.path]


def test_an_assignment_can_follow_an_addition_after_a_removal_in_a_group(vet: Vet) -> None:
    """C4. The knock-on: the stale skip used to leave the name undeclared for the next call."""
    mid = vet.module("mid")
    with vet.group():
        mid.add_wire("w", width=4)
        mid.remove_wire("w")
        mid.add_wire("w", width=4)
        mid.add_assign("w", "a")
    r = vet.finish()
    assert r.failed_checks == []
    assert r.ok, r.report()
    assert b"assign w = a;" in r.files[mid.path]
