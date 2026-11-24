from pathlib import Path

CELL = Path(__file__).resolve().parent.parent / "cells" / "tech_rst_sync.sv"


def apply(vet):
    vet.add_file(CELL)
    top = vet.module("soc_top")
    top.instance("u_rst_sync").replace_module(
        "tech_rst_sync",
        ports={"clk_i": "CK", "rst_ni": "RN", "test_i": "SE", "rst_no": "Q"},
        params={"STAGES": "DEPTH"},
        unconnected=["SO"],
        was="rst_sync",
        waiver="the cell is checked against rst_sync in the library flow, ticket LIB-214",
    )
