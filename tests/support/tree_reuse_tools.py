"""The helpers that test_tree_reuse.py and test_tree_reuse_review.py both use."""

from __future__ import annotations

from pathlib import Path

from hdl_vet.slang_adapter import AdapterError


def describe(design) -> dict:
    """Everything observable on a Design, in comparable form."""
    out: dict = {
        "diagnostics": design.diagnostics(),
        "snapshot": design.snapshot(),
        "paths": design.elaborated_instance_paths(),
        "count": design.elaborated_instance_count(),
        "modules": design.modules(),
        "included": sorted(design.included),
        "included_bytes": {p: bytes(b) for p, b in sorted(design.included_bytes.items())},
        "files": design.files,
        "roundtrip": {p: design.roundtrip(p) for p in design.files},
    }
    models = {}
    sites = {}
    for name in design.modules():
        try:
            models[name] = design.module(name)
        except AdapterError as e:
            models[name] = f"AdapterError: {e}"
        try:
            sites[name] = design.instance_sites(name)
        except AdapterError as e:
            sites[name] = f"AdapterError: {e}"
    out["models"] = models
    out["sites"] = sites
    return out


def differences(a: dict, b: dict) -> list[str]:
    return [key for key in a if a[key] != b.get(key)]


def write(root: Path, files: dict[str, str], filelist: str) -> Path:
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode())
    (root / "t.f").write_bytes(filelist.encode())
    return root / "t.f"
