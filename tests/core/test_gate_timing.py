"""The delay and the drive strength of a gate primitive at sign-off (#55).

Gate primitives and user-defined primitives are in the primitives snapshot since phase 5e,
and their terminals are compared. Their **delays** and **drive strengths** are not, so an
edit that changes only those passes the structural check unseen. Measured on `main` at
`c0730bd`, the whole record of a gate is its type and its terminals:

    top.g1   module='and'   conns={'0': 'y', '1': 'a', '2': 'b'}
    top.g2   module='and'   conns={'0': 'z', '1': 'a', '2': 'b'}

`and #2 g1 (y, a, b)` and `and g1 (y, a, b)` are the same record, and so are
`and (strong1, strong0) g2 (...)` and `and g2 (...)`.

THE ELABORATED ROUTE DOES NOT WORK, AND THAT IS NOT OUR DEFECT

`PrimitiveInstanceSymbol.driveStrength` **raises** `TypeError: Unable to convert function
return value to a Python type` whenever a strength is present, because the optional
`DriveStrength` enum has no Python caster. It is filed upstream as slang#2138, still open,
and it is not fixed in pyslang 12.2.0 either, so a version bump would not help.
`sym.delay` does not raise but carries no value: its text is
`TimingControl(TimingControlKind.Delay)` with the number absent.

The syntax route gives both exactly, and a syntax node is a permitted source of truth
under AGENTS.md. Measured through `_clean_text(sym.syntax.parent.delay)` and `.strength`:

    g1   and    delay='#2'         strength=None
    g2   and    delay=None         strength='(strong1, strong0)'
    g3   nand   delay='#(1:2:3)'   strength=None

OWNER DECISIONS (2026-11-21)

Scope is the delay and the strength only. The attributes, declarations and interface
modports the issue also lists each become their own issue. The two values go in a new field
of the snapshot with **two new change kinds**, `delay-changed` and `strength-changed`, not
into the empty `params` of a gate snap: a report saying `param-changed` for a gate delay
reads wrongly, and two kinds let a script expect one without the other.

The kinds are user-visible, because `Vet.expect` matches on the kind. Nothing validates a
kind against a list, so there is no registry to change, but both documented lists must gain
them: `docs/manual/chapters/operations.tex` (the kinds `Vet.expect` accepts) and
`docs/manual/chapters/signoff.tex` (what each kind means).

Rules
    K1  The snapshot records a gate's delay as the design writes it, and an empty value
        when it has none.
    K2  The snapshot records a gate's drive strength the same way.
    K3  An edit that changes only the delay is reported, as `delay-changed`, and the
        sign-off fails without an expectation.
    K4  An edit that changes only the strength is reported, as `strength-changed`.
    K5  Removing a delay is a change of the delay, not of the terminals.
    K6  A module instance is unaffected: both values stay empty for one, so a run that
            touches no gate gains no change.
    K7  `Vet.expect` covers the new kinds, so a deliberate edit can be declared.

Not checked
    Whether any design in the hdl-bag corpus carries a gate strength or a delay. The
    corpus needs the `bag` CLI, which is not installed here, and there is no `.corpus`
    cache, so **CI is the check for that**. After #116 the lesson is explicit: a cost or a
    behaviour that only the corpus exercises is not measured by a green local suite.

    A user-defined primitive with a delay. A `specify` block, which carries delays of its
    own and is a separate matter from a gate's own delay.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.ops import Vet
from hdl_vet.session import Session

GATES = (
    "module top (input logic a, input logic b, output logic y, output logic z,\n"
    "            output logic w, output logic v);\n"
    "  and #2 g1 (y, a, b);\n"
    "  and (strong1, strong0) g2 (z, a, b);\n"
    "  nand #(1:2:3) g3 (w, a, b);\n"
    "  xor g4 (v, a, b);\n"
    "endmodule\n"
)

LEAF = "module leaf (input logic d, output logic q);\n  assign q = d;\nendmodule\n"

PLAIN = "module top (input logic a, output logic y);\n  leaf u_l (.d(a), .q(y));\nendmodule\n"


def build(root: Path, files: dict[str, str]) -> tuple[Vet, Path]:
    (root / "rtl").mkdir(parents=True)
    for name, text in files.items():
        (root / "rtl" / f"{name}.sv").write_bytes(text.encode())
    fl = root / "design.f"
    fl.write_bytes("".join(f"rtl/{n}.sv\n" for n in files).encode())
    return Vet(Session(fl, top="top")), root / "rtl" / "top.sv"


@pytest.fixture
def gates(tmp_path: Path) -> tuple[Vet, Path]:
    return build(tmp_path / "g", {"top": GATES})


@pytest.fixture
def plain(tmp_path: Path) -> tuple[Vet, Path]:
    return build(tmp_path / "p", {"leaf": LEAF, "top": PLAIN})


def replace(vet: Vet, path: Path, old: bytes, new: bytes) -> None:
    data = vet.text(path)
    at = data.index(old)
    vet.splice(path, at, at + len(old), new, old=old)


def kinds(result) -> list[tuple[str, str]]:
    return [(c.kind, c.path) for c in result.changes]


# --- K1, K2: the values are recorded -------------------------------------------------


def test_a_delay_is_recorded(gates):
    vet, _ = gates
    gate = vet.session.loaded.gate_snapshot()["top.g1"]
    assert gate.delay == "#2", gate


def test_a_three_value_delay_is_recorded_as_written(gates):
    vet, _ = gates
    gate = vet.session.loaded.gate_snapshot()["top.g3"]
    assert gate.delay == "#(1:2:3)", gate


def test_a_strength_is_recorded(gates):
    vet, _ = gates
    gate = vet.session.loaded.gate_snapshot()["top.g2"]
    assert gate.strength == "(strong1, strong0)", gate


def test_a_gate_with_neither_records_empty_values(gates):
    vet, _ = gates
    gate = vet.session.loaded.gate_snapshot()["top.g4"]
    assert gate.delay == "" and gate.strength == "", gate


# --- K3, K4, K5: a change is reported ------------------------------------------------


def test_changing_only_the_delay_is_reported(gates):
    """`and #2 g1` becomes `and #5 g1`. Nothing else moves, and today nothing sees it."""
    vet, top = gates
    replace(vet, top, b"and #2 g1", b"and #5 g1")
    result = vet.finish()
    assert not result.ok, result.report()
    assert ("delay-changed", "top.g1") in kinds(result), kinds(result)


def test_changing_only_the_strength_is_reported(gates):
    vet, top = gates
    replace(vet, top, b"(strong1, strong0)", b"(weak1, weak0)")
    result = vet.finish()
    assert not result.ok, result.report()
    assert ("strength-changed", "top.g2") in kinds(result), kinds(result)


def test_removing_a_delay_is_a_delay_change(gates):
    """Rule K5: it must not be reported as a change of the terminals."""
    vet, top = gates
    replace(vet, top, b"and #2 g1", b"and g1")
    result = vet.finish()
    assert ("delay-changed", "top.g1") in kinds(result), kinds(result)
    assert not any(k == "connection-changed" and p == "top.g1" for k, p in kinds(result)), kinds(result)


# --- K6: a module instance is unaffected ---------------------------------------------


def test_a_design_without_gates_gains_no_change(plain):
    vet, _ = plain
    vet.module("top").add_wire("probe_w")
    result = vet.finish()
    assert result.ok, result.report()
    assert not any(k in ("delay-changed", "strength-changed") for k, _ in kinds(result)), kinds(result)


# --- K7: the new kinds can be expected -----------------------------------------------


def test_an_expect_covers_a_delay_change(gates):
    vet, top = gates
    vet.expect("delay-changed", path="top.g1")
    replace(vet, top, b"and #2 g1", b"and #5 g1")
    result = vet.finish()
    assert result.ok, result.report()


def test_an_expect_covers_a_strength_change(gates):
    vet, top = gates
    vet.expect("strength-changed", path="top.g2")
    replace(vet, top, b"(strong1, strong0)", b"(weak1, weak0)")
    result = vet.finish()
    assert result.ok, result.report()
