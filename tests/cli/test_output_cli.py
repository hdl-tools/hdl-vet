"""Phase 2: output tree (`hdl_vet.output`), scripts and the `vet` command.

Filelist base
    read_filelist(path, base=None): `base` is the directory that relative
    entries of the top-level file list (and of `-f` nested lists) resolve
    against; default is the directory of the top-level file list. `-F` is
    unaffected. Simulators resolve against the working directory, so file lists
    written that way need `base`. Filelist.root is `base` when given.

Output tree
    write_tree(session, result, outdir, scripts=(), force=False) -> Path
        Requires result.ok, else OutputError. Writes a complete, separate tree:
          - Layout root = session.layout_root (the deepest directory containing
            every source file, every included file, every include directory and
            the top-level file list). Each of those files is written at the same relative path under
            `outdir`; unchanged files are byte-identical copies, changed files
            get result.files content. Nothing else is copied.
          - Include directories exist in the output even when empty.
          - The file list is written at its mirrored path, flattened: first one
            `+incdir+<rel>` line per include directory, then one `+define+...`
            line per define, then one source per line, all in original order,
            paths relative to the output file list's directory with "/"
            separators, LF line endings. Loading it gives the same sources,
            include directories and defines.
          - `hdl-vet.json` at the top of `outdir`: JSON, sorted keys, indent 2,
            trailing newline, no timestamps and no absolute paths:
              {"tool": "hdl-vet", "version": <package version>, "top": <str|null>,
               "filelist": <rel>, "scripts": [{"name", "sha256"}...],
               "changed": {<rel>: {"before": <sha256>, "after": <sha256>}}}
        `outdir` must not exist or must be an empty directory, unless `force`,
        which replaces it. `outdir` inside the layout root, or equal to it, is
        refused. The tree is built in a sibling temporary directory and renamed
        into place, so a failure leaves no partial `outdir`. Returns `outdir`.

Scripts
    A script is a Python file defining `apply(vet)`; `vet` is the Session.
    Scripts run in the order given, in one session, so a later script sees the
    result of earlier ones. Its identity is (file name, sha256 of its bytes).
    A script is compiled and executed from its bytes, not imported: no
    `__pycache__` or any other file is created next to it.

Command line  (`vet`, also `python -m hdl_vet`)
    vet apply -f FILELIST -o OUTDIR [-s SCRIPT]... [--top TOP] [--base DIR]
              [--dry-run] [--force]
    Exit status: 0 success; 1 sign-off failed (report on stderr, nothing
    written); 2 usage or input error, including a script that raises (message on
    stderr, nothing written, no traceback for hdl-vet's own error types).
    On success stdout names the output directory and each changed file.
    `--dry-run` prints the unified diff to stdout and writes nothing; `-o` is
    then optional.
    The same inputs give byte-identical output on every run, regardless of
    PYTHONHASHSEED.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hdl_vet import __version__
from hdl_vet.filelist import read_filelist
from hdl_vet.output import OutputError, write_tree
from hdl_vet.session import HEADER_MARK, Session
from hdl_vet.slang_adapter import load
from repo import REPO
from vetkit import ignores_case, keeps_the_asked_spelling, make_design, span, tree_bytes

GOOD = """\
def apply(vet):
    inst = vet.module("mid").instance("u_l1")
    ov = inst.param_override("DEPTH")
    vet.splice(inst.path, ov.start, ov.end, b".DEPTH(5)", old=b".DEPTH(3)")
    vet.expect("param-changed", "top.u_mid.u_l1", "DEPTH")
"""

SECOND = """\
def apply(vet):
    # sees the result of the first script
    ov = vet.module("mid").instance("u_l1").param_override("DEPTH")
    assert ov.value == "5", ov.value
    vet.splice(vet.module("mid").path, ov.start, ov.end, b".DEPTH(6)", old=b".DEPTH(5)")
"""

BAD = """\
def apply(vet):
    inst = vet.module("mid").instance("u_l0")
    c = inst.connection("d")
    vet.splice(inst.path, c.start, c.end, b".d(a_typo)")
    vet.expect("*")
