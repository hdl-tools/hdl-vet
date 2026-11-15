"""Phase 4 acceptance on real hdl-bag output and its regeneration variants.

Three levels of demo_ss: demo_ss_bus_top -> u_main_wrapper -> u_per_wrapper.
A DFT input is punched from the top into per_wrapper; a debug output is punched
from a net inside a wrapper up to the top. The debug nets are select vectors
with one bit per slave, so their width follows the design when a slave is
added or removed. Scripts only touch generated wrapper files.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import corpus

pytest.importorskip("ruamel.yaml", reason="hdl-bag (and its YAML library) not installed")

import variants
from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session

pytestmark = [
    pytest.mark.corpus,
    pytest.mark.skipif(
        (not corpus.bag_available() or not variants.variants())
        and os.environ.get("HDL_VET_REQUIRE_CORPUS") != "1",
        reason="hdl-bag CLI (`bag`) or an hdl-bag checkout (for the variants) is missing",
    ),
]

CACHE = Path(__file__).resolve().parent.parent / ".corpus"
TOP = "demo_ss_bus_top"
MAIN = f"{TOP}.u_main_wrapper"
PER = f"{MAIN}.u_per_wrapper"
AON = f"{TOP}.u_aon_wrapper"
FIFO = f"{MAIN}.u_cpu0_fifo_2_main_xbar"


def edits(diff: str) -> list[str]:
    return [
        line
        for line in diff.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---")) and "Modified by hdl-vet" not in line
    ]


def fresh(built) -> Vet:
    return Vet(Session(built.filelist, top=built.top))


@pytest.fixture(scope="module")
def demo():
    (d,) = [d for d in corpus.designs() if d.id == "demo_ss-flat"]
    return d.build(CACHE)


def comma(line: str) -> list[str]:
    return ["-" + line, "+" + line + ","]


def test_golden_dft_input_through_three_levels(demo):
    vet = fresh(demo)
    vet.punch_port("dft_en_i", "input", to=PER)
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        # rtl/demo_ss_bus_top.sv
        *comma("    input  logic wdt0_apb_pslverr"),
        "+    input  logic dft_en_i",
        *comma("      .uart0_apb4_pslverr(uart0_apb_pslverr)"),
        "+      .dft_en_i(dft_en_i)",
        # rtl/main_wrapper.sv
        *comma("    input  logic uart0_apb4_pslverr"),
        "+    input  logic dft_en_i",
        *comma("      .uart0_apb4_pslverr(uart0_apb4_pslverr)"),
        "+      .dft_en_i(dft_en_i)",
        # rtl/per_wrapper.sv
        *comma("    input  logic uart0_apb4_pslverr"),
        "+    input  logic dft_en_i",
    ]
    assert sorted(p.name for p in r.files) == ["demo_ss_bus_top.sv", "main_wrapper.sv", "per_wrapper.sv"]
    assert {(c.kind, c.path, c.name) for c in r.changes} == {
        ("port-added", TOP, "dft_en_i"),
        ("port-added", MAIN, "dft_en_i"),
        ("port-added", PER, "dft_en_i"),
        ("connection-changed", MAIN, "dft_en_i"),
        ("connection-changed", PER, "dft_en_i"),
    }
    assert [rec.status for rec in vet.log] == ["applied"] * 5
    assert vet.session.reloads == 1


def test_golden_debug_output_through_three_levels(demo):
    vet = fresh(demo)
    vet.punch_port("periph_sel_dbg_o", "output", to=PER, signal="u_periph_apb_m_psel")
    r = vet.finish()
    assert r.ok, r.report()
    port = "+    output logic [3:0] periph_sel_dbg_o"
    conn = "+      .periph_sel_dbg_o(periph_sel_dbg_o)"
    assert edits(r.diff) == [
        # rtl/demo_ss_bus_top.sv
        *comma("    input  logic wdt0_apb_pslverr"),
        port,
        *comma("      .uart0_apb4_pslverr(uart0_apb_pslverr)"),
        conn,
        # rtl/main_wrapper.sv
        *comma("    input  logic uart0_apb4_pslverr"),
        port,
        *comma("      .uart0_apb4_pslverr(uart0_apb4_pslverr)"),
        conn,
        # rtl/per_wrapper.sv
        *comma("    input  logic uart0_apb4_pslverr"),
        port,
        "+  assign periph_sel_dbg_o = u_periph_apb_m_psel;",
    ]
    assert {(c.path, c.after) for c in r.changes if c.kind == "port-added"} == {
        (TOP, "output 4"),
        (MAIN, "output 4"),
        (PER, "output 4"),
    }
    assert vet.session.reloads == 1


def test_propagate_an_adapter_parameter_to_the_top(demo):
    vet = fresh(demo)
    before = vet.session.baseline_snapshot()[FIFO].params
    vet.propagate_param("Depth", to=FIFO)
    r = vet.finish()
    assert r.ok, r.report()
    assert {(c.kind, c.path, c.name) for c in r.changes} == {
        ("param-changed", TOP, "Depth"),
        ("param-changed", MAIN, "Depth"),
    }
    assert {c.after for c in r.changes} == {before["Depth"]}
    assert vet.session.design.snapshot()[FIFO].params == before
    added = [line for line in edits(r.diff) if line.startswith("+")]
    assert sum(".Depth" in line and "(Depth)" in line.replace(" ", "") for line in added) == 2


def test_refusals_on_demo_ss(demo):
    vet = fresh(demo)
    with pytest.raises(VetError, match="did you mean"):
        vet.punch_port("x_i", "input", to=f"{MAIN}.u_per_wraper")
    with pytest.raises(VetError, match="u_periph_apb_m_pselx"):
        vet.punch_port("x_o", "output", to=PER, signal="u_periph_apb_m_pselx")
    assert vet.log == [] and vet.finish().files == {}


# --- regeneration variants --------------------------------------------------------------


def script(vet):
    vet.punch_port("dft_en_i", "input", to=PER)
    vet.punch_port("periph_sel_dbg_o", "output", to=PER, signal="u_periph_apb_m_psel")
    vet.punch_port("aon_sel_dbg_o", "output", to=AON, signal="u_aon_apb_m_psel")


# select-vector widths: (periph slaves, aon slaves)
WIDTHS = {
    "add_slave": (4, 3),
    "remove_slave": (3, 2),
    "width_change": (4, 2),
    "adapter_change": (4, 2),
    "rename_slave": (4, 2),
    "add_master": (4, 2),
}


@pytest.mark.parametrize("name", variants.VARIANT_NAMES)
def test_punched_widths_follow_the_design(name):
    built = variants.build(name, CACHE)
    vet = fresh(built)
    script(vet)
    r = vet.finish()
    assert r.ok, r.report()
    periph, aon = WIDTHS[name]
    added = [line for line in edits(r.diff) if line.startswith("+")]
    assert added.count(f"+    output logic [{periph - 1}:0] periph_sel_dbg_o") == 2
    assert f"+    output logic [{periph - 1}:0] periph_sel_dbg_o," in added  # the top: aon port follows
    assert added.count(f"+    output logic [{aon - 1}:0] aon_sel_dbg_o") == 2
    ports = {(c.path, c.name): c.after for c in r.changes if c.kind == "port-added"}
    assert ports[(TOP, "periph_sel_dbg_o")] == f"output {periph}"
    assert ports[(TOP, "aon_sel_dbg_o")] == f"output {aon}"
    assert ports[(PER, "dft_en_i")] == "input 1"
    assert all(rec.status == "applied" for rec in vet.log)
