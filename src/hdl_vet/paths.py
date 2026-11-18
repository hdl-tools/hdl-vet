"""Path helpers shared by the command line, the output writer and the project file."""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["same_file", "within"]


def same_file(a: Path, b: Path) -> bool:
    """True when `a` and `b` are one file or one directory on disk.

    `Path.resolve()` expands symlinks and `..`, but it does not canonicalise case, so on
    a case-insensitive filesystem two spellings of one name compare as different. It also
    cannot tell that two names are one file through a hard link. The filesystem's own
    identity for the two answers both, so it is asked whenever the spellings differ.
    """
    a, b = Path(a), Path(b)
    if a.resolve() == b.resolve():
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:  # one of them is missing, or its directory cannot be read
        return False


def within(path: Path, parent: Path) -> bool:
    """True when `path` is `parent` or lies under it (both resolved first).

    Spellings that differ are put to the filesystem with `same_file`, so a path that
    names the tree in another case, or through a hard link, is still placed inside it.
    """
    path, parent = Path(path).resolve(), Path(parent).resolve()
    if path == parent or parent in path.parents:
        return True
    # A spelling that `resolve()` cannot canonicalise still has to be placed, because the
    # callers are guards against replacing or deleting the user's files. Asking about the
    # parts of `path` that exist is enough: `parent` cannot hold a path without holding
    # that path's nearest existing ancestor.
    return any(same_file(step, parent) for step in (path, *path.parents))
