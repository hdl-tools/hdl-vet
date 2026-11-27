"""Equivalence check of one module before and after an edit, with Yosys from PyPI.

`check(gold, gate, module)` compares the module of the Source `gold` with the module of
the same name of the Source `gate`. The tool is the optional extra `hdl-vet[equiv]`
(the package `yowasp-yosys`). Nothing of it is imported here at module import time.

Every check runs in a process of its own, started without a shell: this file, run as a
script by the same Python. The child reads a small specification from a temporary
folder, runs Yosys on it, and writes its answer back to that folder. The parent never
loads Yosys, so a crash or a time-out of the tool cannot reach the caller.

Each side is read once, made into plain logic (unpacked arrays are mapped to flops), and
written as a netlist. The checks below read that netlist. The proof flow is `clk2fflogic`,
`equiv_make`, `equiv_simple -undef -seq 5`, `equiv_induct -undef -seq 5` and
`equiv_status -assert`, then the first step (`sat` on a miter, every flop undefined). Only a complete,
explicit proof is "proved".

What the engine does so that "proved" is true
- Undefined values (`-undef`): x in the gate, or a net nobody drives, is a difference. x in the
  gold means that the gate may have any value.
- Clocks: `clk2fflogic` makes every flop logic around a state cell that steps with a global clock, and
  the clock of the design is an input like the others. A step is half a clock period or a pulse: in a
  step that is no clock edge the flops hold and an asynchronous reset, set or load still acts, so a
  reset pulse between two edges is compared. Every flop of both sides must be clocked by one input
  port, with one edge, the same on both sides, after the ties are applied. Else the answer is "refuted" or "error".
- Initial values (`logic r = 1'b1;`, an `initial` block) on either side: "error", also when both
  sides are identical. The proof is by induction and has no start state.
- Children named in `same`: the engine compares the module of each such instance, with
  everything below it and its parameter values, between the two sides. A difference is an "error"
  that names the instance. An instance of another module on the other side is an "error" too.
- Children named in `inert={"child": ["en_i", ...]}`: a child that the caller has proved on its own, with the
  inputs that only the gate's child has. Such a child is a black box on both sides and is not compared. The
  engine itself checks, on the netlists, that each instance of it is on both sides, that apart from the named
  inputs the two boxes have the same ports and the same parameter values, and that after the ties each named
  input of each instance is the constant 0 (see `hdl_vet.equiv_inert`). Anything else is an "error" that names
  the instance and the pin. The other connections of the box are compared as for a `same` child. A module in
  `same` and in `inert` is an "error".
- Names in `params` must be parameters of the module on both sides (checked with the reader of
  `hdl_vet.slang_adapter`, because Yosys drops an unknown name without a word).

Stated limits (each gives "error" or "refuted", except those that say "proved" or "wrong proved")
- Flops on more than one clock, when the module is compared with its children flattened:
  "error". Name the children of each clock domain in `same`.
- A clock that reaches a flop through a mux that is not tied or a divider, flops on both edges
  of one clock, latches, an asynchronous set together with a reset on one flop, interface ports,
  and memories that cannot be mapped to flops: "error". A clock through an inverter is the other
  edge of the input and is compared. Flops with different asynchronous resets are compared.
- A step of the proof is half a clock period or a pulse, and the proof looks a fixed number of
  steps back (`_SEQ`). A chain of two or more registers that are renamed between the two sides is
  "refuted", also when the logic is the same: the clock is free, so the proof cannot take the
  registers as unchanged. A single renamed register, and a rewrite that keeps the names, are
  compared. A rewrite that keeps the name of an internal signal but changes what it carries:
  "refuted".
- A flop that reads its clock as data (the clock input reaches a data, enable or synchronous reset pin
  through any logic, on either side): "error" that asks for a waiver. The proof takes the data from the
  step before the edge, where the clock still has its old value. Such a module gives "error" for every edit.
- A memory with an asynchronous reset makes the proof slower: 16 words of 8 bits take about 10 seconds,
  64 words about two minutes, and more can reach the time limit ("error").
- The first state of a flop is compared in one step, after a proof that is "proved" (`first_script`):
  every flop of both sides starts undefined, every input is defined, and the outputs must agree by the
  rule for undefined values. A difference there is "refuted" ("first state" in the detail). The cells
  `$specify2`, `$specify3`, `$specrule` and `$print` are taken out first; a run that fails is an
  "error" that starts with "the first state could not be checked: ". The step is one step, so a gate
  that differs only in a later step from a state the induction does not ask about is still a wrong
  "proved". An undefined value does not cancel there (`a ^ a` of a flop `a` with no reset is "refuted"
  against 0). A design with a memory was not measured.
- A wide multiplier written another way can reach the timeout.
- A change of the signedness of a port, of the order of the fields of a struct port or of the
  order of the ports is "proved": the module computes the same, but a parent that connects by
  position or by field sees a change. The structure comparison of the sign-off reports it.
- A module with flops on two clocks of its own (a clock-domain bridge) is always an "error", and
  it is answered before any proof is run.
- A memory of 1000 words of 32 bits reaches the timeout. A parameter value cannot be a string.
- Where the gold has an x that reaches an output, the proof may still be "proved" (the gate may
  choose). The detail then says "the gold has undefined values on " and names those outputs. They
  are found by one more proof of the gold against itself, the undriven bits as 0 against the same
  bits as free values that are independent of each other, run only when the
  gold has an x constant or a used net that nothing drives, or a select that can leave its range, or a
  division or modulo whose divisor can be zero (it cannot only when a bit of it is the constant 1 in the
  netlist).
  A constant x or z bit on an output port or on a box pin counts at once (`n / 4'd0` is folded to x).
  `Result.undefined_seen` is True when a named net, or the output of such a select or division, leads through the
  netlist of the gold to an output port or to an input pin of a box (see `_leads_outside`), or when that
  cannot be told. The caller then turns the "proved" into an "error".
- Defines: a name must be a plain identifier. A value is passed on as it is, with `"` escaped,
  except that a value with `$`, a backslash or a line break is an "error" that names the define
  (the reader's file would expand or change it). The same holds for a path or an include folder
  that holds a quote, a backslash, `$` or a line break. `defines` of a wrong type raises TypeError.
- The report of undefined values has a time budget of its own (30 s, a second process). When it
  runs out or fails, the answer of the engine is still "proved". The detail says that the report
  could not be made, and the sign-off (`prove.py`) counts that answer as an "error".
  The report does not see an undefined value of the gold that cancels (`u ^ u`). The one that a
  division by zero makes is not seen either, but it counts through its cell, as a select does.
- Checks run one at a time (a lock). The lock holds for the threads of one process, not for two
  processes.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from hdl_vet import equiv_inert

__all__ = ["Result", "Source", "available", "check"]

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_PORT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_$.]*\Z")
_VALUE = re.compile(r"[A-Za-z0-9_'.+\-]+\Z")
_MACRO_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_MOUNT = "/hostfs_"  # POSIX: the child sees the top-level folder NAME of the host as /hostfs_NAME (see `_Seen`)
_MOUNT_IN_TEXT = re.compile(r"/hostfs_([^/\s'\"]+)")
_FILES = re.compile(r"(?<![\w/.-])((?:gold|gate)(?:_h|_w)?\.(?:f|json|il)|prepared\.il|check\.json)(?!\w)")
_REPORT_TIMEOUT = 30.0  # seconds for the report of undefined values, apart from the timeout of the proof
_MAX_DETAIL = 300
_LOCK = threading.Lock()


@dataclass(frozen=True)
class Source:
    """The files of one side, in file-list order, with its include folders and defines."""

    files: Sequence[Path | str]
    incdirs: Sequence[Path | str] = ()
    defines: Mapping[str, str | None] = field(default_factory=dict)


@dataclass(frozen=True)
class Result:
    """`status` is "proved", "refuted" or "error"; `detail` is one line of text."""

    status: str
    module: str
    detail: str
    seconds: float
    # True when the answer is an "error" only because the engine cannot take the interface of an inert child
    # (a name it cannot handle, a port that is not as `inert` says). The caller can then compare without
    # `inert`. It is never True for a pin that is not 0, a net with two drivers or another parameter value.
    inert_refused: bool = False
    # True when a "proved" rests on an undefined value of the gold that can be seen from outside the module
    # (see `_leads_outside`), or when that could not be told. The caller turns such a "proved" into an "error".
    undefined_seen: bool = False


def available() -> bool:
    """True when the Yosys package can be used."""
    try:
        return importlib.util.find_spec("yowasp_yosys") is not None
    except (ImportError, ValueError):
        return False


# --- the parent: validate, set up a folder, start the child, read its answer -----------------------


def check(
    gold: Source,
    gate: Source,
    module: str,
    *,
    params: Mapping[str, int | str] | None = None,
    tie: Mapping[str, int] | None = None,
    same: Iterable[str] = (),
    inert: Mapping[str, Sequence[str]] | None = None,
    timeout: float = 300.0,
) -> Result:
    """Compare `module` of `gold` (before) with `module` of `gate` (after).

    Raises TypeError or ValueError only for a wrong call. Every other problem is an answer
    with the status "error".
    """
    started = time.monotonic()
    clean_params = _clean_params(params)
    clean_tie = _clean_tie(tie)
    clean_same = _clean_same(same)
    clean_inert = _clean_inert(inert)
    for side in (gold, gate):
        _check_defines(side.defines)
    if not isinstance(module, str) or not _IDENT.match(module):
        raise ValueError(f"not a module name: {module!r}")
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        raise ValueError(f"timeout must be a positive number of seconds: {timeout!r}")

    def done(status: str, detail: str) -> Result:
        if os.name != "nt":
            detail = _MOUNT_IN_TEXT.sub(r"/\1", detail)
            # The part after the mount prefix is the top-level folder's name, so the
            # group is kept. It held a control byte, which went into the message.
        return Result(status, module, _one_line(detail), time.monotonic() - started)

    both = sorted(set(clean_same) & set(clean_inert))
    if both:
        return done("error", f"{', '.join(both)} is named in same and in inert: a child is one or the other")
    if module in clean_inert:
        return done("error", f"the module {module} is named in inert: only a child can be")
    if not available():
        return done("error", "Yosys is not installed: install the extra hdl-vet[equiv]")
    for side in (gold, gate):
        for file in side.files:
            if not Path(file).is_file():
                return done("error", f"cannot read {Path(file)}: no such file")
    with _LOCK:  # one Yosys process at a time, also across threads
        started = time.monotonic()  # the time of a check does not include the wait for the lock
        try:
            wrong = _unknown_parameters(gold, gate, module, clean_params)
            if wrong is not None:
                return done("error", wrong)
            return _run_child(gold, gate, module, clean_params, clean_tie, clean_same, clean_inert, timeout, done)
        except OSError as exc:
            return done("error", f"cannot run the check: {exc}")


def _run_child(
    gold: Source,
    gate: Source,
    module: str,
    params: dict[str, str],
    tie: dict[str, int],
    same: list[str],
    inert: dict[str, list[str]],
    timeout: float,
    done: Callable[[str, str], Result],
) -> Result:
    with tempfile.TemporaryDirectory(prefix="hdl_vet_equiv_", ignore_cleanup_errors=True) as folder:
        work = Path(folder)
        (work / "tmp").mkdir()
        seen = _Seen()
        forms = {work.as_posix(), str(work), work.resolve().as_posix(), str(work.resolve())}
        finish = done

        def done(status: str, detail: str) -> Result:
            for form in sorted(forms, key=len, reverse=True):
                detail = re.sub(re.escape(form) + r"[/\\]?", "", detail, flags=re.IGNORECASE)
            detail = re.sub(re.escape(work.name), "", detail, flags=re.IGNORECASE)
            return finish(status, detail)

        try:
            work_seen = seen(work)
            forms.add(work_seen)
            _write_list(work / "gold.f", gold, seen)
            _write_list(work / "gate.f", gate, seen)
        except _Refused as exc:
            return done("error", str(exc))
        spec = {"module": module, "params": params, "tie": tie, "same": same, "inert": inert}
        (work / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        # The child must not put its scratch folders outside the work folder (the Yosys runtime
        # makes one in the temp folder of each run), so the temp variables point inside it.
        env = dict(os.environ)
        for name in ("TEMP", "TMP", "TMPDIR"):
            env[name] = str(work / "tmp")
        # The user's own YOWASP_MOUNT must not reach the child. On POSIX ours replaces it (see `_Seen`);
        # on Windows the default mounts of the drives are right.
        env.pop("YOWASP_MOUNT", None)
        env.pop("HDL_VET_HOSTFS", None)
        if os.name != "nt":
            env["YOWASP_MOUNT"] = ":".join(f"{_MOUNT}{top}=/{top}" for top in sorted(seen.tops))
            env["HDL_VET_HOSTFS"] = work_seen
        try:
            done_run = _spawn(work, ["spec.json"], env, timeout)
        except subprocess.TimeoutExpired:
            return done("error", f"timed out after {timeout:g} s: the Yosys process was stopped")
        except OSError as exc:
            return done("error", f"cannot start the Yosys process: {exc}")
        answer = _read_answer(work / "result.json")
        if answer is None:
            tail = (done_run.stderr or done_run.stdout or "").strip().splitlines()
            reason = tail[-1] if tail else f"exit code {done_run.returncode}"
            # The usual cause is that the child could not read the design. On macOS a
            # folder such as Documents is readable only with the user's permission,
            # which a process started by a daemon or by cron does not inherit.
            return done(
                "error",
                f"the Yosys process stopped without an answer: {reason}"
                " (check that this process may read the design's files)",
            )
        if answer[0] == "proved" and inert:
            complaint = _parameters_differ(gold, gate, module, params, work / "boxes.json")
            if complaint is not None:
                return done("error", complaint)
        if answer[0] == "proved" and (work / "report.wanted").exists():
            text, visible = _report(work, env)
            return replace(done("proved", f"{answer[1]}; {text}"), undefined_seen=visible)
        answered = done(answer[0], answer[1])
        if answer[0] == "error" and (work / "interface.flag").exists():
            return replace(answered, inert_refused=True)
        return answered


def _spawn(work: Path, args: list[str], env: Mapping[str, str], timeout: float) -> subprocess.CompletedProcess[str]:
    # -P: this file's folder is not put on the path (its modules could shadow the standard ones).
    # Nothing else is hidden, so the child sees the packages that `available()` saw.
    command = [sys.executable, "-P", str(Path(__file__).resolve()), *args]
    # S603: a fixed command (this interpreter, this file) with file names; no shell, no user text.
    return subprocess.run(  # noqa: S603
        command, cwd=work, env=dict(env), capture_output=True, text=True, timeout=timeout, check=False
    )


def _report(work: Path, env: Mapping[str, str]) -> tuple[str, bool]:
    """The report of undefined values of the gold, from a second process with its own time budget.

    Returns the text and whether an undefined value can be seen from outside the module (`_leads_outside`).
    It never changes the status: when it cannot be made, the text says so and the second value is True.
    """
    unknown = "could not tell whether the gold has undefined values"
    try:
        _spawn(work, ["spec.json", "report"], env, _REPORT_TIMEOUT)
        data = json.loads((work / "report.json").read_text(encoding="utf-8"))
        names = [str(n) for n in data["names"]]
        seen = bool(data["seen"])
    except (subprocess.TimeoutExpired, OSError, ValueError, KeyError, TypeError):
        return unknown, True
    return ("the gold has undefined values on " + ", ".join(names) if names else ""), seen


def _unknown_parameters(gold: Source, gate: Source, module: str, params: Mapping[str, str]) -> str | None:
    """None when every name in `params` is a parameter of `module` on both sides, else the complaint.

    The reader drops an unknown `-G` name without a word, so the names are checked with slang
    (the pyslang of `hdl_vet.slang_adapter`, which sees the parameters of the text), not with Yosys.
    """
    if not params:
        return None
    from hdl_vet import slang_adapter  # here and not at the top: the adapter loads pyslang, which is large

    for label, side in (("gold", gold), ("gate", gate)):
        try:
            known = slang_adapter.parameter_names(side.files, side.incdirs, side.defines, module)
        except Exception as exc:  # any refusal of the reader is an answer, not an exception
            return f"cannot read the parameters of {module} in the {label}: {exc}"
        for name in params:
            if name not in known:
                return f"{name} is not a parameter of {module} in the {label}"
    return None


def _check_defines(defines: Mapping[str, str | None]) -> None:
    if not isinstance(defines, Mapping):
        raise TypeError("defines must be a mapping of macro names to str or None values")
    for name, value in defines.items():
        if not isinstance(name, str):
            raise TypeError(f"a macro name must be a str, not {type(name).__name__}")
        if value is not None and not isinstance(value, str):
            raise TypeError(f"the value of the macro {name} must be a str or None, not {type(value).__name__}")


class _Refused(Exception):
    """Something of the sources that cannot be passed to the reader faithfully; the answer is an error."""


def _clean_params(params: Mapping[str, int | str] | None) -> dict[str, str]:
    if params is None:
        return {}
    if not isinstance(params, Mapping):
        raise TypeError("params must be a mapping of parameter names to int or str values")
    clean: dict[str, str] = {}
    for name, value in params.items():
        if not isinstance(name, str) or not _IDENT.match(name):
            raise ValueError(f"not a parameter name: {name!r}")
        if isinstance(value, bool) or not isinstance(value, int | str):
            raise TypeError(f"the value of parameter {name} must be an int or a str, not {type(value).__name__}")
        text = str(value)
        if not _VALUE.match(text):
            raise ValueError(f"the value of parameter {name} is not a plain constant: {text!r}")
        clean[name] = text
    return clean


def _clean_tie(tie: Mapping[str, int] | None) -> dict[str, int]:
    if tie is None:
        return {}
    if not isinstance(tie, Mapping):
        raise TypeError("tie must be a mapping of input port names to int values")
    clean: dict[str, int] = {}
    for name, value in tie.items():
        if not isinstance(name, str) or not _PORT_NAME.match(name):
            raise ValueError(f"not a port name: {name!r}")
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"the tie value of {name} must be an int, not {type(value).__name__}")
        if value < 0:
            raise ValueError(f"the tie value of {name} must not be negative")
        clean[name] = int(value)
    return clean


def _clean_inert(inert: Mapping[str, Sequence[str]] | None) -> dict[str, list[str]]:
    if inert is None:
        return {}
    if not isinstance(inert, Mapping):
        raise TypeError("inert must be a mapping of module names to lists of input names")
    clean: dict[str, list[str]] = {}
    for name, pins in inert.items():
        if not isinstance(name, str):
            raise TypeError(f"a module name must be a str, not {type(name).__name__}")
        if not _IDENT.match(name):
            raise ValueError(f"not a module name: {name!r}")
        if isinstance(pins, str) or not isinstance(pins, Sequence):
            raise TypeError(f"the inputs of {name} must be a list of names")
        for pin in pins:
            if not isinstance(pin, str):
                raise TypeError(f"an input name of {name} must be a str, not {type(pin).__name__}")
            if not _IDENT.match(pin):
                raise ValueError(f"not an input name: {pin!r}")
        if not pins:
            raise ValueError(f"inert {name} names no input: a child that has the same ports is `same`")
        clean[name] = sorted(set(pins))
    return dict(sorted(clean.items()))


def _parameters_differ(gold: Source, gate: Source, module: str, params: Mapping[str, str], boxes: Path) -> str | None:
    """None when each inert box has the same parameter values on the two sides, else the complaint.

    The netlist holds the parameters only as they were used, so the reader of `hdl_vet.slang_adapter` is
    asked, for the instances that the child found as boxes. A doubt is an answer, not a pass.
    """
    from hdl_vet import slang_adapter  # here and not at the top: the adapter loads pyslang, which is large

    try:
        wanted = [(str(m), str(p)) for m, p in json.loads(boxes.read_text(encoding="utf-8"))]
    except (OSError, ValueError, TypeError):
        return "cannot read the list of the inert children"
    names = frozenset(m for m, _ in wanted)
    found = {}
    for label, side in (("gold", gold), ("gate", gate)):
        try:
            found[label] = slang_adapter.instance_parameters(
                side.files, side.incdirs, side.defines, module, params, names
            )
        except Exception as exc:  # any refusal of the reader is an answer, not an exception
            return f"cannot read the parameter values of the inert children in the {label}: {exc}"
    for child, path in wanted:
        before, after = found["gold"].get(path), found["gate"].get(path)
        if before is None or after is None:
            return f"the instance {path} of {child} was not found by the reader of the design"
        if before != after:
            return f"the instance {path} of {child} has other parameter values in the gate than in the gold"
    return None


def _clean_same(same: Iterable[str]) -> list[str]:
    if isinstance(same, str) or not isinstance(same, Iterable):
        raise TypeError("same must be a collection of module names")
    names = list(same)
    for name in names:
        if not isinstance(name, str) or not _IDENT.match(name):
            raise ValueError(f"not a module name: {name!r}")
    return sorted(set(names))


def _one_line(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= _MAX_DETAIL else line[: _MAX_DETAIL - 3] + "..."


def _read_answer(path: Path) -> tuple[str, str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        status, detail = data["status"], data["detail"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if status not in ("proved", "refuted", "error") or not isinstance(detail, str):
        return None
    return status, detail


def _write_list(path: Path, side: Source, seen: Callable[[Path | str], str]) -> None:
    """A file list for read_slang's `-f`: quoted absolute paths with forward slashes (blanks survive)."""
    lines: list[str] = []
    for folder in side.incdirs:
        lines.append(f'-I "{seen(folder)}"')
    for name, value in side.defines.items():
        # Inside the double quotes of the reader's file only four things are special: the quote itself,
        # the backslash, `$` (an environment variable, dropped when unknown) and the line break.
        # The quote is escaped. The other three cannot be passed on, so the define is refused.
        if not _MACRO_NAME.match(name):
            raise _Refused(f"the define {name!r} has a name that Yosys cannot take (a plain identifier is needed)")
        if value is None:
            lines.append(f'-D "{name}"')
            continue
        if any(c in value for c in "\\$\n\r"):
            raise _Refused(f"the define {name} has a value with a backslash, $ or line break, which Yosys cannot take")
        lines.append('-D "{}={}"'.format(name, value.replace('"', '\\"')))
    lines.extend(f'"{seen(file)}"' for file in side.files)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _posix(path: Path | str) -> str:
    text = Path(path).resolve().as_posix()
    # Inside the quotes of the reader's file `$` starts an environment variable that is dropped
    # when it is unknown, and a backslash or a quote ends or escapes the text. None is passed on.
    if any(c in text for c in '"\\$\n\r'):
        raise _Refused(
            f"the path {text!r} has a character (a quote, backslash, $ or line break) that Yosys cannot take"
        )
    return text


