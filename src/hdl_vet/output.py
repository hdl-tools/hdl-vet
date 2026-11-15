"""Write the output tree of a signed-off session.

The tree mirrors the input layout (everything under `Session.layout_root`), with
changed files replaced and the file list flattened, plus a `hdl-vet.json`
manifest. A top-level file list outside `layout_root` is written at the top of the
tree under its own name. It is built next to its destination and renamed into place, so a
failure never leaves a partial output directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

from hdl_vet import __version__
from hdl_vet.session import Result, Session

__all__ = ["OutputError", "write_tree"]


class OutputError(Exception):
    """Raised when the output tree cannot or must not be written."""


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _rel(path: Path, start: Path) -> str:
    return Path(os.path.relpath(path, start)).as_posix()


def _filelist_text(session: Session, here: Path, added: Sequence[str] = ()) -> str:
    """The flattened file list; `here` is the directory it will sit in (input coordinates).

    `added` are the output names of the files a script added: one line each, last.
    """
    fl = session.filelist
    lines = [f"+incdir+{_rel(d, here)}" for d in fl.incdirs]
    for name, value in fl.defines.items():
        lines.append(f"+define+{name}" if value is None else f"+define+{name}={value}")
    lines.extend(_rel(f, here) for f in fl.files)
    lines.extend(_rel(session.layout_root / name, here) for name in added)
    return "\n".join(lines) + "\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_tree(
    session: Session,
    result: Result,
    outdir: Path,
    scripts: Sequence[tuple[str, str]] = (),
    force: bool = False,
) -> Path:
    if not result.ok:
        raise OutputError("refusing to write: the sign-off did not pass")
    out = Path(outdir)
    tmp: Path | None = None
    backup: Path | None = None
    try:
        target = out.resolve()
        root = session.layout_root
        if _is_within(target, root) or _is_within(root, target):
            raise OutputError(f"{out}: output directory must be separate from the input tree {root}")
        if target.exists():
            if not target.is_dir():
                raise OutputError(f"{out}: exists and is not a directory")
            if any(target.iterdir()) and not force:
                raise OutputError(f"{out}: exists and is not empty (use force to replace it)")

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=".hdl-vet-", dir=target.parent))
        changed: dict[str, dict[str, str]] = {}
        for path in session.files:
            rel = path.relative_to(root).as_posix()
            before = session.original(path)
            data = result.files.get(path, before)
            dest = tmp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            if path in result.files:
                changed[rel] = {"before": _sha(before), "after": _sha(data)}
        added: dict[str, dict[str, str]] = {}
        for name, data in result.added.items():
            dest = tmp / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            added[name] = {"sha256": _sha(data)}
        for inc in session.filelist.incdirs:
            (tmp / inc.relative_to(root)).mkdir(parents=True, exist_ok=True)
        fl_path = session.filelist_path
        fl_rel = fl_path.relative_to(root) if _is_within(fl_path, root) else Path(fl_path.name)
        (tmp / fl_rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp / fl_rel).write_bytes(_filelist_text(session, (root / fl_rel).parent, list(result.added)).encode())
        manifest = {
            "tool": "hdl-vet",
            "version": __version__,
            "top": session.top,
            "filelist": fl_rel.as_posix(),
            "scripts": [{"name": n, "sha256": h} for n, h in scripts],
            "changed": changed,
            "added": added,
        }
        (tmp / "hdl-vet.json").write_bytes((json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())

        if target.exists():
            backup = Path(tempfile.mkdtemp(prefix=".hdl-vet-old-", dir=target.parent))
            backup.rmdir()
            os.rename(target, backup)
        try:
            os.rename(tmp, target)
        except OSError:
            if backup is not None:
                os.rename(backup, target)
                backup = None
            raise
    except (OSError, ValueError) as e:  # ValueError: a path that cannot be expressed relative to another
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
        raise OutputError(f"{out}: cannot write the output tree ({e})") from e
    except BaseException:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)
    return out
