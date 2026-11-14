"""D1 acceptance: the .f reader.

Interface under test (hdl_vet.filelist):

    read_filelist(path: Path) -> Filelist
    Filelist.files    : list[Path]   absolute, in filelist order, duplicates kept once
    Filelist.incdirs  : list[Path]   absolute, in order
    Filelist.defines  : dict[str, str | None]   +define+A=1+B -> {"A": "1", "B": None}
    Filelist.root     : Path         directory of the top-level .f
    FilelistError                    raised with the offending file and line in the message

Relative paths resolve against the directory of the .f that names them for `-F`,
and against the top-level .f directory for `-f` (the usual simulator convention).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.filelist import FilelistError, read_filelist

FIXTURES = Path(__file__).parent / "fixtures"


def test_small_filelist_order_and_resolution():
    fl = read_filelist(FIXTURES / "small" / "small.f")
    root = (FIXTURES / "small").resolve()
    assert fl.root == root
    assert fl.incdirs == [root / "include"]
    assert fl.files == [
        root / "rtl" / "small_pkg.sv",
        root / "rtl" / "leaf.sv",
        root / "rtl" / "mid.sv",
        root / "rtl" / "top.sv",
    ]
    assert fl.defines == {}


def test_comments_blank_lines_and_trailing_comment(tmp_path: Path):
    (tmp_path / "a.sv").write_text("module a; endmodule\n")
    (tmp_path / "x.f").write_text(
        "// slash comment\n# hash comment\n\n   \na.sv   // trailing comment\n"
    )
    assert read_filelist(tmp_path / "x.f").files == [(tmp_path / "a.sv").resolve()]


def test_defines(tmp_path: Path):
    (tmp_path / "a.sv").write_text("module a; endmodule\n")
    (tmp_path / "x.f").write_text("+define+A=1+B\n+define+C=hello\na.sv\n")
    assert read_filelist(tmp_path / "x.f").defines == {"A": "1", "B": None, "C": "hello"}


def test_multiple_incdirs_on_one_line(tmp_path: Path):
    (tmp_path / "i1").mkdir()
    (tmp_path / "i2").mkdir()
    (tmp_path / "a.sv").write_text("module a; endmodule\n")
    (tmp_path / "x.f").write_text("+incdir+i1+i2\na.sv\n")
    fl = read_filelist(tmp_path / "x.f")
    assert fl.incdirs == [(tmp_path / "i1").resolve(), (tmp_path / "i2").resolve()]


def test_nested_f_resolves_against_top_level_dir(tmp_path: Path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (tmp_path / "a.sv").write_text("module a; endmodule\n")
    (tmp_path / "b.sv").write_text("module b; endmodule\n")
    # `-f`: paths inside sub/inner.f are relative to the TOP-LEVEL .f directory.
    (sub / "inner.f").write_text("b.sv\n")
    (tmp_path / "x.f").write_text("a.sv\n-f sub/inner.f\n")
    fl = read_filelist(tmp_path / "x.f")
    assert fl.files == [(tmp_path / "a.sv").resolve(), (tmp_path / "b.sv").resolve()]


def test_nested_capital_f_resolves_against_its_own_dir(tmp_path: Path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (tmp_path / "a.sv").write_text("module a; endmodule\n")
    (sub / "b.sv").write_text("module b; endmodule\n")
    # `-F`: paths inside sub/inner.f are relative to sub/.
    (sub / "inner.f").write_text("b.sv\n")
    (tmp_path / "x.f").write_text("a.sv\n-F sub/inner.f\n")
    fl = read_filelist(tmp_path / "x.f")
    assert fl.files == [(tmp_path / "a.sv").resolve(), (sub / "b.sv").resolve()]


def test_env_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "ip").mkdir()
    (tmp_path / "ip" / "a.sv").write_text("module a; endmodule\n")
    (tmp_path / "ip" / "b.sv").write_text("module b; endmodule\n")
    monkeypatch.setenv("VET_TEST_IP", str(tmp_path / "ip"))
    (tmp_path / "x.f").write_text("$VET_TEST_IP/a.sv\n${VET_TEST_IP}/b.sv\n")
    fl = read_filelist(tmp_path / "x.f")
    assert fl.files == [
        (tmp_path / "ip" / "a.sv").resolve(),
        (tmp_path / "ip" / "b.sv").resolve(),
    ]


def test_unset_env_var_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("VET_TEST_UNSET", raising=False)
    # A directory literally named "$VET_TEST_UNSET" exists, so an implementation that
    # leaves unset variables unexpanded (os.path.expandvars does) finds a.sv and
    # does not raise; only an explicit "variable is unset" check passes.
    (tmp_path / "$VET_TEST_UNSET").mkdir()
    (tmp_path / "$VET_TEST_UNSET" / "a.sv").write_text("module a; endmodule\n")
    (tmp_path / "x.f").write_text("$VET_TEST_UNSET/a.sv\n")
    with pytest.raises(FilelistError, match="VET_TEST_UNSET"):
        read_filelist(tmp_path / "x.f")


def test_duplicate_file_listed_once(tmp_path: Path):
    (tmp_path / "a.sv").write_text("module a; endmodule\n")
    (tmp_path / "x.f").write_text("a.sv\n./a.sv\n")
    assert read_filelist(tmp_path / "x.f").files == [(tmp_path / "a.sv").resolve()]


def test_missing_source_file_names_file_and_line(tmp_path: Path):
    (tmp_path / "x.f").write_text("// header\nnope.sv\n")
    with pytest.raises(FilelistError) as exc:
        read_filelist(tmp_path / "x.f")
    msg = str(exc.value)
    # A bare `"2" in msg` passes on any tmp path such as ".../pytest-12/...".
    assert "nope.sv" in msg and "x.f" in msg
    assert "x.f:2" in msg or "line 2" in msg, msg


def test_recursive_include_is_an_error(tmp_path: Path):
    (tmp_path / "x.f").write_text("-f x.f\n")
    with pytest.raises(FilelistError):
        read_filelist(tmp_path / "x.f")


def test_unknown_option_is_an_error_not_silently_dropped(tmp_path: Path):
    (tmp_path / "a.sv").write_text("module a; endmodule\n")
    (tmp_path / "x.f").write_text("--frobnicate\na.sv\n")
    with pytest.raises(FilelistError, match="frobnicate"):
        read_filelist(tmp_path / "x.f")


def test_crlf_filelist(tmp_path: Path):
    (tmp_path / "a.sv").write_text("module a; endmodule\n")
    (tmp_path / "x.f").write_bytes(b"a.sv\r\n")
    assert read_filelist(tmp_path / "x.f").files == [(tmp_path / "a.sv").resolve()]
