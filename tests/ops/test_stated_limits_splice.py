"""Stated limits of a raw splice that had no test (#106, the fourth batch).

From the item "Raw logic edits" of the chapter on limits and from the item on gate primitives. The
survey of 2026-11-27 found no test for these claims. Each test shows that the limit is still a
limit, with a real proof where the claim is about the comparison. A test that fails means that the
limit is gone or is stated wrongly, and the chapter has to change with it.

U1  A `bind` at file level that puts an instance into the module makes its record "error".
U2  A raw splice that changes the default of a parameter that an instance uses makes the record
    of the module "error", and the detail names the parameter. A harmless splice in the same
    module is "proved".
U3  Yosys defines `SYNTHESIS`, and the tool loads the design without it. A change in the
    `else` branch, which is active only without `SYNTHESIS`, is "proved": the comparison does
    not see it. The same change in the `ifdef` branch is "refuted".
U4  A comment that is a directive for another tool is a comment: a splice that adds
    `// synopsys translate_off` around an assignment signs off with no comparison.
U5  A change of an attribute of a gate primitive is "proved". A change of its delay or of its
    drive strength is a change of the structure, which the sign-off reports as not expected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet import equiv
from hdl_vet.ops import Vet
from hdl_vet.session import Result, Session

pytestmark = [pytest.mark.equiv]

needs_yosys = pytest.mark.skipif(not equiv.available(), reason="Yosys (hdl-vet[equiv]) is not installed")

WHY = "the reviewer looked at it"


def run(tmp_path: Path, files: dict[str, str], name: str, old: str, new: str, **more: object) -> Result:
    """Load `files`, replace `old` by `new` in the file `name` with one raw splice, and sign off."""
    root = tmp_path / "design"
    (root / "rtl").mkdir(parents=True)
    for file, text in files.items():
        (root / "rtl" / file).write_bytes(text.encode())
    (root / "design.f").write_bytes("".join(f"rtl/{file}\n" for file in files).encode())
    vet = Vet(Session(root / "design.f", top="top"))
    path = root / "rtl" / name
    at = vet.text(path).index(old.encode())
    vet.splice(path, at, at + len(old), new.encode(), old=old.encode(), **more)
    return vet.finish()


def records(result: Result) -> list[tuple[str, str]]:
    return [(r.module, r.status) for r in result.equivalence]


def top(body: str, ports: str = "input logic a, output logic y") -> str:
    return f"module top ({ports});\n{body}endmodule\n"


# --- U1 -------------------------------------------------------------------------------------------


@needs_yosys
def test_a_bind_at_file_level_makes_the_record_an_error(tmp_path):
    files = {
        "chk.sv": "module chk (input logic x);\nendmodule\n",
        "top.sv": top("  logic n;\n  assign n = a;\n  assign y = n;\n") + "bind top chk u_chk (.x(a));\n",
    }
    result = run(tmp_path, files, "top.sv", "assign y = n;", "assign y = n & 1'b1;")
    assert not result.ok
    assert records(result) == [("top", "error")]
    assert "bind" in result.equivalence[0].detail, result.equivalence[0].detail


# --- U2 -------------------------------------------------------------------------------------------

WIDE = {
    "leaf.sv": (
        "module leaf #(parameter int W = 4) (input logic [W-1:0] a, output logic [W-1:0] y);\n"
        "  assign y = a;\nendmodule\n"
    ),
    "top.sv": top("  leaf u (.a(a), .y(y));\n", "input logic [3:0] a, output logic [3:0] y"),
}


@needs_yosys
def test_a_splice_that_changes_a_default_that_an_instance_uses_is_an_error(tmp_path):
    result = run(tmp_path, WIDE, "leaf.sv", "W = 4", "W = 5")
    assert not result.ok
    assert records(result) == [("leaf", "error")]
    assert "parameter W" in result.equivalence[0].detail, result.equivalence[0].detail


@needs_yosys
def test_a_harmless_splice_in_that_module_is_proved(tmp_path):
    result = run(tmp_path, WIDE, "leaf.sv", "assign y = a;", "assign y = a & {W{1'b1}};")
    assert result.ok, result.report()
    assert records(result) == [("leaf", "proved")]


# --- U3 -------------------------------------------------------------------------------------------

SYNTH = {"top.sv": top("`ifdef SYNTHESIS\n  assign y = a;\n`else\n  assign y = ~a;\n`endif\n")}


@needs_yosys
def test_a_change_in_the_branch_without_synthesis_is_not_seen(tmp_path):
    """`~a` becomes `a`: the design that the tool loads changes, and the comparison says "proved"."""
    result = run(tmp_path, SYNTH, "top.sv", "assign y = ~a;", "assign y = a | 1'b0;")
    assert records(result) == [("top", "proved")]
    assert result.ok, result.report()


@needs_yosys
def test_the_same_change_in_the_branch_with_synthesis_is_refuted(tmp_path):
    result = run(tmp_path, SYNTH, "top.sv", "assign y = a;", "assign y = ~a;")
    assert records(result) == [("top", "refuted")]
    assert not result.ok


# --- U4 -------------------------------------------------------------------------------------------


def test_a_directive_comment_of_another_tool_is_a_comment(tmp_path):
    files = {"top.sv": top("  assign y = a;\n")}
    new = "// synopsys translate_off\n  assign y = a;\n  // synopsys translate_on"
    result = run(tmp_path, files, "top.sv", "assign y = a;", new)
    assert result.ok, result.report()
    assert records(result) == []


# --- U5 -------------------------------------------------------------------------------------------

GATE_PORTS = "input logic a, input logic b, output logic y"


@needs_yosys
def test_a_change_of_an_attribute_of_a_primitive_is_proved(tmp_path):
    files = {"top.sv": top("  (* keep *) and g1 (y, a, b);\n", GATE_PORTS)}
    result = run(tmp_path, files, "top.sv", "(* keep *)", "(* keep = 0 *)")
    assert result.ok, result.report()
    assert records(result) == [("top", "proved")]


@pytest.mark.parametrize(
    ("gate", "old", "new", "kind"),
    [
        ("and #1 g1 (y, a, b);", "#1", "#2", "delay-changed"),
        ("and (strong0, weak1) g1 (y, a, b);", "weak1", "strong1", "strength-changed"),
    ],
    ids=["delay", "strength"],
)
def test_a_change_of_the_delay_or_the_strength_of_a_primitive_is_a_change_of_the_structure(
    tmp_path, monkeypatch, gate, old, new, kind
):
    monkeypatch.setattr(equiv, "available", lambda: False)
    files = {"top.sv": top(f"  {gate}\n", GATE_PORTS)}
    result = run(tmp_path, files, "top.sv", old, new, waiver=WHY)
    assert not result.ok
    assert [(c.kind, c.path) for c in result.unexpected_changes] == [(kind, "top.g1")]
