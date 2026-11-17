"""Parse-tree reuse and include handling: cases the reviewer broke with a fuzzer.

Spec changes and additions (they extend the docstring of test_tree_reuse.py;
where they differ, this file wins). The governing rule is unchanged: a load with
a LoadCache is indistinguishable from a load without one, and whenever reuse is
not provably safe the cache starts over.

Source buffers
    - Every source is parsed under a synthetic name that the mirror of a real
      path can never be (also on its first parse, also in a load without a
      cache). A source's mirrored path therefore only ever exists as a header,
      registered from the current bytes and tracked like any header, so a
      source that is also included elsewhere is always seen with its current
      text, and a real file whose name looks like a versioned name is itself.

What a tree depends on
    - Per source, the cache records the headers that source's text was found to
      include (the closure the adapter resolved). After parsing, the includes
      slang actually performed for that source are compared with it. An include
      the adapter did not predict (a name built by a macro, `__FILE__, ...) may
      have bound to a header some earlier load registered; then the state is
      unsafe: a reused state is discarded and the load is redone fresh, and the
      cache keeps starting over while that stays true.
    - slang remembers failed include lookups in its source manager. When a load
      on a reused state raises anything that is not an AdapterError, the state
      is discarded and the load is retried once on a fresh one, so the outcome
      is that of a fresh load.

Included files
    - `Design.included` takes each tree's own buffer from its end-of-file token
      (always in the tree's own buffer), not from its first token.
    - The headers of a source are computed once, when it is parsed, from the
      buffers that parse created. A re-load does not walk every buffer the
      session ever made, and the cache starts over when its source manager has
      grown far beyond its initial size.

Include resolution (older bugs, the same with and without a cache)
    - The includer-relative key for a header is registered only when the file
      really resolves there. A header found through an include directory is
      registered under its own mirrored path only, so its own relative includes
      resolve from its real directory.
    - A header named by an absolute path is handled like any other: its nested
      includes come from the bytes the caller supplied, never from disk, and
      they appear in `included`.
"""

from __future__ import annotations

import time
from pathlib import Path

from hdl_vet.filelist import read_filelist
from hdl_vet.slang_adapter import LoadCache, load
from tree_reuse_tools import describe, differences, write


def fresh(fl, top, texts):
    return describe(load(fl, top=top, overrides=texts, check_overrides=False))


# --- a reused state must never serve an older text ---------------------------------------


def test_a_source_included_after_it_was_reparsed_is_seen_with_its_current_text(tmp_path: Path):
    fl = read_filelist(
        write(
            tmp_path,
            {"defs.sv": "localparam int W = 1;\n", "t.sv": "module t; localparam int P = 0; endmodule\n"},
            "defs.sv\nt.sv\n",
        )
    )
    defs, t = (tmp_path / "defs.sv").resolve(), (tmp_path / "t.sv").resolve()
    texts = {p: p.read_bytes() for p in fl.files}
    cache = LoadCache()
    load(fl, top="t", overrides=texts, cache=cache)
    texts[defs] = b"localparam int W = 2;\n"
    load(fl, top="t", overrides=texts, cache=cache)
    texts[t] = b'module t;\n`include "defs.sv"\nlocalparam int P = W; endmodule\n'
    got = load(fl, top="t", overrides=texts, check_overrides=False, cache=cache)
    assert got.snapshot()["t"].params["P"] == "2"
    assert differences(describe(got), fresh(fl, "t", texts)) == []


def test_an_earlier_source_may_include_a_later_one(tmp_path: Path):
    fl = read_filelist(
        write(
            tmp_path,
            {
                "t.sv": 'module t;\n`include "defs.sv"\nlocalparam int P = W; endmodule\n',
                "defs.sv": "localparam int W = 5;\n",
            },
            "t.sv\ndefs.sv\n",
        )
    )
    for cache in (None, LoadCache()):
        d = load(fl, top="t", cache=cache)
        assert d.snapshot()["t"].params["P"] == "5"