class _Seen:
    """How the child sees a host path.

    On Windows the runtime mounts every drive under its own name, so a path is passed as it is.
    On POSIX it preopens the top-level folders of `/` under their own names, but then puts its own
    package folders and a fresh `/tmp` over them, so the host's `/tmp` (or a folder with the name
    of a package resource) would be invisible. The engine sets YOWASP_MOUNT instead: each top-level
    folder that a path needs is mounted at `/hostfs_<name>` (one path component: a deeper mountpoint is
    "Not supported" by the runtime), and every path given to Yosys is the host path with its first
    component renamed so. A whole top-level folder is mounted (not just the folder of
    a source) so that `include "../x.svh"` and include folders stay inside a mount.
    """

    def __init__(self) -> None:
        self.tops: set[str] = set()

    def __call__(self, path: Path | str) -> str:
        text = _posix(path)
        if os.name == "nt":
            return text
        parts = text.split("/")
        if len(parts) < 3 or not parts[1]:
            raise _Refused(f"the path {text!r} is not inside a top-level folder, which the Yosys sandbox needs")
        top = parts[1]
        if ":" in top or "=" in top or not os.access("/" + top, os.R_OK | os.X_OK) or not os.path.isdir("/" + top):
            raise _Refused(f"the path {text!r} is under a folder that the Yosys sandbox cannot be given")
        self.tops.add(top)
        return f"{_MOUNT}{top}" + text[len(top) + 1 :]


