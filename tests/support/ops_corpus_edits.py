"""The script that test_ops_corpus.py and test_save_resume_corpus.py use."""

from __future__ import annotations

TOP = "demo_ss_bus_top"


def dft(vet):
    """Structure every variant keeps: a DFT enable into the top and one wrapper."""
    top = vet.module(TOP)
    top.add_port("dft_en_i", "input")
    top.add_wire("dft_en_q")
    top.add_assign("dft_en_q", "dft_en_i")
    for inst in top.instances(module="bag_rst_sync", count=4):
        inst.set_param("Stages", 3)
    vet.module("main_wrapper").add_port("dft_en_i", "input")
    top.instance("u_main_wrapper").connect("dft_en_i", "dft_en_i")
