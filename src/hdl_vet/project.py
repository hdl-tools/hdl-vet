"""The project file `hdl-vet.toml` and `vet init`, which writes a starter project.

A project file names the design (file list, top module, base), the scripts to run and the output
directory, so that `vet apply` with no arguments runs it. Reading is strict: nothing is guessed, and
an unknown table or key, a value of the wrong type or an empty string is an error that names the
file. Writing is done by hand (the standard library reads TOML but does not write it).
"""

from __future__ import annotations

import os
import shutil
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hdl_vet.paths import within

if TYPE_CHECKING:  # reading a project file needs no design: pyslang is loaded only by `init_project`
    from hdl_vet.slang_adapter import InstanceInfo

__all__ = ["PROJECT_FILE", "Project", "ProjectError", "init_project", "load_project"]

PROJECT_FILE = "hdl-vet.toml"
_EXAMPLE = "scripts/01_example.py"
_OUTPUT = "out"


class ProjectError(Exception):
    """A project file that cannot be used, or a project that `init` refuses to write."""


@dataclass(frozen=True)
class Project:
    """A project file as read: every path absolute and resolved, None where the file gives none."""

    path: Path
    filelist: Path | None
    top: str | None
    base: Path | None
    scripts: tuple[Path, ...]
    output: Path | None


# --- reading ------------------------------------------------------------------------------------


