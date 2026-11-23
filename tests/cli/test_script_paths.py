"""A relative path that a script gives to an operation (#63).

The starter project and the example scripts spell an added file with
`Path(__file__).resolve().parent`, because a relative path used to start from the folder
where `vet apply` runs. A script that names a file next to itself had no short way to say
so. These rules give it one, and keep every script that exists working.

Rules
    S1  A relative path that a script gives to `Vet.add_file` starts from the folder of
        that script.
    S2  A relative path that names no file next to the script starts from the current
        folder, as before. A script written for the old rule keeps working.
    S3  A relative path that names one file next to the script and another file under the
        current folder is refused. The message names both. hdl-vet does not choose.
        Two spellings of one file are not two files, so they are not refused.
    S4  An absolute path is unaffected. `Path(__file__).resolve().parent` stays correct.
    S5  Outside a script, a relative path starts from the current folder. The Python API
        has no script folder to start from.
    S6  Each script of a run starts from its own folder.

Not checked
    The rules are stated for `Vet.add_file`, the only operation that takes a path. An
    operation added later has to opt in.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hdl_vet.ops import Vet
from hdl_vet.session import Session
from vetkit import keeps_the_asked_spelling, make_design

CELL = b"module extra_cell (input logic clk);\nendmodule\n"

# Reads the file list of the design, so the run gets as far as the sign-off.
ADDS = "def apply(vet):\n    vet.add_file({path!r})\n"

# The sign-off fails on an added file that nothing instantiates. The failure is the proof
# that hdl-vet read the file: a path it cannot read is refused before the sign-off.
ADDED = "none of its modules is instantiated"
NOT_READ = "cannot read it"


@pytest.fixture
def kit(tmp_path: Path):
    """The design under `in/`, a cell under `cells/`, and `scripts/` for the scripts."""
    k = make_design(tmp_path / "in")
    (tmp_path / "cells").mkdir()
    (tmp_path / "cells" / "extra_cell.sv").write_bytes(CELL)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "deep" / "down").mkdir(parents=True)  # a folder that reaches neither cell
    return k


def script(tmp_path: Path, where: str, path: str) -> Path:
    out = tmp_path / where
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(ADDS.format(path=path).encode())
    return out


def run(kit, tmp_path: Path, *scripts: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    args = ["-m", "hdl_vet", "apply", "-f", str(kit.filelist), "--top", "top", "--dry-run"]
    for s in scripts:
        args += ["-s", str(s)]
    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, *args], capture_output=True, text=True, encoding="utf-8", env=env, cwd=cwd, check=False
    )


# --- S1: the script's folder ---------------------------------------------------------


def test_a_relative_path_starts_from_the_folder_of_the_script(kit, tmp_path: Path):
    s = script(tmp_path, "scripts/add.py", "../cells/extra_cell.sv")
    r = run(kit, tmp_path, s, cwd=tmp_path / "deep" / "down")
    assert ADDED in r.stderr, r.stderr
    assert NOT_READ not in r.stderr


def test_a_plain_name_starts_from_the_folder_of_the_script(kit, tmp_path: Path):
    (tmp_path / "scripts" / "beside.sv").write_bytes(CELL)
    s = script(tmp_path, "scripts/add.py", "beside.sv")
    r = run(kit, tmp_path, s, cwd=tmp_path / "deep" / "down")
    assert ADDED in r.stderr, r.stderr


# --- S2: the old rule keeps working --------------------------------------------------


def test_a_path_with_no_file_next_to_the_script_starts_from_the_current_folder(kit, tmp_path: Path):
    s = script(tmp_path, "deep/add.py", "cells/extra_cell.sv")  # nothing under deep/
    r = run(kit, tmp_path, s, cwd=tmp_path)
    assert ADDED in r.stderr, r.stderr


def test_a_path_under_neither_folder_is_refused_for_the_current_folder(kit, tmp_path: Path):
    s = script(tmp_path, "scripts/add.py", "nowhere/extra_cell.sv")
    r = run(kit, tmp_path, s, cwd=tmp_path)
    assert NOT_READ in r.stderr, r.stderr
    # Not the whole path: it reaches the message through the repr of an OSError, which
    # doubles a Windows separator.
    assert "nowhere" in r.stderr


# --- S3: two files, no choice --------------------------------------------------------


def test_a_path_that_names_a_file_under_both_folders_is_refused(kit, tmp_path: Path):
    (tmp_path / "scripts" / "cells").mkdir()
    (tmp_path / "scripts" / "cells" / "extra_cell.sv").write_bytes(b"module other_cell (input logic clk);\nendmodule\n")
    s = script(tmp_path, "scripts/add.py", "cells/extra_cell.sv")
    r = run(kit, tmp_path, s, cwd=tmp_path)
    assert "add_file extra_cell.sv:" in r.stderr, r.stderr
    assert str(tmp_path / "scripts" / "cells" / "extra_cell.sv") in r.stderr
    assert str(tmp_path / "cells" / "extra_cell.sv") in r.stderr
    assert ADDED not in r.stderr


def test_one_file_under_both_folders_is_not_refused(kit, tmp_path: Path):
    """The script sits in the folder the run starts from, so both paths are one file."""
    s = script(tmp_path, "add.py", "cells/extra_cell.sv")
    r = run(kit, tmp_path, s, cwd=tmp_path)
    assert ADDED in r.stderr, r.stderr


def test_one_file_under_two_spellings_is_not_refused(kit, tmp_path: Path):
    """The two sides reach one file under spellings that `resolve()` cannot make equal.

    A link beside the script names the folder of the current one in another case. On a
    filesystem that ignores case the two are one file, and only the filesystem can say so.
    The assertion on the two resolved paths keeps this test from going vacuous: were they
    equal, `same_file` would answer on its first line and the case would not be covered.
    """
    if not keeps_the_asked_spelling(tmp_path):
        pytest.skip("`resolve()` canonicalises the case here, so the two sides are one string")
    try:
        (tmp_path / "scripts" / "cells").symlink_to(tmp_path / "CELLS", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not let the test make a symlink")
    beside, here = tmp_path / "scripts" / "cells" / "extra_cell.sv", tmp_path / "cells" / "extra_cell.sv"
    assert beside.resolve() != here.resolve(), "the two spellings must not resolve alike"
    s = script(tmp_path, "scripts/add.py", "cells/extra_cell.sv")
    r = run(kit, tmp_path, s, cwd=tmp_path)
    assert ADDED in r.stderr, r.stderr


# --- S4: an absolute path ------------------------------------------------------------


def test_an_absolute_path_is_unaffected(kit, tmp_path: Path):
    s = script(tmp_path, "scripts/add.py", str(tmp_path / "cells" / "extra_cell.sv"))
    r = run(kit, tmp_path, s, cwd=tmp_path / "deep" / "down")
    assert ADDED in r.stderr, r.stderr


def test_the_form_of_the_example_scripts_is_unaffected(kit, tmp_path: Path):
    body = (
        "from pathlib import Path\n\n\n"
        "def apply(vet):\n"
        '    vet.add_file(Path(__file__).resolve().parent.parent / "cells" / "extra_cell.sv")\n'
    )
    s = tmp_path / "scripts" / "add.py"
    s.write_bytes(body.encode())
    r = run(kit, tmp_path, s, cwd=tmp_path / "deep" / "down")
    assert ADDED in r.stderr, r.stderr


# --- S5: no script -------------------------------------------------------------------


def test_the_python_api_starts_from_the_current_folder(kit, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    v = Vet(Session(kit.filelist, top="top"))
    v.add_file("cells/extra_cell.sv")
    assert list(v.session.added) == ["hdl_vet_added/extra_cell.sv"]


def test_the_python_api_refuses_a_path_the_current_folder_does_not_hold(kit, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path / "deep" / "down")
    v = Vet(Session(kit.filelist, top="top"))
    with pytest.raises(Exception, match=NOT_READ):
        v.add_file("cells/extra_cell.sv")


# --- S6: one folder for each script --------------------------------------------------


def test_each_script_starts_from_its_own_folder(kit, tmp_path: Path):
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    (tmp_path / "one" / "a_cell.sv").write_bytes(b"module a_cell (input logic clk);\nendmodule\n")
    (tmp_path / "two" / "b_cell.sv").write_bytes(b"module b_cell (input logic clk);\nendmodule\n")
    first = script(tmp_path, "one/add.py", "a_cell.sv")
    second = script(tmp_path, "two/add.py", "b_cell.sv")
    r = run(kit, tmp_path, first, second, cwd=tmp_path / "deep" / "down")
    assert NOT_READ not in r.stderr, r.stderr
    assert "a_cell.sv" in r.stderr
    assert "b_cell.sv" in r.stderr
