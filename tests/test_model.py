"""Phase 2: additions to `hdl_vet.slang_adapter` (still the only pyslang importer).

Diagnostics
    Diag gains a sixth field, `offset: int | None`: the byte offset of the
    diagnostic in `path` (macro locations resolved to the use site, as for
    `line`); None when there is no location.

    `ImplicitNet` is always reported, with severity "warning". slang ignores it
    by default, which would let a port connected to an undeclared signal pass
    sign-off unnoticed.

    Port width mismatches on an instance connection are always reported
    (severity "warning" or "error").

Source model (syntax level; all ranges are byte offsets [start, end) in `path`)
    Design.modules() -> list[str]
        Names of all module/interface/program definitions in the sources, sorted.

    Design.module(name) -> ModuleInfo          AdapterError if unknown
    ModuleInfo
        name, path
        start, end        the whole declaration, `module` through `endmodule`
        ports_close       offset of the ")" closing an ANSI port list, else None
        body_end          offset of the `endmodule` keyword
        ports: list[PortInfo]            declaration order
        params: list[ParamInfo]          parameter-port-list order
        instances: list[InstanceInfo]    source order
        has_macros        True if any token of the declaration comes from a macro
        .port(name) / .param(name) / .instance(name)   AdapterError if unknown
    PortInfo
        name, direction ("input" | "output" | "inout" | "ref" | "interface")
        start, end        this port's own item in the list: `input logic [3:0] a`,
                          or just `y1` when it shares a declaration (`... y0, y1`)
    ParamInfo
        name, default (source text of the default value, None if absent)
        start, end        the declarator: `WIDTH = 4`
    InstanceInfo
        name, module, path, from_macro
        stmt_start, stmt_end       the whole statement including ";" (as InstanceSite)
        module_start, module_end   the module-name token
        start, end                 this instance: its name through its closing ")"
        params_close               offset of the ")" closing `#( ... )`, else None
        ports_close                offset of the ")" closing the connection list
        connections: list[ConnInfo]            source order
        param_overrides: list[ParamOverride]   source order
        .connection(port) / .param_override(name)   AdapterError if unknown
    ConnInfo
        port     name for `.name(expr)` and `.name`; "*" for `.*`; None if positional
        expr     source text inside the parentheses, stripped; "" for `.name()`;
                 the port name for `.name`; the expression text if positional
        start, end   the whole connection: `.d   (a)`
    ParamOverride
        name (None if positional), value (source text, stripped), start, end

Clarifications
    - modules() lists module, interface and program definitions (not packages);
      module(name) accepts any of them.
    - ModuleInfo.instances includes instances inside generate blocks, taken or
      not. instance(name) raises AdapterError when the name occurs more than
      once in the module (different generate branches).
    - When InstanceInfo.from_macro is True, only name, module, path, stmt_start
      and stmt_end are reliable; the other offsets are best effort and must not
      be used for splicing.
    - A non-ANSI PortInfo's range is its name in the header port list.
    - Separator tokens and trivia are never part of a range.

Elaborated scope
    elaborated_instance_count() and snapshot() cover exactly the instances
    reachable from the top instance(s), module and interface instances alike.
    Instances slang creates for definitions nobody instantiates (path "$unit")
    are excluded from both. (This narrows the phase 1 count, which included
    them.) len(snapshot()) == elaborated_instance_count().

Structural snapshot (elaborated)
    Connection text: the source text of the connected expression with comments
    removed and whitespace runs collapsed to one space; the port name for
    `.name` and `.*` connections; the connected instance's name for an
    interface port; "" when unconnected. For output and inout ports it is the
    connected signal, not an assignment.
    Parameters: value parameters as slang prints the resolved value ("4",
    "32'd2"); type parameters as slang prints the type.
    Ports: an interface port is ("interface", 0).

    Design.snapshot() -> dict[str, InstanceSnap]
        Keyed by hierarchical path ("top", "top.u_mid", "top.u_mid.u_l0",
        "t.u_arr[0]", "t.g[0].u_g"). Only instances.
    InstanceSnap (frozen)
        module: str
        params: dict[str, str]                   resolved value, as slang prints it
        ports: dict[str, tuple[str, int]]        name -> (direction, bit width)
        connections: dict[str, str]              port name -> expression source text
                                                 with whitespace runs collapsed to one
                                                 space; "" when unconnected; {} for a top
"""

