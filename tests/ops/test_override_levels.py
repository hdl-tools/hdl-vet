"""Overrides at more than one level, and the way of each enable to the top (phase 6, part 3).

OWNER DECISIONS (2026-11-18)

- A parent keeps a proved child as a black box when the child's enable is off
  "by the connection": the pin is connected directly to a new enable port of the
  parent, or to a constant 0. No formal proof of the pin in this part.
- Each enable is followed upward to the top of the design. Anything else than a
  new port or a constant 0 on the way fails the sign-off, unless the overrides
  that use the enable have a waiver.

RULES

H1  The way of an enable. For each module M that has a logic operation, and each
    enable E of M, the sign-off looks at each instance of M in the edited
    design. The pin E of the instance must be connected to one of these:
    - a constant 0 (`1'b0`, `1'h0`, `1'd0`, `'0`): the enable is "off" there
    - the plain name of an input port of the parent module that the parent of
      the input design does not have: the enable is "carried". That port is then
      an enable of the parent, and the rule is applied to the instances of the
      parent in the same way.
    A new input port of the top module ends the way. Any other connection (a
    constant 1, a signal or a port that the input design has, an expression, no
    connection, a pin that is left out) is "not off".
H2  `Result.enables` is a list with one record for each pin that was looked at:
    `module` (the parent module), `instance`, `pin`, `connection` (the text, ""
    when there is none), `status` ("off", "carried", "not off", "waived") and
    `operations` (the labels of the logic operations that use the enable, down
    the way). The list is in a fixed order. With no logic operation it is empty.
H3  A record "not off" fails the sign-off. Its line of the report:
      enable not off: top.u_core.test_en_i is connected to 1'b1 (override_net core.rst_sync_n)
    or `... is not connected (...)`. The name is `<parent module>.<instance>.<pin>`.
H4  A waiver on every operation of the record makes it "waived". The sign-off
    can then be ok. Its line:
      waived: enable top.u_core.test_en_i is connected to 1'b1 (override_net core.rst_sync_n)
H5  The check needs no Yosys.
H6  The manifest has the key "enables": a list of {module, instance, pin,
    connection, status} for every record, [] when there is none.
H7  A proved child is inert. A module M is given to the engine as `inert` (see
    tests/equiv/test_equiv_inert.py), with its enables, in the comparison of
    each module above it, when every record of M is "proved" by the engine (not
    waived). If M is not proved, it is part of the logic of the parent as
    before.
H8  The ties of a comparison are the enables of the module's own operations and
    the enables that it carries. The line of the report names them all.
H9  The order of the comparisons is from the leaves upward, so that a child is
    judged before its parent. The order of `Result.equivalence` does not change:
    by the order of the first operation of each module.
H10 A module that only carries an enable (a new port and a connection, no
    operation of its own) gets no comparison, as before.

STATED LIMITS

- "By the connection" only: an enable that goes through a gate, a register or a
  wire on its way up is "not off", although it may be 0 in fact. Use a waiver.
- A child that is waived, refuted or not checked is not inert: the parent is
  compared with the child inside, as before this part.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hdl_vet import equiv
from hdl_vet.cli import main
from override_design import Design, needs_yosys

pytestmark = [pytest.mark.equiv]

LEAF = (
    "module leaf (\n"
    "  input  logic clk_b,\n"
    "  input  logic rst_ni,\n"
    "  input  logic d,\n"
    "  output logic q\n"
    ");\n"
    "  logic q_r;\n"
    "\n"
    "  always_ff @(posedge clk_b or negedge rst_ni)\n"
    "    if (!rst_ni) q_r <= 1'b0;\n"
    "    else         q_r <= d;\n"
    "\n"
    "  assign q = q_r;\n"
    "endmodule\n"
)
MID = (
    "module mid (\n"
    "  input  logic clk_a,\n"
    "  input  logic clk_b,\n"
    "  input  logic rst_ni,\n"
    "  input  logic d,\n"
    "  output logic y\n"
    ");\n"
    "  logic lq, r;\n"
    "\n"
    "  leaf u_leaf (.clk_b(clk_b), .rst_ni(rst_ni), .d(d), .q(lq));\n"
    "\n"
    "  always_ff @(posedge clk_a or negedge rst_ni)\n"
    "    if (!rst_ni) r <= 1'b0;\n"
    "    else         r <= lq;\n"
    "\n"
    "  assign y = r;\n"
    "endmodule\n"
)
TOP = (
    "module top (\n"
    "  input  logic clk_a,\n"
    "  input  logic clk_b,\n"
    "  input  logic rst_ni,\n"
    "  input  logic mode_i,\n"
    "  input  logic d,\n"
    "  output logic y,\n"
    "  output logic z\n"
    ");\n"
    "  mid u_mid (.clk_a(clk_a), .clk_b(clk_b), .rst_ni(rst_ni), .d(d), .y(y));\n"
    "  assign z = d;\n"
    "endmodule\n"
)
FILES = {"leaf": LEAF, "mid": MID, "top": TOP}
LEAF_OP = "override_net leaf.q"
MID_OP = "override_net mid.y"


@pytest.fixture
def design(tmp_path: Path) -> Design:
    return Design(tmp_path / "design", FILES)


def no_yosys(monkeypatch) -> None:
    monkeypatch.setattr(equiv, "available", lambda: False)


def two_levels(vet, waiver: str | None = None) -> None:
    """One enable from the top to `leaf`, an override in `leaf` and one in `mid`."""
    vet.punch_port("test_en_i", "input", to="top.u_mid.u_leaf")
    vet.module("leaf").override_net("q", "1'b0", enable="test_en_i", waiver=waiver)
    vet.module("mid").override_net("y", "1'b1", enable="test_en_i", waiver=waiver)


def enables(result) -> list[tuple[str, str, str, str, str]]:
    return [(e.module, e.instance, e.pin, e.connection, e.status) for e in result.enables]


def records(result) -> dict[str, str]:
    return {r.module: r.status for r in result.equivalence}


# --- H7, H8, H9: two levels ----------------------------------------------------------------------


@needs_yosys
def test_overrides_at_two_levels_are_both_proved(design):
    vet = design.vet()
    two_levels(vet)
    result = vet.finish()
    assert records(result) == {"leaf": "proved", "mid": "proved"}, result.report()
    assert [r.module for r in result.equivalence] == ["leaf", "mid"]
    assert result.ok, result.report()
    assert f"  equivalence proved: mid ({MID_OP}; equal when test_en_i is 0)" in result.report()
    assert enables(result) == [
        ("mid", "u_leaf", "test_en_i", "test_en_i", "carried"),
        ("top", "u_mid", "test_en_i", "test_en_i", "carried"),
    ]
    assert "enable not off" not in result.report()


@needs_yosys
def test_the_parent_first_in_the_script_gives_the_same(design):
    vet = design.vet()
    vet.punch_port("test_en_i", "input", to="top.u_mid.u_leaf")
    vet.module("mid").override_net("y", "1'b1", enable="test_en_i")
    vet.module("leaf").override_net("q", "1'b0", enable="test_en_i")
    result = vet.finish()
    assert records(result) == {"leaf": "proved", "mid": "proved"}, result.report()
    assert [r.module for r in result.equivalence] == ["mid", "leaf"]
    assert result.ok


@needs_yosys
def test_two_enables_with_other_names_are_both_tied(design):
    """`mid` carries `leaf_en_i` to the leaf and has `mid_en_i` for its own override."""
    vet = design.vet()
    vet.punch_port("leaf_en_i", "input", to="top.u_mid.u_leaf")
    vet.punch_port("mid_en_i", "input", to="top.u_mid")
    vet.module("leaf").override_net("q", "1'b0", enable="leaf_en_i")
    vet.module("mid").override_net("y", "1'b1", enable="mid_en_i")
    result = vet.finish()
    assert records(result) == {"leaf": "proved", "mid": "proved"}, result.report()
    line = [line for line in result.report().splitlines() if "equivalence proved: mid" in line]
    assert len(line) == 1 and "leaf_en_i" in line[0] and "mid_en_i" in line[0]
    assert result.ok


@needs_yosys
def test_a_child_that_is_not_proved_is_not_inert(design):
    """The mux of the leaf is turned round by hand: the leaf is refuted, and the parent is not proved."""
    vet = design.vet()
    two_levels(vet)
    design.replace(vet, "leaf", b"test_en_i ? 1'b0 : q_pre", b"test_en_i ? q_pre : 1'b0")
    result = vet.finish()
    found = records(result)
    assert found["leaf"] == "refuted", result.report()
    assert found["mid"] != "proved", result.report()
    assert not result.ok


@needs_yosys
def test_a_waived_child_is_not_inert(design):
    vet = design.vet()
    vet.punch_port("test_en_i", "input", to="top.u_mid.u_leaf")
    vet.module("leaf").override_net("q", "1'b0", enable="test_en_i", waiver="not checked here")
    vet.module("mid").override_net("y", "1'b1", enable="test_en_i")
    result = vet.finish()
    found = records(result)
    assert found["leaf"] == "waived"
    assert found["mid"] == "error", result.report()
    (mid,) = [r for r in result.equivalence if r.module == "mid"]
    assert "clock" in mid.detail
    assert not result.ok


@needs_yosys
def test_a_pin_at_one_in_the_parent_is_never_proved(design):
    vet = design.vet()
    two_levels(vet)
    design.replace(vet, "mid", b".test_en_i(test_en_i)", b".test_en_i(1'b1)")
    result = vet.finish()
    found = records(result)
    assert found["leaf"] == "proved"
    assert found["mid"] != "proved", result.report()
    assert ("mid", "u_leaf", "test_en_i", "1'b1", "not off") in enables(result)
    assert f"  enable not off: mid.u_leaf.test_en_i is connected to 1'b1 ({LEAF_OP})" in result.report()
    assert not result.ok


# --- H1 to H5: the way of an enable --------------------------------------------------------------


def leaf_only(vet, waiver: str | None = "w") -> None:
    vet.punch_port("test_en_i", "input", to="top.u_mid.u_leaf")
    vet.module("leaf").override_net("q", "1'b0", enable="test_en_i", waiver=waiver)


def test_a_punched_enable_is_carried_to_the_top(design, monkeypatch):
    no_yosys(monkeypatch)
    vet = design.vet()
    leaf_only(vet)
    result = vet.finish()
    assert enables(result) == [
        ("mid", "u_leaf", "test_en_i", "test_en_i", "carried"),
        ("top", "u_mid", "test_en_i", "test_en_i", "carried"),
    ]
    assert [e.operations for e in result.enables] == [[LEAF_OP], [LEAF_OP]]
    assert result.ok, result.report()
    assert "enable" not in result.report().replace("test_en_i", "")


@pytest.mark.parametrize("zero", ["1'b0", "1'h0", "1'd0", "'0"])
def test_a_constant_zero_is_off(design, monkeypatch, zero):
    no_yosys(monkeypatch)
    vet = design.vet()
    vet.module("leaf").add_port("test_en_i", "input")
    vet.module("mid").instance("u_leaf").connect("test_en_i", zero)
    vet.module("leaf").override_net("q", "1'b0", enable="test_en_i", waiver="w")
    result = vet.finish()
    assert enables(result) == [("mid", "u_leaf", "test_en_i", zero, "off")]
    assert result.ok, result.report()


@pytest.mark.parametrize(
    "connection, says",
    [
        ("1'b1", "is connected to 1'b1"),
        ("mode_i", "is connected to mode_i"),
        ("d", "is connected to d"),
        ("~test_en_i", "is connected to ~test_en_i"),
        ("test_en_i & mode_i", "is connected to test_en_i & mode_i"),
        ("1'b0 | mode_i", "is connected to 1'b0 | mode_i"),
        ("", "is not connected"),
    ],
)
def test_another_connection_at_the_top_is_not_off(design, monkeypatch, connection, says):
    """The enable is carried through `mid`; the top connects it to something else."""
    no_yosys(monkeypatch)
    vet = design.vet()
    vet.punch_port("test_en_i", "input", to="top.u_mid.u_leaf")
    design.replace(
        vet, "top", b".test_en_i(test_en_i)", f".test_en_i({connection})".encode(), waiver="set-up of the test"
    )
    vet.module("leaf").override_net("q", "1'b0", enable="test_en_i", waiver="w")
    result = vet.finish()
    assert ("top", "u_mid", "test_en_i", connection, "waived") in enables(result)
    assert f"  waived: enable top.u_mid.test_en_i {says} ({LEAF_OP})" in result.report()


@needs_yosys
def test_not_off_without_a_waiver_fails_the_sign_off(design):
    vet = design.vet()
    vet.punch_port("test_en_i", "input", to="top.u_mid.u_leaf")
    design.replace(vet, "top", b".test_en_i(test_en_i)", b".test_en_i(mode_i)", waiver="set-up of the test")
    vet.module("leaf").override_net("q", "1'b0", enable="test_en_i")
    result = vet.finish()
    assert records(result) == {"leaf": "proved", "top": "waived"}
    assert enables(result) == [
        ("mid", "u_leaf", "test_en_i", "test_en_i", "carried"),
        ("top", "u_mid", "test_en_i", "mode_i", "not off"),
    ]
    assert f"  enable not off: top.u_mid.test_en_i is connected to mode_i ({LEAF_OP})" in result.report()
    assert not result.ok


def test_a_pin_that_is_left_out_is_not_off(design, monkeypatch):
    no_yosys(monkeypatch)
    vet = design.vet()
    vet.module("leaf").add_port("test_en_i", "input")
    vet.module("leaf").override_net("q", "1'b0", enable="test_en_i", waiver="w")
    result = vet.check()
    assert ("mid", "u_leaf", "test_en_i", "", "waived") in enables(result)
    assert f"  waived: enable mid.u_leaf.test_en_i is not connected ({LEAF_OP})" in result.report()


def test_a_waiver_on_one_of_two_operations_is_not_enough(design, monkeypatch):
    """Two overrides in the leaf use the enable: both need the waiver."""
    no_yosys(monkeypatch)
    leaf = LEAF.replace("  output logic q\n", "  output logic q,\n  output logic q2\n").replace(
        "  assign q = q_r;\n", "  assign q = q_r;\n  assign q2 = ~q_r;\n"
    )
    design = Design(design.root.parent / "two", {"leaf": leaf, "mid": MID, "top": TOP})
    vet = design.vet()
    vet.module("leaf").add_port("test_en_i", "input")
    vet.module("mid").instance("u_leaf").connect("test_en_i", "1'b1")
    vet.module("leaf").override_net("q", "1'b0", enable="test_en_i", waiver="w")
    vet.module("leaf").override_net("q2", "1'b0", enable="test_en_i", waiver="w")
    assert enables(vet.check()) == [("mid", "u_leaf", "test_en_i", "1'b1", "waived")]
    vet.module("leaf").override_net("q2", "1'b0", enable="test_en_i")
    result = vet.check()
    assert enables(result) == [("mid", "u_leaf", "test_en_i", "1'b1", "not off")]
    assert result.enables[0].operations == [LEAF_OP, "override_net leaf.q2"]
    assert not result.ok


def test_an_enable_of_the_top_module_has_no_way_to_go(design, monkeypatch):
    no_yosys(monkeypatch)
    vet = design.vet()
    vet.module("top").add_port("test_en_i", "input")
    vet.module("top").override_net("z", "1'b0", enable="test_en_i", waiver="w")
    result = vet.finish()
    assert result.enables == []
    assert result.ok


def test_without_a_logic_operation_there_is_no_record(design):
    vet = design.vet()
    vet.punch_port("test_en_i", "input", to="top.u_mid.u_leaf")
    result = vet.finish()
    assert result.enables == [] and result.equivalence == []
    assert result.ok


def test_every_instance_of_the_module_is_looked_at(tmp_path, monkeypatch):
    no_yosys(monkeypatch)
    top = TOP.replace(
        "  assign z = d;\n",
        "  logic y2;\n"
        "  mid u_mid2 (.clk_a(clk_a), .clk_b(clk_b), .rst_ni(rst_ni), .d(~d), .y(y2));\n"
        "  assign z = y2;\n",
    )
    design = Design(tmp_path / "design", {"leaf": LEAF, "mid": MID, "top": top})
    vet = design.vet()
    vet.module("mid", instances=2).add_port("test_en_i", "input")
    vet.module("top").add_port("test_en_i", "input")
    vet.module("top").instance("u_mid").connect("test_en_i", "test_en_i")
    vet.module("top").instance("u_mid2").connect("test_en_i", "mode_i")
    vet.module("mid", instances=2).override_net("y", "1'b1", enable="test_en_i", waiver="w")
    result = vet.finish()
    assert enables(result) == [
        ("top", "u_mid", "test_en_i", "test_en_i", "carried"),
        ("top", "u_mid2", "test_en_i", "mode_i", "waived"),
    ]


# --- H6: the manifest and the command line -------------------------------------------------------


def run(*args: object) -> int:
    return main([str(a) for a in args])


def test_the_manifest_lists_the_enables(design, tmp_path, capsys, monkeypatch):
    no_yosys(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "dft.py").write_text(
        "def apply(vet):\n"
        '    vet.punch_port("test_en_i", "input", to="top.u_mid")\n'
        '    vet.module("leaf").add_port("test_en_i", "input")\n'
        '    vet.module("mid").instance("u_leaf").connect("test_en_i", "1\'b1")\n'
        '    vet.module("leaf").override_net("q", "1\'b0", enable="test_en_i", waiver="see TB-7")\n',
        encoding="utf-8",
    )
    out = tmp_path / "out"
    assert run("apply", "--no-project", "-f", design.filelist, "--top", "top", "-s", "dft.py", "-o", out) == 0
    err = capsys.readouterr().err
    assert f"waived: enable mid.u_leaf.test_en_i is connected to 1'b1 ({LEAF_OP})" in err
    manifest = json.loads((out / "hdl-vet.json").read_text(encoding="utf-8"))
    assert manifest["enables"] == [
        {"module": "mid", "instance": "u_leaf", "pin": "test_en_i", "connection": "1'b1", "status": "waived"}
    ]


def test_not_off_gives_exit_status_one(design, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "dft.py").write_text(
        "def apply(vet):\n"
        '    vet.module("leaf").add_port("test_en_i", "input")\n'
        '    vet.module("mid").instance("u_leaf").connect("test_en_i", "1\'b1")\n'
        '    vet.module("leaf").override_net("q", "1\'b0", enable="test_en_i", waiver=None)\n',
        encoding="utf-8",
    )
    if not equiv.available():
        pytest.skip("Yosys (hdl-vet[equiv]) is missing")
    out = tmp_path / "out"
    assert run("apply", "--no-project", "-f", design.filelist, "--top", "top", "-s", "dft.py", "-o", out) == 1
    err = capsys.readouterr().err
    assert f"enable not off: mid.u_leaf.test_en_i is connected to 1'b1 ({LEAF_OP})" in err
