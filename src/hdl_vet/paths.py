"""Path helpers shared by the command line, the output writer and the project file."""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["Spelling", "same_file", "true_case", "within"]


def true_case(path: Path) -> Path:
    """`path` with its last part spelled as the filesystem holds it.

    `Path.resolve()` canonicalises case on Windows but not on macOS, where a name that
    differs only in case opens the same file under a spelling that is not the one on
    disk. Two spellings of one include would then be two paths. The directory is asked
    for the name it really holds, and only when the given spelling is not among them, so
    on a case-sensitive filesystem nothing changes.
    """
    path = Path(path)
    try:
        names = {entry.name for entry in os.scandir(path.parent)}
    except OSError:  # the directory is gone or cannot be read; the spelling is all there is
        return path
    if path.name in names:
        return path
    folded = path.name.casefold()
    # sorted(): if a case-sensitive directory holds several matches, one of them wins the
    # same way every run, so a design does not load differently from one run to the next.
    return next((path.with_name(n) for n in sorted(names) if n.casefold() == folded), path)


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


class Spelling:
    """Spells whole paths as the file system holds them, remembering what it has listed.

    `true_case` answers for the last part of a path only, so a directory named in another
    case survives it. This walks every part. The cost is the question: asking for each
    part of each path lists one directory again for every file in it, which measured five
    times the cost of reading a file list of 500 sources. One instance shared over a whole
    read lists each directory once instead, and measured under a tenth of that.
    """

    def __init__(self) -> None:
        self._held: dict[Path, frozenset[str]] = {}
        self._known: dict[Path, Path] = {}

    def _names(self, directory: Path) -> frozenset[str]:
        names = self._held.get(directory)
        if names is None:
            try:
                names = frozenset(entry.name for entry in os.scandir(directory))
            except OSError:  # gone, or not readable: the spelling given is all there is
                names = frozenset()
            self._held[directory] = names
        return names

    def __call__(self, path: Path) -> Path:
        path = Path(path)
        known = self._known.get(path)
        if known is not None:
            return known
        parent = path.parent
        if parent == path:  # a root; nothing above it to ask
            out = path
        else:
            real_parent = self(parent)
            held = self._names(real_parent)
            name = path.name
            if name not in held:
                folded = name.casefold()
                # sorted(): a case-sensitive directory holding several matches picks the
                # same one every run, so a design does not load differently each time.
                name = next((n for n in sorted(held) if n.casefold() == folded), name)
            out = real_parent / name
        self._known[path] = out
        return out
