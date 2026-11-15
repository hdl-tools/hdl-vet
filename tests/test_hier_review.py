"""Phase 4 review fixes: cases the reviewer broke with probes.

Spec changes and additions (they extend the docstring of test_hier.py; where
they differ, this file wins):

propagate_param: where the value comes from, and what may already exist
    A lower part of the path may already be propagated. Let k be the smallest
    index such that every module Mk..M(n-1) already has value parameter `name`
    and each passes it on (`.name(name)` on the instance of the module below).
    If no such module exists, k = n: nothing is propagated yet.
    - The value in use is decided at the instance of Mk in M(k-1): <default> is
      `default` if given; else that instance's override text; else the default
      declared in Mk.
    - Only M0..M(k-1) get the parameter, and only the instance of Mk in M(k-1),
      and the instances above it, get `.name(name)`.
    - A module among M0..M(k-1) that already has a parameter called `name` is
      refused (VetError): tying it in would join two unrelated parameters and
      could change either value.
    - k = 0 means the propagation is complete: everything is logged as skipped.
    - Nothing is permitted: propagate_param registers no permit for a parameter
      change at any path. A value that changes anywhere on the path is an
      unexpected change at sign-off.
    - The post-condition compares the destination's resolved value with its
      value when propagate_param was called (after any re-load the call needed),
      not with the baseline, so an earlier set_param in the same script counts.

punch_port
    - An output cannot be routed through a port that is already driven: when a
      module among M(n-1)..M0 already has output port `name` (so it is skipped),
      that port must have no driver other than the connection from the instance
      below, else VetError naming it.
    - A signal declared `interconnect` is refused.
    - Whether or not all_instances is given, an instance of one of M1..Mn that
      is written inside a generate block or instance array, in any module of
      the design, is refused (VetError naming it and saying "generate" or
      "array"), also when its branch is not elaborated today: another
      parameterisation would leave its new port unconnected.

All or nothing, for both operations
    The files to be edited are checked for editability before anything is
    queued; a non-editable file raises VetError naming it. Committing a
    validated plan cannot raise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session
from vetkit import make_design

LEAF_W = b"module leaf #(parameter int W = 4) (input logic a);\nendmodule\n"


def vet_for(tmp_path: Path, src: bytes, top: str = "t") -> Vet:
    (tmp_path / "t.sv").write_bytes(src)
    (tmp_path / "t.f").write_bytes(b"t.sv\n")
    return Vet(Session(tmp_path / "t.f", top=top))


def edits(diff: str) -> list[str]:
    return [
        line
        for line in diff.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---")) and "Modified by hdl-vet" not in line
    ]


@pytest.fixture
def kit(tmp_path: Path):
    return make_design(tmp_path)


@pytest.fixture
def vet(kit):
    return Vet(Session(kit.filelist, top="top"))


# --- propagate_param ----------------------------------------------------------------------


def test_propagate_refuses_an_unrelated_parameter_of_the_same_name(tmp_path: Path):
    # mid has its own W (overridden to 3 from t) and gives leaf a fixed 8.
    vet = vet_for(
        tmp_path,
        LEAF_W + b"module mid #(parameter int W = 8) (input logic a);\n  logic [W-1:0] r;\n"
        b"  leaf #(.W(8)) u_l (.a(a));\nendmodule\n"
        + b"module t (input logic a);\n  mid #(.W(3)) u_mid (.a(a));\nendmodule\n",
    )
    with pytest.raises(VetError, match=r"mid.*'W'"):
        vet.propagate_param("W", to="t.u_mid.u_l")
    assert vet.log == []
    r = vet.finish()
    assert r.ok and r.files == {}


def test_completing_a_propagation_uses_the_value_in_use(tmp_path: Path):
    # mid already passes W down; t overrides it with 3, which is the value in use.
    vet = vet_for(
        tmp_path,
        LEAF_W
        + b"module mid #(parameter int W = 8) (input logic a);\n  leaf #(.W(W)) u_l (.a(a));\nendmodule\n"
        + b"module t (input logic a);\n  mid #(.W(3)) u_mid (.a(a));\nendmodule\n",
    )
    vet.propagate_param("W", to="t.u_mid.u_l")
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        "-module t (input logic a);",
        "-  mid #(.W(3)) u_mid (.a(a));",
        "+module t #(parameter int W = 3) (input logic a);",
        "+  mid #(.W(W)) u_mid (.a(a));",
    ]
    assert [(c.kind, c.path, c.name, c.after) for c in r.changes] == [("param-changed", "t", "W", "3")]


def test_completing_a_propagation_without_an_override_uses_the_declared_default(tmp_path: Path):
    vet = vet_for(
        tmp_path,
        LEAF_W
        + b"module mid #(parameter int W = 8) (input logic a);\n  leaf #(.W(W)) u_l (.a(a));\nendmodule\n"
        + b"module t (input logic a);\n  mid u_mid (.a(a));\nendmodule\n",
    )
    vet.propagate_param("W", to="t.u_mid.u_l")
    r = vet.finish()
    assert r.ok, r.report()
    assert "+module t #(parameter int W = 8) (input logic a);" in edits(r.diff)
    assert "+  mid #(.W(W)) u_mid (.a(a));" in edits(r.diff)


def test_propagate_after_set_param_keeps_the_new_value(vet):
    vet.module("mid").instance("u_l1").set_param("DEPTH", 5, was=3)
    vet.propagate_param("DEPTH", to="top.u_mid.u_l1")
    r = vet.finish()
    assert r.ok, r.report()
    assert "+module top #(parameter int DEPTH = 5) (" in edits(r.diff)
    assert r.failed_checks == []


def test_propagate_permits_no_value_change_on_the_way(tmp_path: Path):
    # A raw splice changes mid's use of the new parameter after the propagation:
    # the destination value moves, and nothing may excuse it.
    vet = vet_for(
        tmp_path,
        LEAF_W
        + b"module mid (input logic a);\n  leaf #(.W(6)) u_l (.a(a));\nendmodule\n"
        + b"module t (input logic a);\n  mid u_mid (.a(a));\nendmodule\n",
    )
    vet.propagate_param("W", to="t.u_mid.u_l")
    _ = vet.design
    text = vet.text(tmp_path / "t.sv")
    at = text.index(b".W(W)) u_l")
    vet.splice(tmp_path / "t.sv", at, at + 5, b".W(W + 1)")
    r = vet.finish()
    assert not r.ok
    assert ("param-changed", "t.u_mid.u_l", "W") in [(c.kind, c.path, c.name) for c in r.unexpected_changes]


# --- punch_port ---------------------------------------------------------------------------


def test_output_through_an_already_driven_port_is_refused(tmp_path: Path):
    vet = vet_for(
        tmp_path,
        b"module leaf (input logic a, output logic [3:0] q);\n  assign q = {4{a}};\nendmodule\n"
        b"module mid (input logic a, output logic [3:0] dbg);\n  assign dbg = 4'h5;\n"
        b"  leaf u_l (.a(a), .q());\nendmodule\n"
        b"module t (input logic a, output logic [3:0] dbg);\n  mid u_mid (.a(a), .dbg(dbg));\nendmodule\n",
    )
    with pytest.raises(VetError, match=r"mid.*'dbg'.*driven"):
        vet.punch_port("dbg", "output", to="t.u_mid.u_l", signal="q")
    assert vet.log == [] and vet.finish().files == {}


def test_interconnect_signal_is_refused(tmp_path: Path):
    vet = vet_for(
        tmp_path,
        b"module leaf (input logic a);\n  interconnect [1:0] ic;\nendmodule\n"
        b"module mid (input logic a);\n  leaf u_leaf (.a(a));\nendmodule\n"
        b"module t (input logic a);\n  mid u_mid (.a(a));\nendmodule\n",
    )
    with pytest.raises(VetError, match="interconnect"):
        vet.punch_port("po", "output", to="t.u_mid.u_leaf", signal="ic")
    assert vet.log == []


def test_all_instances_refuses_copies_in_unelaborated_generate_branches(tmp_path: Path):
    vet = vet_for(
        tmp_path,
        b"module leaf (input logic a);\nendmodule\n"
        b"module mid #(parameter bit EXTRA = 0) (input logic a);\n"
        b"  leaf u_leaf (.a(a));\n"
        b"  if (EXTRA) begin : g_extra\n    leaf u_more (.a(a));\n  end\n"
        b"endmodule\n"
        b"module t (input logic a);\n  mid u_mid (.a(a));\nendmodule\n",
    )
    with pytest.raises(VetError, match=r"u_more.*generate"):
        vet.punch_port("dft", "input", to="t.u_mid.u_leaf", all_instances=True)
    with pytest.raises(VetError, match=r"u_more.*generate"):
        # leaf is elaborated once today, but the module text has a second copy
        vet.punch_port("dft", "input", to="t.u_mid.u_leaf")
    assert vet.log == [] and vet.finish().files == {}


# --- all or nothing ------------------------------------------------------------------------


def test_non_editable_file_on_the_path_queues_nothing(kit):
    vet = Vet(Session(kit.filelist, top="top", editable=[kit.mid, kit.top]))
    with pytest.raises(VetError, match="leaf.sv"):
        vet.punch_port("dft", "input", to="top.u_mid.u_l0", all_instances=True)
    assert vet.log == []
    r = vet.finish()
    assert r.ok and r.files == {}

    vet = Vet(Session(kit.filelist, top="top", editable=[kit.mid]))
    with pytest.raises(VetError, match="top.sv"):
        vet.propagate_param("DEPTH", to="top.u_mid.u_l1")
    assert vet.log == [] and vet.finish().files == {}


def test_destination_file_need_not_be_editable_for_propagate(kit):
    # the destination module (leaf) is not edited by propagate_param
    vet = Vet(Session(kit.filelist, top="top", editable=[kit.mid, kit.top]))
    vet.propagate_param("DEPTH", to="top.u_mid.u_l1")
    assert vet.finish().ok
