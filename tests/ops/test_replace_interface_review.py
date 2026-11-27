"""Review of #141: the interface port is told from the design, not from the spelling of its type.

The first form of the fix compared the text of the type of a port with the names of the interfaces
of the design. The review ran about 80 designs against it and against `main`, and found two ways
in which a text is not the design:

F7  An interface whose name is written as an escaped identifier (`\\ifc  i`, or an interface that
    is declared as `\\eif `) is an interface port. `replace_module` refuses it. With the first
    form the swap was accepted, as on `main`: a wrong "ok".
F8  A type that has the name of an interface of the design, and is no interface, is not refused:
    a typedef in front of the module or from an imported package, and a type parameter of the
    module. With the first form these were refused, where `main` accepts them.

The second form asks the compiler, which elaborates the module on its own. The second review found
that a module which cannot stand on its own was then refused:

F9  A module with a parameter that has no default, a value or a type, is judged like any other
    module: with no interface port the swap is accepted, as on `main`, and with an interface
    port it is refused. When it cannot be told whether a module has an interface port, the
    answer is a refusal, never "no interface port".

The third form asks the compiler about an instance of the module inside a helper module with a
fixed name. The third review found that a module of the design with that name was then judged as
the helper, which has no ports:

F10 A module of the design that has the name of the helper, `__hdlvet_probe`, is not judged.
    `replace_module` to it or from it is refused, and the message says that the name is taken by
    the tool. No swap was accepted through this: only one module can have the name, and the
    comparison of the ports refused it. The rule closes it at the question.

What held in the review: `ifc i, j` (every name of a shared header), an interface port that is not
the first port, an interface that is declared after the modules or in a later file, a macro for
the type or for the whole port, a comment or a line break between the type and the name, a module
that is instantiated nowhere, and a module with two instances.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet import equiv
from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session

IFC = "interface ifc;\n  logic v;\n  modport mp (input v);\nendinterface\n"
EIF = "interface \\eif ;\n  logic v;\nendinterface\n"
WHY = "the two are the same"


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


def leaves(port: str, use: str = "i.v", head: str = "") -> str:
    """`leaf` and `leaf2` in one file, the same but for the name. `head` is text in front of both."""
    return head + "".join(
        f"module {name} ({port}, input logic a, output logic y);\n  assign y = a & {use};\nendmodule\n"
        for name in ("leaf", "leaf2")
    )


def top(bus: str) -> str:
    return f"module top (input logic a, output logic y);\n  {bus}\n  leaf u (.i(b), .a(a), .y(y));\nendmodule\n"


def swap(vet: Vet) -> None:
    vet.module("top").instance("u").replace_module("leaf2", waiver=WHY)


# --- F7: an escaped name --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("interface", "port", "bus"),
    [
        (IFC, "\\ifc  i", "ifc b ();"),
        (IFC, "\\ifc \t i", "ifc b ();"),
        (EIF, "\\eif  i", "\\eif  b ();"),
    ],
    ids=["escaped-use", "escaped-use-tab", "escaped-declaration"],
)
def test_an_interface_under_an_escaped_name_is_an_interface_port(tmp_path, interface, port, bus):
    vet = build(tmp_path, {"ifc.sv": interface, "leaves.sv": leaves(port), "top.sv": top(bus)})
    with pytest.raises(VetError) as caught:
        swap(vet)
    message = str(caught.value)
    assert message.startswith("replace_module top.u: "), message
    assert "interface" in message and "'i'" in message, message
    assert list(vet.log) == []


# --- F8: a type with the name of an interface -----------------------------------------------------

TWO = "logic [1:0] b;\n  assign b = '1;"


def accepted(tmp_path: Path, files: dict[str, str]) -> None:
    vet = build(tmp_path, files)
    swap(vet)
    result = vet.finish()
    assert result.ok, result.report()


def test_a_typedef_with_the_name_of_an_interface_is_no_interface(tmp_path):
    """The interface `ifc` is in the design. In the file of the leaves, `ifc` is a typedef."""
    text = leaves("input ifc i", use="i[0]", head="typedef logic [1:0] ifc;\n")
    accepted(tmp_path, {"ifc.sv": IFC, "leaves.sv": text, "top.sv": top(TWO)})


def test_an_imported_typedef_with_the_name_of_an_interface_is_no_interface(tmp_path):
    package = "package q;\n  typedef logic [1:0] ifc;\nendpackage\n"
    text = "".join(
        f"module {name} import q::*; (input ifc i, input logic a, output logic y);\n  assign y = a & i[0];\nendmodule\n"
        for name in ("leaf", "leaf2")
    )
    accepted(tmp_path, {"ifc.sv": IFC, "q.sv": package, "leaves.sv": text, "top.sv": top(TWO)})


def test_a_type_parameter_with_the_name_of_an_interface_is_no_interface(tmp_path):
    text = "".join(
        f"module {name} #(parameter type ifc = logic) (input ifc i, input logic a, output logic y);\n"
        "  assign y = a & i;\nendmodule\n"
        for name in ("leaf", "leaf2")
    )
    one = "logic b;\n  assign b = 1'b1;"
    accepted(tmp_path, {"ifc.sv": IFC, "leaves.sv": text, "top.sv": top(one)})


# --- F9: a module that cannot stand on its own ----------------------------------------------------

ONE = "logic b;\n  assign b = 1'b1;"


def bare(params: str, port: str = "input logic i", use: str = "i") -> str:
    return "".join(
        f"module {name} #({params}) ({port}, input logic a, output logic y);\n  assign y = a & {use};\nendmodule\n"
        for name in ("leaf", "leaf2")
    )


def top_with(override: str, bus: str = ONE) -> str:
    return (
        f"module top (input logic a, output logic y);\n  {bus}\n"
        f"  leaf #({override}) u (.i(b), .a(a), .y(y));\nendmodule\n"
    )


@pytest.mark.parametrize(
    ("params", "override"),
    [("parameter int W", ".W(1)"), ("parameter type T", ".T(logic)")],
    ids=["value", "type"],
)
def test_a_module_with_a_parameter_that_has_no_default_is_swapped(tmp_path, params, override):
    accepted(tmp_path, {"ifc.sv": IFC, "leaves.sv": bare(params), "top.sv": top_with(override)})


def test_only_the_new_module_has_a_parameter_with_no_default(tmp_path):
    old = "module leaf (input logic i, input logic a, output logic y);\n  assign y = a & i;\nendmodule\n"
    new = (
        "module leaf2 #(parameter int W) (input logic i, input logic a, output logic y);\n"
        "  assign y = a & i;\nendmodule\n"
    )
    plain_top = f"module top (input logic a, output logic y);\n  {ONE}\n  leaf u (.i(b), .a(a), .y(y));\nendmodule\n"
    vet = build(tmp_path, {"ifc.sv": IFC, "leaf.sv": old, "leaf2.sv": new, "top.sv": plain_top})
    vet.module("top").instance("u").replace_module("leaf2", set_params={"W": 1}, waiver=WHY)
    result = vet.finish()
    assert result.ok, result.report()


@pytest.mark.parametrize(
    ("params", "override"),
    [("parameter int W", ".W(1)"), ("parameter type T", ".T(logic)")],
    ids=["value", "type"],
)
def test_such_a_module_with_an_interface_port_is_refused(tmp_path, params, override):
    files = {
        "ifc.sv": IFC,
        "leaves.sv": bare(params, port="ifc i", use="i.v"),
        "top.sv": top_with(override, bus="ifc b ();"),
    }
    vet = build(tmp_path, files)
    with pytest.raises(VetError) as caught:
        swap(vet)
    message = str(caught.value)
    assert message.startswith("replace_module top.u: "), message
    assert "'i'" in message and "interface" in message, message


def test_only_the_new_module_has_no_default_and_an_interface_port(tmp_path):
    old = "module leaf (input logic i, input logic a, output logic y);\n  assign y = a & i;\nendmodule\n"
    new = "module leaf2 #(parameter int W) (ifc i, input logic a, output logic y);\n  assign y = a & i.v;\nendmodule\n"
    plain_top = f"module top (input logic a, output logic y);\n  {ONE}\n  leaf u (.i(b), .a(a), .y(y));\nendmodule\n"
    vet = build(tmp_path, {"ifc.sv": IFC, "leaf.sv": old, "leaf2.sv": new, "top.sv": plain_top})
    with pytest.raises(VetError) as caught:
        vet.module("top").instance("u").replace_module("leaf2", set_params={"W": 1}, waiver=WHY)
    assert str(caught.value).startswith("replace_module top.u: "), str(caught.value)
    assert list(vet.log) == []


# --- F10: the name of the helper ------------------------------------------------------------------

PROBE = "__hdlvet_probe"


@pytest.mark.parametrize("port", ["ifc i", "input logic i"], ids=["interface", "plain"])
def test_a_module_with_the_name_of_the_helper_is_not_judged(tmp_path, port):
    use = "i.v" if port.startswith("ifc") else "i"
    old = "module leaf (input logic i, input logic a, output logic y);\n  assign y = a & i;\nendmodule\n"
    new = f"module {PROBE} ({port}, input logic a, output logic y);\n  assign y = a & {use};\nendmodule\n"
    plain_top = f"module top (input logic a, output logic y);\n  {ONE}\n  leaf u (.i(b), .a(a), .y(y));\nendmodule\n"
    vet = build(tmp_path, {"ifc.sv": IFC, "leaf.sv": old, "probe.sv": new, "top.sv": plain_top})
    with pytest.raises(VetError) as caught:
        vet.module("top").instance("u").replace_module(PROBE, waiver=WHY)
    message = str(caught.value)
    assert message.startswith("replace_module top.u: "), message
    assert PROBE in message and "taken by" in message, message
    assert list(vet.log) == []
