"""Phase 3 acceptance on real hdl-bag output and its regeneration variants.

Golden: one script applied to demo_ss-flat changes exactly the lines listed
here, byte for byte; nothing else in the generated text moves.

Regeneration: the same scripts applied to six variants of demo_ss (see
tests/support/variants.py: a slave added, removed, renamed; a width changed; an adapter
changed; a master added) either apply and pass sign-off, or fail naming the
target that is gone or no longer fits.

Scripts only touch generated wrapper files; vendored and in-house modules are
out of scope for v1.
"""

from __future__ import annotations

import os

import pytest

import corpus
from repo import REPO

pytest.importorskip("ruamel.yaml", reason="hdl-bag (and its YAML library) not installed")

import variants
from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session
from ops_corpus_edits import TOP, dft

pytestmark = [
    pytest.mark.corpus,
    pytest.mark.skipif(
        (not corpus.bag_available() or not variants.variants()) and os.environ.get("HDL_VET_REQUIRE_CORPUS") != "1",
        reason="hdl-bag CLI (`bag`) or an hdl-bag checkout (for the variants) is missing",
    ),
]

CACHE = REPO / ".corpus"
SYNCS = ["u_bag_rst_sync_D_AON", "u_bag_rst_sync_D_CPU", "u_bag_rst_sync_D_PER", "u_bag_rst_sync_D_SYS"]


# --- the script set --------------------------------------------------------------


def tap_wdt(vet):
    top = vet.module(TOP)
    top.add_port("wdt_sel_dbg_o", "output")
    top.add_assign("wdt_sel_dbg_o", "wdt0_apb_psel")


def tap_spi(vet):
    top = vet.module(TOP)
    top.add_port("spi_sel_dbg_o", "output")
    top.add_assign("spi_sel_dbg_o", "spi0_apb_psel")


def tap_uart_addr(vet):
    top = vet.module(TOP)
    top.add_port("uart_addr_dbg_o", "output", width=12)
    top.add_assign("uart_addr_dbg_o", "uart0_apb_paddr")


def touch_sram_cut(vet):
    vet.module("main_wrapper").instance("u_main_xbar_cut_2_sram0").connect("clk_i", "clk_sys_typo", was="nothing")


ALL = [dft, tap_wdt, tap_spi, tap_uart_addr]

# variant -> (script that must fail there, text the failure must contain)
BREAKS = {
    "add_slave": None,
    "add_master": None,
    "remove_slave": (tap_spi, "spi0_apb_psel"),
    "rename_slave": (tap_wdt, "wdt0_apb_psel"),
    "width_change": (tap_uart_addr, "WidthTruncate"),
    "adapter_change": (touch_sram_cut, "u_main_xbar_cut_2_sram0"),
}


def edits(diff: str) -> list[str]:
    return [
        line
        for line in diff.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---")) and "Modified by hdl-vet" not in line
    ]


def apply(built, scripts):
    vet = Vet(Session(built.filelist, top=built.top))
    for script in scripts:
        script(vet)
    return vet, vet.finish([(s.__name__ + ".py", "00" * 32) for s in scripts])


@pytest.fixture(scope="module")
def demo():
    (d,) = [d for d in corpus.designs() if d.id == "demo_ss-flat"]
    return d.build(CACHE)


# --- golden ------------------------------------------------------------------------


def test_golden_edits_on_demo_ss(demo):
    vet, r = apply(demo, [dft])
    assert r.ok, r.report()
    sync_lines = [
        "-      .AsyncAssert(1'b1)",
        "+      .AsyncAssert(1'b1),",
        "+      .Stages     (3)",
    ]
    assert edits(r.diff) == [
        # rtl/demo_ss_bus_top.sv
        "-    input  logic wdt0_apb_pslverr",
        "+    input  logic wdt0_apb_pslverr,",
        "+    input  logic dft_en_i",
        "+  logic dft_en_q;",
        *sync_lines * 4,
        "-      .uart0_apb4_pslverr(uart0_apb_pslverr)",
        "+      .uart0_apb4_pslverr(uart0_apb_pslverr),",
        "+      .dft_en_i(dft_en_i)",
        "+  assign dft_en_q = dft_en_i;",
        # rtl/main_wrapper.sv
        "-    input  logic uart0_apb4_pslverr",
        "+    input  logic uart0_apb4_pslverr,",
        "+    input  logic dft_en_i",
    ]
    assert sorted(p.name for p in r.files) == ["demo_ss_bus_top.sv", "main_wrapper.sv"]
    assert {(c.kind, c.path, c.name) for c in r.changes} == {
        ("port-added", TOP, "dft_en_i"),
        ("port-added", f"{TOP}.u_main_wrapper", "dft_en_i"),
        ("connection-changed", f"{TOP}.u_main_wrapper", "dft_en_i"),
        *{("param-changed", f"{TOP}.{s}", "Stages") for s in SYNCS},
    }
    assert [rec.status for rec in vet.log] == ["applied"] * 9


def test_all_scripts_together_on_demo_ss(demo):
    vet, r = apply(demo, ALL)
    assert r.ok, r.report()
    assert all(rec.status == "applied" for rec in vet.log)


def test_sram_cut_selector_resolves_on_demo_ss(demo):
    vet = Vet(Session(demo.filelist, top=demo.top))
    inst = vet.module("main_wrapper").instance("u_main_xbar_cut_2_sram0")
    assert inst.paths == [f"{TOP}.u_main_wrapper.u_main_xbar_cut_2_sram0"]


# --- regeneration variants ------------------------------------------------------------


def test_variants_are_available():
    assert sorted(v.id for v in variants.variants()) == sorted(
        f"demo_ss-var-{name}-flat" for name in variants.VARIANT_NAMES
    )


@pytest.mark.parametrize("name", variants.VARIANT_NAMES)
def test_scripts_survive_regeneration_or_fail_naming_the_target(name):
    built = variants.build(name, CACHE)
    broken = BREAKS[name]
    surviving = [s for s in ALL if broken is None or s is not broken[0]]

    vet, r = apply(built, surviving)
    assert r.ok, r.report()
    assert all(rec.status == "applied" for rec in vet.log)
    assert "+    input  logic dft_en_i" in edits(r.diff)

    if broken is None:
        return
    script, needle = broken
    try:
        _, r = apply(built, [script])
    except VetError as e:
        assert needle in str(e)
        if script is touch_sram_cut:
            assert "did you mean" in str(e) and "'u_main_xbar_fifo_2_sram0'" in str(e)
    else:
        assert not r.ok
        assert needle in r.report()


def test_variant_inputs_are_untouched():
    built = variants.build("add_slave", CACHE)
    before = {p: p.read_bytes() for p in (built.outdir / "rtl").glob("*.sv")}
    apply(built, ALL)
    assert {p: p.read_bytes() for p in (built.outdir / "rtl").glob("*.sv")} == before
