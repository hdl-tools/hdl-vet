"""Phase 5b review fixes (spec: tests/cli/test_save_resume.py; this file refines it).

Found by the adversarial review of the first implementation, mostly with a
differential harness: a sequence run straight through against the same
sequence with a save and restore at every cut. Where a rule below differs from
the phase 5b spec, this file wins.

One run and a split run
    S1  Sign-off evaluates post-conditions on the design that has every
        queued edit applied, also when the final text equals the original
        (older than 5b: an `add_file` whose only user was spliced in and out
        again passed in one run and failed after a resume). `check()` too.
    S2  The command line applies and loads the queue after each script, so
        that one run has the same stage boundaries as a run split between
        scripts: a script sees in `vet.text` what the scripts before it
        queued, and name-conflict messages read the same either way. If the
        state after a script cannot be loaded, the run continues and sign-off
        reports it as before.
        In the Python API a `save` in the middle of a script is such a
        boundary too; that is documented, not hidden.

Reading an archive
    R1  Nothing in an archive can crash hdl-vet: every malformed archive is a
        SessionError naming the file (exit 2 on the command line, no
        traceback). That includes index JSON nested too deeply to parse.
    R2  Types in the index are checked, not coerced: `format` must be the
        integer 1 (not true, not 1.0, not "1"); strings must be strings,
        counts and offsets integers (booleans are not integers), lists lists.
        Unknown fields are ignored.
    R3  Size: before anything is read into memory, the uncompressed sizes the
        zip directory declares are added up, and an archive above
        `hdl_vet.archive.MAX_BYTES` (2 GiB) is refused.
    R4  Names. hdl-vet chooses where an added file goes in the output tree,
        so its name must be writable on every platform: not a Windows device
        name (CON, PRN, AUX, NUL, COM1-9, LPT1-9, with or without an
        extension), no trailing dot or blank. `add_file` refuses such a file
        (VetError) and restore refuses such an added name; two added names
        that differ only in case are refused.
        The design's own paths are whatever the input tree had. On Windows no
        real file can have a device name, so there a stored path with such a
        component below the layout root is a damaged archive. On other
        systems these are ordinary names and are accepted: a tree that a
        normal run handles must also survive a save and a resume. Components
        of the layout root itself are never checked.
        `write_tree` reports a path it cannot express as OutputError, never
        as a traceback.
    R5  Integrity: replaying the stored stages over each `original/` file
        must give its `current/` file (or the original, when it is not
        listed as changed). A mismatch, or a splice outside its file, is a
        damaged archive.

Not overwriting what the user owns
    W1  `vet.save` refuses, besides a path inside the input tree: the file
        list itself and the original location of an added file.
    W2  The command line refuses, before any script is applied (exit 2):
        `--save` naming one of the `-s` scripts; `-o` that contains the
        `--resume` archive (writing the tree would delete it).
    W3  With `--save` and `-o`, the line "saved <S>" is printed also when
        sign-off then fails (exit 1): the archive exists.

Help text
    `--dry-run` says that no output tree is written (an archive named by
    `--save` still is). `--save` says the state is saved whether or not it
    passes sign-off.
"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

import hdl_vet.archive
from hdl_vet.cli import main
from hdl_vet.ops import Vet, VetError
from hdl_vet.output import OutputError, write_tree
from hdl_vet.session import Session, SessionError
from instance_cells import CELLS
from vetkit import make_design, span, tree_bytes


@pytest.fixture
def kit(tmp_path: Path):
    return make_design(tmp_path / "in")


@pytest.fixture
def cells(tmp_path: Path) -> Path:
    d = tmp_path / "cells"
    d.mkdir()
    for name, text in CELLS.items():
        (d / name).write_bytes(text.encode())
    return d


def run(*args: object) -> int:
    return main([str(a) for a in args])


def script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / f"{name}.py"
    path.write_text("def apply(vet):\n" + "".join(f"    {line}\n" for line in body.splitlines()), encoding="utf-8")
    return path


def saved(kit, tmp_path: Path, cells: Path | None = None) -> Path:
    """An archive of a session with one edit (and one added, used cell when `cells` is given)."""
    vet = Vet(Session(kit.filelist, top="top"))
    vet.module("top").add_port("dft_en", "input")
    if cells is not None:
        vet.add_file(cells / "cell_leaf.sv")
        vet.module("mid").instance("u_l0").replace_module(
            "cell_leaf", ports={"clk": "CK", "d": "D", "q": "Q"}, drop_params=["WIDTH"]
        )
    vet.save(tmp_path / "s.zip")
    return tmp_path / "s.zip"


def recraft(src: Path, dst: Path, index=None, entries=None, rename=None) -> Path:
    """Copy an archive, optionally changing the index (a function), entry bytes and entry names."""
    entries, rename = entries or {}, rename or {}
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "session.json" and index is not None:
                data = index(json.loads(data))
                if not isinstance(data, bytes):
                    data = json.dumps(data).encode()
            data = entries.get(item.filename, data)
            zout.writestr(rename.get(item.filename, item.filename), data)
    return dst


# --- S1, S2 ----------------------------------------------------------------------------------


def test_post_conditions_see_the_queue_when_the_text_ends_up_unchanged(kit, cells):
    vet = Vet(Session(kit.filelist, top="top"))
    vet.add_file(cells / "cell_leaf.sv")
    line = b"  cell_leaf u_x (.CK(clk), .D(a), .Q());\n"
    at = vet.text(kit.mid).index(b"endmodule")
    vet.splice(kit.mid, at, at, line)
    assert "u_x" in [i.name for i in vet.design.module("mid").instances]  # loaded with the instance
    start, end = span(vet.text(kit.mid), line)
    vet.splice(kit.mid, start, end, b"")  # queued: the text is the original again
    assert vet.check().failed_checks == ["add_file cell_leaf.sv: none of its modules is instantiated"]
    r = vet.finish()
    assert not r.ok
    assert r.failed_checks == ["add_file cell_leaf.sv: none of its modules is instantiated"]


def test_a_script_sees_what_the_scripts_before_it_queued(kit, tmp_path: Path, capsys):
    one = script(
        tmp_path,
        "one",
        'p = vet.module("top").path\nat = vet.text(p).index(b"0000")\nvet.splice(p, at, at + 4, b"1111")',
    )
    two = script(
        tmp_path,
        "two",
        'p = vet.module("top").path\nat = vet.text(p).index(b"1111")\nvet.splice(p, at, at + 4, b"2222")',
    )
    base = ["apply", "-f", kit.filelist, "--top", "top"]
    assert run(*base, "-s", one, "-s", two, "-o", tmp_path / "straight") == 0
    assert b"sha256: 2222" in (tmp_path / "straight" / "rtl" / "top.sv").read_bytes()
    assert run(*base, "-s", one, "--save", tmp_path / "s.zip") == 0
    assert run("apply", "--resume", tmp_path / "s.zip", "-s", two, "-o", tmp_path / "split") == 0
    assert tree_bytes(tmp_path / "split") == tree_bytes(tmp_path / "straight")
    capsys.readouterr()


def test_a_refusal_reads_the_same_in_one_run_and_in_a_split_run(kit, tmp_path: Path, capsys):
    one = script(tmp_path, "one", 'vet.module("top").add_wire("nw", 4)')
    two = script(tmp_path, "two", 'vet.module("top").add_port("nw", "input")')
    base = ["apply", "-f", kit.filelist, "--top", "top"]
    assert run(*base, "-s", one, "-s", two, "-o", tmp_path / "straight") == 2
    straight = capsys.readouterr().err
    assert "script two.py: add_port top" in straight and "'nw'" in straight
    assert run(*base, "-s", one, "--save", tmp_path / "s.zip") == 0
    capsys.readouterr()
    assert run("apply", "--resume", tmp_path / "s.zip", "-s", two, "-o", tmp_path / "split") == 2
    assert capsys.readouterr().err == straight


def test_a_state_that_cannot_be_checked_between_scripts_is_still_reported_by_sign_off(kit, tmp_path: Path, capsys):
    # The first script leaves a design with a new error; the run goes on and sign-off reports it.
    one = script(
        tmp_path,
        "one",
        'p = vet.module("mid").path\nat = vet.text(p).index(b".d(a)")\nvet.splice(p, at, at + 5, b".d(nope_x)")',
    )
    two = script(tmp_path, "two", 'vet.module("top").add_wire("w")')
    assert run("apply", "-f", kit.filelist, "--top", "top", "-s", one, "-s", two, "-o", tmp_path / "out") == 1
    err = capsys.readouterr().err
    assert "nope_x" in err and "Traceback" not in err
    assert not (tmp_path / "out").exists()


# --- R1 .. R5 --------------------------------------------------------------------------------


def test_deeply_nested_index_json_is_a_clean_refusal(kit, tmp_path: Path, capsys):
    bad = recraft(saved(kit, tmp_path), tmp_path / "deep.zip", index=lambda _: b"[" * 200000)
    with pytest.raises(SessionError, match="deep.zip"):
        Vet.restore(bad)
    assert run("apply", "--resume", bad, "-o", tmp_path / "out") == 2
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("value", [True, 1.0, "1", None, [1]])
def test_the_format_must_be_the_integer_one(kit, tmp_path: Path, value):
    def change(index):
        index["format"] = value
        return index

    with pytest.raises(SessionError):
        Vet.restore(recraft(saved(kit, tmp_path), tmp_path / "f.zip", index=change))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("top", 7),
        ("scripts", "a.py"),
        ("scripts", [["a.py"]]),
        ("scripts", [["a.py", 5]]),
        ("log", [None, 1, []]),
        ("log", {"op": "x"}),
        ("notes", [3]),
        ("notes", "note"),
        ("checks", [{"key": "add_port", "kind": "add_port", "args": ["top", "dft_en", "x"]}]),
        ("checks", [{"key": ["nope"], "kind": "no_such_kind", "args": []}]),
        ("checks", [{"key": ["add_port", "top", "dft_en"], "kind": "add_port", "args": []}]),
        ("files", "rtl/mid.sv"),
        ("maps", []),
        ("expects", [[]]),
        ("added", {}),
    ],
)
def test_wrong_types_in_the_index_are_refused_at_restore(kit, tmp_path: Path, field, value):
    def change(index):
        assert field in index
        index[field] = value
        return index

    with pytest.raises(SessionError, match="t.zip"):
        Vet.restore(recraft(saved(kit, tmp_path), tmp_path / "t.zip", index=change))


def test_unknown_fields_in_the_index_are_ignored(kit, tmp_path: Path):
    def change(index):
        index["from_a_later_version"] = {"anything": [1, 2, 3]}
        return index

    back = Vet.restore(recraft(saved(kit, tmp_path), tmp_path / "u.zip", index=change))
    assert back.finish().ok


def test_an_archive_that_declares_too_much_data_is_refused_before_it_is_read(kit, tmp_path: Path, monkeypatch):
    assert hdl_vet.archive.MAX_BYTES == 2 * 1024**3
    archive = saved(kit, tmp_path)
    monkeypatch.setattr(hdl_vet.archive, "MAX_BYTES", 500)
    with pytest.raises(SessionError, match="s.zip") as e:
        Vet.restore(archive)
    assert "500" in str(e.value)


@pytest.mark.parametrize("name", ["NUL", "nul.sv", "CON", "com1.sv", "LPT9", "cell.", "cell.sv "])
def test_added_names_that_some_platform_cannot_write_are_refused(kit, cells, tmp_path: Path, name):
    def change(index):
        assert [a["name"] for a in index["added"]] == ["cell_leaf.sv"]
        index["added"][0]["name"] = name
        return index

    bad = recraft(
        saved(kit, tmp_path, cells), tmp_path / "n.zip", index=change, rename={"added/cell_leaf.sv": f"added/{name}"}
    )
    with pytest.raises(SessionError, match="n.zip"):
        Vet.restore(bad)


def test_add_file_refuses_a_name_some_platform_cannot_write(kit, tmp_path: Path):
    vet = Vet(Session(kit.filelist, top="top"))
    odd = tmp_path / "odd"
    odd.mkdir()
    names = ["cell.sv ", "aux.sv"]  # legal file names on POSIX, not creatable on Windows
    if os.name != "nt":
        for i, name in enumerate(names):
            (odd / name).write_bytes(f"module odd{i} (input logic clk);\nendmodule\n".encode())
    for name in names:
        with pytest.raises(VetError, match="add_file") as e:
            vet.add_file(odd / name)
        assert "cannot be written on every platform" in str(e.value)
    assert vet.session.added == {}


@pytest.mark.skipif(os.name == "nt", reason="such a tree cannot exist on Windows")
def test_a_tree_with_device_like_names_survives_a_save_and_a_resume(tmp_path: Path):
    kit = make_design(tmp_path / "aux" / "in")  # a component of the layout root
    renamed = kit.alt_leaf.with_name("con.sv")  # and a file below it
    kit.alt_leaf.rename(renamed)
    kit.filelist.write_bytes(kit.filelist.read_bytes().replace(b"alt_leaf.sv", b"con.sv"))
    vet = Vet(Session(kit.filelist, top="top"))
    vet.module("top").add_port("dft_en", "input")
    vet.save(tmp_path / "s.zip")
    back = Vet.restore(tmp_path / "s.zip")
    r = back.finish()
    assert r.ok, r.report()
    out = write_tree(back.session, r, tmp_path / "out")
    assert (out / "rtl" / "con.sv").read_bytes() == renamed.read_bytes()


@pytest.mark.skipif(os.name != "nt", reason="device names are ordinary file names elsewhere")
def test_a_stored_path_with_a_device_name_is_refused(kit, tmp_path: Path):
    def change(index):
        text = json.dumps(index)
        assert "alt_leaf.sv" in text
        return json.loads(text.replace("alt_leaf.sv", "NUL"))

    bad = recraft(
        saved(kit, tmp_path), tmp_path / "d.zip", index=change, rename={"original/rtl/alt_leaf.sv": "original/rtl/NUL"}
    )
    with pytest.raises(SessionError, match="d.zip"):
        Vet.restore(bad)


def test_write_tree_reports_a_path_it_cannot_express(kit, tmp_path: Path, monkeypatch):
    import hdl_vet.output

    vet = Vet(Session(kit.filelist, top="top"))
    vet.module("top").add_port("dft_en", "input")
    r = vet.finish()

    def cannot(path, start):
        raise ValueError("path is on mount 'X:', start on mount 'Y:'")

    monkeypatch.setattr(hdl_vet.output.os.path, "relpath", cannot)
    with pytest.raises(OutputError, match="mount"):
        write_tree(vet.session, r, tmp_path / "out")
    assert not (tmp_path / "out").exists()
    assert [p.name for p in tmp_path.iterdir()] == ["in"]  # no temporary directory left


def test_an_original_that_does_not_lead_to_the_current_text_is_refused(kit, tmp_path: Path):
    archive = saved(kit, tmp_path)
    with zipfile.ZipFile(archive) as z:
        original = z.read("original/rtl/top.sv")
    tampered = original.replace(b"assign spare_d = a;", b"assign spare_d = ~a;")
    assert tampered != original and len(tampered) != len(original)
    with pytest.raises(SessionError, match="s2.zip"):
        Vet.restore(recraft(archive, tmp_path / "s2.zip", entries={"original/rtl/top.sv": tampered}))
    # an unchanged file may not differ either: it has no current/ entry to be checked against
    with zipfile.ZipFile(archive) as z:
        assert "current/rtl/leaf.sv" not in z.namelist()


def test_a_splice_outside_its_file_is_refused(kit, tmp_path: Path):
    def change(index):
        (path,) = [p for p in index["maps"] if p.endswith("top.sv")]
        stage = index["maps"][path][0]
        stage[0][0] = stage[0][1] = 10**9
        return index

    with pytest.raises(SessionError, match="o.zip"):
        Vet.restore(recraft(saved(kit, tmp_path), tmp_path / "o.zip", index=change))


# --- W1 .. W3 --------------------------------------------------------------------------------


def test_save_does_not_overwrite_the_file_list_or_an_added_files_source(tmp_path: Path, cells):
    kit = make_design(tmp_path / "in")
    outside = tmp_path / "lists" / "design.f"
    outside.parent.mkdir()
    outside.write_bytes(b"rtl/leaf.sv\nrtl/alt_leaf.sv\nrtl/mid.sv\nrtl/top.sv\n")
    vet = Vet(Session(outside, top="top", base=kit.root))  # the file list lies outside the layout root
    vet.add_file(cells / "cell_leaf.sv")
    with pytest.raises(VetError, match="file list"):
        vet.save(outside)
    with pytest.raises(VetError, match="cell_leaf.sv"):
        vet.save(cells / "cell_leaf.sv")
    assert outside.read_bytes().startswith(b"rtl/leaf.sv")
    assert (cells / "cell_leaf.sv").read_bytes() == CELLS["cell_leaf.sv"].encode()


def test_save_naming_a_script_is_refused(kit, tmp_path: Path, capsys):
    one = script(tmp_path, "one", 'vet.module("top").add_wire("w")')
    before = one.read_bytes()
    assert run("apply", "-f", kit.filelist, "--top", "top", "-s", one, "--save", one) == 2
    assert "--save" in capsys.readouterr().err
    assert one.read_bytes() == before


def test_an_output_directory_that_holds_the_resumed_archive_is_refused(kit, tmp_path: Path, capsys):
    out = tmp_path / "out"
    out.mkdir()
    archive = out / "s.zip"
    assert run("apply", "-f", kit.filelist, "--top", "top", "--save", archive) == 0
    kept = archive.read_bytes()
    capsys.readouterr()
    assert run("apply", "--resume", archive, "-o", out, "--force") == 2
    assert "--resume" in capsys.readouterr().err
    assert archive.read_bytes() == kept


def test_the_saved_line_is_printed_when_sign_off_then_fails(kit, tmp_path: Path, capsys):
    bad = script(tmp_path, "bad", 'vet.module("mid").instance("u_l1").disconnect("q", was="y1")')
    archive = tmp_path / "s.zip"
    assert run("apply", "-f", kit.filelist, "--top", "top", "-s", bad, "--save", archive, "-o", tmp_path / "out") == 1
    captured = capsys.readouterr()
    assert f"saved {archive}" in captured.out.splitlines()
    assert "left dangling" in captured.err and archive.is_file()


def test_help_wording(capsys):
    with pytest.raises(SystemExit):
        main(["apply", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "no output tree" in text
    assert "whether or not it passes sign-off" in text
