"""How the equivalence engine makes host files visible to the sandboxed Yosys.

The Yosys of the extra `equiv` runs as WebAssembly, in a sandbox that sees only what it
is given. `_Seen` decides what that is: the **first part** of a path becomes a mount at
`/hostfs_<name>`, because the runtime refuses a deeper mountpoint. So a whole top-level
directory is given read access, not just the directory of a source.

That is the engine's one real platform difference, and it had no test. On Linux the work
directory is `/tmp/...`, so the part is `tmp`. On macOS `$TMPDIR` is under `/var`, which
is a symbolic link to `private/var`, so the part is `private` and a run usually needs two
mounts: one for the work directory and one for the design.

Nothing here needs Yosys. `hdl_vet.equiv` imports none of it at module import time, and
`_Seen` only reads the file system, so these tests carry no `equiv` marker.

On Windows the runtime mounts every drive under its own name, so `_Seen` hands a path
back unchanged and there is nothing to test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hdl_vet import equiv

posix_only = pytest.mark.skipif(os.name == "nt", reason="the drives are already mounted on Windows")


@posix_only
def test_a_path_is_given_under_its_first_part(tmp_path: Path):
    seen = equiv._Seen()
    text = seen(tmp_path / "gold.f")
    top = tmp_path.resolve().parts[1]
    assert text.startswith(f"/hostfs_{top}/")
    assert text.endswith("/gold.f")
    assert seen.tops == {top}


@posix_only
def test_the_parts_needed_are_collected(tmp_path: Path):
    """A run that spans two top-level directories asks for two mounts, which is the usual
    case on macOS: the work directory is under one and the design under another."""
    seen = equiv._Seen()
    seen(tmp_path / "work" / "gate.f")
    seen(Path("/usr") / "share" / "x.sv")
    assert seen.tops == {tmp_path.resolve().parts[1], "usr"}


@posix_only
def test_a_whole_top_level_directory_is_what_is_given(tmp_path: Path):
    """Documented in the manual's chapter on limits, because it is more than the design."""
    seen = equiv._Seen()
    seen(tmp_path / "rtl" / "top.sv")
    (top,) = seen.tops
    assert "/" not in top
    assert Path("/" + top).is_dir()


@posix_only
def test_a_path_outside_any_top_level_directory_is_refused():
    seen = equiv._Seen()
    for bad in ("/", "/x"):
        with pytest.raises(equiv._Refused, match="top-level"):
            seen(bad)
    assert seen.tops == set()


@posix_only
def test_a_path_under_a_part_that_cannot_be_given_is_refused():
    seen = equiv._Seen()
    with pytest.raises(equiv._Refused, match="sandbox cannot be given"):
        seen("/hdl_vet_no_such_top_level_directory/rtl/top.sv")
    assert seen.tops == set()
