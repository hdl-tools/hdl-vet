"""Where the repository is: the folder that holds `pyproject.toml`, found by walking up from here.

One definition for every test file and helper, so that moving a file into another folder does not
make a `parent.parent` point somewhere else.
"""

from __future__ import annotations

from pathlib import Path

REPO = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").is_file())
