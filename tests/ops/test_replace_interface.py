"""`replace_module` and an interface port that is written with no modport (#141).

`Instance.replace_module` refuses when either module has an interface port (the docstring of
`tests/ops/test_instances.py`, and the chapter on limits). It found such a port by its direction.
A port `ifc i`, an interface with no modport, is written like a port of a named type, and the
module information gave it the direction `input`. So the refusal did not fire, and the comparison
of the two port lists took the interface for an input of one bit. Measured on `main` at 34b7b59:

    u.replace_module("leaf2", waiver="w")   -> accepted, ok=True, module-changed top.u

`Vet.punch_port` and `Instance.disconnect` know such a port: they ask the elaborated design.

This part makes the refusal fire, as it is specified. Whether a swap of a module with an interface
port is supported one day is the open question of #141 and of #60. It is not decided here.

RULES

F1  `replace_module` refuses when the module that the instance has, or the module that it gets,
    has a port whose type is an interface, with or without a modport: `ifc i`, `ifc.mp i`,
    `interface i`, and an array of them.
F2  It is told from the design, not from the spelling: the name of the type is an interface of
    the design. The new module need not be instantiated anywhere.
F3  A port of a named type that is no interface (a typedef of a package, a struct) is not
    refused.
F4  The message starts with `replace_module <instance>: `, and names the module and the port.
F5  The refusal comes before anything changes: no record, and the sign-off has no change.
F6  A waiver does not take the refusal away.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet import equiv
from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session

IFC = "interface ifc;\n  logic v;\n  modport mp (input v);\nendinterface\n"
PKG = "package pkg;\n  typedef logic [1:0] two_t;\n  typedef struct packed { logic a; logic b; } pair_t;\nendpackage\n"

# The port `i` of each leaf, by the name of the form.
FORMS = {
    "plain": "ifc i",
    "modport": "ifc.mp i",
    "generic": "interface i",
    "array": "ifc i [2]",
}
USE = {
    "plain": "i.v",
    "modport": "i.v",
    "generic": "i.v",
    "array": "i[0].v",
}


def leaf(name: str, form: str) -> str:
    return f"module {name} ({FORMS[form]}, input logic a, output logic y);\n  assign y = a & {USE[form]};\nendmodule\n"


def top(form: str) -> str:
    bus = "ifc b [2] ();" if form == "array" else "ifc b ();"
    return f"module top (input logic a, output logic y);\n  {bus}\n  leaf u (.i(b), .a(a), .y(y));\nendmodule\n"


PLAIN_LEAF = "module {name} (input logic i, input logic a, output logic y);\n  assign y = a & i;\nendmodule\n"
PLAIN_TOP = (
    "module top (input logic a, output logic y);\n  logic b;\n  assign b = 1'b1;\n"
    "  leaf u (.i(b), .a(a), .y(y));\nendmodule\n"
)


@pytest.fixture(autouse=True)
def no_yosys(monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)


def build(tmp_path: Path, files: dict[str, str]) -> Vet:
    root = tmp_path / "design"
    (root / "rtl").mkdir(parents=True)
    for name, text in files.items():
        (root / "rtl" / name).write_bytes(text.encode())
    (root / "design.f").write_bytes("".join(f"rtl/{name}\n" for name in files).encode())
    return Vet(Session(root / "design.f", top="top"))


def both(tmp_path: Path, form: str) -> Vet:
    """`leaf` and `leaf2` have the interface port in the same form. Only `leaf` is instantiated."""
    return build(
        tmp_path,
        {"ifc.sv": IFC, "leaf.sv": leaf("leaf", form), "leaf2.sv": leaf("leaf2", form), "top.sv": top(form)},
    )


def refused(vet: Vet, module: str = "leaf2", **more: object) -> str:
    with pytest.raises(VetError) as caught:
        vet.module("top").instance("u").replace_module(module, **more)
    return str(caught.value)


# --- F1, F4: every form of an interface port ------------------------------------------------------


@pytest.mark.parametrize("form", sorted(FORMS))
def test_a_module_with_an_interface_port_is_not_replaced(tmp_path, form):
    vet = both(tmp_path, form)
    message = refused(vet, waiver="the two are the same")
    assert message.startswith("replace_module top.u: "), message
    assert "'i'" in message, message
    assert "interface" in message, message


# --- F2: each side alone, and a module that is not instantiated -----------------------------------


def test_the_module_that_the_instance_gets_has_the_interface_port(tmp_path):
    """`top` has an ordinary `leaf`. `leaf2` is in the design, instantiated nowhere, with `ifc i`."""
    vet = build(
        tmp_path,
        {
            "ifc.sv": IFC,
            "leaf.sv": PLAIN_LEAF.format(name="leaf"),
            "leaf2.sv": leaf("leaf2", "plain"),
            "top.sv": PLAIN_TOP,
        },
    )
    message = refused(vet, waiver="w")
    assert message.startswith("replace_module top.u: "), message
    assert "'leaf2'" in message and "'i'" in message, message


def test_the_module_that_the_instance_has_has_the_interface_port(tmp_path):
    vet = build(
        tmp_path,
        {
            "ifc.sv": IFC,
            "leaf.sv": leaf("leaf", "plain"),
            "leaf2.sv": PLAIN_LEAF.format(name="leaf2"),
            "top.sv": top("plain"),
        },
    )
    message = refused(vet, waiver="w")
    assert "'leaf'" in message and "'i'" in message, message


# --- F3: a named type that is no interface --------------------------------------------------------


@pytest.mark.parametrize("named", ["pkg::two_t", "pkg::pair_t"])
def test_a_port_of_a_named_type_that_is_no_interface_is_not_refused(tmp_path, named):
    leaf_text = (
        "module {name} (input " + named + " i, input logic a, output logic y);\n  assign y = a & i[0];\nendmodule\n"
    )
    top_text = (
        "module top (input logic a, output logic y);\n"
        "  " + named + " b;\n  assign b = '1;\n"
        "  leaf u (.i(b), .a(a), .y(y));\nendmodule\n"
    )
    vet = build(
        tmp_path,
        {
            "pkg.sv": PKG,
            "leaf.sv": leaf_text.format(name="leaf"),
            "leaf2.sv": leaf_text.format(name="leaf2"),
            "top.sv": top_text,
        },
    )
    vet.module("top").instance("u").replace_module("leaf2", waiver="the two are the same")
    result = vet.finish()
    assert result.ok, result.report()


def test_a_port_of_an_imported_type_is_not_refused(tmp_path):
    """`input two_t i` after an import: a type with one name, as `ifc` is, and no interface."""
    leaf_text = (
        "module {name} import pkg::*; (input two_t i, input logic a, output logic y);\n"
        "  assign y = a & i[0];\nendmodule\n"
    )
    top_text = (
        "module top (input logic a, output logic y);\n"
        "  wire pkg::two_t b = '1;\n"
        "  leaf u (.i(b), .a(a), .y(y));\nendmodule\n"
    )
    vet = build(
        tmp_path,
        {
            "pkg.sv": PKG,
            "leaf.sv": leaf_text.format(name="leaf"),
            "leaf2.sv": leaf_text.format(name="leaf2"),
            "top.sv": top_text,
        },
    )
    vet.module("top").instance("u").replace_module("leaf2", waiver="the two are the same")
    result = vet.finish()
    assert result.ok, result.report()


# --- F5, F6: nothing changes ----------------------------------------------------------------------


def test_the_refusal_changes_nothing(tmp_path):
    vet = both(tmp_path, "plain")
    refused(vet, waiver="w")
    assert list(vet.log) == []
    result = vet.finish()
    assert result.ok, result.report()
    assert result.files == {}
    assert result.changes == []


def test_it_is_refused_with_no_waiver_too(tmp_path):
    vet = both(tmp_path, "plain")
    message = refused(vet)
    assert message.startswith("replace_module top.u: "), message
    assert "interface" in message, message
