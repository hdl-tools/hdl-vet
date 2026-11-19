"""Helpers over names, numbers and source text that the operations share."""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable

from hdl_vet.listedit import append_items
from hdl_vet.ops.common import VetError
from hdl_vet.slang_adapter import ConnInfo, ParamOverride, is_simple_identifier, tokens

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def _instance_name(name: str) -> str:
    """The name of an instance as `Module.info` lists it: an escaped name without its backslash and ending blank."""
    return name[1:].rstrip() if name.startswith("\\") else name


def _segment(name: str) -> str:
    """The path segment of the instance `name`, spelled as the structural snapshot spells it."""
    return name if _IDENT.fullmatch(name) else f"\\{name} "


def _unsegment(seg: str) -> str:
    """The instance name in the path segment `seg`: the inverse of `_segment`.

    A snapshot path spells an escaped name as SystemVerilog writes it, a backslash and a
    closing blank. The model names the same instance without them, so a path segment has
    to be turned back before it is looked up (#54).
    """
    return seg[1:-1] if len(seg) > 2 and seg.startswith("\\") and seg.endswith(" ") else seg


_DIRECTIONS = ("input", "output", "inout")
_BASED = re.compile(r"'[sS]?[bBoOdDhH]")
_DIGITS = re.compile(r"[0-9a-fA-FxXzZ?_]+")
_NET_KEYWORDS = frozenset(
    [
        "wire",
        "tri",
        "tri0",
        "tri1",
        "triand",
        "trior",
        "trireg",
        "wand",
        "wor",
        "uwire",
        "supply0",
        "supply1",
        "interconnect",
    ]
)


# --- small helpers -----------------------------------------------------------------


def _same(a: object, b: object) -> bool:
    """True when both texts have the same tokens (whitespace and comments do not count)."""
    return tokens(a) == tokens(b)


def _norm(text: object) -> str:
    """A string that is equal for texts with the same tokens."""
    return "\x00".join(tokens(text))


def _refuse_directive(op: str, what: str, directive: str | None) -> None:
    if directive is not None:
        raise VetError(
            f"{op}: the {what} list contains a {directive} conditional directive; sign-off only sees the "
            f"current defines, so the other configuration cannot be checked. Edit it by hand or with a raw splice."
        )


def _header_line_in_comment(data: bytes, start: int, end: int) -> bool:
    """True when the text from `start` to `end` opens a block comment it does not close."""
    line = data[start:end]
    i = 0
    while i < len(line) - 1:
        pair = line[i : i + 2]
        if pair == b"//":
            return False
        if pair == b"/*":
            close = line.find(b"*/", i + 2)
            if close < 0:
                return True
            i = close + 2
            continue
        i += 1
    return False


def _eol(data: bytes) -> bytes:
    """The file's line ending: CRLF if its first break is CRLF, else LF."""
    i = data.find(b"\n")
    return b"\r\n" if i > 0 and data[i - 1 : i] == b"\r" else b"\n"


def _did_you_mean(name: str, candidates: Iterable[str]) -> str:
    close = difflib.get_close_matches(name, sorted(set(candidates)), n=3)
    quoted = [repr(c) for c in close]
    if not quoted:
        return ""
    if len(quoted) == 1:
        return f"; did you mean {quoted[0]}?"
    return f"; did you mean {', '.join(quoted[:-1])} or {quoted[-1]}?"


def _range(width: int | str | None) -> str:
    if width is None:
        return ""
    if isinstance(width, int):
        if width < 1:
            raise VetError(f"width must be at least 1, not {width}")
        return "" if width == 1 else f" [{width - 1}:0]"
    w = str(width).strip()
    return " " + w if w.startswith("[") else f" [{w}-1:0]"


def _line_start(data: bytes, offset: int) -> int:
    return data.rfind(b"\n", 0, offset) + 1


def _append_to_list(
    data: bytes, last_start: int, last_end: int, close: int, item: bytes, eol: bytes
) -> list[tuple[int, int, bytes]]:
    """Splices that append `item` after the last element of a comma separated list."""
    return append_items(data, last_start, last_end, close, [item], eol)


