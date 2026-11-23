"""Reader for simulator-style `.f` file lists.

Supported: source paths, `+incdir+a+b`, `+define+A=1+B`, `-f` (nested, relative
to the top-level `.f` directory), `-F` (nested, relative to its own directory),
`//` and `#` comments, `$VAR` / `${VAR}` / `$(VAR)` expansion. Anything else that
looks like an option is an error rather than being silently dropped.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Filelist", "FilelistError", "read_filelist"]

from hdl_vet.paths import Spelling, same_file

_ENV_RE = re.compile(r"\$(?:\{(\w+)\}|\((\w+)\)|(\w+))")


class FilelistError(Exception):
    """Raised with the offending file and line in the message."""


@dataclass(frozen=True)
class Filelist:
    files: list[Path]
    incdirs: list[Path]
    defines: dict[str, str | None]
    root: Path
    lists: list[Path] = field(default_factory=list)  # every file list read, the top-level one first


@dataclass
class _State:
    root: Path
    lists: list[Path] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)
    incdirs: list[Path] = field(default_factory=list)
    defines: dict[str, str | None] = field(default_factory=dict)
    # One for the whole read, so each directory on the way down is listed once.
    spelling: Spelling = field(default_factory=Spelling)


def _add_once(where: list[Path], path: Path) -> None:
    """Append `path` unless the same file or directory is already there.

    A list that names one thing twice gives it once. The spellings need not match: on a
    file system that ignores case two of them are one file, and keeping both put the same
    source in the design twice, which slang then reported as a duplicate definition.

    Only a spelling that could collide is put to the file system, so the usual list costs
    no syscall beyond the ones `resolve()` already made.
    """
    if path in where:
        return
    folded = str(path).casefold()
    if any(folded == str(other).casefold() and same_file(path, other) for other in where):
        return
    where.append(path)


def read_filelist(path: Path, base: Path | None = None) -> Filelist:
    """Read `path`; relative entries resolve against `base` (default: its directory)."""
    # The root is spelled as the file system holds it for the same reason the sources are:
    # everything downstream takes a path relative to it, and a root in another case than
    # the sources cannot be taken away from them at all.
    spelling = Spelling()
    top = spelling(Path(path).resolve())
    root = top.parent if base is None else spelling(Path(base).resolve())
    state = _State(root=root, spelling=spelling)
    _read(top, root, state, (top,))
    return Filelist(state.files, state.incdirs, state.defines, state.root, state.lists)


def _expand(text: str, where: str) -> str:
    def sub(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2) or m.group(3)
        value = os.environ.get(name)
        if value is None:
            raise FilelistError(f"{where}: environment variable {name} is not set")
        return value

    return _ENV_RE.sub(sub, text)


def _strip_comment(line: str) -> str:
    line = line.strip()
    if line.startswith("#"):
        return ""
    cut = line.find("//")
    return (line if cut < 0 else line[:cut]).strip()


def _read(fl: Path, base: Path, state: _State, stack: tuple[Path, ...]) -> None:
    try:
        text = fl.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise FilelistError(f"{fl}: cannot read file list: {e}") from e
    state.lists.append(fl)
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw)
        if not line:
            continue
        where = f"{fl}:{lineno}"
        line = _expand(line, where)
        if line.startswith("+incdir+"):
            for part in line[len("+incdir+") :].split("+"):
                if part:
                    _add_once(state.incdirs, state.spelling((base / part).resolve()))
        elif line.startswith("+define+"):
            for part in line[len("+define+") :].split("+"):
                if part:
                    name, eq, value = part.partition("=")
                    state.defines[name] = value if eq else None
        elif line.startswith(("-f ", "-F ", "-f\t", "-F\t")):
            nested_arg = line[2:].strip()
            nested = state.spelling((base / nested_arg).resolve())
            if nested in stack:
                raise FilelistError(f"{where}: recursive file list include {nested_arg}")
            if not nested.is_file():
                raise FilelistError(f"{where}: file list not found: {nested_arg}")
            nested_base = nested.parent if line[1] == "F" else state.root
            _read(nested, nested_base, state, (*stack, nested))
        elif line[0] in "-+":
            raise FilelistError(f"{where}: unsupported option {line.split()[0]}")
        else:
            src = state.spelling((base / line).resolve())
            if not src.is_file():
                raise FilelistError(f"{where}: source file not found: {line}")
            _add_once(state.files, src)
