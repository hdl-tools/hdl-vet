"""Helpers over tokens, names, bytes and source text that the other modules share."""

from __future__ import annotations

import functools
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pyslang

from hdl_vet.slang_adapter.records import AdapterError

_WS_RE = re.compile(r"\s+")
_IDENT_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


_BOM = b"\xef\xbb\xbf"


@functools.lru_cache(maxsize=4096)
def _tokens(text: str) -> tuple[str, ...]:
    sm = pyslang.SourceManager()
    buf = sm.assignText(text)
    lexer = pyslang.parsing.Lexer(buf, pyslang.BumpAllocator(), pyslang.Diagnostics(), sm)
    eof = pyslang.parsing.TokenKind.EndOfFile
    out: list[str] = []
    while True:
        tok = lexer.lex()
        if tok.kind == eof:
            return tuple(out)
        out.append(tok.rawText)


def tokens(text: object) -> list[str]:
    """The raw texts of the tokens of `text`; whitespace and comments are not tokens."""
    return list(_tokens(str(text)))


_PLAIN_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def is_simple_identifier(name: str) -> bool:
    """True for one plain identifier that is not a SystemVerilog keyword."""
    if not isinstance(name, str):
        return False
    sm = pyslang.SourceManager()
    buf = sm.assignText(name)
    lexer = pyslang.parsing.Lexer(buf, pyslang.BumpAllocator(), pyslang.Diagnostics(), sm)
    tok = lexer.lex()
    if tok.kind != pyslang.parsing.TokenKind.Identifier or tok.rawText != name:
        return False
    if not _PLAIN_IDENT.fullmatch(name):  # slang also lexes `\a` as one identifier
        return False
    return lexer.lex().kind == pyslang.parsing.TokenKind.EndOfFile


def _key(path: Path) -> str:
    return os.path.normcase(str(path))


def _read(path: Path, overrides: Mapping[Path, bytes]) -> bytes:
    data = overrides.get(path)
    if data is None:
        try:
            data = path.read_bytes()
        except OSError as e:
            raise AdapterError(f"{path}: cannot read ({e})") from e
    return data


def _tokens_of(node: Any) -> list[Any]:
    out: list[Any] = []

    def scan(n: Any) -> None:
        if isinstance(n, pyslang.parsing.Token):
            out.append(n)

    node.visit(scan)
    return out


def _items(lst: Any) -> list[Any]:
    """The nodes of a separated syntax list (separator tokens dropped)."""
    return [x for x in lst if not isinstance(x, pyslang.parsing.Token)]


def _text(node: Any) -> str:
    return str(node).strip()


def _first_token(node: Any) -> Any:
    """First token of `node`, skipping leading attribute instances `(* ... *)`."""
    attrs = getattr(node, "attributes", None)
    if not attrs:
        return node.getFirstToken()
    last = attrs[len(attrs) - 1].getLastToken()
    cut, buf = last.range.end.offset, last.location.buffer
    found: list[Any] = []

    def scan(n: Any) -> None:
        if not found and isinstance(n, pyslang.parsing.Token) and n.location.buffer == buf and n.location.offset >= cut:
            found.append(n)

    node.visit(scan)
    return found[0] if found else node.getFirstToken()


def _clean_text(node: Any) -> str:
    """Source text of `node` without comments and with whitespace runs collapsed."""
    raw = str(node)
    if "/*" not in raw and "//" not in raw:
        return _WS_RE.sub(" ", raw).strip()
    ws = (pyslang.parsing.TriviaKind.Whitespace, pyslang.parsing.TriviaKind.EndOfLine)
    parts: list[str] = []

    def visit(n: Any) -> None:
        if isinstance(n, pyslang.parsing.Token):
            if any(t.kind in ws for t in n.trivia):
                parts.append(" ")
            parts.append(n.rawText)

    node.visit(visit)
    return _WS_RE.sub(" ", "".join(parts)).strip()


def syntax_equivalent(a: str, b: str) -> bool:
    trees = []
    for text in (a, b):
        tree = pyslang.syntax.SyntaxTree.fromText(text)
        if any(d.isError() for d in tree.diagnostics):
            raise AdapterError(f"text does not parse: {text!r}")
        trees.append(tree)
    return bool(trees[0].root.isEquivalentTo(trees[1].root))


