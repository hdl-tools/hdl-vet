"""Re-load cost: what a session may and may not redo on every re-load.

A session reads its files once and re-loads from its own bytes. Two things used
to be repeated on every re-load although their answers cannot change:

    - resolving include names on the file system, and reading a header that is
      named only in an inactive `ifdef branch (slang_adapter.LoadCache);
    - building the source model of every module to look for copies of the
      punched modules in generate blocks (cached in Vet per module while its
      file's text is unchanged, and dropped whenever any header changes).

These tests pin the behaviour, not timings: a re-load touches the file system
only for an include name it has not seen, and a repeated punch looks again only
at modules whose file changed. They also check the caches cannot go stale: a
cache belongs to one file list, a header edit invalidates the scan, and a
re-load that fails leaves the session exactly as it was.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet import slang_adapter
from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session
from vetkit import make_design


def write(root: Path, files: dict[str, str], filelist: str) -> Path:
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode())
    (root / "t.f").write_bytes(filelist.encode())
    return root / "t.f"


INCLUDES = {
    "inc/w.svh": '`include "deep.svh"\n`define W 4\n',
    "inc/deep.svh": "`define D 2\n",
    "rtl/leaf.sv": '`include "w.svh"\nmodule leaf(input logic [`W-1:0] a);\nendmodule\n',
    "rtl/mid.sv": '`include "w.svh"\nmodule mid(input logic [`W-1:0] a);\n  leaf u_leaf (.a(a));\nendmodule\n',
    "rtl/t.sv": '`include "../inc/w.svh"\nmodule t(input logic [`W-1:0] a);\n  mid u_mid (.a(a));\nendmodule\n',
}
INCLUDES_F = "+incdir+inc\nrtl/leaf.sv\nrtl/mid.sv\nrtl/t.sv\n"


@pytest.fixture
def no_file_system(monkeypatch):
    """`with no_file_system():` makes any path resolution or file access fail the test.

    It is a context manager, not a switch: pytest itself needs the file system
    again to report a failure.
    """
    from contextlib import contextmanager

    @contextmanager
    def armed():
        def boom(self, *a, **k):
            raise AssertionError(f"file system touched during a re-load: {self}")

        def no(*a, **k):
            raise AssertionError(f"file system touched during a re-load: {a[:1]}")

        with monkeypatch.context() as m:
            for name in ("resolve", "is_file", "read_bytes", "read_text", "exists", "stat", "open"):
                m.setattr(Path, name, boom)
            m.setattr("builtins.open", no)
            m.setattr("io.open", no)
            m.setattr("os.stat", no)
            m.setattr("os.lstat", no)
            m.setattr("os.path.realpath", no)
            yield

    return armed


def test_reload_does_not_touch_the_file_system(tmp_path: Path, no_file_system):
    fl = write(tmp_path, INCLUDES, INCLUDES_F)
    s = Session(fl, top="t")
    mid = (tmp_path / "rtl" / "mid.sv").resolve()
    at = s.text(mid).index(b"  leaf u_leaf")
    s.splice(mid, at, at, b"  // note\n")
    with no_file_system():
        s.refresh()
        errors = [d for d in s.loaded.diagnostics() if d.severity == "error"]
        snap = s.loaded.snapshot()
    assert s.reloads == 1 and errors == []
    assert snap["t.u_mid.u_leaf"].ports["a"] == ("input", 4)


def test_reload_still_sees_an_edited_header(tmp_path: Path, no_file_system):
    fl = write(tmp_path, INCLUDES, INCLUDES_F)
    s = Session(fl, top="t")
    hdr = (tmp_path / "inc" / "w.svh").resolve()
    start = s.text(hdr).index(b"`define W 4")
    s.splice(hdr, start, start + 11, b"`define W 8")
    with no_file_system():
        s.refresh()
        snap = s.loaded.snapshot()
    assert snap["t.u_mid.u_leaf"].ports["a"] == ("input", 8)


def test_a_newly_added_include_is_still_looked_up_and_rejected(tmp_path: Path):
    fl = write(tmp_path, INCLUDES, INCLUDES_F)
    (tmp_path / "inc" / "extra.svh").write_bytes(b"`define E 1\n")
    s = Session(fl, top="t")
    t = (tmp_path / "rtl" / "t.sv").resolve()
    s.splice(t, 0, 0, b'`include "extra.svh"\n')
    r = s.finish()
    assert not r.ok
    assert [p.name for p in r.new_files] == ["extra.svh"]


def test_load_without_a_cache_is_unchanged_and_a_shared_cache_gives_the_same_design(tmp_path: Path, no_file_system):
    from hdl_vet.filelist import read_filelist

    path = write(tmp_path, INCLUDES, INCLUDES_F)
    fl = read_filelist(path)
    plain = slang_adapter.load(fl, top="t")
    # as a Session does: every load is given the bytes of all sources and headers
    texts = {**{p: p.read_bytes() for p in fl.files}, **plain.included_bytes}
    cache = slang_adapter.LoadCache()
    first = slang_adapter.load(fl, top="t", overrides=texts, cache=cache)
    want = (plain.snapshot(), plain.included)
    assert (first.snapshot(), first.included) == want and len(plain.included) == 2
    with no_file_system():  # the second load must answer everything from the cache
        second = slang_adapter.load(fl, top="t", overrides=texts, cache=cache)
        got = (second.snapshot(), second.included)
    assert got == want


def test_a_cache_cannot_be_shared_between_file_lists(tmp_path: Path):
    write(
        tmp_path,
        {
            "i1/w.svh": "`define W 1\n",
            "i2/w.svh": "`define W 7\n",
            "s/t.sv": '`include "w.svh"\nmodule t(output logic [3:0] y);\n  assign y = `W;\nendmodule\n',
        },
        "+incdir+i1\ns/t.sv\n",
    )
    (tmp_path / "t2.f").write_bytes(b"+incdir+i2\ns/t.sv\n")
    cache = slang_adapter.LoadCache()
    slang_adapter.load(tmp_path / "t.f", top="t", cache=cache)
    with pytest.raises(slang_adapter.AdapterError, match="file list"):
        slang_adapter.load(tmp_path / "t2.f", top="t", cache=cache)


def test_header_in_an_inactive_branch_is_read_once(tmp_path: Path, no_file_system):
    files = dict(INCLUDES)
    files["inc/unused.svh"] = "`define U 1\n"
    files["rtl/t.sv"] = '`ifdef NEVER\n`include "unused.svh"\n`endif\n' + INCLUDES["rtl/t.sv"]
    s = Session(write(tmp_path, files, INCLUDES_F), top="t")
    assert "unused.svh" not in [p.name for p in s.files]  # slang never included it
    mid = (tmp_path / "rtl" / "mid.sv").resolve()
    s.splice(mid, 0, 0, b"// note\n")
    with no_file_system():
        s.refresh()
    assert s.reloads == 1


def test_a_failed_reload_leaves_the_session_as_it_was(tmp_path: Path):
    kit = make_design(tmp_path)
    s = Session(kit.filelist, top="top")
    before = s.text(kit.mid)
    s.splice(kit.mid, 0, 0, b"// caf\xe9\n")  # not valid UTF-8: the edited design cannot be loaded
    with pytest.raises(slang_adapter.AdapterError):
        s.refresh()
    assert s.text(kit.mid) == before and s.reloads == 0
    assert s.loaded.roundtrip(kit.mid) == before  # text and loaded design still agree
    with pytest.raises(slang_adapter.AdapterError):
        s.refresh()  # the bad splice is still queued, not silently dropped
    r = s.finish()
    assert not r.ok and r.load_error


# --- the generate-copy scan ---------------------------------------------------------------


def many_modules(root: Path, extra: int = 30) -> Path:
    others = "".join(f"module other{i}(input logic a);\nendmodule\n" for i in range(extra))
    uses = "".join(f"  other{i} u_o{i} (.a(a));\n" for i in range(extra))
    return write(
        root,
        {
            "others.sv": others,
            "leaf.sv": "module leaf(input logic a);\nendmodule\n",
            "mid.sv": "module mid(input logic a);\n  leaf u_leaf (.a(a));\nendmodule\n",
            "t.sv": "module t(input logic a);\n  mid u_mid (.a(a));\n" + uses + "endmodule\n",
        },
        "others.sv\nleaf.sv\nmid.sv\nt.sv\n",
    )


def count_model_builds(monkeypatch) -> list[str]:
    """Names of the modules whose source model is built from now on."""
    built: list[str] = []
    original = slang_adapter.Design._module_info

    def build(self, name):
        built.append(name)
        return original(self, name)

    monkeypatch.setattr(slang_adapter.Design, "_module_info", build)
    return built


def test_repeated_punch_does_not_rebuild_untouched_modules(tmp_path: Path, monkeypatch):
    vet = Vet(Session(many_modules(tmp_path), top="t"))
    vet.punch_port("p0", "input", to="t.u_mid.u_leaf")
    built = count_model_builds(monkeypatch)
    vet.punch_port("p1", "input", to="t.u_mid.u_leaf")  # re-loads: the lists were edited
    assert vet.session.reloads == 1
    assert not [name for name in built if name.startswith("other")], built
    r = vet.finish()
    assert r.ok, r.report()


def test_scan_cache_sees_a_generate_copy_added_later(tmp_path: Path):
    fl = many_modules(tmp_path, extra=2)
    vet = Vet(Session(fl, top="t"))
    vet.punch_port("p0", "input", to="t.u_mid.u_leaf")  # fills the cache
    t = (tmp_path / "t.sv").resolve()
    _ = vet.design
    text = vet.text(t)
    at = text.index(b"endmodule")
    vet.splice(
        t, at, at, b"  if (1) begin : g\n    leaf u_gen (.a(a), .p0(1'b0));\n  end\n", waiver="set-up of the test"
    )
    with pytest.raises(VetError, match=r"u_gen.*generate"):
        vet.punch_port("p1", "input", to="t.u_mid.u_leaf")


def test_scan_cache_follows_a_copy_that_is_removed(tmp_path: Path):
    fl = write(
        tmp_path,
        {
            "leaf.sv": "module leaf(input logic a);\nendmodule\n",
            "mid.sv": "module mid(input logic a);\n  leaf u_leaf (.a(a));\nendmodule\n",
            "t.sv": "module t(input logic a);\n  mid u_mid (.a(a));\n"
            "  if (0) begin : g\n    leaf u_gen (.a(a));\n  end\nendmodule\n",
        },
        "leaf.sv\nmid.sv\nt.sv\n",
    )
    vet = Vet(Session(fl, top="t"))
    with pytest.raises(VetError, match=r"u_gen.*generate"):
        vet.punch_port("p0", "input", to="t.u_mid.u_leaf")  # caches the copy found in t.sv
    t = (tmp_path / "t.sv").resolve()
    text = vet.text(t)
    start = text.index(b"  if (0) begin : g")
    end = text.index(b"endmodule")
    vet.splice(t, start, end, b"", waiver="set-up of the test")  # the branch was not elaborated: no structural change
    vet.punch_port("p0", "input", to="t.u_mid.u_leaf")
    r = vet.finish()
    assert r.ok, r.report()


@pytest.mark.parametrize(
    ("header", "old", "new", "body"),
    [
        # a macro defined in the header expands to a generate block in `side`
        (
            "`define GEN_COPY\n",
            b"`define GEN_COPY\n",
            b"`define GEN_COPY if (0) begin : g leaf u_gen (.a(a)); end\n",
            "  `GEN_COPY\n",
        ),
        # a define in the header switches on an `ifdef around a generate block
        (
            "`define NO_GEN\n",
            b"`define NO_GEN\n",
            b"`define WITH_GEN\n",
            "`ifdef WITH_GEN\n  if (0) begin : g\n    leaf u_gen (.a(a));\n  end\n`endif\n",
        ),
    ],
)
def test_scan_cache_sees_a_copy_that_an_edited_header_adds(tmp_path: Path, header, old, new, body):
    # side.sv itself never changes; only the header does. Sign-off cannot catch
    # this (the branch is not elaborated), so the refusal is the only guard.
    fl = write(
        tmp_path,
        {
            "inc/gen.svh": header,
            "leaf.sv": "module leaf(input logic a);\nendmodule\n",
            "mid.sv": "module mid(input logic a);\n  leaf u_leaf (.a(a));\nendmodule\n",
            "side.sv": '`include "gen.svh"\nmodule side(input logic a);\n' + body + "endmodule\n",
            "t.sv": "module t(input logic a);\n  mid u_mid (.a(a));\n  side u_side (.a(a));\nendmodule\n",
        },
        "+incdir+inc\nleaf.sv\nmid.sv\nside.sv\nt.sv\n",
    )
    vet = Vet(Session(fl, top="t"))
    vet.punch_port("p0", "input", to="t.u_mid.u_leaf")  # caches side: no copies
    hdr = (tmp_path / "inc" / "gen.svh").resolve()
    vet.splice(hdr, 0, len(old), new, waiver="set-up of the test")
    with pytest.raises(VetError, match=r"u_gen.*generate"):
        vet.punch_port("p1", "input", to="t.u_mid.u_leaf")


def test_vetkit_still_refuses_and_accepts_as_before(tmp_path: Path):
    kit = make_design(tmp_path)
    vet = Vet(Session(kit.filelist, top="top"))
    for i in range(3):
        vet.punch_port(f"p{i}", "input", to="top.u_mid")
    r = vet.finish()
    assert r.ok, r.report()
    assert vet.session.reloads == 3
