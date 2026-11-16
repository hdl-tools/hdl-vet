"""Phase 5c, part 2b: README.md and CONTRIBUTING.md. Rules and the tests that hold them.

Two readers. The README is for an RTL engineer who meets hdl-vet for the first
time and decides in two minutes whether to read on. CONTRIBUTING.md is for a
person who wants to change the repository. Both documents are short; the manual
(`docs/manual`) has the detail and they point to it.

Owner decisions (2026-11-16)

- A contributor does not have to say that an AI tool was used. The contributor
  answers for every line as their own work.
- A contribution is offered under the licence of the project (Apache-2.0) by
  opening the pull request. There is no sign-off line.
- Required for every change: tests that state the behaviour, no change of
  behaviour without a test, a test is never weakened to make code pass, lint
  and type checks clean. The flow of two reviews is the recommended way for a
  larger change, not a requirement.
- The language rules of the manual (tests/test_manual_style.py, T1-T4 and
  U1-U6) apply to both documents.

RULES THE TESTS CHECK

D1  README.md has these second-level headings, in this order (others may come
    between): "What it does", "Status", "Install", "A first run",
    "Documentation", "Contributing", "Licence". CONTRIBUTING.md has: "Before
    you start", "Set up", "What a pull request must satisfy", "A larger
    change", "Work with an AI tool", "Writing", "Licence".
D2  Every `vet` command a reader sees is run. A fenced block marked `console`
    is a transcript (format: tests/transcript.py). The `console` blocks of a
    document are one series: they are replayed in the order of the document in
    ONE fresh copy of `docs/manual/examples`, because a reader types them one
    after the other. (A block that starts again from nothing fails there.)
    A `vet` command does not appear in any other kind of block. With
    HDL_VET_MANUAL_UPDATE=1 the blocks are rewritten with what the commands
    print now.
D3  A fenced block marked `python` is, byte for byte, one of the files in
    `docs/manual/examples/scripts`, so that the script shown is a script the
    tests run. The README shows at least one script and one transcript that
    runs it.
D4  Every relative link in both documents points to a file or folder that
    exists, with the same spelling.
D5  The language rules T1-T4 hold for the prose of both documents: 25 words in
    a sentence (20 in a numbered list), 6 sentences in a paragraph, no word of
    FORBIDDEN, no contraction, no semicolon. Code blocks, tables, headings and
    names in backticks are not prose; a name in backticks counts as one word.
D6  The README does not name a package index and gives no install command with
    the bare package name: the name is not registered yet (part 5c-3 changes
    this). It does not print a version number.
D7  CONTRIBUTING.md gives the commands that CI runs, so that a contributor can
    run them first: `ruff check`, `ruff format --check`, `mypy`, `pytest`, the
    project rules (`tests/test_project_rules.py`), the corpus tests (the
    marker `corpus`, `HDL_BAG_REPO`, `HDL_VET_REQUIRE_CORPUS`), the update of
    the manual's transcripts (`HDL_VET_MANUAL_UPDATE=1`) and the build of the
    manual (`latexmk`). It names the hdl-bag commit that CI pins only by
    pointing to `.github/workflows/ci.yml`, so that the two cannot disagree.
D8  CONTRIBUTING.md states the owner's decisions above in plain words: it has
    the phrases "Apache-2.0" and "your own work", and it does not ask for a
    "Signed-off-by" line or for a statement that an AI tool was used.

WHAT THE REVIEWER CHECKS

E1  Every statement is true of the repository today (commands run, files exist,
    what CI does, what the tool does). Nothing is promised that does not exist.
E2  README: after "What it does" and "Status" a reader knows what hdl-vet is
    for, what it changes and never changes, what stops a wrong edit, and what
    it cannot do yet. "A first run" works when followed literally from a clone.
E3  CONTRIBUTING: "What a pull request must satisfy" is a list a contributor
    can tick off. It includes: the rules of the code base that
    tests/test_project_rules.py enforces; the manual changes with the
    behaviour (the tests of the manual say what is missing); a wrong test is
    reported in the pull request, not changed silently. "A larger change"
    describes the flow as it was used here: specification and tests as the
    docstring of a test file, a review of the tests against the real code
    before implementation, implementation with the tests frozen, an
    adversarial review, review findings turned into tests, a fix round.
    "Work with an AI tool": you answer for every line; you read it, run it and
    can explain it; a statement about behaviour is checked by a run, not taken
    from the tool; the tool does not edit a test to make it pass; you do not
    give it material that you may not share, and you do not submit text that
    you may not license; a reviewer can ask you to explain any line.
E4  U1-U6 of tests/test_manual_style.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import hdl_vet
import transcript
from test_manual_style import CONTRACTION, FORBIDDEN, sentences, words

REPO = transcript.REPO
README = REPO / "README.md"
CONTRIBUTING = REPO / "CONTRIBUTING.md"
SCRIPTS = transcript.EXAMPLES / "scripts"
DOCUMENTS = [README, CONTRIBUTING]

HEADINGS = {
    "README.md": ["What it does", "Status", "Install", "A first run", "Documentation", "Contributing", "Licence"],
    "CONTRIBUTING.md": [
        "Before you start",
        "Set up",
        "What a pull request must satisfy",
        "A larger change",
        "Work with an AI tool",
        "Writing",
        "Licence",
    ],
}

_FENCE = re.compile(r"^```([A-Za-z0-9_-]*)[ \t]*\n(.*?)^```[ \t]*$", re.S | re.M)


def blocks(text: str) -> list[tuple[str, str]]:
    """The fenced blocks of Markdown text as (language, content)."""
    return [(m.group(1), m.group(2)) for m in _FENCE.finditer(text)]


def prose_md(text: str) -> list[tuple[bool, str]]:
    """The paragraphs of Markdown text as (is an item of a numbered list, plain text). A name in backticks is CODE."""
    text = _FENCE.sub("\n\n", text)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"`[^`\n]+`", " CODE ", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_]{1,3}([^*_\n]+)[*_]{1,3}", r"\1", text)
    out: list[tuple[bool, str]] = []
    current: list[str] = []
    numbered = False

    def flush() -> None:
        joined = " ".join(" ".join(current).split())
        if re.search(r"[A-Za-z]{2}", joined.replace("CODE", "")):
            out.append((numbered, joined))
        current.clear()

    for line in text.splitlines():
        stripped = line.strip()
        item = re.match(r"(?:[-*+]|(\d+)[.)])\s+(.*)", stripped)
        if not stripped or stripped.startswith(("#", "|", ">")) or set(stripped) <= set("-=|: "):
            flush()
            if not line.startswith(" "):
                numbered = False
        elif item:
            flush()
            numbered = item.group(1) is not None
            current.append(item.group(2))
        else:
            if not line.startswith(" ") and not current:
                numbered = False
            current.append(stripped)
    flush()
    return out


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _exists_exactly(base: Path, relative: str) -> bool:
    here = base
    for part in Path(relative).parts:
        if part == ".":
            continue
        if part == ".." or not here.is_dir() or part not in {p.name for p in here.iterdir()}:
            return False
        here = here / part
    return here.exists()


def test_the_markdown_reader():
    text = (
        "# Title\n\n"
        "A sentence with `vet apply` and a [link](docs/x.md). A second one.\n"
        "It goes on here.\n\n"
        "```console\n$ vet apply\n0 applied, 0 skipped\n```\n\n"
        "1. Run `vet init`. Then read the file.\n"
        "2. Give **the top** module.\n"
        "   It is one module.\n\n"
        "- An item that is not a step.\n\n"
        "| a | table |\n| --- | --- |\n| cell | cell |\n\n"
        "After the table.\n"
    )
    assert blocks(text) == [("console", "$ vet apply\n0 applied, 0 skipped\n")]
    assert prose_md(text) == [
        (False, "A sentence with CODE and a link. A second one. It goes on here."),
        (True, "Run CODE . Then read the file."),
        (True, "Give the top module. It is one module."),
        (False, "An item that is not a step."),
        (False, "After the table."),
    ]


@pytest.mark.parametrize("path", DOCUMENTS, ids=lambda p: p.name)
def test_the_headings(path: Path):
    """D1."""
    found = re.findall(r"^## (.+?)\s*$", _FENCE.sub("", _text(path)), re.M)
    wanted = HEADINGS[path.name]
    assert [h for h in found if h in wanted] == wanted
    assert re.match(r"# \S", _text(path))


def _console(path: Path) -> list[str]:
    return [content for language, content in blocks(_text(path)) if language == "console"]


def test_every_vet_command_is_in_a_transcript():
    """D2."""
    for path in DOCUMENTS:
        for language, content in blocks(_text(path)):
            if language != "console":
                assert not re.search(r"^\s*\$?\s*vet\s", content, re.M), (path.name, language, content[:60])
    assert len(_console(README)) >= 2


@pytest.mark.parametrize("path", DOCUMENTS, ids=lambda p: p.name)
def test_the_console_blocks_print_what_the_document_shows(path: Path, tmp_path: Path):
    """D2. The blocks of a document are one series: a reader types them one after the other."""
    shown = _console(path)
    if not shown:
        return
    expected, actual = transcript.replay_text("".join(shown), tmp_path)
    if transcript.UPDATE and expected != actual:
        steps = transcript.parse(actual)
        text = _text(path)
        for block in shown:
            count = len(transcript.parse(block))
            assert text.count(block) == 1
            text = text.replace(block, transcript.render(steps[:count]))
            steps = steps[count:]
        path.write_bytes(text.encode("utf-8"))
        expected = actual
    assert actual == expected
    for block in shown:
        assert any(c.split()[0] == "vet" for c in transcript.commands(block))


def test_a_script_in_the_readme_is_a_tested_script():
    """D3."""
    scripts = {p.read_text(encoding="utf-8"): p.name for p in SCRIPTS.glob("*.py")}
    shown = [content for language, content in blocks(_text(README)) if language == "python"]
    assert shown
    names = []
    for content in shown:
        assert content in scripts, content[:80]
        names.append(scripts[content])
    commands = [c for block in _console(README) for c in transcript.commands(block)]
    for name in names:
        assert any(f"scripts/{name}" in c.split() for c in commands), name
    for path in DOCUMENTS:
        for language, _ in blocks(_text(path)):
            assert language in ("console", "python", "sh", "toml", "text"), (path.name, language)


@pytest.mark.parametrize("path", DOCUMENTS, ids=lambda p: p.name)
def test_relative_links_point_to_something(path: Path):
    """D4."""
    targets = re.findall(r"\]\(([^)\s]+)\)", _FENCE.sub("", _text(path)))
    relative = [t.split("#")[0] for t in targets if not re.match(r"[a-z]+:|#", t)]
    assert relative
    missing = sorted(t for t in relative if t and not _exists_exactly(REPO, t))
    assert missing == []


@pytest.mark.parametrize("path", DOCUMENTS, ids=lambda p: p.name)
def test_the_language_rules(path: Path):
    """D5."""
    found = []
    paragraphs = prose_md(_text(path))
    assert len(paragraphs) >= 15
    for numbered, text in paragraphs:
        parts = sentences(text)
        if len(parts) > 6:
            found.append(f"{len(parts)} sentences: {text[:70]}")
        limit = 20 if numbered else 25
        for sentence in parts:
            if words(sentence) > limit:
                found.append(f"{words(sentence)} words (limit {limit}): {sentence[:90]}")
        for pattern, instead in FORBIDDEN.items():
            for match in re.finditer(pattern, text, re.I):
                found.append(
                    f"'{match.group(0)}' (write: {instead}): ...{text[max(0, match.start() - 40) : match.end()]}"
                )
        for match in CONTRACTION.finditer(text):
            found.append(f"'{match.group(0)}': ...{text[max(0, match.start() - 40) : match.end()]}")
        if ";" in text:
            found.append(f"semicolon: {text[:70]}")
    assert found == []


def test_the_readme_names_no_package_index_and_no_version():
    """D6."""
    text = _text(README)
    assert "pypi" not in text.lower()
    assert not re.search(r"pip install (?:-U |--upgrade )?hdl-vet\b", text)
    assert hdl_vet.__version__ not in text
    assert "docs/manual" in text
    assert "CONTRIBUTING.md" in text
    assert "Apache-2.0" in text


def test_contributing_gives_the_commands_of_ci():
    """D7."""
    text = _text(CONTRIBUTING)
    for needed in (
        "ruff check",
        "ruff format --check",
        "mypy",
        "pytest",
        "tests/test_project_rules.py",
        "corpus",
        "HDL_BAG_REPO",
        "HDL_VET_REQUIRE_CORPUS",
        "HDL_VET_MANUAL_UPDATE=1",
        "latexmk",
        ".github/workflows/ci.yml",
        "tests/test_manual_style.py",
    ):
        assert needed in text, needed
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    pinned = re.search(r"HDL_BAG_REF:\s*([0-9a-f]{40})", ci)
    assert pinned
    assert pinned.group(1) not in text
    assert pinned.group(1)[:7] not in text


def test_contributing_states_the_owners_decisions():
    """D8."""
    text = _text(CONTRIBUTING)
    assert "Apache-2.0" in text
    assert "your own work" in text
    assert "Signed-off-by" not in text
    assert "LICENSE" in text