def _raw_tokens(text: str) -> list[str] | None:
    """The raw texts of the tokens of `text`, directives and macro bodies included; None on a lexer error."""
    sm = pyslang.SourceManager()
    buf = sm.assignText(text)
    diags = pyslang.Diagnostics()
    lexer = pyslang.parsing.Lexer(buf, pyslang.BumpAllocator(), diags, sm)
    eof = pyslang.parsing.TokenKind.EndOfFile
    out: list[str] = []
    while True:
        tok = lexer.lex()
        if tok.kind == eof:
            return out if len(diags) == 0 else None
        out.append(tok.rawText)


def equivalent_bytes(a: bytes, b: bytes) -> bool:
    """True when two files differ only in blanks and comments; False when in doubt.

    The syntax trees must be equivalent (`syntax_equivalent`), and so must the tokens of the two files read
    without the preprocessor: the tree leaves out a `define, and the body of a macro that the file does not use.
    A file that does not parse on its own (an unknown macro, an include that is not found) is "in doubt".
    """
    if a == b:
        return True
    try:
        left, right = (x.decode("utf-8", errors="surrogateescape") for x in (a, b))
        tokens_a, tokens_b = _raw_tokens(left), _raw_tokens(right)
        if tokens_a is None or tokens_a != tokens_b:
            return False
        return syntax_equivalent(left, right)
    except Exception:
        return False


# Directives that reach text beyond the place where they stand (an unknown name is a use of a macro).


_OPEN_CLOSE = {
    "module": "endmodule",
    "macromodule": "endmodule",
    "interface": "endinterface",
    "program": "endprogram",
    "package": "endpackage",
    "primitive": "endprimitive",
    "config": "endconfig",
    "checker": "endchecker",
}
_CLOSE = frozenset(_OPEN_CLOSE.values())
_STRUCTURE = frozenset(_OPEN_CLOSE) | _CLOSE | {"bind"}


def _why_not_inside(text: bytes, which: str) -> str | None:
    """Why `text` cannot be put into a module and leave the text beside it alone, or None.

    It must lex without an error, hold no directive and no use of a macro, no `bind`, and no keyword that
    opens or closes a module, an interface, a program, a package, a primitive, a config or a checker.
    """
    found = _lex_all(text)
    if found is None:
        return f"the {which} text does not read on its own (an unfinished comment or string)"
    for _, kind, raw in found:
        if kind == "Directive":
            name = raw[1:]
            what = "the directive" if name in _DIRECTIVES else "a use of the macro"
            return f"the {which} text holds {what} {raw}"
        if raw in _STRUCTURE and kind != "Identifier":
            hint = " (to add text before it, insert at its first byte instead of replacing it)" if raw in _CLOSE else ""
            return f"the {which} text holds the keyword {raw}{hint}"
    return None


_DIRECTIVES = frozenset(
    {
        "include",
        "define",
        "undef",
        "undefineall",
        "ifdef",
        "ifndef",
        "else",
        "elsif",
        "endif",
        "celldefine",
        "endcelldefine",
        "line",
        "default_nettype",
        "unconnected_drive",
        "nounconnected_drive",
        "timescale",
        "resetall",
        "pragma",
        "begin_keywords",
        "end_keywords",
    }
)


def unconfined_reason(old: bytes, new: bytes) -> str | None:
    """Why replacing `old` by `new` inside a module could change the text beside it, or None when it cannot.

    Both texts are read as tokens on their own: no unfinished comment or string, no directive, no use of
    a macro (what it expands to is not in the text), no `bind`, and no keyword that opens or closes a module
    (or an interface, program, package, ...).
    """
    try:
        return _why_not_inside(old, "old") or _why_not_inside(new, "new")
    except Exception:
        return "the old or the new text cannot be read"


def confined(old: bytes, new: bytes) -> bool:
    """True when `unconfined_reason` finds nothing."""
    return unconfined_reason(old, new) is None


