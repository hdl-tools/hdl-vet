"""The manual is written to the writing rules of ASD-STE100, Simplified Technical English.

The owner's reason: the meaning must stay the same for every reader, and the text
must be simple. Many readers do not have English as their first language.

What this is and is not. The rules below are the writing rules of STE as the
planner knows them. The STE dictionary of approved words was not available, so
the words of the manual are NOT checked against it, and the manual does not claim
to comply with ASD-STE100. Where the rules say "approved word", the manual uses a
common word with one meaning, or a technical name from its own glossary.

RULES THE TESTS CHECK (on the prose of `docs/manual/chapters/*.tex`; listings,
tables, headings and names set with \\code, \\opt, \\file, \\cmd, \\apiname do
not count as prose, and each such name counts as one word)

T1  A sentence has 25 words or less. A sentence in a numbered procedure (an
    `enumerate` list) has 20 words or less.
T2  A paragraph has 6 sentences or less. An item of a list is a paragraph.
T3  The words and marks in FORBIDDEN are not used. Each has a plain replacement.
T4  No contraction (don't, it's) and no semicolon in prose.

RULES THE REVIEWER CHECKS

U1  One word, one meaning; one meaning, one word. The glossary of chapter 1
    gives the technical names, and each is used only with that meaning (file:
    a file on disk; path: a place in the hierarchy; and so on). A thing does
    not get a second name for variety.
U2  Active voice. The passive is used only when the one who acts is unknown or
    does not matter ("the file is written in UTF-8").
U3  Procedures: one instruction in a sentence, in the imperative ("Run the
    command."), in the order in which the reader does them. A condition comes
    before the instruction ("If the folder exists, give --force.").
U4  Descriptions: one topic in a paragraph; the first sentence says what the
    paragraph is about. One idea in a sentence.
U5  Simple tenses: present, past, future, imperative. No "-ing" form as a verb
    ("is running", "when using"); write "runs", "when you use". Articles are
    written. A noun group has three words or less; a longer one is turned into
    a phrase with "of" or "that".
U6  A list that depends on its introduction is a vertical list, and the
    introduction ends with a colon.
U7  The rewrite changes how a thing is said, never what is said. Every
    statement of the text before the rewrite is still made, no statement is
    added, and no condition or exception is lost.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHAPTERS = REPO / "docs" / "manual" / "chapters"

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

_NAME = r"\\(?:cmd|code|opt|file|apiname|texttt|lstinline)\{(?:[^{}]|\{[^{}]*\})*\}"
_BREAK = "\n\n"


def prose(tex: str) -> list[tuple[bool, str]]:
    """The paragraphs of LaTeX text as (is in a numbered procedure, plain text). A name is the word CODE."""
    tex = "\n".join(re.sub(r"(?<!\\)%.*", "", line) for line in tex.splitlines())
    tex = re.sub(r"\\begin\{(tabular|tabularx|longtable)\}.*?\\end\{\1\}", _BREAK, tex, flags=re.S)
    tex = re.sub(r"\\lstinputlisting(?:\[[^\]]*\])?\{[^}]*\}", _BREAK, tex)
    tex = re.sub(_NAME, " CODE ", tex)
    tex = re.sub(r"\\(?:chapter|section|subsection|subsubsection|paragraph)\*?\{(?:[^{}]|\{[^{}]*\})*\}", _BREAK, tex)
    tex = re.sub(r"\\begin\{entry\}\{(?:[^{}]|\{[^{}]*\})*\}", _BREAK, tex)
    tex = re.sub(r"\\(?:label|index|sig|vspace|hspace)\*?\{[^{}]*\}", " ", tex)
    tex = re.sub(r"(?:chapter|section|table)?~?\\(?:ref|pageref)\{[^{}]*\}", " N ", tex)
    tex = tex.replace("\\begin{enumerate}", _BREAK + "<<PROC>>" + _BREAK)
    tex = tex.replace("\\end{enumerate}", _BREAK + "<<END>>" + _BREAK)
    tex = re.sub(r"\\(?:begin|end)\{[^{}]*\}(?:\[[^\]]*\])?", _BREAK, tex)
    tex = re.sub(r"\\item(?:\[(?:[^\[\]]|\[[^\]]*\])*\])?", _BREAK, tex)
    for _ in range(3):  # \textbf{\emph{x}} -> x
        tex = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?\{([^{}]*)\}", r"\1", tex)
    tex = re.sub(r"\\([#_%&$])", r"\1", tex)
    tex = tex.replace("\\\\", " ").replace("~", " ")
    tex = re.sub(r"\\[a-zA-Z]+\*?|[{}]", " ", tex)
    out: list[tuple[bool, str]] = []
    depth = 0
    for block in re.split(r"\n\s*\n", tex):
        text = " ".join(block.split())
        if text == "<<PROC>>":
            depth += 1
        elif text == "<<END>>":
            depth -= 1
        elif re.search(r"[A-Za-z]{2}", text.replace("CODE", "")):
            out.append((depth > 0, text))
    return out


def sentences(paragraph: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])[\")]?\s+(?=[A-Z(]|CODE\b|hdl-vet\b)", paragraph)
    return [p.strip() for p in parts if re.search(r"[A-Za-z]{2}", p.replace("CODE", ""))]


def words(sentence: str) -> int:
    return sum(1 for w in sentence.split() if re.search(r"[A-Za-z0-9]", w))


def _chapters() -> list[tuple[str, bool, str]]:
    found = []
    for path in sorted(CHAPTERS.glob("*.tex")):
        for procedure, text in prose(path.read_text(encoding="utf-8")):
            found.append((path.name, procedure, text))
    return found


def test_the_prose_reader():
    tex = (
        "\\chapter{A heading that is not prose}\\label{ch:x}\n"
        "This is \\emph{one} sentence with \\code{vet apply --dry-run} in it. "
        "Here is another, see chapter~\\ref{ch:y}.\n"
        "% a comment. With a sentence.\n\n"
        "\\begin{enumerate}\n"
        "\\item Run \\cmd{vet init}. Then look at \\file{hdl-vet.toml}.\n"
        "\\item[\\opt{--top}] The top module.\n"
        "\\end{enumerate}\n"
        "\\lstinputlisting[style=console]{examples/console/x.txt}\n"
        "\\begin{tabular}{ll} a cell & another cell \\\\ \\end{tabular}\n"
        "After the list: \\textbf{bold \\emph{words}} end.\n"
    )
    found = prose(tex)
    assert found == [
        (False, "This is one sentence with CODE in it. Here is another, see N ."),
        (True, "Run CODE . Then look at CODE ."),
        (True, "The top module."),
        (False, "After the list: bold words end."),
    ]
    assert sentences(found[0][1]) == ["This is one sentence with CODE in it.", "Here is another, see N ."]
    assert words("This is one sentence with CODE in it.") == 8
    assert sentences("CODE adds a port. CODE removes it. It is (see N ) gone.") == [
        "CODE adds a port.",
        "CODE removes it.",
        "It is (see N ) gone.",
    ]


def test_the_manual_has_prose_to_check():
    found = _chapters()
    assert len(found) > 300
    assert any(procedure for _, procedure, _ in found)


def test_sentences_are_short():
    """T1."""
    long = []
    for name, procedure, text in _chapters():
        limit = 20 if procedure else 25
        for sentence in sentences(text):
            if words(sentence) > limit:
                long.append(f"{name}: {words(sentence)} words (limit {limit}): {sentence[:90]}")
    assert long == []


def test_paragraphs_are_short():
    """T2."""
    long = [
        f"{name}: {len(sentences(text))} sentences: {text[:70]}"
        for name, _, text in _chapters()
        if len(sentences(text)) > 6
    ]
    assert long == []


def test_no_forbidden_word():
    """T3."""
    found = []
    for name, _, text in _chapters():
        for pattern, instead in FORBIDDEN.items():
            for match in re.finditer(pattern, text, re.I):
                context = text[max(0, match.start() - 40) : match.end()]
                found.append(f"{name}: '{match.group(0)}' (write: {instead}): ...{context}")
    assert found == []


def test_no_contraction_and_no_semicolon():
    """T4."""
    found = []
    for name, _, text in _chapters():
        for match in CONTRACTION.finditer(text):
            found.append(f"{name}: '{match.group(0)}': ...{text[max(0, match.start() - 40) : match.end() + 20]}")
        for match in re.finditer(";", text):
            found.append(f"{name}: semicolon: ...{text[max(0, match.start() - 50) : match.end() + 20]}")
    assert found == []
