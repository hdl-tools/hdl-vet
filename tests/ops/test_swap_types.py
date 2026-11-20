"""`replace_module` and the types of ports and parameters (#59, the two defects).

`replace_module` compares each carried port on the elaborated design, three ways: the
direction, the bit width, and the type. The type was compared as slang spells it, which is
the name of the alias when the port is declared with a typedef or with a type parameter.
Two modules that agree on every bit were refused because they spell the type differently.

A type parameter had a second problem. The overridable parameters of the new module were
taken as those of kind `value`, so an override of a type parameter landed on nothing and was
refused as "not accounted for". The two remedies the message named do not work. `params=`
refuses the same way, and `drop_params=` takes the parameter back to its default, so every
port that uses the type becomes one bit wide.

Neither of these is a missing option. Both are fixed here. The waiver for a type difference
that is real, `accept_types=`, is a separate part.

Rules
    Y1  A port whose type is a type parameter is compared by the type it resolves to, not
        by the name of the parameter. Two modules that resolve to one type are accepted.
    Y2  A port declared with a typedef is compared by the type it resolves to. Two
        typedefs of the same bits are one type.
    Y3  A difference that is real is still refused. `bit` is not `logic`. The message
        names the types the two ports resolve to, not the aliases.
    Y4  The sign-off is not touched. `InstanceSnap` gains `port_resolved` beside
        `port_types`, and the sign-off keeps reading `port_types`, so a port whose typedef
        is replaced by another typedef of the same bits is still `port-type-changed`.
    Y5  An unpacked dimension is part of the resolved type too, so it is still seen.
    Y6  An override of a type parameter is kept when the new module has a type parameter
        of that name.
    Y7  `params=` renames an override of a type parameter onto a type parameter of the new
        module.
    Y8  An override of a type parameter that lands on nothing is still refused. The
        message names only remedies that work.
    Y9  A type parameter and a value parameter are not interchangeable. `params=` refuses
        a map from one kind to the other.

Not checked
    `accept_types=`, the waiver for a real type difference, which is a separate part.
    A type parameter whose two modules declare different defaults. The override is carried
    over unchanged, so the elaborated type is the same, and the default is not read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session
from hdl_vet.signoff import diff_snapshots
from hdl_vet.slang_adapter import InstanceSnap

TYPES = "typedef logic [7:0] byte_t;\ntypedef logic [7:0] word_t;\ntypedef bit [7:0] two_state_t;\n"

# Every cell below is a drop-in for `old_cell` unless its name says otherwise.
CELLS = {
    "old_cell": "module old_cell #(parameter type T = logic) (input logic clk, input T d, output T q);\n"
    "  always_ff @(posedge clk) q <= d;\nendmodule\n",
    # the same type parameter under the same name
    "new_cell": "module new_cell #(parameter type T = logic) (input logic clk, input T d, output T q);\n"
    "  always_ff @(posedge clk) q <= d;\nendmodule\n",
    # the same type parameter under another name
    "renamed_cell": "module renamed_cell #(parameter type U = logic) (input logic clk, input U d, output U q);\n"
    "  always_ff @(posedge clk) q <= d;\nendmodule\n",
    # concrete, and the same eight bits the override gives the old cell
    "byte_cell": "module byte_cell (input logic clk, input byte_t d, output byte_t q);\n"
    "  always_ff @(posedge clk) q <= d;\nendmodule\n",
    # concrete, eight bits, but two-state
    "bit_cell": "module bit_cell (input logic clk, input two_state_t d, output two_state_t q);\n"
    "  always_ff @(posedge clk) q <= d;\nendmodule\n",
    # a value parameter where the old cell has a type parameter
    "value_cell": "module value_cell #(parameter int W = 8) (input logic clk, input logic [W-1:0] d, "
    "output logic [W-1:0] q);\n  always_ff @(posedge clk) q <= d;\nendmodule\n",
    # no parameter at all
    "plain_cell": "module plain_cell (input logic clk, input logic [7:0] d, output logic [7:0] q);\n"
    "  always_ff @(posedge clk) q <= d;\nendmodule\n",
}

TOP = (
    "module top (input logic clk, input logic [7:0] d, output logic [7:0] q);\n"
    "  old_cell #(.T(byte_t)) u_c (.clk(clk), .d(d), .q(q));\n"
    "endmodule\n"
)


@pytest.fixture
def kit(tmp_path: Path):
    """The design: `top` instantiates `old_cell` with `.T(byte_t)`, and every cell is there."""
    root = tmp_path / "in"
    (root / "rtl").mkdir(parents=True)
    (root / "rtl" / "types.svh").write_bytes(TYPES.encode())
    for name, text in CELLS.items():
        (root / "rtl" / f"{name}.sv").write_bytes(f'`include "types.svh"\n{text}'.encode())
    (root / "rtl" / "top.sv").write_bytes(f'`include "types.svh"\n{TOP}'.encode())
    lines = "+incdir+rtl\n" + "".join(f"rtl/{n}.sv\n" for n in CELLS) + "rtl/top.sv\n"
    (root / "design.f").write_bytes(lines.encode())
    return root / "design.f"


@pytest.fixture
def vet(kit):
    return Vet(Session(kit, top="top"))


def swap(vet: Vet, module: str, **kw):
    return vet.module("top").instance("u_c").replace_module(module, was="old_cell", **kw)


# --- Y1 to Y3: the type a port resolves to -------------------------------------------


def test_the_same_type_parameter_under_the_same_name_is_accepted(vet):
    swap(vet, "new_cell")
    r = vet.finish()
    assert r.ok, r.report()


def test_the_same_type_parameter_under_another_name_is_accepted(vet):
    swap(vet, "renamed_cell", params={"T": "U"})
    r = vet.finish()
    assert r.ok, r.report()


def test_a_typedef_of_the_same_bits_is_accepted(vet):
    """`byte_cell` declares its ports `byte_t`, which is what the override gives the old cell."""
    swap(vet, "byte_cell", drop_params=["T"])
    r = vet.finish()
    assert r.ok, r.report()


def test_a_concrete_type_of_the_same_bits_is_accepted(vet):
    swap(vet, "plain_cell", drop_params=["T"])
    r = vet.finish()
    assert r.ok, r.report()


def test_two_state_against_four_state_is_still_refused(vet):
    with pytest.raises(VetError, match="bit") as e:
        swap(vet, "bit_cell", drop_params=["T"])
    assert "logic" in str(e.value)
    assert "'d'" in str(e.value)


# --- Y4 and Y5: the sign-off keeps comparing the spelling ----------------------------


def test_the_snapshot_keeps_the_spelling_and_adds_the_resolved_type(kit):
    """`port_types` is what slang spells, `port_resolved` is what the type resolves to."""
    snap = Session(kit, top="top").baseline_snapshot()["top.u_c"]
    assert snap.port_types["d"] == "old_cell.T"  # the parameter, not the type
    assert snap.port_resolved["d"] == "logic[7:0]"
    assert snap.port_types["clk"] == snap.port_resolved["clk"] == "logic"


def test_the_sign_off_reads_the_spelling_and_not_the_resolved_type():
    """Two typedefs of the same bits are one resolved type, so only the spelling tells them apart.

    This is why the resolved type goes beside `port_types` instead of replacing it. Were
    the snapshot itself made resolved, a port whose typedef changed would pass unreported.
    """

    def snap(spelled: str, resolved: str) -> InstanceSnap:
        return InstanceSnap(
            module="m",
            params={},
            ports={"a": ("input", 8)},
            connections={},
            port_types={"a": spelled},
            port_resolved={"a": resolved},
        )

    changed = diff_snapshots(
        {"t": snap("byte_t", "logic[7:0]")},
        {"t": snap("word_t", "logic[7:0]")},
    )
    assert [(c.kind, c.name) for c in changed] == [("port-type-changed", "a")]

    # The other way round: the sign-off does not read the resolved type at all.
    same = diff_snapshots(
        {"t": snap("byte_t", "logic[7:0]")},
        {"t": snap("byte_t", "bit[7:0]")},
    )
    assert same == []


def test_an_unpacked_dimension_is_part_of_the_resolved_type_too(tmp_path: Path):
    root = tmp_path / "u"
    root.mkdir()
    (root / "t.sv").write_bytes(b"module t (input logic arr [2], input logic [3:0] v);\nendmodule\n")
    (root / "t.f").write_bytes(b"t.sv\n")
    snap = Session(root / "t.f", top="t").baseline_snapshot()["t"]
    assert snap.port_resolved["arr"] != snap.port_resolved["v"]
    assert "[0:1]" in snap.port_resolved["arr"]


# --- Y6 to Y9: an override of a type parameter ---------------------------------------


def test_an_override_of_a_type_parameter_is_kept(vet):
    swap(vet, "new_cell")
    assert [(r.op, r.target, r.status) for r in vet.log] == [("replace_module", "top.u_c", "applied")]
    text = vet.session.text(vet.module("top").path).decode()
    assert ".T(byte_t)" in text, text


def test_a_type_parameter_override_can_be_renamed(vet):
    swap(vet, "renamed_cell", params={"T": "U"})
    text = vet.session.text(vet.module("top").path).decode()
    assert ".U(byte_t)" in text, text


def test_an_override_that_lands_on_nothing_is_still_refused(vet):
    with pytest.raises(VetError, match="not accounted for") as e:
        swap(vet, "plain_cell")
    message = str(e.value)
    assert "'T'" in message
    # The old message sent the reader to params=, which refuses the same way, and to
    # drop_params=, which changes the design. Neither is a remedy for a type parameter.
    assert "rename it with params=" not in message, message


def test_a_type_parameter_cannot_be_renamed_onto_a_value_parameter(vet):
    with pytest.raises(VetError, match="'W'") as e:
        swap(vet, "value_cell", params={"T": "W"})
    assert "type" in str(e.value)
