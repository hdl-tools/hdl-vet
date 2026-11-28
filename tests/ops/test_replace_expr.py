"""`Module.replace_expr`: another expression for a continuous assign (phase 6; issue #42).

THE PROBLEM

A script that wants another expression for `assign irq_o = ...;` had two ways. `remove_assign`
and `add_assign` are two records and leave the net undriven in between. A raw splice needs byte
offsets of the text. Neither names what the script means: this driver gets this expression.

OWNER DECISIONS

- 2026-11-24: `replace_expr` follows the rule of a raw splice. The module is compared with the
  module of the input design. Equal is "proved". Not equal fails unless a waiver names the check
  that covers the change.
- 2026-11-28: one call replaces the whole expression of the one `assign target = ...;`. `was=` is
  optional and must equal the old expression, as `remove_assign` has it. Only a continuous assign
  with a plain name as its target. A target that is driven in an `always` block, by an instance,
  by several assigns or with a select is refused.

RULES

K1  `Module.replace_expr(target, expr, *, was=None, waiver=None)` writes `expr` in the place of
    the right side of the one continuous assign whose target is `target`. Nothing else of the
    statement changes: the keyword, the target, the spacing in front of the expression, the
    semicolon and a comment behind it stay as they are. The record is `replace_expr` with the
    module as its target.
K2  `was`. When it is given and is not the expression that the assign has (compared as
    `remove_assign` compares it), the call raises `DriftError`, and the message has both
    expressions. Nothing is written.
K3  The same expression. When the assign has `expr` already (compared in the same way), the call
    is "skipped": no text changes, no mark, and a waiver leaves no record.
K4  The mark. A call that changes the text marks the module, whatever the two expressions are:
    a name in the place of a name changes the function too. The sign-off compares the marked
    module with the module of the input design (rule S3 of tests/ops/test_raw_splice.py). The
    label of the operation is `replace_expr <module>.<target>`.
K5  The waiver is one line of text that is not empty, by keyword, as for `add_assign` (rule L4 of
    tests/ops/test_logic_ops.py). The comparison still runs with a waiver.
K6  Without Yosys a call that changes the text and has no waiver is refused when called. The
    message starts with `replace_expr <module>` and names `hdl-vet[equiv]`. Nothing is written.
    With a waiver the call is applied, and the record of the module is "waived".
K7  What is refused, each with `VetError` that starts with `replace_expr <module>: ` and with
    nothing written:
    - `target` is not a plain identifier (a select, a concatenation, an escaped spelling);
    - `expr` is not one expression (the checks of `add_assign`);
    - the module has no continuous assign to `target`: the message says so, and says what drives
      the name when it is driven in an `always` block or by an instance, or that the module has
      no such name (with the nearest name);
    - the module has more than one assign to `target`, or assigns to selects of it;
    - the assign is inside a generate block, comes from a macro or an include, or shares its
      statement with other assignments (`assign a = x, b = y;`);
    - `expr` reads `target`, also through other nets of the module: that would make a loop
      (the guard of `override_net`).
K8  The sign-off checks that the assign to `target` has the expression `expr` at the end of the
    run. A later operation that removes or changes it makes the sign-off fail with a line that
    names the operation and the target.
K9  A saved session keeps the mark and the waiver of the call.
K10 A call that fails, and a group that is left by an exception, leave no mark and no text.

STATED LIMITS

- A replacement and the replacement back are two "applied" records and no "cancelled" pair. The
  module is compared with the input and is "proved" then.
- Procedural drivers are not edited. Use a raw splice.
- The new expression is not checked for its width against the target. The diagnostics of the
  sign-off report what the compiler reports.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet import equiv
from hdl_vet.ops import DriftError, Vet, VetError
from override_design import CORE, FILES, Design, needs_yosys, records, statuses

pytestmark = [pytest.mark.equiv]

OLD = "irq_raw & en_i[1] & seen & (split == en_i)"
SAME_FUNCTION = "(split == en_i) & seen & en_i[1] & irq_raw"
OTHER = "irq_raw & en_i[1]"
WHY = "covered by the test tb_irq"


@pytest.fixture
def design(tmp_path: Path) -> Design:
    return Design(tmp_path / "design")


def no_yosys(monkeypatch) -> None:
    monkeypatch.setattr(equiv, "available", lambda: False)


def core(vet: Vet):
    return vet.module("core")


def variant(tmp_path: Path, old: str, new: str) -> Design:
    assert old in CORE
    return Design(tmp_path / "design", {**FILES, "core": CORE.replace(old, new)})


def refused(vet: Vet, design: Design, *args: object, **options: object) -> str:
    with pytest.raises(VetError) as caught:
        core(vet).replace_expr(*args, **options)
    assert not isinstance(caught.value, DriftError)
    message = str(caught.value)
    assert message.startswith("replace_expr core: "), message
    assert design.changed(vet) == set()
    assert statuses(vet) == []
    return message


# --- K1: the text -----------------------------------------------------------------------------------


def test_the_expression_is_replaced_and_nothing_else(design, monkeypatch):
    no_yosys(monkeypatch)
    vet = design.vet()
    core(vet).replace_expr("irq_o", OTHER, waiver=WHY)
    assert design.text(vet, "core") == CORE.replace(f"assign irq_o    = {OLD};", f"assign irq_o    = {OTHER};")
    assert statuses(vet) == [("replace_expr", "core", "applied")]
    assert design.changed(vet) == {"core"}


def test_a_comment_behind_the_statement_stays(tmp_path, monkeypatch):
    no_yosys(monkeypatch)
    design = variant(tmp_path, "assign rst_o    = rst_sync_n;", "assign rst_o    = rst_sync_n;  // to the pad")
    vet = design.vet()
    core(vet).replace_expr("rst_o", "rst_slow_n", waiver=WHY)
    assert "  assign rst_o    = rst_slow_n;  // to the pad\n" in design.text(vet, "core")


def test_an_expression_over_several_lines_is_replaced_as_a_whole(tmp_path, monkeypatch):
    no_yosys(monkeypatch)
    design = variant(tmp_path, f"assign irq_o    = {OLD};", "assign irq_o    = irq_raw\n                  & seen;")
    vet = design.vet()
    core(vet).replace_expr("irq_o", "irq_raw", waiver=WHY)
    assert "  assign irq_o    = irq_raw;\n" in design.text(vet, "core")


# --- K2: was ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("was", [OLD, "irq_raw&en_i[1]&seen&(split==en_i)"])
def test_was_that_is_the_old_expression_is_accepted(design, monkeypatch, was):
    no_yosys(monkeypatch)
    vet = design.vet()
    core(vet).replace_expr("irq_o", OTHER, was=was, waiver=WHY)
    assert statuses(vet) == [("replace_expr", "core", "applied")]


def test_was_that_is_another_expression_is_a_drift(design, monkeypatch):
    no_yosys(monkeypatch)
    vet = design.vet()
    with pytest.raises(DriftError) as caught:
        core(vet).replace_expr("irq_o", OTHER, was="irq_raw", waiver=WHY)
    message = str(caught.value)
    assert message.startswith("replace_expr core: "), message
    assert OLD in message and "irq_raw" in message, message
    assert design.changed(vet) == set()
    assert statuses(vet) == []


# --- K3: the same expression ------------------------------------------------------------------------


@pytest.mark.parametrize("expr", [OLD, "irq_raw&en_i[1]&seen&(split==en_i)"])
def test_the_same_expression_is_skipped(design, monkeypatch, expr):
    no_yosys(monkeypatch)
    vet = design.vet()
    core(vet).replace_expr("irq_o", expr)
    assert statuses(vet) == [("replace_expr", "core", "skipped")]
    assert design.changed(vet) == set()
    result = vet.finish()
    assert result.ok, result.report()
    assert result.equivalence == []


# --- K4, K5: the mark and the comparison ------------------------------------------------------------


@needs_yosys
def test_an_expression_with_the_same_function_is_proved(design):
    vet = design.vet()
    core(vet).replace_expr("irq_o", SAME_FUNCTION)
    result = vet.finish()
    assert result.ok, result.report()
    assert records(result) == [("core", "proved")]
    assert result.equivalence[0].operations == ["replace_expr core.irq_o"]


@needs_yosys
def test_an_expression_with_another_function_fails_the_sign_off(design):
    vet = design.vet()
    core(vet).replace_expr("irq_o", OTHER)
    result = vet.finish()
    assert not result.ok
    assert records(result) == [("core", "refuted")]


@needs_yosys
def test_a_name_in_the_place_of_a_name_is_compared_too(design):
    vet = design.vet()
    core(vet).replace_expr("rst_o", "rst_slow_n")
    result = vet.finish()
    assert not result.ok
    assert records(result) == [("core", "refuted")]


@needs_yosys
def test_a_waiver_lets_another_function_through_and_the_comparison_still_runs(design):
    vet = design.vet()
    core(vet).replace_expr("irq_o", OTHER, waiver=WHY)
    result = vet.finish()
    assert result.ok, result.report()
    assert records(result) == [("core", "waived")]
    assert WHY in result.report()


@needs_yosys
def test_a_replacement_and_the_replacement_back_are_proved(design):
    """Stated limit: two applied records, no cancelled pair."""
    vet = design.vet()
    core(vet).replace_expr("irq_o", OTHER)
    core(vet).replace_expr("irq_o", OLD, was=OTHER)
    assert [status for _, _, status in statuses(vet)] == ["applied", "applied"]
    result = vet.finish()
    assert result.ok, result.report()
    assert records(result) == [("core", "proved")]


@pytest.mark.parametrize("waiver", ["", "   ", "two\nlines", 7])
def test_a_waiver_must_be_one_line_of_text(design, monkeypatch, waiver):
    no_yosys(monkeypatch)
    vet = design.vet()
    with pytest.raises((VetError, TypeError)) as caught:
        core(vet).replace_expr("irq_o", OTHER, waiver=waiver)
    assert "replace_expr core" in str(caught.value)
    assert design.changed(vet) == set()


# --- K6: without Yosys ------------------------------------------------------------------------------


def test_without_yosys_and_without_a_waiver_the_call_is_refused(design, monkeypatch):
    no_yosys(monkeypatch)
    vet = design.vet()
    with pytest.raises(VetError) as caught:
        core(vet).replace_expr("irq_o", OTHER)
    message = str(caught.value)
    assert message.startswith("replace_expr core"), message
    assert "hdl-vet[equiv]" in message, message
    assert design.changed(vet) == set()
    assert statuses(vet) == []


def test_without_yosys_a_waived_call_is_waived(design, monkeypatch):
    no_yosys(monkeypatch)
    vet = design.vet()
    core(vet).replace_expr("irq_o", OTHER, waiver=WHY)
    result = vet.finish()
    assert result.ok, result.report()
    assert records(result) == [("core", "waived")]


# --- K7: what is refused ----------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["split[0]", "{irq_o}", "\\irq_o ", "irq_o ", "", 7])
def test_a_target_that_is_no_plain_name_is_refused(design, monkeypatch, target):
    no_yosys(monkeypatch)
    vet = design.vet()
    with pytest.raises((VetError, TypeError)) as caught:
        core(vet).replace_expr(target, "irq_raw", waiver=WHY)
    assert "replace_expr core" in str(caught.value)
    assert design.changed(vet) == set()
    assert statuses(vet) == []


@pytest.mark.parametrize("expr", ["", "irq_raw;", "irq_raw; assign rst_o = 1'b0", "irq_raw &", "a b"])
def test_an_expr_that_is_not_one_expression_is_refused(design, monkeypatch, expr):
    no_yosys(monkeypatch)
    vet = design.vet()
    with pytest.raises((VetError, TypeError)) as caught:
        core(vet).replace_expr("irq_o", expr, waiver=WHY)
    assert "replace_expr core" in str(caught.value)
    assert design.changed(vet) == set()
    assert statuses(vet) == []


def test_a_name_that_the_module_does_not_have_is_refused_with_the_nearest_name(design, monkeypatch):
    no_yosys(monkeypatch)
    message = refused(design.vet(), design, "irq_oo", "irq_raw", waiver=WHY)
    assert "irq_o" in message.replace("irq_oo", ""), message


def test_a_name_driven_in_an_always_block_is_refused(design, monkeypatch):
    no_yosys(monkeypatch)
    message = refused(design.vet(), design, "seen", "irq_raw", waiver=WHY)
    assert "no continuous assign" in message and "always" in message, message


def test_a_name_driven_by_an_instance_is_refused(design, monkeypatch):
    no_yosys(monkeypatch)
    message = refused(design.vet(), design, "irq_raw", "en_i[0]", waiver=WHY)
    assert "no continuous assign" in message and "u_timer" in message, message


def test_a_name_with_no_driver_is_refused(design, monkeypatch):
    no_yosys(monkeypatch)
    message = refused(design.vet(), design, "floating", "en_i[0]", waiver=WHY)
    assert "no continuous assign" in message, message


def test_a_name_assigned_by_selects_is_refused(design, monkeypatch):
    no_yosys(monkeypatch)
    message = refused(design.vet(), design, "split", "en_i", waiver=WHY)
    assert "split[0]" in message or "select" in message, message


def test_two_assigns_to_one_name_are_refused(tmp_path, monkeypatch):
    no_yosys(monkeypatch)
    design = variant(
        tmp_path,
        "  assign rst_o    = rst_sync_n;\n",
        "  assign rst_o    = rst_sync_n;\n  wire dual;\n  assign dual = rst_sync_n;\n  assign dual = rst_slow_n;\n",
    )
    message = refused(design.vet(), design, "dual", "rst_ni", waiver=WHY)
    assert "2" in message, message


def test_an_assign_that_shares_its_statement_is_refused(tmp_path, monkeypatch):
    no_yosys(monkeypatch)
    design = variant(
        tmp_path,
        "  assign count_o  = cnt;\n  assign rst_o    = rst_sync_n;\n",
        "  assign count_o  = cnt, rst_o = rst_sync_n;\n",
    )
    message = refused(design.vet(), design, "rst_o", "rst_slow_n", waiver=WHY)
    assert "statement" in message, message


def test_an_assign_in_a_generate_block_is_refused(tmp_path, monkeypatch):
    no_yosys(monkeypatch)
    design = variant(
        tmp_path,
        "  assign rst_o    = rst_sync_n;\n",
        "  if (1) begin : g_rst\n    assign rst_o = rst_sync_n;\n  end\n",
    )
    message = refused(design.vet(), design, "rst_o", "rst_slow_n", waiver=WHY)
    assert "generate" in message, message


def test_an_assign_from_a_macro_is_refused(tmp_path, monkeypatch):
    no_yosys(monkeypatch)
    design = variant(
        tmp_path,
        "  assign rst_o    = rst_sync_n;\n",
        "`define DRIVE(t, v) assign t = v;\n  `DRIVE(rst_o, rst_sync_n)\n",
    )
    message = refused(design.vet(), design, "rst_o", "rst_slow_n", waiver=WHY)
    assert "macro" in message, message


@pytest.mark.parametrize("expr", ["irq_o & irq_raw", "~irq_o", "irq_raw ? irq_o : 1'b0"])
def test_an_expr_that_reads_its_target_is_refused(design, monkeypatch, expr):
    no_yosys(monkeypatch)
    message = refused(design.vet(), design, "irq_o", expr, waiver=WHY)
    assert "loop" in message, message


def test_an_expr_that_reads_its_target_through_another_net_is_refused(tmp_path, monkeypatch):
    no_yosys(monkeypatch)
    design = variant(tmp_path, "  assign count_o  = cnt;\n", "  assign count_o  = cnt;\n  assign floating = rst_o;\n")
    message = refused(design.vet(), design, "rst_o", "floating & rst_sync_n", waiver=WHY)
    assert "loop" in message, message


# --- K8: the check of the sign-off ------------------------------------------------------------------


def test_a_later_removal_of_the_assign_fails_the_sign_off(tmp_path, monkeypatch):
    """The net `floating` is read by nothing, so its assign can be removed."""
    no_yosys(monkeypatch)
    design = variant(tmp_path, "  assign count_o  = cnt;\n", "  assign count_o  = cnt;\n  assign floating = en_i[0];\n")
    vet = design.vet()
    core(vet).replace_expr("floating", "en_i[1]", waiver=WHY)
    core(vet).remove_assign("floating")
    result = vet.finish()
    assert not result.ok
    assert "replace_expr core" in result.report() and "floating" in result.report(), result.report()


def test_a_later_splice_over_the_expression_fails_the_sign_off(design, monkeypatch):
    no_yosys(monkeypatch)
    vet = design.vet()
    core(vet).replace_expr("rst_o", "rst_slow_n", waiver=WHY)
    design.replace(vet, "core", b"= rst_slow_n;", b"= rst_ni;", waiver=WHY)
    result = vet.finish()
    assert not result.ok
    assert "replace_expr core" in result.report() and "rst_o" in result.report(), result.report()


# --- K9: a saved session ----------------------------------------------------------------------------


def test_a_saved_session_keeps_the_mark_and_the_waiver(design, monkeypatch, tmp_path):
    no_yosys(monkeypatch)
    vet = design.vet()
    core(vet).replace_expr("irq_o", OTHER, waiver=WHY)
    vet.save(tmp_path / "saved.vet")
    result = Vet.restore(tmp_path / "saved.vet").finish()
    assert result.ok, result.report()
    assert records(result) == [("core", "waived")]
    assert result.equivalence[0].operations == ["replace_expr core.irq_o"]
    assert WHY in result.report()


# --- K10: taking back -------------------------------------------------------------------------------


def test_a_group_that_is_left_by_an_exception_leaves_nothing(design, monkeypatch):
    no_yosys(monkeypatch)
    vet = design.vet()
    with pytest.raises(RuntimeError), vet.group():
        core(vet).replace_expr("irq_o", OTHER, waiver=WHY)
        raise RuntimeError("stop")
    assert design.changed(vet) == set()
    result = vet.finish()
    assert result.ok, result.report()
    assert result.equivalence == []