"""

EMPTY = "def apply(vet):\n    pass\n"
RAISES = "def apply(vet):\n    raise RuntimeError('boom in script')\n"
NO_APPLY = "x = 1\n"


@pytest.fixture
def kit(tmp_path: Path):
    return make_design(tmp_path / "in")


def script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_bytes(body.encode())
    return path


def vet(*args: object, seed: str = "0", cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "hdl_vet", *map(str, args)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=cwd,
        check=False,
    )


# --- read_filelist base -----------------------------------------------------------


def test_filelist_base(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "rtl" / "inc").mkdir(parents=True)
    (repo / "lists").mkdir()
    (repo / "rtl" / "a.sv").write_text("module a; endmodule\n")
    (repo / "rtl" / "b.sv").write_text("module b; endmodule\n")
    (repo / "lists" / "more.f").write_text("rtl/b.sv\n")
    (repo / "lists" / "top.f").write_text("+incdir+rtl/inc\nrtl/a.sv\n-f lists/more.f\n")
    fl = read_filelist(repo / "lists" / "top.f", base=repo)
    assert fl.files == [(repo / "rtl" / "a.sv").resolve(), (repo / "rtl" / "b.sv").resolve()]
    assert fl.incdirs == [(repo / "rtl" / "inc").resolve()]
    assert fl.root == repo.resolve()
    with pytest.raises(Exception, match="a.sv"):
        read_filelist(repo / "lists" / "top.f")


# --- write_tree ---------------------------------------------------------------------


def test_untouched_design_gives_an_identical_tree(kit, tmp_path: Path):
    s = Session(kit.filelist, top="top")
    out = write_tree(s, s.finish(), tmp_path / "out")
    assert out == tmp_path / "out"
    got = tree_bytes(out)
    assert sorted(got) == ["design.f", "hdl-vet.json", "rtl/alt_leaf.sv", "rtl/leaf.sv", "rtl/mid.sv", "rtl/top.sv"]
    for p in kit.sources:
        assert got[p.relative_to(kit.root).as_posix()] == p.read_bytes()
    assert not any(HEADER_MARK in data for data in got.values())
    manifest = json.loads(got["hdl-vet.json"])
    assert manifest == {
        "tool": "hdl-vet",
        "version": __version__,
        "top": "top",
        "filelist": "design.f",
        "scripts": [],
        "changed": {},
        "added": {},
        "files": ["design.f", "rtl/alt_leaf.sv", "rtl/leaf.sv", "rtl/mid.sv", "rtl/top.sv"],
        "equivalence": [],
        "waivers": [],
    }


def test_changed_file_and_manifest(kit, tmp_path: Path):
    s = Session(kit.filelist, top="top")
    start, end = span(s.text(kit.mid), b".DEPTH(3)")
    s.splice(kit.mid, start, end, b".DEPTH(5)")
    s.expect("param-changed")
    scripts = [("p.py", "ab" * 32)]
    r = s.finish(scripts)
    out = write_tree(s, r, tmp_path / "out", scripts=scripts)
    got = tree_bytes(out)
    assert got["rtl/mid.sv"] == r.files[kit.mid]
    assert got["rtl/top.sv"] == kit.top.read_bytes()
    raw = got["hdl-vet.json"]
    manifest = json.loads(raw)
    assert raw == (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    assert manifest["scripts"] == [{"name": "p.py", "sha256": "ab" * 32}]
    assert manifest["changed"] == {
        "rtl/mid.sv": {
            "before": hashlib.sha256(kit.mid.read_bytes()).hexdigest(),
            "after": hashlib.sha256(r.files[kit.mid]).hexdigest(),
        }
    }
    assert str(tmp_path) not in raw.decode() and str(tmp_path).replace("\\", "/") not in raw.decode()


def test_output_tree_loads_and_matches_the_checked_design(kit, tmp_path: Path):
    s = Session(kit.filelist, top="top")
    start, end = span(s.text(kit.mid), b".DEPTH(3)")
    s.splice(kit.mid, start, end, b".DEPTH(5)")
    s.expect("param-changed")
    out = write_tree(s, s.finish(), tmp_path / "out")
    design = load(out / "design.f", top="top")
    assert design.snapshot() == s.design.snapshot()
    assert [d for d in design.diagnostics() if d.severity == "error"] == []


def test_failed_result_is_not_written(kit, tmp_path: Path):
    s = Session(kit.filelist, top="top")
    start, end = span(s.text(kit.mid), b".DEPTH(3)")
    s.splice(kit.mid, start, end, b".DEPTH(5)")
    r = s.finish()
    assert not r.ok
    with pytest.raises(OutputError):
        write_tree(s, r, tmp_path / "out")
    assert not (tmp_path / "out").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["in"]


def test_existing_output_directory(kit, tmp_path: Path):
    out = tmp_path / "out"
    out.mkdir()
    s = Session(kit.filelist, top="top")
    r = s.finish()
    write_tree(s, r, out)  # empty directory is fine
    (out / "stale.txt").write_text("old")
    with pytest.raises(OutputError, match="out"):
        write_tree(s, r, out)
    assert (out / "stale.txt").read_text() == "old"
    write_tree(s, r, out, force=True)
    assert not (out / "stale.txt").exists() and (out / "design.f").is_file()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["in", "out"]


def test_output_inside_the_input_tree_is_refused(kit):
    s = Session(kit.filelist, top="top")
    r = s.finish()
    for bad in (kit.root, kit.root / "out", kit.root / "rtl" / "out"):
        with pytest.raises(OutputError):
            write_tree(s, r, bad, force=True)
    assert kit.top.is_file()


def test_output_in_a_differently_cased_input_tree_is_refused(kit):
    """A case-insensitive filesystem must not let --force delete the input tree."""
    if not ignores_case(kit.root.parent):
        pytest.skip("this filesystem keeps case, so the spelling is a different directory")
    s = Session(kit.filelist, top="top")
    r = s.finish()
    before = tree_bytes(kit.root)
    variant = kit.root.parent / kit.root.name.upper()
    with pytest.raises(OutputError):
        write_tree(s, r, variant, force=True)
    assert tree_bytes(kit.root) == before


def test_a_header_included_in_two_cases_keeps_its_real_name(tmp_path: Path):
    """The output tree is a copy of the input tree, so it must not rename a header.

    Two spellings of one `include used to make two files of the design, and the tree
    was written under whichever spelling came last.
    """
    if not keeps_the_asked_spelling(tmp_path):
        pytest.skip("a wrong spelling cannot survive here, so one header cannot become two")
    root = tmp_path / "in"
    (root / "inc").mkdir(parents=True)
    (root / "rtl").mkdir()
    (root / "inc" / "defs.svh").write_text("`ifndef DEFS\n`define DEFS\n`define W 4\n`endif\n")
    (root / "rtl" / "t.sv").write_text(
        '`include "defs.svh"\n`include "DEFS.svh"\nmodule t(input logic [`W-1:0] a);\nendmodule\n'
    )
    (root / "t.f").write_text("+incdir+inc\nrtl/t.sv\n")
    s = Session(root / "t.f", top="t")
    assert [p.name for p in s.files] == ["t.sv", "defs.svh"]
    out = write_tree(s, s.finish(), tmp_path / "out")
    got = tree_bytes(out)
    assert sorted(got) == ["hdl-vet.json", "inc/defs.svh", "rtl/t.sv", "t.f"]
    assert got["inc/defs.svh"] == (root / "inc" / "defs.svh").read_bytes()


