from pathlib import Path

CELL = Path(__file__).resolve().parent.parent / "cells" / "tech_rst_sync.sv"


def apply(vet):
    vet.add_file(CELL)
    top = vet.module("soc_top")
    top.instance("u_rst_sync").replace_module(
        "tech_rst_sync",
        ports={"clk_i": "CK", "rst_ni": "RN", "rst_no": "Q"},
        drop_ports=["test_i"],
        connect={"SE": "1'b0"},
        unconnected=["SO"],
        drop_params=["STAGES"],
        set_params={"DEPTH": 3},
        waiver="a three-stage cell is wanted here, checked in the library flow, ticket LIB-215",
    )
