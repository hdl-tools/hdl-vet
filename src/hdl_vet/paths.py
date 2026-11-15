"""Path helpers shared by the command line, the output writer and the project file."""

from __future__ import annotations

from pathlib import Path

__all__ = ["within"]


def within(path: Path, parent: Path) -> bool:
    """True when `path` is `parent` or lies under it (both resolved first)."""
    path, parent = Path(path).resolve(), Path(parent).resolve()
    return path == parent or parent in path.parents