def test_layout_with_includes_defines_and_nested_lists(tmp_path: Path):
    root = tmp_path / "in"
    (root / "lists").mkdir(parents=True)
    (root / "ip" / "inc").mkdir(parents=True)
    (root / "ip" / "unused_inc").mkdir()
    (root / "rtl").mkdir()
    (root / "ip" / "inc" / "w.svh").write_bytes(b"`define W 4\r\n")
    (root / "ip" / "inc" / "never_included.svh").write_text("`define Z 1\n")
    (root / "ip" / "cell.sv").write_text("module cell_m(input logic a);\nendmodule\n")
    (root / "rtl" / "t.sv").write_text(
        '`include "w.svh"\nmodule t(input logic [`W-1:0] a);\n  cell_m u_c (.a(a[0]));\n'
        "`ifdef EXTRA\n  logic e;\n`endif\nendmodule\n"
    )
    (root / "README.md").write_text("not rtl\n")
    (root / "ip" / "ip.f").write_text("cell.sv\n+incdir+inc\n")
    (root / "lists" / "t.f").write_text(
        "+define+EXTRA+MODE=2\n+incdir+../ip/unused_inc\n-F ../ip/ip.f\n../rtl/t.sv\n"
    )  # fmt: skip
    s = Session(root / "lists" / "t.f", top="t")
    out = write_tree(s, s.finish(), tmp_path / "out")
    got = tree_bytes(out)
    assert sorted(got) == ["hdl-vet.json", "ip/cell.sv", "ip/inc/w.svh", "lists/t.f", "rtl/t.sv"]
    assert got["ip/inc/w.svh"] == b"`define W 4\r\n"
    assert (out / "ip" / "unused_inc").is_dir()
    assert got["lists/t.f"].decode() == (
        "+incdir+../ip/unused_inc\n+incdir+../ip/inc\n+define+EXTRA\n+define+MODE=2\n"
        "../ip/cell.sv\n../rtl/t.sv\n"
    )  # fmt: skip
    a, b = read_filelist(root / "lists" / "t.f"), read_filelist(out / "lists" / "t.f")
    assert [p.relative_to(root) for p in a.files] == [p.relative_to(out.resolve()) for p in b.files]
    assert [p.relative_to(root) for p in a.incdirs] == [p.relative_to(out.resolve()) for p in b.incdirs]
    assert a.defines == b.defines
    assert json.loads(got["hdl-vet.json"])["filelist"] == "lists/t.f"


