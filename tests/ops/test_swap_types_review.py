"""Review of the resolved port type of `replace_module` (#59).

The review attacked the switch from `port_types`, the type as slang spells it, to
`port_resolved`, the type behind the aliases. The switch was meant to be a pure loosening:
two modules that agree on every bit should not be refused for spelling a type differently.
It went wrong in both directions, because `str(canonicalType)` is a printed form and not a
key.

`str(canonicalType)` of an **enum does not name its base type**. A four-state enum and a
two-state enum with the same members print alike, so a swap that changes four-state
hardware to two-state was accepted and the sign-off said `ok`. That is a wrong `ok`, the
worst result in this project. It was also a regression: the two spell differently, so the
comparison on `port_types` refused it.

`str(canonicalType)` of an **unnamed struct or union carries a number**, `s$3`, which slang
counts per compilation. The number moves with the set of elaborated types. The same typedef
out of one shared header, or one package, printed `s$1` on one side and `s$2` on the other,
so an identical type was refused with a message that printed the same type twice. That was
a regression too: both sides spell the type `s_t`, so the old comparison accepted it.

So the comparison key is the printed form with the per-compilation number removed, and with
the state count added, because the printed form hides it.

Rules
    Z1  Two enums with the same members and a different base state are not the same type.
        A swap between them is refused, and nothing downstream could catch it: Yosys has no
        four-state value, so the equivalence check cannot see it either.
    Z2  The same typedef of a packed struct, on both sides, out of one shared header, is
        the same type. The swap is accepted.
    Z3  The same typedef out of one package is the same type, whatever else the design
        elaborates. The number slang gives an unnamed type is not part of the key.
    Z4  Two packed structs with different fields are still different types.
    Z5  An unpacked dimension survives the key. `logic$[0:1]` carries a `$` that is not a
        number, and removing the number must not remove the dimension.
    Z6  A refusal does not promise a default that does not exist. When the new module has
        no type parameter of that name, dropping the override does not take a type "back to
        the default"; the new module's own port types apply.

Not checked
    Whether the number slang gives an unnamed type is stable across pyslang versions. The
    pinned version is the one these rules were written against.
    Signedness. Every canonical form measured shows it, `logic signed[7:0]` and `int` among
    them, and a signed enum base prints its members as `2'sd0`. The key rests on the printed
    form for that one fact.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session
from no_comparison import no_comparison  # noqa: F401

ENUMS = {
    # The old cell takes its output type from a type parameter, which `top` sets to its own
    # four-state `e_t`. The new cell declares a two-state `e_t` with the same members. Both
    # outputs land on a plain vector in `top`, so nothing else objects.
    "old_e.sv": "module old_e #(parameter type T = logic) (input logic clk, input logic [1:0] d, output T q);\n"
    "  always_ff @(posedge clk) q <= T'(d);\nendmodule\n",
    "new_e.sv": "typedef enum bit [1:0] {A, B} e_t;\n"
    "module new_e (input logic clk, input logic [1:0] d, output e_t q);\n"
    "  always_ff @(posedge clk) q <= e_t'(d);\nendmodule\n",
    "four_e.sv": "typedef enum logic [1:0] {A, B} e_t;\n"
    "module four_e (input logic clk, input logic [1:0] d, output e_t q);\n"
    "  always_ff @(posedge clk) q <= e_t'(d);\nendmodule\n",
    "top.sv": "typedef enum logic [1:0] {A, B} e_t;\n"
    "module top (input logic clk, input logic [1:0] d, output logic [1:0] q);\n"
    "  old_e #(.T(e_t)) u_c (.clk(clk), .d(d), .q(q));\nendmodule\n",
}

HEADER = "typedef struct packed { logic [3:0] a; logic [3:0] b; } s_t;\n"
OTHER_HEADER = "typedef struct packed { logic [1:0] a; logic [5:0] b; } s_t;\n"

STRUCTS = {
    "old_s.sv": '`include "types.svh"\nmodule old_s (input logic clk, input s_t d, output s_t q);\n'
    "  always_ff @(posedge clk) q <= d;\nendmodule\n",
    "new_s.sv": '`include "types.svh"\nmodule new_s (input logic clk, input s_t d, output s_t q);\n'
    "  always_ff @(posedge clk) q <= d;\nendmodule\n",
    "odd_s.sv": '`include "other.svh"\nmodule odd_s (input logic clk, input s_t d, output s_t q);\n'
    "  always_ff @(posedge clk) q <= d;\nendmodule\n",
    "top.sv": '`include "types.svh"\nmodule top (input logic clk, input s_t d, output s_t q);\n'
    "  old_s u_c (.clk(clk), .d(d), .q(q));\nendmodule\n",
}

PKG = "package pkg;\n  typedef struct packed { logic [3:0] a; logic [3:0] b; } s_t;\nendpackage\n"
PKG_CELLS = {
    "old_p.sv": "module old_p (input logic clk, input pkg::s_t d, output pkg::s_t q);\n"
    "  always_ff @(posedge clk) q <= d;\nendmodule\n",
    # three unnamed types of its own, so slang's counter is at another place here
    "new_p.sv": "module new_p (input logic clk, input pkg::s_t d, output pkg::s_t q);\n"
    "  typedef struct packed { logic x; } f1_t;\n"
    "  typedef struct packed { logic y; } f2_t;\n"
    "  typedef struct packed { logic z; } f3_t;\n"
    "  f1_t u1; f2_t u2; f3_t u3;\n"
    "  always_ff @(posedge clk) q <= d;\nendmodule\n",
    "top.sv": "module top (input logic clk, input pkg::s_t d, output pkg::s_t q);\n"
    "  old_p u_c (.clk(clk), .d(d), .q(q));\nendmodule\n",
}


def design(root: Path, files: dict[str, str], headers: dict[str, str], order: list[str]) -> Path:
    rtl = root / "rtl"
    rtl.mkdir(parents=True)
    for name, text in {**files, **headers}.items():
        (rtl / name).write_bytes(text.encode())
    lines = "+incdir+rtl\n" + "".join(f"rtl/{n}\n" for n in order)
    (root / "d.f").write_bytes(lines.encode())
    return root / "d.f"


def swap(filelist: Path, module: str, was: str, **kw) -> str:
    v = Vet(Session(filelist, top="top"))
    try:
        v.module("top").instance("u_c").replace_module(module, was=was, **kw)
    except VetError as e:
        return f"refused: {e}"
    r = v.finish()
    return "ok" if r.ok else f"sign-off failed: {r.report()}"


# --- Z1: an enum carries its base state ----------------------------------------------


def test_a_four_state_enum_is_not_a_two_state_enum(tmp_path: Path):
    """The members match, so the printed canonical form matches. The hardware does not."""
    f = design(tmp_path / "e", ENUMS, {}, ["old_e.sv", "new_e.sv", "four_e.sv", "top.sv"])
    out = swap(f, "new_e", was="old_e", drop_params=["T"])
    assert out.startswith("refused:"), out
    assert "'q'" in out


def test_a_four_state_enum_of_the_same_members_is_accepted(tmp_path: Path):
    """The counterpart of Z1: the same base state is the same type, so the swap goes through."""
    f = design(tmp_path / "e", ENUMS, {}, ["old_e.sv", "new_e.sv", "four_e.sv", "top.sv"])
    assert swap(f, "four_e", was="old_e", drop_params=["T"]) == "ok"


# --- Z2 and Z3: one type, two compilations -------------------------------------------


def test_the_same_struct_typedef_from_a_shared_header_is_one_type(tmp_path: Path):
    f = design(
        tmp_path / "s",
        STRUCTS,
        {"types.svh": HEADER, "other.svh": OTHER_HEADER},
        ["old_s.sv", "new_s.sv", "odd_s.sv", "top.sv"],
    )
    assert swap(f, "new_s", was="old_s") == "ok"


def test_the_same_struct_typedef_from_a_package_is_one_type(tmp_path: Path):
    f = design(tmp_path / "p", PKG_CELLS, {"pkg.sv": PKG}, ["pkg.sv", "old_p.sv", "new_p.sv", "top.sv"])
    assert swap(f, "new_p", was="old_p") == "ok"


def test_the_package_order_does_not_decide_the_answer(tmp_path: Path):
    """The package last, which moves slang's counter for every unnamed type after it."""
    f = design(tmp_path / "p", PKG_CELLS, {"pkg.sv": PKG}, ["old_p.sv", "new_p.sv", "top.sv", "pkg.sv"])
    assert swap(f, "new_p", was="old_p") == "ok"


