"""A hierarchical operation through an instance that the run replaced (#149).

`Vet.punch_port` and `Vet.propagate_param` resolve their path against the design of the start of
the run. After `Instance.replace_module` the instance has another module, and the two operations
still edited the old one. The sign-off failed, so there was no wrong "ok", but the refusal came
late and its message was a compiler error about a port. Measured on `main` at f90ffd2:

    vet.module("mid").instance("u_leaf").replace_module("leaf3", waiver="w")
    vet.punch_port("pin", "input", "top.u_mid.u_leaf")      accepted, `pin` was added to `leaf`

Owner decision of 2026-11-27: refuse at the call.

RULES

W1  `punch_port` and `propagate_param` raise `VetError` when their path ends at, or passes through,
    an instance whose module was replaced in this session. The message starts with the operation,
    names the instance and its parent module, and says `replace_module`.
W2  The refused call changes nothing: no record is added to the log, and no file is edited.
W3  A path that passes no replaced instance works as before, also after a replacement elsewhere.
W4  The refusal holds in a session that was saved after the replacement and restored.

STATED LIMITS

- The refusal also holds after a replacement back to the first module. The path would be right
  again, but the tool does not look. Do the hierarchical operation in another run.
- `vet.module(name, instances=N)` still counts the instances of the design of the start of the
  run. After a replacement the count that a script must give is the old one. Not changed here.
- A script that needs both operations on one instance does the hierarchical one first, or is
  split into two runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet import equiv
from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session

LEAF = "module {name} #(parameter int N = 1) (input logic a, output logic y);\n  assign y = a;\nendmodule\n"
FILES = {
    "leaf.sv": LEAF.format(name="leaf"),
    "leaf3.sv": LEAF.format(name="leaf3"),
    "mid.sv": "module mid (input logic a, output logic y);\n  leaf u_leaf (.a(a), .y(y));\nendmodule\n",
    "mid3.sv": "module mid3 (input logic a, output logic y);\n  leaf u_leaf (.a(a), .y(y));\nendmodule\n",
    "side.sv": (
        "module side #(parameter int N = 1) (input logic a, output logic y);\n"
        "  leaf3 u_three (.a(a), .y(y));\nendmodule\n"
    ),
    "top.sv": (
        "module top (input logic a, output logic y, output logic z);\n"
        "  mid u_mid (.a(a), .y(y));\n  side u_side (.a(a), .y(z));\nendmodule\n"
    ),
}
WHY = "the reviewer looked at it"


@pytest.fixture(autouse=True)
def no_yosys(monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "design"
    (root / "rtl").mkdir(parents=True)
    for name, text in FILES.items():
        (root / "rtl" / name).write_bytes(text.encode())
    (root / "design.f").write_bytes("".join(f"rtl/{name}\n" for name in FILES).encode())
    return root


@pytest.fixture
def vet(root: Path) -> Vet:
    return Vet(Session(root / "design.f", top="top"))


def punch(vet: Vet, to: str) -> None:
    vet.punch_port("pin", "input", to)


def propagate(vet: Vet, to: str) -> None:
    vet.propagate_param("N", to)


CALLS = {"punch_port": punch, "propagate_param": propagate}


def refused(vet: Vet, op: str, to: str, instance: str, parent: str) -> None:
    before = [(r.op, r.target) for r in vet.log]
    texts = {p: vet.session.text(p) for p in vet.session.files}
    with pytest.raises(VetError) as caught:
        CALLS[op](vet, to)
    message = str(caught.value)
    assert message.startswith(op), message
    assert repr(instance) in message and repr(parent) in message and "replace_module" in message, message
    assert [(r.op, r.target) for r in vet.log] == before
    assert {p: vet.session.text(p) for p in vet.session.files} == texts


# --- W1, W2 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("op", CALLS)
def test_a_path_that_ends_at_a_replaced_instance_is_refused(vet, op):
    vet.module("mid").instance("u_leaf").replace_module("leaf3", waiver=WHY)
    refused(vet, op, "top.u_mid.u_leaf", "u_leaf", "mid")


@pytest.mark.parametrize("op", CALLS)
def test_a_path_that_passes_through_a_replaced_instance_is_refused(vet, op):
    vet.module("top").instance("u_mid").replace_module("mid3", waiver=WHY)
    refused(vet, op, "top.u_mid.u_leaf", "u_mid", "top")


@pytest.mark.parametrize("op", CALLS)
def test_the_refusal_holds_after_a_replacement_back(vet, op):
    """Stated limit."""
    vet.module("mid").instance("u_leaf").replace_module("leaf3", waiver=WHY)
    vet.module("mid").instance("u_leaf").replace_module("leaf", waiver=WHY)
    refused(vet, op, "top.u_mid.u_leaf", "u_leaf", "mid")


# --- W3 -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("op", CALLS)
def test_a_path_beside_a_replaced_instance_works_as_before(vet, op):
    vet.module("mid").instance("u_leaf").replace_module("leaf3", waiver=WHY)
    CALLS[op](vet, "top.u_side")
    targets = [r.target for r in vet.log]
    assert "top.u_side" in targets, targets


@pytest.mark.parametrize("op", CALLS)
def test_a_path_with_no_replacement_works_as_before(vet, op):
    CALLS[op](vet, "top.u_mid.u_leaf")
    targets = [r.target for r in vet.log]
    assert "mid.u_leaf" in targets, targets


# --- W4 -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("op", CALLS)
def test_the_refusal_holds_in_a_restored_session(vet, tmp_path, op):
    vet.module("mid").instance("u_leaf").replace_module("leaf3", waiver=WHY)
    vet.save(tmp_path / "saved.vet")
    again = Vet.restore(tmp_path / "saved.vet")
    refused(again, op, "top.u_mid.u_leaf", "u_leaf", "mid")