def test_changed_header_is_written(tmp_path: Path):
    root = tmp_path / "in"
    root.mkdir()
    (root / "w.svh").write_text("`define W 4\n")
    (root / "t.sv").write_text('`include "w.svh"\nmodule t(input logic [`W-1:0] a);\nendmodule\n')
    (root / "t.f").write_text("+incdir+.\nt.sv\n")
    s = Session(root / "t.f", top="t")
    s.splice(root / "w.svh", 0, len(s.text(root / "w.svh")), b"`define W 8\n")
    s.expect("port-changed", "t", "a")
    r = s.finish()
    assert r.ok, r.report()
    out = write_tree(s, r, tmp_path / "out")
    assert (out / "w.svh").read_bytes() == b"// Modified by hdl-vet\n`define W 8\n"
    assert (out / "t.sv").read_bytes() == (root / "t.sv").read_bytes()


# --- command line -------------------------------------------------------------------


def test_cli_no_scripts_copies_the_tree(kit, tmp_path: Path):
    out = tmp_path / "out"
    p = vet("apply", "-f", kit.filelist, "-o", out, "--top", "top")
    assert p.returncode == 0, p.stderr
    got = tree_bytes(out)
    for src in kit.sources:
        assert got[src.relative_to(kit.root).as_posix()] == src.read_bytes()
    assert str(out) in p.stdout or out.as_posix() in p.stdout


def test_cli_applies_scripts_in_order(kit, tmp_path: Path):
    out = tmp_path / "out"
    a = script(tmp_path, "a.py", GOOD)
    b = script(tmp_path, "b.py", SECOND)
    p = vet("apply", "-f", kit.filelist, "-s", a, "-s", b, "-o", out, "--top", "top")
    assert p.returncode == 0, p.stderr
    mid = (out / "rtl" / "mid.sv").read_bytes()
    assert b".DEPTH(6)" in mid
    sha_a = hashlib.sha256(a.read_bytes()).hexdigest()
    sha_b = hashlib.sha256(b.read_bytes()).hexdigest()
    header = f"// Modified by hdl-vet; scripts: a.py sha256:{sha_a[:12]}, b.py sha256:{sha_b[:12]}\n"
    assert mid.startswith(header.encode())
    manifest = json.loads((out / "hdl-vet.json").read_text(encoding="utf-8"))
    assert manifest["scripts"] == [{"name": "a.py", "sha256": sha_a}, {"name": "b.py", "sha256": sha_b}]
    assert list(manifest["changed"]) == ["rtl/mid.sv"]
    assert "rtl/mid.sv" in p.stdout.replace("\\", "/")


def test_cli_is_deterministic_across_hash_seeds(kit, tmp_path: Path):
    a = script(tmp_path, "a.py", GOOD)
    trees = []
    for seed in ("0", "1", "12345"):
        out = tmp_path / f"out{seed}"
        p = vet("apply", "-f", kit.filelist, "-s", a, "-o", out, "--top", "top", seed=seed)
        assert p.returncode == 0, p.stderr
        trees.append(tree_bytes(out))
    assert trees[0] == trees[1] == trees[2]


