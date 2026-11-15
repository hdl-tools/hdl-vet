"""Phase 5 acceptance on real hdl-bag output (spec: tests/test_instances.py).

- The reset synchroniser of every clock domain of demo_ss is swapped for a
  tech cell that a script adds; the same script applies to all six
  regeneration variants.
- A swap whose ports do not fit is refused before anything is written.
- A synchroniser is bypassed: removed and its output tied to its input, as
  one group; the removal alone is refused.
- The output tree carries the added cell and is a loadable design.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import corpus

pytest.importorskip("ruamel.yaml", reason="hdl-bag (and its YAML library) not installed")

import variants
from hdl_vet.ops import Vet, VetError
from hdl_vet.output import write_tree
from hdl_vet.session import Session
from hdl_vet.slang_adapter import load

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
DOMAINS = ["AON", "CPU", "PER", "SYS"]

TECH_CELL = (
    "// A stand-in for a technology reset synchroniser cell.\n"
    "module tech_rst_sync #(\n"
    "    parameter int unsigned Stages = 2\n"
    ") (\n"
    "    input  logic CK,\n"
    "    input  logic RN,\n"
    "    output logic Q\n"
    ");\n"
    "  logic [Stages-1:0] q;\n"
    "  always_ff @(posedge CK or negedge RN) begin\n"
    "    if (!RN) q <= '0;\n"
    "    else q <= {q[Stages-2:0], 1'b1};\n"
    "  end\n"
    "  assign Q = q[Stages-1];\n"
    "endmodule\n"
)
# The same cell with a two-bit output: it does not fit a one-bit reset.
WIDE_CELL = TECH_CELL.replace("tech_rst_sync", "wide_rst_sync").replace(
    "output logic Q", "output logic [1:0] Q"
).replace("assign Q = q[Stages-1];", "assign Q = {2{q[Stages-1]}};")


@pytest.fixture
def cell(tmp_path: Path) -> Path:
    path = tmp_path / "cells" / "tech_rst_sync.sv"
    path.parent.mkdir()
    path.write_bytes(TECH_CELL.encode())
    (path.parent / "wide_rst_sync.sv").write_bytes(WIDE_CELL.encode())
    return path


def swap(vet: Vet, cell: Path, module: str = "tech_rst_sync") -> None:
    vet.add_file(cell.parent / f"{module}.sv")
    top = vet.module(TOP)
    for inst in top.instances(module="bag_rst_sync", count=4):
        inst.replace_module(
            module,
            ports={"clk_i": "CK", "rst_i": "RN", "rst_no": "Q"},
            drop_params=["ActiveLow", "AsyncAssert"],
            was="bag_rst_sync",
        )


def edits(diff: str) -> list[str]:
    return [
        line
        for line in diff.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---")) and "Modified by hdl-vet" not in line
    ]


@pytest.fixture(scope="module")
def demo():
    (d,) = [d for d in corpus.designs() if d.id == "demo_ss-flat"]
    return d.build(CACHE)


def test_reset_synchronisers_swapped_for_a_tech_cell_on_demo_ss(demo, cell):
    vet = Vet(Session(demo.filelist, top=demo.top))
    swap(vet, cell)
    r = vet.finish([("swap.py", "00" * 32)])
    assert r.ok, r.report()

    expected: list[str] = []
    for dom in DOMAINS:
        low = dom.lower()
        expected += [
            "-  bag_rst_sync #(",
            "-      .ActiveLow  (1'b1),",
            "-      .AsyncAssert(1'b1)",
            f"-  ) u_bag_rst_sync_D_{dom} (",
            f"-      .clk_i (clk_{low}),",
            f"-      .rst_i (rst_{low}_n),",
            f"-      .rst_no(bag_rst_D_{dom}_n)",
            f"+  tech_rst_sync u_bag_rst_sync_D_{dom} (",
            f"+      .CK (clk_{low}),",
            f"+      .RN (rst_{low}_n),",
            f"+      .Q(bag_rst_D_{dom}_n)",
        ]
    assert edits(r.diff) == expected
    assert [p.name for p in r.files] == ["demo_ss_bus_top.sv"]
    assert r.added == {"hdl_vet_added/tech_rst_sync.sv": TECH_CELL.encode()}
    paths = {f"{TOP}.u_bag_rst_sync_D_{dom}" for dom in DOMAINS}
    assert {c.path for c in r.changes if c.kind == "module-changed"} == paths
    assert {c.path for c in r.changes} == paths  # nothing changed anywhere else
    assert [(rec.op, rec.status) for rec in vet.log] == [("add_file", "applied")] + [
        ("replace_module", "applied")
    ] * 4
    assert vet.notes == []


@pytest.mark.parametrize("name", variants.VARIANT_NAMES)
def test_the_swap_survives_regeneration(name, cell):
    built = variants.build(name, CACHE)
    vet = Vet(Session(built.filelist, top=built.top))
    swap(vet, cell)
    r = vet.finish()
    assert r.ok, r.report()
    assert all(rec.status == "applied" for rec in vet.log)
    assert len([c for c in r.changes if c.kind == "module-changed"]) == 4
    assert sum(1 for line in edits(r.diff) if line.startswith("+  tech_rst_sync u_bag_rst_sync_D_")) == 4


def test_a_cell_that_does_not_fit_is_refused_before_anything_is_written(demo, cell):
    vet = Vet(Session(demo.filelist, top=demo.top))
    with pytest.raises(VetError, match=r"replace_module demo_ss_bus_top\.u_bag_rst_sync_D_AON.*'Q'"):
        swap(vet, cell, module="wide_rst_sync")
    assert [(rec.op, rec.status) for rec in vet.log] == [("add_file", "applied")]
    top_file = vet.module(TOP).path
    assert vet.text(top_file) == top_file.read_bytes()
    assert vet.design.module(TOP).instance("u_bag_rst_sync_D_AON").module == "bag_rst_sync"


def test_a_synchroniser_is_bypassed_as_one_group(demo):
    vet = Vet(Session(demo.filelist, top=demo.top))
    top = vet.module(TOP)
    with pytest.raises(VetError, match="bag_rst_D_AON_n"):
        top.remove_instance("u_bag_rst_sync_D_AON")  # the domain's reset would have no driver
    assert vet.log == []
    with vet.group():
        top.remove_instance("u_bag_rst_sync_D_AON", module="bag_rst_sync")
        top.add_assign("bag_rst_D_AON_n", "rst_aon_n")
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        "-  bag_rst_sync #(",
        "-      .ActiveLow  (1'b1),",
        "-      .AsyncAssert(1'b1)",
        "-  ) u_bag_rst_sync_D_AON (",
        "-      .clk_i (clk_aon),",
        "-      .rst_i (rst_aon_n),",
        "-      .rst_no(bag_rst_D_AON_n)",
        "-  );",
        "+  assign bag_rst_D_AON_n = rst_aon_n;",
    ]
    assert {(c.kind, c.path) for c in r.changes} == {("instance-removed", f"{TOP}.u_bag_rst_sync_D_AON")}


def test_the_output_tree_carries_the_cell_and_loads(demo, cell, tmp_path: Path):
    vet = Vet(Session(demo.filelist, top=demo.top))
    swap(vet, cell)
    r = vet.finish()
    assert r.ok, r.report()
    out = write_tree(vet.session, r, tmp_path / "out")
    assert (out / "hdl_vet_added" / "tech_rst_sync.sv").read_bytes() == TECH_CELL.encode()
    manifest = json.loads((out / "hdl-vet.json").read_bytes())
    assert list(manifest["added"]) == ["hdl_vet_added/tech_rst_sync.sv"]
    filelist = out / manifest["filelist"]
    assert filelist.read_text(encoding="utf-8").splitlines()[-1].endswith("hdl_vet_added/tech_rst_sync.sv")
    d = load(filelist, top=demo.top)
    assert [g for g in d.diagnostics() if g.severity == "error"] == []
    snap = d.snapshot()
    assert {snap[f"{TOP}.u_bag_rst_sync_D_{dom}"].module for dom in DOMAINS} == {"tech_rst_sync"}


def test_the_swap_does_not_touch_the_input(demo, cell):
    before = {p: p.read_bytes() for p in (demo.outdir / "rtl").rglob("*.sv")}
    vet = Vet(Session(demo.filelist, top=demo.top))
    swap(vet, cell)
    assert vet.finish().ok
    assert {p: p.read_bytes() for p in (demo.outdir / "rtl").rglob("*.sv")} == before
    assert cell.read_bytes() == TECH_CELL.encode()
