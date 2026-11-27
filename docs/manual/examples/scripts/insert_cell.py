from pathlib import Path

CELL = Path(__file__).resolve().parent.parent / "cells" / "tech_iso.sv"


def apply(vet):
    vet.add_file(CELL)
    top = vet.module("soc_top")
    top.add_port("iso_en_i", "input")
    top.insert_cell_on_net(
        "rst_sync_n",
        "tech_iso",
        name="u_iso_rst",
        data_in="A",
        data_out="Z",
        enable_pin="ISO",
        enable="iso_en_i",
    )