# --- the Yosys scripts (pure text, built from checked names) ---------------------------------------


def _read_lines(
    listing: str, module: str, params: Mapping[str, str], same: Sequence[str], hierarchy_json: str
) -> list[str]:
    """Read one side and make it plain logic: flops and gates, no memories.

    With `same`, those modules stay boxes and their pins become ports of the parent. The netlist
    before the flattening goes to `hierarchy_json`, to compare the boxes of the two sides. The netlist
    of the module with its boxes as cells (before `expose` makes ports of every wire) goes to `gold_w.json`
    or `gate_w.json`, for the walk of `_leads_outside`.
    """
    flags = " ".join(f"-G {name}={value}" for name, value in params.items())
    keep = " --keep-hierarchy" if same else ""
    lines = [f"read_slang -j 1{keep}{' ' + flags if flags else ''} -f {listing} --top {module}"]
    if same:
        # slang names the copy of a module for each instance `NAME$parent.instance`.
        patterns = " ".join(f"{name} {name}$*" for name in same)
        lines += [
            f"setattr -mod -set keep_hierarchy 1 {patterns}",
            f"write_json {hierarchy_json}",
            "flatten",
            f"write_json {listing.split('.')[0]}_w.json",
            f"expose -evert {module}",
            f"hierarchy -top {module}",
        ]
    # An unpacked array is a memory for the reader. Mapped to flops it is compared like any logic.
    # A memory that this cannot map stays a `$mem` cell, which the clock check refuses.
    lines += ["memory -nomap", "memory_map", "opt_clean"]
    if not same:
        lines.append(f"write_json {listing.split('.')[0]}_w.json")
    return lines


