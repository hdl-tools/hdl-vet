"""An operation on a file that is not editable in this session.

A session can be given the files that it may edit (`Session(..., editable=...)`). An operation
that would edit another file of the design is refused. A review of #57 saw that the refusal has
two forms, and neither is the one that the project asks for. Measured on `main` at f90ffd2, with
`top.sv` not editable:

    top.instance("u").connect("s", "a", was="b")
        SessionError: <path>/top.sv: is not editable in this session
    top.add_wire("n"), top.add_port("p", "input"), top.add_param("P", 1)
        SessionError, the same text
    top.instance("u").replace_module("leaf2", waiver=...)
        VetError: replace_module: cannot edit <path>/top.sv: not editable in this session
    top.remove_instance("u"), top.remove_assign("w")
        VetError, the same form

In every case nothing changed. The error is of the wrong kind in the first form, and the message
has no target in both.

RULES

E1  An operation that would edit a file that is not editable raises `VetError`. It never raises
    `SessionError`: that is the error of the session, for a call of the session.
E2  The message starts with the operation and its target, `connect top.u: ` or `add_wire top: `,
    as every refusal does. It names the file and says that it is not editable.
E3  Nothing changes: no record, and the sign-off is ok with no file.
E4  An operation on a file that is editable is not touched by this.

STATED LIMIT

- The rules are tested for the operations of one module and of one instance. An operation that
  edits several files (`Vet.punch_port`, `Vet.propagate_param`, `Module.override_net` with an
  enable that is carried up) refuses when one of them is not editable, with the same kind of
  error. Which of the files its message names first is not fixed.
- `Module.override_net` and `Module.insert_cell_on_net` are not tested here. They need an enable
  that the run added to the module, and that addition is refused first when the file is not
  editable. If their own refusal is reached, it names the module and the net after the
  operation (`override_net core.net: `), where their other refusals name the module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet import equiv
from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session

LEAF = (
    "module {name} #(parameter int W = 1) (input logic a, input logic s, output logic y);\n  assign y = a;\nendmodule\n"
)
TOP = (
    "module top #(parameter int UNUSED = 0) (input logic a, input logic b, input logic spare_i, output logic y);\n"
    "  logic w;\n"
    "  logic open;\n"
    "  logic idle;\n"
    "  assign w = b;\n"
    "  logic dbg;\n"
    "  assign dbg = a;\n"
    "  leaf u (.a(a), .s(w), .y(y));\n"
    "  leaf v (.a(a), .s(w), .y());\n"
    "endmodule\n"
)
WHY = "the reviewer looked at it"


@pytest.fixture(autouse=True)
def no_yosys(monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)


def build(tmp_path: Path, editable: list[str]) -> Vet:
    root = tmp_path / "design"
    (root / "rtl").mkdir(parents=True)
    for name in ("leaf", "leaf2"):
        (root / "rtl" / f"{name}.sv").write_bytes(LEAF.format(name=name).encode())
    (root / "rtl" / "top.sv").write_bytes(TOP.encode())
    (root / "design.f").write_bytes(b"rtl/leaf.sv\nrtl/leaf2.sv\nrtl/top.sv\n")
    return Vet(Session(root / "design.f", top="top", editable=[root / "rtl" / name for name in editable]))


def top(vet: Vet):
    return vet.module("top")


def u(vet: Vet):
    return top(vet).instance("u")


# (operation, target, the call). Each one is accepted when `top.sv` is editable.
CALLS = [
    ("add_wire", "top", lambda v: top(v).add_wire("n")),
    ("remove_wire", "top", lambda v: top(v).remove_wire("idle")),
    ("add_assign", "top", lambda v: top(v).add_assign("open", "a", waiver=WHY)),
    ("remove_assign", "top", lambda v: top(v).remove_assign("dbg")),
    ("add_port", "top", lambda v: top(v).add_port("p", "input")),
    ("remove_port", "top", lambda v: top(v).remove_port("spare_i")),
    ("add_param", "top", lambda v: top(v).add_param("P", 1)),
    ("remove_param", "top", lambda v: top(v).remove_param("UNUSED")),
    ("remove_instance", "top.v", lambda v: top(v).remove_instance("v")),
    ("connect", "top.u", lambda v: u(v).connect("s", "a", was="w", waiver=WHY)),
    ("set_param", "top.u", lambda v: u(v).set_param("W", 2, waiver=WHY)),
    ("replace_module", "top.u", lambda v: u(v).replace_module("leaf2", waiver=WHY)),
]
IDS = [op for op, _, _ in CALLS]


@pytest.mark.parametrize(("op", "target", "call"), CALLS, ids=IDS)
def test_the_call_is_good_when_the_file_is_editable(tmp_path, op, target, call):
    """The fixture is right: each call is applied when `top.sv` may be edited."""
    vet = build(tmp_path, ["leaf.sv", "leaf2.sv", "top.sv"])
    call(vet)
    assert [(r.op, r.status) for r in vet.log] == [(op, "applied")]


@pytest.mark.parametrize(("op", "target", "call"), CALLS, ids=IDS)
def test_an_operation_on_a_file_that_is_not_editable_is_refused(tmp_path, op, target, call):
    vet = build(tmp_path, ["leaf.sv", "leaf2.sv"])
    with pytest.raises(VetError) as caught:
        call(vet)
    message = str(caught.value)
    assert message.startswith(f"{op} {target}: "), message
    assert "top.sv" in message and "not editable" in message, message
    assert list(vet.log) == []
    result = vet.finish()
    assert result.ok, result.report()
    assert result.files == {}


def test_a_file_that_is_editable_is_edited(tmp_path):
    vet = build(tmp_path, ["leaf.sv", "leaf2.sv"])
    vet.module("leaf", instances=2).add_wire("n")
    result = vet.finish()
    assert result.ok, result.report()
    assert [p.name for p in result.files] == ["leaf.sv"]
