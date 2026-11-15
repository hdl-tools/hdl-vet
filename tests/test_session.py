"""Phase 2: the edit session (`hdl_vet.session`). Imports no pyslang.

    Session(filelist, top=None, editable=None, base=None)
        Loads the baseline design. `editable` is an optional collection of paths
        that may be changed; None means every source and included file.
        `base` is passed to read_filelist (see test_output_cli.py).
        Raises SessionError when
          - the baseline has error diagnostics (the message names the first), or
          - any source or included file already carries the hdl-vet header
            (HEADER_MARK at the start of a line): input must be a fresh tree.

    Coordinates. Offsets passed to `splice` for a file are offsets into
    `session.text(path)`, which are the same offsets that the most recent
    `module()` result for that file reported.

    .text(path) -> bytes
        Content of a source or included file as of the last (re-)load.
    .splice(path, start, end, text, old=None)
        Queue a replacement of [start, end) by `text` (bytes). Nothing is parsed.
        SessionError if `path` is not a file of the design or is not editable,
        or if `old` is given and differs from the bytes currently at
        [start, end). SpliceError if it overlaps a queued splice of that file.
    .module(name) -> ModuleInfo
        Re-load on demand: queued splices are applied and the whole design is
        re-loaded only if the file that defines `name` has queued splices, or if
        `name` is unknown while anything is queued. Otherwise the answer comes
        from the last load. A re-load applies the queued splices of all files.
    .design -> Design
        The design with every queued splice applied (re-loads if any is queued).
    .reloads -> int
        Number of re-loads so far; the baseline load does not count.
    .expect(kind, path="*", name="*")
        Declare an expected structural change (see signoff.Expect).
    .finish(scripts=()) -> Result
        `scripts` is a sequence of (name, sha256 hex) pairs. Steps:
          1. apply queued splices;
          2. add the header line to every file whose content differs from the
             baseline (see below);
          3. if any file differs, re-load and compare with the baseline:
             new_diagnostics over all stages, diff_snapshots + check_changes;
          4. return the Result. A session can be finished once.
        The header is part of the text that is checked: what is verified is
        byte-for-byte what will be written. The header insertion is one more
        stage in each changed file's offset maps, so it never makes an existing
        diagnostic look new. The re-load in step 3 counts in `reloads`.
        After finish, `.design` is the final design.
    .layout_root -> Path
        The deepest directory containing every source file, included file,
        include directory and the top-level file list. Diff labels and manifest
        keys are POSIX paths relative to it.

    Header line
        HEADER_MARK = b"// Modified by hdl-vet"
        Full line: `// Modified by hdl-vet` when there are no scripts, else
        `// Modified by hdl-vet; scripts: a.py sha256:0123456789ab, b.py sha256:...`
        (first 12 hex digits). It is inserted as its own line directly after
        the file's leading comment block: the run of `//` lines, or one
        `/* ... */` block, that starts at the first byte after any BOM. Without
        such a block it becomes the first line (after any BOM). It ends with the
        file's own line ending: CRLF if the first line break in the file is
        CRLF, else LF (also for a file with no line break). A `/* */` block
        followed by code on its closing line is not a leading block. If the
        leading `//` run is the whole file and lacks a final line break, one is
        added before the header.

    Result
        ok: bool      no new diagnostics, no unexpected changes, no unmatched expects
        new_diagnostics: list[Diag]
        changes: list[Change]              everything diff_snapshots found
        unexpected_changes: list[Change]
        unmatched_expects: list[Expect]
        files: dict[Path, bytes]           new content of each changed file
        diff: str                          unified diffs of changed files, sorted by
                                           path, labels relative to layout_root
        report() -> str                    human-readable summary; names every new
                                           diagnostic (code, file, line), unexpected
                                           change and unmatched expect
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.session import HEADER_MARK, Session, SessionError
from hdl_vet.signoff import Change, Expect
from hdl_vet.splice import SpliceError
from vetkit import make_design, span

SCRIPTS = [("dft.py", "0123456789abcdef" * 4), ("swap.py", "fedcba9876543210" * 4)]
HEADER = b"// Modified by hdl-vet; scripts: dft.py sha256:0123456789ab, swap.py sha256:fedcba987654"


@pytest.fixture
def kit(tmp_path: Path):
    return make_design(tmp_path)


def replace(session: Session, path: Path, old: bytes, new: bytes, nth: int = 0) -> None:
    start, end = span(session.text(path), old, nth)
    session.splice(path, start, end, new, old=old)


# --- nothing to do -------------------------------------------------------------


def test_empty_session_changes_nothing(kit):
    s = Session(kit.filelist, top="top")
    r = s.finish(SCRIPTS)
    assert r.ok
    assert r.files == {} and r.diff == "" and r.changes == []
    assert r.new_diagnostics == [] and r.unexpected_changes == [] and r.unmatched_expects == []
    assert s.reloads == 0


def test_splice_that_restores_identical_bytes_changes_nothing(kit):
    s = Session(kit.filelist, top="top")
    replace(s, kit.top, b"u_mid", b"u_mid")
    r = s.finish(SCRIPTS)
    assert r.ok and r.files == {}


def test_finish_twice_is_an_error(kit):
    s = Session(kit.filelist, top="top")
    s.finish()
    with pytest.raises(SessionError):
        s.finish()


# --- refusals --------------------------------------------------------------------


def test_baseline_with_errors_is_refused(kit):
    kit.top.write_bytes(kit.top.read_bytes().replace(b"assign spare_d = a;", b"assign spare_d = nope;"))
    with pytest.raises(SessionError, match="UndeclaredIdentifier"):
        Session(kit.filelist, top="top")


def test_already_modified_input_is_refused(kit):
    kit.mid.write_bytes(HEADER_MARK + b"\n" + kit.mid.read_bytes())
    with pytest.raises(SessionError, match="mid.sv"):
        Session(kit.filelist, top="top")


def test_header_mark_inside_a_line_is_not_a_header(kit):
    kit.mid.write_bytes(kit.mid.read_bytes() + b"// see: // Modified by hdl-vet docs\n")
    Session(kit.filelist, top="top").finish()


def test_non_editable_file_is_refused(kit):
    s = Session(kit.filelist, top="top", editable=[kit.top])
    with pytest.raises(SessionError, match="leaf.sv"):
        s.splice(kit.leaf, 0, 0, b"// x\n")
    s.splice(kit.top, 0, 0, b"// x\n")


def test_file_outside_the_design_is_refused(kit):
    s = Session(kit.filelist, top="top")
    stray = kit.root / "stray.sv"
    stray.write_text("module stray; endmodule\n")
    with pytest.raises(SessionError, match="stray.sv"):
        s.splice(stray, 0, 0, b"x")


def test_old_text_guard(kit):
    s = Session(kit.filelist, top="top")
    start, end = span(s.text(kit.top), b"u_mid")
    with pytest.raises(SessionError, match="u_mid"):
        s.splice(kit.top, start, end, b"u_x", old=b"u_mxd")
    s.splice(kit.top, start, end, b"u_x", old=b"u_mid")


def test_overlapping_splices_are_rejected_when_queued(kit):
    s = Session(kit.filelist, top="top")
    start, end = span(s.text(kit.top), b"u_mid")
    s.splice(kit.top, start, end, b"u_x")
    with pytest.raises(SpliceError):
        s.splice(kit.top, start + 1, end + 1, b"u_y")


# --- re-load on demand -------------------------------------------------------------


def test_reload_only_when_a_queried_file_has_queued_splices(kit):
    s = Session(kit.filelist, top="top")
    assert s.reloads == 0
    top = s.module("top")
    assert [i.name for i in top.instances] == ["u_mid", "u_spare"]
    replace(s, kit.top, b"u_mid", b"u_middle")

    mid = s.module("mid")  # mid.sv is clean: no re-load
    assert s.reloads == 0
    i0 = mid.instance("u_l0")
    s.splice(kit.mid, i0.start, i0.start + 4, b"u_first", old=b"u_l0")
    s.module("leaf")  # leaf.sv is clean
    assert s.reloads == 0
    assert s.text(kit.top) == kit.top.read_bytes()  # still the last-loaded text

    top = s.module("top")  # top.sv has a queued splice: re-load everything
    assert s.reloads == 1
    assert [i.name for i in top.instances] == ["u_middle", "u_spare"]
    assert b"u_middle" in s.text(kit.top) and b"u_first" in s.text(kit.mid)

    assert [i.name for i in s.module("mid").instances] == ["u_first", "u_l1"]
    assert s.reloads == 1  # mid.sv was brought up to date by the same re-load


def test_offsets_after_a_reload_refer_to_the_new_text(kit):
    s = Session(kit.filelist, top="top")
    s.splice(kit.top, 0, 0, b"// extra first line\n")
    inst = s.module("top").instance("u_mid")
    text = s.text(kit.top)
    assert text.startswith(b"// extra first line\n")
    assert text[inst.start : inst.start + 5] == b"u_mid"


def test_unknown_module_reloads_when_something_is_queued(kit):
    s = Session(kit.filelist, top="top")
    end = len(s.text(kit.leaf))
    s.splice(kit.leaf, end, end, b"module extra;\nendmodule\n")
    assert s.module("extra").name == "extra"
    assert s.reloads == 1
    with pytest.raises(Exception, match="nope"):
        s.module("nope")
    assert s.reloads == 1


def test_design_property_is_always_current(kit):
    s = Session(kit.filelist, top="top")
    assert s.design is s.design and s.reloads == 0
    replace(s, kit.top, b"u_mid", b"u_middle")
    assert "top.u_middle" in s.design.snapshot()
    assert s.reloads == 1


def test_nothing_on_disk_changes(kit):
    before = {p: p.read_bytes() for p in kit.sources}
    s = Session(kit.filelist, top="top")
    replace(s, kit.top, b"u_mid", b"u_middle")
    s.module("top")
    s.expect("*")
    s.finish(SCRIPTS)
    assert {p: p.read_bytes() for p in kit.sources} == before


# --- sign-off: good edits ------------------------------------------------------------


def test_pure_rename_gives_no_new_diagnostic(kit):
    s = Session(kit.filelist, top="top")
    # Lengthen the banner and rename an instance above the baseline warnings:
    # both shift them, neither touches their text.
    s.splice(kit.top, 0, 0, b"// extra first line\n")
    replace(s, kit.top, b"u_mid", b"u_mid_with_a_longer_name")
    s.expect("instance-removed", "top.u_mid*")
    s.expect("instance-added", "top.u_mid_with_a_longer_name*")
    r = s.finish(SCRIPTS)
    assert r.new_diagnostics == []
    assert r.ok, r.report()
    assert list(r.files) == [kit.top]


def test_renaming_a_warning_bearing_net_is_reported(kit):
    # Conservative by design: inside edited text a warning must match exactly,
    # because "one implicit net went, another came" is also what a typo looks like.
    s = Session(kit.filelist, top="top")
    replace(s, kit.top, b"spare_q", b"spare_q_renamed")
    s.expect("connection-changed", "top.u_spare", "q")
    r = s.finish(SCRIPTS)
    assert not r.ok
    assert "ImplicitNet" in [d.code for d in r.new_diagnostics]
    assert r.unexpected_changes == [] and r.unmatched_expects == []


def test_comment_only_edit_needs_no_expects(kit):
    s = Session(kit.filelist, top="top")
    replace(s, kit.mid, b"  leaf #(.WIDTH(4)) u_l0", b"  // first leaf\n  leaf #(.WIDTH(4)) u_l0")
    r = s.finish(SCRIPTS)
    assert r.ok, r.report()
    assert r.changes == []
    assert r.files[kit.mid].startswith(HEADER + b"\nmodule mid (")
    assert "+  // first leaf" in r.diff.splitlines()
    assert "--- a/rtl/mid.sv" in r.diff.splitlines()


def test_edits_across_stages_and_files(kit):
    s = Session(kit.filelist, top="top")
    replace(s, kit.leaf, b"  output logic [WIDTH-1:0] q\n", b"  output logic [WIDTH-1:0] q,\n  input  logic scan_en\n")
    replace(s, kit.leaf, b"q <= d;", b"q <= scan_en ? '0 : d;")
    leaf = s.module("leaf")  # re-load 1
    assert [p.name for p in leaf.ports][-1] == "scan_en"
    for inst in s.module("mid").instances:
        s.splice(kit.mid, inst.ports_close, inst.ports_close, b", .scan_en(1'b0)")
    spare = s.module("top").instance("u_spare")
    s.splice(kit.top, spare.ports_close, spare.ports_close, b", .scan_en(1'b0)")
    s.expect("port-added", "top.*", "scan_en")
    s.expect("connection-changed", "top.*", "scan_en")
    r = s.finish(SCRIPTS)
    assert r.ok, r.report()
    assert s.reloads == 2
    assert sorted(r.files) == [kit.leaf, kit.mid, kit.top]
    assert len([c for c in r.changes if c.kind == "port-added"]) == 3
    assert r.diff.index("rtl/leaf.sv") < r.diff.index("rtl/mid.sv") < r.diff.index("rtl/top.sv")


# --- sign-off: bad edits must be caught -----------------------------------------------


def test_undeclared_signal_in_a_port_connection_is_caught(kit):
    s = Session(kit.filelist, top="top")
    replace(s, kit.mid, b".d(a), .q(y0)", b".d(a_typo), .q(y0)")
    s.expect("*")
    r = s.finish(SCRIPTS)
    assert not r.ok
    assert "ImplicitNet" in [d.code for d in r.new_diagnostics]
    assert all(d.path == kit.mid for d in r.new_diagnostics)
    assert "ImplicitNet" in r.report() and "mid.sv" in r.report()


def test_undeclared_identifier_in_an_expression_is_caught(kit):
    s = Session(kit.filelist, top="top")
    replace(s, kit.top, b"assign spare_d = a;", b"assign spare_d = nope;")
    r = s.finish(SCRIPTS)
    assert not r.ok
    assert [d.code for d in r.new_diagnostics] == ["UndeclaredIdentifier"]


def test_module_swap_with_wrong_ports_is_caught(kit):
    s = Session(kit.filelist, top="top")
    inst = s.module("top").instance("u_spare")
    s.splice(kit.top, inst.module_start, inst.module_end, b"alt_leaf", old=b"leaf")
    s.expect("*")
    r = s.finish(SCRIPTS)
    assert not r.ok
    assert any(d.severity == "error" and "'d'" in d.message for d in r.new_diagnostics)


def test_wrong_width_parameter_is_caught_twice(kit):
    s = Session(kit.filelist, top="top")
    ov = s.module("mid").instance("u_l0").param_override("WIDTH")
    s.splice(kit.mid, ov.start, ov.end, b".WIDTH(8)", old=b".WIDTH(4)")
    r = s.finish(SCRIPTS)
    assert not r.ok
    assert r.new_diagnostics, "the width mismatch must be diagnosed"
    assert all(d.path == kit.mid for d in r.new_diagnostics)
    assert Change("param-changed", "top.u_mid.u_l0", "WIDTH", "4", "8") in r.unexpected_changes
    assert Change("port-changed", "top.u_mid.u_l0", "q", "output 4", "output 8") in r.unexpected_changes


def test_undeclared_structural_change_is_caught_even_without_diagnostics(kit):
    s = Session(kit.filelist, top="top")
    replace(s, kit.mid, b".DEPTH(3)", b".DEPTH(5)")
    r = s.finish(SCRIPTS)
    assert r.new_diagnostics == []
    assert not r.ok
    assert r.unexpected_changes == [Change("param-changed", "top.u_mid.u_l1", "DEPTH", "3", "5")]
    assert "top.u_mid.u_l1" in r.report() and "DEPTH" in r.report()


def test_declared_change_passes_and_unmatched_expect_fails(kit):
    s = Session(kit.filelist, top="top")
    replace(s, kit.mid, b".DEPTH(3)", b".DEPTH(5)")
    s.expect("param-changed", "top.u_mid.u_l1", "DEPTH")
    assert s.finish(SCRIPTS).ok

    s = Session(kit.filelist, top="top")
    replace(s, kit.mid, b".DEPTH(3)", b".DEPTH(5)")
    s.expect("param-changed", "top.u_mid.u_l1", "DEPTH")
    s.expect("port-added", "top", "dft_en")
    r = s.finish(SCRIPTS)
    assert not r.ok
    assert r.unmatched_expects == [Expect("port-added", "top", "dft_en")]
    assert "dft_en" in r.report()


def test_expect_without_any_edit_fails(kit):
    s = Session(kit.filelist, top="top")
    s.expect("port-added", "top", "dft_en")
    r = s.finish()
    assert not r.ok and r.unmatched_expects == [Expect("port-added", "top", "dft_en")]


def test_syntax_error_is_caught(kit):
    s = Session(kit.filelist, top="top")
    replace(s, kit.mid, b"endmodule", b"endmodul")
    r = s.finish(SCRIPTS)
    assert not r.ok
    assert any(d.severity == "error" for d in r.new_diagnostics)


# --- header ------------------------------------------------------------------------


def test_header_goes_after_the_banner(kit):
    s = Session(kit.filelist, top="top")
    replace(s, kit.top, b"  assign spare_d = a;\n", b"  assign spare_d = a;  // tap\n")
    r = s.finish(SCRIPTS)
    assert r.ok, r.report()
    lines = r.files[kit.top].split(b"\n")
    assert lines[:4] == [
        b"// Generated by a generator. Do not edit.",
        b"// sha256: 0000",
        HEADER,
        b"",
    ]
    assert r.files[kit.top].count(HEADER_MARK) == 1


def test_header_without_scripts(kit):
    s = Session(kit.filelist, top="top")
    replace(s, kit.mid, b"  leaf #(.WIDTH(4)) u_l0", b"  /* x */ leaf #(.WIDTH(4)) u_l0")
    r = s.finish()
    assert r.files[kit.mid].startswith(b"// Modified by hdl-vet\nmodule mid (")


def test_header_uses_crlf_in_a_crlf_file(tmp_path: Path):
    kit = make_design(tmp_path, newline="\r\n")
    s = Session(kit.filelist, top="top")
    replace(s, kit.top, b"  assign spare_d = a;\r\n", b"  assign spare_d = a;  // tap\r\n")
    r = s.finish(SCRIPTS)
    assert r.ok, r.report()
    out = r.files[kit.top]
    assert b"// sha256: 0000\r\n" + HEADER + b"\r\n\r\nmodule top" in out
    assert out.count(b"\n") == out.count(b"\r\n")


@pytest.mark.parametrize(
    ("prefix", "expected_start"),
    [
        (b"/* block\n   banner */\n", b"/* block\n   banner */\n" + HEADER + b"\n"),
        (b"\xef\xbb\xbf", b"\xef\xbb\xbf" + HEADER + b"\n"),
        (b"\xef\xbb\xbf// one\n", b"\xef\xbb\xbf// one\n" + HEADER + b"\n"),
        (b"\n// not leading\n", HEADER + b"\n\n// not leading\n"),
    ],
)
def test_header_position_variants(kit, prefix, expected_start):
    kit.mid.write_bytes(prefix + kit.mid.read_bytes())
    s = Session(kit.filelist, top="top")
    replace(s, kit.mid, b"  leaf #(.WIDTH(4)) u_l0", b"  /* x */ leaf #(.WIDTH(4)) u_l0")
    r = s.finish(SCRIPTS)
    assert r.ok, r.report()
    assert r.files[kit.mid].startswith(expected_start + b"module mid (")


def test_output_is_what_was_checked(kit):
    from hdl_vet.slang_adapter import load

    s = Session(kit.filelist, top="top")
    replace(s, kit.mid, b".DEPTH(3)", b".DEPTH(5)")
    s.expect("param-changed")
    r = s.finish(SCRIPTS)
    assert r.ok
    design = load(kit.filelist, top="top", overrides=r.files)
    assert design.snapshot() == s.design.snapshot()
    assert [d for d in design.diagnostics() if d.severity == "error"] == []