def ports_script(module: str, params: Mapping[str, str], same: Sequence[str]) -> str:
    """Read both sides once: write the netlist of each as JSON (for the checks) and as RTLIL (for the proof)."""
    lines: list[str] = []
    for side in ("gold", "gate"):
        lines += _read_lines(f"{side}.f", module, params, same, f"{side}_h.json")
        lines += [f"write_json {side}.json", f"write_rtlil {side}.il", "design -reset"]
    return "\n".join(lines) + "\n"


# Cells that `sat` has no model for (`equiv_simple` and the first step use it): specify cells and `$display`.
_NO_LOGIC = "delete t:$specify2 t:$specify3 t:$specrule t:$print"


def prepare_script(
    module: str,
    free: Mapping[str, int],
    tie: Mapping[str, tuple[int, int]],
    drop: Sequence[str],
) -> str:
    """Set the two sides up for the proof (step 2 of the child), on the netlists that step 1 wrote.

    `free`: inputs that only the gate has, with their widths, given to the gold side too.
    `tie`: inputs that only the gate has, as name -> (width, value), made constants in the gate.
    `drop`: outputs that only the gate has.
    The two sides go to `prepared.il`. A cleaned-up copy of them goes to `check.json`, for the
    clock check, which looks at the netlist with the ties applied.
    """
    lines = ["read_rtlil gold.il", f"rename {module} gold", "design -stash gold"]
    lines += ["read_rtlil gate.il", f"rename {module} gate", "design -stash gate"]
    lines += ["design -copy-from gold -as gold gold", "design -copy-from gate -as gate gate"]
    for name, width in free.items():
        lines.append(f"add -input {name} {width} gold")
    for name, (width, value) in tie.items():
        bits = format(value, "b").zfill(width)
        lines += ["cd gate", f"delete -port w:{name}", f"connect -set {name} {width}'b{bits}", "cd .."]
    lines += [f"delete -port gate/{name}" for name in drop]
    lines += [_NO_LOGIC, "write_rtlil prepared.il", "opt_expr", "opt_clean", "write_json check.json"]
    return "\n".join(lines) + "\n"


# A step is half a clock period or a pulse of a reset, so this looks back half as far in clock cycles.
_SEQ = 5


def proof_script() -> str:
    """The proof flow (step 4 of the child). `-undef`: an x of the gold is free, an x of the gate is a difference."""
    lines = [
        "read_rtlil prepared.il",
        "clk2fflogic",
        "equiv_make gold gate equiv",
        "hierarchy -top equiv",
        f"equiv_simple -undef -seq {_SEQ}",
        f"equiv_induct -undef -seq {_SEQ}",
        "equiv_status -assert",
    ]
    return "\n".join(lines) + "\n"


def first_script() -> str:
    """The first step (#139): every flop starts undefined, every input is defined, one step, outputs compared.

    The specify cells and `$print` are already out (`_NO_LOGIC`, in the prepare step). Any other cell that
    `sat` has no model for is an "error" of the run.
    """
    lines = [
        "read_rtlil prepared.il",
        "clk2fflogic",
        "miter -equiv -ignore_gold_x -flatten -make_outputs gold gate miter",
        "hierarchy -top miter",
        "sat -verify -prove trigger 0 -seq 1 -set-init-undef -set-def-inputs -enable_undef miter",
    ]
    return "\n".join(lines) + "\n"


