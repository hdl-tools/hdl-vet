"""The session archive: one zip of plain data, written and read here and nowhere else.

`session.json` is the index (JSON, `"format": 5`); every other entry is file content. The zip
holds no code and is never extracted: a reader looks entries up by the names the index implies.
The bytes depend only on what is saved: entries sorted by name, one fixed timestamp, one
creator system, deflated at one fixed level, the index with sorted keys and fixed separators.
It is written to a temporary file next to its destination and renamed, so a failure leaves
neither a partial archive nor a temporary file behind. Only format 5 is read: a format 4 archive
does not say which expectations the script registered, so it is refused.

Reading is defensive: the uncompressed sizes the zip directory declares are added up and
refused above `MAX_BYTES` before anything is read, no entry is read past its declared size,
and the `need_*` helpers check the types of the index (a boolean is not an integer, nothing is
coerced). Every failure is an `ArchiveError` naming the file, or, for a malformed index value,
a TypeError or ValueError that the caller turns into one.

This module knows nothing of sessions; `Session.to_state` and `Session.from_state` give the
index and the entries their meaning.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import zipfile
import zlib
from pathlib import Path
from typing import Any

from hdl_vet import __version__

__all__ = [
    "FORMAT",
    "MAX_BYTES",
    "ArchiveError",
    "need_bool",
    "need_dict",
    "need_int",
    "need_list",
    "need_str",
    "read_archive",
    "write_archive",
]

# 2: one check key for a pair of operations. 3: the index holds the marks of the raw splices; an
# archive without them cannot be judged, so every older format is refused. 4: a mark has a label, and
# the logic calls (add_assign, connect, set_param, replace_module) leave marks too. 5: an expectation says
# whether the script registered it; a format 4 archive does not say it, so it is refused.
FORMAT = 5
MAX_BYTES = 2 * 1024**3  # the most uncompressed data an archive may declare
INDEX = "session.json"
_STAMP = (1980, 1, 1, 0, 0, 0)  # the earliest time a zip can hold
_LEVEL = 6
_UNIX = 3  # `create_system`: the same whatever the platform that wrote it


class ArchiveError(Exception):
    """An archive that cannot be written, read or understood; the message names the file."""


def need_str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected a string, not {value!r}")
    return value


def need_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"expected an integer, not {value!r}")
    return value


def need_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"expected true or false, not {value!r}")
    return value


def need_list(value: object, size: int | None = None) -> list[Any]:
    """`value` as a list (of exactly `size` items when given)."""
    if not isinstance(value, list):
        raise TypeError(f"expected a list, not {value!r}")
    if size is not None and len(value) != size:
        raise ValueError(f"expected a list of {size} items, not {value!r}")
    return value


def need_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"expected an object, not {value!r}")
    return value


def write_archive(path: Path, index: dict[str, Any], files: dict[str, bytes]) -> None:
    """Write the archive of `index` (which gets its format, tool and version) and `files`.

    An existing file at `path` is replaced when, and only when, everything was written.
    """
    if INDEX in files:
        raise ArchiveError(f"{INDEX} is reserved for the index")
    full = {"format": FORMAT, "tool": "hdl-vet", "version": __version__, **index}
    entries = {INDEX: json.dumps(full, sort_keys=True, separators=(",", ":")).encode("ascii"), **files}
    dest = Path(path)
    tmp: str | None = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=".hdl-vet-", suffix=".tmp", dir=dest.resolve().parent)
        with os.fdopen(fd, "wb") as fh, zipfile.ZipFile(fh, "w") as z:
            for name in sorted(entries):
                info = zipfile.ZipInfo(name, _STAMP)
                info.create_system = _UNIX
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, entries[name], zipfile.ZIP_DEFLATED, _LEVEL)
        os.replace(tmp, dest)  # the zip is closed: Windows cannot replace an open file
        tmp = None
    except OSError as e:
        raise ArchiveError(f"{dest}: cannot write the session archive ({e})") from e
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def read_archive(path: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    """The index and the other entries of the archive at `path`.

    ArchiveError when it cannot be read, is not a zip, declares too much data, has no index,
    or has another format version.
    """
    src = Path(path)
    try:
        with zipfile.ZipFile(src) as z:
            infos = z.infolist()
            names = [i.filename for i in infos]
            if len(set(names)) != len(names):
                raise ArchiveError(f"{src}: damaged session archive (an entry is listed twice)")
            if INDEX not in names:
                raise ArchiveError(f"{src}: not a session archive (it has no {INDEX})")
            declared = sum(i.file_size for i in infos)
            if declared > MAX_BYTES:
                raise ArchiveError(
                    f"{src}: the archive declares {declared} bytes of data, more than the limit of {MAX_BYTES}"
                )
            entries: dict[str, bytes] = {}
            for info in infos:
                with z.open(info) as fh:
                    data = fh.read(info.file_size + 1)
                if len(data) != info.file_size:
                    raise ArchiveError(f"{src}: damaged session archive ({info.filename} is not its declared size)")
                entries[info.filename] = data
    except ArchiveError:
        raise
    except OSError as e:
        raise ArchiveError(f"{src}: cannot read the session archive ({e})") from e
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError) as e:
        raise ArchiveError(f"{src}: not a session archive, or damaged ({e})") from e
    try:
        index = json.loads(entries.pop(INDEX))
    except (ValueError, RecursionError) as e:  # ValueError includes UnicodeDecodeError
        raise ArchiveError(f"{src}: damaged session archive (the index is not usable JSON: {type(e).__name__})") from e
    if not isinstance(index, dict) or index.get("tool") != "hdl-vet":
        raise ArchiveError(f"{src}: not a session archive (the index is not an hdl-vet index)")
    found = index.get("format")
    if isinstance(found, bool) or not isinstance(found, int) or found != FORMAT:
        raise ArchiveError(
            f"{src}: session archive format {found!r}, but this hdl-vet reads format {FORMAT} (an integer)"
        )
    return index, entries
