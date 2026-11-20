"""Review of the script-relative path of `Vet.add_file` (#63).

The review attacked the first implementation of the rules S1 to S6 of
`test_script_paths.py`. It found that the rules were kept, but that the two questions the
implementation asks the filesystem, "is there a file next to the script" and "is there one
in the current folder", were asked with `Path.is_file()`, which answers neither question
when it cannot reach the name, and answers "no" for a name that exists as something other
than a readable file.

`Path.is_file()` hides the error only for a name that is absent, for a name under a file,
for a bad descriptor and for a loop of links. For a folder that cannot be searched, and for
a name too long for the filesystem, it raises. The caller caught nothing, so a refusal that
the project promises came out as a traceback, and a script that worked before the change
stopped working.

Rules
    R1  A name too long for the filesystem is refused, with the operation and the target
        at the start of the message. hdl-vet does not raise a traceback.
    R2  A folder next to the script that cannot be searched does not stop a script that
        names a readable file in the current folder. hdl-vet cannot see what is next to
        the script, so it uses the old route.
    R3  A current folder that cannot be searched is refused, and the message names the
        file. hdl-vet does not take the file next to the script instead, because it cannot
        see whether the current folder holds a different one.
    R4  A name that the current folder holds as something other than a readable file is
        refused when a file of that name is next to the script. A dangling link, a pipe
        and a folder are all names that the user put there. hdl-vet does not choose.
    R5  The refusal names both paths with no `..` and no `.` left in them.

Not checked
    Windows. The errors that R1 to R3 rest on are the ones CPython raises on a POSIX
    filesystem. `Path.is_file()` hides a different set of Windows errors, so which
    Windows failures reach the user as a refusal is not known.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from vetkit import make_design

CELL = b"module extra_cell (input logic clk);\nendmodule\n"
OTHER = b"module other_cell (input logic clk);\nendmodule\n"
ADDS = "def apply(vet):\n    vet.add_file({path!r})\n"

ADDED = "none of its modules is instantiated"  # the sign-off: proof the file was read
NOT_READ = "cannot read it"

posix_only = pytest.mark.skipif(os.name != "posix", reason="the error depends on a POSIX filesystem")
not_root = pytest.mark.skipif(
    os.name == "posix" and os.geteuid() == 0, reason="root searches a folder with no permission bits"
)


@pytest.fixture
def kit(tmp_path: Path):
    k = make_design(tmp_path / "in")
    (tmp_path / "cells").mkdir()
    (tmp_path / "cells" / "extra_cell.sv").write_bytes(CELL)
    (tmp_path / "scripts").mkdir()
    return k


def script(tmp_path: Path, where: str, path: str) -> Path:
    out = tmp_path / where
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(ADDS.format(path=path).encode())
    return out


def run(kit, *scripts: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    args = ["-m", "hdl_vet", "apply", "-f", str(kit.filelist), "--top", "top", "--dry-run"]
    for s in scripts:
        args += ["-s", str(s)]
    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, *args], capture_output=True, text=True, encoding="utf-8", env=env, cwd=cwd, check=False
    )


def shut(folder: Path):
    """`folder` with no permission bits while the test runs."""
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o000)
    return folder


# --- R1: a name the filesystem cannot hold -------------------------------------------


@posix_only
def test_a_name_too_long_is_refused_and_not_raised(kit, tmp_path: Path):
    s = script(tmp_path, "scripts/add.py", "x" * 400 + ".sv")
    r = run(kit, s, cwd=tmp_path)
    assert "Traceback" not in r.stderr, r.stderr
    assert r.stderr.startswith("vet: script add.py: add_file "), r.stderr
    assert NOT_READ in r.stderr


# --- R2 and R3: a folder that cannot be searched -------------------------------------


@posix_only
@not_root
def test_an_unsearchable_folder_beside_the_script_does_not_stop_the_script(kit, tmp_path: Path):
    """The file in the current folder is readable and was added before the change."""
    closed = shut(tmp_path / "scripts" / "cells")
    try:
        s = script(tmp_path, "scripts/add.py", "cells/extra_cell.sv")
        r = run(kit, s, cwd=tmp_path)
        assert "Traceback" not in r.stderr, r.stderr
        assert ADDED in r.stderr, r.stderr
    finally:
        os.chmod(closed, 0o700)


@posix_only
@not_root
def test_an_unsearchable_folder_in_the_current_folder_is_refused(kit, tmp_path: Path):
    closed = shut(tmp_path / "shut" / "cells")
    try:
        (tmp_path / "scripts" / "cells").mkdir()
        (tmp_path / "scripts" / "cells" / "extra_cell.sv").write_bytes(CELL)
        s = script(tmp_path, "scripts/add.py", "cells/extra_cell.sv")
        r = run(kit, s, cwd=tmp_path / "shut")
        assert "Traceback" not in r.stderr, r.stderr
        assert "add_file extra_cell.sv:" in r.stderr, r.stderr
        assert ADDED not in r.stderr
    finally:
        os.chmod(closed, 0o700)


# --- R4: a name the current folder holds as something else ---------------------------


def test_a_dangling_link_in_the_current_folder_is_not_replaced_silently(kit, tmp_path: Path):
    (tmp_path / "scripts" / "cells").mkdir()
    (tmp_path / "scripts" / "cells" / "extra_cell.sv").write_bytes(OTHER)
    (tmp_path / "cells" / "extra_cell.sv").unlink()
    try:
        (tmp_path / "cells" / "extra_cell.sv").symlink_to(tmp_path / "cells" / "gone.sv")
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not let the test make a symlink")
    s = script(tmp_path, "scripts/add.py", "cells/extra_cell.sv")
    r = run(kit, s, cwd=tmp_path)
    assert "add_file extra_cell.sv:" in r.stderr, r.stderr
    assert str(tmp_path / "scripts" / "cells" / "extra_cell.sv") in r.stderr
    assert str(tmp_path / "cells" / "extra_cell.sv") in r.stderr
    assert ADDED not in r.stderr


def test_a_folder_in_the_current_folder_is_not_replaced_silently(kit, tmp_path: Path):
    (tmp_path / "scripts" / "cells").mkdir()
    (tmp_path / "scripts" / "cells" / "extra_cell.sv").write_bytes(OTHER)
    (tmp_path / "cells" / "extra_cell.sv").unlink()
    (tmp_path / "cells" / "extra_cell.sv").mkdir()
    s = script(tmp_path, "scripts/add.py", "cells/extra_cell.sv")
    r = run(kit, s, cwd=tmp_path)
    assert "add_file extra_cell.sv:" in r.stderr, r.stderr
    assert ADDED not in r.stderr


@posix_only
def test_a_pipe_in_the_current_folder_is_not_replaced_silently(kit, tmp_path: Path):
    (tmp_path / "scripts" / "cells").mkdir()
    (tmp_path / "scripts" / "cells" / "extra_cell.sv").write_bytes(OTHER)
    (tmp_path / "cells" / "extra_cell.sv").unlink()
    os.mkfifo(tmp_path / "cells" / "extra_cell.sv")
    s = script(tmp_path, "scripts/add.py", "cells/extra_cell.sv")
    r = run(kit, s, cwd=tmp_path)
    assert "add_file extra_cell.sv:" in r.stderr, r.stderr
    assert ADDED not in r.stderr


# --- R5: the message -----------------------------------------------------------------


def test_the_refusal_names_both_paths_without_dot_parts(kit, tmp_path: Path):
    (tmp_path / "sub").mkdir()  # both routes need the part that `..` takes back
    (tmp_path / "scripts" / "sub").mkdir()
    (tmp_path / "scripts" / "cells").mkdir()
    (tmp_path / "scripts" / "cells" / "extra_cell.sv").write_bytes(OTHER)
    s = script(tmp_path, "scripts/add.py", "sub/../cells/extra_cell.sv")
    r = run(kit, s, cwd=tmp_path)
    assert "add_file extra_cell.sv:" in r.stderr, r.stderr
    assert ".." not in r.stderr, r.stderr
    assert str(tmp_path / "scripts" / "cells" / "extra_cell.sv") in r.stderr
    assert str(tmp_path / "cells" / "extra_cell.sv") in r.stderr
