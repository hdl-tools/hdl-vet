"""`Module.replace_expr`: what the review found (issue #42).

The review of rules K1 to K10 (test_replace_expr.py) ran without Yosys, with a waiver on every
call. It found two edits that were wrong and that the sign-off called ok, and one refusal that
could be walked around.

RULES

K11  A comment between the `=` and the expression is not thrown away. The call is refused, and
     the message says "comment". (`assign rst_o = /* c */ rst_sync_n;` lost its comment.)
K12  A statement with a conditional directive in it is refused, also when the directive comes
     after the last token of the expression that the current configuration has. The message says
     "directive". (`assign rst_o = rst_sync_n` / `` `ifdef SIM `` / `| seen` / `` `endif `` / `;`
     got a new expression in front of the old tail: with SIM the module was `rst_slow_n | seen`.)
K13  An expression that reads something that cannot be followed to its nets is refused, as
     `override_net` refuses it: a call of a function of the design, a `let`. Such a thing can read
     the target, and the loop guard of K7 cannot see it. The message names what cannot be followed.
     A system function (`$clog2`, `$signed`) can be followed: it reads only its arguments.

STATED LIMITS

- An expression of many thousand terms, or with thousands of nested parentheses, can end the
  process or raise an error of Python that is no `VetError`. The reader of expressions has no
  bound of its own. `add_assign` has the same limit.
- The check of rule K8 compares tokens. A later raw splice that puts parentheses around the
  expression fails the sign-off, although the function is the same.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet import equiv
from hdl_vet.ops import DriftError, Vet, VetError
from override_design import CORE, FILES, Design, statuses

OLD = "  assign rst_o    = rst_sync_n;\n"
WHY = "covered by the test tb_rst"


@pytest.fixture(autouse=True)
def no_yosys(monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)


def variant(tmp_path: Path, new: str, more: str = "") -> Design:
    assert OLD in CORE
    text = CORE.replace(OLD, new)
    if more:
        text = text.replace("  logic       floating;\n", "  logic       floating;\n" + more)
    return Design(tmp_path / "design", {**FILES, "core": text})


def refused(design: Design, target: str, expr: str) -> str:
    vet: Vet = design.vet()
    with pytest.raises(VetError) as caught:
        vet.module("core").replace_expr(target, expr, waiver=WHY)
    assert not isinstance(caught.value, DriftError)
    message = str(caught.value)
    assert message.startswith("replace_expr core: "), message
    assert design.changed(vet) == set()
    assert statuses(vet) == []
    return message


def applied(design: Design, target: str, expr: str) -> str:
    vet: Vet = design.vet()
    vet.module("core").replace_expr(target, expr, waiver=WHY)
    assert statuses(vet) == [("replace_expr", "core", "applied")]
    result = vet.finish()
    assert result.ok, result.report()
    return design.text(vet, "core")


# --- K11 --------------------------------------------------------------------------------------------

COMMENTS = {
    "block": "  assign rst_o = /* c */ rst_sync_n;\n",
    "line": "  assign rst_o = // c\n    rst_sync_n;\n",
    "two": "  assign rst_o = /* a */ /* b */ rst_sync_n;\n",
}


@pytest.mark.parametrize("comment", COMMENTS)
def test_a_comment_in_front_of_the_expression_is_refused(tmp_path, comment):
    message = refused(variant(tmp_path, COMMENTS[comment]), "rst_o", "rst_slow_n")
    assert "comment" in message, message


def test_a_comment_in_front_of_the_equals_sign_stays(tmp_path):
    text = applied(variant(tmp_path, "  assign rst_o /* rst_o = */ = rst_sync_n;\n"), "rst_o", "rst_slow_n")
    assert "  assign rst_o /* rst_o = */ = rst_slow_n;\n" in text


def test_a_comment_inside_the_expression_goes_with_it(tmp_path):
    old = "  assign rst_o = rst_sync_n /* and */\n    & rst_slow_n;\n"
    text = applied(variant(tmp_path, old), "rst_o", "rst_slow_n")
    assert "  assign rst_o = rst_slow_n;\n" in text


# --- K12 --------------------------------------------------------------------------------------------

DIRECTIVES = {
    "a-tail": "  assign rst_o = rst_sync_n\n`ifdef SIM\n    | seen\n`endif\n    ;\n",
    "a-tail-on-one-line": "  assign rst_o = rst_sync_n `ifdef SIM & seen `endif ;\n",
    "another-statement": "  assign rst_o = rst_sync_n\n`ifdef SIM\n    ; assign floating = seen\n`endif\n    ;\n",
    "an-empty-branch": "  assign rst_o = rst_sync_n\n`ifdef SIM\n`endif\n    ;\n",
    "a-head": "  assign rst_o =\n`ifdef SIM\n    seen |\n`endif\n    rst_sync_n;\n",
}


@pytest.mark.parametrize("shape", DIRECTIVES)
def test_a_statement_with_a_conditional_directive_is_refused(tmp_path, shape):
    message = refused(variant(tmp_path, DIRECTIVES[shape]), "rst_o", "rst_slow_n")
    assert "directive" in message, message


def test_a_directive_behind_the_statement_is_no_part_of_it(tmp_path):
    old = "  assign rst_o = rst_sync_n;\n`ifdef SIM\n  assign floating = seen;\n`endif\n"
    text = applied(variant(tmp_path, old), "rst_o", "rst_slow_n")
    assert "  assign rst_o = rst_slow_n;\n`ifdef SIM\n  assign floating = seen;\n`endif\n" in text


# --- K13 --------------------------------------------------------------------------------------------

READS = "  function automatic logic f(input logic a);\n    return a & rst_o;\n  endfunction\n"
THROUGH = "  logic t1;\n  assign t1 = ~rst_o;\n  function automatic logic h();\n    return t1;\n  endfunction\n"
PLAIN = "  function automatic logic g(input logic a);\n    return ~a;\n  endfunction\n"


@pytest.mark.parametrize(
    ("more", "expr", "name"),
    [(READS, "f(seen)", "f"), (THROUGH, "h()", "h"), (PLAIN, "g(seen)", "g"), ("  let L = rst_o;\n", "L", "L")],
)
def test_an_expression_that_cannot_be_followed_is_refused(tmp_path, more, expr, name):
    message = refused(variant(tmp_path, OLD, more), "rst_o", expr)
    assert repr(name) in message and "follow" in message, message


@pytest.mark.parametrize("expr", ["$signed(rst_slow_n)", "1'(rst_slow_n)", "rst_slow_n & seen"])
def test_an_expression_that_can_be_followed_is_applied(tmp_path, expr):
    text = applied(variant(tmp_path, OLD), "rst_o", expr)
    assert f"  assign rst_o    = {expr};\n" in text
