"""D1 acceptance: the pyslang adapter, on a small hand-written fixture.

These tests pin the pyslang capabilities the architecture rests on (handover doc,
"pyslang API calls that worked"). They talk only to hdl_vet.slang_adapter, which is
the single module allowed to import pyslang.

Interface under test (hdl_vet.slang_adapter):

    load(filelist: Path, top: str | None = None) -> Design

    Design.files                      : list[Path]   source files, filelist order
    Design.roundtrip(path) -> bytes   the file re-printed from its syntax tree
    Design.diagnostics() -> list[Diag]   parse + elaboration diagnostics
    Design.elaborated_instance_count() -> int   module instances under the top(s),
                                                the top itself included
    Design.instance_sites(module: str) -> list[InstanceSite]
        instantiation statements written inside the body of `module`, source order
    Design.drivers_of(hier_path: str) -> list[Driver]   every driver slang reports
    syntax_equivalent(a: str, b: str) -> bool  trees equal ignoring trivia;
                                               AdapterError if either text fails to parse

    diagnostics() includes the analysis pass (e.g. MultipleContAssigns), and
    severity is the engine's effective severity, not Diagnostic.isError().

    Driver: is_input_port (bool) - True when the "driver" is the module's own
            input port, i.e. the value comes from outside the module.

    Encoding: a UTF-8 BOM is preserved by roundtrip. A file that is not valid
    UTF-8 makes load() raise AdapterError naming the file (hdl-vet refuses it).

    Diag: severity ("error" | "warning" | "note"), code (str, non-empty, stable name
          such as "UndeclaredIdentifier"), message (str), path (Path | None),
          line (int | None, 1-based)

    InstanceSite: instance_name (str), module_name (str), path (Path),
                  start (int), end (int), from_macro (bool)
        For from_macro == False, [start, end) are BYTE offsets into the file at
        `path` covering the whole instantiation statement: first byte of the module
        name through the terminating ';'.
        For from_macro == True the site was produced by a macro expansion; path,
        start and end then describe the macro USE in the file that invokes it
        (the "`LEAF_INST(...)" text), never the macro definition.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet import slang_adapter
from hdl_vet.slang_adapter import load, syntax_equivalent
from vetkit import keeps_the_asked_spelling

FIXTURES = Path(__file__).resolve().parent.parent / "support" / "fixtures"
SMALL = FIXTURES / "small"


@pytest.fixture(scope="module")
def small():
    return load(SMALL / "small.f", top="top")


# --- round trip ------------------------------------------------------------


def test_roundtrip_is_byte_identical_for_every_fixture_file(small):
    assert len(small.files) == 4
    for path in small.files:
        assert small.roundtrip(path) == path.read_bytes(), path.name


def test_roundtrip_included_header_is_untouched():
    # The header is not in the filelist; it must still be readable and unchanged.
    hdr = SMALL / "include" / "macros.svh"
    before = hdr.read_bytes()
    load(SMALL / "small.f", top="top")
    assert hdr.read_bytes() == before


@pytest.mark.parametrize(
    "body",
    [
        b"module m;\r\n  logic a;\r\nendmodule\r\n",  # CRLF
        b"module m;\n\tlogic\ta;\t// tab\n endmodule\n",  # tabs, leading space
        "module m; // café µm 中\n  logic a;\nendmodule\n".encode(),  # UTF-8
        b"module m;\n  logic a;\nendmodule",  # no final newline
        b"\n\n// only trivia before\nmodule m; endmodule\n\n\n// trailing trivia\n",
    ],
    ids=["crlf", "tabs", "utf8", "no-final-newline", "leading-trailing-trivia"],
)
def test_roundtrip_preserves_awkward_bytes(tmp_path: Path, body: bytes):
    src = tmp_path / "m.sv"
    src.write_bytes(body)
    (tmp_path / "m.f").write_text("m.sv\n")
    design = load(tmp_path / "m.f", top="m")
    assert design.roundtrip(src) == body


# --- elaboration and diagnostics ------------------------------------------


def test_small_elaborates_without_errors(small):
    errors = [d for d in small.diagnostics() if d.severity == "error"]
    assert errors == []


def test_elaborated_instance_count(small):
    # top, u_mid, u_mid.u_leaf_a, u_mid.u_leaf_b, u_macro_leaf
    assert small.elaborated_instance_count() == 5


def test_undeclared_identifier_is_reported_with_code_and_location():
    design = load(FIXTURES / "broken" / "broken.f", top="bad")
    errors = [d for d in design.diagnostics() if d.severity == "error"]
    assert len(errors) == 1
    (d,) = errors
    assert d.code == "UndeclaredIdentifier"
    assert "not_declared" in d.message
    assert d.path == (FIXTURES / "broken" / "bad.sv").resolve()
    assert d.line == 5


def test_syntax_error_still_loads_and_reports(tmp_path: Path):
    (tmp_path / "s.sv").write_text("module s;\n  logic a\nendmodule\n")
    (tmp_path / "s.f").write_text("s.sv\n")
    design = load(tmp_path / "s.f", top="s")
    errors = [d for d in design.diagnostics() if d.severity == "error"]
    assert errors, "a missing semicolon must produce an error diagnostic"
    assert all(d.code for d in errors)
    assert any(d.path == (tmp_path / "s.sv").resolve() for d in errors)


def test_analysis_diagnostics_are_included_with_engine_severity(tmp_path: Path):
    # Two continuous assigns to a variable: reported only by the analysis pass,
    # and Diagnostic.isError() is False for it although its severity is error.
    (tmp_path / "m.sv").write_text(
        "module m(input logic a, input logic b, output logic y);\n"
        "  assign y = a;\n  assign y = b;\nendmodule\n"
    )  # fmt: skip
    (tmp_path / "m.f").write_text("m.sv\n")
    diags = load(tmp_path / "m.f", top="m").diagnostics()
    hits = [d for d in diags if d.code == "MultipleContAssigns"]
    assert len(hits) == 1
    assert hits[0].severity == "error"
    assert hits[0].path == (tmp_path / "m.sv").resolve()


def test_filelist_defines_reach_elaboration(tmp_path: Path):
    (tmp_path / "m.sv").write_text(
        "module m(output logic [7:0] y);\n"
        "`ifdef VET_ON\n  assign y = `VET_VAL;\n`else\n  assign y = nope;\n`endif\n"
        "endmodule\n"
    )
    (tmp_path / "m.f").write_text("+define+VET_ON+VET_VAL=8'd3\nm.sv\n")
    design = load(tmp_path / "m.f", top="m")
    assert [d for d in design.diagnostics() if d.severity == "error"] == []
    assert design.roundtrip(tmp_path / "m.sv") == (tmp_path / "m.sv").read_bytes()


def test_utf8_bom_is_preserved(tmp_path: Path):
    body = b"\xef\xbb\xbfmodule m;\n  logic a;\nendmodule\n"
    (tmp_path / "m.sv").write_bytes(body)
    (tmp_path / "m.f").write_text("m.sv\n")
    assert load(tmp_path / "m.f", top="m").roundtrip(tmp_path / "m.sv") == body


def test_invalid_utf8_is_refused(tmp_path: Path):
    (tmp_path / "m.sv").write_bytes(b"module m; // caf\xe9\nendmodule\n")
    (tmp_path / "m.f").write_text("m.sv\n")
    with pytest.raises(slang_adapter.AdapterError, match="m.sv"):
        load(tmp_path / "m.f", top="m")


def test_unknown_top_is_an_error_not_an_empty_design():
    with pytest.raises(slang_adapter.AdapterError, match="no_such_top"):
        load(SMALL / "small.f", top="no_such_top")


def test_diagnostics_are_deterministic():
    a = load(SMALL / "small.f", top="top").diagnostics()
    b = load(SMALL / "small.f", top="top").diagnostics()
    assert a == b


# --- instance sites: elaborated design back to source bytes ----------------


def test_instance_sites_in_mid(small):
    sites = small.instance_sites("mid")
    assert [(s.instance_name, s.module_name) for s in sites] == [
        ("u_leaf_a", "leaf"),
        ("u_leaf_b", "leaf"),
    ]
    data = (SMALL / "rtl" / "mid.sv").read_bytes()
    for s in sites:
        assert s.path == (SMALL / "rtl" / "mid.sv").resolve()
        assert s.from_macro is False
        text = data[s.start : s.end]
        assert text.startswith(b"leaf #(")
        assert text.endswith(b");")
        assert s.instance_name.encode() in text
    assert sites[0].end <= sites[1].start
    # Exact statement, nothing more: the two ranges contain exactly one ';' each.
    assert all(data[s.start : s.end].count(b";") == 1 for s in sites)


def test_instance_sites_in_top_flag_macro_origin(small):
    sites = small.instance_sites("top")
    assert [(s.instance_name, s.module_name) for s in sites] == [
        ("u_mid", "mid"),
        ("u_macro_leaf", "leaf"),
    ]
    plain, macro = sites
    data = (SMALL / "rtl" / "top.sv").read_bytes()

    assert plain.from_macro is False
    assert data[plain.start : plain.end].startswith(b"mid u_mid (")
    assert data[plain.start : plain.end].endswith(b");")

    assert macro.from_macro is True
    assert macro.path == (SMALL / "rtl" / "top.sv").resolve()
    # The range points at the macro use in top.sv, not into macros.svh.
    assert data[macro.start : macro.end] == b"`LEAF_INST(u_macro_leaf, mid_q, macro_q)"


def test_instance_sites_of_leaf_module_is_empty(small):
    assert small.instance_sites("leaf") == []


def test_instance_sites_unknown_module_raises(small):
    with pytest.raises(slang_adapter.AdapterError, match="nope"):
        small.instance_sites("nope")


def test_byte_offsets_are_bytes_not_characters(tmp_path: Path):
    # A multi-byte comment before the instance shifts byte offsets but not
    # character offsets; the range must still cut the exact statement.
    (tmp_path / "leaf.sv").write_text("module leaf(input logic a); endmodule\n")
    top = "module t(input logic a);\n  // 中文 café\n  leaf u_l (.a(a));\nendmodule\n"
    (tmp_path / "t.sv").write_bytes(top.encode())
    (tmp_path / "t.f").write_text("leaf.sv\nt.sv\n")
    design = load(tmp_path / "t.f", top="t")
    (site,) = design.instance_sites("t")
    data = (tmp_path / "t.sv").read_bytes()
    assert data[site.start : site.end] == b"leaf u_l (.a(a));"


# --- drivers ---------------------------------------------------------------


def test_drivers_of_counts_continuous_assigns(small):
    assert len(small.drivers_of("top.w_o")) == 2
    assert len(small.drivers_of("top.q_o")) == 1
    assert not any(d.is_input_port for d in small.drivers_of("top.w_o"))


def test_drivers_of_sees_through_instance_output(small):
    # stage is driven only by u_leaf_a's output port connection.
    (drv,) = small.drivers_of("top.u_mid.stage")
    assert drv.is_input_port is False


def test_drivers_of_input_port_is_flagged_not_hidden(small):
    # slang reports the port itself as the driver; callers decide what that means.
    for path in ("top.sel_i", "top.u_mid.d_i"):
        (drv,) = small.drivers_of(path)
        assert drv.is_input_port is True, path


def test_drivers_of_unknown_path_raises(small):
    with pytest.raises(slang_adapter.AdapterError, match="nope"):
        small.drivers_of("top.nope")


# --- syntax equivalence ----------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("assign q = sel ? a : b;", "assign  q = sel ?a:b ; // note", True),
        ("assign q = sel ? a : b;", "assign q = sel ?\n    a /* x */ : b;", True),
        ("assign q = sel ? a : b;", "assign q = sel ? b : a;", False),
        ("assign q = a + b;", "assign q = a - b;", False),
        ("assign q = 1'b1;", "assign q = 1'b0;", False),
        ("assign q = a;", "assign q = (a);", False),
    ],
)
def test_syntax_equivalent(a: str, b: str, same: bool):
    assert syntax_equivalent(a, b) is same
    assert syntax_equivalent(b, a) is same


def test_syntax_equivalent_rejects_unparseable_text():
    with pytest.raises(slang_adapter.AdapterError):
        syntax_equivalent("assign q = ;", "assign q = a;")


# --- containment -----------------------------------------------------------


def test_only_the_adapter_imports_pyslang():
    import ast

    src = Path(slang_adapter.__file__).parent.parent
    offenders = []
    for p in src.rglob("*.py"):
        if p.parent.name == "slang_adapter":
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            if any(n.split(".")[0] == "pyslang" for n in names):
                offenders.append(p.name)
    assert offenders == []


def test_a_header_included_in_two_cases_is_one_file(tmp_path: Path):
    """On a case-insensitive filesystem two spellings of one include used to resolve to
    two paths, so the design held one header twice and the output tree renamed it."""
    if not keeps_the_asked_spelling(tmp_path):
        pytest.skip("a wrong spelling cannot survive here, so one header cannot become two")
    (tmp_path / "inc").mkdir()
    (tmp_path / "rtl").mkdir()
    (tmp_path / "inc" / "defs.svh").write_text("`ifndef DEFS\n`define DEFS\n`define W 4\n`endif\n")
    (tmp_path / "rtl" / "t.sv").write_text(
        '`include "defs.svh"\n`include "DEFS.svh"\nmodule t(input logic [`W-1:0] a);\nendmodule\n'
    )
    (tmp_path / "t.f").write_text("+incdir+inc\nrtl/t.sv\n")
    design = load(tmp_path / "t.f", top="t")
    assert [d for d in design.diagnostics() if d.severity == "error"] == []
    assert design.included == [tmp_path / "inc" / "defs.svh"], design.included
    assert list(design.included_bytes) == [tmp_path / "inc" / "defs.svh"]
