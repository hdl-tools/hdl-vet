"""Rules of the code base that used to be checked by hand in every review.

- Only `hdl_vet.slang_adapter` imports pyslang. Everything else talks to the
  adapter, so a pyslang upgrade touches one package.
- Nothing in the package can run code from data: no pickle, marshal or shelve,
  and no `eval` or `exec`, with one exception: the command line executes the
  edit scripts the user names (`cli._load_script`).
- No module of the package imports from the tests.
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
