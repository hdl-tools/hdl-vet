"""A driver the run removed that nothing reports (#58).

`tests/core/test_dangling_signoff.py` holds the dangling check, which rests on slang's
unused analysis. That analysis has a blind spot, and the maintainer of slang states it is
deliberate (slang#1602, open since 2025-12-12, labelled `feature-request`):

    This is by design; most people get very annoyed by "unused" lint warnings in shared
    code that sometimes sets parameters that control generate blocks one way and sometimes
    another.

So when a driver sits inside a generate branch that elaboration does not select, nothing
reports the signal as undriven, and hdl-vet signs the run off as `ok`. The report filed
upstream for this, slang#2139, was closed as a duplicate of #1602. Waiting is therefore
not an option, and the owner decided on 2026-11-21 that hdl-vet detects it itself.

WHAT THE VERIFICATION PASS MEASURED, 2026-11-22

The motivating case, a parameter change selecting the empty branch:

    sign-off ok : True        <- the wrong `ok`
    dangling    : []
    child.y drivers before : [(False, None)]     after : []
    top.w   drivers before : [(False, 'u_c')]    after : [(False, 'u_c')]

Two things follow. `drivers_in` does see the loss, so the comparison is possible. And the
loss is in **child**, while the operation edits **top**, so a check scoped to the modules
an operation touched would miss it. That is why the check covers the whole design.

The discriminator needed no bookkeeping. Measured on the two shapes:

    harmless : remove_assign('spare'), nothing reads spare
               unused -> UnusedVariable 'spare'      dangling -> []
    dangerous: set_param MODE=1, child.y read upstream
               unused -> []                          dangling -> []

So a lost driver matters exactly when slang does NOT call the signal unused. Every
deliberate removal is already handled: removing the last driver of a signal something
reads is refused today (`remove_assign('mid')` gives "left dangling: variable 'mid' is
never assigned a value"), and removing the last driver of a signal nothing reads leaves it
in the unused set. The new check is therefore purely additive and cannot refuse an
operation that works today.

Cost, measured on a generated design of 2400 signals: `drivers_in` is about 23 microseconds
per signal, so one whole-design pass is 0.056 s and the before-and-after pair is about
0.11 s. `unused()` is 0.002 s. That is why the whole design is checked on every run.

Rules
    D1  A signal that had a driver before the run and has none after is reported, and the
        sign-off is not ok.
    D2  The generate-branch case is the motivating one and must be caught: a parameter
        change that selects a branch which does not drive the signal.
    D3  A signal that loses its last driver and that nothing reads is NOT reported. slang
        puts it in the unused set, and the run asked for it.
    D4  The check covers the whole design. The motivating case loses the driver in a module
        the operation did not edit.
    D5  The report names the module and the signal, and says the run removed its last
        driver, so the reader is not left to guess which edit did it.
    D6  A run that removes no driver is unaffected: nothing is added to the report and the
        sign-off result is unchanged.

Not checked
    A driver lost through a hierarchical assignment from another module. A signal that
    loses one of several drivers, which leaves it driven and is not this defect. The
    interaction with a `group`, where the comparison is taken once at the end.

    A signal declared **inside a generate block** is not a candidate, because
    `ModuleInfo.decls` does not hold it. So a driver lost on such a signal is not reported.
    That is the same shape of gap as the one this part closes, one level further in, and it
    is not measured.

Limits, stated
    Liveness is judged exactly as the dangling check judges it (`src/hdl_vet/session.py`,
    the dangling set). A module-level net or variable is live when slang does not call it
    unused. An **output port** is live when some instance connects it to a non-empty
    expression or its own module reads it. An output every instance leaves open is
    therefore not live, and losing its driver is allowed, which
    `tests/core/test_dangling_signoff.py::test_the_driver_of_an_output_every_instance_leaves_open_can_be_removed`
    already requires. Using one notion of liveness for both checks is deliberate: two
    notions would disagree somewhere, and that disagreement would be a defect.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.ops import Vet
from hdl_vet.session import Session

# the driver of `y` sits in the MODE == 0 branch only
CHILD = (
    "module child #(parameter int MODE = 0) (input logic a, output logic y);\n"
    "  if (MODE == 0) begin : g0\n"
    "    assign y = a;\n"
    "  end else begin : g1\n"
    "    // nothing drives y\n"
    "  end\n"
    "endmodule\n"
)

GEN_TOP = (
    "module top (input logic a, output logic y);\n"
    "  logic w;\n"
    "  child #(.MODE(0)) u_c (.a(a), .y(w));\n"
    "  assign y = w;\n"
    "endmodule\n"
)

# `spare` has a driver and nothing reads it
LEAF = "module leaf (input logic a, output logic y);\n  logic spare;\n  assign spare = a;\n  assign y = a;\nendmodule\n"

LEAF_TOP = (
    "module top (input logic a, output logic y);\n  logic w;\n  leaf u_l (.a(a), .y(w));\n  assign y = w;\nendmodule\n"
)


def build(root: Path, files: dict[str, str]) -> Vet:
    (root / "rtl").mkdir(parents=True)
    for name, text in files.items():
        (root / "rtl" / f"{name}.sv").write_bytes(text.encode())
    fl = root / "design.f"
    fl.write_bytes("".join(f"rtl/{n}.sv\n" for n in files).encode())
    return Vet(Session(fl, top="top"))


@pytest.fixture
def gen_vet(tmp_path: Path) -> Vet:
    return build(tmp_path / "d", {"child": CHILD, "top": GEN_TOP})


@pytest.fixture
def leaf_vet(tmp_path: Path) -> Vet:
    return build(tmp_path / "d", {"leaf": LEAF, "top": LEAF_TOP})


# --- D1, D2, D4, D5: the motivating case is caught -----------------------------------


def test_a_parameter_change_that_drops_a_driver_is_reported(gen_vet):
    """Before this part the sign-off said `ok = True` with `dangling = []`."""
    gen_vet.module("top").instance("u_c").set_param("MODE", 1, was=0)
    result = gen_vet.finish()
    assert not result.ok, result.report()


def test_the_report_names_the_module_and_the_signal(gen_vet):
    gen_vet.module("top").instance("u_c").set_param("MODE", 1, was=0)
    report = gen_vet.finish().report()
    assert "child" in report, report
    assert "'y'" in report or " y " in report, report


def test_the_module_that_loses_the_driver_is_not_the_edited_one(gen_vet):
    """Rule D4. The operation edits `top`; the driver is lost in `child`.

    A check scoped to the modules an operation touched would not look at `child` at all,
    which is why the whole design is compared.
    """
    gen_vet.module("top").instance("u_c").set_param("MODE", 1, was=0)
    result = gen_vet.finish()
    assert not result.ok, result.report()
    assert "child" in result.report(), result.report()


# --- D3: a signal nothing reads is not reported --------------------------------------


def test_a_driver_removed_from_an_unread_signal_is_not_reported(leaf_vet):
    """`spare` loses its only driver and nothing reads it, so the run asked for it.

    slang puts it in the unused set. Reporting it would refuse an operation that works
    today, which is what makes this rule the guard against over-refusal.
    """
    leaf_vet.module("leaf").remove_assign("spare")
    result = leaf_vet.finish()
    assert result.ok, result.report()


# --- D6: a run that removes no driver is unaffected ----------------------------------


def test_a_run_that_removes_no_driver_is_unchanged(leaf_vet):
    leaf_vet.module("leaf").add_wire("probe_w")
    result = leaf_vet.finish()
    assert result.ok, result.report()
    assert "last driver" not in result.report(), result.report()


def test_a_clean_run_with_no_operation_is_ok(gen_vet):
    result = gen_vet.finish()
    assert result.ok, result.report()
    assert "last driver" not in result.report(), result.report()
