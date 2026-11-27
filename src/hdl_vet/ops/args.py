"""The checks of the arguments that are not names (tests/ops/test_argument_checks.py, rules A1 to A11).

Each check takes the `label` (the operation and the module or instance, such as `add_port top`) and the name of the
argument as the signature spells it. A refusal is a `VetError` that starts with the label. An operation calls
these first, so nothing is queued when one refuses.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

from hdl_vet.ops.common import VetError
from hdl_vet.slang_adapter import check_expression, tokens

_WHOLE = re.compile(r"[+-]?[0-9]+")


def _no(label: str, arg: str, wanted: str, value: object) -> VetError:
    return VetError(f"{label}: {arg} must be {wanted}, not {value!r}")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def name(label: str, arg: str, value: object, *, none: bool = False) -> None:
    """A name is a text (what makes it a valid name is checked elsewhere)."""
    if not (isinstance(value, str) or (none and value is None)):
        raise _no(label, arg, "a text", value)


def text(label: str, arg: str, value: object, *, none: bool = False) -> None:
    """A text that is not blank (None too, when `none`)."""
    if none and value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise _no(label, arg, "a text that is not blank", value)


def width(label: str, value: object) -> None:
    """None, an int of 1 or more, or a text that is not blank (a whole number as a text is judged as that number)."""
    if value is None:
        return
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and not (_WHOLE.fullmatch(stripped) and int(stripped) < 1):
            # a text in brackets is the script's own range and is not parsed (#56); any other is one expression
            if not stripped.startswith("[") and check_expression(stripped) is not None:
                raise _no(label, "width", "one expression with no comment, or a range in brackets", value)
            return
    elif _is_int(value) and value >= 1:  # type: ignore[operator]
        return
    raise _no(label, "width", "None, a number of 1 or more, or a text that is not blank", value)


def value(label: str, arg: str, v: object, *, none: bool = False, blank: bool = False) -> None:
    """A text, an int or a float. No bool, no None (unless `none`), no blank text (unless `blank`)."""
    if none and v is None:
        return
    if isinstance(v, str):
        if blank or v.strip():
            return
    elif _is_int(v) or (isinstance(v, float) and math.isfinite(v)):
        return
    raise _no(label, arg, "a text that is not blank or a number" if not blank else "a text or a number", v)


def expression(label: str, arg: str, v: object, *, blank: bool = False) -> None:
    """A value that reads as one expression with no comment."""
    value(label, arg, v, blank=blank)
    if blank and not tokens(str(v)):  # a blank or a comment alone keeps the answer of the operation
        return
    reason = check_expression(str(v))
    if reason is not None:
        raise VetError(f"{label}: {arg} {str(v)!r} is not one expression: {reason}")


def count(label: str, arg: str, v: object, *, none: bool = False) -> None:
    if not (_is_int(v) or (none and v is None)):
        raise _no(label, arg, "an int" + (" or None" if none else ""), v)


def flag(label: str, arg: str, v: object) -> None:
    if not isinstance(v, bool):
        raise _no(label, arg, "True or False", v)


def mapping(label: str, arg: str, v: object, *, texts: bool = False, values: bool = False) -> None:
    """None or a dict with text keys; its values are texts (`texts`) or values (`values`)."""
    if v is None:
        return
    if not isinstance(v, Mapping):
        raise _no(label, arg, "None or a mapping", v)
    for key, item in v.items():
        if not isinstance(key, str):
            raise _no(label, f"a key of {arg}", "a text", key)
        if texts and not isinstance(item, str):
            raise _no(label, f"{arg}[{key!r}]", "a text", item)
        if values:
            value(label, f"{arg}[{key!r}]", item)


def texts(label: str, arg: str, v: object) -> list[str]:
    """A collection of texts (not one text, bytes or a mapping), read once and returned as a list."""
    if not isinstance(v, (str, bytes, Mapping)) and isinstance(v, Iterable):
        found = list(v)
        if all(isinstance(i, str) for i in found):
            return found
    raise _no(label, arg, "a collection of texts", v)


def path(label: str, v: object, arg: str = "path") -> None:
    if not isinstance(v, (str, os.PathLike)):
        raise _no(label, arg, "a text or a Path", v)


def scripts(label: str, v: Iterable[Any]) -> list[tuple[str, str]]:
    """The scripts of `finish`, `check` and `save`: pairs of two texts without a line break."""
    if isinstance(v, (str, bytes)) or not isinstance(v, Iterable):
        raise _no(label, "scripts", "a list of (name, hash) pairs", v)
    v = list(v)
    for pair in v:
        if (
            not isinstance(pair, (list, tuple))
            or len(pair) != 2
            or not all(isinstance(t, str) and t.splitlines() in ([t], []) for t in pair)
        ):
            raise _no(label, "scripts", "pairs of two texts that are one line each", pair)
    return [(a, b) for a, b in v]