def test_an_include_the_adapter_does_not_predict_does_not_bind_to_an_earlier_loads_header(tmp_path: Path):
    fl = read_filelist(
        write(
            tmp_path,
            {
                "h.svh": "localparam int W = 3;\n",
                "a.sv": '`include "h.svh"\nmodule a; endmodule\n',
                "t.sv": '`define H "h.svh"\nmodule t;\n`include `H\nlocalparam int P = W; endmodule\n',
            },
            "a.sv\nt.sv\n",
        )
    )
    a = (tmp_path / "a.sv").resolve()
    texts = {p: p.read_bytes() for p in fl.files}
    cache = LoadCache()
    load(fl, top="t", overrides=texts, cache=cache)
    texts[a] = b"module a; endmodule\n"  # nothing registers h.svh any more
    got = load(fl, top="t", overrides=texts, check_overrides=False, cache=cache)
    assert differences(describe(got), fresh(fl, "t", texts)) == []
    # and the cache stays correct on the loads that follow
    texts[a] = b"module a; logic x; endmodule\n"
    again = load(fl, top="t", overrides=texts, check_overrides=False, cache=cache)
    assert differences(describe(again), fresh(fl, "t", texts)) == []


def test_an_include_lookup_slang_remembered_does_not_break_a_later_load(tmp_path: Path):
    fl = read_filelist(
        write(
            tmp_path,
            {
                "inc/w.svh": "`define W 4\n",
                "inc/sub/rel.svh": '`include "../w.svh"\n',
                "rtl/a.sv": '`include "sub/rel.svh"\nmodule a; endmodule\n',
                "rtl/b.sv": "module b; endmodule\n",
            },
            "+incdir+inc\nrtl/a.sv\nrtl/b.sv\n",
        )
    )
    a, b = (tmp_path / "rtl/a.sv").resolve(), (tmp_path / "rtl/b.sv").resolve()
    cache = LoadCache()
    load(fl, cache=cache)
    texts = {a: b"module a; endmodule\n", b: b'`include "w.svh"\nmodule b; endmodule\n'}
    got = load(fl, overrides=texts, check_overrides=False, cache=cache)
    assert differences(describe(got), fresh(fl, None, texts)) == []


def test_a_header_named_like_a_versioned_source_is_the_header(tmp_path: Path):
    fl = read_filelist(
        write(
            tmp_path,
            {
                "x.sv": "module x; endmodule\n",
                "x.sv.__r1": "localparam int HDR = 7;\n",
                "t.sv": "module t; x u(); endmodule\n",
            },
            "x.sv\nt.sv\n",
        )
    )
    x, t = (tmp_path / "x.sv").resolve(), (tmp_path / "t.sv").resolve()
    texts = {p: p.read_bytes() for p in fl.files}
    cache = LoadCache()
    load(fl, top="t", overrides=texts, cache=cache)
    texts[x] = b"module x; logic a; endmodule\n"
    load(fl, top="t", overrides=texts, cache=cache)
    texts[t] = b'`include "x.sv.__r1"\nmodule t; x u(); localparam int Q = HDR; endmodule\n'
    got = load(fl, top="t", overrides=texts, check_overrides=False, cache=cache)
    assert got.snapshot()["t"].params["Q"] == "7"
    assert differences(describe(got), fresh(fl, "t", texts)) == []


# --- included files ------------------------------------------------------------------------


def test_included_does_not_depend_on_where_the_first_token_comes_from(tmp_path: Path):
    fl = read_filelist(
        write(
            tmp_path,
            {
                "rtl/v.svh": '`include "w.svh"\n`define V 1\n',
                "rtl/w.svh": '`include "v.svh"\nlocalparam int VW = 1;\n',
                "rtl/a.sv": '`include "w.svh"\nmodule a; endmodule\n',
            },
            "rtl/a.sv\n",
        )
    )
    # the first token comes out of the include cycle at slang's depth limit
    assert [p.name for p in load(fl).included] == ["v.svh", "w.svh"]
    assert [p.name for p in load(fl, cache=LoadCache()).included] == ["v.svh", "w.svh"]


