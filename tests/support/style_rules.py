"""The word lists and the helpers of the language rules; test_manual_style.py and test_readme.py both use them."""

from __future__ import annotations

import re

# T3: pattern -> what to write instead.
FORBIDDEN = {
    r"\bshould\b": "must (or say what happens)",
    r"\bmay\b": "can",
    r"\bmight\b": "can",
    r"\bneeds? to\b": "must",
    r"\bin order to\b": "to",
    r"\bprior to\b": "before",
    r"\bvia\b": "through, with",
    r"\butili[sz]e[sd]?\b": "use",
    r"\bensures?\b": "make sure",
    r"\be\.g\.": "for example",
    r"\bi\.e\.": "that is",
    r"\betc\b": "name the things",
    r"\bplease\b": "leave it out",
    r"\bsimply\b": "leave it out",
    r"\bjust\b": "leave it out, or 'only'",
    r"\bobviously\b": "leave it out",
    r"\beasily\b": "leave it out",
    r"\bbasically\b": "leave it out",
}
CONTRACTION = re.compile(r"\b\w+n't\b|\b(?:it|that|there|what|let|here)'s\b|\b\w+'(?:re|ll|ve)\b", re.I)


def sentences(paragraph: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])[\")]?\s+(?=[A-Z(]|CODE\b|hdl-vet\b)", paragraph)
    return [p.strip() for p in parts if re.search(r"[A-Za-z]{2}", p.replace("CODE", ""))]


def words(sentence: str) -> int:
    return sum(1 for w in sentence.split() if re.search(r"[A-Za-z0-9]", w))