_INT_TEXT = re.compile(r"[+-]?[0-9]+")
_SIZED_TEXT = re.compile(r"([0-9]*)'([sS]?)([bBoOdDhH])([0-9a-fA-F_]+)")


def _as_int(text: str) -> int | None:
    """The integer a parameter value stands for (`4`, `32'd4`, `8'hFF`), or None."""
    t = "".join(text.split())
    if _INT_TEXT.fullmatch(t):
        return int(t)
    m = _SIZED_TEXT.fullmatch(t)
    if m is None:
        return None
    try:
        value = int(m.group(4).replace("_", ""), {"b": 2, "o": 8, "d": 10, "h": 16}[m.group(3).lower()])
    except ValueError:
        return None
    if m.group(2) and m.group(1) and value >> (int(m.group(1)) - 1) & 1:
        value -= 1 << int(m.group(1))
    return value


def _same_value(a: str, b: str) -> bool:
    """Equal as integers when both are integral, whatever their type or width; else as text."""
    x, y = _as_int(a), _as_int(b)
    return (x == y) if x is not None and y is not None else a == b


_REG_WORD = re.compile(r"(?<![A-Za-z0-9_$])reg(?![A-Za-z0-9_$])")


def _type_key(text: str) -> str:
    """A port type for comparison: `reg` is `logic` under another keyword."""
    return _REG_WORD.sub("logic", text)


def _common_width(fields: Iterable[int]) -> int:
    """The width all name fields share, or 0 when they differ (or there are none)."""
    fs = list(fields)
    return fs[0] if fs and len(set(fs)) == 1 else 0


def _renamed_field(start: int, open_: int | None, name: str, new: str | None) -> int:
    """Width of the name field of a connection or override (name and blanks before "("), after a rename."""
    base = (open_ - start - 1) if open_ is not None else len(name)
    return base + (len(new) - len(name) if new is not None else 0)


def _list_gap(data: bytes, close: int) -> bytes:
    """One blank when removing a list that ends at the ")" at `close` would glue the next name to the previous one."""
    nxt = data[close + 1 : close + 2]
    return b" " if nxt and (nxt.isalnum() or nxt in (b"_", b"$", b"\\")) else b""


def _name_width(items: Iterable[ConnInfo | ParamOverride]) -> int:
    """Common width of the name fields (name plus blanks before "(") or 0."""
    widths = []
    for c in items:
        name = c.port if isinstance(c, ConnInfo) else c.name
        widths.append(c.open - c.start - 1 if c.open is not None else len(name or ""))
    return widths[0] if widths and len(set(widths)) == 1 else 0


def _bare_name(text: object) -> str | None:
    """The first token of `text` that would not mean the same in every module, or None.

    Allowed: keywords, system functions, literals (also based ones, whose digits lex as
    identifier-looking tokens) and package-scoped names `pkg::NAME`. Anything else that
    looks like an identifier is a bare name; `.` (a hierarchical name) and macro uses
    are refused too.
    """
    toks = tokens(text)
    i = 0
    while i < len(toks):
        t = toks[i]
        if _BASED.fullmatch(t):
            i += 1
            while i < len(toks) and _DIGITS.fullmatch(toks[i]):
                i += 1
            continue
        if t.startswith("$"):
            i += 1
            continue
        if t == "." or t.startswith("`"):
            return t
        if _IDENT.fullmatch(t) and is_simple_identifier(t):
            if i + 1 < len(toks) and toks[i + 1] == "::":
                i += 3
                continue
            return t
        i += 1
    return None


def _logic_type(text: str) -> str:
    """A net keyword or an implicit type becomes `logic` with the same dimensions."""
    toks = tokens(text)
    if toks and toks[0] in _NET_KEYWORDS:
        text = text.strip()[len(toks[0]) :].strip()
        toks = toks[1:]
    if not toks or toks[0] in ("[", "signed", "unsigned"):
        return ("logic " + text).strip()
    return text.strip()


def _param_decl(type: str, name: str, default: object) -> str:
    t = str(type).strip()
    return f"parameter {t + ' ' if t else ''}{name} = {default}"
