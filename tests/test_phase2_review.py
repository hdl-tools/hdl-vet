"""Phase 2 review fixes: cases the reviewer broke with probes.

Spec changes and additions (they extend the docstrings of the other phase 2
test files; where they differ, this file wins):

Session
    - Every (re-)load is built from the session's own state: the file list is
      parsed once, `base` is resolved once, and the bytes of every source and
      included file are read once, at construction. Nothing is read from disk,
      the environment or the working directory afterwards. What is verified is
      what is written, even if the input tree changes during the run.
    - finish(): if the final design reads any file that was not part of the
      baseline (an edit added an `include), sign-off fails. Result gains
      `new_files: list[Path]` (sorted) and report() names them.
    - finish(): if the edited design cannot be loaded at all (top renamed away,
      invalid UTF-8, ...), the result is a failed Result, not an exception.
      Result gains `load_error: str | None`; report() shows it.
    - module(name) also re-loads when any included (non-source) file has queued
      splices: a header can change any module.
    - layout_root = deepest directory containing every source file, included
      file, include directory, and `base` when given, else the directory of the
      top-level file list. A file list outside layout_root is written at the top
      of the output tree under its own file name.
    - HEADER_MARK counts as a header only when it starts a line and is followed
      by end of line, end of file or ";".

Adapter
    - Includes resolve as slang does on a real tree: relative to the directory
      of the including file first (the real one, also for nested includes and
      for names containing ".."), then the include directories in order.
    - Source-model ranges never include a leading attribute instance
      `(* ... *)`.
    - module(name) raises AdapterError, naming the included file, when a port,
      parameter or instance of the declaration comes from an `include: its
      offsets would not be offsets in the module's file.
    - An empty positional connection (`u (a, )`) is ConnInfo(None, "", p, p)
      with p the offset where an expression would go.
    - An explicit non-ANSI port `.c(cc)` takes its direction from `cc`.
    - Snapshot connection text keeps explicit casts; only implicit conversions
      are unwrapped.
    - InstanceSnap gains `port_types: dict[str, str]` (default empty): slang's
      spelling of each port's full type, including unpacked dimensions.
      diff_snapshots emits Change("port-type-changed", path, port, before, after)
      when a port's direction and width are unchanged but its type differs.

Output and command line
    - write_tree raises OutputError for any file-system failure, including an
      output path whose parent is a file.
    - A script that raises SystemExit (sys.exit) is an input error: exit 2.
    - Output is written so that text the console encoding cannot represent is
      escaped, never a crash.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hdl_vet import slang_adapter
from hdl_vet.cli import main
from hdl_vet.output import OutputError, write_tree
from hdl_vet.session import Session, SessionError
from hdl_vet.signoff import Change, diff_snapshots
from hdl_vet.slang_adapter import InstanceSnap, load
from vetkit import make_design, span, tree_bytes


@pytest.fixture
def kit(tmp_path: Path):
    return make_design(tmp_path / "in")


def replace(session: Session, path: Path, old: bytes, new: bytes) -> None:
    start, end = span(session.text(path), old)
    session.splice(path, start, end, new, old=old)


def design(tmp_path: Path, files: dict[str, str], filelist: str, top: str):
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode())
    (tmp_path / "t.f").write_bytes(filelist.encode())
    return load(tmp_path / "t.f", top=top)


# --- sign-off soundness -------------------------------------------------------------


def test_edit_that_includes_a_new_file_fails(kit):
    extra = kit.root / "rtl" / "extra.svh"
    extra.write_bytes(b"logic [3:0] extra_sig;\n")
    s = Session(kit.filelist, top="top")
    a, _ = span(s.text(kit.top), b"  assign spare_d")
    s.splice(kit.top, a, a, b'  `include "extra.svh"\n')
    r = s.finish()
    assert not r.ok
    assert r.new_files == [extra.resolve()]
    assert "extra.svh" in r.report()


def test_vanished_warning_does_not_mask_a_new_one_in_the_same_splice(kit):
    s = Session(kit.filelist, top="top")
    replace(s, kit.top, b".clk(clk), .d(spare_d), .q(spare_q)", b".clk(clk_typo), .d(spare_d), .q()")
    s.expect("connection-changed", "top.u_spare", "clk")
    s.expect("connection-changed", "top.u_spare", "q")
    r = s.finish()
    assert not r.ok
    assert [d.code for d in r.new_diagnostics] == ["ImplicitNet"]
    assert "clk_typo" in r.new_diagnostics[0].message


def test_macro_use_site_warning_changed_through_a_header_is_new(tmp_path: Path):
    kit = make_design(tmp_path / "in")
    (kit.root / "rtl" / "sig.svh").write_bytes(b"`define SIGQ spare_q\n")
    kit.top.write_bytes(
        b'`include "sig.svh"\n' + kit.top.read_bytes().replace(b".q(spare_q)", b".q(`SIGQ)")
    )
    s = Session(kit.filelist, top="top")
    hdr = kit.root / "rtl" / "sig.svh"
    replace(s, hdr, b"spare_q", b"other_typo")
    s.expect("connection-changed", "top.u_spare", "q")
    r = s.finish()
    assert not r.ok
    assert any(d.code == "ImplicitNet" and "other_typo" in d.message for d in r.new_diagnostics)


def test_reload_uses_session_bytes_not_disk(kit):
    s = Session(kit.filelist, top="top")
    replace(s, kit.top, b"assign spare_d = a;", b"assign spare_d = a; // x")
    kit.mid.write_bytes(kit.mid.read_bytes().replace(b".q(y0)", b".q(y0_typo)"))
    kit.filelist.write_bytes(b"rtl/top.sv\n")
    r = s.finish()
    assert r.ok, r.report()
    assert r.changes == [] and list(r.files) == [kit.top]


def test_output_copies_the_bytes_that_were_verified(kit, tmp_path: Path):
    s = Session(kit.filelist, top="top")
    original_mid = kit.mid.read_bytes()
    kit.mid.write_bytes(original_mid.replace(b".q(y0)", b".q(y0_typo)"))
    out = write_tree(s, s.finish(), tmp_path / "out")
    assert (out / "rtl" / "mid.sv").read_bytes() == original_mid


def test_environment_and_cwd_changes_after_start_do_not_matter(tmp_path: Path, monkeypatch):
    kit = make_design(tmp_path / "in")
    monkeypatch.setenv("VET_RTL", "rtl")
    kit.filelist.write_bytes(b"$VET_RTL/leaf.sv\n$VET_RTL/alt_leaf.sv\n$VET_RTL/mid.sv\n$VET_RTL/top.sv\n")
    monkeypatch.chdir(kit.root.parent)
    s = Session(kit.filelist, top="top", base=Path("in"))
    monkeypatch.setenv("VET_RTL", "nowhere")
    monkeypatch.chdir(tmp_path.parent)
    replace(s, kit.top, b"assign spare_d = a;", b"assign spare_d = a; // x")
    assert s.module("top").name == "top"
    assert s.finish().ok


def test_edit_that_breaks_the_load_is_a_failed_result(kit):
    s = Session(kit.filelist, top="top")
    replace(s, kit.top, b"module top", b"module top2")
    r = s.finish()
    assert not r.ok
    assert r.load_error and "top" in r.load_error
    assert r.load_error in r.report()

    s = Session(kit.filelist, top="top")
    s.splice(kit.mid, 0, 0, b"// caf\xe9\n")
    r = s.finish()
    assert not r.ok and r.load_error and "mid.sv" in r.load_error


def test_module_reloads_when_an_included_header_has_queued_splices(tmp_path: Path):
    root = tmp_path / "in"
    root.mkdir()
    (root / "w.svh").write_bytes(b"`define W 4\n")
    (root / "t.sv").write_bytes(b'`include "w.svh"\nmodule t(input logic [`W-1:0] a);\nendmodule\n')
    (root / "t.f").write_bytes(b"+incdir+.\nt.sv\n")
    s = Session(root / "t.f", top="t")
    s.module("t")
    s.splice(root / "w.svh", 0, 0, b"// note\n")
    s.module("t")
    assert s.reloads == 1


def test_header_mark_must_be_a_whole_marker(kit):
    kit.mid.write_bytes(b"// Modified by hdl-vetting team\n" + kit.mid.read_bytes())
    Session(kit.filelist, top="top").finish()
    kit.leaf.write_bytes(b"// Modified by hdl-vet; scripts: a.py sha256:000000000000\n" + kit.leaf.read_bytes())
    with pytest.raises(SessionError, match="leaf.sv"):
        Session(kit.filelist, top="top")


# --- include resolution ---------------------------------------------------------------


def test_nested_include_resolves_next_to_the_including_header(tmp_path: Path):
    d = design(
        tmp_path,
        {
            "inc/a.svh": '`include "b.svh"\n',
            "inc/b.svh": "`define W 4\n",
            "rtl/b.svh": "`define W 8\n",
            "rtl/t.sv": '`include "a.svh"\nmodule t(input logic [`W-1:0] x);\nendmodule\n',
        },
        "+incdir+inc\nrtl/t.sv\n",
        "t",
    )
    assert [p.relative_to(tmp_path.resolve()).as_posix() for p in d.included] == ["inc/a.svh", "inc/b.svh"]
    assert d.snapshot()["t"].ports["x"] == ("input", 4)


def test_include_with_dotdot(tmp_path: Path):
    d = design(
        tmp_path,
        {
            "inc/h.svh": "`define W 4\n",
            "rtl/t.sv": '`include "../inc/h.svh"\nmodule t(input logic [`W-1:0] x);\nendmodule\n',
        },
        "rtl/t.sv\n",
        "t",
    )
    assert [x for x in d.diagnostics() if x.severity == "error"] == []
    assert [p.relative_to(tmp_path.resolve()).as_posix() for p in d.included] == ["inc/h.svh"]


# --- source model -------------------------------------------------------------------


def test_ranges_exclude_leading_attributes(tmp_path: Path):
    src = (
        "module leaf(input logic clk, input logic d);\nendmodule\n"
        "(* no_ungroup *)\nmodule t (\n  (* keep *) input logic clk,\n  input logic a\n);\n"
        "  (* dont_touch *) leaf u_l (.clk(clk), (* async *) .d(a));\n"
        "endmodule\n"
    )
    d = design(tmp_path, {"t.sv": src}, "t.sv\n", "t")
    data = src.encode()
    m = d.module("t")
    assert data[m.start : m.start + 8] == b"module t"
    assert data[m.port("clk").start : m.port("clk").end] == b"input logic clk"
    inst = m.instance("u_l")
    assert data[inst.stmt_start : inst.stmt_end] == b"leaf u_l (.clk(clk), (* async *) .d(a));"
    assert inst.module_start == inst.stmt_start
    assert data[inst.connection("d").start : inst.connection("d").end] == b".d(a)"


def test_declaration_items_from_an_include_are_refused(tmp_path: Path):
    d = design(
        tmp_path,
        {
            "ports.svh": "input logic a,\ninput logic b\n",
            "m.sv": 'module m (\n`include "ports.svh"\n);\nendmodule\n',
            "ok.sv": 'module ok (input logic a);\n`include "body.svh"\nendmodule\n',
            "body.svh": "logic w;\nassign w = a;\n",
        },
        "+incdir+.\nm.sv\nok.sv\n",
        "m",
    )
    with pytest.raises(slang_adapter.AdapterError, match="ports.svh"):
        d.module("m")
    assert [p.name for p in d.module("ok").ports] == ["a"]


def test_empty_positional_connection(tmp_path: Path):
    src = (
        "module c(input logic a, input logic b);\nendmodule\n"
        "module t(input logic a);\n  c u (a, );\nendmodule\n"
    )
    d = design(tmp_path, {"t.sv": src}, "t.sv\n", "t")
    conns = d.module("t").instance("u").connections
    assert [(c.port, c.expr) for c in conns] == [(None, "a"), (None, "")]
    assert conns[1].start == conns[1].end
    assert src.encode()[: conns[1].start].endswith(b"(a,") or src.encode()[: conns[1].start].endswith(b"(a, ")


def test_explicit_non_ansi_port_direction(tmp_path: Path):
    src = "module m (a, .c(cc));\n  input a;\n  input cc;\nendmodule\n"
    d = design(tmp_path, {"t.sv": src}, "t.sv\n", "m")
    assert [(p.name, p.direction) for p in d.module("m").ports] == [("a", "input"), ("c", "input")]


# --- snapshot -----------------------------------------------------------------------


def test_explicit_cast_stays_in_connection_text(tmp_path: Path):
    src = (
        "module leaf(input logic [3:0] a);\nendmodule\n"
        "module t(input logic [7:0] w);\n  leaf u (.a(4'(w)));\nendmodule\n"
    )
    assert design(tmp_path, {"t.sv": src}, "t.sv\n", "t").snapshot()["t.u"].connections == {"a": "4'(w)"}


def test_unpacked_port_dimension_change_is_caught(tmp_path: Path):
    root = tmp_path / "in"
    root.mkdir()
    (root / "t.sv").write_bytes(b"module t(input logic arr [2], input logic [3:0] v);\nendmodule\n")
    (root / "t.f").write_bytes(b"t.sv\n")
    snap = load(root / "t.f", top="t").snapshot()["t"]
    assert snap.ports["v"] == ("input", 4)
    assert snap.port_types["arr"] != snap.port_types["v"]
    s = Session(root / "t.f", top="t")
    replace(s, root / "t.sv", b"arr [2]", b"arr [8]")
    r = s.finish()
    assert not r.ok
    assert [(c.kind, c.path, c.name) for c in r.unexpected_changes] == [("port-type-changed", "t", "arr")]


def test_port_type_change_in_diff():
    def snap(types):
        return InstanceSnap(module="m", params={}, ports={"a": ("input", 4)}, connections={}, port_types=types)

    assert diff_snapshots({"t": snap({"a": "logic[3:0]"})}, {"t": snap({"a": "logic signed[3:0]"})}) == [
        Change("port-type-changed", "t", "a", "logic[3:0]", "logic signed[3:0]")
    ]
    assert diff_snapshots({"t": snap({"a": "logic[3:0]"})}, {"t": snap({"a": "logic[3:0]"})}) == []


# --- layout, output, command line -------------------------------------------------------


def test_filelist_outside_the_design_goes_to_the_top_of_the_output(tmp_path: Path):
    kit = make_design(tmp_path / "repo")
    lists = tmp_path / "elsewhere" / "deep"
    lists.mkdir(parents=True)
    (lists / "combined.f").write_bytes(b"rtl/leaf.sv\nrtl/alt_leaf.sv\nrtl/mid.sv\nrtl/top.sv\n")
    s = Session(lists / "combined.f", top="top", base=kit.root)
    assert s.layout_root == kit.root
    out = write_tree(s, s.finish(), tmp_path / "out")
    assert sorted(tree_bytes(out)) == [
        "combined.f",
        "hdl-vet.json",
        "rtl/alt_leaf.sv",
        "rtl/leaf.sv",
        "rtl/mid.sv",
        "rtl/top.sv",
    ]
    assert (out / "combined.f").read_text() == "rtl/leaf.sv\nrtl/alt_leaf.sv\nrtl/mid.sv\nrtl/top.sv\n"
    assert load(out / "combined.f", top="top").snapshot() == s.design.snapshot()


def test_output_path_under_a_file_is_an_output_error(kit, tmp_path: Path):
    (tmp_path / "afile").write_text("x")
    s = Session(kit.filelist, top="top")
    with pytest.raises(OutputError):
        write_tree(s, s.finish(), tmp_path / "afile" / "out")


@pytest.mark.parametrize("body", ["import sys; sys.exit(0)", "raise SystemExit('bye')"])
def test_script_calling_exit_is_an_input_error(kit, tmp_path: Path, body, capsys):
    script = tmp_path / "s.py"
    script.write_text(f"def apply(vet):\n    {body}\n")
    out = tmp_path / "out"
    assert main(["apply", "-f", str(kit.filelist), "-o", str(out), "-s", str(script), "--top", "top"]) == 2
    assert not out.exists()
    assert "s.py" in capsys.readouterr().err


def test_dry_run_with_text_the_console_cannot_encode(kit, tmp_path: Path):
    kit.mid.write_bytes(kit.mid.read_bytes().replace(b"endmodule", "// größe ✓\nendmodule".encode()))
    script = tmp_path / "s.py"
    script.write_text(
        "def apply(vet):\n"
        "    m = vet.module('mid')\n"
        "    vet.splice(m.path, m.body_end, m.body_end, b'// note\\n')\n"
    )
    env = dict(os.environ, PYTHONIOENCODING="ascii")
    p = subprocess.run(
        [
            sys.executable, "-m", "hdl_vet", "apply", "-f", str(kit.filelist), "-s", str(script),
            "--top", "top", "--dry-run",
        ],
        capture_output=True,
        env=env,
        check=False,
    )
    assert p.returncode == 0, p.stderr
    assert b"+// note" in p.stdout