from __future__ import annotations

from pathlib import Path

import pytest
from vetkit import make_design, span

from hdl_vet import slang_adapter
from hdl_vet.slang_adapter import load


@pytest.fixture
def kit(tmp_path: Path):
    return make_design(tmp_path)


# --- diagnostics ------------------------------------------------------------


def test_implicit_net_is_reported_with_offset(kit):
    design = load(kit.filelist, top="top")
    data = kit.top.read_bytes()
    hits = [d for d in design.diagnostics() if d.code == "ImplicitNet"]
    assert len(hits) == 1
    (d,) = hits
    assert d.severity == "warning"
    assert d.path == kit.top
    assert d.offset == span(data, b"spare_q")[0]
    assert d.line == data[: d.offset].count(b"\n") + 1


def test_baseline_has_no_errors(kit):
    design = load(kit.filelist, top="top")
    assert [d for d in design.diagnostics() if d.severity == "error"] == []


def test_every_located_diagnostic_has_an_offset_in_range(kit):
    bad = kit.top.read_bytes().replace(b"assign spare_d = a;", b"assign spare_d = nope;")
    design = load(kit.filelist, top="top", overrides={kit.top: bad})
    diags = design.diagnostics()
    (undeclared,) = [d for d in diags if d.code == "UndeclaredIdentifier"]
    assert undeclared.offset == span(bad, b"nope")[0]
    for d in diags:
        assert (d.path is None) == (d.offset is None)
        if d.path is not None:
            assert 0 <= d.offset < len(bad if d.path == kit.top else d.path.read_bytes())


def test_port_width_mismatch_is_reported(kit):
    bad = kit.mid.read_bytes().replace(b"#(.WIDTH(4)) u_l0", b"#(.WIDTH(8)) u_l0")
    base = load(kit.filelist, top="top").diagnostics()
    diags = load(kit.filelist, top="top", overrides={kit.mid: bad}).diagnostics()
    extra = [d for d in diags if d.path == kit.mid]
    assert not [d for d in base if d.path == kit.mid]
    assert extra, "an 8-bit port connected to 4-bit signals must be diagnosed"
    lo, hi = span(bad, b"u_l0 (.clk(clk), .d(a), .q(y0))")
    assert all(lo <= d.offset < hi for d in extra)


# --- source model -----------------------------------------------------------


def test_modules_lists_all_definitions(kit):
    assert load(kit.filelist, top="top").modules() == ["alt_leaf", "leaf", "mid", "top"]


def test_unknown_module_is_an_adapter_error(kit):
    with pytest.raises(slang_adapter.AdapterError, match="nope"):
        load(kit.filelist, top="top").module("nope")


def test_module_ranges(kit):
    m = load(kit.filelist, top="top").module("top")
    data = kit.top.read_bytes()
    assert (m.name, m.path) == ("top", kit.top)
    assert data[m.start : m.end] == data[data.index(b"module top") :].rstrip(b"\n")
    assert data[m.body_end : m.body_end + 9] == b"endmodule"
    assert data[m.ports_close : m.ports_close + 2] == b");"
    assert data[:m.ports_close].count(b")") == 0
    assert m.has_macros is False


def test_ports_with_ranges(kit):
    m = load(kit.filelist, top="top").module("mid")
    data = kit.mid.read_bytes()
    assert [(p.name, p.direction) for p in m.ports] == [
        ("clk", "input"),
        ("a", "input"),
        ("y0", "output"),
        ("y1", "output"),
    ]
    assert [data[p.start : p.end] for p in m.ports] == [
        b"input  logic       clk",
        b"input  logic [3:0] a",
        b"output logic [3:0] y0",
        b"y1",
    ]
    assert m.port("y1").name == "y1"
    with pytest.raises(slang_adapter.AdapterError, match="zz"):
        m.port("zz")


