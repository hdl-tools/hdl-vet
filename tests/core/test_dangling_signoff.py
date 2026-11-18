"""The dangling check at sign-off, for every script.

Owner rule (2026-11-15): an output may be left floating; an input may not be
left undriven. Phase 5 applied this inside its own operations only. Phase 3
operations and raw splices could still leave a signal without a driver
(`disconnect` on an output whose signal is read elsewhere; a new output port
that nothing drives). Sign-off now checks it for every script.

What counts as dangling ("the dangling set" of a design):
    - a net or variable that is read but never driven (slang: UndrivenNet,
      UnassignedVariable);
    - an output port of a module that nothing inside the module drives
      (UndrivenPort), but only when something reads it:
        * some instance of that module, in that design, connects the port to
          a non-empty expression (whoever reads that expression gets no
          value), or
        * the module itself reads the port's signal (it feeds an instance
          input or an expression inside the module).
      An undriven output that nobody reads (every instance leaves it open as
      `.p()`, or it is an output of a top module) is a floating output and is
      allowed.
      The port is identified by the symbol slang reports, not by text: the
      module is the one the port belongs to (also when the module comes from
      a macro or the port declaration from an included file), and a port
      with a different external name (`.qo(q)`) is matched by its internal
      signal. An entry whose module or port cannot be identified counts
      (fail safe).
    Entries located inside an added file never count.
`Design.dangling()` itself is unchanged (it reports every UndrivenPort); the
rule above is applied where designs are compared.

An instance input that is connected to nothing:
    - absent from the list: slang's UnconnectedInputPort, already a warning
      that sign-off counts;
    - an empty `.d()`: slang's EmptyInputPortConn, which joins the
      always-reported warnings. Baseline occurrences do not matter; a new one
      fails sign-off like any new diagnostic.

Sign-off (`Session.finish`):
    The dangling set of the final design is compared with that of the
    baseline, as multisets of (code, module, file, message). Entries that are
    new make `Result.ok` False and are listed in `Result.dangling`
    (list[Diag], empty otherwise). `Result.report()` has one line for each:
        "  left dangling: <slang's message> in module <module> (<file>:<line>)"
    Nothing is compared when no file changed. No extra re-load is made.

Phase 5 checked operations and groups use the same dangling set, so an
operation that leaves an unconnected output port undriven is no longer
refused. The comparison of driven bits for the signals a removal touched
stays as it is, and stays with those operations.

Not covered at sign-off (slang reports nothing in these cases):
    - a signal that loses the driver of only some of its bits through a
      phase 3 operation or a raw splice;
    - a signal or port whose only driver sits in a generate branch that a
      parameter change switches off;
    - an inout port or the net behind it;
    - a new reader of something the baseline already left undriven (the
      entry is not new).
A raw splice that renames a module makes that module's baseline entries look
new; no operation renames a module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session
from hdl_vet.slang_adapter import load
from vetkit import make_design, span


@pytest.fixture
def kit(tmp_path: Path):
    return make_design(tmp_path / "in")


@pytest.fixture
def vet(kit):
    return Vet(Session(kit.filelist, top="top"))


def design(tmp_path: Path, text: str, top: str = "top") -> Vet:
    root = tmp_path / "d"
    root.mkdir()
    (root / "d.sv").write_bytes(text.encode())
    (root / "d.f").write_bytes(b"d.sv\n")
    return Vet(Session(root / "d.f", top=top))


def dangling(result) -> list[tuple[str, str]]:
    return [(d.code, d.message) for d in result.dangling]


# --- phase 3 operations ----------------------------------------------------------------


def test_disconnecting_an_output_that_is_still_read_fails_sign_off(vet):
    vet.module("mid").instance("u_l1").disconnect("q", was="y1")  # mid's y1 feeds top's y1
    r = vet.finish()
    assert not r.ok
    assert dangling(r) == [("UndrivenPort", "undriven output port 'y1'")]
    assert r.dangling[0].path.name == "mid.sv"
    assert "  left dangling: undriven output port 'y1' in module mid (rtl/mid.sv:" in r.report()
    assert r.new_diagnostics == [] and r.unexpected_changes == []


def test_disconnecting_and_tying_off_passes(vet):
    mid = vet.module("mid")
    mid.instance("u_l1").disconnect("q", was="y1")
    mid.add_assign("y1", "'0")
    r = vet.finish()
    assert r.ok, r.report()
    assert r.dangling == []


def test_an_output_nobody_reads_may_float(vet):
    vet.module("top").instance("u_spare").disconnect("q", was="spare_q")
    r = vet.finish()
    assert r.ok, r.report()


def test_a_new_output_port_that_is_connected_must_be_driven(vet):
    mid, top = vet.module("mid"), vet.module("top")
    mid.add_port("dbg_o", "output")
    top.add_wire("dbg")
    top.instance("u_mid").connect("dbg_o", "dbg")
    r = vet.finish()
    assert not r.ok
    assert dangling(r) == [("UndrivenPort", "undriven output port 'dbg_o'")]


def test_a_new_output_port_that_is_driven_passes(vet):
    mid, top = vet.module("mid"), vet.module("top")
    mid.add_port("dbg_o", "output")
    mid.add_assign("dbg_o", "a[0]")
    top.add_wire("dbg")
    top.instance("u_mid").connect("dbg_o", "dbg")
    assert vet.finish().ok


def test_an_undriven_output_of_the_top_may_float(vet):
    vet.module("top").add_port("later_o", "output")
    r = vet.finish()
    assert r.ok, r.report()


def test_a_signal_that_is_read_but_never_driven_fails_sign_off(vet):
    top = vet.module("top")
    top.add_wire("fresh", width=4)
    top.instance("u_spare").connect("d", "fresh", was="spare_d")
    r = vet.finish()
    assert not r.ok
    assert dangling(r) == [("UnassignedVariable", "variable 'fresh' is never assigned a value")]


# --- raw splices -------------------------------------------------------------------------


def test_an_empty_input_connection_is_a_new_diagnostic(vet, kit):
    start, end = span(vet.text(kit.mid), b".d(a)")
    vet.splice(kit.mid, start, end, b".d()")
    vet.expect("connection-changed", "top.u_mid.u_l0", "d")
    r = vet.finish()
    assert not r.ok
    assert [d.code for d in r.new_diagnostics] == ["EmptyInputPortConn"]


def test_a_raw_splice_that_removes_a_driver_fails_sign_off(vet, kit):
    start, end = span(vet.text(kit.top), b"  assign spare_d = a;\n")
    vet.splice(kit.top, start, end, b"")
    r = vet.finish()
    assert not r.ok
    assert dangling(r) == [("UnassignedVariable", "variable 'spare_d' is never assigned a value")]


# --- the baseline ------------------------------------------------------------------------


ALREADY = (
    "module leafy (input logic clk, input logic [3:0] d, output logic [3:0] q, output logic spare_o);\n"
    "  always_ff @(posedge clk) q <= d;\n"
    "endmodule\n"
    "module top (input logic clk, input logic [3:0] a, output logic [3:0] y, output logic [3:0] z);\n"
    "  logic [3:0] floating;\n"
    "  leafy u0 (.clk(clk), .d(a), .q(y), .spare_o());\n"
    "  leafy u1 (.clk(clk), .d(floating), .q(z), .spare_o());\n"
    "endmodule\n"
)


def test_what_the_baseline_already_has_does_not_count(tmp_path: Path):
    vet = design(tmp_path, ALREADY)
    assert [g.code for g in load(vet.session.filelist_path, top="top").dangling()] != []
    vet.module("top").instance("u0").connect("d", "~a", was="a")
    r = vet.finish()
    assert r.ok, r.report()
    assert r.dangling == []


def test_connecting_an_output_the_baseline_left_undriven_and_open_fails(tmp_path: Path):
    vet = design(tmp_path, ALREADY)
    top = vet.module("top")
    top.add_wire("s")
    top.instance("u0").connect("spare_o", "s")
    r = vet.finish()
    assert not r.ok
    assert dangling(r) == [("UndrivenPort", "undriven output port 'spare_o'")]


def test_nothing_is_compared_when_nothing_changed(vet):
    r = vet.finish()
    assert r.ok and r.dangling == [] and vet.session.reloads == 0


# --- phase 5 operations use the same set -------------------------------------------------


OPEN = (
    "module child (input logic a, output logic y, output logic spare_o);\n"
    "  assign y = a;\n"
    "  assign spare_o = a;\n"
    "endmodule\n"
    "module top (input logic a, output logic y);\n"
    "  child u (.a(a), .y(y), .spare_o());\n"
    "endmodule\n"
)


def test_the_driver_of_an_output_every_instance_leaves_open_can_be_removed(tmp_path: Path):
    vet = design(tmp_path, OPEN)
    vet.module("child").remove_assign("spare_o")
    r = vet.finish()
    assert r.ok, r.report()


def test_the_driver_of_a_connected_output_still_cannot_be_removed(tmp_path: Path):
    vet = design(tmp_path, OPEN)
    with pytest.raises(VetError, match="'y'"):
        vet.module("child").remove_assign("y")
    assert vet.log == []


# --- review fixes --------------------------------------------------------------------------


CHILD = "module child (input logic [3:0] d, output logic [3:0] q);\n  assign q = d;\nendmodule\n"
CHAIN = (
    CHILD + "module top (input logic [3:0] a, output logic [3:0] y, output logic [3:0] z);\n"
    "  child u0 (.d(a), .q(y));\n"
    "  child u1 (.d(y), .q(z));\n"
    "endmodule\n"
)


def remove_text(vet: Vet, path: Path, text: bytes) -> None:
    start, end = span(vet.text(path), text)
    vet.splice(path, start, end, b"", waiver="set-up of the test")


def test_an_output_its_own_module_reads_must_stay_driven(tmp_path: Path):
    vet = design(tmp_path, CHAIN)
    vet.module("top").instance("u0").disconnect("q", was="y")  # `y` is a top output, and feeds u1.d
    r = vet.finish()
    assert not r.ok
    assert dangling(r) == [("UndrivenPort", "undriven output port 'y'")]


def test_an_output_read_inside_a_module_that_leaves_it_open_must_stay_driven(tmp_path: Path):
    vet = design(
        tmp_path,
        CHILD + "module mid (input logic [3:0] a, output logic [3:0] y, output logic [3:0] w);\n"
        "  assign y = a;\n"
        "  child u (.d(y), .q(w));\n"
        "endmodule\n"
        "module top (input logic [3:0] a, output logic [3:0] w);\n"
        "  mid u_mid (.a(a), .y(), .w(w));\n"
        "endmodule\n",
    )
    remove_text(vet, vet.module("mid").path, b"  assign y = a;\n")
    r = vet.finish()
    assert not r.ok
    assert dangling(r) == [("UndrivenPort", "undriven output port 'y'")]


def test_a_module_that_comes_from_a_macro_is_still_matched(tmp_path: Path):
    vet = design(
        tmp_path,
        "`define MK(name) module name (input logic [3:0] d, output logic [3:0] q); endmodule\n"
        "`MK(child)\n"
        "module top (input logic [3:0] a, output logic [3:0] y);\n"
        "  logic [3:0] t;\n"
        "  child u (.d(a), .q());\n"
        "  assign y = a;\n"
        "endmodule\n",
    )
    path = vet.module("top").path
    start, end = span(vet.text(path), b".q()")
    vet.splice(path, start, end, b".q(t)")
    vet.expect("connection-changed", "top.u", "q")
    r = vet.finish()
    assert not r.ok
    assert dangling(r) == [("UndrivenPort", "undriven output port 'q'")]


def test_a_port_with_another_external_name_is_matched_by_its_signal(tmp_path: Path):
    vet = design(
        tmp_path,
        "module child (d, .qo(q));\n"
        "  input  logic [3:0] d;\n"
        "  output logic [3:0] q;\n"
        "  assign q = d;\n"
        "endmodule\n"
        "module top (input logic [3:0] a, output logic [3:0] y);\n"
        "  logic [3:0] t;\n"
        "  child u (.d(a), .qo(t));\n"
        "  assign y = ~t;\n"
        "endmodule\n",
    )
    remove_text(vet, vet.module("child").path, b"  assign q = d;\n")
    r = vet.finish()
    assert not r.ok
    assert dangling(r) == [("UndrivenPort", "undriven output port 'q'")]


def test_a_port_declared_in_an_included_header_is_matched(tmp_path: Path):
    root = tmp_path / "inc"
    root.mkdir()
    (root / "ports.svh").write_bytes(b"  output logic [3:0] q,\n")
    (root / "d.sv").write_bytes(
        b'module child (\n`include "ports.svh"\n  input logic [3:0] d\n);\n  assign q = d;\nendmodule\n'
        b"module top (input logic [3:0] a, output logic [3:0] y);\n  child u (.d(a), .q(y));\nendmodule\n"
    )
    (root / "d.f").write_bytes(b"d.sv\n")
    vet = Vet(Session(root / "d.f", top="top"))
    remove_text(vet, (root / "d.sv").resolve(), b"  assign q = d;\n")
    r = vet.finish()
    assert not r.ok
    assert dangling(r) == [("UndrivenPort", "undriven output port 'q'")]


def test_an_undriven_output_carried_up_to_a_top_output_is_refused(vet):
    # Nothing inside the design reads the top's output, but the chain below it is
    # connected and carries no value: the rule looks at each connection, not at the end.
    vet.module("mid").add_port("dbg_o", "output")
    vet.module("top").add_port("dbg_o", "output")
    vet.module("top").instance("u_mid").connect("dbg_o", "dbg_o")
    r = vet.finish()
    assert not r.ok
    assert dangling(r) == [("UndrivenPort", "undriven output port 'dbg_o'")]
    assert r.dangling[0].path.name == "mid.sv"
