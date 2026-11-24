"""The design and the helpers of tests/ops/test_override.py and tests/ops/test_override_review.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet import equiv
from hdl_vet.ops import Vet
from hdl_vet.session import Session

needs_yosys = pytest.mark.skipif(not equiv.available(), reason="Yosys (hdl-vet[equiv]) is missing")

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
    "module timer (\n"
    "  input  logic       clk_i,\n"
    "  input  logic       rst_ni,\n"
    "  input  logic       en_i,\n"
    "  output logic [7:0] count_o,\n"
    "  output logic       irq_o\n"
    ");\n"
    "  always_ff @(posedge clk_i or negedge rst_ni)\n"
    "    if (!rst_ni)   count_o <= '0;\n"
    "    else if (en_i) count_o <= count_o + 1'b1;\n"
    "\n"
    "  assign irq_o = &count_o;\n"
    "endmodule\n"
)
CORE = (
    "module core (\n"
    "  input  logic       clk_i,\n"
    "  input  logic       rst_ni,\n"
    "  input  logic [1:0] en_i,\n"
    "  output logic [7:0] count_o,\n"
    "  output logic       irq_o,\n"
    "  output logic       rst_o\n"
    ");\n"
    "  logic       rst_sync_n;\n"
    "  logic       rst_slow_n;\n"
    "  logic [7:0] cnt;\n"
    "  logic       irq_raw;\n"
    "  logic       seen;\n"
    "  logic [1:0] split;\n"
    "  logic       floating;\n"
    "\n"
    "  rst_sync #(.STAGES(2)) u_sync (.clk_i(clk_i), .rst_ni(rst_ni), .rst_no(rst_sync_n));\n"
    "  rst_sync #(.STAGES(3)) u_slow (.clk_i(clk_i), .rst_ni(rst_ni), .rst_no(rst_slow_n));\n"
    "  timer u_timer (.clk_i(clk_i), .rst_ni(rst_sync_n), .en_i(en_i[0]), .count_o(cnt), .irq_o(irq_raw));\n"
    "\n"
    "  always_ff @(posedge clk_i or negedge rst_slow_n)\n"
    "    if (!rst_slow_n) seen <= 1'b0;\n"
    "    else             seen <= seen | irq_raw;\n"
    "\n"
    "  assign split[0] = en_i[0];\n"
    "  assign split[1] = en_i[1];\n"
    "  assign irq_o    = irq_raw & en_i[1] & seen & (split == en_i);\n"
    "  assign count_o  = cnt;\n"
    "  assign rst_o    = rst_sync_n;\n"
    "endmodule\n"
)
TOP = (
    "module top (\n"
    "  input  logic       clk_i,\n"
    "  input  logic       rst_ni,\n"
    "  input  logic [1:0] en_i,\n"
    "  output logic [7:0] count_o,\n"
    "  output logic       irq_o,\n"
    "  output logic       rst_o\n"
    ");\n"
    "  core u_core (\n"
    "    .clk_i   (clk_i),\n"
    "    .rst_ni  (rst_ni),\n"
    "    .en_i    (en_i),\n"
    "    .count_o (count_o),\n"
    "    .irq_o   (irq_o),\n"
    "    .rst_o   (rst_o)\n"
    "  );\n"
    "endmodule\n"
)
FILES = {"rst_sync": RST_SYNC, "timer": TIMER, "core": CORE, "top": TOP}


class Design:
    """top -> u_core (core) -> u_sync, u_slow (rst_sync, 2 and 3 stages), u_timer (timer)."""

    def __init__(self, root: Path, files: dict[str, str] | None = None) -> None:
        self.root = root.resolve()
        (self.root / "rtl").mkdir(parents=True)
        self.files = dict(FILES if files is None else files)
        for name, text in self.files.items():
            (self.root / "rtl" / f"{name}.sv").write_bytes(text.encode())
        self.filelist = self.root / "design.f"
        self.filelist.write_bytes("".join(f"rtl/{name}.sv\n" for name in self.files).encode())

    def path(self, name: str) -> Path:
        return self.root / "rtl" / f"{name}.sv"

    def vet(self) -> Vet:
        return Vet(Session(self.filelist, top="top"))

    def text(self, vet: Vet, name: str) -> str:
        vet.session.refresh()
        return vet.text(self.path(name)).decode()

    def changed(self, vet: Vet) -> set[str]:
        vet.session.refresh()
        return {n for n in self.files if vet.text(self.path(n)) != self.path(n).read_bytes()}

    def replace(self, vet: Vet, name: str, old: bytes, new: bytes, **options: object) -> None:
        """A raw splice of `new` over `old`; `options` go to `vet.splice` (for example `waiver=`)."""
        vet.session.refresh()
        data = vet.text(self.path(name))
        at = data.index(old)
        vet.splice(self.path(name), at, at + len(old), new, old=old, **options)


def statuses(vet: Vet) -> list[tuple[str, str, str]]:
    return [(r.op, r.target, r.status) for r in vet.log]


def with_enable(vet: Vet):
    """The enable as a script adds it: a port of `top` and of `core`, connected."""
    vet.punch_port("test_en_i", "input", to="top.u_core")
    return vet.module("core")


def records(result) -> list[tuple[str, str]]:
    return [(r.module, r.status) for r in result.equivalence]
