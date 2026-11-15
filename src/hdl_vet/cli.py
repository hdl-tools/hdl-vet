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
    a.add_argument("-f", "--filelist", type=Path, help="top-level .f file (required unless --resume is given)")
    a.add_argument("-o", "--outdir", type=Path, help="output directory (one of -o, --save and --dry-run is required)")
    a.add_argument("-s", "--script", action="append", default=[], type=Path, help="script defining apply(vet)")
    a.add_argument("--top", help="top module")
    a.add_argument("--base", type=Path, help="directory that relative file list entries resolve against")
    a.add_argument("--dry-run", action="store_true", help="print the diff; no output tree is written (an archive named by --save still is)")
    a.add_argument("--force", action="store_true", help="replace an existing output directory")
    a.add_argument(
        "--save",
        type=Path,
        metavar="FILE",
        help="after the scripts have run, save the session to this zip archive (not inside the input tree or "
        "the -o directory, and not a script or the file list). The state is saved whether or not it passes "
        "sign-off; without -o its status so far is reported and the exit status is 0",
    )
    a.add_argument(
        "--resume",
        type=Path,
        metavar="FILE",
        help="continue from a session saved with --save instead of reading -f (which, like --top and --base, "
        "must not be given). The saved state is used as it is: changes made to the file list, the sources or "
        "the applied scripts after the save are not detected",
    )
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


def _within(path: Path, parent: Path) -> bool:
    path, parent = path.resolve(), parent.resolve()
    return path == parent or parent in path.parents


def _same_file(a: Path, b: Path) -> bool:
    return a.resolve() == b.resolve()


def _check_usage(args: argparse.Namespace) -> None:
    if args.resume is None:
        if args.filelist is None:
            raise _InputError("one of -f/--filelist and --resume is required")
    else:
        given = [opt for opt, v in (("-f", args.filelist), ("--top", args.top), ("--base", args.base)) if v is not None]
        if given:
            raise _InputError(f"{' and '.join(given)} must not be given with --resume: the saved session has them")
    if args.outdir is None and args.save is None and not args.dry_run:
        raise _InputError("one of -o/--outdir, --save and --dry-run is required")
    if args.save is not None:
        if args.outdir is not None and _within(args.save, args.outdir):
            raise _InputError(f"--save {args.save} is inside the -o directory {args.outdir}")
        owned = [*args.script, *([args.filelist] if args.filelist is not None else [])]
        if any(_same_file(args.save, p) for p in owned):
            raise _InputError(f"--save {args.save} names a script or the file list; it would be overwritten")
    if args.resume is not None and args.outdir is not None and _within(args.resume, args.outdir):
        raise _InputError(f"--resume {args.resume} is inside the -o directory {args.outdir}, which would be replaced")


def _apply(args: argparse.Namespace) -> int:
    _check_usage(args)
    scripts = [_load_script(p) for p in args.script]
    if args.resume is not None:
        vet = Vet.restore(args.resume)
        session = vet.session
        for name, sha, _ in scripts:
            if (name, sha) in session.scripts:
                raise _InputError(f"script {name}: already applied by the session saved in {args.resume}")
    else:
        session = Session(args.filelist, top=args.top, base=args.base)
        vet = Vet(session)
    if args.save is not None:
        if _within(args.save, session.layout_root):
            raise _InputError(f"--save {args.save} is inside the input tree {session.layout_root}")
        for owned, what in session.owned_paths().items():
            if _same_file(args.save, owned):
                raise _InputError(f"--save {args.save} is {what}; it would be overwritten")
    first_record, first_note = len(vet.log), len(vet.notes)  # what the saved session did is not reported again
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
        # Every script ends a stage: the next one sees what this one queued, as it would after a
        # resume. A state that cannot be loaded is left for sign-off to report.
        try:
            session.refresh()
        except AdapterError:
            pass
    ident = [(name, sha) for name, sha, _ in scripts]
    everything = [*session.scripts, *ident]
    ran = vet.log[first_record:]
    applied = sum(1 for r in ran if r.status == "applied")
    summary = f"{applied} applied, {len(ran) - applied} skipped"
    notes = [f"note: {note}" for note in vet.notes[first_note:]]
    if args.save is not None:
        try:
            vet.save(args.save, ident)
        except VetError as e:
            raise _InputError(f"--save: {e}") from e
        ident = []  # the session lists them now
        if args.outdir is None and not args.dry_run:
            status = vet.check()
            sys.stdout.write("\n".join([f"saved {args.save}", summary, *notes]) + "\n")
            if status.ok:
                sys.stdout.write("sign-off so far: ok\n")
            else:
                sys.stdout.write("sign-off so far: FAILED\n")
                sys.stderr.write(status.report())
            return 0
    result = vet.finish(ident)
    saved = [] if args.save is None else [f"saved {args.save}"]
    if not result.ok:
        (sys.stderr if args.dry_run else sys.stdout).write("".join(f"{line}\n" for line in saved))
        sys.stderr.write(result.report())
        return 1
    if args.dry_run:
        sys.stdout.write(result.diff)
        sys.stderr.write("".join(f"added {name}\n" for name in result.added))
        sys.stderr.write("".join(f"{note}\n" for note in notes))
        sys.stderr.write("".join(f"{line}\n" for line in saved))
        sys.stderr.write(summary + "\n")
        return 0
    out = write_tree(session, result, args.outdir, scripts=everything, force=args.force)
    root = session.layout_root
    lines = [f"wrote {out}", *saved, summary]
    lines += [f"changed {p.relative_to(root).as_posix()}" for p in sorted(result.files, key=lambda p: p.as_posix())]
    lines += [f"added {name}" for name in result.added]
    lines += notes
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