def test_cli_signoff_failure_writes_nothing(kit, tmp_path: Path):
    out = tmp_path / "out"
    p = vet("apply", "-f", kit.filelist, "-s", script(tmp_path, "bad.py", BAD), "-o", out, "--top", "top")
    assert p.returncode == 1
    assert "ImplicitNet" in p.stderr and "mid.sv" in p.stderr
    assert not out.exists()
    assert sorted(x.name for x in tmp_path.iterdir()) == ["bad.py", "in"]


def test_cli_dry_run(kit, tmp_path: Path):
    out = tmp_path / "out"
    a = script(tmp_path, "a.py", GOOD)
    p = vet("apply", "-f", kit.filelist, "-s", a, "--top", "top", "--dry-run")
    assert p.returncode == 0, p.stderr
    assert "--- a/rtl/mid.sv" in p.stdout and "+++ b/rtl/mid.sv" in p.stdout
    assert any(line.startswith("+") and ".DEPTH(5)" in line for line in p.stdout.splitlines())
    p = vet("apply", "-f", kit.filelist, "-s", a, "-o", out, "--top", "top", "--dry-run")
    assert p.returncode == 0 and not out.exists()
    bad = vet("apply", "-f", kit.filelist, "-s", script(tmp_path, "bad.py", BAD), "--top", "top", "--dry-run")
    assert bad.returncode == 1


@pytest.mark.parametrize(
    ("body", "needle"),
    [(RAISES, "boom in script"), (NO_APPLY, "apply"), ("def apply(vet:\n", "s.py")],
)
def test_cli_script_problems_are_input_errors(kit, tmp_path: Path, body, needle):
    out = tmp_path / "out"
    p = vet("apply", "-f", kit.filelist, "-s", script(tmp_path, "s.py", body), "-o", out, "--top", "top")
    assert p.returncode == 2
    assert needle in p.stderr
    assert not out.exists()


def test_cli_input_errors(kit, tmp_path: Path):
    out = tmp_path / "out"
    cases = [
        (["apply", "-f", tmp_path / "missing.f", "-o", out], "missing.f"),
        (["apply", "-f", kit.filelist, "-o", out, "--top", "no_such_top"], "no_such_top"),
        (["apply", "-f", kit.filelist, "-s", tmp_path / "missing.py", "-o", out], "missing.py"),
        (["apply", "-f", kit.filelist, "--top", "top"], "-o"),
    ]
    for args, needle in cases:
        p = vet(*args)
        assert p.returncode == 2, (args, p.stdout, p.stderr)
        assert needle in p.stderr
        assert "Traceback" not in p.stderr
        assert not out.exists()


def test_cli_force_and_existing_output(kit, tmp_path: Path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "stale.txt").write_text("old")
    p = vet("apply", "-f", kit.filelist, "-o", out, "--top", "top")
    assert p.returncode == 2 and (out / "stale.txt").is_file()
    p = vet("apply", "-f", kit.filelist, "-o", out, "--top", "top", "--force")
    assert p.returncode == 0 and not (out / "stale.txt").exists()


def test_cli_base_for_cwd_relative_filelists(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "rtl").mkdir(parents=True)
    (repo / "lists").mkdir()
    (repo / "rtl" / "a.sv").write_text("module a(input logic x);\nendmodule\n")
    (repo / "lists" / "a.f").write_text("rtl/a.sv\n")
    out = tmp_path / "out"
    p = vet("apply", "-f", repo / "lists" / "a.f", "--base", repo, "-o", out, "--top", "a")
    assert p.returncode == 0, p.stderr
    assert sorted(tree_bytes(out)) == ["hdl-vet.json", "lists/a.f", "rtl/a.sv"]
    assert (out / "lists" / "a.f").read_text() == "../rtl/a.sv\n"
    assert load(out / "lists" / "a.f", top="a").modules() == ["a"]


def test_vet_entry_point_is_declared():
    import tomllib

    root = REPO
    scripts = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts["vet"] == "hdl_vet.cli:main"
