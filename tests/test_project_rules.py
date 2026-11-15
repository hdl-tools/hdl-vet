"""Rules of the code base that used to be checked by hand in every review.

    - Only `hdl_vet.slang_adapter` imports pyslang. Everything else talks to the
      adapter, so a pyslang upgrade touches one module.
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

PACKAGE = Path(hdl_vet.__file__).resolve().parent
MODULES = sorted(PACKAGE.glob("*.py"))
CODE_FROM_DATA = {"pickle", "cPickle", "marshal", "shelve", "dill"}


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
    assert len(MODULES) >= 10 and (PACKAGE / "slang_adapter.py") in MODULES


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_only_the_adapter_imports_pyslang(path: Path):
    names = imported(ast.parse(path.read_bytes()))
    if path.name == "slang_adapter.py":
        assert "pyslang" in names
    else:
        assert "pyslang" not in names, f"{path.name} imports pyslang; only slang_adapter.py may"


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_nothing_runs_code_from_data(path: Path):
    tree = ast.parse(path.read_bytes())
    assert not imported(tree) & CODE_FROM_DATA
    found = calls(tree, {"eval", "exec", "compile", "__import__"})
    allowed = [("_load_script", "exec"), ("_load_script", "compile")] if path.name == "cli.py" else []
    assert sorted(found) == sorted(allowed)


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_the_package_does_not_import_the_tests(path: Path):
    helpers = {p.stem for p in Path(__file__).resolve().parent.glob("*.py")}
    assert not imported(ast.parse(path.read_bytes())) & helpers
