"""Phase 5b: save a session and resume it.

Motivation: incremental work. Apply scripts A and B, save, then try script C
again and again without reading the input tree and without running A and B.

Owner decisions:
    - Staleness is the user's business. Nothing checks whether the file list,
      the sources or the applied scripts changed after the save. A resumed
      session continues from exactly what was saved. The help text of
      `--resume` says so.
    - A state is saved as it is, whether or not it would pass sign-off, and
      its sign-off status is reported. The sign-off after a resume covers
      every script, earlier and later.
    - The saved session is one zip archive of plain data (no pickle: loading
      it never runs code).
    - A script the saved session already applied (same name and content) is
      refused on resume.
    - Same paths only: the archive records where the input tree was, and a
      resumed session reports and lays out files exactly as the first run
      did. The input tree itself is not needed any more.

Python API
    vet.save(path, scripts=())
        First applies and loads what is queued (as `refresh` does), then
        writes the archive. `scripts` are (name, sha256) pairs applied since
        the session began or was resumed; they are appended to
        `vet.session.scripts` (a list of tuples). The session stays usable.
        VetError, with no archive written and the session unchanged: inside a
        `vet.group()` block, after `finish`, when the queued edits cannot be
        loaded, or when `path` lies inside the input tree (its layout root).
        An existing file is replaced; the archive is written next to its
        destination and renamed into place.
        The bytes of the archive depend only on the state: saving the same
        state twice gives identical files (fixed timestamps, sorted entries,
        deflated). A save ends an offset-map stage, so archives saved at
        different points of the same edits may differ in bytes while
        restoring to sessions that behave the same.
    Vet.restore(path) -> Vet
        A Vet on a new Session in the saved state. The input tree and the
        added files' original locations are not needed; if the input tree
        still exists, include lookups may still consult it. SessionError when
        the file is not a session archive, is damaged, or has another format
        version (the message names both versions), and when a path in the
        index is not an absolute, normalised path (no `..`) under the layout
        root or an added file's name is not a plain file name: a crafted
        archive must not make `write_tree` write outside the output directory.
        After a restore `session.reloads` is 1 (0 only when nothing had
        changed, so that the loaded design is the baseline itself); the
        counter is not stored. Operations use `reloads == 0` to mean "the
        loaded design is the baseline".
    vet.session.scripts -> list[tuple[str, str]]
        Scripts applied so far, in order (empty for a new session).
    vet.finish(scripts=())
        As before; the header line lists `session.scripts` followed by
        `scripts`.
    vet.check(scripts=()) -> Result
        What `finish` would return now, without finishing: afterwards the
        session, its text and its queue are as before, and operations and
        `finish` still work. The Vet keeps its record of what is waiting for
        a re-load (dirty marks, names added since the last load); only
        `session.reloads` advances. VetError inside a group and after
        `finish`.

    What a restored Vet has: the text of every file, the stages that map
    baseline offsets to it, expectations and permits, post-conditions (now
    plain data, not closures, so they can be stored; they are evaluated at
    `finish` as before), the log, the notes, added files, and what
    `replace_module` recorded about swapped instances, which files are
    editable, and the scripts applied. The baseline is re-loaded from the
    saved original bytes, so sign-off after a resume compares against the
    same baseline as an unsaved run. No slang object, diagnostic list or
    snapshot is stored. Values that operations recorded (an added file's own
    warnings, the swapped modules, the values post-conditions captured, the
    notes) are stored as recorded. Whatever is a tuple, an ordered list or
    bytes in the live session is the same after a restore: a later operation
    that replaces or drops an earlier post-condition or expectation
    (`remove_instance` after `connect`) behaves as in an unsaved run.

Archive layout (a reader may open it with any zip tool)
    session.json                 the index: {"format": 3, "tool": "hdl-vet",
                                 "version": ..., "top": ..., "scripts": [...],
                                 ...}; everything that is not file content
    original/<path>              every source and included file as loaded,
                                 <path> relative to the layout root
    current/<path>               the files that differ from the original
    added/<name>                 the files added with `add_file`, under
                                 their plain file names
    Entries are deflated.

Format 2 (#57)
    The index stores the key of every post-condition. A later operation
    replaces an earlier post-condition only when the two keys are equal.
    F1  An archive written now has `"format": 3` (2 came with the one key for a
        pair; 3 with the marks of the raw splices, tests/ops/test_raw_splice_review.py).
    F2  An archive of format 1 is refused, as any other format is. Format 1
        stored a key for each operation, such as `add_wire` and `remove_wire`.
        A resumed `remove_wire` could then never replace the saved check of
        an `add_wire`, and the sign-off would fail a correct run.
    F3  From format 2 on the two operations of a pair store one key: `wire`,
        `assign`, `port` or `param`, then the module, then the name. The
        `kind` stays the name of the operation, so the right evaluator runs.
    F4  A pair that a save splits behaves as in an unsaved run, in every
        order (rules W6 to W11 in `tests/ops/test_ops.py`).

Command line
    vet apply -f F [--top T] [--base B] -s a.py -s b.py --save S [-o OUT]
        Runs the scripts, then saves. stdout gets "saved <S>" and one line
        "sign-off so far: ok" or "sign-off so far: FAILED"; in the second
        case the report goes to stderr. Saving a state that fails sign-off is
        not an error: the exit status is 0 unless `-o` is also given.
        `-o` is not required with `--save`. With `-o` or `--dry-run` as
        well, the archive is written once every script has run, before
        sign-off; then the run goes on as usual (exit 1 when sign-off fails;
        the save exists either way) and the "sign-off so far" line is not
        printed. `--save` naming a file inside the `-o` directory or inside
        the input tree is refused before anything runs (exit 2).
    vet apply --resume S -s c.py -o OUT [--save S2] [--dry-run] [--force]
        Continues from S. `-f`, `--top` and `--base` must not be given
        (exit 2). S is not modified unless `--save` names it; then it is
        replaced by the new archive at the usual moment (once every script
        has run).
        A script whose name and sha256 are among the saved session's scripts
        is refused (exit 2, naming the script). The manifest and the header
        line list every script, saved ones first.
        "N applied, M skipped" counts the operations since the resume, and
        only notes made since the resume are printed.
    One of `-f` and `--resume` is required, and one of `-o`, `--save` and
    `--dry-run`. These usage errors, like the others above, make `main`
    return 2 with a message on stderr (they do not exit through argparse).
    A script that fails in any way writes no archive.

Accept: save after A and B, resume with C: the output tree, manifest
included, is byte-identical to one run of A, B and C.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from hdl_vet.cli import main
from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session, SessionError
from instance_cells import CELLS
from no_comparison import no_comparison  # noqa: F401
from vetkit import make_design, span, tree_bytes

A = """\
def apply(vet):
    vet.module("top").add_port("dft_en", "input")
    vet.module("mid").add_port("dft_en", "input")
    vet.module("top").instance("u_mid").connect("dft_en", "dft_en")
    vet.module("mid").instance("u_l1").set_param("DEPTH", 4, was=3)