def test_params_with_ranges(kit):
    m = load(kit.filelist, top="top").module("leaf")
    data = kit.leaf.read_bytes()
    assert [(p.name, p.default) for p in m.params] == [("WIDTH", "4"), ("DEPTH", "2")]
    assert [data[p.start : p.end] for p in m.params] == [b"WIDTH = 4", b"DEPTH = 2"]
    assert m.param("DEPTH").default == "2"
    assert load(kit.filelist, top="top").module("mid").params == []


def test_instances_with_ranges(kit):
    m = load(kit.filelist, top="top").module("mid")
    data = kit.mid.read_bytes()
    assert [(i.name, i.module) for i in m.instances] == [("u_l0", "leaf"), ("u_l1", "leaf")]
    i0, i1 = m.instances
    assert data[i0.stmt_start : i0.stmt_end] == b"leaf #(.WIDTH(4)) u_l0 (.clk(clk), .d(a), .q(y0));"
    assert data[i0.start : i0.end] == b"u_l0 (.clk(clk), .d(a), .q(y0))"
    assert data[i0.module_start : i0.module_end] == b"leaf"
    assert i0.module_start == i0.stmt_start
    assert data[i0.params_close] == ord(")") and data[i0.params_close - 9 : i0.params_close] == b".WIDTH(4)"
    assert data[i0.ports_close : i0.ports_close + 2] == b");"
    assert i0.from_macro is False and i0.path == kit.mid
    assert data[i1.start : i1.end].startswith(b"u_l1 (\n") and data[i1.end - 1] == ord(")")
    assert data[i1.stmt_end - 1] == ord(";")
    assert m.instance("u_l1") == i1
    with pytest.raises(slang_adapter.AdapterError, match="u_zz"):
        m.instance("u_zz")


def test_connections_and_param_overrides(kit):
    m = load(kit.filelist, top="top").module("mid")
    data = kit.mid.read_bytes()
    i0, i1 = m.instances
    assert [(c.port, c.expr) for c in i0.connections] == [("clk", "clk"), ("d", "a"), ("q", "y0")]
    assert [data[c.start : c.end] for c in i0.connections] == [b".clk(clk)", b".d(a)", b".q(y0)"]
    assert [data[c.start : c.end] for c in i1.connections] == [b".clk (clk)", b".d   (a)", b".q   (y1)"]
    assert [(p.name, p.value) for p in i1.param_overrides] == [("WIDTH", "4"), ("DEPTH", "3")]
    assert [data[p.start : p.end] for p in i1.param_overrides] == [b".WIDTH(4)", b".DEPTH(3)"]
    assert i1.connection("d").expr == "a"
    assert i1.param_override("DEPTH").value == "3"
    with pytest.raises(slang_adapter.AdapterError, match="nope"):
        i1.connection("nope")


def test_instance_without_parameter_overrides(kit):
    (u_mid, u_spare) = load(kit.filelist, top="top").module("top").instances
    assert u_mid.params_close is None and u_mid.param_overrides == []
    assert u_spare.connection("q").expr == "spare_q"


