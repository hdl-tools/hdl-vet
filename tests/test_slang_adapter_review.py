"""Phase 1 review fixes: cases the reviewer broke with probes.

Additions to the hdl_vet.slang_adapter interface:

    load(filelist, top=None, overrides: Mapping[Path, bytes] | None = None)
        `overrides` replaces the on-disk bytes of the named source or include
        files for this load only. Nothing is written. This is how a modified
        design is re-elaborated before any output tree exists.

    InstanceSite ranges, restated: [start, end) always lies in ONE file, `path`,
    with start < end. If any token of the instantiation statement comes from a
    macro expansion, from_macro is True and the range runs from the first byte of
    the statement as written in `path` (macro use included) through its last byte
    as written in `path`.

    A statement declaring several instances yields one site per instance, in
    source order, all sharing the statement's range.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet import slang_adapter
from hdl_vet.slang_adapter import load

LEAF = "module leaf(input logic a, output logic y);\n  assign y = a;\nendmodule\n"


def _design(tmp_path: Path, top_src: str, extra: str = ""):
    (tmp_path / "leaf.sv").write_text(LEAF)
    (tmp_path / "t.sv").write_text(top_src)
    (tmp_path / "t.f").write_text(extra + "leaf.sv\nt.sv\n")
    return load(tmp_path / "t.f", top="t")


def test_several_instances_in_one_statement(tmp_path: Path):
    src = (
        "module t(input logic a, output logic y1, output logic y2);\n"
        "  leaf u1 (.a(a), .y(y1)), u2 (.a(a), .y(y2));\n"
        "endmodule\n"
    )
    design = _design(tmp_path, src)
    sites = design.instance_sites("t")
    assert [(s.instance_name, s.module_name) for s in sites] == [("u1", "leaf"), ("u2", "leaf")]
    data = (tmp_path / "t.sv").read_bytes()
    stmt = b"leaf u1 (.a(a), .y(y1)), u2 (.a(a), .y(y2));"
    assert [data[s.start : s.end] for s in sites] == [stmt, stmt]
    assert not any(s.from_macro for s in sites)


def test_instantiation_ending_in_a_macro(tmp_path: Path):
    src = (
        "`define TAIL .y(y1));\n"
        "module t(input logic a, output logic y1);\n"
        "  leaf u2 (.a(a), `TAIL\n"
        "endmodule\n"
    )
    design = _design(tmp_path, src)
    assert [d for d in design.diagnostics() if d.severity == "error"] == []
    (site,) = design.instance_sites("t")
    assert site.from_macro is True
    assert site.path == (tmp_path / "t.sv").resolve()
    assert site.start < site.end
    data = (tmp_path / "t.sv").read_bytes()
    assert data[site.start : site.end] == b"leaf u2 (.a(a), `TAIL"


def test_instantiation_starting_with_a_macro(tmp_path: Path):
    src = (
        "`define TY leaf\n"
        "module t(input logic a, output logic y1);\n"
        "  `TY u_x (.a(a), .y(y1));\n"
        "endmodule\n"
    )
    design = _design(tmp_path, src)
    (site,) = design.instance_sites("t")
    assert site.from_macro is True
    assert site.module_name == "leaf"
    data = (tmp_path / "t.sv").read_bytes()
    assert data[site.start : site.end] == b"`TY u_x (.a(a), .y(y1));"


def test_macro_only_inside_a_connection_still_flags_the_site(tmp_path: Path):
    src = (
        "`define SIG a\n"
        "module t(input logic a, output logic y1);\n"
        "  leaf u_m (.a(`SIG), .y(y1));\n"
        "endmodule\n"
    )
    design = _design(tmp_path, src)
    (site,) = design.instance_sites("t")
    assert site.from_macro is True
    data = (tmp_path / "t.sv").read_bytes()
    assert data[site.start : site.end] == b"leaf u_m (.a(`SIG), .y(y1));"


def test_non_ascii_and_spaced_project_path(tmp_path: Path):
    root = tmp_path / "caf\u00e9 \u4e2d dir"
    root.mkdir()
    src = (
        "module t(input logic a, output logic y1);\n"
        "  leaf u_l (.a(a), .y(y1));\n"
        "  assign y1 = nope;\n"
        "endmodule\n"
    )
    design = _design(root, src)
    assert design.files == [(root / "leaf.sv").resolve(), (root / "t.sv").resolve()]
    assert design.roundtrip(root / "t.sv") == (root / "t.sv").read_bytes()
    (site,) = design.instance_sites("t")
    assert site.path == (root / "t.sv").resolve()
    errors = [d for d in design.diagnostics() if d.code == "UndeclaredIdentifier"]
    assert [d.path for d in errors] == [(root / "t.sv").resolve()]
    assert errors[0].line == 3


def test_include_dir_with_non_ascii_path(tmp_path: Path):
    inc = tmp_path / "inc \u00fc"
    inc.mkdir()
    (inc / "w.svh").write_text("`define W 4\n")
    (tmp_path / "t.sv").write_text(
        '`include "w.svh"\nmodule t(output logic [`W-1:0] y);\n  assign y = \'0;\nendmodule\n'
    )
    (tmp_path / "t.f").write_text("+incdir+inc \u00fc\nt.sv\n", encoding="utf-8")
    design = load(tmp_path / "t.f", top="t")
    assert [d for d in design.diagnostics() if d.severity == "error"] == []


def test_define_value_with_backslash_and_quotes(tmp_path: Path):
    (tmp_path / "t.sv").write_text(
        "module t(output string s, output string p);\n"
        "  assign s = `MSG;\n  assign p = `WINPATH;\nendmodule\n"
    )
    (tmp_path / "t.f").write_text('+define+MSG="hi there"\n+define+WINPATH="C:\\\\tmp\\\\x"\nt.sv\n')
    design = load(tmp_path / "t.f", top="t")
    assert [d for d in design.diagnostics() if d.severity == "error"] == []


def test_overrides_replace_file_content_without_writing(tmp_path: Path):
    src = "module t(input logic a, output logic y1);\n  leaf u_l (.a(a), .y(y1));\nendmodule\n"
    _design(tmp_path, src)
    on_disk = (tmp_path / "t.sv").read_bytes()
    # A port that does not exist, not an undeclared signal: an unknown identifier
    # in a port connection is a legal implicit net, which slang ignores by default.
    new = src.replace("u_l", "u_renamed").replace(".a(a)", ".no_such_port(a)").encode()

    design = load(tmp_path / "t.f", top="t", overrides={tmp_path / "t.sv": new})

    assert (tmp_path / "t.sv").read_bytes() == on_disk
    assert design.roundtrip(tmp_path / "t.sv") == new
    (site,) = design.instance_sites("t")
    assert site.instance_name == "u_renamed"
    assert new[site.start : site.end] == b"leaf u_renamed (.no_such_port(a), .y(y1));"
    errors = [d for d in design.diagnostics() if d.severity == "error"]
    assert errors, "a connection to a port that does not exist must be an error"
    assert all(d.path == (tmp_path / "t.sv").resolve() for d in errors)
    assert any("no_such_port" in d.message for d in errors)


def test_overrides_apply_to_included_headers(tmp_path: Path):
    (tmp_path / "w.svh").write_bytes(b"`define SIG nope\n")
    (tmp_path / "t.sv").write_text(
        '`include "w.svh"\nmodule t(input logic a, output logic y);\n'
        "  assign y = `SIG & 1'b1;\nendmodule\n"
    )
    (tmp_path / "t.f").write_text("+incdir+.\nt.sv\n")
    on_disk = load(tmp_path / "t.f", top="t").diagnostics()
    fixed = load(
        tmp_path / "t.f", top="t", overrides={tmp_path / "w.svh": b"`define SIG a\n"}
    ).diagnostics()
    assert [d.code for d in on_disk if d.severity == "error"] == ["UndeclaredIdentifier"]
    assert [d for d in fixed if d.severity == "error"] == []
    assert (tmp_path / "w.svh").read_bytes() == b"`define SIG nope\n"


def test_override_for_a_file_not_in_the_design_is_an_error(tmp_path: Path):
    _design(tmp_path, "module t(input logic a, output logic y1);\n  assign y1 = a;\nendmodule\n")
    with pytest.raises(slang_adapter.AdapterError, match="stray.sv"):
        load(tmp_path / "t.f", top="t", overrides={tmp_path / "stray.sv": b"module s; endmodule\n"})


def test_bom_state_is_fixed_at_load_time(tmp_path: Path):
    body = b"\xef\xbb\xbfmodule m;\n  logic a;\nendmodule\n"
    (tmp_path / "m.sv").write_bytes(body)
    (tmp_path / "m.f").write_text("m.sv\n")
    design = load(tmp_path / "m.f", top="m")
    (tmp_path / "m.sv").write_bytes(body[3:])  # file changes after load
    assert design.roundtrip(tmp_path / "m.sv") == body


def test_invalid_utf8_in_included_header_is_refused(tmp_path: Path):
    (tmp_path / "h.svh").write_bytes(b"// caf\xe9\n`define W 4\n")
    (tmp_path / "t.sv").write_text('`include "h.svh"\nmodule t; endmodule\n')
    (tmp_path / "t.f").write_text("+incdir+.\nt.sv\n")
    with pytest.raises(slang_adapter.AdapterError, match="h.svh"):
        load(tmp_path / "t.f", top="t")


def test_drivers_of_an_instance_path_is_an_adapter_error(tmp_path: Path):
    design = _design(
        tmp_path, "module t(input logic a, output logic y1);\n  leaf u_l (.a(a), .y(y1));\nendmodule\n"
    )
    with pytest.raises(slang_adapter.AdapterError, match="u_l"):
        design.drivers_of("t.u_l")


def test_instance_sites_follow_the_elaborated_definition_for_interfaces(tmp_path: Path):
    (tmp_path / "leaf.sv").write_text(LEAF)
    (tmp_path / "t.sv").write_text(
        "interface ifc(input logic a);\n  logic y;\n  leaf u_in_ifc (.a(a), .y(y));\nendinterface\n"
        "module t(input logic a);\n  ifc u_ifc (.a(a));\nendmodule\n"
    )
    (tmp_path / "t.f").write_text("leaf.sv\nt.sv\n")
    design = load(tmp_path / "t.f", top="t")
    assert [s.instance_name for s in design.instance_sites("ifc")] == ["u_in_ifc"]
    assert [s.instance_name for s in design.instance_sites("t")] == ["u_ifc"]
