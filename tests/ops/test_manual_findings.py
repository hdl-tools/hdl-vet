"""Four problems of the tool that the work on the manual found. Rules and tests.

The owner chose to fix all four (2026-11-17). Each was listed in the chapter
"Limits and known problems" of the manual; the manual changes with the fix.

G1  Names. `Module.add_port` and `Module.add_param` refuse a name that is not a
    simple identifier, or that is a SystemVerilog keyword, when they are called:
    the same rule and the same words as `Module.add_wire` ("... is not a simple
    identifier (or is a SystemVerilog keyword)"). Nothing is queued and nothing
    is recorded. Before, such a name was written into the design and only the
    sign-off failed, with parse errors.

G2  Escaped identifiers. An instance whose name is an escaped identifier
    (`leaf \\u/esc (...)`) is named in a script by the name without the backslash
    and without the blank that ends it: "u/esc", as `Module.info` shows it. The
    spelling with the backslash, with or without the blank, names the same
    instance. Every operation on such an instance works as on any other:
    `Module.instance`, `Instance.connect`, `disconnect`, `set_param`,
    `replace_module`, `allow_changes_below`, `Module.remove_instance`,
    `Instance.paths`, and the sign-off accepts the result. Before, `instance`
    found it, every operation on it was refused ("not elaborated under the top
    at a known path"), and `remove_instance` was applied and then failed
    sign-off, or was skipped without a word when the name had the backslash.
    The edited text keeps the escaped identifier as it was written.
    Left open: a hierarchical path (`punch_port`, `propagate_param`, `expect`)
    through an instance with an escaped name.

G3  Errors of the raw interface. `Vet.splice` and `Vet.text` raise a `VetError`
    for every request they cannot carry out: a range that overlaps an earlier
    splice, a range outside the file, a file that is not part of the design, an
    `old=` that does not match. The message starts with "splice " or "text " and
    the name of the file. So a script catches it with `except VetError`, and
    `vet apply` prints it as every other refusal: "vet: script NAME: ...", exit
    status 2. Before, these were a `SpliceError` or a `SessionError`, which are
    not a `VetError`, and the command line printed them without the script.

G4  Primitives at sign-off. The comparison of the structure also covers the
    instances that are not module instances: gate primitives (`and g1 (...)`),
    user-defined primitives and checker instances. They are compared like module
    instances and reported with the same kinds of change:
      instance-added, instance-removed   the instance appears or is gone
      module-changed                     another primitive (`and` became `or`)
      connection-changed                 the text of a connection differs; the
                                         name of the change is the position of
                                         the terminal, "0" for the first
    A named primitive has the path of its name (`top.g1`). An unnamed one has
    the path `<scope>.<TYPE:N>`, N counting the unnamed primitives of that type
    in the scope from 0, in the order of the text (`top.<not:0>`).
    No operation edits a primitive, so such a change comes from a raw splice and
    must be declared with `vet.expect` (or allowed with `vet.permit`), as any
    other change the operations do not make. Before, the sign-off did not see it.
    Nothing else changes: a primitive is not a module (`vet.module("and")` is an
    error, `Vet.module_paths` does not list it), the paths of module instances
    are as before, and a design without primitives gives the same result.
    Left open: delays and strengths of a primitive are not compared.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from findings_design import TOP, Design, changes, statuses
from hdl_vet.cli import main
from hdl_vet.ops import Vet, VetError
from no_comparison import no_comparison  # noqa: F401


@pytest.fixture
def design(tmp_path: Path) -> Design:
    return Design(tmp_path / "design")


@pytest.fixture
def vet(design: Design) -> Vet:
    return design.vet()


def test_the_design_is_clean(vet):
    assert vet.finish().ok


# --- G1 -----------------------------------------------------------------------------------------

BAD_NAMES = ["a b", "module", "wire", "1x", "", "a-b", "a.b", "\\x/y ", "x[0]", "input"]


@pytest.mark.parametrize("name", BAD_NAMES)
def test_add_port_refuses_a_name_that_is_not_an_identifier(vet, design, name):
    with pytest.raises(VetError, match="add_port top: .*is not a simple identifier") as e:
        vet.module("top").add_port(name, "input")
    assert repr(name) in str(e.value)
    assert statuses(vet) == []
    assert design.text(vet) == TOP
    assert vet.finish().ok


@pytest.mark.parametrize("name", BAD_NAMES)
def test_add_param_refuses_a_name_that_is_not_an_identifier(vet, design, name):
    with pytest.raises(VetError, match="add_param top: .*is not a simple identifier") as e:
        vet.module("top").add_param(name, 1)
    assert repr(name) in str(e.value)
    assert statuses(vet) == []
    assert design.text(vet) == TOP
    assert vet.finish().ok


@pytest.mark.parametrize("name", ["test_en_i", "_x", "a$b", "Module", "x1"])
def test_good_names_are_accepted(design, name):
    vet = design.vet()
    vet.module("top").add_port(name, "output")
    vet.module("top").add_param(name + "_P", 1)
    assert [s[2] for s in statuses(vet)] == ["applied", "applied"]
    assert f" {name}\n" in design.text(vet) or f" {name}," in design.text(vet)


# --- G2 -----------------------------------------------------------------------------------------

SPELLINGS = ["u/esc", "\\u/esc", "\\u/esc "]


def test_the_escaped_instance_is_listed_without_the_backslash(vet):
    top = vet.module("top")
    assert [i.name for i in top.info.instances] == ["u/esc", "u_plain"]
    assert top.instance("u/esc").paths == ["top.\\u/esc "]


@pytest.mark.parametrize("name", SPELLINGS)
def test_operations_on_an_escaped_instance(design, name):
    vet = design.vet()
    inst = vet.module("top").instance(name)
    inst.connect("d", "b", was="a")
    inst.set_param("W", 2)
    assert [s[2] for s in statuses(vet)] == ["applied", "applied"]
    assert "  leaf #(.W(2)) \\u/esc (.clk(clk), .d(b), .q(y));\n" in design.text(vet)
    assert vet.finish().ok


@pytest.mark.parametrize("name", SPELLINGS)
def test_a_second_run_on_an_escaped_instance_is_skipped(design, name):
    vet = design.vet()
    vet.module("top").instance(name).connect("d", "b", was="a")
    vet.module("top").instance("u/esc").connect("d", "b", was="a")
    assert [s[2] for s in statuses(vet)] == ["applied", "skipped"]
    assert vet.finish().ok


@pytest.mark.parametrize("name", SPELLINGS)
def test_an_escaped_instance_can_be_removed(design, name):
    vet = design.vet()
    top = vet.module("top")
    with vet.group():
        top.remove_instance(name, module="leaf")
        top.remove_port("y")
    assert [s[0:3:2] for s in statuses(vet)] == [("remove_instance", "applied"), ("remove_port", "applied")]
    text = design.text(vet)
    assert "u/esc" not in text
    assert "u_plain" in text
    result = vet.finish()
    assert result.ok, result
    assert ("instance-removed", "top.\\u/esc ", "") in changes(result)


def test_removing_it_twice_is_skipped_and_a_wrong_name_is_too(design):
    vet = design.vet()
    top = vet.module("top")
    with vet.group():
        top.remove_instance("u/esc")
        top.remove_port("y")
    top.remove_instance("\\u/esc ")
    top.remove_instance("u/other")
    assert [s[2] for s in statuses(vet)] == ["applied", "applied", "skipped", "skipped"]
    assert vet.finish().ok


def test_an_escaped_instance_can_get_another_module(vet, design):
    inst = vet.module("top").instance("\\u/esc ")
    inst.replace_module("other", was="leaf")
    assert "  other \\u/esc (.clk(clk), .d(a), .q(y));\n" in design.text(vet)
    assert vet.finish().ok


def test_disconnect_and_allow_changes_below_on_an_escaped_instance(vet, design):
    inst = vet.module("top").instance("u/esc")
    inst.allow_changes_below()
    inst.disconnect("q", was="y")
    assert ".q()" in design.text(vet)
    assert [s[2] for s in statuses(vet)] == ["applied"]


def test_a_plain_instance_is_not_found_by_an_escaped_spelling_of_another(vet):
    with pytest.raises(VetError, match="no instance"):
        vet.module("top").instance("u/plain")
    assert vet.module("top").instance("\\u_plain ").paths == ["top.u_plain"]


# --- G3 -----------------------------------------------------------------------------------------


def test_an_overlapping_splice_is_a_vet_error(vet, design):
    vet.splice(design.top, 0, 5, b"x", waiver="set-up of the test")
    with pytest.raises(VetError, match=r"splice top\.sv: .*overlaps"):
        vet.splice(design.top, 2, 7, b"y", waiver="set-up of the test")
    vet.splice(design.top, 0, 5, b"x", waiver="set-up of the test")  # the same splice again is accepted, as before


def test_a_splice_outside_the_file_is_a_vet_error(vet, design):
    with pytest.raises(VetError, match=r"splice top\.sv: .*out of range"):
        vet.splice(design.top, 10, 10**6, b"x")
    assert vet.finish().ok


def test_a_splice_into_a_file_that_is_not_of_the_design_is_a_vet_error(vet, design, tmp_path):
    stranger = tmp_path / "stranger.sv"
    stranger.write_bytes(b"module stranger; endmodule\n")
    with pytest.raises(VetError, match=r"splice stranger\.sv: "):
        vet.splice(stranger, 0, 0, b"// x\n")
    with pytest.raises(VetError, match=r"text stranger\.sv: "):
        vet.text(stranger)
    assert vet.finish().ok


def test_a_splice_whose_old_text_differs_is_a_vet_error(vet, design):
    with pytest.raises(VetError, match=r"splice top\.sv: "):
        vet.splice(design.top, 0, 6, b"MODULE", old=b"nodule")
    assert design.text(vet) == TOP


def test_a_script_can_catch_the_error_of_a_splice(design, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "catch.py").write_text(
        "from pathlib import Path\n"
        "\n"
        "from hdl_vet.ops import VetError\n"
        "\n"
        "\n"
        "def apply(vet):\n"
        f"    top = Path({str(design.top)!r})\n"
        "    vet.splice(top, 0, 5, b'x', old=b'modul', waiver='set-up of the test')\n"
        "    try:\n"
        "        vet.splice(top, 2, 7, b'y', waiver='set-up of the test')\n"
        "    except VetError as error:\n"
        "        print('caught:', error)\n"
        "    vet.splice(top, 0, 5, b'modul', old=b'modul')\n",
        encoding="utf-8",
    )
    status = main(["apply", "--no-project", "-f", str(design.filelist), "--top", "top", "-s", "catch.py", "--dry-run"])
    captured = capsys.readouterr()
    assert "caught: splice top.sv:" in captured.out
    assert "Traceback" not in captured.err
    assert status in (0, 1, 2)


def test_the_command_line_names_the_script_for_a_splice_error(design, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bad.py").write_text(
        "from pathlib import Path\n"
        "\n"
        "\n"
        "def apply(vet):\n"
        f"    top = Path({str(design.top)!r})\n"
        "    vet.splice(top, 0, 5, b'x', waiver='set-up of the test')\n"
        "    vet.splice(top, 2, 7, b'y', waiver='set-up of the test')\n",
        encoding="utf-8",
    )
    status = main(["apply", "--no-project", "-f", str(design.filelist), "--top", "top", "-s", "bad.py", "--dry-run"])
    captured = capsys.readouterr()
    assert status == 2
    lines = captured.err.splitlines()
    assert len(lines) == 1, captured.err
    assert lines[0].startswith("vet: script bad.py: splice top.sv: ")
    assert "overlaps" in lines[0]


# --- G4 -----------------------------------------------------------------------------------------


def test_a_rewired_gate_fails_sign_off(vet, design):
    design.replace(vet, b"(n1, a, b)", b"(n1, a, a)")
    result = vet.finish()
    assert not result.ok
    assert [(c.kind, c.path, c.name) for c in result.unexpected_changes] == [("connection-changed", "top.g1", "2")]
    assert "unexpected change: connection-changed top.g1" in result.report()


def test_a_declared_change_of_a_gate_passes(vet, design):
    vet.expect("connection-changed", "top.g1", "2")
    design.replace(vet, b"(n1, a, b)", b"(n1, a, a)")
    result = vet.finish()
    assert result.ok, result.report()
    assert changes(result) == {("connection-changed", "top.g1", "2")}


def test_another_gate_type_is_a_changed_module(vet, design):
    design.replace(vet, b"and g1", b"or  g1")
    result = vet.finish()
    assert not result.ok
    found = [(c.kind, c.path, c.before, c.after) for c in result.unexpected_changes]
    assert found == [("module-changed", "top.g1", "and", "or")]


def test_a_gate_that_is_added_or_removed(design):
    vet = design.vet()
    design.replace(vet, b"  and g1 (n1, a, b);\n", b"  assign n1 = a & b;\n")
    result = vet.finish()
    assert [(c.kind, c.path) for c in result.unexpected_changes] == [("instance-removed", "top.g1")]

    vet = design.vet()
    design.replace(vet, b"  logic n1;\n", b"  logic n1;\n  logic n2;\n  xor g2 (n2, a, b);\n")
    result = vet.finish()
    assert [(c.kind, c.path, c.after) for c in result.unexpected_changes] == [("instance-added", "top.g2", "xor")]

    vet = design.vet()
    vet.permit("instance-added", "top.g2")
    design.replace(vet, b"  logic n1;\n", b"  logic n1;\n  logic n2;\n  xor g2 (n2, a, b);\n")
    assert vet.finish().ok


def test_an_unnamed_gate_has_a_path_by_type_and_order(vet, design):
    design.replace(vet, b"not (z, n1)", b"not (z, a)")
    result = vet.finish()
    assert [(c.kind, c.path, c.name) for c in result.unexpected_changes] == [("connection-changed", "top.<not:0>", "1")]

    again = design.vet()
    again.expect("connection-changed", "top.<not:0>", "1")
    design.replace(again, b"not (z, n1)", b"not (z, a)")
    assert again.finish().ok


def test_a_second_unnamed_gate_of_the_same_type(tmp_path: Path):
    top = TOP.replace("  not (z, n1);\n", "  logic n2;\n  not (n2, n1);\n  not (z, n2);\n")
    design = Design(tmp_path / "design", top)
    vet = design.vet()
    assert vet.check().ok
    design.replace(vet, b"not (z, n2)", b"not (z, n1)")
    result = vet.finish()
    assert [(c.kind, c.path, c.name) for c in result.unexpected_changes] == [("connection-changed", "top.<not:1>", "1")]


def test_a_user_defined_primitive_is_compared(tmp_path: Path):
    udp = (
        "primitive mux_p (output y, input s, input a, input b);\n"
        "  table\n"
        "    0 0 ? : 0;\n"
        "    0 1 ? : 1;\n"
        "    1 ? 0 : 0;\n"
        "    1 ? 1 : 1;\n"
        "  endtable\n"
        "endprimitive\n"
    )
    top = udp + TOP.replace("  not (z, n1);\n", "  mux_p u_mux (z, n1, a, b);\n")
    design = Design(tmp_path / "design", top)
    vet = design.vet()
    assert vet.check().ok
    design.replace(vet, b"(z, n1, a, b)", b"(z, n1, b, a)")
    result = vet.finish()
    assert {(c.kind, c.path, c.name) for c in result.unexpected_changes} == {
        ("connection-changed", "top.u_mux", "2"),
        ("connection-changed", "top.u_mux", "3"),
    }


def test_an_operation_beside_a_gate_is_not_disturbed(vet, design):
    top = vet.module("top")
    top.add_port("t_i", "input")
    top.instance("u_plain").connect("d", "t_i", was="b")
    result = vet.finish()
    assert result.ok, result.report()
    assert not any(c.path in ("top.g1", "top.<not:0>") for c in result.changes)


def test_a_primitive_is_not_a_module(vet):
    assert vet.module_paths("and") == []
    assert vet.module_paths("leaf") == ["top.\\u/esc ", "top.u_plain"]
    with pytest.raises(VetError, match="no module 'and'"):
        vet.module("and")
    assert [i.name for i in vet.module("top").info.instances] == ["u/esc", "u_plain"]


def test_a_design_without_primitives_is_as_before(tmp_path: Path):
    top = TOP.replace("  and g1 (n1, a, b);\n", "  assign n1 = a & b;\n").replace(
        "  not (z, n1);\n", "  assign z = ~n1;\n"
    )
    design = Design(tmp_path / "design", top)
    vet = design.vet()
    vet.module("top").instance("u_plain").set_param("W", 3)
    result = vet.finish()
    assert result.ok
    assert changes(result) == {("param-changed", "top.u_plain", "W")}
