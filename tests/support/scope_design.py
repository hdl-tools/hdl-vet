"""The design and the helpers that test_scope.py and test_scope_review.py both use."""

from __future__ import annotations

from pathlib import Path

from hdl_vet.ops import Vet

CELL = (
    "module {name} #(\n"
    "  parameter int W = 1\n"
    ") (\n"
    "  input  logic clk,\n"
    "  input  logic d,\n"
    "  output logic q\n"
    ");\n"
    "  always_ff @(posedge clk) q <= d;\n"
    "endmodule\n"
)
CELLS = "// cells\n" + CELL.format(name="cell_a") + "\n" + CELL.format(name="cell_b")
BUF = "module buf_cell (\n  input  logic a,\n  output logic y\n);\n  assign y = a;\nendmodule\n"
SHARED = (
    "module shared (\n"
    "  input  logic clk,\n"
    "  input  logic d,\n"
    "  output logic q\n"
    ");\n"
    "  logic r;\n"
    "  always_ff @(posedge clk) r <= d;\n"
    "  buf_cell u_buf (.a(r), .y(q));\n"
    "endmodule\n"
)
BLK = (
    "// block\n"
    "module {name} (\n"
    "  input  logic clk,\n"
    "  input  logic d,\n"
    "  output logic q\n"
    ");\n"
    "  logic m;\n"
    "  {cell} {inst} (.clk(clk), .d(d), .q(m));\n"
    "  shared u_s (.clk(clk), .d(m), .q(q));\n"
    "endmodule\n"
)
TOP = (
    "module top (\n"
    "  input  logic clk,\n"
    "  input  logic a,\n"
    "  input  logic b,\n"
    "  output logic ya,\n"
    "  output logic yb\n"
    ");\n"
    "  blk_a u_a (.clk(clk), .d(a), .q(ya));\n"
    "  blk_b u_b (.clk(clk), .d(b), .q(yb));\n"
    "endmodule\n"
)
FILES = {
    "cells": CELLS,
    "buf_cell": BUF,
    "shared": SHARED,
    "blk_a": BLK.format(name="blk_a", cell="cell_a", inst="u_c0"),
    "blk_b": BLK.format(name="blk_b", cell="cell_b", inst="u_c"),
    "top": TOP,
}


class Design:
    """top -> u_a (blk_a) -> u_c0 (cell_a), u_s (shared -> u_buf);  top -> u_b (blk_b) -> u_c (cell_b), u_s (shared).

    `cell_a` and `cell_b` are in one file, `rtl/cells.sv`.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        (self.root / "rtl").mkdir(parents=True)
        for name, text in FILES.items():
            (self.root / "rtl" / f"{name}.sv").write_bytes(text.encode())
        self.filelist = self.root / "design.f"
        self.filelist.write_bytes("".join(f"rtl/{name}.sv\n" for name in FILES).encode())

    def path(self, name: str) -> Path:
        return self.root / "rtl" / f"{name}.sv"

    def changed(self, vet: Vet) -> set[str]:
        """The files whose text differs from the input, with everything queued applied."""
        vet.session.refresh()
        return {n for n in FILES if vet.text(self.path(n)) != self.path(n).read_bytes()}


def statuses(vet: Vet) -> list[tuple[str, str, str]]:
    return [(r.op, r.target, r.status) for r in vet.log]
