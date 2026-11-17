"""Phase 5b acceptance on real hdl-bag output (spec: tests/cli/test_save_resume.py).

On demo_ss: the phase 3 DFT script and a phase 4 punch are applied and saved;
the phase 5 tech-cell swap is applied after a resume. The output tree is
byte-identical to one run of all three, and resuming does not load slower
than starting over.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import corpus
from repo import REPO

pytest.importorskip("ruamel.yaml", reason="hdl-bag (and its YAML library) not installed")

from hdl_vet.ops import Vet
from hdl_vet.output import write_tree
from hdl_vet.session import Session
from instances_corpus_edits import TECH_CELL, swap
from ops_corpus_edits import dft
from vetkit import tree_bytes

pytestmark = [
    pytest.mark.corpus,
    pytest.mark.skipif(
        not corpus.bag_available() and os.environ.get("HDL_VET_REQUIRE_CORPUS") != "1",
        reason="hdl-bag CLI (`bag`) is missing",
    ),
]

CACHE = REPO / ".corpus"
NAMES = [("dft.py", "11" * 32), ("punch.py", "22" * 32), ("swap.py", "33" * 32)]


@pytest.fixture(scope="module")
def demo():
    (d,) = [d for d in corpus.designs() if d.id == "demo_ss-flat"]
    return d.build(CACHE)


@pytest.fixture
def cell(tmp_path: Path) -> Path:
    path = tmp_path / "cells" / "tech_rst_sync.sv"
    path.parent.mkdir()
    path.write_bytes(TECH_CELL.encode())
    return path


def punch(vet: Vet) -> None:
    vet.punch_port("scan_mode_i", "input", to="demo_ss_bus_top.u_main_wrapper")


def test_save_and_resume_on_demo_ss_equals_one_run(demo, cell, tmp_path: Path):
    one = Vet(Session(demo.filelist, top=demo.top))
    dft(one)
    punch(one)
    swap(one, cell)
    r1 = one.finish(NAMES)
    assert r1.ok, r1.report()
    write_tree(one.session, r1, tmp_path / "one", scripts=NAMES)

    first = Vet(Session(demo.filelist, top=demo.top))
    dft(first)
    punch(first)
    first.save(tmp_path / "s.zip", scripts=NAMES[:2])

    t0 = time.perf_counter()
    back = Vet.restore(tmp_path / "s.zip")
    restore_time = time.perf_counter() - t0
    t0 = time.perf_counter()
    Session(demo.filelist, top=demo.top)
    fresh_time = time.perf_counter() - t0
    assert restore_time < 3 * fresh_time + 1.0, (restore_time, fresh_time)

    swap(back, cell)
    r2 = back.finish(NAMES[2:])
    assert r2.ok, r2.report()
    write_tree(back.session, r2, tmp_path / "two", scripts=back.session.scripts + NAMES[2:])
    assert tree_bytes(tmp_path / "two") == tree_bytes(tmp_path / "one")
    assert [rec.op for rec in back.log] == [rec.op for rec in one.log]


def test_the_archive_is_smaller_than_the_sources(demo, tmp_path: Path):
    vet = Vet(Session(demo.filelist, top=demo.top))
    dft(vet)
    vet.save(tmp_path / "s.zip")
    sources = sum(len(vet.session.original(p)) for p in vet.session.files)
    assert (tmp_path / "s.zip").stat().st_size < sources