def _string(where: str, table: str, key: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ProjectError(f"{where}: [{table}] {key}: expected a string")
    if not value:
        raise ProjectError(f"{where}: [{table}] {key}: must not be empty")
    if "\x00" in value:
        raise ProjectError(f"{where}: [{table}] {key}: must not contain a NUL character")
    return value


def _table(where: str, data: dict[str, Any], name: str, keys: tuple[str, ...]) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        kind = "an array of tables" if isinstance(value, list) else "not a table"
        raise ProjectError(f"{where}: {name!r} must be a table [{name}], but it is {kind}")
    for key, item in value.items():
        if key not in keys:
            if isinstance(item, dict):
                raise ProjectError(f"{where}: [{name}.{key}]: nested tables are not allowed")
            raise ProjectError(f"{where}: [{name}] unknown key {key!r} (expected {', '.join(keys)})")
    return value


def _resolve(where: str, directory: Path, text: str, what: str) -> Path:
    try:
        return (directory / text).resolve()
    except (OSError, ValueError) as e:
        raise ProjectError(f"{where}: {what}: {text!r} is not a usable path ({e})") from e


def load_project(path: Path | str) -> Project:
    """Read and check a project file; relative paths in it resolve against its directory."""
    file = Path(path).resolve()
    where = str(file)
    try:
        raw = file.read_bytes()
    except OSError as e:
        raise ProjectError(f"{where}: cannot read the project file ({e})") from e
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ProjectError(f"{where}: not UTF-8 text ({e})") from e
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ProjectError(f"{where}: not valid TOML ({e})") from e
    except RecursionError as e:
        raise ProjectError(f"{where}: not valid TOML (nested too deeply)") from e
    for name in data:
        if name not in ("design", "apply"):
            kind = "table" if isinstance(data[name], dict) else "key"
            raise ProjectError(f"{where}: unknown {kind} {name!r} (expected the tables [design] and [apply])")
    design = _table(where, data, "design", ("filelist", "top", "base"))
    apply = _table(where, data, "apply", ("scripts", "output"))
    here = file.parent

    if "base" in design and "filelist" not in design:
        raise ProjectError(f"{where}: [design] base: a base belongs to a file list, but there is no filelist")
    filelist = base = output = top = None
    if "filelist" in design:
        filelist = _resolve(where, here, _string(where, "design", "filelist", design["filelist"]), "[design] filelist")
    if "top" in design:
        top = _string(where, "design", "top", design["top"])
    if "base" in design:
        base = _resolve(where, here, _string(where, "design", "base", design["base"]), "[design] base")
    scripts: list[Path] = []
    if "scripts" in apply:
        listed = apply["scripts"]
        if not isinstance(listed, list):
            raise ProjectError(f"{where}: [apply] scripts: expected an array of strings")
        for item in listed:
            scripts.append(_resolve(where, here, _string(where, "apply", "scripts", item), "[apply] scripts"))
    if "output" in apply:
        output = _resolve(where, here, _string(where, "apply", "output", apply["output"]), "[apply] output")
    return Project(file, filelist, top, base, tuple(scripts), output)


# --- writing ------------------------------------------------------------------------------------


def _escape(text: str, hexform: str) -> str:
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif code < 0x20 or code == 0x7F:
            out.append(hexform.format(code))
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _toml_string(text: str) -> str:
    """A TOML basic string: escapes for backslash, quote and control characters, the rest as it is."""
    return _escape(text, "\\u{:04X}")


def _py_string(text: str) -> str:
    """A double-quoted Python string literal."""
    return _escape(text, "\\x{:02x}")


_MAX_UP = 3  # a path is written relative to the project only while that takes at most this many ".." steps
_MAX_TOPS = 10


def _relative(path: Path, directory: Path) -> str:
    """`path` relative to `directory` with "/" separators; absolute when that is far or impossible (another drive)."""
    try:
        rel = Path(os.path.relpath(path, directory))
    except ValueError:
        return path.as_posix()
    ups = 0
    for part in rel.parts:
        if part != "..":
            break
        ups += 1
    return path.as_posix() if ups > _MAX_UP else rel.as_posix()


def _only_top(filelist: Path, base: Path | None) -> str:
    """The design's only top-level module. Looked up without a top, and before errors are looked for."""
    from hdl_vet.slang_adapter import load

    snap = load(filelist, top=None, base=base).snapshot()
    below: dict[str, int] = {}
    for key, item in snap.items():
        if "." not in key:
            count = sum(1 for other in snap if other.startswith(key + "."))
            below[item.module] = max(below.get(item.module, 0), count)
    if not below:
        raise ProjectError(f"{filelist}: the design has no top-level module; name one with --top")
    if len(below) > 1:
        ranked = sorted(below, key=lambda name: (-below[name], name))  # the biggest hierarchy first
        shown = ", ".join(ranked[:_MAX_TOPS])
        more = f" and {len(ranked) - _MAX_TOPS} more" if len(ranked) > _MAX_TOPS else ""
        raise ProjectError(f"{filelist}: the design has several top-level modules ({shown}{more}); use --top")
    return next(iter(below))


def _listed(instances: list[InstanceInfo], interfaces: frozenset[str]) -> list[tuple[InstanceInfo, str | None]]:
    """Each instance name once, with the reason the operations cannot edit it (None when they can)."""
    out: list[tuple[InstanceInfo, str | None]] = []
    seen: set[str] = set()
    for inst in instances:
        if inst.name in seen:
            continue
        seen.add(inst.name)
        note = None
        if inst.module in interfaces:
            note = "an interface instance"
        elif inst.is_array:
            note = "an instance array"
        elif inst.in_generate:
            note = "in a generate block"
        elif inst.from_macro:
            note = "from a macro"
        out.append((inst, note))
    return out


def _example_script(top: str, instances: list[InstanceInfo], interfaces: frozenset[str]) -> str:
    q = _py_string
    listed = _listed(instances, interfaces)
    lines = [
        '"""Starter edit script. It changes nothing yet: put your edits in apply().',
        "",
        "Run `vet apply --dry-run` to see what a script does, `vet apply` to write the verified copy.",
        '"""',
        "",
        "",
        "def apply(vet):",
        f"    top = vet.module({q(top)})  # the top-level module of the design",
        "",
    ]
    if not listed:
        lines.append(f"    # {q(top)} instantiates nothing.")
    else:
        lines.append(f"    # {q(top)} instantiates:")
        for inst, note in listed:
            suffix = "" if note is None else f"  ({note}: the operations cannot edit it)"
            lines.append(f"    #   {q(inst.name)}: {q(inst.module)}{suffix}")
    lines += [
        "    #",
        "    # Examples; remove the leading `# ` of one to use it:",
        '    #   top.add_port("dft_en", "input")',
        '    #   top.add_wire("spare_wire", 4)',
    ]
    editable = next((inst for inst, note in listed if note is None), None)
    if editable is not None:
        ref = f"top.instance({q(editable.name)})"
        port = next((c.port for c in editable.connections if c.port), None)
        lines += [
            f"    #   {ref}.connect({q(port or 'port')}, {q('expression')})",
            f"    #   {ref}.set_param({q('PARAM')}, 8)",
            f"    #   {ref}.replace_module({q('other_module')})",
            f"    #   top.remove_instance({q(editable.name)})",
        ]
    lines += [
        "    #",
        "    # A file that is not part of the design is named relative to this script, so that the script",
        "    # works from any directory (put `from pathlib import Path` at the top):",
        '    #   vet.add_file(Path(__file__).parent.parent / "cells" / "NAME.sv")',
    ]
    return "\n".join(lines) + "\n"


def _project_text(filelist: str, top: str, base: str | None) -> str:
    lines = [
        "# hdl-vet project. Paths are relative to this file.",
        "[design]",
        f"filelist = {_toml_string(filelist)}",
        f"top = {_toml_string(top)}",
    ]
    if base is not None:
        lines.append(f"base = {_toml_string(base)}")
    lines += [
        "",
        "[apply]",
        f"scripts = [{_toml_string(_EXAMPLE)}]",
        f"output = {_toml_string(_OUTPUT)}",
    ]
    return "\n".join(lines) + "\n"


def _readme_text(filelist: str, top: str) -> str:
    return (
        "# hdl-vet project\n"
        "\n"
        f"Design: top module `{top}`, file list `{filelist}`.\n"
        f"`{PROJECT_FILE}` names the design, the scripts to run (in order) and the output directory.\n"
        "Run these commands in this directory.\n"
        "\n"
        "    vet apply --dry-run                     show the diff; write nothing\n"
        f"    vet apply                               run the scripts and write the verified copy to {_OUTPUT}/\n"
        f"    vet apply --force                       the same, replacing {_OUTPUT}/ left by an earlier run\n"
        "    vet apply --save work.session.zip       run the scripts and save the session\n"
        "    vet apply --resume work.session.zip     continue from a saved session (add scripts with -s)\n"
        "\n"
        "`vet apply` executes the project's scripts, also with `--dry-run`: run it only in a project you trust.\n"
        "\n"
        f"Edit `{_EXAMPLE}` or add scripts to `scripts` in `{PROJECT_FILE}`.\n"
        "A script defines `apply(vet)`. `-s FILE` on the command line adds scripts after the project's.\n"
        "Files that a script adds with `vet.add_file` go in `cells/`.\n"
    )


_CELLS_README = (
    "# cells\n"
    "\n"
    "Put the files that scripts add to the design here (for example a hand-written cell), and add them\n"
    "in a script with `vet.add_file`. Name the file relative to the script, not to the current directory,\n"
    "so that the project also runs with `vet apply --project FILE` from elsewhere:\n"
    "\n"
    "    from pathlib import Path\n"
    "\n"
    '    vet.add_file(Path(__file__).parent.parent / "cells" / "NAME.sv")\n'
)

_GITIGNORE = f"/{_OUTPUT}/\n*.session.zip\n"


def _wide_tree_hint(session: Any) -> str:
    """Why the input tree is so wide, when the file list is the reason (it is not with the sources)."""
    sources = [*session.files, *session.filelist.incdirs]
    try:
        narrow = Path(os.path.commonpath([str(p) for p in sources]))
    except ValueError:
        return ""
    where = session.filelist_path.parent
    if within(where, narrow) or within(narrow, where):
        return ""
    return (
        f" The tree is that wide because the file list is not with the sources (which are under {narrow}): "
        "use --base with the directory the sources are relative to."
    )


def init_project(
    directory: Path | str, filelist: Path | str, top: str | None = None, base: Path | str | None = None
) -> list[Path]:
    """Write a starter project into `directory`; the files written, in order.

    ProjectError for its own refusals (not empty, inside the input tree, several tops); errors of
    the file list or the design keep their own types. The project appears complete or not at all:
    the files are written next to `directory` and renamed into place (an `OSError` while writing
    propagates, after the temporary directory is removed).
    """
    from hdl_vet.session import Session

    root = Path(directory).resolve()
    fl = Path(filelist).resolve()
    base_path = None if base is None else Path(base).resolve()
    if root.exists():
        if not root.is_dir():
            raise ProjectError(f"{directory}: exists and is not a directory")
        try:
            used = any(root.iterdir())
        except OSError as e:
            raise ProjectError(f"{directory}: cannot be listed ({e})") from e
        if used:
            raise ProjectError(f"{directory}: exists and is not empty")
    chosen = top if top is not None else _only_top(fl, base_path)
    session = Session(fl, top=chosen, base=base_path)
    if within(root, session.layout_root):
        raise ProjectError(
            f"{directory}: is inside the design's input tree {session.layout_root}; "
            f"a regenerated design would delete it.{_wide_tree_hint(session)}"
        )
    design = session.loaded
    instances = list(design.module(chosen).instances)

    fl_text = _relative(fl, root)
    base_text = None if base_path is None else _relative(base_path, root)
    texts = [
        (PROJECT_FILE, _project_text(fl_text, chosen, base_text)),
        (_EXAMPLE, _example_script(chosen, instances, design.interfaces())),
        ("cells/README.md", _CELLS_README),
        ("README.md", _readme_text(fl_text, chosen)),
        (".gitignore", _GITIGNORE),
    ]
    try:
        files = [(name, text.encode("utf-8")) for name, text in texts]
    except UnicodeEncodeError as e:
        raise ProjectError(f"{directory}: a name in the project cannot be written as UTF-8 ({e})") from e

    tmp = root.parent / f".hdl-vet-init-{uuid.uuid4().hex}"
    try:
        root.parent.mkdir(parents=True, exist_ok=True)
        tmp.mkdir()
        for name, data in files:
            dest = tmp / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        if root.exists():  # an empty directory: the entries move in
            moved: list[Path] = []
            try:
                for child in sorted(tmp.iterdir()):
                    os.replace(child, root / child.name)
                    moved.append(root / child.name)
            except BaseException:
                for done in moved:
                    if done.is_dir():
                        shutil.rmtree(done, ignore_errors=True)
                    else:
                        done.unlink(missing_ok=True)
                raise
            tmp.rmdir()
        else:
            os.rename(tmp, root)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return [root / name for name, _ in files]
