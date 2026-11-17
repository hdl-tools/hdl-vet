"""`--save` must not overwrite a file the design is read from. Rules and tests.

Found while writing the manual: `vet apply -f design.f --save lists/common.f`,
where `design.f` reads `lists/common.f` with `-f` and that list lies outside the
input tree, replaced the list with the archive and exited with status 0. The
check compared the target with the top-level file list only.

V1  `--save FILE` is refused when FILE is a file list the design reads: the
    top-level one or one it reads with `-f` or `-F`, at any depth, wherever it
    lies. Exit status 2, one line on stderr that names the file, says it is a
    file list and that it would be overwritten. The file is unchanged, nothing
    is saved, and no script's `apply` runs. (Sources and include files are
    inside the input tree, which is refused already.)
V2  The file is recognised whatever the spelling of the path (`..`, relative or
    absolute).
V3  `Vet.save(path)` refuses the same files with a VetError, so a caller that
    does not go through the command line is protected too.
V4  A file list that is not valid UTF-8, top-level or nested, is an ordinary
    error: `vet: ...` on one line that names the file, exit status 2, no
    traceback. (After the overwrite above, the next run crashed on the archive.)
V5  Saving next to a nested file list, under another name, works as before.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.cli import main
from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session
from vetkit import Kit, make_design

SCRIPT = 'def apply(vet):\n    print("apply ran")\n    vet.module("top").add_wire("w")\n'


def run(*args: object) -> int:
    return main([str(a) for a in args])


class Lists:
    """The vetkit design with its sources named by nested file lists outside the input tree.

    design.f  ->  -f <lists>/common.f  ->  -f <lists>/deep/leafs.f
    """

    def __init__(self, tmp: Path) -> None:
        self.kit: Kit = make_design(tmp / "in")
        self.common = tmp / "lists" / "common.f"
        self.leafs = tmp / "lists" / "deep" / "leafs.f"
        self.leafs.parent.mkdir(parents=True)
        self.leafs.write_bytes(b"rtl/leaf.sv\nrtl/alt_leaf.sv\n")
        self.common.write_bytes(f"-f {self.leafs.as_posix()}\nrtl/mid.sv\n".encode())
        self.kit.filelist.write_bytes(f"-f {self.common.as_posix()}\nrtl/top.sv\n".encode())
        self.script = tmp / "one.py"
        self.script.write_text(SCRIPT, encoding="utf-8")

    def apply(self, *more: object) -> int:
        return run("apply", "--no-project", "-f", self.kit.filelist, "--top", "top", "-s", self.script, *more)


@pytest.fixture
def lists(tmp_path: Path) -> Lists:
    return Lists(tmp_path)


def test_the_design_with_nested_lists_loads(lists, tmp_path, capsys):
    assert lists.apply("--dry-run") == 0
    assert "1 applied, 0 skipped" in "".join(capsys.readouterr())


@pytest.mark.parametrize("which", ["top", "common", "leafs"])
def test_save_onto_a_file_list_is_refused(lists, capsys, which):
    target = {"top": lists.kit.filelist, "common": lists.common, "leafs": lists.leafs}[which]
    before = target.read_bytes()
    assert lists.apply("--save", target) == 2
    captured = capsys.readouterr()
    assert target.read_bytes() == before
    assert captured.out == ""
    assert "apply ran" not in captured.out + captured.err
    lines = captured.err.splitlines()
    assert len(lines) == 1, captured.err
    assert lines[0].startswith("vet: --save ")
    assert target.name in lines[0]
    assert "file list" in lines[0]
    assert "it would be overwritten" in lines[0]


def test_another_spelling_of_the_path_is_recognised(lists, tmp_path, capsys, monkeypatch):
    before = lists.common.read_bytes()
    assert lists.apply("--save", tmp_path / "lists" / "deep" / ".." / "common.f") == 2
    monkeypatch.chdir(tmp_path / "lists" / "deep")
    assert lists.apply("--save", Path("..") / "common.f") == 2
    assert lists.apply("--save", "leafs.f") == 2
    assert lists.common.read_bytes() == before
    assert "it would be overwritten" in capsys.readouterr().err


def test_save_onto_a_file_list_is_refused_with_an_output_directory_too(lists, tmp_path, capsys):
    before = lists.common.read_bytes()
    out = tmp_path / "out"
    assert lists.apply("--save", lists.common, "-o", out) == 2
    assert lists.common.read_bytes() == before
    assert not out.exists()
    assert "it would be overwritten" in capsys.readouterr().err


def test_saving_beside_a_nested_file_list_works(lists, tmp_path, capsys):
    archive = lists.common.with_name("session.zip")
    assert lists.apply("--save", archive) == 0
    assert archive.read_bytes()[:2] == b"PK"
    assert "saved " in "".join(capsys.readouterr())
    again = Vet.restore(archive)
    assert again.finish().ok


@pytest.mark.parametrize("which", ["top", "common", "leafs"])
def test_vet_save_refuses_a_file_list(lists, which):
    target = {"top": lists.kit.filelist, "common": lists.common, "leafs": lists.leafs}[which]
    before = target.read_bytes()
    vet = Vet(Session(lists.kit.filelist, top="top"))
    vet.module("top").add_wire("w")
    with pytest.raises(VetError, match="file list|inside the input tree"):
        vet.save(target)
    assert target.read_bytes() == before
    assert vet.finish().ok  # the session is still usable


@pytest.mark.parametrize("which", ["top", "common", "leafs"])
def test_a_file_list_that_is_not_text_is_an_ordinary_error(lists, capsys, which):
    target = {"top": lists.kit.filelist, "common": lists.common, "leafs": lists.leafs}[which]
    target.write_bytes(b"PK\x03\x04\xfe\xff\x00 not text\n")
    assert lists.apply("--dry-run") == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    lines = captured.err.splitlines()
    assert len(lines) == 1, captured.err
    assert lines[0].startswith("vet: ")
    assert target.name in lines[0]
    assert "apply ran" not in captured.out


def test_the_session_api_reports_a_file_list_that_is_not_text(lists):
    from hdl_vet.filelist import FilelistError

    lists.common.write_bytes(b"\xfe\xff")
    with pytest.raises(FilelistError, match=r"common\.f"):
        Session(lists.kit.filelist, top="top")


# --- V6: a resumed session ----------------------------------------------------------------------
#
# V6  The archive records the file lists the design was read from, so a session
#     resumed with `--resume` refuses to save onto them as V1 says, also when the
#     input tree is no longer there. An archive written before this change has no
#     such record: it still loads, and protects the top-level file list only.


@pytest.mark.parametrize("which", ["top", "common", "leafs"])
def test_a_resumed_session_refuses_to_save_onto_a_file_list(lists, tmp_path, capsys, which):
    target = {"top": lists.kit.filelist, "common": lists.common, "leafs": lists.leafs}[which]
    archive = tmp_path / "s.zip"
    assert lists.apply("--save", archive) == 0
    capsys.readouterr()
    before = target.read_bytes()
    assert run("apply", "--no-project", "--resume", archive, "--save", target) == 2
    captured = capsys.readouterr()
    assert target.read_bytes() == before
    lines = captured.err.splitlines()
    assert len(lines) == 1, captured.err
    assert target.name in lines[0]
    assert "it would be overwritten" in lines[0]


def test_a_restored_vet_refuses_to_save_onto_a_file_list(lists, tmp_path):
    archive = tmp_path / "s.zip"
    vet = Vet(Session(lists.kit.filelist, top="top"))
    vet.save(archive)
    again = Vet.restore(archive)
    before = lists.leafs.read_bytes()
    with pytest.raises(VetError, match="file list"):
        again.save(lists.leafs)
    assert lists.leafs.read_bytes() == before
    again.save(tmp_path / "t.zip")
    assert Vet.restore(tmp_path / "t.zip").finish().ok


def test_saving_twice_gives_the_same_archive(lists, tmp_path):
    """The record of the file lists does not make the archive depend on anything but the session."""
    one, two = tmp_path / "one.zip", tmp_path / "two.zip"
    vet = Vet(Session(lists.kit.filelist, top="top"))
    vet.save(one)
    vet.save(two)
    assert one.read_bytes() == two.read_bytes()
