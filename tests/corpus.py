"""Test helper: generate real hdl-bag designs on demand and cache them.

Not part of the package. See the docstring of test_corpus.py for the interface.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

STYLES = ("flat", "sv-interface")
_SAMPLE_ID = "demo_ss"


@dataclass(frozen=True)
class Built:
    outdir: Path
    filelist: Path
    top: str
    hashes: dict[str, str]


def _bag_exe() -> str | None:
    found = shutil.which("bag")
    if found:
        return found
    bindir = Path(sys.executable).parent
    for name in ("bag.exe", "bag"):
        if (bindir / name).is_file():
            return str(bindir / name)
    return None


def bag_available() -> bool:
    return _bag_exe() is not None


def _run_bag(*args: str) -> str:
    exe = _bag_exe()
    if exe is None:
        raise RuntimeError("`bag` CLI not found")
    proc = subprocess.run([exe, *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"bag {' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


def _bag_version() -> str:
    return _run_bag("--version").strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _checkout() -> Path | None:
    env = os.environ.get("HDL_BAG_REPO")
    repo = Path(env) if env else Path(__file__).resolve().parent.parent.parent / "hdl-bag"
    return repo if (repo / "tests" / "golden" / "demo_ss.ir.yaml").is_file() else None


@dataclass(frozen=True)
class CorpusDesign:
    id: str
    style: str
    spec: Path | None  # None: the `bag template --sample` workbook

    def build(self, cache: Path) -> Built:
        cache = Path(cache)
        cache.mkdir(parents=True, exist_ok=True)
        version = _bag_version()
        spec = self.spec if self.spec is not None else _sample_workbook(cache, version)
        spec_sha = _sha256(spec)
        outdir = cache / self.id
        stamp_file = cache / f"{self.id}.stamp.json"
        stamp = {"bag_version": version, "spec_sha256": spec_sha}
        if not (_stamp(stamp_file) == stamp and (outdir / "bag_outputs.json").is_file()):
            stamp_file.unlink(missing_ok=True)
            tmp = cache / f"{self.id}.{os.getpid()}.partial"
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(outdir, ignore_errors=True)
            _run_bag("generate", str(spec), "-o", str(tmp), "--style", self.style)
            try:
                tmp.rename(outdir)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            stamp_file.write_text(json.dumps(stamp), encoding="utf-8")
        outputs = json.loads((outdir / "bag_outputs.json").read_text(encoding="utf-8"))
        return Built(
            outdir=outdir,
            filelist=outdir / outputs["filelist"],
            top=outputs["top"],
            hashes=dict(outputs["files"]),
        )


def _stamp(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _sample_workbook(cache: Path, version: str) -> Path:
    # Each template write has a different sha256, so write it once per bag version.
    book = cache / "_sample" / "sample.xlsx"
    stamp = book.with_name("sample.version")
    if not (book.is_file() and stamp.is_file() and stamp.read_text(encoding="utf-8") == version):
        book.parent.mkdir(parents=True, exist_ok=True)
        stamp.unlink(missing_ok=True)
        _run_bag("template", "--sample", "-o", str(book))
        stamp.write_text(version, encoding="utf-8")
    return book


def designs() -> list[CorpusDesign]:
    repo = _checkout()
    specs: list[tuple[str, Path | None]] = []
    if repo is None:
        specs.append((_SAMPLE_ID, None))
    else:
        specs.append((_SAMPLE_ID, repo / "tests" / "golden" / "demo_ss.ir.yaml"))
        for group in ("p2", "p3"):
            for p in sorted((repo / "tests" / "configs" / group).glob("*.ir.yaml")):
                specs.append((f"{group}-{p.name[: -len('.ir.yaml')]}", p))
    found = [CorpusDesign(f"{name}-{style}", style, spec) for name, spec in specs for style in STYLES]
    return sorted(found, key=lambda d: d.id)