"""

B = """\
from pathlib import Path
def apply(vet):
    vet.add_file(Path(CELLS) / "cell_leaf.sv")
    vet.module("mid").instance("u_l0").replace_module(
        "cell_leaf", ports={"clk": "CK", "d": "D", "q": "Q"}, drop_params=["WIDTH"])
"""

C = """\
def apply(vet):
    vet.module("top").remove_instance("u_spare")
    vet.module("mid").instance("u_l0").connect("D", "~a", was="a")  # the instance B swapped
"""

C0 = """\
def apply(vet):
    vet.module("top").remove_instance("u_spare")
"""

# Undoes A's override behind its back: A's post-condition must still be there after a resume.
UNDO = """\
def apply(vet):
    path = vet.module("mid").path
    data = vet.text(path)
    at = data.index(b".DEPTH(4)")
    vet.splice(path, at, at + len(b".DEPTH(4)"), b".DEPTH(3)")
"""

BREAK = """\
def apply(vet):
    vet.module("mid").instance("u_l1").disconnect("q", was="y1")
"""

MEND = """\
def apply(vet):
    vet.module("mid").add_assign("y1", "'0")
"""


@pytest.fixture
def work(tmp_path: Path):
    """A fresh input tree, the cells and the scripts; returns a small namespace."""
    kit = make_design(tmp_path / "in")
    cells = tmp_path / "cells"
    cells.mkdir()
    for name, text in CELLS.items():
        (cells / name).write_bytes(text.encode())
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name, text in (("a", A), ("b", B), ("c", C), ("c0", C0), ("undo", UNDO), ("break", BREAK), ("mend", MEND)):
        (scripts / f"{name}.py").write_text(text.replace("CELLS", repr(str(cells))), encoding="utf-8")

    class Work:
        pass

    w = Work()
    w.kit, w.cells, w.tmp = kit, cells, tmp_path
    w.script = lambda name: str(scripts / f"{name}.py")
    return w


def run(*args: object) -> int:
    return main([str(a) for a in args])


def first(w, *names: str) -> list[str]:
    out = ["apply", "-f", str(w.kit.filelist), "--top", "top"]
    for n in names:
        out += ["-s", w.script(n)]
    return out


def scripts_of(*paths: str) -> list[tuple[str, str]]:
    return [(Path(p).name, hashlib.sha256(Path(p).read_bytes()).hexdigest()) for p in paths]


# --- command line: the acceptance ----------------------------------------------------------


def test_save_then_resume_equals_one_run(work, capsys):
    w = work
    assert run(*first(w, "a", "b", "c"), "-o", w.tmp / "one") == 0
    assert run(*first(w, "a", "b"), "--save", w.tmp / "ab.zip") == 0
    out = capsys.readouterr().out.splitlines()
    assert f"saved {w.tmp / 'ab.zip'}" in out and "sign-off so far: ok" in out
    assert not (w.tmp / "two").exists()
    assert run("apply", "--resume", w.tmp / "ab.zip", "-s", w.script("c"), "-o", w.tmp / "two") == 0
    lines = capsys.readouterr().out.splitlines()
    assert "2 applied, 0 skipped" in lines  # the operations of c only
    assert tree_bytes(w.tmp / "two") == tree_bytes(w.tmp / "one")
    manifest = json.loads((w.tmp / "two" / "hdl-vet.json").read_bytes())
    assert [s["name"] for s in manifest["scripts"]] == ["a.py", "b.py", "c.py"]
    assert b"scripts: a.py sha256:" in (w.tmp / "two" / "rtl" / "mid.sv").read_bytes()


def test_resume_needs_neither_the_input_tree_nor_the_cells(work):
    w = work
    assert run(*first(w, "a", "b", "c"), "-o", w.tmp / "one") == 0
    assert run(*first(w, "a", "b"), "--save", w.tmp / "ab.zip") == 0
    shutil.rmtree(w.kit.root)
    shutil.rmtree(w.cells)
    assert run("apply", "--resume", w.tmp / "ab.zip", "-s", w.script("c"), "-o", w.tmp / "two") == 0
    assert tree_bytes(w.tmp / "two") == tree_bytes(w.tmp / "one")


def test_resuming_twice_gives_the_same_and_leaves_the_archive_alone(work):
    w = work
    assert run(*first(w, "a", "b"), "--save", w.tmp / "ab.zip") == 0
    saved = (w.tmp / "ab.zip").read_bytes()
    for name in ("two", "three"):
        assert run("apply", "--resume", w.tmp / "ab.zip", "-s", w.script("c"), "-o", w.tmp / name) == 0
    assert tree_bytes(w.tmp / "two") == tree_bytes(w.tmp / "three")
    assert (w.tmp / "ab.zip").read_bytes() == saved


def test_saves_can_be_chained(work):
    w = work
    assert run(*first(w, "a", "b", "c"), "-o", w.tmp / "one") == 0
    assert run(*first(w, "a"), "--save", w.tmp / "a.zip") == 0
    assert run("apply", "--resume", w.tmp / "a.zip", "-s", w.script("b"), "--save", w.tmp / "ab.zip") == 0
    assert run("apply", "--resume", w.tmp / "ab.zip", "-s", w.script("c"), "-o", w.tmp / "two") == 0
    assert tree_bytes(w.tmp / "two") == tree_bytes(w.tmp / "one")
    # an archive saved directly behaves the same (its bytes may differ: see `save`)
    assert run(*first(w, "a", "b"), "--save", w.tmp / "direct.zip") == 0
    assert run("apply", "--resume", w.tmp / "direct.zip", "-s", w.script("c"), "-o", w.tmp / "three") == 0
    assert tree_bytes(w.tmp / "three") == tree_bytes(w.tmp / "one")


def test_resume_with_no_further_script_equals_the_unsaved_run(work):
    w = work
    assert run(*first(w, "a", "b"), "-o", w.tmp / "one") == 0
    assert run(*first(w, "a", "b"), "--save", w.tmp / "ab.zip") == 0
    assert run("apply", "--resume", w.tmp / "ab.zip", "-o", w.tmp / "two") == 0
    assert tree_bytes(w.tmp / "two") == tree_bytes(w.tmp / "one")


def test_dry_run_after_a_resume_prints_the_whole_diff(work, capsys):
    w = work
    assert run(*first(w, "a", "b", "c"), "--dry-run") == 0
    whole = capsys.readouterr().out
    assert run(*first(w, "a", "b"), "--save", w.tmp / "ab.zip") == 0
    capsys.readouterr()
    assert run("apply", "--resume", w.tmp / "ab.zip", "-s", w.script("c"), "--dry-run") == 0
    assert capsys.readouterr().out == whole


def test_save_and_output_in_one_command(work):
    w = work
    assert run(*first(w, "a", "b"), "--save", w.tmp / "ab.zip", "-o", w.tmp / "one") == 0
    assert run(*first(w, "a", "b"), "-o", w.tmp / "ref") == 0
    assert tree_bytes(w.tmp / "one") == tree_bytes(w.tmp / "ref")
    assert run("apply", "--resume", w.tmp / "ab.zip", "-o", w.tmp / "two") == 0
    assert tree_bytes(w.tmp / "two") == tree_bytes(w.tmp / "ref")


# --- sign-off covers everything ------------------------------------------------------------


def test_post_conditions_of_saved_scripts_are_still_checked(work, capsys):
    w = work
    assert run(*first(w, "a"), "--save", w.tmp / "a.zip") == 0
    capsys.readouterr()
    assert run("apply", "--resume", w.tmp / "a.zip", "-s", w.script("undo"), "-o", w.tmp / "out") == 1
    err = capsys.readouterr().err
    assert "failed check: set_param mid.u_l1 DEPTH" in err
    assert not (w.tmp / "out").exists()


def test_a_failing_state_is_saved_and_can_be_mended_after_a_resume(work, capsys):
    w = work
    assert run(*first(w, "break"), "--save", w.tmp / "broken.zip") == 0
    captured = capsys.readouterr()
    assert "sign-off so far: FAILED" in captured.out.splitlines()
    assert "left dangling: undriven output port 'y1'" in captured.err
    assert (w.tmp / "broken.zip").is_file()
    assert run("apply", "--resume", w.tmp / "broken.zip", "-o", w.tmp / "still") == 1
    assert "left dangling" in capsys.readouterr().err
    assert run("apply", "--resume", w.tmp / "broken.zip", "-s", w.script("mend"), "-o", w.tmp / "mended") == 0
    assert run(*first(w, "break", "mend"), "-o", w.tmp / "ref") == 0
    assert tree_bytes(w.tmp / "mended") == tree_bytes(w.tmp / "ref")


def test_save_with_output_fails_when_sign_off_fails_but_keeps_the_save(work):
    w = work
    assert run(*first(w, "break"), "--save", w.tmp / "broken.zip", "-o", w.tmp / "out") == 1
    assert (w.tmp / "broken.zip").is_file() and not (w.tmp / "out").exists()


# --- refusals --------------------------------------------------------------------------------


def test_a_script_already_applied_is_refused_on_resume(work, capsys):
    w = work
    assert run(*first(w, "a", "b"), "--save", w.tmp / "ab.zip") == 0
    capsys.readouterr()
    assert run("apply", "--resume", w.tmp / "ab.zip", "-s", w.script("b"), "-o", w.tmp / "out") == 2
    err = capsys.readouterr().err
    assert "b.py" in err and "already" in err and "Traceback" not in err
    assert not (w.tmp / "out").exists()
    # the same name with other content is another script
    Path(w.script("b")).write_text("def apply(vet):\n    pass\n", encoding="utf-8")
    assert run("apply", "--resume", w.tmp / "ab.zip", "-s", w.script("b"), "-o", w.tmp / "out") == 0


@pytest.mark.parametrize("extra", [["-f", "x.f"], ["--top", "top"], ["--base", "."]])
def test_resume_takes_no_input_options(work, capsys, extra):
    w = work
    assert run(*first(w, "a"), "--save", w.tmp / "a.zip") == 0
    capsys.readouterr()
    assert run("apply", "--resume", w.tmp / "a.zip", *extra, "-o", w.tmp / "out") == 2
    assert "--resume" in capsys.readouterr().err


def test_something_must_be_asked_for(work, capsys):
    w = work
    assert run(*first(w, "a")) == 2  # neither -o, --save nor --dry-run
    assert run("apply", "-s", w.script("a"), "-o", w.tmp / "out") == 2  # neither -f nor --resume
    capsys.readouterr()


def test_another_format_version_is_refused(work, capsys):
    w = work
    assert run(*first(w, "a"), "--save", w.tmp / "a.zip") == 0
    other = w.tmp / "future.zip"
    with zipfile.ZipFile(w.tmp / "a.zip") as src, zipfile.ZipFile(other, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "session.json":
                index = json.loads(data)
                assert index["format"] == 4 and index["tool"] == "hdl-vet"
                index["format"] = 99
                data = json.dumps(index).encode()
            dst.writestr(item, data)
    capsys.readouterr()
    assert run("apply", "--resume", other, "-o", w.tmp / "out") == 2
    err = capsys.readouterr().err
    assert "format" in err and "99" in err and "reads format 4" in err and "Traceback" not in err
    with pytest.raises(SessionError, match="99"):
        Vet.restore(other)


@pytest.mark.parametrize("content", [b"", b"not a zip at all", b"PK\x03\x04 truncated"])
def test_a_file_that_is_not_a_session_archive_is_refused(work, capsys, content):
    w = work
    bad = w.tmp / "bad.zip"
    bad.write_bytes(content)
    assert run("apply", "--resume", bad, "-o", w.tmp / "out") == 2
    assert "Traceback" not in capsys.readouterr().err
    assert run("apply", "--resume", w.tmp / "missing.zip", "-o", w.tmp / "out") == 2
    with pytest.raises(SessionError, match="bad.zip"):
        Vet.restore(bad)


def test_a_zip_without_an_index_is_refused(work):
    w = work
    other = w.tmp / "other.zip"
    with zipfile.ZipFile(other, "w") as z:
        z.writestr("readme.txt", "hello")
    with pytest.raises(SessionError, match="session"):
        Vet.restore(other)


def test_a_script_error_writes_no_archive(work, capsys):
    w = work
    bad = w.tmp / "bad.py"
    bad.write_text("def apply(vet):\n    vet.module('nope')\n", encoding="utf-8")
    assert run(*first(w, "a"), "-s", bad, "--save", w.tmp / "a.zip") == 2
    assert not (w.tmp / "a.zip").exists()
    capsys.readouterr()


def test_the_help_says_that_changes_after_a_save_are_not_detected(capsys):
    with pytest.raises(SystemExit):
        main(["apply", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "--resume" in text and "--save" in text
    assert "not detected" in text


# --- Python API ----------------------------------------------------------------------------


@pytest.fixture
def kit(tmp_path: Path):
    return make_design(tmp_path / "in")


def swap_and_port(vet: Vet, cells: Path) -> None:
    vet.module("top").add_port("dft_en", "input")
    vet.add_file(cells / "cell_leaf.sv")
    vet.module("mid").instance("u_l0").replace_module(
        "cell_leaf", ports={"clk": "CK", "d": "D", "q": "Q"}, drop_params=["WIDTH"]
    )
    vet.module("top").remove_instance("u_spare")  # leaves a note about spare_d


@pytest.fixture
def cells(tmp_path: Path) -> Path:
    d = tmp_path / "cells"
    d.mkdir()
    for name, text in CELLS.items():
        (d / name).write_bytes(text.encode())
    return d


def test_restore_gives_back_the_state(kit, cells, tmp_path: Path):
    vet = Vet(Session(kit.filelist, top="top"))
    swap_and_port(vet, cells)
    vet.save(tmp_path / "s.zip", scripts=[("first.py", "ab" * 32)])
    assert vet.session.scripts == [("first.py", "ab" * 32)]

    back = Vet.restore(tmp_path / "s.zip")
    assert back.log == vet.log and back.notes == vet.notes and back.notes != []
    assert back.session.scripts == [("first.py", "ab" * 32)]
    assert back.session.top == "top" and back.session.layout_root == vet.session.layout_root
    assert back.session.files == vet.session.files
    assert all(back.text(p) == vet.text(p) for p in vet.session.files)
    assert back.session.original(kit.mid) == kit.mid.read_bytes()
    assert back.session.added == vet.session.added
    assert back.module("mid").instance("u_l0").paths == ["top.u_mid.u_l0"]  # the swapped handle

    a, b = vet.finish([("second.py", "cd" * 32)]), back.finish([("second.py", "cd" * 32)])
    assert a.ok and b.ok, a.report() + b.report()
    assert (b.diff, b.files, b.added, b.changes) == (a.diff, a.files, a.added, a.changes)
    assert b"scripts: first.py sha256:abababababab, second.py sha256:cdcdcdcdcdcd" in b.files[kit.mid]


def test_the_session_stays_usable_after_a_save(kit, tmp_path: Path):
    vet = Vet(Session(kit.filelist, top="top"))
    vet.module("top").add_port("dft_en", "input")
    vet.save(tmp_path / "s.zip")
    vet.module("mid").instance("u_l0").connect("d", "~a", was="a")
    r = vet.finish()
    assert r.ok and len(r.files) == 2


def test_saving_the_same_state_twice_gives_identical_archives(kit, cells, tmp_path: Path):
    for name in ("one.zip", "two.zip"):
        vet = Vet(Session(kit.filelist, top="top"))
        swap_and_port(vet, cells)
        vet.save(tmp_path / name)
    assert (tmp_path / "one.zip").read_bytes() == (tmp_path / "two.zip").read_bytes()
    back = Vet.restore(tmp_path / "one.zip")
    back.save(tmp_path / "three.zip")
    assert (tmp_path / "three.zip").read_bytes() == (tmp_path / "one.zip").read_bytes()


def test_the_archive_can_be_read_with_a_zip_tool(kit, cells, tmp_path: Path):
    vet = Vet(Session(kit.filelist, top="top"))
    swap_and_port(vet, cells)
    vet.save(tmp_path / "s.zip")
    with zipfile.ZipFile(tmp_path / "s.zip") as z:
        names = z.namelist()
        assert names == sorted(names) and "session.json" in names
        assert {n for n in names if n.startswith("original/")} == {
            "original/rtl/leaf.sv",
            "original/rtl/alt_leaf.sv",
            "original/rtl/mid.sv",
            "original/rtl/top.sv",
        }
        assert {n for n in names if n.startswith("current/")} == {"current/rtl/mid.sv", "current/rtl/top.sv"}
        assert [n for n in names if n.startswith("added/")] == ["added/cell_leaf.sv"]
        assert z.read("original/rtl/mid.sv") == kit.mid.read_bytes()
        assert z.read("current/rtl/mid.sv") == vet.text(kit.mid)
        assert z.read("added/cell_leaf.sv") == CELLS["cell_leaf.sv"].encode()
        index = json.loads(z.read("session.json"))
        assert index["format"] == 4 and index["tool"] == "hdl-vet" and index["top"] == "top"
        assert index["scripts"] == []


def test_save_is_refused_inside_a_group_and_after_finish(kit, tmp_path: Path):
    vet = Vet(Session(kit.filelist, top="top"))
    with pytest.raises(VetError, match="group"):
        with vet.group():
            vet.save(tmp_path / "s.zip")
    vet.finish()
    with pytest.raises(VetError, match="finish"):
        vet.save(tmp_path / "s.zip")
    assert not (tmp_path / "s.zip").exists()


def test_save_replaces_an_existing_file_only_when_it_succeeds(kit, tmp_path: Path):
    target = tmp_path / "s.zip"
    target.write_bytes(b"old")
    vet = Vet(Session(kit.filelist, top="top"))
    vet.module("top").add_port("dft_en", "input")
    vet.save(target)
    assert zipfile.is_zipfile(target)
    assert [p.name for p in tmp_path.iterdir() if p.name != "in"] == ["s.zip"]  # no temporary file left


def test_check_reports_without_finishing(kit):
    vet = Vet(Session(kit.filelist, top="top"))
    vet.module("mid").instance("u_l1").disconnect("q", was="y1")
    before = vet.text(kit.mid)
    r = vet.check()
    assert not r.ok and [d.message for d in r.dangling] == ["undriven output port 'y1'"]
    assert vet.text(kit.mid) == before  # no header line was left behind
    assert vet.check().dangling == r.dangling
    vet.module("mid").add_assign("y1", "'0")
    assert vet.check().ok
    final = vet.finish()
    assert final.ok, final.report()


def test_expectations_and_raw_splices_survive_a_resume(kit, tmp_path: Path):
    vet = Vet(Session(kit.filelist, top="top"))
    start, end = span(vet.text(kit.mid), b"u_l0")
    vet.splice(kit.mid, start, end, b"u_first")
    vet.expect("instance-removed", "top.u_mid.u_l0", exact=True)
    vet.expect("instance-added", "top.u_mid.u_first", exact=True)
    vet.expect("instance-added", "top.u_mid.u_never", exact=True)  # will not happen
    vet.save(tmp_path / "s.zip")
    back = Vet.restore(tmp_path / "s.zip")
    r = back.finish()
    assert not r.ok
    assert [(e.kind, e.path) for e in r.unmatched_expects] == [("instance-added", "top.u_mid.u_never")]
    assert r.unexpected_changes == [] and r.new_diagnostics == []


def test_a_baseline_warning_that_moved_is_not_new_after_a_resume(kit, tmp_path: Path):
    # top.sv has a baseline ImplicitNet warning (spare_q). Lines are added above it before
    # the save and after the resume: the stages of both must reach sign-off.
    vet = Vet(Session(kit.filelist, top="top"))
    vet.module("top").add_wire("one")
    vet.save(tmp_path / "s.zip")
    back = Vet.restore(tmp_path / "s.zip")
    back.module("top").add_wire("two")
    r = back.finish()
    assert r.ok, r.report()
    assert r.new_diagnostics == []


# --- review of the tests -----------------------------------------------------------------------


def test_check_keeps_what_is_waiting_for_a_reload(kit):
    vet = Vet(Session(kit.filelist, top="top"))
    vet.module("mid").add_port("dft_en", "input")  # queued, not loaded yet
    assert not vet.check().ok  # top.u_mid does not connect the new input yet
    with pytest.raises(VetError):
        vet.module("mid").add_port("dft_en", "output")  # the name is still known to be taken
    vet.module("top").add_port("dft_en", "input")
    vet.module("top").instance("u_mid").connect("dft_en", "dft_en")
    r = vet.finish()
    assert r.ok, r.report()


def test_check_is_refused_inside_a_group_and_after_finish(kit):
    vet = Vet(Session(kit.filelist, top="top"))
    with pytest.raises(VetError, match="group"):
        with vet.group():
            vet.check()
    vet.finish()
    with pytest.raises(VetError, match="finish"):
        vet.check()


def test_a_restored_session_does_not_take_its_design_for_the_baseline(kit, tmp_path: Path):
    def first(vet):
        vet.module("mid").instance("u_l1").set_param("DEPTH", 4, was=3)

    def second(vet):
        vet.propagate_param("DEPTH", to="top.u_mid.u_l1")

    one = Vet(Session(kit.filelist, top="top"))
    first(one)
    second(one)
    r1 = one.finish()
    assert r1.ok, r1.report()

    vet = Vet(Session(kit.filelist, top="top"))
    first(vet)
    vet.save(tmp_path / "s.zip")
    back = Vet.restore(tmp_path / "s.zip")
    assert back.session.reloads == 1
    second(back)
    r2 = back.finish()
    assert r2.ok, r2.report()
    assert r2.diff == r1.diff

    untouched = Vet(Session(kit.filelist, top="top"))
    untouched.save(tmp_path / "empty.zip")
    assert Vet.restore(tmp_path / "empty.zip").session.reloads == 0


def test_a_later_removal_drops_what_a_saved_operation_registered(kit, tmp_path: Path):
    vet = Vet(Session(kit.filelist, top="top"))
    vet.module("top").instance("u_spare").connect("d", "4'd0", was="spare_d")
    vet.save(tmp_path / "s.zip")
    back = Vet.restore(tmp_path / "s.zip")
    back.module("top").remove_instance("u_spare")
    r = back.finish()
    assert r.ok, r.report()
    assert [(c.kind, c.path) for c in r.changes] == [("instance-removed", "top.u_spare")]


def test_files_that_were_not_editable_stay_so(kit, tmp_path: Path):
    vet = Vet(Session(kit.filelist, top="top", editable=[kit.top, kit.mid]))
    vet.module("top").add_port("dft_en", "input")
    vet.save(tmp_path / "s.zip")
    back = Vet.restore(tmp_path / "s.zip")
    assert back.session.is_editable(kit.mid) and not back.session.is_editable(kit.leaf)
    with pytest.raises((VetError, SessionError), match="editable"):
        back.module("leaf", instances=3).add_port("x", "input")


def test_save_is_refused_inside_the_input_tree(kit):
    vet = Vet(Session(kit.filelist, top="top"))
    with pytest.raises(VetError, match="input tree"):
        vet.save(kit.root / "rtl" / "s.zip")
    with pytest.raises(VetError, match="input tree"):
        vet.save(kit.mid)
    assert kit.mid.read_bytes().startswith(b"module mid")


def test_save_inside_the_output_directory_is_refused(work, capsys):
    w = work
    assert run(*first(w, "a"), "--save", w.tmp / "out" / "s.zip", "-o", w.tmp / "out") == 2
    assert "--save" in capsys.readouterr().err
    assert not (w.tmp / "out").exists()


def test_save_with_dry_run_saves_and_prints_the_diff(work, capsys):
    w = work
    assert run(*first(w, "a"), "--dry-run") == 0
    diff = capsys.readouterr().out
    assert run(*first(w, "a"), "--save", w.tmp / "a.zip", "--dry-run") == 0
    captured = capsys.readouterr()
    assert captured.out == diff and "sign-off so far" not in captured.out + captured.err
    assert zipfile.is_zipfile(w.tmp / "a.zip")


def test_resume_and_save_to_the_same_file(work):
    w = work
    assert run(*first(w, "a", "b", "c"), "-o", w.tmp / "one") == 0
    assert run(*first(w, "a"), "--save", w.tmp / "s.zip") == 0
    assert run("apply", "--resume", w.tmp / "s.zip", "-s", w.script("b"), "--save", w.tmp / "s.zip") == 0
    assert run("apply", "--resume", w.tmp / "s.zip", "-s", w.script("c"), "-o", w.tmp / "two") == 0
    assert tree_bytes(w.tmp / "two") == tree_bytes(w.tmp / "one")


def test_notes_from_before_the_save_are_not_printed_again(work, capsys):
    w = work
    assert run(*first(w, "a", "c0"), "--save", w.tmp / "s.zip") == 0
    assert any(line.startswith("note: remove_instance top.u_spare") for line in capsys.readouterr().out.splitlines())
    assert run("apply", "--resume", w.tmp / "s.zip", "-s", w.script("b"), "-o", w.tmp / "out") == 0
    assert "note:" not in capsys.readouterr().out


def test_an_archive_with_a_path_outside_the_layout_root_is_refused(work):
    w = work
    assert run(*first(w, "a"), "--save", w.tmp / "a.zip") == 0
    crafted = w.tmp / "crafted.zip"
    with zipfile.ZipFile(w.tmp / "a.zip") as src, zipfile.ZipFile(crafted, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "session.json":
                text = data.decode()
                assert "leaf.sv" in text
                data = text.replace("alt_leaf.sv", "../../escaped.sv").encode()
            dst.writestr(item, data)
    with pytest.raises(SessionError):
        Vet.restore(crafted)


# --- format 2: one key for a pair (#57), rules F1 to F4 ------------------------------------------


def with_format(src: Path, dst: Path, value: object) -> Path:
    """A copy of the archive `src` whose index claims the format `value`."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "session.json":
                data = json.dumps({**json.loads(data), "format": value}).encode()
            zout.writestr(item, data)
    return dst


