"""Replay the console transcripts shown in the manual and the README.

A transcript is a text file. A line that starts with "$ " is a command; the lines
after it, up to the next command, are what the command prints. Example:

    $ vet apply -f design/design.f --top soc_top -s scripts/add_port.py --dry-run
    1 applied, 0 skipped
    ...
    $ vet apply -f design/missing.f --dry-run
    vet: ...
    [exit status 2]

Rules of the format:

- Commands: `vet ...` (run in this process through `hdl_vet.cli.main`), `cd DIR`,
  `cat FILE`, `cp SRC DST` (one file), `ls [DIR]` (names in sorted order, one per
  line, a directory with a trailing "/"). Words are split as a POSIX shell does;
  there are no pipes, variables or wildcards.
- A transcript starts in a fresh copy of `docs/manual/examples`. Commands of one
  transcript share that copy and the current directory, so a later command sees
  what an earlier one wrote. Every path stays inside the copy.
- What `vet` writes to stdout and stderr is shown as one stream, in the order it
  was written. A non-zero exit status is shown as a last line `[exit status N]`.
- Four things are made the same on every machine before comparing: a backslash
  reads "/" (so a transcript never contains a backslash); the path of the copy
  reads `<examples>`; the version of hdl-vet reads `<version>`; spaces at the end
  of a line and blank lines at the end of a command's output are dropped.
- Output that cannot be the same everywhere is refused, with a ValueError: `--help`
  and argparse usage messages (they follow the Python version and the terminal
  width), a Python traceback, a Python warning, a temporary file name, and any
  other path of this machine. Choose another example, or fix the message in vet.

With the environment variable HDL_VET_MANUAL_UPDATE=1, `replay` rewrites the
transcript with what the commands print now, and returns it as both values. Use it
to write a new transcript or after an intended change of output; read the
resulting diff before committing it.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import shlex
import shutil
import tempfile
import warnings
from collections.abc import Iterator
from pathlib import Path

import hdl_vet
from hdl_vet.cli import main

REPO = Path(__file__).resolve().parent.parent
MANUAL = REPO / "docs" / "manual"
EXAMPLES = MANUAL / "examples"
UPDATE = os.environ.get("HDL_VET_MANUAL_UPDATE") == "1"

_VERSION = re.compile(r"(?<![\w.])" + re.escape(hdl_vet.__version__) + r"(?![\w.])")


def parse(text: str) -> list[tuple[str, list[str]]]:
    """[(command, expected output lines)] of a transcript."""
    steps: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        if line.startswith("$ "):
            steps.append((line[2:].strip(), []))
        elif steps:
            steps[-1][1].append(line)
        elif line.strip():
            raise ValueError("a transcript starts with a command line ('$ ...')")
    for _, lines in steps:
        while lines and not lines[-1].strip():
            lines.pop()
    return steps


@contextlib.contextmanager
def _cwd(path: Path) -> Iterator[None]:
    before = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(before)


def _inside(root: Path, cwd: Path, word: str) -> Path:
    path = (cwd / word).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"{word!r} leaves the copy of the examples")
    return path


def _vet(args: list[str], cwd: Path) -> tuple[str, int]:
    if any(a in ("-h", "--help") for a in args):
        raise ValueError("a transcript must not show --help")
    buf = io.StringIO()
    with (
        _cwd(cwd),
        contextlib.redirect_stdout(buf),
        contextlib.redirect_stderr(buf),
        warnings.catch_warnings(record=True) as caught,
    ):
        warnings.simplefilter("always")
        try:
            status = main(args)
        except SystemExit as e:
            status = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    if caught:
        raise ValueError(f"Python warning while running vet (a terminal would show it): {caught[0].message}")
    return buf.getvalue(), int(status or 0)


def _unstable(text: str) -> str | None:
    """Why this output cannot be shown: it would differ on another machine or Python version."""
    if "Traceback (most recent call last)" in text:
        return "a Python traceback"
    if text.startswith("usage:"):
        return "an argparse usage message (its line breaks follow the terminal width)"
    if re.search(r"\.hdl-vet-\w+\.tmp", text):
        return "a temporary file name"
    for place in (Path.home(), Path(tempfile.gettempdir())):
        if place.as_posix().lower() in text.lower():
            return f"a path of this machine ({place.as_posix()})"
    return None


def _normalise(text: str, root: Path) -> list[str]:
    """The same text on every machine: "/" for the backslash, `<examples>` for the copy, `<version>`."""
    text = text.replace("\\\\", "\\").replace("\\", "/")
    spellings = {root.as_posix(), root.as_posix().replace("/", "//")}
    for spelling in sorted(spellings, key=len, reverse=True):
        text = re.sub(re.escape(spelling), "<examples>", text, flags=re.IGNORECASE)
    text = _VERSION.sub("<version>", text)
    reason = _unstable(text)
    if reason:
        raise ValueError(f"the output contains {reason}; it cannot be shown in a transcript:\n{text}")
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def run(command: str, root: Path, cwd: Path) -> tuple[list[str], Path]:
    """Run one command; (normalised output lines, the current directory after it)."""
    words = shlex.split(command)
    if not words:
        raise ValueError("empty command")
    name, args = words[0], words[1:]
    if name == "vet":
        text, status = _vet(args, cwd)
        lines = _normalise(text, root)
        if status:
            lines.append(f"[exit status {status}]")
        return lines, cwd
    if name == "cd" and len(args) == 1:
        target = _inside(root, cwd, args[0])
        if not target.is_dir():
            raise ValueError(f"cd: {args[0]!r} is not a directory")
        return [], target
    if name == "cat" and len(args) == 1:
        return _normalise(_inside(root, cwd, args[0]).read_text(encoding="utf-8"), root), cwd
    if name == "cp" and len(args) == 2:
        target = _inside(root, cwd, args[1])
        if target.is_dir():
            target = target / Path(args[0]).name
        shutil.copyfile(_inside(root, cwd, args[0]), target)
        return [], cwd
    if name == "ls" and len(args) <= 1:
        folder = _inside(root, cwd, args[0] if args else ".")
        return [p.name + ("/" if p.is_dir() else "") for p in sorted(folder.iterdir(), key=lambda p: p.name)], cwd
    raise ValueError(f"not a transcript command: {command!r}")


def render(steps: list[tuple[str, list[str]]]) -> str:
    out: list[str] = []
    for command, lines in steps:
        out.append("$ " + command)
        out.extend(lines)
    return "\n".join(out) + "\n"


def replay_text(text: str, tmp: Path) -> tuple[str, str]:
    """(the transcript as written, the transcript as the commands print it now)."""
    root = (tmp / "examples").resolve()
    shutil.copytree(EXAMPLES, root)
    cwd = root
    steps = parse(text)
    actual: list[tuple[str, list[str]]] = []
    for command, _ in steps:
        lines, cwd = run(command, root, cwd)
        actual.append((command, lines))
    return render(steps), render(actual)


def replay(path: Path, tmp: Path) -> tuple[str, str]:
    """As `replay_text`, for a transcript file; rewrites the file in update mode."""
    expected, actual = replay_text(path.read_text(encoding="utf-8"), tmp)
    if UPDATE and expected != actual:
        path.write_bytes(actual.encode("utf-8"))
        return actual, actual
    return expected, actual


def commands(text: str) -> list[str]:
    return [command for command, _ in parse(text)]