def test_a_reload_does_not_walk_every_buffer_the_session_ever_made(tmp_path: Path):
    # 300 macro uses: every re-parse leaves hundreds of expansion buffers behind.
    src = '`include "m.svh"\nmodule t;\n' + "".join(f"  `T(x{i})\n" for i in range(300)) + "endmodule\n"
    fl = read_filelist(write(tmp_path, {"m.svh": "`define T(n) logic [7:0] n;\n", "t.sv": src}, "t.sv\n"))
    t = (tmp_path / "t.sv").resolve()
    texts = {t: src.encode(), (tmp_path / "m.svh").resolve(): b"`define T(n) logic [7:0] n;\n"}
    cache = LoadCache()

    def reload(i: int) -> float:
        texts[t] = texts[t] + b"// %d\n" % i
        t0 = time.perf_counter()
        load(fl, top="t", overrides=texts, check_overrides=False, cache=cache)
        return time.perf_counter() - t0

    early = min(reload(i) for i in range(5))
    for i in range(5, 400):
        reload(i)
    late = min(reload(i) for i in range(400, 405))
    # before the fix: about 1 ms early, 60 ms late. Generous bound against timer noise.
    assert late < 5 * early + 0.02, (early, late)
    got = load(fl, top="t", overrides=texts, check_overrides=False, cache=cache)
    assert differences(describe(got), fresh(fl, "t", texts)) == []


# --- include resolution, with and without a cache --------------------------------------------


def test_incdir_header_and_a_source_include_the_same_sibling(tmp_path: Path):
    fl = read_filelist(
        write(
            tmp_path,
            {
                "inc/w.svh": '`include "deep.svh"\n`define W 4\n',
                "inc/deep.svh": "`define D 2\n",
                "rtl/a.sv": '`include "w.svh"\nmodule a(input logic [`W-1:0] x);\nendmodule\n',
                "rtl/b.sv": '`include "deep.svh"\nmodule b(input logic [`D-1:0] x);\nendmodule\n',
            },
            "+incdir+inc\nrtl/a.sv\nrtl/b.sv\n",
        )
    )
    for cache in (None, LoadCache()):
        d = load(fl, cache=cache)
        assert [x for x in d.diagnostics() if x.severity == "error"] == []
        assert [p.name for p in d.included] == ["deep.svh", "w.svh"]
        assert d.snapshot()["a"].ports["x"] == ("input", 4) and d.snapshot()["b"].ports["x"] == ("input", 2)


def test_relative_include_inside_a_header_found_through_an_include_directory(tmp_path: Path):
    fl = read_filelist(
        write(
            tmp_path,
            {
                "inc/deep.svh": "`define D 2\n",
                "inc/sub/rel.svh": '`include "../deep.svh"\n',
                "rtl/deep.svh": "`define D 9\n",  # a decoy next to the includer's parent: must not be used
                "rtl/t.sv": '`include "sub/rel.svh"\nmodule t(input logic [`D-1:0] x);\nendmodule\n',
            },
            "+incdir+inc\nrtl/t.sv\n",
        )
    )
    for cache in (None, LoadCache()):
        d = load(fl, top="t", cache=cache)
        assert [x for x in d.diagnostics() if x.severity == "error"] == []
        assert d.snapshot()["t"].ports["x"] == ("input", 2)
        assert sorted(p.relative_to(tmp_path.resolve()).as_posix() for p in d.included) == [
            "inc/deep.svh",
            "inc/sub/rel.svh",
        ]


def test_nested_include_of_an_absolute_include_comes_from_the_supplied_bytes(tmp_path: Path):
    fl = read_filelist(
        write(
            tmp_path,
            {
                "inc/w.svh": '`include "deep.svh"\n',
                "inc/deep.svh": "`define D 2\n",
                "rtl/a.sv": "module a; endmodule\n",
            },
            "rtl/a.sv\n",
        )
    )
    a = (tmp_path / "rtl/a.sv").resolve()
    w = (tmp_path / "inc/w.svh").resolve()
    deep = (tmp_path / "inc/deep.svh").resolve()
    src = b'`include "' + w.as_posix().encode() + b'"\nmodule a; localparam int P = `D; endmodule\n'
    texts = {a: src, deep: b"`define D 7\n"}  # the caller's deep.svh, not the one on disk
    for cache in (None, LoadCache()):
        d = load(fl, top="a", overrides=texts, check_overrides=False, cache=cache)
        assert d.snapshot()["a"].params["P"] == "7"
        assert [p.name for p in d.included] == ["deep.svh", "w.svh"]
        assert d.included_bytes[deep] == b"`define D 7\n"
