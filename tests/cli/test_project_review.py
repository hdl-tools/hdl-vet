"""Phase 5c-1 review fixes (spec: tests/cli/test_project.py; this file refines it).

From the adversarial review of the first implementation, and from reading what
`vet init` wrote for a real design. Where a rule below differs from the spec in
tests/cli/test_project.py, this file wins.

Not deleting what is not an hdl-vet output
    F1  The manifest `hdl-vet.json` gains "files": the POSIX relative path of
        every file of the output tree except the manifest itself, sorted.
    F2  When the output directory comes from the project file, `--force`
        replaces it only if it is empty, or if it is an hdl-vet output that
        holds nothing else: `hdl-vet.json` is a manifest with "tool":
        "hdl-vet" and a "files" list, and every file in the directory is the
        manifest or is in that list. Otherwise it is refused, naming one file
        that is not accounted for (or saying the manifest cannot be read). A
        file merely named `hdl-vet.json` unlocks nothing, and a note the user
        dropped into an old output is not deleted.

Never a traceback
    C1  TOML nested too deeply to parse, and a NUL character in any string of
        the project file, are errors naming the file (and the key).
    C2  A directory that cannot be listed (the output directory, or DIR of
        `vet init`) is a clean error.
    C3  `hdl-vet.toml`, or the file named by `--project`, that is a directory
        is an error saying so; it is not ignored.
    C4  `base` without `filelist` in a project file is refused: a base belongs
        to a file list.

Order of checks, as before this phase
    O1  Usage errors first, then each script is read (a script that cannot be
        read or has a syntax error is reported), then the saved session.
        A script named twice is reported after the usage errors.

`vet init`
    I1  The example script shows one set of example operations, on the first
        instance that can be edited, not one set per instance.
    I2  Instances of interfaces are listed with a note and get no example
        operation. An instance name that appears in several generate branches
        is listed once.
    I3  Several tops and no `--top`: the list starts with the top that has
        the most instances below it and shows at most ten, then
        "and N more".
    I4  A path is written relative to the project only while that takes at
        most three ".." steps; beyond that it is written absolute.
    I5  `.gitignore` holds `/out/` and `*.session.zip`; the README's examples
        save to a `*.session.zip` file. Other zip files are not ignored.
    I6  The project appears complete or not at all: the files are written to
        a temporary directory next to DIR, which is then renamed (or, for an
        existing empty DIR, moved into it).
    I7  The README says that `vet apply`, also with `--dry-run`, executes the
        project's scripts: run it only in a project you trust.
    I8  `cells/README.md` and the example script show how to name a cell
        relative to the script, not to the current directory:
        `Path(__file__).parent.parent / "cells" / "NAME.sv"`.
    I9  When DIR is refused for being inside the input tree and the file list
        does not lie in or under the directory that holds all the sources,
        the message suggests `--base` (the tree is that wide only because of
        where the file list is).
    I10 "created <DIR>" prints DIR exactly as it was given.

Messages
    M1  On resume, a project script with the saved name but other content:
        the message says its content differs from the one the session
        applied.
    M2  Where the output directory comes from the project, messages call it
        the project's output directory, not "the -o directory".

Also
    `import hdl_vet.project` does not load pyslang: reading a project file
    needs no design.
    With a project, paths in messages and in "wrote ..." are absolute, as they
    would be for the same command line written with absolute paths.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import hdl_vet.project
from hdl_vet.cli import main
from hdl_vet.project import ProjectError, init_project, load_project
from no_comparison import no_comparison  # noqa: F401
from project_files import FILES, PORT, manifest_scripts, write_project
from vetkit import make_design, tree_bytes


def run(*args: object) -> int:
    return main([str(a) for a in args])


@pytest.fixture
def kit(tmp_path: Path):
    return make_design(tmp_path / "gen")


@pytest.fixture
def proj(kit, tmp_path: Path, monkeypatch, capsys):
    root = (tmp_path / "proj").resolve()
    assert run("init", root, "-f", kit.filelist, "--top", "top") == 0
    capsys.readouterr()
    (root / "scripts" / "10_port.py").write_text(PORT, encoding="utf-8")
    write_project(root, scripts=["scripts/10_port.py"])
    monkeypatch.chdir(root)
    return root


def design(tmp_path: Path, name: str, text: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "d.sv").write_bytes(text.encode())
    (root / "d.f").write_bytes(b"d.sv\n")
    return root / "d.f"


# --- F1, F2 ----------------------------------------------------------------------------------


def test_the_manifest_lists_every_file_of_the_tree(proj, capsys):
    assert run("apply") == 0
    manifest = json.loads((proj / "out" / "hdl-vet.json").read_bytes())
    assert manifest["files"] == sorted(name for name in tree_bytes(proj / "out") if name != "hdl-vet.json")
    assert "design.f" in manifest["files"] and "rtl/top.sv" in manifest["files"]
    capsys.readouterr()


def test_force_does_not_delete_a_directory_that_only_has_a_file_of_that_name(proj, tmp_path: Path, capsys):
    docs = tmp_path / "mydocs"
    docs.mkdir()
    (docs / "thesis.txt").write_text("mine", encoding="utf-8")
    write_project(proj, scripts=["scripts/10_port.py"], output="../mydocs")
    for content in ("{}", "not json", '{"tool": "hdl-vet"}', '{"tool": "other", "files": ["thesis.txt"]}', "[]"):
        (docs / "hdl-vet.json").write_text(content, encoding="utf-8")
        assert run("apply", "--force") == 2, content
        assert "hdl-vet.json" in capsys.readouterr().err
        assert (docs / "thesis.txt").read_text(encoding="utf-8") == "mine"


def test_force_does_not_delete_what_the_user_added_to_an_old_output(proj, capsys):
    assert run("apply") == 0
    (proj / "out" / "my_notes.txt").write_text("keep me", encoding="utf-8")
    capsys.readouterr()
    assert run("apply", "--force") == 2
    assert "my_notes.txt" in capsys.readouterr().err
    assert (proj / "out" / "my_notes.txt").read_text(encoding="utf-8") == "keep me"
    (proj / "out" / "my_notes.txt").unlink()
    assert run("apply", "--force") == 0
    capsys.readouterr()


# --- C1 .. C4 --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "[design]\nfilelist = " + "[" * 5000 + "]" * 5000 + "\n",
        '[design]\nfilelist = "a\\u0000b"\n',
        '[design]\nfilelist = "../gen/design.f"\nbase = "x\\u0000y"\n',
        '[design]\nfilelist = "../gen/design.f"\n[apply]\nscripts = ["s\\u0000.py"]\n',
        '[design]\nfilelist = "../gen/design.f"\n[apply]\noutput = "o\\u0000ut"\n',
        '[design]\nbase = "../gen"\n',
    ],
    ids=["deep", "nul-filelist", "nul-base", "nul-script", "nul-output", "base-without-filelist"],
)
def test_awkward_project_files_are_clean_errors(proj, capsys, text):
    (proj / "hdl-vet.toml").write_text(text, encoding="utf-8")
    assert run("apply", "--dry-run") == 2
    err = capsys.readouterr().err
    assert "hdl-vet.toml" in err and "Traceback" not in err
    with pytest.raises(ProjectError, match="hdl-vet.toml"):
        load_project(proj / "hdl-vet.toml")


def test_a_directory_that_cannot_be_listed_is_a_clean_error(proj, kit, tmp_path: Path, monkeypatch, capsys):
    locked = (tmp_path / "locked").resolve()
    locked.mkdir()
    real = Path.iterdir

    def iterdir(self):
        if self.resolve() == locked:
            raise PermissionError(13, "Access is denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    write_project(proj, scripts=["scripts/10_port.py"], output="../locked")
    assert run("apply", "--force") == 2
    assert "locked" in capsys.readouterr().err
    assert run("init", locked, "-f", kit.filelist, "--top", "top") == 2
    err = capsys.readouterr().err
    assert "locked" in err and "Traceback" not in err


def test_a_project_file_that_is_a_directory_is_an_error(proj, kit, tmp_path: Path, monkeypatch, capsys):
    assert run("apply", "--project", proj / "scripts", "--dry-run") == 2
    assert "directory" in capsys.readouterr().err
    odd = tmp_path / "odd"
    (odd / "hdl-vet.toml").mkdir(parents=True)
    monkeypatch.chdir(odd)
    assert run("apply", "-f", kit.filelist, "--top", "top", "--dry-run") == 2  # not silently ignored
    assert "directory" in capsys.readouterr().err
    assert run("apply", "--no-project", "-f", kit.filelist, "--top", "top", "--dry-run") == 0
    capsys.readouterr()


# --- O1 --------------------------------------------------------------------------------------


def test_the_order_of_checks_is_the_old_one(kit, tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bad.py").write_text("def apply(vet:\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text(PORT, encoding="utf-8")
    assert run("apply", "--resume", "missing.zip", "-s", "bad.py", "-o", "out") == 2
    assert "bad.py" in capsys.readouterr().err  # the script is read before the archive
    assert run("apply", "--resume", "missing.zip", "-s", "nope.py", "-o", "out") == 2
    assert "nope.py" in capsys.readouterr().err
    assert run("apply", "-s", "ok.py", "-s", "ok.py") == 2
    assert "-f" in capsys.readouterr().err  # the usage error comes before "named twice"
    assert run("apply", "-f", kit.filelist, "--top", "top", "-s", "ok.py", "-s", "ok.py", "--dry-run") == 2
    assert "twice" in capsys.readouterr().err


# --- I1 .. I10 -------------------------------------------------------------------------------


def test_examples_are_shown_once(kit, tmp_path: Path, capsys):
    assert run("init", tmp_path / "p", "-f", kit.filelist, "--top", "top") == 0
    text = (tmp_path / "p" / "scripts" / "01_example.py").read_text(encoding="utf-8")
    assert text.count("remove_instance(") == 1 and text.count(".connect(") == 1
    assert text.count("replace_module(") == 1 and text.count(".set_param(") == 1
    assert 'top.instance("u_mid").connect("clk"' in text  # the first instance, a real port
    assert 'Path(__file__).parent.parent / "cells"' in text
    cells = (tmp_path / "p" / "cells" / "README.md").read_text(encoding="utf-8")
    assert 'Path(__file__).parent.parent / "cells"' in cells
    capsys.readouterr()


ODD = (
    "interface bus_if;\n  logic v;\nendinterface\n"
    "module leaf (input logic a, output logic y);\n  assign y = a;\nendmodule\n"
    "module top #(parameter bit ALT = 0) (input logic a, output logic y, output logic z);\n"
    "  bus_if u_bus ();\n"
    "  if (ALT) begin : g\n    leaf u_g (.a(a), .y(y));\n  end else begin : g\n    leaf u_g (.a(~a), .y(y));\n  end\n"
    "  leaf u_plain (.a(a), .y(z));\n"
    "endmodule\n"
)


def test_interface_and_generate_instances_are_listed_but_get_no_example(tmp_path: Path, capsys):
    assert run("init", tmp_path / "p", "-f", design(tmp_path, "odd", ODD), "--top", "top") == 0
    text = (tmp_path / "p" / "scripts" / "01_example.py").read_text(encoding="utf-8")
    listing = [line for line in text.splitlines() if '"u_bus"' in line or '"u_g"' in line or '"u_plain"' in line]
    assert sum('"u_g"' in line for line in listing) == 1  # both branches use the name: listed once
    assert any('"u_bus"' in line and "interface" in line for line in listing)
    assert any('"u_g"' in line and "generate" in line for line in listing)
    assert 'top.instance("u_plain").connect(' in text  # the first one that can be edited
    assert 'instance("u_bus")' not in text and 'instance("u_g")' not in text
    capsys.readouterr()


def test_the_list_of_tops_starts_with_the_largest_and_is_short(tmp_path: Path, capsys):
    libs = "".join(f"module lib_{i:02d} (input logic a);\nendmodule\n" for i in range(14))
    text = libs + (
        "module leaf (input logic a, output logic y);\n  assign y = a;\nendmodule\n"
        "module zz_real_top (input logic a, output logic y);\n  logic m;\n"
        "  leaf u0 (.a(a), .y(m));\n  leaf u1 (.a(m), .y(y));\nendmodule\n"
    )
    assert run("init", tmp_path / "p", "-f", design(tmp_path, "many", text)) == 2
    err = capsys.readouterr().err
    assert err.index("zz_real_top") < err.index("lib_00")  # the one with a hierarchy comes first
    assert "and 5 more" in err and "lib_13" not in err  # ten shown, of fifteen
    assert "--top" in err


def test_a_far_file_list_is_written_absolute(kit, tmp_path: Path, capsys):
    near = (tmp_path / "a" / "b" / "proj").resolve()  # three steps up to tmp_path
    far = (tmp_path / "a" / "b" / "c" / "proj").resolve()  # four
    assert run("init", near, "-f", kit.filelist, "--top", "top") == 0
    assert run("init", far, "-f", kit.filelist, "--top", "top") == 0
    assert load_project(near / "hdl-vet.toml").filelist == kit.filelist
    assert load_project(far / "hdl-vet.toml").filelist == kit.filelist
    assert 'filelist = "../../../gen/design.f"' in (near / "hdl-vet.toml").read_text(encoding="utf-8")
    assert f'filelist = "{kit.filelist.as_posix()}"' in (far / "hdl-vet.toml").read_text(encoding="utf-8")
    capsys.readouterr()


def test_the_ignore_file_and_the_readme(proj):
    assert (proj / ".gitignore").read_text(encoding="utf-8").split() == ["/out/", "*.session.zip"]
    readme = (proj / "README.md").read_text(encoding="utf-8")
    assert ".session.zip" in readme and "--save session.zip" not in readme
    assert "executes" in readme and "--dry-run" in readme and "trust" in readme


def test_init_leaves_no_partial_project(kit, tmp_path: Path, monkeypatch, capsys):
    real = Path.write_bytes
    seen: list[str] = []

    def write_bytes(self, data):
        seen.append(self.name)
        if self.name == "README.md" and self.parent.name != "cells":
            raise OSError(28, "No space left on device", str(self))
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", write_bytes)
    with pytest.raises(OSError, match="No space"):
        init_project(tmp_path / "p", kit.filelist, top="top")
    assert "hdl-vet.toml" in seen  # some files had been written
    assert not (tmp_path / "p").exists()
    assert [p.name for p in tmp_path.iterdir()] == ["gen"]  # and no temporary directory is left
    assert run("init", tmp_path / "p", "-f", kit.filelist, "--top", "top") == 2
    assert "Traceback" not in capsys.readouterr().err
    monkeypatch.undo()
    assert run("init", tmp_path / "p", "-f", kit.filelist, "--top", "top") == 0
    assert sorted(tree_bytes(tmp_path / "p")) == sorted(FILES)
    capsys.readouterr()


def test_a_wide_input_tree_suggests_base(kit, tmp_path: Path, capsys):
    lists = tmp_path / "lists"
    lists.mkdir()
    entries = "".join(f"{p.as_posix()}\n" for p in kit.sources)
    (lists / "abs.f").write_bytes(entries.encode())  # the file list is not with the sources
    assert run("init", tmp_path / "p", "-f", lists / "abs.f", "--top", "top") == 2
    err = capsys.readouterr().err
    assert "--base" in err and "input tree" in err
    assert run("init", tmp_path / "p", "-f", lists / "abs.f", "--top", "top", "--base", kit.root / "rtl") == 0
    capsys.readouterr()
    # an ordinary refusal does not talk about --base
    assert run("init", kit.root / "p", "-f", kit.filelist, "--top", "top") == 2
    assert "--base" not in capsys.readouterr().err


def test_created_prints_the_directory_as_given(kit, tmp_path: Path, monkeypatch, capsys):
    (tmp_path / "work").mkdir()
    monkeypatch.chdir(tmp_path / "work")
    given = "../work/./p_dotdot/"
    assert run("init", given, "-f", kit.filelist, "--top", "top") == 0
    assert capsys.readouterr().out.splitlines()[0] == f"created {given}"
    assert (tmp_path / "work" / "p_dotdot" / "hdl-vet.toml").is_file()


# --- M1, M2, and the rest --------------------------------------------------------------------


def test_resume_says_that_the_content_differs(proj, capsys):
    assert run("apply", "--save", "s.session.zip") == 0
    (proj / "scripts" / "10_port.py").write_text(PORT + "# edited\n", encoding="utf-8")
    capsys.readouterr()
    assert run("apply", "--resume", "s.session.zip") == 2
    err = capsys.readouterr().err
    assert "10_port.py" in err and "content" in err and "--no-project" in err


def test_messages_name_the_projects_output_directory(proj, capsys):
    (proj / "out").mkdir()
    assert run("apply", "--save", "out/s.session.zip") == 0  # --save alone writes no tree
    capsys.readouterr()
    assert run("apply", "--resume", "out/s.session.zip") == 2  # now the project's output would replace it
    err = capsys.readouterr().err
    assert "project" in err.splitlines()[-1] and "-o directory" not in err


def test_importing_the_project_module_does_not_load_pyslang():
    code = "import sys, hdl_vet.project; raise SystemExit(1 if 'pyslang' in sys.modules else 0)"
    done = subprocess.run([sys.executable, "-c", code], check=False, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert hdl_vet.project.PROJECT_FILE == "hdl-vet.toml"


def test_paths_are_absolute_with_a_project(proj, capsys):
    assert run("apply") == 0
    assert f"wrote {proj / 'out'}" in capsys.readouterr().out.splitlines()
    assert manifest_scripts(proj / "out") == ["10_port.py"]
    assert os.path.isabs(str(load_project(proj / "hdl-vet.toml").output))