_FIRST_OK = "SAT proof finished - no model found: SUCCESS!"
_FIRST_FAIL = "SAT proof finished - model found: FAIL!"


def _first_state() -> tuple[str, str] | None:
    """None when the first step proves the outputs equal; else the "error" or "refuted" answer."""
    code, log = _run_yosys(first_script(), "first")
    # shortcut: only the two Yosys sentences decide; a log with both, or neither, is an error.
    if _FIRST_FAIL in log and _FIRST_OK not in log:
        return "refuted", "differs in the first state of a flop (every flop starts undefined)"
    if code == 0 and _FIRST_OK in log and _FIRST_FAIL not in log:
        return None
    return "error", "the first state could not be checked: " + _problem(log)


def undefined_script(module: str) -> str:
    """The gold against itself: its undriven nets as 0 on one side and as free, independent bits on the other."""
    lines = ["read_rtlil gold.il", f"rename {module} gold", "setundef -undriven -zero", "design -stash gold"]
    lines += ["read_rtlil gold.il", f"rename {module} gate", "setundef -undriven -anyseq", "design -stash gate"]
    lines += ["design -copy-from gold -as gold gold", "design -copy-from gate -as gate gate", "clk2fflogic"]
    lines += [
        "equiv_make gold gate equiv",
        "hierarchy -top equiv",
        f"equiv_simple -seq {_SEQ}",
        f"equiv_induct -seq {_SEQ}",
    ]
    lines += ["equiv_status"]
    return "\n".join(lines) + "\n"


# --- the child: runs Yosys, decides, writes result.json --------------------------------------------


