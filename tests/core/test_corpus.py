"""D1 acceptance: real hdl-bag output parses, round-trips and elaborates.

Needs the `bag` CLI on the test interpreter's PATH (pip install hdl-bag). Extra
spec variants come from an hdl-bag checkout: set HDL_BAG_REPO, or keep the checkout
next to this repo as ../hdl-bag. Without `bag` every test here is skipped; without
a checkout only the built-in demo_ss sample is exercised.

Interface under test (tests/support/corpus.py, a test helper, not part of the package):

    bag_available() -> bool             looks on PATH and next to sys.executable
    designs() -> list[CorpusDesign]     cheap: enumerates, generates nothing
    CorpusDesign.id        : str        e.g. "demo_ss-flat", "p3-axi_adapters-sv-interface"
    CorpusDesign.style     : "flat" | "sv-interface"
    CorpusDesign.build(cache: Path) -> Built    generates on first use, then reuses
    Built.outdir   : Path
    Built.filelist : Path
    Built.top      : str    the top recorded in bag_outputs.json
    Built.hashes   : dict[str, str]   the `files` map of bag_outputs.json
                                      (relative posix path -> sha256)

demo_ss is generated from <checkout>/tests/golden/demo_ss.ir.yaml when a checkout
is present, otherwise from `bag template --sample`, whose workbook is written once
into the cache and reused (each template write has a different sha256). Ids come
from the spec file name plus the style, never from the top module name.

Set HDL_VET_REQUIRE_CORPUS=1 (CI does) to make a missing `bag` a failure, not a skip.

The cache lives in <repo>/.corpus (git-ignored). A cached design is reused only
when the hdl-bag version and the spec's sha256 both match what built it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import corpus
from hdl_vet.filelist import read_filelist
from hdl_vet.slang_adapter import load
from repo import REPO

pytestmark = [
    pytest.mark.corpus,
    pytest.mark.skipif(
        not corpus.bag_available() and os.environ.get("HDL_VET_REQUIRE_CORPUS") != "1",
        reason="hdl-bag CLI (`bag`) not installed",
    ),
]

CACHE = REPO / ".corpus"
DESIGNS = corpus.designs() if corpus.bag_available() else []


@pytest.fixture(scope="module", params=DESIGNS, ids=lambda d: d.id)
def built(request):
    return request.param.build(CACHE)


def test_bag_cli_is_available():
    assert corpus.bag_available(), "HDL_VET_REQUIRE_CORPUS=1 but `bag` was not found"


def test_corpus_always_contains_the_sample_in_both_styles():
    ids = {d.id for d in DESIGNS}
    assert {"demo_ss-flat", "demo_ss-sv-interface"} <= ids


def test_corpus_ids_are_unique_and_sorted():
    ids = [d.id for d in DESIGNS]
    assert ids == sorted(set(ids))


def test_every_source_file_round_trips(built):
    design = load(built.filelist, top=built.top)
    assert design.files, "filelist produced no source files"
    bad = [p.name for p in design.files if design.roundtrip(p) != p.read_bytes()]
    assert bad == []


def test_elaborates_with_zero_errors(built):
    design = load(built.filelist, top=built.top)
    errors = [d for d in design.diagnostics() if d.severity == "error"]
    assert errors == [], "\n".join(f"{d.path}:{d.line} {d.code} {d.message}" for d in errors[:10])
    assert design.elaborated_instance_count() >= 1


def test_filelist_matches_what_the_adapter_loaded(built):
    assert load(built.filelist, top=built.top).files == read_filelist(built.filelist).files


# --- the demo_ss sample: numbers the handover doc reports -------------------


@pytest.fixture(scope="module")
def demo():
    (d,) = [d for d in DESIGNS if d.id == "demo_ss-flat"]
    return d.build(CACHE)


def test_demo_shape_matches_the_handover_doc(demo):
    assert demo.top == "demo_ss_bus_top"
    assert len(demo.hashes) == 132
    fl = read_filelist(demo.filelist)
    assert len(fl.files) == 83
    assert len(fl.incdirs) == 2


def test_demo_wrappers_have_no_macro_generated_instances(demo):
    # The architecture's "low risk" verdict for generated wrappers rests on this.
    design = load(demo.filelist, top=demo.top)
    for module in ("demo_ss_bus_top", "main_wrapper", "aon_wrapper", "per_wrapper"):
        sites = design.instance_sites(module)
        assert sites, module
        assert [s.instance_name for s in sites if s.from_macro] == [], module
        data = {p: p.read_bytes() for p in {s.path for s in sites}}
        for s in sites:
            text = data[s.path][s.start : s.end]
            assert text.startswith(s.module_name.encode()), (module, s.instance_name)
            assert text.endswith(b";"), (module, s.instance_name)


def test_demo_elaborated_size_is_in_the_reported_range(demo):
    # Doc: "about 4,800 instances". A loose band so a counting-convention
    # difference shows up as a failure to investigate, not a silent pass.
    n = load(demo.filelist, top=demo.top).elaborated_instance_count()
    assert 4000 <= n <= 6000, n


def test_generation_is_byte_deterministic(demo, tmp_path: Path):
    (d,) = [d for d in DESIGNS if d.id == "demo_ss-flat"]
    again = d.build(tmp_path / "fresh")
    assert again.outdir != demo.outdir
    assert again.hashes == demo.hashes


def test_sv_interface_sample_uses_the_flat_harness_top():
    (d,) = [d for d in DESIGNS if d.id == "demo_ss-sv-interface"]
    assert d.build(CACHE).top == "demo_ss_bus_top_flat"
