"""The `vet` command line."""

from __future__ import annotations

import argparse
import hashlib
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from hdl_vet import __version__
from hdl_vet.filelist import FilelistError
from hdl_vet.ops import Vet, VetError
from hdl_vet.output import OutputError, write_tree
from hdl_vet.session import Session, SessionError
from hdl_vet.slang_adapter import AdapterError
from hdl_vet.splice import SpliceError

_OWN_ERRORS = (FilelistError, AdapterError, SessionError, SpliceError, OutputError)


class _InputError(Exception):
    """A usage or input problem: reported on stderr, exit status 2."""


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vet", description="Scripted, verified edits to generated RTL.")
    p.add_argument("--version", action="version", version=f"hdl-vet {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("apply", help="run edit scripts on a design and write a verified copy")
    a.add_argument("-f", "--filelist", required=True, type=Path, help="top-level .f file")
    a.add_argument("-o", "--outdir", type=Path, help="output directory (required unless --dry-run)")
    a.add_argument("-s", "--script", action="append", default=[], type=Path, help="script defining apply(vet)")
    a.add_argument("--top", help="top module")
    a.add_argument("--base", type=Path, help="directory that relative file list entries resolve against")
    a.add_argument("--dry-run", action="store_true", help="print the diff, write nothing")
    a.add_argument("--force", action="store_true", help="replace an existing output directory")
    return p


def _load_script(path: Path) -> tuple[str, str, Any]:
    """Compile and exec a script from its bytes (never imported, so no __pycache__)."""
    try:
        data = path.read_bytes()
    except OSError as e:
        raise _InputError(f"{path}: cannot read script ({e})") from e
    namespace: dict[str, Any] = {"__name__": "__hdl_vet_script__", "__file__": str(path)}
    try:
        exec(compile(data, str(path), "exec"), namespace)  # noqa: S102
    except SyntaxError as e:
        raise _InputError(f"{path}: syntax error: {e}") from e
    except SystemExit as e:
        raise _InputError(f"{path}: the script called exit ({e.code!r}) while loading") from e
    except Exception as e:
        raise _InputError(f"{path}: failed while loading: {type(e).__name__}: {e}") from e
    func = namespace.get("apply")
    if not callable(func):
        raise _InputError(f"{path}: the script must define apply(vet)")
    return path.name, hashlib.sha256(data).hexdigest(), func


def _apply(args: argparse.Namespace) -> int:
    if args.outdir is None and not args.dry_run:
        raise _InputError("-o/--outdir is required unless --dry-run is given")
    scripts = [_load_script(p) for p in args.script]
    session = Session(args.filelist, top=args.top, base=args.base)
    vet = Vet(session)
    for name, _, func in scripts:
        try:
            func(vet)
        except VetError as e:
            raise _InputError(f"script {name}: {e}") from e
        except _OWN_ERRORS:
            raise
        except SystemExit as e:
            raise _InputError(f"script {name} called exit ({e.code!r}); scripts must return from apply()") from e
        except Exception as e:
            raise _InputError(
                f"script {name} raised {type(e).__name__}: {e}\n{traceback.format_exc()}"
            ) from e
    ident = [(name, sha) for name, sha, _ in scripts]
    result = vet.finish(ident)
    if not result.ok:
        sys.stderr.write(result.report())
        return 1
    applied = sum(1 for r in vet.log if r.status == "applied")
    skipped = len(vet.log) - applied
    summary = f"{applied} applied, {skipped} skipped"
    if args.dry_run:
        sys.stdout.write(result.diff)
        sys.stderr.write(summary + "\n")
        return 0
    out = write_tree(session, result, args.outdir, scripts=ident, force=args.force)
    root = session.layout_root
    lines = [f"wrote {out}", summary]
    lines += [f"changed {p.relative_to(root).as_posix()}" for p in sorted(result.files, key=lambda p: p.as_posix())]
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def _tolerate_any_text() -> None:
    """Never crash on text the console encoding cannot represent: escape it."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: Sequence[str] | None = None) -> int:
    _tolerate_any_text()
    args = _parser().parse_args(argv)
    try:
        return _apply(args)
    except _InputError as e:
        sys.stderr.write(f"vet: {e}\n")
        return 2
    except _OWN_ERRORS as e:
        sys.stderr.write(f"vet: {e}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
