"""The project files and the helpers that test_project.py and test_project_review.py both use."""

from __future__ import annotations

import json
from pathlib import Path

from hdl_vet.project import PROJECT_FILE

FILES = ["hdl-vet.toml", "scripts/01_example.py", "cells/README.md", "README.md", ".gitignore"]
PORT = 'def apply(vet):\n    vet.module("top").add_port("dft_en", "input")\n'


def write_project(root: Path, scripts: list[str] | None = None, output: str | None = "out", **design: str) -> None:
    design = {"filelist": "../gen/design.f", "top": "top", **design}
    lines = ["[design]", *(f'{k} = "{v}"' for k, v in design.items()), "", "[apply]"]
    if scripts is not None:
        lines.append("scripts = [" + ", ".join(f'"{s}"' for s in scripts) + "]")
    if output is not None:
        lines.append(f'output = "{output}"')
    (root / PROJECT_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")


def manifest_scripts(out: Path) -> list[str]:
    return [s["name"] for s in json.loads((out / "hdl-vet.json").read_bytes())["scripts"]]