def test_connection_forms(tmp_path: Path):
    (tmp_path / "t.sv").write_text(
        "module leaf #(parameter int P = 0) (input logic clk, input logic d, output logic q);\n"
        "  assign q = d;\nendmodule\n"
        "module t(input logic clk, input logic d, output logic q, output logic q2, output logic q3);\n"
        "  leaf u_a (.clk, .d(), .q(q));\n"
        "  leaf u_b (clk, d & 1'b1, q2);\n"
        "  leaf u_c (.q(q3), .*);\n"
        "  leaf #(1) u_d [1:0] (.clk(clk), .d(d), .q());\n"
        "endmodule\n"
    )
    (tmp_path / "t.f").write_text("t.sv\n")
    m = load(tmp_path / "t.f", top="t").module("t")
    data = (tmp_path / "t.sv").read_bytes()
    a, b, c, d = m.instances
    assert [(x.port, x.expr) for x in a.connections] == [("clk", "clk"), ("d", ""), ("q", "q")]
    assert [data[x.start : x.end] for x in a.connections] == [b".clk", b".d()", b".q(q)"]
    assert [(x.port, x.expr) for x in b.connections] == [(None, "clk"), (None, "d & 1'b1"), (None, "q2")]
    assert [(x.port, x.expr) for x in c.connections] == [("q", "q3"), ("*", "")]
    assert data[c.connections[1].start : c.connections[1].end] == b".*"
    assert d.name == "u_d"
    assert data[d.start : d.end] == b"u_d [1:0] (.clk(clk), .d(d), .q())"
    assert [(p.name, p.value) for p in d.param_overrides] == [(None, "1")]


def test_macro_tokens_are_flagged(tmp_path: Path):
    (tmp_path / "t.sv").write_text(
        "`define W 4\n"
        "module plain(input logic a);\nendmodule\n"
        "module t(input logic [`W-1:0] a);\n  plain u_p (.a(a[0]));\nendmodule\n"
    )
    (tmp_path / "t.f").write_text("t.sv\n")
    design = load(tmp_path / "t.f", top="t")
    assert design.module("t").has_macros is True
    assert design.module("plain").has_macros is False
    assert design.module("t").instances[0].from_macro is False


def test_several_modules_in_one_file(tmp_path: Path):
    src = "module a(input logic x);\nendmodule\n\nmodule b(input logic x);\n  a u_a (.x(x));\nendmodule\n"
    (tmp_path / "t.sv").write_bytes(src.encode())
    (tmp_path / "t.f").write_text("t.sv\n")
    design = load(tmp_path / "t.f", top="b")
    data = src.encode()
    a, b = design.module("a"), design.module("b")
    assert data[a.start : a.end] == b"module a(input logic x);\nendmodule"
    assert data[b.start : b.end].startswith(b"module b(") and b.start > a.end
    assert [i.name for i in a.instances] == [] and [i.name for i in b.instances] == ["u_a"]


def test_module_without_ports_or_with_non_ansi_ports(tmp_path: Path):
    (tmp_path / "t.sv").write_text(
        "module none;\nendmodule\n"
        "module old(a, y);\n  input a;\n  output y;\n  assign y = a;\nendmodule\n"
        "module t(input logic a, output logic y);\n  none u_n ();\n  old u_o (.a(a), .y(y));\nendmodule\n"
    )
    (tmp_path / "t.f").write_text("t.sv\n")
    design = load(tmp_path / "t.f", top="t")
    assert design.module("none").ports == [] and design.module("none").ports_close is None
    old = design.module("old")
    assert old.ports_close is None
    assert [(p.name, p.direction) for p in old.ports] == [("a", "input"), ("y", "output")]
    assert design.module("t").instance("u_n").connections == []


# --- snapshot ---------------------------------------------------------------


def test_snapshot_of_the_small_design(kit):
    snap = load(kit.filelist, top="top").snapshot()
    assert sorted(snap) == ["top", "top.u_mid", "top.u_mid.u_l0", "top.u_mid.u_l1", "top.u_spare"]
    assert snap["top"].module == "top" and snap["top"].connections == {}
    assert snap["top"].ports == {
        "clk": ("input", 1),
        "a": ("input", 4),
        "y0": ("output", 4),
        "y1": ("output", 4),
    }
    l1 = snap["top.u_mid.u_l1"]
    assert l1.module == "leaf"
    assert l1.params == {"WIDTH": "4", "DEPTH": "3"}
    assert l1.ports == {"clk": ("input", 1), "d": ("input", 4), "q": ("output", 4)}
    assert l1.connections == {"clk": "clk", "d": "a", "q": "y1"}
    assert snap["top.u_spare"].params == {"WIDTH": "4", "DEPTH": "2"}
    assert snap["top.u_spare"].connections["q"] == "spare_q"