def _lex_all(data: bytes) -> list[tuple[int, str, str]] | None:
    """(offset, kind, raw text) of every token of `data`; None on a lexer error or text that is not UTF-8."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    sm = pyslang.SourceManager()
    buf = sm.assignText(text)
    diags = pyslang.Diagnostics()
    lexer = pyslang.parsing.Lexer(buf, pyslang.BumpAllocator(), diags, sm)
    eof = pyslang.parsing.TokenKind.EndOfFile
    out: list[tuple[int, str, str]] = []
    while True:
        tok = lexer.lex()
        if tok.kind == eof:
            return out if len(diags) == 0 else None
        out.append((tok.location.offset, tok.kind.name, tok.rawText))


def _module_whole(inside_a: list[tuple[str, str]], inside_b: list[tuple[str, str]]) -> bool:
    """True when the tokens `inside_a` are still one module like `inside_b`: the same first token, keywords that open
    and close in matching pairs and close the module only at its last token, the same directives in the same
    order, and conditional directives that are balanced."""
    if not inside_b or not inside_a or inside_a[0] != inside_b[0]:
        return False
    stack: list[str] = []
    for i, (kind, raw) in enumerate(inside_a):
        if kind == "Identifier":
            continue
        if raw in _OPEN_CLOSE:
            stack.append(raw)
        elif raw in _CLOSE:
            if not stack or _OPEN_CLOSE[stack[-1]] != raw:
                return False
            stack.pop()
            if not stack and i != len(inside_a) - 1:
                return False
    if stack or inside_a[-1][1] not in _CLOSE:
        return False
    directives = [r for k, r in inside_a if k == "Directive"]
    if directives != [r for k, r in inside_b if k == "Directive"]:
        return False
    depth = 0
    for raw in directives:
        if raw in ("`ifdef", "`ifndef"):
            depth += 1
        elif raw in ("`else", "`elsif", "`endif"):
            if depth == 0:
                return False
            if raw == "`endif":
                depth -= 1
    return depth == 0


def splice_stays(before: bytes, after: bytes, spans: list[tuple[int, int, int, int]]) -> bool:
    """True when each module of `spans` is, after the splices, the same module and nothing beside them changed.

    A span is (start, end, new_start, new_end): the module at [start, end) of `before` is at
    [new_start, new_end) of `after`. The whole of both files is read as tokens (a comment is not a token, so a
    comment that swallows text shows as missing tokens). The tokens outside the modules must be the same, in
    the same order, directives included. The tokens of each module must still be one module (`_module_whole`).
    False when in doubt.
    """
    try:
        tb, ta = _lex_all(before), _lex_all(after)
        if tb is None or ta is None:
            return False
        if [r for o, _, r in tb if not any(s <= o < e for s, e, _, _ in spans)] != [
            r for o, _, r in ta if not any(ns <= o < ne for _, _, ns, ne in spans)
        ]:
            return False
        return all(
            _module_whole(
                [(k, r) for o, k, r in ta if ns <= o < ne],
                [(k, r) for o, k, r in tb if s <= o < e],
            )
            for s, e, ns, ne in spans
        )
    except Exception:
        return False


def _directive_lines(data: bytes) -> list[tuple[int, int, bool]] | None:
    """(start, stop, is_define) of the line of each compiler directive, read from the lexed file.

    A line runs from the directive token to its line break (`stop` is the place of the break). A `define goes on
    through the lines that a backslash continues. A use of a macro is not a directive. None when the file does
    not lex.
    """
    found = _lex_all(data)
    if found is None:
        return None
    out: list[tuple[int, int, bool]] = []
    for offset, kind, raw in found:
        if kind != "Directive" or raw[1:] not in _DIRECTIVES:
            continue
        define = raw == "`define"
        stop = offset
        while True:
            nl = data.find(b"\n", stop)
            if nl < 0:
                stop = len(data)
                break
            if define and data[stop:nl].rstrip(b"\r").endswith(b"\\"):
                stop = nl + 1
                continue
            stop = nl
            break
        out.append((offset, stop, define))
    return out


def touches_define(data: bytes, ranges: list[tuple[int, int]]) -> bool:
    """True when one of the byte ranges meets a `define: its line, with the lines that a backslash continues.

    True when in doubt (the file does not lex).
    """
    try:
        lines = _directive_lines(data)
        if lines is None:
            return True
        return any(d and s < stop and e > offset for offset, stop, d in lines for s, e in ranges)
    except Exception:
        return True


def meets_directive(data: bytes, ranges: list[tuple[int, int]]) -> bool:
    """True when one of the byte ranges meets the line of a directive: the directive token, its arguments, the name
    of an include, the lines of a `define that a backslash continues, or the line break that ends it.

    The lines come from the lexed file, not from a search of the text. True when in doubt.
    """
    try:
        lines = _directive_lines(data)
        if lines is None:
            return True
        return any(s <= stop and e > offset for offset, stop, _ in lines for s, e in ranges)
    except Exception:
        return True
