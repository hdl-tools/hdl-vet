"""Phase 5c-1: the project file and `vet init`.

Owner decisions:
    - A project file read by vet: `vet apply` with no arguments runs a project.
    - `vet init` writes a starter project that knows the design: the real file
      list, top module and instance names are filled in.
    - `-s` on the command line adds to the project's scripts: they run after
      them. `--no-project` ignores the project file altogether.

The project file: `hdl-vet.toml`

    [design]
    filelist = "../gen/design.f"    # required when the project supplies the design
    top = "top"                     # optional
    base = "../gen"                 # optional, as --base

    [apply]
    scripts = ["scripts/01_example.py"]    # optional, in order
    output = "out"                         # optional

    Paths are relative to the directory of the project file ("/" separators;
    absolute paths are taken as they are).
    The file is checked strictly: an unknown table or key, a value of the
    wrong type, an empty string, a nested table or an array of tables where a
    table is expected, or text that is not TOML is an error naming the file
    and the table or key. Nothing is guessed. The file is read as UTF-8 (a
    byte-order mark is accepted); bytes that are not UTF-8 are such an error.

How `vet apply` uses it
    - Found: `hdl-vet.toml` in the current directory, or the file named by
      `--project FILE`. There is no search in parent directories.
      `--no-project` ignores it. `--project` with `--no-project`, and a
      `--project` file that does not exist, are usage errors.
    - Whenever a project file is used, the first line on stderr is
      "project: <absolute, resolved path of the file>", so that a project is
      never applied unnoticed. It is printed as soon as the file has been
      found, before its content is checked.
    - Design: `--top` on the command line replaces the project's. `-f` replaces
      the project's file list and its `base` together (a base belongs to its
      file list); `--base` replaces the base. With `--resume` the project's
      design is not used (the saved session has it).
    - Scripts: the project's, then those given with `-s`. When `-f` is given
      and the project has scripts, a second stderr line says so:
      "note: the project's scripts run first: a.py, b.py".
      A script named twice in the combined list (the same file) is an error,
      with or without a project.
    - Scripts with `--resume`: the saved session already applied some. The
      project's list and the saved list are compared from the start, by name
      and content. Where both have a script they must be the same one;
      otherwise an error names the first that differs (the project changed
      since the save; `--no-project` resumes regardless). The project's
      scripts beyond the saved ones run, then the `-s` ones.
    - Output: the project's `output` is used only when the command line names
      none of `-o`, `--save` and `--dry-run`. So `vet apply --save s.zip` saves
      and writes no tree, as without a project.
    - Everything else is as before: `--force` is needed to replace an output
      directory that exists, every check on `--save` and `--resume` applies to
      the combined settings (the project file itself counts as a file `--save`
      must not replace), and without a project file nothing changes.
    - Paths on the command line (`-f`, `-s`, `-o`, `--save`, `--resume`) are
      relative to the current directory, also when `--project` names a file
      elsewhere.
    Not deleting the project: writing the output tree replaces its directory.
    So the output directory, wherever it comes from, must not contain a script
    being run or the project file; a project's `output` must not be the
    project's directory or contain it. And when the output directory comes
    from the project file, `--force` replaces it only if it is empty or is an
    earlier hdl-vet output (it holds `hdl-vet.json`); anything else is refused
    with a message that says so.
    All of these errors make `main` return 2 with a message on stderr; they
    are checked by vet, not left to argparse (which would exit).

`vet init DIR -f FILELIST [--top TOP] [--base BASE]`
    Loads the design (so a design that cannot be loaded, or has errors, is
    refused) and writes a starter project into DIR:

        hdl-vet.toml             file list, top, base (when given), the example
                                 script, output = "out"
        scripts/01_example.py    `apply(vet)` that changes nothing, with the
                                 design's real names: the top module, and its
                                 instances listed in comments with ready-made
                                 example operations, commented out
        cells/README.md          where files for `vet.add_file` go
        README.md                the commands to run, for this project
        .gitignore               the output directory and saved sessions

    - The top: `--top`, or the design's only top-level module. With several
      and no `--top`: an error that lists them and asks for `--top`. This is
      decided before the design is checked for errors: without a top, library
      modules that nothing instantiates count as tops and may have errors of
      their own (an unconnected interface port), which must not hide the
      question.
    - The file list and base are written relative to DIR when they are on the
      same drive, with "/" separators; otherwise absolute.
    - DIR and its parents are created. A DIR that exists and is not empty is
      refused; so is a DIR inside the design's input tree (regenerating would
      delete it). Nothing is written when `init` refuses. `init` without `-f`
      is a usage error (return 2).
    - The files do not depend on the time or the machine beyond the paths
      given, and none of them names DIR itself: the same arguments give the
      same bytes. Lines end with LF on every platform.
    - Strings in `hdl-vet.toml` are written as TOML basic strings: a backslash
      and a double quote are escaped, U+0000 to U+001F and U+007F are written
      as four-digit Unicode escapes, everything else as UTF-8. Names in the
      example script are written as double-quoted Python string literals.
    - Instances the operations cannot edit (in a generate block, an instance
      array, from a macro) are listed in the example script with a note, and
      without an example operation. The script is not executed by `init`.
    - stdout: "created <DIR as given>", then one line "wrote <path relative to DIR>"
      per file, in the order above. Exit status 0.
    - Right after `init`, `vet apply --dry-run` in DIR succeeds and reports
      "0 applied, 0 skipped": the example script is a script that runs.

Python: `hdl_vet.project`
    PROJECT_FILE = "hdl-vet.toml"
    load_project(path) -> Project      frozen dataclass: path, filelist, top,
                                       base, scripts (tuple), output; every
                                       path absolute and resolved (None where
                                       the file gives none); ProjectError
                                       otherwise
    init_project(directory, filelist, top=None, base=None) -> list[Path]
                                       the files written, in order.
                                       ProjectError for its own refusals (not
                                       empty, inside the input tree, several
                                       tops); errors of the file list or the
                                       design keep their own types
    ProjectError(Exception)
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from hdl_vet.cli import main
from hdl_vet.project import PROJECT_FILE, ProjectError, init_project, load_project
from vetkit import make_design, tree_bytes

FILES = ["hdl-vet.toml", "scripts/01_example.py", "cells/README.md", "README.md", ".gitignore"]

PORT = 'def apply(vet):\n    vet.module("top").add_port("dft_en", "input")\n'
WIRE = 'def apply(vet):\n    vet.module("mid").add_wire("w")\n'
SPARE = 'def apply(vet):\n    vet.module("top").remove_instance("u_spare")\n'


def run(*args: object) -> int:
    return main([str(a) for a in args])


@pytest.fixture
def kit(tmp_path: Path):
    return make_design(tmp_path / "gen")


@pytest.fixture
def proj(kit, tmp_path: Path, monkeypatch, capsys):
    """A project made by `vet init`, with two scripts of its own; the current directory is the project."""
    root = (tmp_path / "proj").resolve()
    assert run("init", root, "-f", kit.filelist, "--top", "top") == 0
    capsys.readouterr()
    (root / "scripts" / "10_port.py").write_text(PORT, encoding="utf-8")
    (root / "scripts" / "20_wire.py").write_text(WIRE, encoding="utf-8")
    (root / "extra.py").write_text(SPARE, encoding="utf-8")
    write_project(root, scripts=["scripts/10_port.py", "scripts/20_wire.py"])
    monkeypatch.chdir(root)
    return root


def write_project(root: Path, scripts: list[str] | None = None, output: str | None = "out", **design: str) -> None:
    design = {"filelist": "../gen/design.f", "top": "top", **design}
    lines = ["[design]", *(f'{k} = "{v}"' for k, v in design.items()), "", "[apply]"]
    if scripts is not None:
        lines.append("scripts = [" + ", ".join(f'"{s}"' for s in scripts) + "]")
    if output is not None:
        lines.append(f'output = "{output}"')
    (root / PROJECT_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")


def manifest_scripts(out: Path) -> list[str]:
    return [s["name"] for s in json.loads((out / "hdl-vet.json").read_bytes())["scripts"]]


# --- vet init ----------------------------------------------------------------------------------


def test_init_writes_a_project_that_knows_the_design(kit, tmp_path: Path, capsys):
    root = (tmp_path / "proj").resolve()
    assert run("init", root, "-f", kit.filelist, "--top", "top") == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [f"created {root}", *(f"wrote {name}" for name in FILES)]
    assert sorted(tree_bytes(root)) == sorted(FILES)
    index = tomllib.loads((root / "hdl-vet.toml").read_text(encoding="utf-8"))
    assert index == {
        "design": {"filelist": "../gen/design.f", "top": "top"},
        "apply": {"scripts": ["scripts/01_example.py"], "output": "out"},
    }
    for name in FILES:
        data = (root / name).read_bytes()
        assert data.endswith(b"\n") and b"\r" not in data, name


def test_the_example_script_names_the_real_design_and_runs(kit, tmp_path: Path, capsys):
    root = (tmp_path / "proj").resolve()
    assert run("init", root, "-f", kit.filelist, "--top", "top") == 0
    text = (root / "scripts" / "01_example.py").read_text(encoding="utf-8")
    namespace: dict[str, object] = {}
    exec(compile(text, "01_example.py", "exec"), namespace)  # noqa: S102
    assert callable(namespace["apply"])
    assert 'vet.module("top")' in text
    for name in ("u_mid", "u_spare", "mid", "leaf"):  # the top's instances and their modules
        assert re.search(rf"\b{name}\b", text), name
    assert "u_l0" not in text  # not the whole hierarchy: only what the top instantiates
    live = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert not any("add_port" in line or "connect(" in line for line in live)  # examples are commented out

    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "vet apply --dry-run" in readme and "vet apply --save" in readme and "--resume" in readme
    assert set((root / ".gitignore").read_text(encoding="utf-8").split()) >= {"/out/"}
    assert "add_file" in (root / "cells" / "README.md").read_text(encoding="utf-8")
    capsys.readouterr()


def test_a_new_project_runs_straight_away(kit, tmp_path: Path, monkeypatch, capsys):
    root = (tmp_path / "proj").resolve()
    assert run("init", root, "-f", kit.filelist, "--top", "top") == 0
    monkeypatch.chdir(root)
    capsys.readouterr()
    assert run("apply", "--dry-run") == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines()[0] == f"project: {root / 'hdl-vet.toml'}"
    assert "0 applied, 0 skipped" in captured.err.splitlines()
    assert run("apply") == 0
    assert (root / "out" / "rtl" / "top.sv").read_bytes() == kit.top.read_bytes()
    assert manifest_scripts(root / "out") == ["01_example.py"]
    capsys.readouterr()


def test_init_finds_the_only_top_or_asks(kit, tmp_path: Path, capsys):
    # vetkit has two top-level modules: `top`, and `alt_leaf`, which nothing instantiates.
    assert run("init", tmp_path / "p1", "-f", kit.filelist) == 2
    err = capsys.readouterr().err
    assert "--top" in err and "alt_leaf" in err and "top" in err
    assert not (tmp_path / "p1").exists()

    single = tmp_path / "single"
    single.mkdir()
    (single / "d.sv").write_bytes(
        b"module child (input logic a, output logic y);\n  assign y = a;\nendmodule\n"
        b"module parent (input logic a, output logic y);\n  child u_c (.a(a), .y(y));\nendmodule\n"
    )
    (single / "d.f").write_bytes(b"d.sv\n")
    assert run("init", tmp_path / "p2", "-f", single / "d.f") == 0
    index = tomllib.loads((tmp_path / "p2" / "hdl-vet.toml").read_text(encoding="utf-8"))
    assert index["design"] == {"filelist": "../single/d.f", "top": "parent"}
    assert 'vet.module("parent")' in (tmp_path / "p2" / "scripts" / "01_example.py").read_text(encoding="utf-8")
    capsys.readouterr()


def test_init_writes_paths_relative_to_the_project(kit, tmp_path: Path, capsys):
    deep = (tmp_path / "work" / "area" / "proj").resolve()
    assert run("init", deep, "-f", kit.filelist, "--top", "top", "--base", kit.root) == 0
    index = tomllib.loads((deep / "hdl-vet.toml").read_text(encoding="utf-8"))
    assert index["design"] == {"filelist": "../../../gen/design.f", "top": "top", "base": "../../../gen"}
    project = load_project(deep / "hdl-vet.toml")
    assert project.filelist == kit.filelist and project.base == kit.root and project.top == "top"
    assert project.scripts == (deep / "scripts" / "01_example.py",) and project.output == deep / "out"
    assert project.path == deep / "hdl-vet.toml"
    capsys.readouterr()


def test_init_gives_the_same_bytes_for_the_same_arguments(kit, tmp_path: Path, capsys):
    assert run("init", tmp_path / "one", "-f", kit.filelist, "--top", "top") == 0
    assert run("init", tmp_path / "two", "-f", kit.filelist, "--top", "top") == 0
    assert tree_bytes(tmp_path / "one") == tree_bytes(tmp_path / "two")
    capsys.readouterr()


def test_init_refuses_and_writes_nothing(kit, tmp_path: Path, capsys):
    used = tmp_path / "used"
    used.mkdir()
    (used / "notes.txt").write_text("mine", encoding="utf-8")
    assert run("init", used, "-f", kit.filelist, "--top", "top") == 2
    assert "not empty" in capsys.readouterr().err
    assert sorted(p.name for p in used.iterdir()) == ["notes.txt"]

    assert run("init", kit.root / "proj", "-f", kit.filelist, "--top", "top") == 2
    assert "input tree" in capsys.readouterr().err
    assert not (kit.root / "proj").exists()

    assert run("init", tmp_path / "p", "-f", kit.filelist, "--top", "nope") == 2
    assert "nope" in capsys.readouterr().err
    assert run("init", tmp_path / "p", "-f", tmp_path / "missing.f", "--top", "top") == 2
    assert "missing.f" in capsys.readouterr().err
    assert run("init", tmp_path / "p") == 2  # no file list
    assert "Traceback" not in capsys.readouterr().err
    assert not (tmp_path / "p").exists()


def test_init_accepts_an_empty_directory(kit, tmp_path: Path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run("init", empty, "-f", kit.filelist, "--top", "top") == 0
    assert sorted(tree_bytes(empty)) == sorted(FILES)
    capsys.readouterr()


def test_init_refuses_a_design_with_errors(tmp_path: Path, capsys):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "d.sv").write_bytes(b"module top (input logic a);\n  assign nope = undeclared_thing;\nendmodule\n")
    (bad / "d.f").write_bytes(b"d.sv\n")
    assert run("init", tmp_path / "p", "-f", bad / "d.f", "--top", "top") == 2
    assert "error" in capsys.readouterr().err
    assert not (tmp_path / "p").exists()


def test_init_project_function(kit, tmp_path: Path):
    written = init_project(tmp_path / "p", kit.filelist, top="top")
    assert [p.relative_to(tmp_path / "p").as_posix() for p in written] == FILES
    with pytest.raises(ProjectError, match="not empty"):
        init_project(tmp_path / "p", kit.filelist, top="top")


# --- vet apply with a project -------------------------------------------------------------------


def test_apply_runs_the_project(proj, capsys):
    assert run("apply") == 0
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [f"project: {proj / 'hdl-vet.toml'}"]
    assert "2 applied, 0 skipped" in captured.out.splitlines()
    assert manifest_scripts(proj / "out") == ["10_port.py", "20_wire.py"]
    assert b"dft_en" in (proj / "out" / "rtl" / "top.sv").read_bytes()


def test_command_line_scripts_run_after_the_projects(proj, capsys):
    assert run("apply", "-s", "extra.py", "-o", "with_extra") == 0
    assert manifest_scripts(proj / "with_extra") == ["10_port.py", "20_wire.py", "extra.py"]
    assert not (proj / "out").exists()  # -o replaced the project's output
    capsys.readouterr()


def test_command_line_design_options_replace_the_projects(proj, tmp_path: Path, capsys):
    other = make_design(tmp_path / "other")
    other.top.write_bytes(other.top.read_bytes().replace(b"// sha256: 0000", b"// sha256: 9999"))
    assert run("apply", "-f", other.filelist) == 0
    assert b"sha256: 9999" in (proj / "out" / "rtl" / "top.sv").read_bytes()
    assert run("apply", "--top", "mid", "--force") == 2  # the project's scripts name `top`
    assert "script 10_port.py" in capsys.readouterr().err


def test_the_projects_output_is_not_used_with_save_or_dry_run(proj, capsys):
    assert run("apply", "--dry-run") == 0
    assert run("apply", "--save", "s.zip") == 0
    assert (proj / "s.zip").is_file() and not (proj / "out").exists()
    assert "sign-off so far: ok" in capsys.readouterr().out.splitlines()


def test_resume_uses_the_saved_session_not_the_projects_design_or_scripts(proj, capsys):
    assert run("apply", "-s", "extra.py", "-o", "ref") == 0
    assert run("apply", "--save", "s.zip") == 0
    capsys.readouterr()
    assert run("apply", "--resume", "s.zip", "-s", "extra.py") == 0  # writes the project's output
    captured = capsys.readouterr()
    assert captured.err.splitlines()[0] == f"project: {proj / 'hdl-vet.toml'}"
    assert "1 applied, 0 skipped" in captured.out.splitlines()
    assert tree_bytes(proj / "out") == tree_bytes(proj / "ref")


def test_no_project_ignores_the_file(proj, kit, capsys):
    assert run("apply", "--no-project", "--dry-run") == 2
    err = capsys.readouterr().err
    assert "-f" in err and "project:" not in err
    assert run("apply", "--no-project", "-f", kit.filelist, "--top", "top", "-s", "extra.py", "-o", "plain") == 0
    assert manifest_scripts(proj / "plain") == ["extra.py"]
    assert "project:" not in capsys.readouterr().err


def test_a_project_named_on_the_command_line(proj, tmp_path: Path, monkeypatch, capsys):
    elsewhere = (tmp_path / "elsewhere").resolve()
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert run("apply", "--dry-run") == 2  # no project here, and no -f
    capsys.readouterr()
    assert run("apply", "--project", proj / "hdl-vet.toml") == 0
    assert manifest_scripts(proj / "out") == ["10_port.py", "20_wire.py"]  # paths relative to the file
    assert not (elsewhere / "out").exists()
    assert run("apply", "--project", proj / "hdl-vet.toml", "--no-project") == 2
    assert run("apply", "--project", elsewhere / "hdl-vet.toml") == 2
    assert "hdl-vet.toml" in capsys.readouterr().err


def test_parent_directories_are_not_searched(proj, monkeypatch, capsys):
    monkeypatch.chdir(proj / "scripts")
    assert run("apply", "--dry-run") == 2
    assert "project:" not in capsys.readouterr().err


def test_an_existing_output_directory_still_needs_force(proj, capsys):
    assert run("apply") == 0
    assert run("apply") == 2
    assert "force" in capsys.readouterr().err
    assert run("apply", "--force") == 0
    capsys.readouterr()


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("[design]\nfilelist = 5\n", "filelist"),
        ('[design]\nfilelist = "../gen/design.f"\ntop = ["top"]\n', "top"),
        ('[design]\nfilelist = "../gen/design.f"\ntopp = "top"\n', "topp"),
        ('[design]\nfilelist = "../gen/design.f"\n[apply]\nscripts = "a.py"\n', "scripts"),
        ('[design]\nfilelist = "../gen/design.f"\n[apply]\nscripts = ["a.py", 3]\n', "scripts"),
        ('[design]\nfilelist = "../gen/design.f"\n[apply]\noutput = false\n', "output"),
        ('[design]\nfilelist = "../gen/design.f"\n[aply]\noutput = "out"\n', "aply"),
        ('filelist = "../gen/design.f"\n', "filelist"),
        ('[design]\nfilelist = ""\n', "filelist"),
        ('[design]\nfilelist = "../gen/design.f"\n[design.sub]\nx = 1\n', "sub"),
        ('[[design]]\nfilelist = "../gen/design.f"\n', "design"),
        ('[design]\nfilelist = "../gen/design.f"\n[apply]\nscripts = [""]\n', "scripts"),
        ('[design]\nfilelist = "../gen/design.f"\n[apply]\noutput = ""\n', "output"),
        ("[design\nfilelist = 1\n", "hdl-vet.toml"),
        ('design = "x"\n', "design"),
    ],
)
def test_a_project_file_is_checked_strictly(proj, capsys, text, needle):
    (proj / "hdl-vet.toml").write_text(text, encoding="utf-8")
    assert run("apply", "--dry-run") == 2
    err = capsys.readouterr().err
    assert "hdl-vet.toml" in err and needle in err and "Traceback" not in err
    with pytest.raises(ProjectError, match=needle):
        load_project(proj / "hdl-vet.toml")


def test_a_project_without_a_file_list_needs_one_from_the_command_line(proj, kit, capsys):
    (proj / "hdl-vet.toml").write_text('[apply]\nscripts = ["scripts/10_port.py"]\noutput = "out"\n', encoding="utf-8")
    assert run("apply") == 2
    assert "-f" in capsys.readouterr().err
    assert run("apply", "-f", kit.filelist, "--top", "top") == 0
    assert manifest_scripts(proj / "out") == ["10_port.py"]
    capsys.readouterr()


def test_an_empty_project_file_changes_nothing(proj, kit, capsys):
    (proj / "hdl-vet.toml").write_text("", encoding="utf-8")
    assert run("apply", "-f", kit.filelist, "--top", "top", "-s", "extra.py", "-o", "plain") == 0
    assert manifest_scripts(proj / "plain") == ["extra.py"]
    assert capsys.readouterr().err.splitlines() == [f"project: {proj / 'hdl-vet.toml'}"]


def test_save_checks_apply_to_the_projects_settings(proj, capsys):
    assert run("apply", "--save", "scripts/10_port.py") == 2  # a project script would be overwritten
    assert "--save" in capsys.readouterr().err
    assert (proj / "scripts" / "10_port.py").read_text(encoding="utf-8") == PORT
    assert run("apply", "--save", "out/s.zip", "-o", "out") == 2
    capsys.readouterr()


def test_absolute_paths_in_a_project_file_are_taken_as_they_are(proj, kit, tmp_path: Path, capsys):
    out = (tmp_path / "abs_out").as_posix()
    write_project(proj, scripts=[(proj / "extra.py").as_posix()], output=out, filelist=kit.filelist.as_posix())
    assert run("apply") == 0
    assert manifest_scripts(tmp_path / "abs_out") == ["extra.py"]
    capsys.readouterr()


# --- review of the tests ---------------------------------------------------------------------------


IFACE_LIB = (
    b"interface bus_if;\n  logic v;\nendinterface\n"
    b"module lib_cell (bus_if b);\nendmodule\n"  # nothing instantiates it: a top with an unconnected interface port
    b"module real_top (input logic a, output logic y);\n  assign y = a;\nendmodule\n"
)


def test_init_asks_for_the_top_before_it_looks_for_errors(tmp_path: Path, capsys):
    src = tmp_path / "lib"
    src.mkdir()
    (src / "d.sv").write_bytes(IFACE_LIB)
    (src / "d.f").write_bytes(b"d.sv\n")
    assert run("init", tmp_path / "p", "-f", src / "d.f") == 2
    err = capsys.readouterr().err
    assert "--top" in err and "lib_cell" in err and "real_top" in err
    assert not (tmp_path / "p").exists()
    assert run("init", tmp_path / "p", "-f", src / "d.f", "--top", "real_top") == 0
    assert 'vet.module("real_top")' in (tmp_path / "p" / "scripts" / "01_example.py").read_text(encoding="utf-8")
    capsys.readouterr()


def test_init_writes_toml_that_reads_back_for_awkward_names(tmp_path: Path, capsys):
    odd = tmp_path / "gen \u00e9 \U0001f600"
    odd.mkdir()
    (odd / "d.sv").write_bytes(b"module top (input logic a, output logic y);\n  assign y = a;\nendmodule\n")
    (odd / "d.f").write_bytes(b"d.sv\n")
    root = (tmp_path / "p").resolve()
    assert run("init", root, "-f", odd / "d.f", "--top", "top") == 0
    project = load_project(root / "hdl-vet.toml")
    assert project.filelist == (odd / "d.f").resolve()
    capsys.readouterr()


def test_a_project_file_with_a_byte_order_mark_is_read(proj, capsys):
    data = (proj / "hdl-vet.toml").read_bytes()
    (proj / "hdl-vet.toml").write_bytes(b"\xef\xbb\xbf" + data)
    assert run("apply", "--dry-run") == 0
    (proj / "hdl-vet.toml").write_bytes(b"\xff\xfe" + data)
    assert run("apply", "--dry-run") == 2
    assert "hdl-vet.toml" in capsys.readouterr().err


def test_the_project_line_comes_before_an_error_in_the_file(proj, capsys):
    (proj / "hdl-vet.toml").write_text("[nope]\n", encoding="utf-8")
    assert run("apply", "--dry-run") == 2
    err = capsys.readouterr().err.splitlines()
    assert err[0] == f"project: {proj / 'hdl-vet.toml'}" and "nope" in err[1]


def test_a_file_list_on_the_command_line_drops_the_projects_base(proj, kit, tmp_path: Path, capsys):
    # The project's base points at `gen`; another design's file list must not be resolved against it.
    write_project(proj, scripts=["scripts/10_port.py"], base="../gen")
    assert run("apply", "--dry-run") == 0
    other = make_design(tmp_path / "other")
    other.top.write_bytes(other.top.read_bytes().replace(b"// sha256: 0000", b"// sha256: 9999"))
    capsys.readouterr()
    assert run("apply", "-f", other.filelist) == 0
    assert b"sha256: 9999" in (proj / "out" / "rtl" / "top.sv").read_bytes()
    err = capsys.readouterr().err.splitlines()
    assert err == [f"project: {proj / 'hdl-vet.toml'}", "note: the project's scripts run first: 10_port.py"]


def test_resume_runs_the_project_scripts_the_save_did_not_have(proj, capsys):
    assert run("apply", "-s", "extra.py", "-o", "ref") == 0
    write_project(proj, scripts=["scripts/10_port.py"])
    assert run("apply", "--save", "s.zip") == 0
    write_project(proj, scripts=["scripts/10_port.py", "scripts/20_wire.py"])  # one more than the save
    capsys.readouterr()
    assert run("apply", "--resume", "s.zip", "-s", "extra.py") == 0
    assert "2 applied, 0 skipped" in capsys.readouterr().out.splitlines()
    assert tree_bytes(proj / "out") == tree_bytes(proj / "ref")


def test_resume_refuses_a_project_whose_scripts_differ_from_the_save(proj, capsys):
    assert run("apply", "--save", "s.zip") == 0  # 10_port, 20_wire
    write_project(proj, scripts=["scripts/20_wire.py", "scripts/10_port.py"])
    capsys.readouterr()
    assert run("apply", "--resume", "s.zip") == 2
    err = capsys.readouterr().err
    assert "20_wire.py" in err and "--no-project" in err
    write_project(proj, scripts=["scripts/10_port.py", "scripts/20_wire.py"])
    (proj / "scripts" / "10_port.py").write_text(PORT + "# edited after the save\n", encoding="utf-8")
    assert run("apply", "--resume", "s.zip") == 2
    assert "10_port.py" in capsys.readouterr().err
    assert run("apply", "--resume", "s.zip", "--no-project", "-o", "out") == 0  # the user's business
    capsys.readouterr()


def test_a_saved_session_with_more_scripts_than_the_project_resumes(proj, capsys):
    assert run("apply", "-s", "extra.py", "--save", "s.zip") == 0  # the save has the project's two and extra.py
    capsys.readouterr()
    assert run("apply", "--resume", "s.zip") == 0
    assert "0 applied, 0 skipped" in capsys.readouterr().out.splitlines()
    assert manifest_scripts(proj / "out") == ["10_port.py", "20_wire.py", "extra.py"]


def test_a_script_named_twice_is_refused(proj, kit, capsys):
    assert run("apply", "-s", "scripts/10_port.py", "--dry-run") == 2
    assert "10_port.py" in capsys.readouterr().err
    assert run("apply", "--no-project", "-f", kit.filelist, "--top", "top", "-s", "extra.py", "-s", "extra.py") == 2
    assert "extra.py" in capsys.readouterr().err


def test_save_does_not_replace_the_project_file(proj, capsys):
    before = (proj / "hdl-vet.toml").read_bytes()
    assert run("apply", "--save", "hdl-vet.toml") == 2
    assert "--save" in capsys.readouterr().err
    assert (proj / "hdl-vet.toml").read_bytes() == before


@pytest.mark.parametrize("output", [".", "..", "scripts", "./"])
def test_an_output_that_would_delete_the_project_is_refused(proj, capsys, output):
    write_project(proj, scripts=["scripts/10_port.py"], output=output)
    assert run("apply", "--force") == 2
    assert "output" in capsys.readouterr().err
    assert (proj / "scripts" / "10_port.py").read_text(encoding="utf-8") == PORT
    assert (proj / "hdl-vet.toml").is_file()


def test_an_output_directory_on_the_command_line_must_not_hold_the_scripts_either(proj, kit, capsys):
    assert run("apply", "-o", proj, "--force") == 2
    assert (
        run("apply", "--no-project", "-f", kit.filelist, "--top", "top", "-s", "extra.py", "-o", proj, "--force") == 2
    )
    assert "-o" in capsys.readouterr().err
    assert (proj / "extra.py").is_file() and (proj / "hdl-vet.toml").is_file()


def test_force_with_a_project_output_replaces_only_an_hdl_vet_tree(proj, capsys):
    (proj / "out").mkdir()
    (proj / "out" / "thesis.txt").write_text("mine", encoding="utf-8")
    assert run("apply", "--force") == 2
    assert "hdl-vet.json" in capsys.readouterr().err
    assert (proj / "out" / "thesis.txt").read_text(encoding="utf-8") == "mine"
    (proj / "out" / "thesis.txt").unlink()
    assert run("apply", "--force") == 0  # empty: fine
    assert run("apply", "--force") == 0  # an earlier output: fine
    assert run("apply", "-o", "out", "--force") == 0  # named on the command line: as before
    capsys.readouterr()
