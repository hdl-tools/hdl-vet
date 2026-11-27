"""Stated limits of the calls that had no test (#106, the second batch).

The survey of 2026-11-27 listed the claims of the chapter on limits that no test asserts. This file
takes seven from the items "Arguments that are checked late or not at all", "Paths after a
replacement" and the list of what the tool does not edit. Each test shows that the limit is still
a limit. A test that fails means that the limit is gone or is stated wrongly, and the chapter has
to change with it.

T1  An argument whose `repr` or `str` raises gives that Python error, and no `VetError`.
T2  A type that is a text is not parsed at the call. `logic x1; logic` for `add_wire` is taken,
    and the sign-off fails.
T3  A value that is a text is not parsed at the call. `8;` for `set_param` is taken, and the
    sign-off fails.
T4  The kind of `Vet.expect` and of `Vet.permit` is not compared with the kinds that exist: a
    kind that is spelled wrong is taken.
T5  An instance with a positional parameter override is not edited: `set_param` refuses it.
T6  After `replace_module`, `punch_port` through that instance still walks the design of the
    start of the run. It adds the port to the old module, and the sign-off fails (#149).
T7  A name that text disabled by a conditional directive only reads is refused as a new name,
    as a name that it declares is: every identifier of the disabled text counts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet import equiv
from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session

LEAF = (
    "module {name} #(parameter int N = 1, parameter type T = logic) (input logic a, output logic y);\n"
    "  assign y = a;\nendmodule\n"
)
TOP = "module top (input logic a, output logic y);\n  leaf u (.a(a), .y(y));\nendmodule\n"
WHY = "the reviewer looked at it"


@pytest.fixture(autouse=True)
def no_yosys(monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)


def build(tmp_path: Path, top_text: str = TOP) -> Vet:
    root = tmp_path / "design"
    (root / "rtl").mkdir(parents=True)
    files = {"leaf.sv": LEAF.format(name="leaf"), "leaf3.sv": LEAF.format(name="leaf3"), "top.sv": top_text}
    for name, text in files.items():
        (root / "rtl" / name).write_bytes(text.encode())
    (root / "design.f").write_bytes("".join(f"rtl/{name}\n" for name in files).encode())
    return Vet(Session(root / "design.f", top="top"))


@pytest.fixture
def vet(tmp_path: Path) -> Vet:
    return build(tmp_path)


class Bad:
    def __repr__(self) -> str:
        raise RuntimeError("repr raises")

    __str__ = __repr__


def test_an_argument_whose_repr_raises_gives_that_error(vet):
    with pytest.raises(RuntimeError, match="repr raises") as caught:
        vet.module("leaf").add_wire("w3", Bad())
    assert not isinstance(caught.value, VetError)


def test_a_type_text_is_caught_only_at_the_sign_off(vet):
    vet.module("leaf").add_wire("w4", None, type="logic x1; logic")
    assert [(r.op, r.status) for r in vet.log] == [("add_wire", "applied")]
    assert not vet.finish().ok


def test_a_value_text_is_caught_only_at_the_sign_off(vet):
    vet.module("top").instance("u").set_param("N", "8;", waiver=WHY)
    assert [(r.op, r.status) for r in vet.log] == [("set_param", "applied")]
    assert not vet.finish().ok


@pytest.mark.parametrize("call", ["expect", "permit"])
def test_a_kind_that_is_spelled_wrong_is_taken(vet, call):
    getattr(vet, call)("prot-added", "top", "x")


def test_a_positional_parameter_override_is_not_edited(tmp_path):
    vet = build(tmp_path, TOP.replace("leaf u", "leaf #(2) u"))
    with pytest.raises(VetError) as caught:
        vet.module("top").instance("u").set_param("N", 3, waiver=WHY)
    message = str(caught.value)
    assert message.startswith("set_param top.u: "), message
    assert "positional" in message, message
    assert list(vet.log) == []


def test_a_punch_after_a_replacement_edits_the_old_module(vet):
    vet.module("top").instance("u").replace_module("leaf3", waiver=WHY)
    vet.punch_port("pin", "input", "top.u")
    assert ("add_port", "leaf") in [(r.op, r.target) for r in vet.log]
    assert ("add_port", "leaf3") not in [(r.op, r.target) for r in vet.log]
    result = vet.finish()
    assert not result.ok
    assert "leaf.sv" in [p.name for p in result.files]


DISABLED = (
    "module top (input logic a, output logic y);\n"
    "`ifdef OTHER_CONFIG\n  logic declared;\n  assign y = only_read;\n`endif\n"
    "  leaf u (.a(a), .y(y));\nendmodule\n"
)


@pytest.mark.parametrize("name", ["declared", "only_read"])
def test_a_name_of_disabled_text_is_refused_as_a_new_name(tmp_path, name):
    vet = build(tmp_path, DISABLED)
    with pytest.raises(VetError) as caught:
        vet.module("top").add_wire(name)
    message = str(caught.value)
    assert message.startswith("add_wire top: "), message
    assert name in message, message
    assert list(vet.log) == []