def test_snapshot_reflects_overrides_and_is_equal_when_nothing_changes(kit):
    base = load(kit.filelist, top="top").snapshot()
    assert load(kit.filelist, top="top").snapshot() == base
    bad = kit.mid.read_bytes().replace(b"#(.WIDTH(4)) u_l0", b"#(.WIDTH(8)) u_l0")
    snap = load(kit.filelist, top="top", overrides={kit.mid: bad}).snapshot()
    assert snap["top.u_mid.u_l0"].params["WIDTH"] == "8"
    assert snap["top.u_mid.u_l0"].ports["q"] == ("output", 8)
    assert snap["top.u_mid.u_l1"] == base["top.u_mid.u_l1"]


def test_snapshot_connection_text_is_whitespace_normalised_and_handles_unconnected(tmp_path: Path):
    (tmp_path / "t.sv").write_text(
        "module leaf(input logic [1:0] d, output logic q, output logic r);\n"
        "  assign q = ^d;\n  assign r = 1'b0;\nendmodule\n"
        "module t(input logic a, input logic b, output logic q);\n"
        "  leaf u_a (.d({a,\n                b}), .q(q), .r());\n"
        "endmodule\n"
    )
    (tmp_path / "t.f").write_text("t.sv\n")
    snap = load(tmp_path / "t.f", top="t").snapshot()
    assert snap["t.u_a"].connections == {"d": "{a, b}", "q": "q", "r": ""}


def test_snapshot_covers_generate_and_arrays(tmp_path: Path):
    (tmp_path / "t.sv").write_text(
        "module leaf(input logic d);\nendmodule\n"
        "module t(input logic [1:0] a);\n"
        "  leaf u_arr [1:0] (.d(a));\n"
        "  for (genvar i = 0; i < 2; i++) begin : g\n    leaf u_g (.d(a[i]));\n  end\n"
        "endmodule\n"
    )
    (tmp_path / "t.f").write_text("t.sv\n")
    snap = load(tmp_path / "t.f", top="t").snapshot()
    assert len(snap) == 5
    assert sum(1 for s in snap.values() if s.module == "leaf") == 4
    assert "t.g[0].u_g" in snap and "t.g[1].u_g" in snap
    assert {"t.u_arr[0]", "t.u_arr[1]"} <= set(snap)


def test_instance_count_excludes_uninstantiated_definitions(kit):
    design = load(kit.filelist, top="top")
    assert design.elaborated_instance_count() == 5  # alt_leaf is never instantiated
    assert len(design.snapshot()) == 5


def test_comment_inside_a_connection_is_not_part_of_its_snapshot_text(tmp_path: Path):
    (tmp_path / "t.sv").write_text(
        "module leaf(input logic d);\nendmodule\n"
        "module t(input logic a, input logic b);\n  leaf u_a (.d(a /* why */ | b));\nendmodule\n"
    )
    (tmp_path / "t.f").write_text("t.sv\n")
    assert load(tmp_path / "t.f", top="t").snapshot()["t.u_a"].connections == {"d": "a | b"}


def test_repeated_instance_name_in_generate_branches(tmp_path: Path):
    (tmp_path / "t.sv").write_text(
        "module leaf(input logic d);\nendmodule\n"
        "module t #(parameter bit P = 1) (input logic a);\n"
        "  if (P) begin : g_a\n    leaf u_x (.d(a));\n  end else begin : g_b\n    leaf u_x (.d(~a));\n  end\n"
        "endmodule\n"
    )
    (tmp_path / "t.f").write_text("t.sv\n")
    m = load(tmp_path / "t.f", top="t").module("t")
    assert [i.name for i in m.instances] == ["u_x", "u_x"]
    with pytest.raises(slang_adapter.AdapterError, match="u_x"):
        m.instance("u_x")
