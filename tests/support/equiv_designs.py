"""The designs and the helpers that test_equiv.py and test_equiv_review.py both use."""

from __future__ import annotations

from pathlib import Path

import pytest

equiv = pytest.importorskip("hdl_vet.equiv")
RST_SYNC = (
    "module rst_sync #(\n"
    "  parameter int STAGES = 2\n"
    ") (\n"
    "  input  logic clk_i,\n"
    "  input  logic rst_ni,\n"
    "  output logic rst_no\n"
    ");\n"
    "  logic [STAGES-1:0] q;\n"
    "\n"
    "  always_ff @(posedge clk_i or negedge rst_ni)\n"
    "    if (!rst_ni) q <= '0;\n"
    "    else         q <= {q[STAGES-2:0], 1'b1};\n"
    "\n"
    "  assign rst_no = q[STAGES-1];\n"
    "endmodule\n"
)
TIMER = (
    "module timer #(\n"
    "  parameter int WIDTH = 8\n"
    ") (\n"
    "  input  logic             clk_i,\n"
    "  input  logic             rst_ni,\n"
    "  input  logic             en_i,\n"
    "  output logic [WIDTH-1:0] count_o,\n"
    "  output logic             irq_o\n"
    ");\n"
    "  always_ff @(posedge clk_i or negedge rst_ni)\n"
    "    if (!rst_ni)   count_o <= '0;\n"
    "    else if (en_i) count_o <= count_o + 1'b1;\n"
    "\n"
    "  assign irq_o = &count_o;\n"
    "endmodule\n"
)
WRAP = (
    "module wrap (\n"
    "  input  logic       clk_i,\n"
    "  input  logic       rst_ni,\n"
    "  input  logic [1:0] en_i,\n"
    "  output logic       irq_o\n"
    ");\n"
    "  logic [1:0] irq;\n"
    "  logic [7:0] c0, c1;\n"
    "  timer u_t0 (.clk_i(clk_i), .rst_ni(rst_ni), .en_i(en_i[0]), .count_o(c0), .irq_o(irq[0]));\n"
    "  timer u_t1 (.clk_i(clk_i), .rst_ni(rst_ni), .en_i(en_i[1]), .count_o(c1), .irq_o(irq[1]));\n"
    "  assign irq_o = |irq;\n"
    "endmodule\n"
)


def source(folder: Path, *texts: str) -> object:
    """A Source made of one file for each text, written into `folder`."""
    folder.mkdir(parents=True, exist_ok=True)
    files = []
    for number, text in enumerate(texts):
        path = folder / f"m{number}.sv"
        path.write_bytes(text.encode())
        files.append(path)
    return equiv.Source(files)


def run(tmp_path: Path, gold: tuple[str, ...], gate: tuple[str, ...], module: str, **options):
    result = equiv.check(source(tmp_path / "gold", *gold), source(tmp_path / "gate", *gate), module, **options)
    assert result.module == module
    assert "\n" not in result.detail
    return result
