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