def stored_checks(path: Path) -> dict[tuple[str, ...], str]:
    """The kind of every post-condition in the archive `path`, by its key."""
    with zipfile.ZipFile(path) as z:
        checks = json.loads(z.read("session.json"))["checks"]
    assert len({tuple(c["key"]) for c in checks}) == len(checks)
    return {tuple(c["key"]): c["kind"] for c in checks}


def test_a_new_archive_has_format_two(kit, tmp_path: Path) -> None:
    """F1."""
    Vet(Session(kit.filelist, top="top")).save(tmp_path / "s.zip")
    with zipfile.ZipFile(tmp_path / "s.zip") as z:
        assert json.loads(z.read("session.json"))["format"] == 4


def test_an_archive_of_format_one_is_refused(kit, tmp_path: Path, capsys) -> None:
    """F2. The message is the one for any other format, and it names both versions."""
    vet = Vet(Session(kit.filelist, top="top"))
    vet.module("top").add_wire("t", width=4)
    vet.save(tmp_path / "s.zip")
    old = with_format(tmp_path / "s.zip", tmp_path / "old.zip", 1)
    message = "session archive format 1, but this hdl-vet reads format 4 (an integer)"
    with pytest.raises(SessionError) as refused:
        Vet.restore(old)
    assert message in str(refused.value) and str(old) in str(refused.value)
    capsys.readouterr()
    assert run("apply", "--resume", old, "-o", tmp_path / "out") == 2
    err = capsys.readouterr().err
    assert message in err and "Traceback" not in err
    assert not (tmp_path / "out").exists()


