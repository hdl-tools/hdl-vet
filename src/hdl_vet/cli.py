"""The `vet` command line."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys
import traceback
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hdl_vet import __version__
from hdl_vet.filelist import FilelistError
from hdl_vet.ops import Vet, VetError
from hdl_vet.output import OutputError, write_tree
from hdl_vet.paths import within
from hdl_vet.project import PROJECT_FILE, Project, ProjectError, init_project, load_project
from hdl_vet.session import Session, SessionError
from hdl_vet.slang_adapter import AdapterError
from hdl_vet.splice import SpliceError

_OWN_ERRORS = (FilelistError, AdapterError, SessionError, SpliceError, OutputError, ProjectError)


class _InputError(Exception):
    """A usage or input problem: reported on stderr, exit status 2."""


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vet", description="Scripted, verified edits to generated RTL.")
    p.add_argument("--version", action="version", version=f"hdl-vet {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("apply", help="run edit scripts on a design and write a verified copy")
    a.add_argument(
        "-f",
        "--filelist",
        type=Path,
        help="top-level .f file (required unless --resume is given or the project file names one); "
        "replaces the project's file list and its base",
    )
    a.add_argument("-o", "--outdir", type=Path, help="output directory (one of -o, --save and --dry-run is required)")
    a.add_argument(
        "-s",
        "--script",
        action="append",
        default=[],
        type=Path,
        help="script defining apply(vet); runs after the project's scripts",
    )
    a.add_argument("--top", help="top module (replaces the project's)")
    a.add_argument("--base", type=Path, help="directory that relative file list entries resolve against")
    a.add_argument(
        "--dry-run",
        action="store_true",
        help="print the diff; no output tree is written (an archive named by --save still is)",
    )
    a.add_argument("--force", action="store_true", help="replace an existing output directory")
    a.add_argument(
        "--save",
        type=Path,
        metavar="FILE",
        help="after the scripts have run, save the session to this zip archive (not inside the input tree or "
        "the -o directory, and not a script, the project file or the file list). The state is saved whether "
        "or not it passes sign-off; without -o its status so far is reported and the exit status is 0",
    )
    a.add_argument(
        "--resume",
        type=Path,
        metavar="FILE",
        help="continue from a session saved with --save instead of reading -f (which, like --top and --base, "
        "must not be given). The saved state is used as it is: changes made to the file list, the sources or "
        "the applied scripts after the save are not detected",
    )
    a.add_argument(
        "--project",
        type=Path,
        metavar="FILE",
        help=f"use this project file instead of {PROJECT_FILE} in the current directory",
    )
    a.add_argument("--no-project", action="store_true", help=f"ignore the project file ({PROJECT_FILE})")
    i = sub.add_parser(
        "init", help=f"write a starter project ({PROJECT_FILE}, an example script, ...) into a directory"
    )
    i.add_argument("directory", type=str, metavar="DIR", help="the new project directory (absent or empty)")
    i.add_argument("-f", "--filelist", type=Path, help="top-level .f file of the design (required)")
    i.add_argument("--top", help="top module (default: the design's only top-level module)")
    i.add_argument("--base", type=Path, help="directory that relative file list entries resolve against")
    return p


def _read_script(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as e:
        raise _InputError(f"{path}: cannot read script ({e})") from e


def _load_script(path: Path) -> tuple[str, str, Any]:
    """Compile and exec a script from its bytes (never imported, so no __pycache__)."""
    data = _read_script(path)
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


def _same_file(a: Path, b: Path) -> bool:
    return a.resolve() == b.resolve()


@dataclass(frozen=True)
class _Plan:
    """What `apply` does: the command line combined with the project file."""

    filelist: Path | None
    base: Path | None
    top: str | None
    scripts: list[Path]  # the project's scripts, then those of -s
    project_scripts: int  # how many of `scripts` are the project's
    outdir: Path | None


def _find_project(args: argparse.Namespace) -> Project | None:
    """The project file to use, if any; its path goes to stderr as soon as it is found."""
    if args.project is not None and args.no_project:
        raise _InputError("--project and --no-project cannot be given together")
    if args.no_project:
        return None
    path: Path = args.project if args.project is not None else Path(PROJECT_FILE)
    if path.is_dir():
        what = f"--project {path}" if args.project is not None else f"{PROJECT_FILE} in the current directory"
        raise _InputError(f"{what} is a directory, not a project file")
    if not path.is_file():
        if args.project is not None:
            raise _InputError(f"--project {path}: no such file")
        return None
    path = path.resolve()
    sys.stderr.write(f"project: {path}\n")
    return load_project(path)


def _raise(error: OSError) -> None:
    raise error


def _check_replaceable(outdir: Path) -> None:
    """`--force` on the output directory a project names: only an earlier hdl-vet output with nothing else in it."""
    if not outdir.is_dir():
        return
    try:
        if not any(outdir.iterdir()):
            return
    except OSError as e:
        raise _InputError(f"the project's output {outdir}: cannot be listed ({e})") from e

    def refuse(why: str) -> _InputError:
        return _InputError(
            f"the project's output {outdir} is not an hdl-vet output with nothing else in it ({why}), "
            "so --force will not replace it"
        )

    try:
        manifest = json.loads((outdir / "hdl-vet.json").read_bytes())
    except (OSError, ValueError, RecursionError):
        raise refuse("hdl-vet.json is missing or cannot be read") from None
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("tool") != "hdl-vet"
        or not isinstance(files, list)
        or not all(isinstance(f, str) for f in files)
    ):
        raise refuse("hdl-vet.json is not an hdl-vet manifest with a list of files")
    known = {*files, "hdl-vet.json"}
    try:
        for here, _, names in os.walk(outdir, onerror=_raise):
            for name in names:
                rel = (Path(here) / name).relative_to(outdir).as_posix()
                if rel not in known:
                    raise refuse(f"{rel} is not listed in hdl-vet.json")
    except OSError as e:
        raise _InputError(f"the project's output {outdir}: cannot be listed ({e})") from e


def _plan(args: argparse.Namespace, project: Project | None) -> _Plan:
    scripts = [*(project.scripts if project is not None else ()), *args.script]
    filelist, base, top = args.filelist, args.base, args.top
    if args.resume is None:
        if project is not None:
            if filelist is None:  # a file list brings its own base: the project's belongs to the project's file list
                filelist = project.filelist
                if base is None:
                    base = project.base
            if top is None:
                top = project.top
        if filelist is None:
            hint = f"; {project.path.name} names none" if project is not None else ""
            raise _InputError(f"one of -f/--filelist and --resume is required{hint}")
    else:
        given = [opt for opt, v in (("-f", filelist), ("--top", top), ("--base", base)) if v is not None]
        if given:
            raise _InputError(f"{' and '.join(given)} must not be given with --resume: the saved session has them")
    # After the file list is settled (a usage error of its own), before the output is looked at.
    seen: set[Path] = set()
    for script in scripts:
        if script.resolve() in seen:
            raise _InputError(f"script {script} is named twice")
        seen.add(script.resolve())

    outdir, from_project = args.outdir, False  # from_project: the directory comes from the project file
    if outdir is None and args.save is None and not args.dry_run:
        if project is not None and project.output is not None:
            outdir, from_project = project.output, True
        else:
            raise _InputError("one of -o/--outdir, --save and --dry-run is required")
    called = "the project's output directory" if from_project else "the -o directory"
    if args.save is not None:
        if outdir is not None and within(args.save, outdir):
            raise _InputError(f"--save {args.save} is inside {called} {outdir}")
        owned = [*scripts, *([filelist] if filelist is not None else [])]
        if any(_same_file(args.save, p) for p in owned):
            raise _InputError(f"--save {args.save} names a script or the file list; it would be overwritten")
        if project is not None and _same_file(args.save, project.path):
            raise _InputError(f"--save {args.save} names the project file; it would be overwritten")
    if args.resume is not None and outdir is not None and within(args.resume, outdir):
        raise _InputError(f"--resume {args.resume} is inside {called} {outdir}, which would be replaced")
    if outdir is not None:
        # Writing the output tree replaces its directory: it must not take the user's own files along.
        where = f"the project's output {outdir}" if from_project else f"-o {outdir}"
        for path in [*scripts, *([project.path] if project is not None else [])]:
            if within(path, outdir):
                raise _InputError(f"{where} would replace {path}; the output directory must not contain it")
        if from_project and args.force:
            _check_replaceable(outdir)
    return _Plan(filelist, base, top, scripts, len(scripts) - len(args.script), outdir)


def _apply(args: argparse.Namespace) -> int:
    project = _find_project(args)
    if project is not None and args.filelist is not None and project.scripts:
        names = ", ".join(p.name for p in project.scripts)
        sys.stderr.write(f"note: the project's scripts run first: {names}\n")
    plan = _plan(args, project)
    loaded = [_load_script(p) for p in plan.scripts]  # each script is read before the saved session
    if args.resume is not None:
        vet = Vet.restore(args.resume)
        session = vet.session
        saved = session.scripts
        for number, ((name, sha, _), path) in enumerate(
            zip(loaded[: plan.project_scripts], plan.scripts, strict=False), start=1
        ):
            if number > len(saved) or (name, sha) == saved[number - 1]:
                continue
            if saved[number - 1][0] == name:
                what = f"has other content than the script {name} the session saved in {args.resume} applied"
            else:
                what = f"is not script {saved[number - 1][0]} of the session saved in {args.resume}"
            raise _InputError(
                f"the project's script {number}, {path.name}, {what}: the project has changed since the save "
                "(--no-project resumes regardless)"
            )
        # The project's scripts the save did not have run first, then those of -s.
        scripts = [*loaded[len(saved) : plan.project_scripts], *loaded[plan.project_scripts :]]
        for name, sha, _ in scripts:
            if (name, sha) in session.scripts:
                raise _InputError(f"script {name}: already applied by the session saved in {args.resume}")
    else:
        scripts = loaded
        if plan.filelist is None:  # _plan has seen to it
            raise _InputError("-f/--filelist is required")
        session = Session(plan.filelist, top=plan.top, base=plan.base)
        vet = Vet(session)
    if args.save is not None:
        for owned, what in session.owned_paths().items():
            if _same_file(args.save, owned):
                raise _InputError(f"--save {args.save} is {what}; it would be overwritten")
        if within(args.save, session.layout_root):
            raise _InputError(f"--save {args.save} is inside the input tree {session.layout_root}")
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
            raise _InputError(f"script {name} raised {type(e).__name__}: {e}\n{traceback.format_exc()}") from e
        if (left := vet.open_block()) is not None:
            raise _InputError(f"script {name} returned with a {left} still open; close every `with` block")
        # Every script ends a stage: the next one sees what this one queued, as it would after a
        # resume. A state that cannot be loaded is left for sign-off to report.
        with contextlib.suppress(AdapterError):
            session.refresh()
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
        if plan.outdir is None and not args.dry_run:
            status = vet.check()
            sys.stdout.write("\n".join([f"saved {args.save}", summary, *notes]) + "\n")
            if status.ok:
                sys.stdout.write("sign-off so far: ok\n")
            else:
                sys.stdout.write("sign-off so far: FAILED\n")
                sys.stderr.write(status.report())
            return 0
    result = vet.finish(ident)
    saved_lines = [] if args.save is None else [f"saved {args.save}"]
    if not result.ok:
        (sys.stderr if args.dry_run else sys.stdout).write("".join(f"{line}\n" for line in saved_lines))
        sys.stderr.write(result.report())
        return 1
    if args.dry_run:
        sys.stdout.write(result.diff)
        sys.stderr.write("".join(f"added {name}\n" for name in result.added))
        sys.stderr.write("".join(f"{note}\n" for note in notes))
        sys.stderr.write("".join(f"{line}\n" for line in saved_lines))
        sys.stderr.write(summary + "\n")
        return 0
    if plan.outdir is None:  # --save without -o returned above
        raise _InputError("one of -o/--outdir, --save and --dry-run is required")
    out = write_tree(session, result, plan.outdir, scripts=everything, force=args.force)
    root = session.layout_root
    lines = [f"wrote {out}", *saved_lines, summary]
    lines += [f"changed {p.relative_to(root).as_posix()}" for p in sorted(result.files, key=lambda p: p.as_posix())]
    lines += [f"added {name}" for name in result.added]
    lines += notes
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def _init(args: argparse.Namespace) -> int:
    if args.filelist is None:
        raise _InputError("init: -f/--filelist is required")
    written = init_project(args.directory, args.filelist, top=args.top, base=args.base)
    root = Path(args.directory).resolve()
    lines = [f"created {args.directory}", *(f"wrote {p.relative_to(root).as_posix()}" for p in written)]
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def _tolerate_any_text() -> None:
    """Never crash on text the console encoding cannot represent: escape it."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]


def main(argv: Sequence[str] | None = None) -> int:
    _tolerate_any_text()
    args = _parser().parse_args(argv)
    try:
        return _init(args) if args.command == "init" else _apply(args)
    except _InputError as e:
        sys.stderr.write(f"vet: {e}\n")
        return 2
    except (*_OWN_ERRORS, OSError) as e:
        sys.stderr.write(f"vet: {e}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
