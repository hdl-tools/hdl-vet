"""The cell and the swap that test_instances_corpus.py and test_save_resume_corpus.py use."""

from __future__ import annotations

from pathlib import Path

from hdl_vet.ops import Vet

TOP = "demo_ss_bus_top"
TECH_CELL = (
    "// A stand-in for a technology reset synchroniser cell.\n"
    "module tech_rst_sync #(\n"
    "    parameter int unsigned Stages = 2\n"
    ") (\n"
    "    input  logic CK,\n"
    "    input  logic RN,\n"
    "    output logic Q\n"
    ");\n"
    "  logic [Stages-1:0] q;\n"
    "  always_ff @(posedge CK or negedge RN) begin\n"
    "    if (!RN) q <= '0;\n"
    "    else q <= {q[Stages-2:0], 1'b1};\n"
    "  end\n"
    "  assign Q = q[Stages-1];\n"
    "endmodule\n"
)


def swap(vet: Vet, cell: Path, module: str = "tech_rst_sync") -> None:
    vet.add_file(cell.parent / f"{module}.sv")
    top = vet.module(TOP)
    for inst in top.instances(module="bag_rst_sync", count=4):
        inst.replace_module(
            module,
            ports={"clk_i": "CK", "rst_i": "RN", "rst_no": "Q"},
            drop_params=["ActiveLow", "AsyncAssert"],
            was="bag_rst_sync",
        )