def test_a_pair_stores_one_key_and_the_kind_of_its_last_operation(kit, tmp_path: Path) -> None:
    """F3. The four additions are saved, then the four removals. The keys do not change."""
    keys = {
        "wire": ("wire", "top", "t"),
        "assign": ("assign", "top", "t"),
        "port": ("port", "top", "dft_en"),
        "param": ("param", "mid", "DFT"),
    }
    vet = Vet(Session(kit.filelist, top="top"))
    top, mid = vet.module("top"), vet.module("mid")
    top.add_wire("t", width=4)
    top.add_assign("t", "a & spare_d")
    top.add_port("dft_en", "input")
    mid.add_param("DFT", 0)
    vet.save(tmp_path / "added.zip")
    assert stored_checks(tmp_path / "added.zip") == {key: f"add_{name}" for name, key in keys.items()}

    top.remove_assign("t")
    top.remove_wire("t")
    top.remove_port("dft_en")
    mid.remove_param("DFT")
    vet.save(tmp_path / "removed.zip")
    assert stored_checks(tmp_path / "removed.zip") == {key: f"remove_{name}" for name, key in keys.items()}
    r = Vet.restore(tmp_path / "removed.zip").finish()
    assert r.ok, r.report()
    assert r.files == {}


def test_a_pair_that_a_save_splits_still_cancels(kit, tmp_path: Path) -> None:
    """F4. Each addition is saved, and its removal comes after the resume."""
    vet = Vet(Session(kit.filelist, top="top"))
    vet.module("top").add_wire("t", width=4)
    vet.module("top").add_assign("t", "a & spare_d")
    vet.module("top").add_port("dft_en", "input")
    vet.module("mid").add_param("DFT", 0)
    vet.save(tmp_path / "s.zip")
    back = Vet.restore(tmp_path / "s.zip")
    back.module("top").remove_assign("t")
    back.module("top").remove_wire("t")
    back.module("top").remove_port("dft_en")
    back.module("mid").remove_param("DFT")
    r = back.finish()
    assert r.failed_checks == []
    assert r.unmatched_expects == [], [str(e) for e in r.unmatched_expects]
    assert r.ok, r.report()
    assert r.files == {}


