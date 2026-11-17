"""Reusing parse trees across the loads of one session (`slang_adapter.LoadCache`).

Why. A re-load re-parsed every source file although an edit touches two or
three. Parsing is about 85% of a re-load. A measured experiment showed slang
lets a parse tree be added to a later compilation, also after the earlier one
was elaborated, frozen and analysed.

The rule: a load given a LoadCache must be indistinguishable from a load without
one. Everything observable on the returned Design is equal: diagnostics (every
field), snapshot, modules(), every module()'s source model with its byte ranges,
included, included_bytes, roundtrip of every file, instance sites, drivers,
elaborated paths. Designs returned by earlier loads stay valid and unchanged.

How (all inside the package slang_adapter; nothing else imports pyslang)
    - A LoadCache keeps one slang driver / source manager and, per source file,
      the tree it parsed together with what it was parsed from: the source bytes
      and the bytes of every header that source included, directly or not.
    - A later load with the same cache reuses a source's tree when its bytes and
      all those header bytes are unchanged, and parses only the others.
    - slang refuses a second buffer under a path it already holds. A changed
      source is therefore parsed under a new synthetic file name in the same
      mirrored directory (its relative includes resolve as before); the name
      never shows: every path the Design reports is the real one.
    - A header cannot be re-registered and its name is fixed by the include
      directives. When any header's bytes differ from what the cache's source
      manager holds, the cache starts over with a new source manager and parses
      everything (headers are few and rarely edited). In general: whenever
      reuse is not provably safe, start over; correctness never depends on a
      tree being reused.
    - `Design.included` reports only headers reached from the trees of THAT
      design, not headers that an earlier version of a file included.
    - A cache is bound to one file list (already) and one `top`; a load with
      another `top` raises AdapterError.
    - A load that raises (invalid UTF-8, top not found) leaves the cache usable:
      the next load with good text equals a fresh load.

    LoadCache gains
        parsed: int        number of source files parsed by loads using this
                           cache, in total (reused trees do not count)
        reuse: bool        constructor argument, default True; False keeps the
                           file-system caching but parses every file every time

Session needs no change in behaviour: it already passes one LoadCache to all its
loads. Its baseline Design is kept for sign-off while later loads share trees
with it, so the baseline's diagnostics and snapshot must be unaffected by them.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest

from hdl_vet import slang_adapter
from hdl_vet.filelist import read_filelist
from hdl_vet.ops import Vet
from hdl_vet.session import Session
from hdl_vet.slang_adapter import AdapterError, LoadCache, load
from repo import REPO
from tree_reuse_tools import describe, differences, write
from vetkit import make_design, span


def texts_of(fl, design) -> dict[Path, bytes]:
    return {**{p: p.read_bytes() for p in fl.files}, **design.included_bytes}


def count_parses(monkeypatch) -> list[str]:
    """Names of the source files parsed from now on."""
    parsed: list[str] = []
    original = slang_adapter._Loader.tree

    def tree(self, src, *a, **k):
        parsed.append(Path(src).name)
        return original(self, src, *a, **k)

    monkeypatch.setattr(slang_adapter._Loader, "tree", tree)
    return parsed


@pytest.fixture
def kit(tmp_path: Path):
    return make_design(tmp_path)


# --- equivalence on the small design ---------------------------------------------------


EDITS = [
    # (file attribute, old, new): each applied on top of the previous ones
    ("mid", b"  leaf #(.WIDTH(4)) u_l0", b"  // a comment\n  leaf #(.WIDTH(4)) u_l0"),
    ("top", b"assign spare_d = a;", b"assign spare_d = ~a;"),
    ("mid", b".DEPTH(3)", b".DEPTH(5)"),
    ("leaf", b"q <= d;", b"q <= d_typo;"),  # introduces an error
    ("leaf", b"q <= d_typo;", b"q <= d;"),  # and removes it again
    ("top", b"spare_q", b"spare_q2"),  # moves a baseline warning's text
    ("mid", b"  // a comment\n", b""),  # back to an earlier text of the same file
]


def test_every_load_with_a_cache_equals_a_fresh_load(kit, monkeypatch):
    fl = read_filelist(kit.filelist)
    fresh = load(fl, top="top")
    texts = texts_of(fl, fresh)
    cache = LoadCache()
    first = load(fl, top="top", overrides=texts, cache=cache)
    assert differences(describe(first), describe(fresh)) == []
    assert cache.parsed == len(fl.files)

    kept = [(first, describe(fresh))]
    for attr, old, new in EDITS:
        path = getattr(kit, attr)
        assert old in texts[path], (attr, old)
        texts = {**texts, path: texts[path].replace(old, new, 1)}
        before = cache.parsed
        parsed = count_parses(monkeypatch)
        reused = load(fl, top="top", overrides=texts, check_overrides=False, cache=cache)
        monkeypatch.undo()
        want = describe(load(fl, top="top", overrides=texts, check_overrides=False))
        assert differences(describe(reused), want) == [], (attr, old)
        assert parsed == [path.name], "only the changed source is parsed"
        assert cache.parsed == before + 1
        kept.append((reused, want))

    # designs from earlier loads are still valid and unchanged
    for design, want in kept:
        assert differences(describe(design), want) == []


def test_a_second_load_of_unchanged_text_parses_nothing(kit, monkeypatch):
    fl = read_filelist(kit.filelist)
    texts = texts_of(fl, load(fl, top="top"))
    cache = LoadCache()
    a = load(fl, top="top", overrides=texts, cache=cache)
    parsed = count_parses(monkeypatch)
    b = load(fl, top="top", overrides=texts, cache=cache)
    assert parsed == [] and cache.parsed == len(fl.files)
    assert a is not b
    assert differences(describe(a), describe(b)) == []


def test_reuse_can_be_switched_off(kit, monkeypatch):
    fl = read_filelist(kit.filelist)
    texts = texts_of(fl, load(fl, top="top"))
    cache = LoadCache(reuse=False)
    load(fl, top="top", overrides=texts, cache=cache)
    parsed = count_parses(monkeypatch)
    again = load(fl, top="top", overrides=texts, cache=cache)
    assert sorted(parsed) == sorted(p.name for p in fl.files)
    assert differences(describe(again), describe(load(fl, top="top"))) == []


def test_diagnostics_and_ranges_of_a_reparsed_file_use_its_real_path_and_new_text(kit):
    fl = read_filelist(kit.filelist)
    texts = texts_of(fl, load(fl, top="top"))
    cache = LoadCache()
    load(fl, top="top", overrides=texts, cache=cache)
    new = b"// pushed down\n" + texts[kit.top].replace(b"assign spare_d = a;", b"assign spare_d = nope;")
    design = load(fl, top="top", overrides={**texts, kit.top: new}, check_overrides=False, cache=cache)
    (err,) = [d for d in design.diagnostics() if d.code == "UndeclaredIdentifier"]
    assert err.path == kit.top
    assert err.offset == span(new, b"nope")[0] and err.line == new[: err.offset].count(b"\n") + 1
    inst = design.module("top").instance("u_mid")
    assert inst.path == kit.top and new[inst.start : inst.start + 5] == b"u_mid"
    assert design.roundtrip(kit.top) == new
    assert design.files == fl.files


# --- headers ---------------------------------------------------------------------------

HDR = {
    "inc/w.svh": '`include "deep.svh"\n`define W 4\n',
    "inc/deep.svh": "`define D 2\n",
    "inc/other.svh": "`define O 3\n",
    "rtl/leaf.sv": '`include "w.svh"\nmodule leaf(input logic [`W-1:0] a);\nendmodule\n',
    "rtl/solo.sv": '`include "other.svh"\nmodule solo(input logic [`O-1:0] a);\nendmodule\n',
    "rtl/t.sv": "module t(input logic [3:0] a);\n  leaf u_leaf (.a(a));\n  solo u_solo (.a(a[2:0]));\nendmodule\n",
}
HDR_F = "+incdir+inc\nrtl/leaf.sv\nrtl/solo.sv\nrtl/t.sv\n"


def hdr_setup(tmp_path: Path):
    fl = read_filelist(write(tmp_path, HDR, HDR_F))
    texts = texts_of(fl, load(fl, top="t"))
    cache = LoadCache()
    load(fl, top="t", overrides=texts, cache=cache)
    return fl, texts, cache


def test_a_header_edit_is_seen_and_still_equals_a_fresh_load(tmp_path: Path):
    fl, texts, cache = hdr_setup(tmp_path)
    w = (tmp_path / "inc" / "w.svh").resolve()
    for body in (b'`include "deep.svh"\n`define W 8\n', b'`include "deep.svh"\n`define W 2\n', texts[w]):
        t2 = {**texts, w: body}
        got = load(fl, top="t", overrides=t2, check_overrides=False, cache=cache)
        want = load(fl, top="t", overrides=t2, check_overrides=False)
        assert differences(describe(got), describe(want)) == [], body
    # a nested header too
    deep = (tmp_path / "inc" / "deep.svh").resolve()
    t3 = {**texts, deep: b"`define D 9\n"}
    got = load(fl, top="t", overrides=t3, check_overrides=False, cache=cache)
    assert differences(describe(got), describe(load(fl, top="t", overrides=t3, check_overrides=False))) == []


def test_sources_after_a_header_edit_are_reused_again(tmp_path: Path, monkeypatch):
    fl, texts, cache = hdr_setup(tmp_path)
    w = (tmp_path / "inc" / "w.svh").resolve()
    t2 = {**texts, w: b'`include "deep.svh"\n`define W 8\n'}
    load(fl, top="t", overrides=t2, check_overrides=False, cache=cache)
    top = (tmp_path / "rtl" / "t.sv").resolve()
    t3 = {**t2, top: t2[top].replace(b"endmodule", b"  logic x;\nendmodule")}
    parsed = count_parses(monkeypatch)
    got = load(fl, top="t", overrides=t3, check_overrides=False, cache=cache)
    assert parsed == ["t.sv"]
    assert differences(describe(got), describe(load(fl, top="t", overrides=t3, check_overrides=False))) == []


def test_included_follows_the_current_text_not_an_earlier_one(tmp_path: Path):
    fl, texts, cache = hdr_setup(tmp_path)
    solo = (tmp_path / "rtl" / "solo.sv").resolve()
    other = (tmp_path / "inc" / "other.svh").resolve()
    assert other in load(fl, top="t", overrides=texts, cache=cache).included
    # solo.sv no longer includes other.svh
    t2 = {**texts, solo: b"module solo(input logic [2:0] a);\nendmodule\n"}
    got = load(fl, top="t", overrides=t2, check_overrides=False, cache=cache)
    assert other not in got.included and other not in got.included_bytes
    assert differences(describe(got), describe(load(fl, top="t", overrides=t2, check_overrides=False))) == []
    # and includes it again
    again = load(fl, top="t", overrides=texts, cache=cache)
    assert other in again.included
    assert differences(describe(again), describe(load(fl, top="t", overrides=texts))) == []


def test_a_source_that_gains_an_include_of_a_known_header(tmp_path: Path):
    fl, texts, cache = hdr_setup(tmp_path)
    top = (tmp_path / "rtl" / "t.sv").resolve()
    t2 = {**texts, top: b'`include "other.svh"\n' + texts[top]}
    got = load(fl, top="t", overrides=t2, check_overrides=False, cache=cache)
    assert differences(describe(got), describe(load(fl, top="t", overrides=t2, check_overrides=False))) == []


# --- binding and failures ----------------------------------------------------------------


def test_a_cache_is_bound_to_one_top(kit):
    fl = read_filelist(kit.filelist)
    cache = LoadCache()
    load(fl, top="top", cache=cache)
    with pytest.raises(AdapterError, match="top"):
        load(fl, top="mid", cache=cache)


def test_a_failed_load_leaves_the_cache_usable(kit):
    fl = read_filelist(kit.filelist)
    texts = texts_of(fl, load(fl, top="top"))
    cache = LoadCache()
    load(fl, top="top", overrides=texts, cache=cache)
    with pytest.raises(AdapterError, match="mid.sv"):
        load(fl, top="top", overrides={**texts, kit.mid: b"// caf\xe9\n" + texts[kit.mid]}, cache=cache)
    with pytest.raises(AdapterError, match="top"):
        load(
            fl,
            top="top",
            overrides={**texts, kit.top: texts[kit.top].replace(b"module top", b"module top2")},
            check_overrides=False,
            cache=cache,
        )
    good = {**texts, kit.mid: texts[kit.mid].replace(b".DEPTH(3)", b".DEPTH(7)")}
    got = load(fl, top="top", overrides=good, check_overrides=False, cache=cache)
    assert differences(describe(got), describe(load(fl, top="top", overrides=good, check_overrides=False))) == []
    back = load(fl, top="top", overrides=texts, cache=cache)
    assert differences(describe(back), describe(load(fl, top="top", overrides=texts))) == []


def test_syntax_error_in_a_reparsed_file_equals_a_fresh_load(kit):
    fl = read_filelist(kit.filelist)
    texts = texts_of(fl, load(fl, top="top"))
    cache = LoadCache()
    load(fl, top="top", overrides=texts, cache=cache)
    bad = {**texts, kit.mid: texts[kit.mid].replace(b"endmodule", b"endmodul")}
    got = load(fl, top="top", overrides=bad, check_overrides=False, cache=cache)
    want = load(fl, top="top", overrides=bad, check_overrides=False)
    assert differences(describe(got), describe(want)) == []
    assert any(d.severity == "error" for d in got.diagnostics())


def test_non_ascii_paths_and_bom_with_reuse(tmp_path: Path):
    root = tmp_path / "café 中 dir"
    root.mkdir()
    (root / "leaf.sv").write_bytes(b"\xef\xbb\xbfmodule leaf(input logic a);\nendmodule\n")
    (root / "t.sv").write_bytes(b"module t(input logic a);\n  leaf u (.a(a));\nendmodule\n")
    (root / "t.f").write_bytes(b"leaf.sv\nt.sv\n")
    fl = read_filelist(root / "t.f")
    texts = {p: p.read_bytes() for p in fl.files}
    cache = LoadCache()
    load(fl, top="t", overrides=texts, cache=cache)
    leaf = (root / "leaf.sv").resolve()
    t2 = {**texts, leaf: texts[leaf].replace(b"endmodule", b"  logic x;\nendmodule")}
    got = load(fl, top="t", overrides=t2, cache=cache)
    assert differences(describe(got), describe(load(fl, top="t", overrides=t2))) == []
    assert got.roundtrip(leaf) == t2[leaf] and t2[leaf].startswith(b"\xef\xbb\xbf")


# --- through a session -------------------------------------------------------------------


def shadow_reloads(monkeypatch) -> list[int]:
    """Make every Session re-load also do an independent fresh load and compare."""
    checked: list[int] = []
    original = Session._reload

    def reload(self):
        original(self)
        fresh = load(self.filelist, top=self.top, overrides=dict(self._text), check_overrides=False)
        diff = differences(describe(self.loaded), describe(fresh))
        assert diff == [], f"re-load {self.reloads} differs from a fresh load in: {diff}"
        checked.append(self.reloads)

    monkeypatch.setattr(Session, "_reload", reload)
    return checked


def test_session_reloads_equal_fresh_loads_and_parse_only_what_changed(kit, monkeypatch):
    checked = shadow_reloads(monkeypatch)
    vet = Vet(Session(kit.filelist, top="top"))
    parsed = count_parses(monkeypatch)
    vet.punch_port("dft_en", "input", to="top.u_mid.u_l0", all_instances=True)
    vet.punch_port("dbg_o", "output", to="top.u_mid", signal="y0")  # re-loads: three files changed
    session_parses = list(parsed)
    vet.module("mid").instance("u_l1").set_param("DEPTH", 5, was=3)
    vet.propagate_param("DEPTH", to="top.u_mid.u_l1")
    r = vet.finish()
    assert r.ok, r.report()
    assert checked == list(range(1, vet.session.reloads + 1)) and len(checked) >= 2
    # the shadow loads parse everything; the session's own first re-load parsed 3 of 4 files
    assert session_parses.count("alt_leaf.sv") == 1  # once, by the shadow load only


def test_baseline_is_unaffected_by_later_loads(kit):
    want = describe(load(kit.filelist, top="top"))
    s = Session(kit.filelist, top="top")
    for old, new in ((b".DEPTH(3)", b".DEPTH(5)"), (b".DEPTH(5)", b".DEPTH(6)"), (b"endmodule", b"endmodul")):
        start, end = span(s.text(kit.mid), old)
        s.splice(kit.mid, start, end, new)
        with contextlib.suppress(AdapterError):
            s.refresh()
        s.loaded.diagnostics()
    assert s.baseline_snapshot() == want["snapshot"]
    assert differences(describe(s._baseline), want) == []


# --- real designs ----------------------------------------------------------------------

corpus = pytest.importorskip("corpus")
CACHE = REPO / ".corpus"
needs_corpus = pytest.mark.skipif(
    not corpus.bag_available() and os.environ.get("HDL_VET_REQUIRE_CORPUS") != "1",
    reason="hdl-bag CLI (`bag`) not installed",
)


@pytest.mark.corpus
@needs_corpus
@pytest.mark.parametrize("design_id", ["demo_ss-flat", "demo_ss-sv-interface"])
def test_demo_ss_script_with_shadowed_reloads(design_id, monkeypatch):
    (d,) = [d for d in corpus.designs() if d.id == design_id]
    built = d.build(CACHE)
    checked = shadow_reloads(monkeypatch)
    vet = Vet(Session(built.filelist, top=built.top))
    top = vet.module(built.top)
    top.add_port("dft_en_i", "input")
    top.add_wire("dft_en_q")
    top.add_assign("dft_en_q", "dft_en_i")
    if design_id == "demo_ss-flat":  # the reset synchronisers sit directly in this top
        for inst in top.instances(module="bag_rst_sync", count=4):
            inst.set_param("Stages", 3)
    top.add_port("dbg_o", "output")
    top.add_assign("dbg_o", "dft_en_q")
    r = vet.finish()
    assert r.ok, r.report()
    assert len(checked) >= 3


@pytest.mark.corpus
@needs_corpus
def test_reuse_parses_two_files_out_of_eighty_three(monkeypatch):
    (d,) = [d for d in corpus.designs() if d.id == "demo_ss-flat"]
    built = d.build(CACHE)
    vet = Vet(Session(built.filelist, top=built.top))
    vet.punch_port("p0_i", "input", to="demo_ss_bus_top.u_main_wrapper")
    parsed = count_parses(monkeypatch)
    vet.punch_port("p1_i", "input", to="demo_ss_bus_top.u_main_wrapper")  # forces one re-load
    assert sorted(parsed) == ["demo_ss_bus_top.sv", "main_wrapper.sv"]
    assert vet.finish().ok
