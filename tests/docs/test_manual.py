"""Phase 5c, part 2a: the user manual. Rules of the manual, and the tests that hold it to them.

The owner's rule: every example a reader sees is run by a test. The manual is LaTeX
source in the repository; CI builds the PDF. Its reader is an RTL engineer who has
never used hdl-vet. English is not the first language of many readers.

LAYOUT (all under `docs/manual`)

    hdl-vet.tex          the main file: preamble, title, table of contents, \\input of the chapters
    version.tex          one line: \\newcommand{\\hdlvetversion}{<hdl_vet.__version__>}
    api.tex              one line per public name of the scripting API (see API below); generated
    chapters/*.tex       the text
    examples/design/     the example design (written by the planner; do not change it)
    examples/regenerated/  files of a later generator run; a transcript copies them over the design
    examples/cells/      cells that are not part of the design; scripts add them
    examples/scripts/    the example scripts, one file each
    examples/console/    console transcripts (format and rules: tests/support/transcript.py)

Nothing else lives under `examples/`. `.gitattributes` keeps `examples/` and the
`.tex` files on LF line ends on every machine, because a script's hash is printed
into the output.

RULES THE TESTS CHECK

M1  Version. `version.tex` is exactly the line above; pyproject's version agrees
    with `hdl_vet.__version__`. The manual prints the version only through
    \\hdlvetversion.
M2  Every file named by \\input, \\include, \\lstinputlisting or \\includegraphics
    exists, with the same spelling (CI builds on Linux). Paths are relative to
    `docs/manual`.
M3  No example is typed into the text. The environments lstlisting, verbatim,
    Verbatim, minted and alltt, and \\verbatiminput, are not used. Code and output are
    shown with \\lstinputlisting from a file under `examples/`. Short names inside a
    sentence (\\texttt, \\lstinline) are fine.
M4  Nothing under `examples/scripts`, `examples/console` or `examples/cells` is
    unused: the manual shows every such file with \\lstinputlisting.
M5  Every transcript replays exactly (tests/support/transcript.py), has at least one `vet`
    command, and every script is run by at least one transcript (its path
    `scripts/<name>.py` appears in a `vet` or `cp` command).
M6  API. `api.tex` is exactly: a comment line "% Generated. Checked by
    tests/docs/test_manual.py against hdl_vet.ops; rewrite it with
    HDL_VET_MANUAL_UPDATE=1." and then, sorted, one line
    \\apisig{Class.name}{signature} for every public method and property of `Vet`,
    `Module` and `Instance`. A signature is the parameter list without `self`
    and without annotations, string defaults in double quotes:
    \\apisig{Module.add_port}{(name, direction, type="logic", width=None)}; a
    property reads `property`. The preamble defines \\apisig to store an entry
    and \\sig{Class.name} to print it. The chapters use \\sig{...} once or more
    for every entry, so the reference is complete and cannot drift.
M7  An example for every operation: each name in OPERATIONS below is called in at
    least one example script that a transcript runs with exit status 0, and every
    keyword of `replace_module` is used in such a script.
M8  Every option of `vet apply` and `vet init` (long form) appears as
    \\opt{--name} in the chapters.
M9  Only the packages in PACKAGES are loaded, so that the manual builds the same
    with TeX Live (CI) and MiKTeX (the owner's machine). The preamble has
    \\DisableLigatures[-]{encoding = T1, family = tt*} (microtype): without it
    `--dry-run` is printed with a dash that cannot be copied.
M10 The `.tex` files and the examples are ASCII, and contain no TODO, TBD, FIXME
    or XXX.
M11 `.github/workflows/manual.yml` builds the PDF: on pull requests and pushes to
    main that touch `docs/manual/**` or the workflow itself, on workflow_dispatch,
    and on workflow_call (the release workflow of part 5c-3 will call it). It runs
    on ubuntu-24.04 with TeX Live from apt (the package `lmodern` is named: the
    texlive packages do not pull it in), runs
    `latexmk -pdf -interaction=nonstopmode -halt-on-error hdl-vet.tex` in
    `docs/manual`, fails when the log reports an undefined or multiply defined
    reference, and uploads `hdl-vet.pdf` as the artifact `hdl-vet-manual`.
    Third-party actions other than actions/checkout and actions/upload-artifact
    are not used. LaTeX build products are ignored by git; the sources are not.
M12 The message for an output directory that exists says `--force`, the option's
    name (today it says "use force"). This is the one change to `src/` in this part.

WHAT THE TESTS CANNOT CHECK (the reviewer does)

W1  Every statement is true of the code today. The sources of truth are the spec
    docstrings of the test files and the code; where they disagree with the plan,
    the code wins and the difference is reported.
W2  Plain words. Short sentences, present tense, "you". A term is defined where it
    first appears and used with one meaning (input tree, output tree, script,
    operation, sign-off, applied, skipped, drift, expectation). No marketing.
W3  Nothing is promised that does not exist. What is missing is listed in the
    chapter on limits, not hinted at elsewhere.
W4  A property is shown differently from a method (\\sig prints no parameter
    list for it), and a long signature breaks across lines. Each reference entry
    says: what the operation writes, when it is refused,
    what `was=` means for it (where it has one), and what it is when run a second
    time (skipped). Each has, or points to, a transcript that shows it.
W5  The PDF builds without errors, undefined references or overfull boxes wider
    than the margin, on TeX Live and on MiKTeX.

OUTLINE (chapter files in this order; names may differ, content may not be less)

 1  Introduction: what hdl-vet is for; the input tree is never changed, the output
    tree is a copy with the edits; text that is not edited stays byte-identical;
    sign-off decides whether the output is written; what it does not do.
 2  Installing: Python 3.11 or newer; from PyPI (`pip install hdl-vet`), from a
    checkout or from a wheel.
 3  A first project, start to finish, on the example design: `vet init`, what it
    writes, a first script (a new input port on `soc_top` and its connection),
    `--dry-run`, the real run, the output tree (header line, file list,
    `hdl-vet.json`), running it again on a regenerated input (copy
    `regenerated/rtl/soc_top.sv` over the design; the output tree itself is
    refused as an input).
 4  Writing scripts: `apply(vet)`; finding modules and instances (`vet.module`,
    `Module.instance`, `Module.instances`, `count=`/`instances=`, `paths`, `info`);
    applied and skipped; `was=` and drift; several scripts in order; `vet.group()`;
    `vet.scope()` (what is inside, what is refused and when, hierarchical paths
    inside a scope, references that reach outside; two scripts each with its
    own scope, and one that leaves its scope, as transcripts; the rules are the
    docstrings of tests/ops/test_scope.py and tests/ops/test_scope_review.py);
    `vet.notes`; a script is a program and runs with your rights.
 5  Operations, grouped: ports and connections; parameters; wires and assigns;
    through the hierarchy (`punch_port`, `propagate_param`, `all_instances`);
    replacing a module (`add_file`, `replace_module` with every keyword);
    removing things; expectations by hand (`expect`, `permit`,
    `allow_changes_below`) and `splice`.
 6  Sign-off: every line `session.report` can print (the edited design cannot be
    loaded; a new file is read; new diagnostics; structural changes against
    expectations; post-conditions of the operations; nothing left undriven that
    was driven, with the rule that an output may float and an input may not);
    how each failure reads and what to do. A failed sign-off is exit status 1;
    drift (a `was=` that does not match) is exit status 2. Name the top module: with no `--top`,
    a module whose last instance is replaced becomes a top-level module and
    sign-off reports `instance-added` (show it with a transcript).
 7  The command line: `vet apply` and `vet init`, every option; exit status 0, 1
    (sign-off failed) and 2 (the request cannot be carried out); `--force` and
    what it refuses.
 8  The project file: every key; what the command line replaces; which script
    runs first; where it is looked for.
 9  Saving and resuming a session: `--save`, `--resume`, what is and is not
    detected.
10  Using hdl-vet from Python: `Vet.check`, `finish`, `save`, `restore`, `text`,
    `design`, `module_paths`, `open_block` (reference only; no example is required).
11  Limits and known problems: from the "left open" lists of the plan, each
    checked against the code before it is written down.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

import hdl_vet
import transcript
from hdl_vet import ops
from hdl_vet.cli import _parser

REPO = transcript.REPO
MANUAL = transcript.MANUAL
EXAMPLES = transcript.EXAMPLES
CONSOLE = EXAMPLES / "console"
SCRIPTS = EXAMPLES / "scripts"

# M7: every one of these is called in at least one example script.
OPERATIONS = {
    "Vet": ["module", "group", "scope", "add_file", "punch_port", "propagate_param", "expect", "permit"],
    "Module": [
        "instance",
        "instances",
        "add_port",
        "add_param",
        "add_wire",
        "add_assign",
        "override_net",
        "remove_instance",
        "remove_wire",
        "remove_assign",
        "remove_port",
        "remove_param",
    ],
    "Instance": ["allow_changes_below", "connect", "disconnect", "set_param", "replace_module"],
}

# M9: in texlive-latex-recommended, texlive-latex-extra or texlive-fonts-recommended, and in MiKTeX.
PACKAGES = {
    "array",
    "booktabs",
    "caption",
    "enumitem",
    "fancyhdr",
    "fontenc",
    "geometry",
    "graphicx",
    "hyperref",
    "inputenc",
    "listings",
    "lmodern",
    "longtable",
    "makeidx",
    "microtype",
    "parskip",
    "tabularx",
    "textcomp",
    "titlesec",
    "upquote",
    "xcolor",
}

API_HEADER = (
    "% Generated. Checked by tests/docs/test_manual.py against hdl_vet.ops; rewrite it with HDL_VET_MANUAL_UPDATE=1."
)


def _strip_comments(text: str) -> str:
    return "\n".join(re.sub(r"(?<!\\)%.*", "", line) for line in text.splitlines())


def _tex_files() -> list[Path]:
    return [MANUAL / "hdl-vet.tex", *sorted((MANUAL / "chapters").glob("*.tex"))]


def _tex() -> str:
    return "\n".join(_strip_comments(p.read_text(encoding="utf-8")) for p in _tex_files())


def _chapters() -> str:
    return "\n".join(
        _strip_comments(p.read_text(encoding="utf-8")) for p in sorted((MANUAL / "chapters").glob("*.tex"))
    )


def _listed() -> set[str]:
    return set(re.findall(r"\\lstinputlisting(?:\[[^\]]*\])?\{([^}]*)\}", _tex()))


def _exists_exactly(relative: str) -> bool:
    """The file exists under docs/manual with exactly this spelling, also on Windows."""
    here = MANUAL
    for part in Path(relative).parts:
        if part in (".", ".."):
            return False
        if not here.is_dir() or part not in {p.name for p in here.iterdir()}:
            return False
        here = here / part
    return here.is_file()


def _transcripts() -> list[Path]:
    return sorted(CONSOLE.glob("*.txt"))


# ------------------------------------------------------------------ M1


def test_the_version_comes_from_the_package():
    assert (MANUAL / "version.tex").read_text(encoding="utf-8") == (
        "\\newcommand{\\hdlvetversion}{" + hdl_vet.__version__ + "}\n"
    )
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == hdl_vet.__version__
    assert "\\hdlvetversion" in _tex()
    assert hdl_vet.__version__ not in _tex()


# ------------------------------------------------------------------ M2


def test_every_file_the_manual_names_exists():
    tex = _tex()
    named = set(re.findall(r"\\(?:input|include)\{([^}]*)\}", tex))
    named = {n if n.endswith(".tex") else n + ".tex" for n in named}
    named |= _listed()
    named |= set(re.findall(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]*)\}", tex))
    assert {"version.tex", "api.tex"} <= named
    assert any(n.startswith("chapters/") for n in named)
    missing = sorted(n for n in named if not _exists_exactly(n))
    assert missing == []


def test_every_chapter_file_is_part_of_the_manual():
    tex = _tex()
    named = {n if n.endswith(".tex") else n + ".tex" for n in re.findall(r"\\(?:input|include)\{([^}]*)\}", tex)}
    chapters = {"chapters/" + p.name for p in (MANUAL / "chapters").glob("*.tex")}
    assert len(chapters) >= 11
    assert chapters <= named


# ------------------------------------------------------------------ M3


def test_no_example_is_typed_into_the_text():
    tex = _tex()
    for environment in ("lstlisting", "verbatim", "Verbatim", "minted", "alltt"):
        assert "\\begin{" + environment + "}" not in tex, environment
        assert "\\begin{" + environment + "*}" not in tex, environment
    assert "\\verbatiminput" not in tex
    outside = sorted(n for n in _listed() if not n.startswith("examples/"))
    assert outside == []


# ------------------------------------------------------------------ M4


def test_the_examples_folder_holds_nothing_unused():
    assert sorted(p.name for p in EXAMPLES.iterdir()) == ["cells", "console", "design", "regenerated", "scripts"]
    files = [p for folder in ("cells", "console", "scripts") for p in sorted((EXAMPLES / folder).rglob("*"))]
    files = [p for p in files if p.is_file()]
    assert all(p.suffix == ".py" for p in SCRIPTS.iterdir())
    assert all(p.suffix == ".txt" for p in CONSOLE.iterdir())
    unused = sorted({p.relative_to(MANUAL).as_posix() for p in files} - _listed())
    assert unused == []


def test_the_example_design_is_the_planners():
    """The design the examples run on: its file list, and that it loads with nothing to report."""
    assert (EXAMPLES / "design" / "design.f").read_text(encoding="utf-8").split() == [
        "rtl/rst_sync.sv",
        "rtl/timer.sv",
        "rtl/periph_wrap.sv",
        "rtl/soc_top.sv",
    ]
    assert sorted(p.name for p in (EXAMPLES / "cells").iterdir()) == ["tech_rst_sync.sv"]
    files = sorted(
        p.relative_to(EXAMPLES).as_posix()
        for folder in ("design", "regenerated")
        for p in (EXAMPLES / folder).rglob("*")
        if p.is_file()
    )
    assert files == [
        "design/design.f",
        "design/rtl/periph_wrap.sv",
        "design/rtl/rst_sync.sv",
        "design/rtl/soc_top.sv",
        "design/rtl/timer.sv",
        "regenerated/rtl/soc_top.sv",
    ]


def test_the_examples_use_lf_line_ends():
    attributes = (REPO / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "docs/manual/examples/** text eol=lf" in attributes
    assert "*.tex text eol=lf" in attributes
    with_cr = sorted(
        p.relative_to(REPO).as_posix() for p in EXAMPLES.rglob("*") if p.is_file() and b"\r" in p.read_bytes()
    )
    assert with_cr == []


# ------------------------------------------------------------------ M5


def test_there_are_transcripts():
    assert len(_transcripts()) >= 10


@pytest.mark.parametrize("path", _transcripts(), ids=lambda p: p.stem)
def test_a_transcript_prints_what_the_manual_shows(path: Path, tmp_path: Path):
    expected, actual = transcript.replay(path, tmp_path)
    assert actual == expected


@pytest.mark.parametrize("path", _transcripts(), ids=lambda p: p.stem)
def test_a_transcript_runs_vet(path: Path):
    commands = transcript.commands(path.read_text(encoding="utf-8"))
    assert any(c.split()[0] == "vet" for c in commands)
    assert not any(c.split()[0] == "vet" and {"-h", "--help"} & set(c.split()) for c in commands)


def test_every_script_is_run_by_a_transcript():
    commands = [c for p in _transcripts() for c in transcript.commands(p.read_text(encoding="utf-8"))]
    commands = [c for c in commands if c.split()[0] in ("vet", "cp")]
    scripts = sorted(SCRIPTS.glob("*.py"))
    assert scripts
    never_run = [s.name for s in scripts if not any(f"scripts/{s.name}" in c.split() for c in commands)]
    assert never_run == []


def test_the_replay_notices_a_wrong_transcript(tmp_path: Path):
    """The harness itself: a wrong line, a wrong exit status and a path outside the copy are caught."""
    good = "$ vet apply --no-project -f design/design.f --top soc_top --dry-run\n0 applied, 0 skipped\n"
    expected, actual = transcript.replay_text(good, tmp_path / "a")
    assert actual == expected

    expected, actual = transcript.replay_text(good.replace("0 skipped", "1 skipped"), tmp_path / "b")
    assert actual != expected

    expected, actual = transcript.replay_text("$ vet apply --no-project -f design/none.f --dry-run\n", tmp_path / "c")
    assert actual.splitlines()[-1] == "[exit status 2]"
    assert actual != expected

    with pytest.raises(ValueError, match="leaves the copy"):
        transcript.replay_text("$ cat ../../../../pyproject.toml\n", tmp_path / "d")
    with pytest.raises(ValueError, match="not a transcript command"):
        transcript.replay_text("$ rm -rf design\n", tmp_path / "e")
    with pytest.raises(ValueError, match="--help"):
        transcript.replay_text("$ vet apply --help\n", tmp_path / "f")
    with pytest.raises(ValueError, match="usage"):
        transcript.replay_text("$ vet apply -f\n", tmp_path / "g")


def test_the_replay_refuses_what_differs_between_machines(tmp_path: Path):
    """A script that raises (a traceback) or warns cannot be shown; nor can a path of this machine."""
    scripts = EXAMPLES / "scripts"
    run = "$ vet apply --no-project -f design/design.f --top soc_top --dry-run -s "
    for name, body, reason in (
        ("zz_raises.py", "def apply(vet):\n    raise RuntimeError(1)\n", "traceback"),
        ("zz_warns.py", "import warnings\n\n\ndef apply(vet):\n    warnings.warn('old', stacklevel=1)\n", "warning"),
    ):
        path = scripts / name
        path.write_text(body, encoding="utf-8")
        try:
            with pytest.raises(ValueError, match=reason):
                transcript.replay_text(run + "scripts/" + name + "\n", tmp_path / name)
        finally:
            path.unlink()


def test_the_replay_shows_paths_the_same_everywhere(tmp_path: Path):
    text = "$ vet apply --no-project -f design/design.f --top soc_top -o out/first\n$ ls out\n"
    _, actual = transcript.replay_text(text, tmp_path)
    lines = actual.splitlines()
    assert lines[1] in ("wrote out/first", "wrote <examples>/out/first")
    assert lines[-1] == "first/"
    assert "\\" not in actual


def test_an_existing_output_directory_names_the_option(tmp_path: Path):
    """M12."""
    _, actual = transcript.replay_text("$ vet apply --no-project -f design/design.f --top soc_top -o cells\n", tmp_path)
    assert "--force" in actual
    assert actual.splitlines()[-1] == "[exit status 2]"


def test_the_replay_hides_the_machine(tmp_path: Path):
    text = "$ cp design/design.f copy.f\n$ ls\n$ vet --version\n"
    _, actual = transcript.replay_text(text, tmp_path)
    assert actual.splitlines() == [
        "$ cp design/design.f copy.f",
        "$ ls",
        "cells/",
        "console/",
        "copy.f",
        "design/",
        "regenerated/",
        "scripts/",
        "$ vet --version",
        "hdl-vet <version>",
    ]
    assert str(tmp_path) not in actual


# ------------------------------------------------------------------ M6


def _default(value: object) -> str:
    return json.dumps(value) if isinstance(value, str) else repr(value)


def _signature(cls: type, name: str) -> str:
    if isinstance(inspect.getattr_static(cls, name), property):
        return "property"
    parts: list[str] = []
    star = False
    for p in inspect.signature(getattr(cls, name)).parameters.values():
        if p.name in ("self", "cls"):
            continue
        if p.kind is p.VAR_POSITIONAL:
            parts.append("*" + p.name)
            star = True
            continue
        if p.kind is p.VAR_KEYWORD:
            parts.append("**" + p.name)
            continue
        if p.kind is p.KEYWORD_ONLY and not star:
            parts.append("*")
            star = True
        parts.append(p.name if p.default is p.empty else p.name + "=" + _default(p.default))
    return "(" + ", ".join(parts) + ")"


def _api() -> dict[str, str]:
    api = {}
    for cls in (ops.Vet, ops.Module, ops.Instance):
        for klass in cls.__mro__[:-1]:  # the class and its bases; a method may be defined in a mixin
            for name in vars(klass):
                if not name.startswith("_"):
                    api[f"{cls.__name__}.{name}"] = _signature(cls, name)
    return api


def test_the_signature_rendering():
    api = _api()
    assert api["Module.add_port"] == '(name, direction, type="logic", width=None)'
    assert api["Vet.expect"] == '(kind, path="*", name="*", *, exact=False)'
    assert api["Vet.restore"] == "(path)"
    assert api["Vet.group"] == "()"
    assert api["Module.paths"] == "property"
    assert api["Instance.replace_module"].startswith("(module, *, ports=None, params=None, connect=None")
    assert "drop_ports=()" in api["Instance.replace_module"]
    assert len(api) >= 35


def test_api_tex_lists_the_scripting_api():
    api = _api()
    wanted = "\n".join([API_HEADER, *(f"\\apisig{{{key}}}{{{api[key]}}}" for key in sorted(api))]) + "\n"
    path = MANUAL / "api.tex"
    if transcript.UPDATE:
        path.write_bytes(wanted.encode("utf-8"))
    assert path.read_text(encoding="utf-8") == wanted


def test_the_manual_has_an_entry_for_every_name_of_the_api():
    chapters = _chapters()
    shown = set(re.findall(r"\\sig\{([^}]*)\}", chapters))
    api = set(_api())
    assert sorted(api - shown) == []
    assert sorted(shown - api) == []
    assert "\\apisig" in (MANUAL / "hdl-vet.tex").read_text(encoding="utf-8")
    assert "vet.notes" in chapters.replace("\\_", "_")


# ------------------------------------------------------------------ M7


def _scripts_that_pass() -> list[Path]:
    """The example scripts that some transcript runs in a `vet` command that exits with status 0."""
    passing: set[str] = set()
    for path in _transcripts():
        for command, lines in transcript.parse(path.read_text(encoding="utf-8")):
            words = command.split()
            if words[0] == "vet" and not any(line.startswith("[exit status") for line in lines):
                passing |= {Path(w).name for w in words if w.startswith("scripts/") or "/scripts/" in w}
    return [p for p in sorted(SCRIPTS.glob("*.py")) if p.name in passing]


def _calls(paths: list[Path]) -> tuple[set[str], set[str], set[str]]:
    """(method names called, keywords of replace_module calls, every keyword) in these scripts."""
    names: set[str] = set()
    swap: set[str] = set()
    keywords: set[str] = set()
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
                found = {k.arg for k in node.keywords if k.arg}
                keywords |= found
                if node.func.attr == "replace_module":
                    swap |= found
                if node.func.attr == "punch_port" and any(
                    k.arg == "all_instances" and isinstance(k.value, ast.Constant) and k.value.value is True
                    for k in node.keywords
                ):
                    keywords.add("all_instances=True")
    return names, swap, keywords


def test_every_operation_has_an_example_script():
    api = _api()
    names, swap, keywords = _calls(_scripts_that_pass())
    wanted = [f"{cls}.{name}" for cls, group in OPERATIONS.items() for name in group]
    assert all(key in api for key in wanted)
    assert sorted(key for key in wanted if key.split(".")[1] not in names) == []
    assert swap >= {"ports", "params", "connect", "set_params", "drop_ports", "drop_params", "unconnected", "was"}
    assert "was" in keywords
    assert "all_instances=True" in keywords


def test_every_example_script_defines_apply():
    for path in sorted(SCRIPTS.glob("*.py")):
        assert re.search(r"^def apply\(vet\)", path.read_text(encoding="utf-8"), re.M), path.name


# ------------------------------------------------------------------ M8


def _options(parser: argparse.ArgumentParser) -> set[str]:
    found: set[str] = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                found |= _options(sub)
        found |= {o for o in action.option_strings if o.startswith("--")}
    return found - {"--help"}


def test_every_option_is_in_the_manual():
    options = _options(_parser())
    assert {"--filelist", "--dry-run", "--resume", "--no-project", "--version"} <= options
    shown = set(re.findall(r"\\opt\{([^}]*)\}", _chapters()))
    assert sorted(options - shown) == []
    assert sorted(o for o in shown if o.startswith("--") and o not in options) == []


# ------------------------------------------------------------------ M9, M10


def test_only_the_agreed_packages_are_loaded():
    loaded: set[str] = set()
    for names in re.findall(r"\\(?:usepackage|RequirePackage)(?:\[[^\]]*\])?\{([^}]*)\}", _tex()):
        loaded |= {n.strip() for n in names.split(",")}
    assert sorted(loaded - PACKAGES) == []
    assert {"listings", "hyperref", "microtype"} <= loaded
    assert re.search(r"\\DisableLigatures\[-\]\{encoding\s*=\s*T1,\s*family\s*=\s*tt\*\}", _tex())


def test_the_text_is_ascii_and_finished():
    marks = re.compile(r"\b(TODO|TBD|FIXME|XXX)\b")
    for path in [*_tex_files(), MANUAL / "api.tex", MANUAL / "version.tex"]:
        data = path.read_bytes()
        assert data.isascii(), path.name
        assert not marks.search(data.decode()), path.name
    for path in sorted(EXAMPLES.rglob("*")):
        if path.is_file():
            assert path.read_bytes().isascii(), path.name
            assert not marks.search(path.read_text(encoding="utf-8")), path.name


# ------------------------------------------------------------------ M11


def test_the_workflow_that_builds_the_pdf():
    text = (REPO / ".github" / "workflows" / "manual.yml").read_text(encoding="utf-8")
    for needed in (
        "docs/manual/**",
        ".github/workflows/manual.yml",
        "workflow_dispatch",
        "workflow_call",
        "ubuntu-24.04",
        "lmodern",
        "latexmk -pdf -interaction=nonstopmode -halt-on-error hdl-vet.tex",
        "name: hdl-vet-manual",
        "hdl-vet.pdf",
    ):
        assert needed in text, needed
    used = set(re.findall(r"uses:\s*([^@\s]+)@", text))
    assert used <= {"actions/checkout", "actions/upload-artifact"}
    git = shutil.which("git")
    assert git

    def ignored(name: str) -> bool:
        return subprocess.run([git, "check-ignore", "-q", name], cwd=REPO, check=False).returncode == 0

    for product in ("hdl-vet.aux", "hdl-vet.log", "hdl-vet.pdf", "hdl-vet.fdb_latexmk", "hdl-vet.toc"):
        assert ignored("docs/manual/" + product), product
    for source in ("hdl-vet.tex", "api.tex", "chapters/intro.tex", "examples/console/first.txt"):
        assert not ignored("docs/manual/" + source), source
