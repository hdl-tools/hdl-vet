from pathlib import Path


def apply(vet):
    vet.add_file(Path(__file__).resolve().parent.parent / "cells" / "tech_rst_sync.sv")
