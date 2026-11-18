"""The path guards of `hdl_vet.paths`.

`within()` answers "is this path that place, or under it?". Its callers are all
guards against a destructive action: `output.py` refuses to write the output
tree into the input tree, and `cli.py` refuses a `--save` that would overwrite
the file list, a script or the project file.

The guard has to agree with the filesystem. `Path.resolve()` expands symlinks
and `..` but does not canonicalise case, so on a case-insensitive filesystem
(APFS by default, and NTFS) two spellings of one directory used to compare as
unrelated and the guards let them through. It also cannot tell that two
names are one file through a hard link, on every platform.

These tests state the invariant as "the guard agrees with the filesystem", so
they are meaningful on a case-sensitive filesystem too: there `DESIGN` really
is a second directory, and the answer really is False.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hdl_vet.paths import same_file, within


def ignores_case(where: Path) -> bool:
    """True when the filesystem holding `where` ignores case in names.

    Probed by writing, not by asking the platform: a case-sensitive volume on
    macOS and a case-insensitive one on Linux both exist.
    """
    probe = where / "hdl_vet_case_probe"
    probe.mkdir()
    try:
        return (where / "HDL_VET_CASE_PROBE").is_dir()
    finally:
        probe.rmdir()


def test_a_directory_is_within_itself(tmp_path: Path):
    design = tmp_path / "design"
    design.mkdir()
    assert within(design, design)


def test_a_nested_path_is_within_the_tree(tmp_path: Path):
    design = tmp_path / "design"
    (design / "rtl").mkdir(parents=True)
    assert within(design / "rtl", design)
    assert within(design / "rtl" / "top.sv", design)
    assert not within(design, design / "rtl")


def test_an_unrelated_path_is_not_within_the_tree(tmp_path: Path):
    design = tmp_path / "design"
    other = tmp_path / "other"
    design.mkdir()
    other.mkdir()
    assert not within(other, design)
    assert not within(design, other)


def test_a_path_that_does_not_exist_yet_is_still_placed(tmp_path: Path):
    design = tmp_path / "design"
    design.mkdir()
    assert within(design / "out", design)
    assert within(design / "out" / "deep", design)
    assert not within(tmp_path / "out", design)


def test_the_guard_agrees_with_the_filesystem_about_case(tmp_path: Path):
    """The case that let the output tree be written over the input tree."""
    design = tmp_path / "design"
    (design / "rtl").mkdir(parents=True)
    variant = tmp_path / "DESIGN"
    same_place = ignores_case(tmp_path)
    assert within(variant, design) is same_place
    assert within(design, variant) is same_place
    assert within(variant / "rtl", design) is same_place
    # The output directory of a first run does not exist yet, so the spelling that has to
    # be placed is usually one whose last part is missing. Only its parent can answer.
    assert within(variant / "out", design) is same_place
    assert within(variant / "out" / "deep", design) is same_place


def test_the_guard_sees_through_a_symlink(tmp_path: Path):
    """A regression guard: `resolve()` already expands symlinks."""
    design = tmp_path / "design"
    design.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(design, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not let the test make a symlink")
    assert within(link, design)
    assert within(design, link)
    assert within(link / "out", design)


# --- same_file ----------------------------------------------------------------------


def test_same_file_on_one_path_spelled_two_ways(tmp_path: Path):
    """The case that let `--save` overwrite the file list."""
    listing = tmp_path / "design.f"
    listing.write_text("rtl/top.sv\n")
    variant = tmp_path / "DESIGN.F"
    assert same_file(listing, variant) is ignores_case(tmp_path)
    assert same_file(listing, listing)


def test_same_file_on_two_names_of_one_file(tmp_path: Path):
    """A hard link is one file under two names, and `resolve()` cannot tell."""
    listing = tmp_path / "design.f"
    listing.write_text("rtl/top.sv\n")
    link = tmp_path / "also.f"
    try:
        os.link(listing, link)
    except (OSError, AttributeError, NotImplementedError):
        pytest.skip("this platform does not let the test make a hard link")
    assert same_file(listing, link)


def test_same_file_on_two_different_files(tmp_path: Path):
    a = tmp_path / "a.f"
    b = tmp_path / "b.f"
    a.write_text("a\n")
    b.write_text("b\n")
    assert not same_file(a, b)


def test_same_file_when_one_side_is_missing(tmp_path: Path):
    """`--save` usually names a file that does not exist yet."""
    listing = tmp_path / "design.f"
    listing.write_text("rtl/top.sv\n")
    assert not same_file(listing, tmp_path / "new.yaml")
    assert not same_file(tmp_path / "gone.f", tmp_path / "also-gone.f")


def test_same_file_on_one_missing_path_named_twice(tmp_path: Path):
    """The filesystem has no identity to give for a file that is not there yet, so the
    spelling is what is left to go on: `--save` may name a script that a run will write."""
    missing = tmp_path / "later.yaml"
    assert same_file(missing, missing)
    assert same_file(missing, tmp_path / "sub" / ".." / "later.yaml")