def _run_yosys(script: str, name: str) -> tuple[int, str]:
    """Run one script in this process; return the exit code and the log text."""
    # With YOWASP_MOUNT the working folder is not the sandbox's current folder, so each file
    # name is given in full, as the child sees it.
    prefix = os.environ.get("HDL_VET_HOSTFS", "")
    prefix = prefix + "/" if prefix else ""
    script = _FILES.sub(lambda m: prefix + m.group(0), script) if prefix else script
    Path(f"{name}.ys").write_text(script, encoding="utf-8")
    yosys = importlib.import_module("yowasp_yosys")
    code = yosys.run_yosys(["-q", "-l", f"{prefix}{name}.log", f"{prefix}{name}.ys"])
    try:
        log = Path(f"{name}.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        log = ""
    return int(code or 0), log


def _problem(log: str) -> str:
    """The first diagnostic of a failed run: the reader's own message with its place, else Yosys's ERROR line."""
    lines = log.splitlines()
    for line in lines:
        if ": ERROR: " in line or ": error: " in line:
            return line.strip()
    for line in lines:
        if line.startswith("ERROR: "):
            return line[len("ERROR: ") :].strip()
    return "Yosys failed without a message"


def _load(path: str, module: str) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        loaded = data["modules"][module]
        loaded["ports"], loaded["cells"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return loaded  # type: ignore[no-any-return]


def _ports_of(loaded: Mapping[str, Any]) -> dict[str, tuple[str, int]]:
    return {str(n): (str(p["direction"]), len(p["bits"])) for n, p in loaded["ports"].items()}


_STATE_CELLS = frozenset({"$ff", "$sr", "$anyinit", "$initstate"})
_FLOP = re.compile(r"\$[a-z]*dff[a-z0-9]*\Z")


def _clocks(loaded: Mapping[str, Any]) -> frozenset[str] | str:
    """The clocks of all flops of a module as `edge port[bit]` texts, or the reason they cannot be told.

    A clock is told only when it is a bit of an input port, reached through nothing but buffers.
    Anything else a flop could be clocked by (a gate, a constant, a register) is a derived clock.
    A latch, a memory or any other cell that holds state and is not a plain flop is refused, because
    the proof flow would not see its clock or its enable.
    """
    input_bits: dict[int, str] = {}
    for name, port in loaded["ports"].items():
        if port["direction"] == "input":
            for index, bit in enumerate(port["bits"]):
                if isinstance(bit, int):
                    input_bits[bit] = name if len(port["bits"]) == 1 else f"{name}[{index}]"
    source: dict[int, int] = {}  # a bit driven by a buffer -> the bit that feeds the buffer
    for cell in loaded["cells"].values():
        if cell["type"] in ("$buf", "$_BUF_"):
            for driven, feeding in zip(cell["connections"]["Y"], cell["connections"]["A"], strict=True):
                if isinstance(driven, int) and isinstance(feeding, int):
                    source[driven] = feeding
    found: set[str] = set()
    for cell_name, cell in loaded["cells"].items():
        kind = str(cell["type"])
        if not kind.startswith("$"):
            return f"the cell {cell_name} is an instance of {kind}, which is not looked into"
        if not _FLOP.match(kind):
            # `$_...` are gate-level cells, which the reader never makes.
            stateful = kind.startswith(("$mem", "$fsm", "$_")) or "latch" in kind or kind in _STATE_CELLS
            if stateful or any("CLK" in str(port) for port in cell["connections"]):
                return f"the cell {cell_name} is a {kind}: only flops are supported"
            continue
        clock = cell["connections"].get("CLK")
        bit = clock[0] if clock and len(clock) == 1 else None
        for _ in range(len(source) + 1):
            if bit not in source:
                break
            bit = source[bit]
        if bit not in input_bits:
            return f"the flop {cell_name} is clocked by something other than an input port"
        polarity = str(cell["parameters"].get("CLK_POLARITY", ""))
        if not polarity or set(polarity) - {"0", "1"}:
            return f"the flop {cell_name} has no stated clock edge"
        edge = "posedge" if polarity.endswith("1") else "negedge"
        found.add(f"{edge} {input_bits[bit]}")
    return frozenset(found)


def _clock_verdict(gold: Mapping[str, Any], gate: Mapping[str, Any]) -> tuple[str, str] | None:
    """None when the clocks are the same single clock (or there is no flop); else the answer."""
    sides = (_clocks(gold), _clocks(gate))
    for name, found in zip(("gold", "gate"), sides, strict=True):
        if isinstance(found, str):
            return "error", f"{name}: {found}"
    gold_clocks, gate_clocks = (s for s in sides if not isinstance(s, str))
    for name, found in zip(("gold", "gate"), (gold_clocks, gate_clocks), strict=True):
        if len(found) > 1:
            return "error", f"the {name} has flops on more than one clock ({', '.join(sorted(found))}): not supported"
    if gold_clocks and gate_clocks and gold_clocks != gate_clocks:
        was, now = next(iter(gold_clocks)), next(iter(gate_clocks))
        return "refuted", f"the clock differs: {was} in the gold, {now} in the gate"
    return None


_ASYNC_PINS = ("ARST", "SET", "CLR", "ALOAD")


def _clock_as_data(side: Mapping[str, Any]) -> str | None:
    """The name of an input that is the clock of a flop and also reaches a data pin of a flop (Z7), else None.

    The proof takes the data of a flop from the step before the edge, where the clock still has its old
    value, so a flop that reads its clock is not judged. The clock is an input that reaches a CLK pin through
    buffers alone (as in `_clocks`). Data pins are all pins of a flop but the clock, the output and the
    asynchronous ones. The walk goes forward through combinational cells and follows the netlist and not the
    logic. A child that is not a Yosys primitive is not entered.
    """
    reads: dict[int, list[int]] = {}  # an input bit of a combinational cell -> its output bits
    buffered: dict[int, list[int]] = {}  # the same, for buffers only
    clocks: set[int] = set()
    data: set[int] = set()
    for cell in side["cells"].values():
        kind = str(cell["type"])
        if not kind.startswith("$"):
            continue
        pins = {pin: [b for b in bits if isinstance(b, int)] for pin, bits in cell["connections"].items()}
        if _FLOP.match(kind):
            clocks.update(pins.get("CLK", []))
            data.update(b for pin, bits in pins.items() if pin not in ("CLK", "Q", *_ASYNC_PINS) for b in bits)
            continue
        directions = cell.get("port_directions", {})
        outs = [b for pin, bits in pins.items() if directions.get(pin) == "output" for b in bits]
        for pin, bits in pins.items():
            if directions.get(pin) != "output":
                for bit in bits:
                    reads.setdefault(bit, []).extend(outs)
                    if kind in ("$buf", "$_BUF_"):
                        buffered.setdefault(bit, []).extend(outs)

    def reach(start: int, graph: Mapping[int, list[int]]) -> set[int]:
        seen: set[int] = set()
        todo = [start]
        while todo:
            bit = todo.pop()
            if bit not in seen:
                seen.add(bit)
                todo += graph.get(bit, [])
        return seen

    for name, port in side["ports"].items():
        if port["direction"] != "input":
            continue
        for index, bit in enumerate(port["bits"]):
            if isinstance(bit, int) and reach(bit, buffered) & clocks and reach(bit, reads) & data:
                return name if len(port["bits"]) == 1 else f"{name}[{index}]"
    return None


def _decide_ports(
    gold: Mapping[str, tuple[str, int]], gate: Mapping[str, tuple[str, int]], tie: Mapping[str, int]
) -> tuple[tuple[str, str] | None, dict[str, int], dict[str, tuple[int, int]], list[str]]:
    """Return (an early answer or None, free inputs, ties, dropped outputs) for the two port lists."""
    only_gate_inputs = {n: w for n, (d, w) in gate.items() if n not in gold and d == "input"}
    for name in tie:
        if name not in only_gate_inputs:
            return ("error", f"tie {name}: not an input port that only the gate has"), {}, {}, []
    for name, (direction, _) in list(gold.items()) + list(gate.items()):
        if direction not in ("input", "output"):
            return ("error", f"port {name} is {direction}: not supported"), {}, {}, []
    for name, (direction, width) in gold.items():
        if name not in gate:
            return ("refuted", f"port {name} of the gold module is missing in the gate"), {}, {}, []
        gate_dir, gate_width = gate[name]
        if gate_dir != direction:
            return ("refuted", f"port {name} is an {direction} in the gold and an {gate_dir} in the gate"), {}, {}, []
        if gate_width != width:
            return ("refuted", f"port {name} is {width} bits wide in the gold and {gate_width} in the gate"), {}, {}, []
    for name in [n for n in gate if n not in gold]:
        if not _PORT_NAME.match(name):
            return ("error", f"the extra port {name} of the gate has a name that cannot be handled"), {}, {}, []
    ties: dict[str, tuple[int, int]] = {}
    for name, value in tie.items():
        width = only_gate_inputs[name]
        if value >= 1 << width:
            return ("error", f"tie {name}: the value {value} does not fit in {width} bits"), {}, {}, []
        ties[name] = (width, value)
    free = {n: w for n, w in only_gate_inputs.items() if n not in tie}
    drop = [n for n, (d, _) in gate.items() if n not in gold and d == "output"]
    return None, free, ties, drop


_UNPROVEN = re.compile(r"^\s*Unproven \$equiv \S+: \\(\S+?)_gold(?: \[\d+\])? ", re.MULTILINE)
_COUNT = re.compile(r"^\s*Found (\d+) \$equiv cells in equiv:", re.MULTILINE)
_FAILED = re.compile(r"Found (\d+) unproven \$equiv cells in 'equiv_status")


def _verdict(code: int, log: str, gold_ports: Mapping[str, tuple[str, int]]) -> tuple[str, str]:
    """Turn the log of the proof run into (status, detail). Only a complete, explicit proof is "proved"."""
    failed = _FAILED.search(log)
    if code != 0 and failed:
        names: list[str] = []
        for name in _UNPROVEN.findall(log):
            if name not in names:
                names.append(name)
        names.sort(key=lambda n: (n not in gold_ports, n))
        if names:
            return "refuted", "not proven equal: " + ", ".join(names[:5])
        return "refuted", f"{failed.group(1)} signals were not proven equal"
    if code != 0:
        return "error", _problem(log)
    counted = _COUNT.findall(log)
    outputs = sum(width for direction, width in gold_ports.values() if direction == "output")
    if "Equivalence successfully proven!" not in log or not counted or int(counted[-1]) < max(outputs, 1):
        return "error", "Yosys finished without a proof and without a counter-example"
    return "proved", f"{counted[-1]} signals proven equal"


def _strip_src(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_src(v) for k, v in value.items() if k != "src"}
    if isinstance(value, list):
        return [_strip_src(v) for v in value]
    return value


def _colours(
    cells: Sequence[Mapping[str, Any]], bodies: Sequence[str], named: Mapping[int, tuple[str, int]]
) -> dict[int, int]:
    """A colour for each bit that depends on the names and the neighbourhood of the bit, not on its number.

    A named bit has its name and index. The colour of any other bit is made again a few times from the
    cells that touch it (the type, the parameters and the colours of the other bits of the cell), so that
    thousands of cells of one kind with no names are told apart by what they connect. The colours are
    hashes of this process, so they are only comparable inside it (both sides are read in one process).
    """
    colour: dict[int, int] = {b: hash((n, i)) for b, (n, i) in named.items()}
    for _ in range(4):
        touching: dict[int, list[tuple[int, str, int]]] = {}
        for cell, body in zip(cells, bodies, strict=True):
            wires = cell.get("connections", {})
            signature = hash(
                (
                    body,
                    tuple(
                        (p, tuple(colour.get(b, 0) if isinstance(b, int) else b for b in bits))
                        for p, bits in sorted(wires.items())
                    ),
                )
            )
            for port, bits in wires.items():
                for index, value in enumerate(bits):
                    if isinstance(value, int) and value not in named:
                        touching.setdefault(value, []).append((signature, port, index))
        colour.update({b: hash(tuple(sorted(items))) for b, items in touching.items()})
    return colour


def _renumbered(module: Mapping[str, Any]) -> str:
    """One module as a string that does not depend on the integer ids that the reader gave to its bits.

    The bits are numbered again by a walk: the ports in name order, the public nets in name order, then
    the cells in the order of a key that holds no bit id (type, parameters, and for each connection the
    constants and the public names it touches). Two modules give the same string only when one numbering
    maps the one onto the other, so a changed connection, cell or name still shows.
    """
    ids: dict[int, int] = {}

    def bit(value: Any) -> Any:
        if not isinstance(value, int):
            return value
        return ids.setdefault(value, len(ids))

    ports = module.get("ports", {})
    nets = {n: v for n, v in module.get("netnames", {}).items() if not n.startswith("$")}
    named: dict[int, tuple[str, int]] = {}
    for name in sorted(ports):
        for index, value in enumerate(ports[name]["bits"]):
            if isinstance(value, int):
                named.setdefault(value, (name, index))
    for name in sorted(nets):
        for index, value in enumerate(nets[name]["bits"]):
            if isinstance(value, int):
                named.setdefault(value, (name, index))

    cells = list(module.get("cells", {}).values())
    bodies = [json.dumps(_strip_src({k: v for k, v in c.items() if k != "connections"}), sort_keys=True) for c in cells]
    colour = _colours(cells, bodies, named)

    def shape(value: Any) -> Any:
        return colour.get(value, 0) if isinstance(value, int) else value

    keyed = []
    for cell, body in zip(cells, bodies, strict=True):
        wires = {p: [shape(b) for b in bits] for p, bits in cell.get("connections", {}).items()}
        keyed.append((json.dumps([body, wires], sort_keys=True), cell))
    keyed.sort(key=lambda item: item[0])
    out_ports = {
        n: [_strip_src({k: v for k, v in ports[n].items() if k != "bits"}), [bit(b) for b in ports[n]["bits"]]]
        for n in sorted(ports)
    }
    out_nets = {
        n: [[bit(b) for b in nets[n]["bits"]], _strip_src(nets[n].get("attributes", {})), nets[n].get("signed", 0)]
        for n in sorted(nets)
    }
    out_cells = []
    for key, cell in keyed:
        wires = {p: [bit(b) for b in cell["connections"][p]] for p in sorted(cell.get("connections", {}))}
        out_cells.append([key, wires])
    memories = sorted(json.dumps(_strip_src(m), sort_keys=True) for m in module.get("memories", {}).values())
    return json.dumps([out_ports, out_nets, out_cells, memories], sort_keys=True)


def _plain(module: Mapping[str, Any]) -> str:
    """One module as a string that keeps the numbers of the bits: cheap, and equal for most identical children."""
    cells = sorted(json.dumps(_strip_src(c), sort_keys=True) for c in module.get("cells", {}).values())
    nets = {
        n: [v["bits"], _strip_src(v.get("attributes", {})), v.get("signed", 0)]
        for n, v in module.get("netnames", {}).items()
        if not n.startswith("$")
    }
    memories = sorted(json.dumps(_strip_src(m), sort_keys=True) for m in module.get("memories", {}).values())
    return json.dumps([_strip_src(module.get("ports")), cells, nets, memories], sort_keys=True)


def _below(modules: Mapping[str, Any], start: str) -> list[str]:
    """The names of a module and of every module below it that the netlist holds."""
    seen: list[str] = []
    todo = [start]
    while todo:
        name = todo.pop()
        if name in seen or name not in modules:
            continue
        seen.append(name)
        todo += [str(c["type"]) for c in modules[name].get("cells", {}).values()]
    return seen


def _same_below(gold: Mapping[str, Any], gate: Mapping[str, Any], start: str) -> bool:
    """True when a module and everything below it is the same on the two sides.

    Generated names of cells, the source places and (in the second look) the numbers of the bits are left
    out. Ports, public net names and the cells (type, parameters, connections) stay. A module that is not
    equal as it is gets the second look, which numbers its bits again.
    """
    names = _below(gold, start)
    if names != _below(gate, start) and sorted(names) != sorted(_below(gate, start)):
        return False
    for name in names:
        if _plain(gold[name]) != _plain(gate[name]) and _renumbered(gold[name]) != _renumbered(gate[name]):
            return False
    return True


def _boxes(modules: Mapping[str, Any], same: Sequence[str]) -> dict[tuple[str, str], str]:
    """(module, instance name) -> module of the instance, for every instance of a `same` module."""
    boxes: dict[tuple[str, str], str] = {}
    for name, module in modules.items():
        for cell_name, cell in module.get("cells", {}).items():
            kind = str(cell["type"])
            if any(kind == s or kind.startswith(s + "$") for s in same):
                boxes[(name, cell_name)] = kind
    return boxes


def _hierarchies() -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The netlists before the flattening, of the gold and of the gate, or None when they cannot be read."""
    try:
        gold = json.loads(Path("gold_h.json").read_text(encoding="utf-8"))["modules"]
        gate = json.loads(Path("gate_h.json").read_text(encoding="utf-8"))["modules"]
    except (OSError, ValueError, KeyError):
        return None
    return gold, gate


def _boxes_differ(gold: Mapping[str, Any], gate: Mapping[str, Any], same: Sequence[str]) -> str | None:
    """None when each instance that is not looked into is the same on both sides, else the complaint."""
    before, after = _boxes(gold, same), _boxes(gate, same)
    for key in sorted(set(before) & set(after)):
        if before[key] != after[key]:
            return f"the instance {key[1]} is of {before[key]} in the gold and of {after[key]} in the gate"
    for kind in sorted(set(before.values()) & set(after.values())):
        if not _same_below(gold, gate, kind):
            names = sorted({key[1] for key, value in before.items() if value == kind})
            return f"the child {', '.join(names)} is named in same but is not the same on the two sides"
    return None


def _initial_value(side: Mapping[str, Any]) -> str | None:
    for net, info in side.get("netnames", {}).items():
        if "init" in info.get("attributes", {}):
            return str(net)
    return None


def _decide(spec: Mapping[str, Any]) -> tuple[str, str]:
    module: str = spec["module"]
    params: dict[str, str] = spec["params"]
    same: list[str] = spec["same"]
    inert: dict[str, list[str]] = spec.get("inert", {})
    code, log = _run_yosys(ports_script(module, params, sorted({*same, *inert})), "ports")
    if code != 0:
        return "error", _problem(log)
    gold_side = _load("gold.json", module)
    gate_side = _load("gate.json", module)
    if gold_side is None or gate_side is None:
        return "error", f"module {module} was not found"
    for label, side in (("gold", gold_side), ("gate", gate_side)):
        net = _initial_value(side)
        if net is not None:
            return "error", f"the {label} has an initial value on {net}: the proof does not model initial values"
    pins: list[tuple[str, str]] = []
    if same or inert:
        hierarchies = _hierarchies()
        if hierarchies is None:
            return "error", "cannot read the children named in same or inert"
        if same:
            complaint = _boxes_differ(*hierarchies, same)
            if complaint is not None:
                return "error", complaint
        if inert:
            try:
                pins, boxes = equiv_inert.interfaces(*hierarchies, module, same, inert)
            except equiv_inert.Refusal as refusal:
                if refusal.interface:
                    Path("interface.flag").write_text("", encoding="utf-8")
                return "error", str(refusal)
            Path("boxes.json").write_text(json.dumps(boxes), encoding="utf-8")
    gold_ports, gate_ports = _ports_of(gold_side), _ports_of(gate_side)
    early, free, ties, drop = _decide_ports(gold_ports, gate_ports, spec["tie"])
    if early is not None:
        return early
    problem = equiv_inert.pin_problem(gate_side, pins, ties)
    if problem is not None:
        return "error", problem
    code, log = _run_yosys(prepare_script(module, free, ties, drop), "prepare")
    if code != 0:
        return "error", _problem(log)
    # The clocks come first (a module that cannot be judged costs no proof), and they are read after
    # the ties and a clean-up: a clock mux with a tied select is its input.
    gold_clean, gate_clean = _load("check.json", "gold"), _load("check.json", "gate")
    if gold_clean is None or gate_clean is None:
        return "error", "the netlist for the clock check is missing"
    clock_answer = _clock_verdict(gold_clean, gate_clean)
    if clock_answer is not None:
        return clock_answer
    for side in (gold_clean, gate_clean):
        read = _clock_as_data(side)
        if read is not None:
            return "error", f"the clock {read} is read as data by a flop: the check cannot judge it; give a waiver"
    code, log = _run_yosys(proof_script(), "proof")
    status, detail = _verdict(code, log, gold_ports)
    if status == "proved":
        first = _first_state()
        if first is not None:
            return first
    if status == "proved" and (_has_undefined(gold_side) or _undefined_sources(gold_side)):
        Path("report.wanted").write_text("", encoding="utf-8")  # the parent runs the report, with its own budget
    return status, detail


def _has_undefined(side: Mapping[str, Any]) -> bool:
    """True when the netlist has an x constant or a used bit that nothing drives."""
    driven: set[int] = set()
    used: set[int] = set()
    for port in side["ports"].values():
        bits = {b for b in port["bits"] if isinstance(b, int)}
        (driven if port["direction"] == "input" else used).update(bits)
        if port["direction"] != "input" and any(b in ("x", "z") for b in port["bits"]):
            return True
    for cell in side["cells"].values():
        directions = cell.get("port_directions", {})
        for name, bits in cell["connections"].items():
            if directions.get(name) == "input":
                if any(b in ("x", "z") for b in bits):
                    return True
                used.update(b for b in bits if isinstance(b, int))
            else:
                driven.update(b for b in bits if isinstance(b, int))
    return bool(used - driven)


def _undefined_names(module: str) -> list[str] | None:
    """The nets of the gold that depend on an undefined value, found by a proof of the gold against itself.

    None when the report could not be made.
    """
    code, log = _run_yosys(undefined_script(module), "undefined")
    return None if code != 0 else list(dict.fromkeys(_UNPROVEN.findall(log)))


def _wide_selects(side: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The shift cells that can address past their data input, by the widths alone.

    A select with a variable index (`v[i +: 1]`) is a `$shiftx` with data of w bits, an index of k bits and a
    result of r bits. It is in range when an unsigned index gives (2**k - 1) + r <= w. A signed index is not.
    Constant 0 bits on top of the index are no index bits (the reader puts one on an unsigned index to make it signed).
    """
    found = []
    for cell in side["cells"].values():
        if cell.get("type") not in ("$shiftx", "$shift"):
            continue
        params = cell.get("parameters", {})
        try:
            width, result = (int(str(params[name]), 2) for name in ("A_WIDTH", "Y_WIDTH"))
            index = list(cell["connections"]["B"])
        except (KeyError, ValueError):
            found.append(cell)  # not understood: it counts
            continue
        # The reader gives an unsigned index as a signed one with a constant 0 on top: that bit is no index bit.
        signed = str(params.get("B_SIGNED", "1")).strip("0") != ""
        while index and index[-1] in ("0", 0):
            index.pop()
            signed = False
        if signed or (1 << len(index)) - 1 + result > width:
            found.append(cell)
    return found


def _zero_divisors(side: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The division and modulo cells whose divisor can be zero, by the netlist alone.

    The divisor cannot be zero only when one of its bits is the constant 1 (a constant bit is the text "1").
    Every other divisor counts, and so does a cell that is not understood.
    """
    return [
        cell
        for cell in side["cells"].values()
        if cell.get("type") in ("$div", "$mod", "$divfloor", "$modfloor")
        and "1" not in cell.get("connections", {}).get("B", ())
    ]


def _undefined_sources(side: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The cells whose result is undefined for some input that the report cannot see: wide selects and zero divisors."""
    return _wide_selects(side) + _zero_divisors(side)


def _leads_outside(side: Mapping[str, Any], names: Iterable[str]) -> bool:
    """True when an undefined value of the gold can be seen from outside the module, or when that cannot be told.

    The start bits are the bits of the nets that the report names and the outputs of the selects of
    `_undefined_sources` (a select that can leave its range, a division or modulo whose divisor can be zero).
    The walk goes forward through every cell, flops and memories too (an input bit leads to all
    the output bits), any number of steps. It reaches the outside at a bit of an output or inout port, or at an
    input pin of a cell that is not a Yosys primitive (a child that stayed a black box, wherever it is
    in the hierarchy). A constant `x` or `z` bit on such a port or pin counts at once (`n / 4'd0` is folded to it).
    A named net that is not in the netlist counts as seen. The walk follows the netlist and not
    the logic: a value that other logic masks still counts.
    """
    netnames = side.get("netnames", {})
    todo: list[int] = []
    for name in names:
        info = netnames.get(name)
        if info is None:
            return True
        todo += [b for b in info["bits"] if isinstance(b, int)]
    reads: dict[int, list[int]] = {}  # input bit -> the output bits of its cell
    box_pins: set[int] = set()
    for cell in side["cells"].values():
        directions = cell.get("port_directions", {})
        ins: list[int] = []
        outs: list[int] = []
        for pin, bits in cell["connections"].items():
            ints = [b for b in bits if isinstance(b, int)]
            direction = directions.get(pin, "inout")
            if direction != "output":
                ins += ints
            if direction != "input":
                outs += ints
        if not str(cell.get("type", "")).startswith("$"):
            box_pins.update(ins)
            for pin, bits in cell["connections"].items():
                if directions.get(pin, "inout") != "output" and any(b in ("x", "z") for b in bits):
                    return True  # a constant undefined bit on a box pin
        for bit in ins:
            reads.setdefault(bit, []).extend(outs)
    for cell in _undefined_sources(side):
        todo += [b for pin, bits in cell["connections"].items() if pin == "Y" for b in bits if isinstance(b, int)]
    if any(b in ("x", "z") for port in side["ports"].values() if port["direction"] != "input" for b in port["bits"]):
        return True  # a constant undefined bit on an output port
    visible = {
        b for port in side["ports"].values() if port["direction"] != "input" for b in port["bits"] if isinstance(b, int)
    }
    seen: set[int] = set()
    while todo:
        bit = todo.pop()
        if bit in seen:
            continue
        seen.add(bit)
        if bit in visible or bit in box_pins:
            return True
        todo += reads.get(bit, [])
    return False


def _report_child(spec_path: str) -> int:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    gold_side = _load("gold.json", spec["module"])
    walk_side = _load("gold_w.json", spec["module"])
    if gold_side is None or walk_side is None:
        return 1
    outputs = [name for name, (direction, _) in _ports_of(gold_side).items() if direction == "output"]
    found = _undefined_names(spec["module"])
    # Not examined: name every output, which is the safe side. Else the outputs among the named nets.
    names = outputs if found is None else [n for n in found if n in outputs]
    seen = found is None or _leads_outside(walk_side, found)
    Path("report.json").write_text(json.dumps({"names": names, "seen": seen}), encoding="utf-8")
    return 0


def _child(spec_path: str) -> int:
    try:
        spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
        status, detail = _decide(spec)
    except BaseException as exc:  # a trap or crash of the tool is an answer, not an exception
        status, detail = "error", f"Yosys crashed: {type(exc).__name__}: {exc}"
    Path("result.json").write_text(json.dumps({"status": status, "detail": detail}), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(_report_child(sys.argv[1]) if sys.argv[2:] == ["report"] else _child(sys.argv[1]))
