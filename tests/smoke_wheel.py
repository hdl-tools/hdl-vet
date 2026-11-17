"""Smoke check of an installed wheel (run by CI's `package` job, not by pytest).

Usage: <python of an environment that has only the wheel installed> tests/smoke_wheel.py

The test suite runs against an editable install, so it cannot notice a module
left out of the wheel or a broken entry point. This script checks that the
installed package is the wheel's (not the source tree), that it holds every
module of `src/hdl_vet`, and that `vet` runs a script on a small design.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))

import hdl_vet  # noqa: E402
from vetkit import make_design  # noqa: E402

SCRIPT = """\
def apply(vet):
    vet.module("top").add_port("dft_en", "input")
    vet.module("top").remove_instance("u_spare")
"""


def main() -> int:
    installed = Path(hdl_vet.__file__).resolve().parent
    source = REPO / "src" / "hdl_vet"
    # The environment may live inside the checkout (CI creates it there); what must not
    # happen is that the package comes from `src`, as it would with an editable install.
    if installed == source or (REPO / "src") in installed.parents:
        print(f"FAIL: hdl_vet was imported from the source tree ({installed}), not from an installed wheel")
        return 1
    missing = sorted({p.name for p in source.glob("*.py")} - {p.name for p in installed.glob("*.py")})
    if missing:
        print(f"FAIL: modules missing from the wheel: {', '.join(missing)}")
        return 1

    vet = shutil.which("vet", path=str(Path(sys.executable).parent))
    if vet is None:
        print("FAIL: the wheel installed no `vet` command next to this Python")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        kit = make_design(root / "in")
        script = root / "edit.py"
        script.write_text(SCRIPT, encoding="utf-8")
        out = root / "out"
        steps = [
            [vet, "--version"],
            [vet, "apply", "-f", str(kit.filelist), "--top", "top", "-s", str(script), "--save", str(root / "s.zip")],
            [vet, "apply", "--resume", str(root / "s.zip"), "-o", str(out)],
        ]
        for cmd in steps:
            done = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if done.returncode != 0:
                print(f"FAIL: {' '.join(cmd[1:])} exited {done.returncode}\n{done.stdout}{done.stderr}")
                return 1
        top = (out / "rtl" / "top.sv").read_bytes()
        if b"input  logic dft_en" not in top or b"u_spare" in top or not (out / "hdl-vet.json").is_file():
            print("FAIL: the output tree is not what the script asked for")
            return 1
    problem = _engine_without_the_extra()
    if problem:
        print(f"FAIL: {problem}")
        return 1
    print(f"ok: hdl-vet {hdl_vet.__version__} from {installed}")
    return 0


def _engine_without_the_extra() -> str | None:
    """The wheel is installed alone: hdl_vet.equiv must import and say that Yosys is missing."""
    import importlib.util

    from hdl_vet import equiv

    if importlib.util.find_spec("yowasp_yosys") is not None:
        return None  # the extra is installed here; nothing to check
    if equiv.available():
        return "hdl_vet.equiv says Yosys is available, but the package is not installed"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "m.sv"
        path.write_text("module m (input logic a, output logic o);\n  assign o = a;\nendmodule\n", encoding="utf-8")
        source = equiv.Source([path])
        result = equiv.check(source, source, "m")
    if result.status != "error" or "hdl-vet[equiv]" not in result.detail:
        return f"without the extra, equiv.check answered {result.status!r}: {result.detail!r}"
    return None


if __name__ == "__main__":
    raise SystemExit(main())
