"""Rules of the code base that used to be checked by hand in every review.

- Only `hdl_vet.slang_adapter` imports pyslang. Everything else talks to the
  adapter, so a pyslang upgrade touches one package.
- Nothing in the package can run code from data: no pickle, marshal or shelve,
  and no `eval` or `exec`, with one exception: the command line executes the
  edit scripts the user names (`cli._load_script`).
- No module of the package imports from the tests.
- No source file holds an invisible character. A control byte in a string looks
  like nothing in a terminal, in a diff and in a review. One sat in a regular
  expression replacement for months and sent itself into Yosys diagnostics
  (#37), and three readings of the line in a row described the wrong defect.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import hdl_vet
from repo import REPO

PACKAGE = Path(hdl_vet.__file__).resolve().parent
MODULES = sorted([*PACKAGE.glob("*.py"), *(p for p in PACKAGE.iterdir() if (p / "__init__.py").is_file())])
CODE_FROM_DATA = {"pickle", "cPickle", "marshal", "shelve", "dill"}


def parse(path: Path) -> ast.Module:
    """The syntax tree of a module; of a package, one tree with the bodies of all its modules."""
    files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
    return ast.Module(body=[n for p in files for n in ast.parse(p.read_bytes()).body], type_ignores=[])


def imported(tree: ast.AST) -> set[str]:
    """Top-level names of every module imported anywhere in `tree`."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def calls(tree: ast.AST, wanted: set[str]) -> list[tuple[str, str]]:
    """(function the call is in, name called) for every call of a name in `wanted`."""
    found: list[tuple[str, str]] = []

    def visit(node: ast.AST, where: str) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            where = node.name
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in wanted:
            found.append((where, node.func.id))
        for child in ast.iter_child_nodes(node):
            visit(child, where)

    visit(tree, "<module>")
    return found


def test_the_package_has_the_modules_this_file_checks():
    assert len(MODULES) >= 10 and (PACKAGE / "slang_adapter") in MODULES


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_only_the_adapter_imports_pyslang(path: Path):
    names = imported(parse(path))
    if path.name == "slang_adapter":
        assert "pyslang" in names
    else:
        assert "pyslang" not in names, f"{path.name} imports pyslang; only slang_adapter may"


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_nothing_runs_code_from_data(path: Path):
    tree = parse(path)
    assert not imported(tree) & CODE_FROM_DATA
    found = calls(tree, {"eval", "exec", "compile", "__import__"})
    allowed = [("_load_script", "exec"), ("_load_script", "compile")] if path.name == "cli.py" else []
    assert sorted(found) == sorted(allowed)


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_the_package_does_not_import_the_tests(path: Path):
    helpers = {p.stem for p in (REPO / "tests").rglob("*.py")}
    assert not imported(parse(path)) & helpers


# --- invisible characters ------------------------------------------------------------


# Tab and newline are the only control characters a source file has any business holding.
ALLOWED_CONTROL = {"\t", "\n", "\r"}
# Characters that are not control codes but still show as nothing, or as an ordinary
# space, so a reader cannot tell them from what they expect.
LOOKS_LIKE_NOTHING = {
    "\u00a0",  # no-break space
    "\u200b",  # zero width space
    "\u200c",  # zero width non-joiner
    "\u200d",  # zero width joiner
    "\u2060",  # word joiner
    "\ufeff",  # byte order mark, when it is not the first character
    "\u202a",  # the bidirectional overrides, which can reorder a line as shown
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
}


def every_source_file() -> list[Path]:
    """The Python of the package and of the tests, and the files that describe the build."""
    return sorted(
        [
            *(REPO / "src").rglob("*.py"),
            *(REPO / "tests").rglob("*.py"),
            *(REPO / ".github").rglob("*.yml"),
            REPO / "pyproject.toml",
        ]
    )


def test_this_rule_looks_at_the_files_it_means_to():
    files = every_source_file()
    assert len(files) > 100
    assert any(p.name == "equiv.py" for p in files)
    assert any(p.name == "ci.yml" for p in files)


def test_no_source_file_holds_an_invisible_character():
    """The rule that would have caught #37 in review instead of months later."""
    found = []
    for path in every_source_file():
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            for column, char in enumerate(line, start=1):
                bad = (char.isprintable() is False and char not in ALLOWED_CONTROL) or char in LOOKS_LIKE_NOTHING
                if bad:
                    where = f"{path.relative_to(REPO).as_posix()}:{number}:{column}"
                    found.append(f"{where}: {char!r} in {line.strip()[:60]!r}")
    assert found == []
