"""The three removals and the name they are given (#103).

`Module.remove_wire`, `Module.remove_port` and `Module.remove_param` never checked the
name. Anything they did not find was reported as `skipped`, which says "the design has no
such thing". That is wrong for two kinds of name. One is a name that cannot be an
identifier at all, where `skipped` hides a mistake in the script. The other is the escaped
spelling of a name the design does have, where `skipped` is simply untrue: IEEE 1800
section 5.6.1 says an escaped identifier made only of simple-identifier characters **is**
that simple identifier, so `\\helper ` is `helper`.

The additions have been strict about this all along, so the pair was asymmetric:

    add_wire('\\helper ')    -> VetError: '\\helper ' is not a simple identifier ...
    remove_wire('\\helper ') -> [('remove_wire', 'skipped')]      <- the wire is still there

Measured on this design before the part, with `logic helper;`, a port `spare` and a
parameter `P` all present and used:

    remove_wire   '\\helper '     -> [('remove_wire', 'skipped')]
    remove_wire   'not a name!!' -> [('remove_wire', 'skipped')]
    remove_wire   ''             -> [('remove_wire', 'skipped')]
    remove_port   '\\spare '      -> [('remove_port', 'skipped')]
    remove_param  '\\P '          -> [('remove_param', 'skipped')]

The empty string is not in the issue text. It behaves the same way and is covered here.

Owner decision, taken from the issue: refuse the name, do not normalise it. The operations
take a name as the design spells it, and a silent rewrite would hide a real mismatch
between the script and the design.

Rules
    M1  Each of the three removals refuses a name that is not a simple identifier. The
        refusal is a VetError that starts with the operation and the module, and it uses
        the words the additions already use, so the two sides of the pair read alike.
    M2  The escaped spelling of a name the design holds is refused, not skipped.
    M3  The empty string, a name with a space or a punctuation mark, a name that starts
        with a digit, and a SystemVerilog keyword are all refused.
    M4  A valid name that the design does not hold is still `skipped`. That is what
        `skipped` is for, and this part does not change it.
    M5  A valid name that the design does hold still works as before.
    M6  The refusal happens when the operation is called. Nothing is queued in `vet.log`
        and no file changes.

Not checked
    Whether any other operation reports `skipped` for a name it should refuse. The three
    removals are the ones the issue measured. `Instance` operations take an instance name
    and are out of scope here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session

TOP = (
    "module top #(parameter int P = 1) (input logic a, input logic spare, output logic y);\n"
    "  logic helper;\n"
    "  logic loose;\n"
    "  assign helper = a & spare;\n"
    "  assign y = helper ^ P[0];\n"
    "endmodule\n"
)

# every name that must be refused, and why it is not a simple identifier
BAD = [
    "\\helper ",
    "\\spare ",
    "\\P ",
    "",
    "not a name!!",
    "1bad",
    "has space",
    "module",
    "logic",
]

OPS = ["remove_wire", "remove_port", "remove_param"]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "top.sv").write_bytes(TOP.encode())
    fl = tmp_path / "design.f"
    fl.write_bytes(b"rtl/top.sv\n")
    return fl


@pytest.fixture
def vet(root: Path) -> Vet:
    return Vet(Session(root, top="top"))


# --- M1, M2, M3: every bad name is refused by every removal --------------------------


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("name", BAD)
def test_a_name_that_is_not_a_simple_identifier_is_refused(vet, op, name):
    with pytest.raises(VetError, match="simple identifier") as e:
        getattr(vet.module("top"), op)(name)
    assert str(e.value).startswith(f"{op} top: "), str(e.value)
    assert repr(name) in str(e.value), str(e.value)


# --- M4: a valid name the design does not hold is still skipped ----------------------


@pytest.mark.parametrize("op", OPS)
def test_a_valid_name_that_is_absent_is_still_skipped(vet, op):
    getattr(vet.module("top"), op)("nosuchname")
    assert [(r.op, r.status) for r in vet.log] == [(op, "skipped")]


# --- M5: a valid name the design does hold still works -------------------------------


def test_a_wire_the_design_holds_is_still_removed(vet):
    """`loose` is declared and nothing reads it, so the removal is clean."""
    vet.module("top").remove_wire("loose")
    assert [(r.op, r.status) for r in vet.log] == [("remove_wire", "applied")]


# --- M6: nothing is queued and nothing is written -------------------------------------


@pytest.mark.parametrize("op", OPS)
def test_nothing_is_queued_when_the_name_is_refused(vet, op, root):
    before = root.parent.joinpath("rtl", "top.sv").read_bytes()
    with pytest.raises(VetError):
        getattr(vet.module("top"), op)("\\helper ")
    assert vet.log == []
    vet.session.refresh()
    assert root.parent.joinpath("rtl", "top.sv").read_bytes() == before
