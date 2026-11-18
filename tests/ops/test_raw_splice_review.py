"""A raw splice needs a proof or a waiver: tests from the adversarial review.

The base specification is tests/ops/test_raw_splice.py (S1 to S12). The review
(about 70 cases, six on a copy of demo_ss) found three ways for a change of
logic to end with an ok sign-off, no waiver and no record. These rules close
them, and one more door.

T1  Splices that wait are judged together. Raw splices of one file that are not
    applied yet all have ranges in the same text (the one `vet.text()` gives).
    Each new splice is sorted on the file with all of them applied, against the
    file before the first of them: "blanks and comments only" must hold for the
    whole, and T2 too. The ranges that a script took from `vet.text()` stay
    right. Before: two splices that each changed only a comment, made from one
    `vet.text()`, together turned code into a comment, and neither was marked.
T2  "Inside one module" is judged on the result too. After the splice, the file
    holds the same module declarations in the same order, the text outside the
    module is the same token for token, and the new text lies inside the text
    of that module. If not, the splice is outside the modules (S7). Before: an
    insertion in the middle of the word `endmodule` ended the module early and
    put a declaration at file level.
T3  `vet.session.splice` in a script is a raw splice like `vet.splice`: it is
    sorted, marked or refused in the same way. (It takes no waiver: a script
    that needs one uses `vet.splice`.)
T4  A saved session of an older version is refused. Its raw splices have no
    mark, so a run that resumes it could not judge them. The format number of
    the archive is 2; an archive with format 1 gives the message of another
    format version.
T5  A waiver has no line separator of any kind (also U+2028, U+2029, a form
    feed, a vertical tab).

FROM THE SECOND REVIEW (of the fixes; it found seven more ways through)

U1  One `Vet` for a session. `Vet(session)` on a session that has a `Vet` is
    refused. The session has no public name that queues an edit without the
    guard, and no public name that takes the guard away. (A script that reaches
    into names that begin with `_` is not covered: a stated limit.)
U2  A module that is read from outside is not proved. When a hierarchical
    reference that is written outside a module M leads into an instance of M
    (a net, a variable, a function of M), a comparison of M cannot stand for
    the design: the record of M is "error", and the detail names the
    reference. This holds for every module that is compared, also one with
    `override_net`.
U3  T2 in full. After the splice the text outside the module is the same token
    for token, over the whole file. A comment or a string that the splice
    opens and that reaches text after the module makes the splice outside.
U4  A macro use (`` `NAME ``) in the old or the new text of a raw splice makes
    it outside: what it expands to is not in the text.
U5  A splice that touches a `` `define `` is never "blanks and comments only".
U6  A `bind` in the old or the new text of a raw splice makes it outside.
U7  A module that is proved and has no enable is not given to the engine as
    inert: its parent is compared with it inside. (Before: an exception from
    the engine, and a failed sign-off, for a harmless splice in a child and one
    in its parent.)

FROM THE THIRD REVIEW (every earlier break stayed closed; five more causes)

WHO THE RULE IS FOR. The rule protects a script that uses the public interface
as the manual shows it, mistakes included. A script is Python: one that works
against the check (it reaches into names that begin with `_`, patches hdl-vet,
forges objects) can always pass. That is a stated limit, not a hole. Doors that
an honest script can walk through are closed; doors that only a hostile script
finds are closed where it is cheap.

V1  A `bind` that is written in another module and has the module M, or an
    instance of M, as its target puts logic into M that a comparison of M
    alone does not read. The record of M is "error", and the detail names the
    bind. This holds for every module that is compared.
V2  The marks and the waiting splices are part of the state of the session.
    What `checkpoint`, `rollback`, `check`, `refresh`, `add_file`, a group and
    a saved session do to the text, they do to the marks and to the list of
    waiting splices. Splices that wait are the ones that really wait in the
    session. `Session.reloads` cannot be set from outside.
V3  An argument of a near type (an integer of a subclass, a `bytearray`, a
    `memoryview`) is refused or judged like the plain type. It is never
    applied without a mark.
V4  A `Vet` cannot be copied.
V5  `Session.rollback` takes only a checkpoint that this session gave.

FROM THE FOURTH REVIEW (every earlier break stayed closed; three more, and a
false failure)

W1  T1 over the file. The raw splices of a FILE that wait are judged together,
    whatever module each lies in: one at file level and one in a module can
    add up to another module body.
W2  V1 by meaning. The target of a bind is what it resolves to, not how it is
    written (a comment in the name, blanks round a dot, an escaped name, a
    path from `$root`). If hdl-vet cannot tell what a bind of the design
    targets, every compared module is "error".
W3  A changed default of a parameter. The input side of a comparison has the
    parameter values that the input design gives the instance; the edited side
    has the values of the edited design. A default that a raw splice changes,
    and that an instance uses, is not "proved": it is "refuted" or "error".
W4  Splices that count because of a later one take its waiver. A comment
    splice and then a waived change of logic, both waiting: the module is
    "waived", as when the change is made by one splice.
W5  The refusal of a splice whose old or new text holds a keyword that opens
    or closes a module names that keyword.
W6  `Session.check` and `Session.finish`, called without the comparison of a
    `Vet` on a session that has marks, give an "error" record for each marked
    module. They never say ok for it.

FROM THE FIFTH REVIEW (every earlier break stayed closed; 120 random sets of
waiting splices gave no break; one class was left)

X1  A directive that is written inside a module reaches the modules after it in
    the file. A raw splice whose range meets the line of a directive (its name,
    its arguments, the name of an include, its continuation lines, an insertion
    at its line break) is outside, also when the old and the new text are plain
    names. A splice that changes only blanks there is still "blanks and
    comments only", but for a `define (U5).
X2  `Session.check` and `Session.finish` keep the waivers of the splices outside
    the modules, in `Result.outside` and in the report.

LIMITS, STATED (in the manual too)

- `Instance.set_param` on an instance of a module that is compared makes the
  record of that parameter set "error": the comparison takes one set of values
  for both sides (W3).

- A child with a waived raw splice under a parent that is marked itself: the
  parent is compared with the child inside, so the child's waived change shows
  in the record of the parent. Give the parent's splice a waiver too.
- A module that holds an `interface class` (or another construct whose opening
  keyword pairs with a closing keyword of a declaration at file level) makes
  every raw splice in it "outside".
- `fork`, a covergroup, a property and a `specify` block are sorted correctly,
  and the comparison then answers "error": Yosys does not read them.

- A module with a type parameter at another type than its default is "not
  checked" (R1 of tests/ops/test_override_levels_review.py). On demo_ss this
  holds for three of five leaf modules that the review tried: a harmless raw
  splice there needs a waiver.

- A comment can be a directive for another tool (`// synopsys translate_off`,
  `// synopsys full_case`). hdl-vet reads it as a comment: a splice that adds or
  removes one needs nothing, and no comparison sees it.
- `` `__LINE__ `` changes when a line is added above it, also by the header line
  that hdl-vet writes into a changed file.
- The comparison reads the sources as a synthesis tool does. It does not see a
  change in what only a simulation runs: the text of a `$display`, a delay, an
  `initial` block. The Yosys reader defines `SYNTHESIS`; the load of hdl-vet
  does not. A change in a branch that is active only without `SYNTHESIS` is not
  seen by the comparison.
- A file that cannot be read by itself (it uses a macro of a header) is never
  "blanks and comments only": a comment added there marks the module.
- A proof costs time: a harmless splice in the top of demo_ss takes about a
  minute.
- Operations that are given an expression write logic without a comparison:
  `Module.add_assign`, `Instance.connect` with an expression, `Instance.set_param`.
  The sign-off records them as structural changes only. (Issue for the owner.)
- The session by itself (Python, without `Vet`) has no marks and no comparison.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from hdl_vet import archive, equiv
from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session
from override_design import Design, needs_yosys, records

pytestmark = [pytest.mark.equiv]

LEAF = (
    "module leaf (\n"
    "  input  logic a,\n"
    "  input  logic b,\n"
    "  output logic y\n"
    ");\n"
    "  assign y = a // p\n"
    "// q */\n"
    "& b\n"
    "// r */\n"
    ";\n"
    "endmodule\n"
)
TOP = (
    "module top (\n"
    "  input  logic a,\n"
    "  input  logic b,\n"
    "  output logic y\n"
    ");\n"
    "  leaf u_leaf (.a(a), .b(b), .y(y));\n"
    "endmodule\n"
)


def no_yosys(monkeypatch) -> None:
    monkeypatch.setattr(equiv, "available", lambda: False)


def two_comment_splices(vet, design, call) -> None:
    """Both ranges are taken from one read of the text, as a script does."""
    path = design.path("leaf")
    text = vet.text(path)
    first = text.index(b"// p")
    second = text.index(b"// q */")
    call(path, first, first + 4, b"/* p", old=b"// p")
    call(path, second, second + 7, b"// q", old=b"// q */")


# --- T1 -------------------------------------------------------------------------------------------


@needs_yosys
def test_two_comment_splices_without_a_reload_are_judged_together(tmp_path):
    design = Design(tmp_path / "d", {"leaf": LEAF, "top": TOP})
    vet = design.vet()
    two_comment_splices(vet, design, vet.splice)
    result = vet.finish()
    assert records(result) == [("leaf", "refuted")], result.report()
    assert not result.ok


def test_two_comment_splices_without_yosys_are_refused(tmp_path, monkeypatch):
    no_yosys(monkeypatch)
    design = Design(tmp_path / "d", {"leaf": LEAF, "top": TOP})
    vet = design.vet()
    with pytest.raises(VetError, match=r"hdl-vet\[equiv\]"):
        two_comment_splices(vet, design, vet.splice)


@needs_yosys
def test_a_splice_after_a_queued_operation_is_judged_on_the_text_of_the_call(tmp_path):
    """`add_wire` is queued; the splice then changes logic further down in the same file."""
    design = Design(tmp_path / "d", {"leaf": LEAF, "top": TOP})
    vet = design.vet()
    vet.module("leaf").add_wire("spare")
    vet.session.refresh()
    path = design.path("leaf")
    at = vet.text(path).index(b"& b")
    vet.module("leaf").add_wire("spare2")
    vet.splice(path, at, at + 3, b"| b", old=b"& b")
    result = vet.finish()
    assert [(r.module, r.status) for r in result.equivalence] == [("leaf", "refuted")], result.report()


# --- T2 -------------------------------------------------------------------------------------------

AB = (
    "module a (\n"
    "  input  logic x,\n"
    "  output logic y\n"
    ");\n"
    "  assign y = x;\n"
    "endmodule\n"
    "module b (\n"
    "  input  logic x,\n"
    "  output logic z\n"
    ");\n"
    "  assign z = ~x;\n"
    "endmodule\n"
)
AB_TOP = (
    "module top (\n"
    "  input  logic x,\n"
    "  output logic y,\n"
    "  output logic z\n"
    ");\n"
    "  a u_a (.x(x), .y(y));\n"
    "  b u_b (.x(x), .z(z));\n"
    "endmodule\n"
)


@pytest.mark.parametrize(
    "text",
    [
        b"dule\nlocalparam int K = 5;\n//",
        b"dule\nbind b a u_bound (.x(x), .y());\n//",
        b"dule\nmodule c; endmo",
    ],
)
@pytest.mark.parametrize("yosys", [True, False])
def test_a_splice_that_splits_endmodule_is_outside(tmp_path, monkeypatch, text, yosys):
    if not yosys:
        no_yosys(monkeypatch)
    design = Design(tmp_path / "d", {"ab": AB, "top": AB_TOP})
    vet = design.vet()
    path = design.path("ab")
    at = vet.text(path).index(b"endmodule") + len(b"endmo")
    with pytest.raises(VetError, match="outside"):
        vet.splice(path, at, at, text)
    assert design.changed(vet) == set()


@needs_yosys
def test_a_splice_in_the_middle_of_a_word_inside_a_module_is_still_inside(tmp_path):
    """`~x` to `~~x`, written as an insertion after the first character: nothing leaves the module."""
    design = Design(tmp_path / "d", {"ab": AB, "top": AB_TOP})
    vet = design.vet()
    path = design.path("ab")
    at = vet.text(path).index(b"~x") + 1
    vet.splice(path, at, at, b"~")
    result = vet.finish()
    assert records(result) == [("b", "error")], result.report()  # slang takes `~~x` for an error
    assert not result.ok


# --- T3 -------------------------------------------------------------------------------------------


@needs_yosys
def test_the_splice_of_the_session_is_raw_too(tmp_path):
    design = Design(tmp_path / "d", {"ab": AB, "top": AB_TOP})
    vet = design.vet()
    path = design.path("ab")
    at = vet.text(path).index(b"~x")
    vet.session.splice(path, at, at + 2, b"x", old=b"~x")
    result = vet.finish()
    assert records(result) == [("b", "refuted")], result.report()
    assert not result.ok


def test_the_splice_of_the_session_is_refused_without_yosys(tmp_path, monkeypatch):
    no_yosys(monkeypatch)
    design = Design(tmp_path / "d", {"ab": AB, "top": AB_TOP})
    vet = design.vet()
    path = design.path("ab")
    at = vet.text(path).index(b"~x")
    with pytest.raises(Exception, match=r"hdl-vet\[equiv\]"):
        vet.session.splice(path, at, at + 2, b"x", old=b"~x")
    assert design.changed(vet) == set()


def test_operations_are_not_raw(tmp_path, monkeypatch):
    """No Yosys and no waiver: the operations still work, so they do not go through the raw door."""
    no_yosys(monkeypatch)
    design = Design(tmp_path / "d", {"ab": AB, "top": AB_TOP})
    vet = design.vet()
    vet.module("a").add_wire("spare")
    vet.punch_port("dbg_i", "input", to="top.u_b")
    result = vet.finish()
    assert result.equivalence == [] and result.ok, result.report()


# --- T4 -------------------------------------------------------------------------------------------


def test_an_archive_of_an_older_version_is_refused(tmp_path, monkeypatch):
    no_yosys(monkeypatch)
    assert archive.FORMAT == 2
    design = Design(tmp_path / "d", {"ab": AB, "top": AB_TOP})
    vet = design.vet()
    vet.module("a").add_wire("spare")
    saved = tmp_path / "session.zip"
    vet.save(saved)
    old = tmp_path / "old.zip"
    with zipfile.ZipFile(saved) as src, zipfile.ZipFile(old, "w") as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "session.json":
                index = json.loads(data)
                assert index["format"] == 2
                index["format"] = 1
                data = json.dumps(index).encode()
            dst.writestr(info, data)
    with pytest.raises(Exception, match="format"):
        Vet.restore(old)
    assert Vet.restore(saved).finish().ok


# --- T5 -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("waiver", [f"a{chr(code)}b" for code in (0x2028, 0x2029, 0x0C, 0x0B, 0x85)])
def test_a_waiver_has_no_line_separator(tmp_path, monkeypatch, waiver):
    no_yosys(monkeypatch)
    design = Design(tmp_path / "d", {"ab": AB, "top": AB_TOP})
    vet = design.vet()
    with pytest.raises(VetError, match="waiver"):
        design.replace(vet, "ab", b"~x", b"x", waiver=waiver)
    vet.punch_port("test_en_i", "input", to="top.u_b")
    with pytest.raises(VetError, match="waiver"):
        vet.module("b").override_net("z", "1'b0", enable="test_en_i", waiver=waiver)


# --- second review: U1 to U7 ----------------------------------------------------------------------
#
# The second review broke the fixes: the check of T2 counted keywords and did not compare the text
# outside the module, and the guard of T3 could be gone round by public names. The rules, stated in
# the docstring of this file under "FROM THE SECOND REVIEW", close what it found.


def ab(tmp_path: Path) -> Design:
    return Design(tmp_path / "d", {"ab": AB, "top": AB_TOP})


def bug_in_b(design: Design, vet, call) -> None:
    path = design.path("ab")
    at = vet.text(path).index(b"~x")
    call(path, at, at + 2, b"x", old=b"~x")


def test_the_session_has_no_public_way_round_the_guard(tmp_path, monkeypatch):
    """U1."""
    no_yosys(monkeypatch)
    design = ab(tmp_path)
    vet = design.vet()
    public = [name for name in dir(vet.session) if not name.startswith("_")]
    assert "queue_edit" not in public and "splice_guard" not in public, public
    with pytest.raises(VetError, match="session"):
        Vet(vet.session)
    with pytest.raises(Exception, match=r"hdl-vet\[equiv\]"):
        bug_in_b(design, vet, vet.session.splice)
    assert design.changed(vet) == set()


@needs_yosys
def test_a_second_vet_cannot_take_the_marks_away(tmp_path):
    """U1."""
    design = ab(tmp_path)
    vet = design.vet()
    with pytest.raises(VetError):
        Vet(vet.session).splice(design.path("ab"), 0, 0, b"")
    bug_in_b(design, vet, vet.session.splice)
    assert records(vet.finish()) == [("b", "refuted")]


HIER_A = (
    "module a (\n  input  logic x,\n  output logic y\n);\n  logic w;\n  assign w = x;\n  assign y = x;\nendmodule\n"
)
HIER_TOP = (
    "module top (\n"
    "  input  logic x,\n"
    "  output logic y,\n"
    "  output logic z\n"
    ");\n"
    "  a u_a (.x(x), .y(y));\n"
    "  assign z = u_a.w;\n"
    "endmodule\n"
)


@needs_yosys
def test_a_module_that_is_read_from_outside_is_never_proved(tmp_path):
    """U2: `top` reads `u_a.w`. The outputs of `a` are equal, and the design is not."""
    design = Design(tmp_path / "d", {"a": HIER_A, "top": HIER_TOP})
    vet = design.vet()
    design.replace(vet, "a", b"assign w = x;", b"assign w = ~x;")
    result = vet.finish()
    (record,) = result.equivalence
    assert (record.module, record.status) == ("a", "error"), result.report()
    assert "u_a.w" in record.detail
    assert not result.ok


@needs_yosys
def test_an_override_in_a_module_that_is_read_from_outside_is_never_proved(tmp_path):
    """U2 holds for every module that is compared."""
    design = Design(tmp_path / "d", {"a": HIER_A, "top": HIER_TOP})
    vet = design.vet()
    vet.punch_port("test_en_i", "input", to="top.u_a")
    vet.module("a").override_net("w", "1'b0", enable="test_en_i")
    result = vet.finish()
    assert records(result) == [("a", "error")], result.report()
    assert not result.ok


UNDEF = (
    "`define INV\n"
    "module a (\n"
    "  input  logic x,\n"
    "  output logic y\n"
    ");\n"
    "  assign y = x;\n"
    "endmodule `undef INV\n"
    "module b (\n"
    "  input  logic x,\n"
    "  output logic z\n"
    ");\n"
    "`ifdef INV\n"
    "  assign z = ~x;\n"
    "`else\n"
    "  assign z = x;\n"
    "`endif\n"
    "endmodule\n"
)


@pytest.mark.parametrize("yosys", [True, False])
def test_a_comment_that_swallows_the_text_after_the_module_is_outside(tmp_path, monkeypatch, yosys):
    """U3: `endmodule` becomes `endmodule //`, and the `undef` on that line is a comment."""
    if not yosys:
        no_yosys(monkeypatch)
    design = Design(tmp_path / "d", {"ab": UNDEF, "top": AB_TOP})
    vet = design.vet()
    path = design.path("ab")
    at = vet.text(path).index(b"endmodule `undef") + len(b"endmodul")
    with pytest.raises(VetError, match="outside"):
        vet.splice(path, at, at + 1, b"e //", old=b"e")
    assert design.changed(vet) == set()


MACROS = "`define TAIL endmodule module b2 (input logic q); endmodule\n`define ONE 1'b1\n" + AB


@pytest.mark.parametrize("text", [b"`TAIL\n", b"  wire spare = `ONE;\n"])
def test_a_macro_use_in_a_raw_splice_is_outside(tmp_path, monkeypatch, text):
    """U4: what a macro expands to is not in the text of the splice."""
    no_yosys(monkeypatch)
    design = Design(tmp_path / "d", {"ab": MACROS, "top": AB_TOP})
    vet = design.vet()
    path = design.path("ab")
    at = vet.text(path).index(b"endmodule")
    with pytest.raises(VetError, match="outside"):
        vet.splice(path, at, at, text)
    assert design.changed(vet) == set()


def test_a_blank_in_a_define_is_not_blanks_only(tmp_path, monkeypatch):
    """U5: the header holds a macro with a string; `top` compares it with "a b"."""
    no_yosys(monkeypatch)
    top = AB_TOP.replace("module top (", '`include "inc.svh"\nmodule top (').replace(
        "  a u_a (.x(x), .y(y));", '  a u_a (.x((`S == "a b") ? x : ~x), .y(y));'
    )
    assert "inc.svh" in top and "`S" in top
    design = Design(tmp_path / "d", {"ab": AB, "top": top})
    header = design.root / "rtl" / "inc.svh"
    header.write_bytes(b'`define S `"a b`"\n')
    vet = design.vet()
    at = vet.text(header).index(b"a b") + 1
    with pytest.raises(VetError, match="outside"):
        vet.splice(header, at, at + 1, b"   ", old=b" ")


def test_a_bind_in_a_raw_splice_is_outside(tmp_path, monkeypatch):
    """U6: a `bind` puts logic into another module."""
    no_yosys(monkeypatch)
    design = ab(tmp_path)
    vet = design.vet()
    path = design.path("ab")
    at = vet.text(path).index(b"endmodule")
    with pytest.raises(VetError, match="outside"):
        vet.splice(path, at, at, b"  bind b a u_bound (.x(x), .y());\n")


@needs_yosys
def test_a_splice_in_a_child_and_one_in_its_parent_are_both_proved(tmp_path):
    """U7: a module that is proved with no enable is not handed to the engine as inert."""
    design = Design(tmp_path / "d")
    vet = design.vet()
    design.replace(vet, "timer", b"assign irq_o = &count_o;", b"assign irq_o = &(count_o);")
    design.replace(vet, "core", b"assign rst_o    = rst_sync_n;", b"assign rst_o    = (rst_sync_n);")
    result = vet.finish()
    assert {r.module: r.status for r in result.equivalence} == {"timer": "proved", "core": "proved"}, result.report()
    assert result.ok, result.report()


# --- third review: V1 to V5 -----------------------------------------------------------------------
#
# All earlier breaks stayed closed. The third review found five more causes. Two belong to the design
# (V1) and to honest scripts (V2, V3); the others need a script that works against the check, and are
# closed where it is cheap (V4, V5). See "FROM THE THIRD REVIEW" in the docstring.

BIND_A = (
    "module a (\n"
    "  input  logic x,\n"
    "  output logic y\n"
    ");\n"
    "  logic k;\n"
    "  wire  t;\n"
    "  assign k = x;\n"
    "  assign y = t;\n"
    "endmodule\n"
)
DRV = "module drv (\n  input  logic i,\n  output logic o\n);\n  assign o = i;\nendmodule\n"
BIND_TOP = "module top (\n  input  logic x,\n  output logic y\n);\n  a u_a (.x(x), .y(y));\n  BIND\nendmodule\n"


@needs_yosys
@pytest.mark.parametrize("bind", ["bind u_a drv u_d (.i(k), .o(t));", "bind a drv u_d (.i(k), .o(t));"])
def test_a_module_with_a_bind_from_another_module_is_never_proved(tmp_path, bind):
    """V1: the bind is written in `top`; a comparison of `a` alone does not read it."""
    design = Design(tmp_path / "d", {"drv": DRV, "a": BIND_A, "top": BIND_TOP.replace("BIND", bind)})
    vet = design.vet()
    design.replace(vet, "a", b"assign k = x;", b"assign k = ~x;")
    result = vet.finish()
    (record,) = result.equivalence
    assert (record.module, record.status) == ("a", "error"), result.report()
    assert "bind" in record.detail
    assert not result.ok


def comment_pair(design: Design, vet, between) -> None:
    """The two comment splices of T1, from one read of the text, with a call of the script between them."""
    path = design.path("leaf")
    text = vet.text(path)
    first = text.index(b"// p")
    second = text.index(b"// q */")
    vet.splice(path, first, first + 4, b"/* p", old=b"// p")
    between(vet)
    text = vet.text(path)
    second = text.index(b"// q */")
    vet.splice(path, second, second + 7, b"// q", old=b"// q */")


def add_a_file(vet) -> None:
    extra = Path(vet.session.layout_root) / "rtl" / "extra.sv"
    extra.write_bytes(b"module extra (input logic q);\nendmodule\n")
    vet.add_file(extra)


@needs_yosys
@pytest.mark.parametrize(
    "between",
    [lambda vet: vet.session.check(), lambda vet: vet.check(), add_a_file, lambda vet: vet.session.refresh()],
    ids=["session.check", "vet.check", "add_file", "refresh"],
)
def test_a_call_between_two_comment_splices_does_not_part_them(tmp_path, between):
    """V2: whatever the script calls between the two, the logic change is judged."""
    design = Design(tmp_path / "d", {"leaf": LEAF, "top": TOP})
    vet = design.vet()
    comment_pair(design, vet, between)
    result = vet.finish()
    assert ("leaf", "refuted") in records(result), result.report()
    assert not result.ok


@needs_yosys
def test_a_splice_that_a_rollback_took_back_is_not_counted(tmp_path):
    """V2: the comment opener is rolled back; the later change of logic stands by itself."""
    design = Design(tmp_path / "d", {"leaf": LEAF, "top": TOP})
    vet = design.vet()
    path = design.path("leaf")
    mark = vet.session.checkpoint()
    at = vet.text(path).index(b"// p")
    vet.splice(path, at, at + 4, b"/* p", old=b"// p")
    vet.session.rollback(mark)
    at = vet.text(path).index(b"& b")
    vet.splice(path, at, at + 3, b"& b & 1'b0", old=b"& b")
    result = vet.finish()
    assert records(result) == [("leaf", "refuted")], result.report()


def test_the_count_of_reloads_cannot_be_set(tmp_path):
    design = Design(tmp_path / "d", {"leaf": LEAF, "top": TOP})
    vet = design.vet()
    with pytest.raises(AttributeError):
        vet.session.reloads += 1


class Whole(int):
    """An integer of another type, as a numeric library gives."""


@needs_yosys
@pytest.mark.parametrize("door", ["vet", "session"])
@pytest.mark.parametrize("form", ["int subclass", "bytearray", "memoryview"])
def test_an_argument_of_a_near_type_does_not_slip_past(tmp_path, door, form):
    """V3: refused, or judged. Never applied without a mark."""
    design = ab(tmp_path)
    vet = design.vet()
    path = design.path("ab")
    at = vet.text(path).index(b"~x")
    start = Whole(at) if form == "int subclass" else at
    text = {"int subclass": b"x", "bytearray": bytearray(b"x"), "memoryview": memoryview(b"x")}[form]
    call = vet.splice if door == "vet" else vet.session.splice
    try:
        call(path, start, at + 2, text, old=b"~x")
    except Exception as e:
        assert "Traceback" not in str(e)
        assert design.changed(vet) == set()
        return
    assert records(vet.finish()) == [("b", "refuted")]


def test_a_vet_cannot_be_copied(tmp_path):
    """V4."""
    import copy

    vet = ab(tmp_path).vet()
    for make in (copy.copy, copy.deepcopy):
        with pytest.raises(VetError, match="cop"):
            make(vet)


def test_a_checkpoint_of_another_session_is_refused(tmp_path, monkeypatch):
    """V5."""
    import dataclasses

    no_yosys(monkeypatch)
    design = ab(tmp_path)
    vet = design.vet()
    other = Session(design.filelist, top="top")
    with pytest.raises(Exception, match="checkpoint"):
        vet.session.rollback(other.checkpoint())
    mark = vet.session.checkpoint()
    if dataclasses.is_dataclass(mark):
        with pytest.raises(Exception, match="checkpoint"):
            vet.session.rollback(dataclasses.replace(mark))
    vet.session.rollback(mark)
    assert vet.finish().ok


# --- fourth review: W1 to W6 ----------------------------------------------------------------------
#
# Every earlier break stayed closed, and 21 orders of calls and five save and resume pairs agreed
# with the plain case. Three more ways through, and a false failure. See "FROM THE FOURTH REVIEW".

HIDDEN = (
    "/* module leaf */ module leaf // */\n"
    "  (input logic a, input logic b, output logic y);\n"
    "  assign y = a & b;\n"
    "endmodule // */\n"
)
HIDDEN_NEW = b"// */ (input logic a, input logic b, output logic y); assign y = a | b; endmodule /*"


@needs_yosys
@pytest.mark.parametrize("order", ["file level first", "module first"])
def test_a_splice_at_file_level_and_one_in_a_module_are_judged_together(tmp_path, order):
    """W1: each alone changes only comments; together they give the module another body."""
    design = Design(tmp_path / "d", {"leaf": HIDDEN, "top": TOP})
    vet = design.vet()
    path = design.path("leaf")
    text = vet.text(path)
    assert text.startswith(b"/* module leaf */")
    at = text.index(b"// */")
    calls = [
        lambda: vet.splice(path, 0, 17, b"module leaf /*", old=b"/* module leaf */"),
        lambda: vet.splice(path, at, at + 5, HIDDEN_NEW, old=b"// */"),
    ]
    if order == "module first":
        calls.reverse()
    try:
        for call in calls:
            call()
    except VetError as e:
        assert "outside" in str(e)
        return
    result = vet.finish()
    assert not result.ok, result.report()
    assert ("leaf", "refuted") in records(result) or result.outside, result.report()


@needs_yosys
@pytest.mark.parametrize(
    "bind",
    [
        "bind /* tap */ u_a drv u_d (.i(k), .o(t));",
        "bind // tap\n u_a drv u_d (.i(k), .o(t));",
        "bind top . u_a drv u_d (.i(k), .o(t));",
        "bind \\u_a  drv u_d (.i(k), .o(t));",
        "bind $root.top.u_a drv u_d (.i(k), .o(t));",
    ],
)
def test_a_bind_is_found_however_its_target_is_written(tmp_path, bind):
    """W2."""
    design = Design(tmp_path / "d", {"drv": DRV, "a": BIND_A, "top": BIND_TOP.replace("BIND", bind)})
    vet = design.vet()
    design.replace(vet, "a", b"assign k = x;", b"assign k = ~x;")
    result = vet.finish()
    assert records(result) == [("a", "error")], result.report()
    assert not result.ok


CNT = (
    "module cnt #(\n"
    "  parameter int W    = 4,\n"
    "  parameter int STEP = 1\n"
    ") (\n"
    "  input  logic         clk_i,\n"
    "  output logic [W-1:0] q\n"
    ");\n"
    "  always_ff @(posedge clk_i) q <= q + W'(STEP);\n"
    "endmodule\n"
)
CNT_TOP = (
    "module top (\n"
    "  input  logic       clk_i,\n"
    "  output logic [3:0] q\n"
    ");\n"
    "  cnt u_c (.clk_i(clk_i), .q(q));\n"
    "endmodule\n"
)


@needs_yosys
def test_a_changed_default_of_a_parameter_is_never_proved(tmp_path):
    """W3: the input design counts in steps of 1, the edited one in steps of 2."""
    design = Design(tmp_path / "d", {"cnt": CNT, "top": CNT_TOP})
    vet = design.vet()
    design.replace(vet, "cnt", b"STEP = 1", b"STEP = 2")
    vet.permit("param-changed")
    result = vet.finish()
    (record,) = result.equivalence
    assert record.module == "cnt" and record.status in ("refuted", "error"), result.report()
    assert not result.ok


@pytest.mark.parametrize("yosys", [True, False])
def test_a_comment_and_then_a_waived_change_are_waived(tmp_path, monkeypatch, yosys):
    """W4: the two splices wait together; the waiver of the one that changes logic is enough."""
    if not yosys:
        no_yosys(monkeypatch)
    elif not equiv.available():
        pytest.skip("Yosys (hdl-vet[equiv]) is missing")
    design = Design(tmp_path / "d", {"cnt": CNT, "top": CNT_TOP})
    vet = design.vet()
    path = design.path("cnt")
    text = vet.text(path)
    head = text.index(b"module cnt")
    step = text.index(b"W'(STEP)")
    vet.splice(path, head, head, b"// a counter\n")
    vet.splice(path, step, step + 8, b"W'(STEP + 1)", old=b"W'(STEP)", waiver="counts faster: see TB-4")
    result = vet.finish()
    (record,) = result.equivalence
    assert record.status == "waived", result.report()
    assert result.ok, result.report()


def test_the_refusal_names_the_keyword_that_makes_it_outside(tmp_path, monkeypatch):
    """W5."""
    no_yosys(monkeypatch)
    design = Design(tmp_path / "d", {"cnt": CNT, "top": CNT_TOP})
    vet = design.vet()
    path = design.path("cnt")
    at = vet.text(path).index(b"endmodule")
    with pytest.raises(VetError, match="outside") as e:
        vet.splice(path, at, at + 9, b"  wire spare;\nendmodule", old=b"endmodule")
    assert "endmodule" in str(e.value)


@needs_yosys
def test_the_finish_of_the_session_does_not_pass_a_mark_without_a_proof(tmp_path):
    """W6: `vet.session.finish()` runs no comparison. With a mark it cannot say ok."""
    design = Design(tmp_path / "d", {"cnt": CNT, "top": CNT_TOP})
    vet = design.vet()
    design.replace(vet, "cnt", b"W'(STEP)", b"W'(STEP + 1)")
    result = vet.session.check()
    assert not result.ok
    assert [(r.module, r.status) for r in result.equivalence] == [("cnt", "error")], result.report()
    assert records(vet.finish()) == [("cnt", "refuted")]


# --- fifth review: X1, X2 -------------------------------------------------------------------------
#
# Every earlier break stayed closed; 120 random sets of waiting splices gave no break; ordinary use
# is right. One class was left: a directive that is written INSIDE a module reaches the modules after
# it in the file, and a splice of its argument was compared as that module only.

DIRECTIVES = (
    "module a (\n"
    "  input  logic x,\n"
    "  output logic y\n"
    ");\n"
    "`define INV 0\n"
    "// + 1\n"
    "`define Q\n"
    "`ifdef NOPE\n"
    "`define W\n"
    "`endif\n"
    "`undef Q\n"
    "  assign y = x;\n"
    "endmodule\n"
    "module b (\n"
    "  input  logic x,\n"
    "  output logic z\n"
    ");\n"
    "`ifdef W\n"
    "  assign z = x;\n"
    "`else\n"
    "  assign z = `INV ? ~x : x;\n"
    "`endif\n"
    "endmodule\n"
)


@pytest.mark.parametrize(
    "old, new, grow",
    [
        (b"`define INV 0", b"`define INV 1", 0),
        (b"INV 0", b"INV 1", 0),
        (b"0\n// + 1", b"1\n// + 1", 0),
        (b"`ifdef NOPE", b"`ifdef Q", 0),
        (b"NOPE", b"Q", 0),
        (b"`undef Q", b"`undef INV", 0),
        (b"INV 0\n", b"INV 0 \\\n", 0),
    ],
    ids=["define", "define body", "define body and next line", "ifdef", "ifdef name", "undef", "continue"],
)
@pytest.mark.parametrize("yosys", [True, False])
def test_a_splice_on_the_line_of_a_directive_is_outside(tmp_path, monkeypatch, old, new, grow, yosys):
    """X1: also when the old and the new text are plain names, and also inside a module."""
    if not yosys:
        no_yosys(monkeypatch)
    design = Design(tmp_path / "d", {"ab": DIRECTIVES, "top": AB_TOP})
    vet = design.vet()
    path = design.path("ab")
    text = vet.text(path)
    assert text.count(old) == 1, old
    at = text.index(old)
    with pytest.raises(VetError, match="outside"):
        vet.splice(path, at, at + len(old), new, old=old)
    assert design.changed(vet) == set()


def test_an_insertion_at_the_end_of_a_directive_line_is_outside(tmp_path, monkeypatch):
    """X1: a backslash put before the line break makes the next line part of the macro."""
    no_yosys(monkeypatch)
    design = Design(tmp_path / "d", {"ab": DIRECTIVES, "top": AB_TOP})
    vet = design.vet()
    path = design.path("ab")
    at = vet.text(path).index(b"`define INV 0") + len(b"`define INV 0")
    with pytest.raises(VetError, match="outside"):
        vet.splice(path, at, at, b" \\")


@needs_yosys
def test_a_splice_beside_the_directives_is_still_inside(tmp_path):
    design = Design(tmp_path / "d", {"ab": DIRECTIVES, "top": AB_TOP})
    vet = design.vet()
    design.replace(vet, "ab", b"  assign y = x;", b"  assign y = x & 1'b1;")
    result = vet.finish()
    assert records(result) == [("a", "proved")], result.report()


def test_the_check_of_the_session_keeps_the_outside_waivers(tmp_path, monkeypatch):
    """X2."""
    no_yosys(monkeypatch)
    design = Design(tmp_path / "d", {"ab": DIRECTIVES, "top": AB_TOP})
    vet = design.vet()
    design.replace(vet, "ab", b"INV 0", b"INV 1", waiver="the other branch: see TB-2")
    for result in (vet.session.check(), vet.check()):
        assert [(o.file, o.waiver) for o in result.outside] == [("rtl/ab.sv", "the other branch: see TB-2")]
        assert "waived: splice rtl/ab.sv" in result.report()