# --- Z4 and Z5: what the key must keep -----------------------------------------------


def test_two_structs_with_different_fields_are_different_types(tmp_path: Path):
    f = design(
        tmp_path / "s",
        STRUCTS,
        {"types.svh": HEADER, "other.svh": OTHER_HEADER},
        ["old_s.sv", "new_s.sv", "odd_s.sv", "top.sv"],
    )
    out = swap(f, "odd_s", was="old_s")
    assert out.startswith("refused:"), out


def test_an_unpacked_dimension_survives_the_key(tmp_path: Path):
    """`logic$[0:1]` holds a `$` that is not a number. Removing the number must keep it."""
    root = tmp_path / "u"
    root.mkdir()
    (root / "t.sv").write_bytes(b"module t (input logic arr [2], input logic [1:0] v);\nendmodule\n")
    (root / "t.f").write_bytes(b"t.sv\n")
    snap = Session(root / "t.f", top="t").baseline_snapshot()["t"]
    assert snap.port_resolved["arr"] != snap.port_resolved["v"]
    assert "[0:1]" in snap.port_resolved["arr"]


# --- Z6: a refusal that promises nothing false ---------------------------------------


def test_a_module_without_the_type_parameter_is_not_said_to_have_a_default(tmp_path: Path):
    f = design(tmp_path / "e", ENUMS, {}, ["old_e.sv", "new_e.sv", "four_e.sv", "top.sv"])
    out = swap(f, "four_e", was="old_e")  # the override of 'T' lands on nothing
    assert out.startswith("refused:"), out
    assert "'T'" in out
    assert "back to the default" not in out, out


@pytest.mark.parametrize("name", ["new_e", "four_e"])
def test_the_remedy_a_refusal_names_works(tmp_path: Path, name: str):
    """Whatever the refusal says to do about the override, doing it must get past that refusal."""
    f = design(tmp_path / "e", ENUMS, {}, ["old_e.sv", "new_e.sv", "four_e.sv", "top.sv"])
    out = swap(f, name, was="old_e")
    assert "drop_params=" in out, out
    after = swap(f, name, was="old_e", drop_params=["T"])
    assert "not accounted for" not in after, after
