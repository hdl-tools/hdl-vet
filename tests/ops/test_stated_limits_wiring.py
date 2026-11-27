"""Stated limits of what is wiring and what is logic, that had no test (#106, the fifth batch).

From the items "Wiring is not compared" and "Not compared" of the chapter on limits. A call with
wiring needs nothing. A call with logic costs a proof or a waiver: with no Yosys and no waiver it
is refused, and that refusal is what the tests look for.

X1  A tie to a constant and a select with a constant index are wiring. `connect` to them
    changes the function, needs no waiver, and the sign-off is ok.
X2  A struct member (`s.v`) is logic.
X3  A select with a package constant as its index is logic.
X4  A select whose bound is a call, such as `$clog2(W)`, is logic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet import equiv
from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session

PKG = "package pk;\n  localparam int I = 1;\n  typedef struct packed { logic v; logic w; } pair_t;\nendpackage\n"
LEAF = "module leaf (input logic a, output logic y);\n  assign y = a;\nendmodule\n"
TOP = (
    "module top #(parameter int W = 4) (input logic a, input logic [3:0] b, input pk::pair_t s, output logic y);\n"
    "  leaf u (.a(a), .y(y));\n"
    "endmodule\n"
)


@pytest.fixture(autouse=True)
def no_yosys(monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)


@pytest.fixture
def vet(tmp_path: Path) -> Vet:
    root = tmp_path / "design"
    (root / "rtl").mkdir(parents=True)
    for name, text in (("pk.sv", PKG), ("leaf.sv", LEAF), ("top.sv", TOP)):
        (root / "rtl" / name).write_bytes(text.encode())
    (root / "design.f").write_bytes(b"rtl/pk.sv\nrtl/leaf.sv\nrtl/top.sv\n")
    return Vet(Session(root / "design.f", top="top"))


def connect(vet: Vet, expr: str) -> None:
    vet.module("top").instance("u").connect("a", expr, was="a")


@pytest.mark.parametrize("expr", ["1'b0", "b[1]", "b[W-3]"], ids=["tie", "constant-index", "parameter-index"])
def test_wiring_needs_nothing_and_changes_the_function(vet, expr):
    connect(vet, expr)
    result = vet.finish()
    assert result.ok, result.report()
    assert result.equivalence == []
    assert [p.name for p in result.files] == ["top.sv"]


@pytest.mark.parametrize(
    "expr",
    ["s.v", "b[pk::I]", "b[$clog2(W)]"],
    ids=["struct-member", "package-constant-index", "call-in-the-bound"],
)
def test_logic_costs_a_proof_or_a_waiver(vet, expr):
    with pytest.raises(VetError) as caught:
        connect(vet, expr)
    message = str(caught.value)
    assert message.startswith("connect top.u: "), message
    assert "waiver" in message, message
    assert list(vet.log) == []


@pytest.mark.parametrize("expr", ["s.v", "b[pk::I]", "b[$clog2(W)]"])
def test_the_same_call_with_a_waiver_is_taken(vet, expr):
    vet.module("top").instance("u").connect("a", expr, was="a", waiver="the reviewer looked at it")
    assert [(r.op, r.status) for r in vet.log] == [("connect", "applied")]