def test_an_item_added_again_after_a_resume_signs_off(kit, tmp_path: Path) -> None:
    """F4 and rule W7. Added and removed, saved, and added again after the resume.

    The saved check is the one of the removal. The addition after the resume must replace
    it, which it can only do when both are stored under one key.
    """
    vet = Vet(Session(kit.filelist, top="top"))
    vet.module("top").add_wire("t", width=4)
    vet.module("top").remove_wire("t")
    vet.module("top").add_port("dft_en", "input")
    vet.module("top").remove_port("dft_en")
    vet.save(tmp_path / "s.zip")
    back = Vet.restore(tmp_path / "s.zip")
    back.module("top").add_wire("t", width=4)
    back.module("top").add_port("dft_en", "input")
    r = back.finish()
    assert r.failed_checks == []
    assert r.unmatched_expects == [], [str(e) for e in r.unmatched_expects]
    assert r.ok, r.report()
    assert b"  logic [3:0] t;\n" in r.files[kit.top] and b"  input  logic dft_en\n" in r.files[kit.top]


def test_a_port_of_the_input_design_removed_again_after_a_resume_signs_off(tmp_path: Path) -> None:
    """F4 and rules W8, W9 and W12. What the `add_port` left is saved, and the run signs off as one removal."""
    root = tmp_path / "in"
    root.mkdir()
    (root / "d.sv").write_bytes(
        b"module child (\n  input  logic a,\n  input  logic spare,\n  output logic q\n);\n  assign q = a;\nendmodule\n"
        b"module top (input logic a, output logic q);\n  child u (.a(a), .spare(1'b0), .q(q));\nendmodule\n"
    )
    (root / "d.f").write_bytes(b"d.sv\n")
    vet = Vet(Session(root / "d.f", top="top"))
    vet.module("child").remove_port("spare")
    vet.module("child").add_port("spare", "input")
    vet.save(tmp_path / "s.zip")
    back = Vet.restore(tmp_path / "s.zip")
    back.module("child").remove_port("spare")
    r = back.finish()
    assert r.failed_checks == []
    assert r.unmatched_expects == [], [str(e) for e in r.unmatched_expects]
    assert r.ok, r.report()
    assert b"spare" not in r.files[root / "d.sv"]
    assert sorted((c.kind, c.path, c.name) for c in r.changes) == [
        ("connection-changed", "top.u", "spare"),
        ("port-removed", "top.u", "spare"),
    ]
