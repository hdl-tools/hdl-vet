"""The cost of the whole-design driver scan at sign-off (#58, #116).

The sign-off compares the drivers of every signal before and after a run, to catch a
driver lost through a generate branch that the elaboration does not take
(`tests/core/test_lost_driver.py`). The first version asked `drivers_in(module, name)`
once per signal. Each of those calls runs `_canonical_bodies(module)`, which walks every
instance of the design, so the scan cost grew as modules times signals times instances.

That was invisible locally, because the corpus tests skip without the hdl-bag CLI and the
synthetic design used to measure the cost had 41 instances. On CI, with the corpus, the
test job went from six to fifteen minutes up to the thirty-minute timeout, and GitHub
reported the timed-out jobs as cancelled. Measured on a generated design of 1200 instances
and 1200 distinct signals: 0.72 s for one comparison, against under 0.01 s for a scan that
walks the instances once and then reads the drivers of each member.

This file follows the rule of the folder: it pins behaviour, not a duration. A timing bound
would be a different number on every machine. The invariant is that the scan does not walk
the design again for each signal, so **the number of canonical-body walks must not grow
with the number of signals per module**.

Rules
    X1  Growing the signals per module does not grow the number of canonical-body walks.
        Four times the signals, the same design shape: the walk count must not scale with
        it.
    X2  The scan still finds what it is for. The same design with a driver removed from a
        read signal is still reported, so a faster scan is not a blinder one.

Not checked
    A duration. The folder's other tests avoid them too, and CI machines differ by more
    than the margin any bound would need.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet import slang_adapter
from hdl_vet.ops import Vet
from hdl_vet.session import Session


def generated(root: Path, kinds: int, signals: int, copies: int) -> Path:
    """`kinds` module kinds of `signals` chained signals each, instantiated `copies` times."""
    rtl = root / "rtl"
    rtl.mkdir(parents=True)
    for m in range(kinds):
        lines = [f"module m{m} (input logic a, output logic y);"]
        lines += [f"  logic s{i};" for i in range(signals)]
        lines += ["  assign s0 = a;"]
        lines += [f"  assign s{i} = s{i - 1};" for i in range(1, signals)]
        lines += [f"  assign y = s{signals - 1};", "endmodule"]
        (rtl / f"m{m}.sv").write_bytes(("\n".join(lines) + "\n").encode())
    top = ["module top (input logic a, output logic y);"]
    for m in range(kinds):
        top += [f"  logic [{copies - 1}:0] w{m};"]
        top += [f"  m{m} u{m}_{c} (.a(a), .y(w{m}[{c}]));" for c in range(copies)]
    top += ["  assign y = " + " | ".join(f"(|w{m})" for m in range(kinds)) + ";", "endmodule"]
    (rtl / "top.sv").write_bytes(("\n".join(top) + "\n").encode())
    fl = root / "design.f"
    fl.write_bytes(("".join(f"rtl/m{m}.sv\n" for m in range(kinds)) + "rtl/top.sv\n").encode())
    return fl


@pytest.fixture
def count_body_walks(monkeypatch) -> list[str]:
    """The module name of every canonical-body walk from now on."""
    walked: list[str] = []
    original = slang_adapter.Design._canonical_bodies

    def walk(self, module):
        walked.append(module)
        return original(self, module)

    monkeypatch.setattr(slang_adapter.Design, "_canonical_bodies", walk)
    return walked


# --- X1: the scan does not walk the design once per signal ---------------------------


def test_more_signals_do_not_mean_more_body_walks(tmp_path: Path, count_body_walks):
    """Four times the signals, the same shape. The walk count must not follow.

    With the first version this count was roughly two per signal, because every
    `drivers_in` call walked the instances again. Measured on a design of 31 modules and
    1200 signals: 2522 walks, each over 1201 instances. The whole point of the fix is that
    the scan walks once and then reads each member.

    The `add_wire` matters: with no edit at all the sign-off skips the comparison and the
    count is zero, which made an earlier version of this test vacuous.
    """
    small = Vet(Session(generated(tmp_path / "small", kinds=4, signals=5, copies=3), top="top"))
    small.module("top").add_wire("probe_w")  # without an edit the sign-off skips the scan
    small.finish()
    few = len(count_body_walks)

    count_body_walks.clear()
    big = Vet(Session(generated(tmp_path / "big", kinds=4, signals=20, copies=3), top="top"))
    big.module("top").add_wire("probe_w")
    big.finish()
    many = len(count_body_walks)

    assert many <= few + 4, (
        f"the body walks grew with the signals: {few} walks for 5 signals a module, "
        f"{many} for 20. The scan is asking per signal again."
    )


# --- X2: a faster scan is not a blinder one ------------------------------------------

CHILD = (
    "module child #(parameter int MODE = 0) (input logic a, output logic y);\n"
    "  if (MODE == 0) begin : g0\n"
    "    assign y = a;\n"
    "  end else begin : g1\n"
    "  end\n"
    "endmodule\n"
)

TOP = (
    "module top (input logic a, output logic y);\n"
    "  logic w;\n"
    "  child #(.MODE(0)) u_c (.a(a), .y(w));\n"
    "  assign y = w;\n"
    "endmodule\n"
)


def test_the_scan_still_catches_a_lost_driver(tmp_path: Path):
    """The guard against making the scan cheap by making it empty."""
    rtl = tmp_path / "rtl"
    rtl.mkdir(parents=True)
    (rtl / "child.sv").write_bytes(CHILD.encode())
    (rtl / "top.sv").write_bytes(TOP.encode())
    fl = tmp_path / "design.f"
    fl.write_bytes(b"rtl/child.sv\nrtl/top.sv\n")
    vet = Vet(Session(fl, top="top"))
    vet.module("top").instance("u_c").set_param("MODE", 1, was=0)
    result = vet.finish()
    assert not result.ok, result.report()
    assert "last driver" in result.report(), result.report()
