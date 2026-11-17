"""`override_net` on the generated design: the acceptance test of phase 6, part 2.

The plan's acceptance: a DFT override mux on the four reset synchronisers of
demo_ss is proved inert when its enable is tied off. The four synchronisers are
instances in `demo_ss_bus_top`, each on a clock of its own; their output nets are
`bag_rst_D_<DOMAIN>_n`, and the override passes the raw reset `rst_<domain>_n`.

C1  The four overrides are applied, the sign-off is ok, and there is one record
    for `demo_ss_bus_top` with status "proved". The children of the top are not
    looked into: none of them changed.
C2  A second run of the script on the result is skipped operation by operation.
C3  With one of the four overrides wired the wrong way round, the sign-off fails
    and the record is "refuted".
C4  The check of this module, with about twenty children, takes less than two
    minutes.
C5  (From the review.) An override of an output that an instance drives in
    `bag_axi_dw`, which is elaborated with
    three sets of parameter values and has children that did not change, is
    proved for each of the three sets. Two of them were answered "the child
    i_dw is named in same but is not the same" by a comparison that depended
    on the numbers the reader gives to the bits of a netlist.
"""

from __future__ import annotations

import os
import time

import pytest

import corpus
from hdl_vet import equiv
from hdl_vet.ops import Vet
from hdl_vet.session import Session
from repo import REPO

pytestmark = [
    pytest.mark.corpus,
    pytest.mark.equiv,
    pytest.mark.bigproof,
    pytest.mark.skipif(
        not corpus.bag_available() and os.environ.get("HDL_VET_REQUIRE_CORPUS") != "1",
        reason="hdl-bag CLI (`bag`) is missing",
    ),
    pytest.mark.skipif(
        not equiv.available() and os.environ.get("HDL_VET_REQUIRE_EQUIV") != "1",
        reason="Yosys (hdl-vet[equiv]) is missing",
    ),
]

CACHE = REPO / ".corpus"
TOP = "demo_ss_bus_top"
DOMAINS = ["AON", "CPU", "PER", "SYS"]


def dft_bypass(vet):
    top = vet.module(TOP)
    top.add_port("dft_en_i", "input")
    for domain in DOMAINS:
        top.override_net(f"bag_rst_D_{domain}_n", f"rst_{domain.lower()}_n", enable="dft_en_i")


@pytest.fixture(scope="module")
def demo():
    (d,) = [d for d in corpus.designs() if d.id == "demo_ss-flat"]
    return d.build(CACHE)


def test_the_bypass_of_the_four_reset_synchronisers_is_proved(demo):
    vet = Vet(Session(demo.filelist, top=demo.top))
    started = time.monotonic()
    dft_bypass(vet)
    result = vet.finish()
    took = time.monotonic() - started
    assert result.ok, result.report()
    assert [(r.module, r.status) for r in result.equivalence] == [(TOP, "proved")]
    (record,) = result.equivalence
    assert record.operations == [f"override_net {TOP}.bag_rst_D_{d}_n" for d in DOMAINS]
    assert f"equivalence proved: {TOP} " in result.report()
    assert "equal when dft_en_i is 0" in result.report()
    for domain in DOMAINS:
        assert (
            f"assign bag_rst_D_{domain}_n = dft_en_i ? rst_{domain.lower()}_n : bag_rst_D_{domain}_n_pre;"
            in result.diff
        )
    assert took < 120


def test_a_second_run_is_skipped(demo):
    vet = Vet(Session(demo.filelist, top=demo.top))
    dft_bypass(vet)
    dft_bypass(vet)
    statuses = [r.status for r in vet.log if r.op == "override_net"]
    assert statuses == ["applied"] * 4 + ["skipped"] * 4
    assert vet.finish().ok


def test_a_bypass_wired_the_wrong_way_round_is_refuted(demo):
    vet = Vet(Session(demo.filelist, top=demo.top))
    dft_bypass(vet)
    vet.session.refresh()
    top_file = demo.outdir / "rtl" / f"{TOP}.sv"
    data = vet.text(top_file)
    old = b"dft_en_i ? rst_cpu_n : bag_rst_D_CPU_n_pre"
    at = data.index(old)
    vet.splice(top_file, at, at + len(old), b"dft_en_i ? bag_rst_D_CPU_n_pre : rst_cpu_n", old=old)
    result = vet.finish()
    assert not result.ok
    assert [(r.module, r.status) for r in result.equivalence] == [(TOP, "refuted")]
    assert "not equivalent: " + TOP in result.report()


def test_an_override_in_a_module_with_three_parameter_sets_and_unchanged_children(demo):
    """C5."""
    vet = Vet(Session(demo.filelist, top=demo.top))
    vet.punch_port("dft_en_i", "input", to=f"{TOP}.u_main_wrapper.u_cpu0_dw_2_main_xbar", all_instances=True)
    vet.module("bag_axi_dw", instances=3).override_net("m_arlock_o", "1'b0", enable="dft_en_i")
    result = vet.finish()
    found = [(r.module, r.status, r.detail) for r in result.equivalence]
    assert [f[:2] for f in found] == [("bag_axi_dw", "proved")] * 3, found
    assert len({tuple(sorted(r.params.items())) for r in result.equivalence}) == 3
    assert result.ok, result.report()
