"""The layout of `src` and `tests`. Rules and the tests that hold them.

The owner asked for the two folders to be organised (2026-11-17): the tests in
subfolders by area, and the two large modules of the package split. This is a
reorganisation: code moves, and no behaviour changes.

TESTS

L1  A test file lives in one of these folders, by what it tests:
      tests/core     the file list, splices, the adapter, the session, sign-off
      tests/ops      the operations: ports to scopes, and the fixes found later
      tests/cli      the command line, the project file, saved sessions
      tests/docs     the manual, the README, the release, the rules of the code
      tests/equiv    the equivalence engine
      tests/perf     the cost of re-loading and the reuse of syntax trees
    A corpus test (`*_corpus.py`) lives beside the area it tests. No test file
    is directly in `tests/`; `tests/smoke_wheel.py` stays there (CI runs it by
    that name, and it is not a pytest file), and `tests/conftest.py` stays there
    (pytest reads it by that name, and it holds no test).
L2  Helpers live in `tests/support`: `vetkit`, `corpus`, `variants`,
    `transcript`, and the designs and helper functions that one test file used
    to import from another. A test file does not import from another test
    file. Helpers and test files are imported by their plain names, as before
    (`from vetkit import make_design`); `pyproject.toml` puts `tests/support` on
    the path.
L3  The names of the test files and of the tests in them do not change, and no
    test is lost: the planner compares the list of test IDs before and after.
    A file keeps its history (`git mv`).
L4  Every place that names a test file by its path names the new path: CI,
    CONTRIBUTING.md, the manual, the docstrings in `src` and in the tests.

THE PACKAGE

L5  `hdl_vet.ops` and `hdl_vet.slang_adapter` are packages. What other code
    imports from them is unchanged: `from hdl_vet.ops import Vet, Module,
    Instance, VetError, DriftError, OpRecord`, and every name that the rest of
    the package and the tests import from `hdl_vet.slang_adapter` today.
L6  No module in the two packages has more than 900 lines.
L7  Only the modules of the package `hdl_vet.slang_adapter` import pyslang
    (the rule of tests/docs/test_project_rules.py, for a package now).
L8  The scripting interface is the same: the public methods and properties of
    `Vet`, `Module` and `Instance`, with the same signatures, as the manual's
    `api.tex` lists them. A method may now be defined in a base class of its
    class; a script cannot tell.

WHAT THE REVIEWER CHECKS

L9  Nothing but the place changed. Every function and method of the two old
    modules is in the new packages with the same body, apart from what the
    split itself needs (imports, the class a helper is reached through). The
    same holds for every test function.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import hdl_vet
from hdl_vet import ops, slang_adapter

REPO = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").is_file())
TESTS = REPO / "tests"
PACKAGE = REPO / "src" / "hdl_vet"
AREAS = {"core", "ops", "cli", "docs", "equiv", "perf"}
MAX_LINES = 900


def _test_files() -> list[Path]:
    return sorted(p for p in TESTS.rglob("test_*.py"))


def test_every_test_file_is_in_an_area():
    """L1."""
    files = _test_files()
    assert len(files) >= 40
    wrong = sorted(
        p.relative_to(TESTS).as_posix() for p in files if p.parent.name not in AREAS or p.parent.parent != TESTS
    )
    assert wrong == []
    assert sorted(p.name for p in TESTS.iterdir() if p.is_file()) == ["conftest.py", "smoke_wheel.py"]
    assert {p.name for p in TESTS.iterdir() if p.is_dir() and p.name != "__pycache__"} == AREAS | {"support"}
    for area in AREAS:
        assert list((TESTS / area).glob("test_*.py")), area


def test_a_test_runs_where_no_project_file_is_found():
    """L1. `vet apply` reads `hdl-vet.toml` from the current directory.

    Without the fixture of `tests/conftest.py` a test that does not change directory
    inherits the one pytest was started in, normally this repository's root, and a
    project file there reaches every such test. Ten tests of `tests/cli` failed that way
    before #64. This holds the fixture in place: it is the invariant, not the mechanism.
    """
    here = Path.cwd()
    assert here != REPO, "a test is running in the repository root, where a project file can be found"
    assert not (here / "hdl-vet.toml").exists(), f"{here} holds a project file"


def test_a_corpus_test_is_beside_its_area():
    """L1."""
    where = {
        p.name: p.parent.name for p in _test_files() if p.name.endswith("_corpus.py") or p.name == "test_corpus.py"
    }
    assert where == {
        "test_corpus.py": "core",
        "test_pipeline_corpus.py": "core",
        "test_ops_corpus.py": "ops",
        "test_hier_corpus.py": "ops",
        "test_instances_corpus.py": "ops",
        "test_override_corpus.py": "ops",
        "test_save_resume_corpus.py": "cli",
    }


def test_the_names_of_the_test_files_are_unique():
    names = [p.name for p in _test_files()]
    assert len(names) == len(set(names))


def test_helpers_are_in_support_and_no_test_file_imports_another():
    """L2."""
    support = {p.stem for p in (TESTS / "support").glob("*.py")}
    assert {"vetkit", "corpus", "variants", "transcript"} <= support
    assert not any(name.startswith("test_") for name in support)
    offenders = []
    for path in [*_test_files(), *sorted((TESTS / "support").glob("*.py")), TESTS / "smoke_wheel.py"]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            offenders += [f"{path.name}: {n}" for n in names if n.split(".")[0].startswith("test_")]
    assert offenders == []
    config = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert '"tests/support"' in config


def test_paths_written_down_name_files_that_exist():
    """L4."""
    texts = [
        REPO / ".github" / "workflows" / "ci.yml",
        REPO / "CONTRIBUTING.md",
        REPO / "README.md",
        REPO / "docs" / "manual" / "hdl-vet.tex",
        REPO / "docs" / "manual" / "api.tex",
        *sorted((REPO / "docs" / "manual" / "chapters").glob("*.tex")),
        *sorted(PACKAGE.rglob("*.py")),
        *_test_files(),
        *sorted((TESTS / "support").glob("*.py")),
    ]
    missing = []
    for path in texts:
        for named in re.findall(r"tests/[A-Za-z0-9_/]+\.py", path.read_text(encoding="utf-8")):
            if not (REPO / named).is_file():
                missing.append(f"{path.relative_to(REPO).as_posix()}: {named}")
    assert sorted(set(missing)) == []


def test_the_two_large_modules_are_packages():
    """L5, L6."""
    for name in ("ops", "slang_adapter"):
        folder = PACKAGE / name
        assert (folder / "__init__.py").is_file(), name
        assert not (PACKAGE / f"{name}.py").exists(), name
        modules = sorted(folder.glob("*.py"))
        assert len(modules) >= 4, name
        long = [f"{p.name}: {len(p.read_text(encoding='utf-8').splitlines())}" for p in modules]
        long = [entry for entry in long if int(entry.split(": ")[1]) > MAX_LINES]
        assert long == [], name
    assert sorted(ops.__all__) == ["DriftError", "Instance", "Module", "OpRecord", "Vet", "VetError"]
    for public in ("load", "Design", "AdapterError", "ModuleInfo", "InstanceInfo", "InstanceSnap", "Diag"):
        assert hasattr(slang_adapter, public), public
    assert hdl_vet.__version__


def test_only_the_adapter_package_imports_pyslang():
    """L7."""
    importers = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            if any(m.split(".")[0] == "pyslang" for m in modules):
                importers.append(path.relative_to(PACKAGE).as_posix())
    assert importers
    assert all(name.startswith("slang_adapter/") for name in importers), sorted(set(importers))


def test_the_scripting_interface_is_complete():
    """L8: the names of api.tex are the public names of the three classes, wherever they are defined."""
    listed = set(re.findall(r"\\apisig\{([^}]*)\}", (REPO / "docs" / "manual" / "api.tex").read_text(encoding="utf-8")))
    found = set()
    for cls in (ops.Vet, ops.Module, ops.Instance):
        for name, _ in inspect.getmembers(cls):
            if not name.startswith("_"):
                found.add(f"{cls.__name__}.{name}")
    assert sorted(listed - found) == []
    assert sorted(found - listed) == []


def test_contributing_describes_the_layout():
    """The owner's condition: the names may differ from the first sketch as long as the documents show them."""
    text = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    missing = []
    for package in ("ops", "slang_adapter"):
        assert f"`{package}/`" in text, package
        for path in sorted((PACKAGE / package).glob("*.py")):
            if path.name != "__init__.py" and f"`{path.name}`" not in text:
                missing.append(f"{package}/{path.name}")
    for path in sorted(PACKAGE.glob("*.py")):
        if not path.name.startswith("__") and f"`{path.name}`" not in text:
            missing.append(path.name)
    for area in sorted(AREAS | {"support"}):
        if f"| `{area}` |" not in text:
            missing.append(f"tests/{area}")
    assert missing == []
