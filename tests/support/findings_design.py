"""The design and the helpers that test_manual_findings.py and test_manual_findings_review.py both use."""

from __future__ import annotations

from pathlib import Path

from hdl_vet.ops import Vet
from hdl_vet.session import Session

LEAF = (
    "module leaf #(\n"
    "  parameter int W = 1\n"
    ") (\n"
    "  input  logic clk,\n"
    "  input  logic d,\n"
    "  output logic q\n"
    ");\n"
    "  always_ff @(posedge clk) q <= d;\n"
    "endmodule\n"
)
OTHER = LEAF.replace("module leaf", "module other")
ESC = "  leaf \\u/esc (.clk(clk), .d(a), .q(y));\n"
TOP = (
    "module top (\n"
    "  input  logic clk,\n"
    "  input  logic a,\n"
    "  input  logic b,\n"
    "  output logic y,\n"
    "  output logic z,\n"
    "  output logic w\n"
    ");\n"
    "  logic n1;\n" + ESC + "  leaf u_plain (.clk(clk), .d(b), .q(w));\n"
    "  and g1 (n1, a, b);\n"
    "  not (z, n1);\n"
    "endmodule\n"
)


class Design:
    """top: `\\u/esc ` and `u_plain` (leaf), the gate `and g1 (n1, a, b)` and an unnamed `not (z, n1)`."""

    def __init__(self, root: Path, top: str = TOP) -> None:
        self.root = root.resolve()
        (self.root / "rtl").mkdir(parents=True)
        for name, text in (("leaf", LEAF), ("other", OTHER), ("top", top)):
            (self.root / "rtl" / f"{name}.sv").write_bytes(text.encode())
        self.filelist = self.root / "design.f"
        self.filelist.write_bytes(b"rtl/leaf.sv\nrtl/other.sv\nrtl/top.sv\n")
        self.top = self.root / "rtl" / "top.sv"

    def vet(self) -> Vet:
        return Vet(Session(self.filelist, top="top"))

    def text(self, vet: Vet) -> str:
        vet.session.refresh()
        return vet.text(self.top).decode()

    def replace(self, vet: Vet, old: bytes, new: bytes) -> None:
        data = vet.text(self.top)
        at = data.index(old)
        vet.splice(self.top, at, at + len(old), new, old=old)


def statuses(vet: Vet) -> list[tuple[str, str, str]]:
    return [(r.op, r.target, r.status) for r in vet.log]


def changes(result) -> set[tuple[str, str, str]]:
    return {(c.kind, c.path, c.name) for c in result.changes}
