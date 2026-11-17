"""The four fixes of tests/test_manual_findings.py: tests from the adversarial review.

The review accepted G2 and G3 as sound and found no false failure of G4 in the
flows that matter (a gate-level cell added with `add_file` and swapped in, cells
removed or swapped away, operations beside gates). These rules close what it
found.

H1  Arrays of gates (G4). Each element of `and g [3:0] (...)` is compared with
    the text of the array's connections, so a rewired array is seen. Before,
    every element had empty connection texts and no change was reported.
H2  Only what is elaborated (G4). A primitive in a generate branch that is not
    taken is not in the comparison, as a module instance there is not. So a raw
    edit in such a branch reports nothing, and an operation that makes the
    branch taken (a `set_param`) reports the primitive as `instance-added`.
H3  A design with a user-defined primitive can be edited. `vet.module`, the
    operations and `vet init` work on it. Before (and before G4), any design
    with a UDP made `vet.module` and `vet init` fail with an AttributeError.
H4  Names (G1). A name that begins with a backslash is refused by `add_port`,
    `add_param`, `add_wire` and `punch_port`, also without the ending blank; and
    a name that is not a string is refused with the same VetError.
H5  Escaped names (G2). An instance whose escaped name is a keyword or a plain
    identifier (`leaf \\module (...)`, `leaf \\u_x (...)`) works with every
    operation, also `replace_module`, which failed with a RuntimeError.
H6  The raw interface (G3). `Vet.splice` refuses, with a VetError that starts
    with "splice " and the file name, offsets that are not integers and a text
    or an `old=` that is not bytes. `Vet.text` refuses a file that is not a
    path. A script that calls `vet.finish` is stopped by the command line with
    one line that names the script, exit status 2.

LEFT OPEN

- A wrong type for another argument of an operation (a name that is None, a
  mapping that is a list) still raises a Python error, not a VetError.
- An exact expectation for an element of an array needs `exact=True`, because a
  pattern reads `[0]` as a set of characters.
- Changing the type of an unnamed gate, or the order of two, shifts the numbers
  in `<TYPE:N>`; the report then shows several changes for one edit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.cli import main
from hdl_vet.ops import Vet, VetError
from test_manual_findings import ESC, LEAF, TOP, Design, changes, statuses


@pytest.fixture
def design(tmp_path: Path) -> Design:
    return Design(tmp_path / "design")


@pytest.fixture
def vet(design: Design) -> Vet:
    return design.vet()


# --- H1 -----------------------------------------------------------------------------------------

ARRAY_TOP = TOP.replace(
    "  logic n1;\n",
    "  logic n1;\n  logic [3:0] ya;\n  and g_arr [3:0] (ya, a, b);\n",
)


def test_a_rewired_gate_array_fails_sign_off(tmp_path: Path):
    design = Design(tmp_path / "design", ARRAY_TOP)
    vet = design.vet()
    assert vet.check().ok
    design.replace(vet, b"(ya, a, b)", b"(ya, a, a)")
    result = vet.finish()
    assert not result.ok
    assert {(c.kind, c.path, c.name) for c in result.unexpected_changes} == {
        ("connection-changed", f"top.g_arr[{i}]", "2") for i in range(4)
    }


def test_a_rewired_gate_array_with_scalar_terminals_fails_sign_off(tmp_path: Path):
    """The case of the review: every terminal is one bit, so slang gives the elements no text of their own."""
    top = TOP.replace("  logic n1;\n", "  logic n1;\n  wire yw;\n  and g_s [3:0] (yw, a, b);\n")
    design = Design(tmp_path / "design", top)
    vet = design.vet()
    assert vet.check().ok
    design.replace(vet, b"(yw, a, b)", b"(yw, a, a)")
    result = vet.finish()
    assert not result.ok
    assert {(c.kind, c.path, c.name) for c in result.unexpected_changes} == {
        ("connection-changed", f"top.g_s[{i}]", "2") for i in range(4)
    }
    assert all(c.before == "b" and c.after == "a" for c in result.unexpected_changes)


def test_a_rewired_gate_array_with_a_vector_terminal_fails_sign_off(tmp_path: Path):
    """A terminal as wide as the array: each element gets one bit of it, and slang gives that bit no text."""
    top = TOP.replace(
        "  logic n1;\n",
        "  logic n1;\n  logic [3:0] yv;\n  logic [3:0] av;\n  assign av = {4{a}};\n  and g_v [3:0] (yv, av, b);\n",
    )
    design = Design(tmp_path / "design", top)
    vet = design.vet()
    assert vet.check().ok
    design.replace(vet, b"(yv, av, b)", b"(yv, ~av, b)")
    result = vet.finish()
    assert not result.ok
    assert {(c.kind, c.path, c.name) for c in result.unexpected_changes} == {
        ("connection-changed", f"top.g_v[{i}]", "1") for i in range(4)
    }
    assert all(c.before and c.after and c.before != c.after for c in result.unexpected_changes)


def test_a_declared_change_of_a_gate_array_passes(tmp_path: Path):
    design = Design(tmp_path / "design", ARRAY_TOP)
    vet = design.vet()
    vet.expect("connection-changed", "top.g_arr*", "2")
    design.replace(vet, b"(ya, a, b)", b"(ya, a, a)")
    result = vet.finish()
    assert result.ok, result.report()

    again = design.vet()
    again.expect("connection-changed", "top.g_arr[0]", "2", exact=True)
    again.permit("connection-changed", "top.g_arr*", "2")
    design.replace(again, b"(ya, a, b)", b"(ya, a, a)")
    assert again.finish().ok


def test_an_untouched_gate_array_reports_nothing(tmp_path: Path):
    design = Design(tmp_path / "design", ARRAY_TOP)
    vet = design.vet()
    vet.module("top").instance("u_plain").set_param("W", 2)
    result = vet.finish()
    assert result.ok
    assert changes(result) == {("param-changed", "top.u_plain", "W")}


# --- H2 -----------------------------------------------------------------------------------------

MID = (
    "module mid #(\n"
    "  parameter int N = 1\n"
    ") (\n"
    "  input  logic       a,\n"
    "  output logic [1:0] y\n"
    ");\n"
    "  for (genvar i = 0; i < 2; i++) begin : gl\n"
    "    if (i < N) begin : on\n"
    "      not c (y[i], a);\n"
    "    end else begin : off\n"
    "      buf d (y[i], a);\n"
    "    end\n"
    "  end\n"
    "endmodule\n"
)
GEN_TOP = MID + TOP.replace(
    "  logic n1;\n",
    "  logic n1;\n  logic [1:0] ym;\n  mid #(.N(1)) u_mid (.a(a), .y(ym));\n",
)


def test_a_gate_in_a_branch_that_is_not_taken_is_not_compared(tmp_path: Path):
    design = Design(tmp_path / "design", GEN_TOP)
    vet = design.vet()
    assert vet.check().ok
    result = vet.check()
    assert result.changes == []
    # gl[0] takes `on`, gl[1] takes `off`: these two gates exist, and nothing else of the loop.
    vet.expect("connection-changed", "top.u_mid.gl[[]0[]].on.c", "1")
    vet.expect("connection-changed", "top.u_mid.gl[[]1[]].off.d", "1")
    design.replace(vet, b"not c (y[i], a)", b"not c (y[i], 1'b0)")
    design.replace(vet, b"buf d (y[i], a)", b"buf d (y[i], 1'b1)")
    result = vet.finish()
    assert changes(result) == {
        ("connection-changed", "top.u_mid.gl[0].on.c", "1"),
        ("connection-changed", "top.u_mid.gl[1].off.d", "1"),
    }
    assert result.ok, result.report()


def test_a_parameter_that_takes_another_branch_shows_the_gates(tmp_path: Path):
    design = Design(tmp_path / "design", GEN_TOP)
    vet = design.vet()
    vet.module("top").instance("u_mid").set_param("N", 2, was=1)
    result = vet.finish()
    found = changes(result)
    assert ("param-changed", "top.u_mid", "N") in found
    assert ("instance-added", "top.u_mid.gl[1].on.c", "") in found
    assert ("instance-removed", "top.u_mid.gl[1].off.d", "") in found
    assert not result.ok  # the two gate changes are not declared

    again = design.vet()
    inst = again.module("top").instance("u_mid")
    inst.allow_changes_below()
    inst.set_param("N", 2, was=1)
    assert again.finish().ok


# --- H3 -----------------------------------------------------------------------------------------

UDP = "primitive inv_p (output y, input a);\n  table\n    0 : 1;\n    1 : 0;\n  endtable\nendprimitive\n"
UDP_TOP = UDP + TOP.replace("  not (z, n1);\n", "  inv_p u_inv (z, n1);\n")


def test_a_design_with_a_udp_can_be_edited(tmp_path: Path):
    design = Design(tmp_path / "design", UDP_TOP)
    vet = design.vet()
    top = vet.module("top")
    assert [i.name for i in top.info.instances] == ["u/esc", "u_plain"]
    top.add_port("t_i", "input")
    top.instance("u_plain").connect("d", "t_i", was="b")
    assert vet.module_paths("inv_p") == []
    with pytest.raises(VetError, match="no module 'inv_p'"):
        vet.module("inv_p")
    result = vet.finish()
    assert result.ok, result.report()


def test_vet_init_works_on_a_design_with_a_udp(tmp_path: Path, capsys):
    design = Design(tmp_path / "design", UDP_TOP)
    project = tmp_path / "proj"
    status = main(["init", str(project), "-f", str(design.filelist), "--top", "top"])
    captured = capsys.readouterr()
    assert status == 0, captured.err
    script = (project / "scripts" / "01_example.py").read_text(encoding="utf-8")
    assert "u_plain" in script
    assert "u_inv" not in script
    assert "Traceback" not in captured.err


# --- H4 -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["\\a", "\\a ", "\\", "\\a b"])
def test_a_name_with_a_backslash_is_refused_everywhere(vet, design, name):
    top = vet.module("top")
    for call in (
        lambda: top.add_port(name, "input"),
        lambda: top.add_param(name, 1),
        lambda: top.add_wire(name),
        lambda: vet.punch_port(name, "input", to="top.u_plain"),
    ):
        with pytest.raises(VetError, match="is not a simple identifier"):
            call()
    assert statuses(vet) == []
    assert design.text(vet) == TOP


@pytest.mark.parametrize("name", [None, 3, b"x", ["x"]])
def test_a_name_that_is_not_a_string_is_refused(vet, design, name):
    top = vet.module("top")
    for call in (lambda: top.add_port(name, "input"), lambda: top.add_param(name, 1), lambda: top.add_wire(name)):
        with pytest.raises(VetError, match="is not a simple identifier"):
            call()
    assert statuses(vet) == []
    assert design.text(vet) == TOP


# --- H5 -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("written, name", [("\\module ", "module"), ("\\u_x ", "u_x"), ("\\wire ", "wire")])
def test_an_escaped_name_that_is_a_keyword_or_a_plain_name(tmp_path: Path, written, name):
    line = ESC.replace("\\u/esc ", written)
    assert line != ESC
    design = Design(tmp_path / "design", TOP.replace(ESC, line))
    vet = design.vet()
    assert vet.check().ok
    top = vet.module("top")
    assert name in [i.name for i in top.info.instances]

    inst = top.instance(name)
    inst.connect("d", "b", was="a")
    inst.set_param("W", 2)
    inst.replace_module("other", was="leaf")
    assert [s[2] for s in statuses(vet)] == ["applied", "applied", "applied"]
    assert f"  other #(.W(2)) {written}(.clk(clk), .d(b), .q(y));\n" in design.text(vet)
    assert vet.finish().ok

    again = design.vet()
    top = again.module("top")
    with again.group():
        top.remove_instance(written, module="leaf")
        top.remove_port("y")
    assert again.finish().ok
    assert LEAF  # shared with test_manual_findings.py


# --- H6 -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args, kwargs",
    [
        ((0.0, 6.0, b"module"), {}),
        (("0", "6", b"module"), {}),
        ((0, 6, "module"), {}),
        ((0, 6, b"module"), {"old": "module"}),
        ((None, 6, b"module"), {}),
        ((6, 0, b"module"), {}),
        ((-1, 6, b"module"), {}),
    ],
    ids=["float", "str-offsets", "str-text", "str-old", "none", "start-after-end", "negative"],
)
def test_a_splice_with_a_wrong_argument_is_a_vet_error(vet, design, args, kwargs):
    with pytest.raises(VetError, match=r"splice top\.sv: "):
        vet.splice(design.top, *args, **kwargs)
    assert design.text(vet) == TOP
    assert vet.finish().ok


@pytest.mark.parametrize("path", [None, 3, b"top.sv"])
def test_text_of_something_that_is_not_a_path_is_a_vet_error(vet, path):
    with pytest.raises(VetError, match="text "):
        vet.text(path)


def test_a_path_given_as_a_string_still_works(vet, design):
    assert vet.text(str(design.top)) == TOP.encode()
    vet.splice(str(design.top), 0, 6, b"module", old=b"module")
    assert vet.finish().ok


@pytest.mark.parametrize("mode", [["--dry-run"], ["-o", "out"], ["--save", "s.zip"]], ids=["dry-run", "output", "save"])
def test_a_script_that_finishes_the_session_is_stopped(design, tmp_path, capsys, monkeypatch, mode):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ends.py").write_text(
        'def apply(vet):\n    vet.module("top").add_wire("t")\n    vet.finish()\n', encoding="utf-8"
    )
    status = main(["apply", "--no-project", "-f", str(design.filelist), "--top", "top", "-s", "ends.py", *mode])
    captured = capsys.readouterr()
    assert status == 2
    lines = captured.err.splitlines()
    assert len(lines) == 1, captured.err
    assert lines[0].startswith("vet: script ends.py")
    assert "finish" in lines[0]
    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "s.zip").exists()
