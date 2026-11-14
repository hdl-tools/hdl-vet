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

_ENV_RE = re.compile(r"\$(?:\{(\w+)\}|\((\w+)\)|(\w+))")


class FilelistError(Exception):
    """Raised with the offending file and line in the message."""


@dataclass(frozen=True)
class Filelist:
    files: list[Path]
    incdirs: list[Path]
    defines: dict[str, str | None]
    root: Path


@dataclass
class _State:
    root: Path
    files: list[Path] = field(default_factory=list)
    incdirs: list[Path] = field(default_factory=list)
    defines: dict[str, str | None] = field(default_factory=dict)


def read_filelist(path: Path) -> Filelist:
    top = Path(path).resolve()
    state = _State(root=top.parent)
    _read(top, top.parent, state, (top,))
    return Filelist(state.files, state.incdirs, state.defines, state.root)


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
    except OSError as e:
        raise FilelistError(f"{fl}: cannot read file list: {e}") from e
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw)
        if not line:
            continue
        where = f"{fl}:{lineno}"
        line = _expand(line, where)
        if line.startswith("+incdir+"):
            for part in line[len("+incdir+") :].split("+"):
                if part:
                    state.incdirs.append((base / part).resolve())
        elif line.startswith("+define+"):
            for part in line[len("+define+") :].split("+"):
                if part:
                    name, eq, value = part.partition("=")
                    state.defines[name] = value if eq else None
        elif line.startswith(("-f ", "-F ", "-f\t", "-F\t")):
            nested_arg = line[2:].strip()
            nested = (base / nested_arg).resolve()
            if nested in stack:
                raise FilelistError(f"{where}: recursive file list include {nested_arg}")
            if not nested.is_file():
                raise FilelistError(f"{where}: file list not found: {nested_arg}")
            nested_base = nested.parent if line[1] == "F" else state.root
            _read(nested, nested_base, state, (*stack, nested))
        elif line[0] in "-+":
            raise FilelistError(f"{where}: unsupported option {line.split()[0]}")
        else:
            src = (base / line).resolve()
            if not src.is_file():
                raise FilelistError(f"{where}: source file not found: {line}")
            if src not in state.files:
                state.files.append(src)
