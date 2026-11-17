"""Check that the version is the same in the three places, and that a tag names it.

Usage: python .github/scripts/check_version.py [TAG]

Exit status 0: the three versions are equal and, if TAG is given, TAG is "v" and the version.
Exit status 1: one line on standard output says what differs.

The script needs only the standard library and does not import hdl_vet,
because it runs before anything is installed. It needs Python 3.11 or newer (tomllib).
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PYPROJECT = "pyproject.toml"
INIT = "src/hdl_vet/__init__.py"
TEX = "docs/manual/version.tex"


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8-sig")


def _find(relative: str, pattern: str) -> str | None:
    """The version that the file names, or None unless it names exactly one, at the start of a line."""
    found = re.findall(pattern, _read(relative), re.M)
    return found[0] if len(found) == 1 else None


def versions() -> dict[str, str | None]:
    """The version that each of the three files names, or None if the file does not name one."""
    project = tomllib.loads(_read(PYPROJECT)).get("project", {})
    return {
        PYPROJECT: project["version"] if isinstance(project.get("version"), str) else None,
        INIT: _find(INIT, r"^__version__\s*=\s*[\"']([^\"']+)[\"']"),
        TEX: _find(TEX, r"^\\newcommand\{\\hdlvetversion\}\{([^}]*)\}"),
    }


def check(tag: str | None) -> str | None:
    """Return the text of the problem, or None if all is well."""
    found = versions()
    for name, value in found.items():
        if value is None:
            return f"Not exactly one version in {name}."
    if len(set(found.values())) != 1:
        return "The versions differ: " + ", ".join(f"{name} has {value}" for name, value in found.items()) + "."
    version = str(found[PYPROJECT])
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version):
        return f"The version {version!r} is not of the form X.Y.Z."
    if tag is not None and tag != "v" + version:
        return f"The tag {tag!r} is not 'v{version}'. The version in the files is {version}."
    return None


def main(argv: list[str]) -> int:
    tag = argv[1] if len(argv) > 1 else None
    try:
        problem = check(tag)
    except (OSError, ValueError) as error:
        problem = f"Cannot read the version: {type(error).__name__}: {error}".replace("\n", " ")
    if problem:
        print(problem)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
