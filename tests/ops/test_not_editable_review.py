"""Review of the refusal on a file that is not editable: the operations of several files.

The review ran about 110 calls in 6 sets of editable files, against the branch and against the code
before. Nothing changed after a refusal, no good call was refused, and nothing but a `VetError`
came out. One finding, on the message:

E5  `Vet.punch_port` and `Vet.propagate_param` edit several files, through one plan for each. The
    first form of the fix named the target of the first plan that could not be made, a module or
    an instance (`punch_port top: `, `punch_port top.u_mid: `). The other refusals of these two
    operations name the port or the parameter (`punch_port pin: `, `propagate_param W: `). The
    refusal on a file that is not editable does the same.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet import equiv
from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session

LEAF = "module leaf #(parameter int W = 1) (input logic a, output logic y);\n  assign y = a;\nendmodule\n"
MID = "module mid (input logic a, output logic y);\n  leaf u_leaf (.a(a), .y(y));\nendmodule\n"
TOP = "module top (input logic a, output logic y);\n  mid u_mid (.a(a), .y(y));\nendmodule\n"
FILES = {"leaf.sv": LEAF, "mid.sv": MID, "top.sv": TOP}
DEEP = "top.u_mid.u_leaf"


@pytest.fixture(autouse=True)
def no_yosys(monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)


def build(tmp_path: Path, locked: str | None) -> Vet:
    root = tmp_path / "design"
    (root / "rtl").mkdir(parents=True)
    for name, text in FILES.items():
        (root / "rtl" / name).write_bytes(text.encode())
    (root / "design.f").write_bytes("".join(f"rtl/{name}\n" for name in FILES).encode())
    editable = [root / "rtl" / name for name in FILES if name != locked]
    return Vet(Session(root / "design.f", top="top", editable=editable))


CALLS = [
    ("punch_port pin: ", lambda v: v.punch_port("pin", "input", DEEP)),
    ("propagate_param W: ", lambda v: v.propagate_param("W", DEEP)),
]
IDS = ["punch_port", "propagate_param"]


@pytest.mark.parametrize(("start", "call"), CALLS, ids=IDS)
def test_the_call_is_good_when_every_file_is_editable(tmp_path, start, call):
    vet = build(tmp_path, None)
    call(vet)
    assert vet.log, "the call made no record"
    assert {r.status for r in vet.log} == {"applied"}


# `propagate_param` does not edit `leaf.sv`: the leaf has the parameter already.
LOCKED = [
    (*CALLS[0], "top.sv"),
    (*CALLS[0], "mid.sv"),
    (*CALLS[0], "leaf.sv"),
    (*CALLS[1], "top.sv"),
    (*CALLS[1], "mid.sv"),
]


@pytest.mark.parametrize(("start", "call", "locked"), LOCKED, ids=[f"{s.split()[0]}-{f}" for s, _, f in LOCKED])
def test_the_message_names_the_port_or_the_parameter(tmp_path, start, call, locked):
    vet = build(tmp_path, locked)
    with pytest.raises(VetError) as caught:
        call(vet)
    message = str(caught.value)
    assert message.startswith(start), message
    assert locked in message and "not editable" in message, message
    assert list(vet.log) == []
    result = vet.finish()
    assert result.ok, result.report()
    assert result.files == {}
