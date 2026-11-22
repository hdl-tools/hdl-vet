"""`remove_assign` and `remove_instance` and the name they are given (#113).

#103 fixed the three declaration removals: they judge the name against what the module
declares, and fall back to a spelling test only for a name the module does not hold. These
two were left out, because neither takes a declared name in that sense, and both still
report `skipped` for a name they cannot act on. Measured on `main` at `c0730bd`:

    remove_assign('\\y ')          -> [('remove_assign', 'skipped')]    while `y` IS assigned
    remove_assign('not a name!!') -> [('remove_assign', 'skipped')]
    remove_assign('')             -> [('remove_assign', 'skipped')]
    remove_instance('not a name!!') -> [('remove_instance', 'skipped')]
    remove_instance('')             -> [('remove_instance', 'skipped')]

WHAT CHANGED IN THE DECISION, AND WHY IT MATTERS HERE

The first decision was to refuse an escaped spelling everywhere, and for the instance side
to stop normalising. That was withdrawn on 2026-11-22, because rule G2 in
`tests/ops/test_manual_findings.py` is a frozen specification requiring all three spellings
of an escaped instance name to name the same instance, and it records that as the
deliberate fix for #54, where the escaped form was "skipped without a word".

The two sides are not symmetrical, and the difference is the point:

    an instance      every spelling resolves to one unambiguous instance, and
                     `Module.info` reports the plain form. Accepting all three hides
                     nothing.
    a declaration    or an assign target: reporting `skipped` for `\\helper ` says the
                     thing is absent when it is there. That is a wrong answer, not a
                     convenience.

So this part refuses only where the present answer lies, and leaves the instance spellings
alone.

THE TRAP #103 ALREADY WALKED INTO

A spelling test alone breaks a working operation. The adapter reports an escaped instance
as `u/esc`, which is **not** a simple identifier, and `remove_instance('u/esc')` works
today. Requiring a simple identifier would make that instance unreachable, exactly as the
first attempt at #103 made `\\my-net ` unreachable. So a name the module lists is acted on
whatever its characters.

WHAT VALIDATES AN ASSIGN TARGET

Not `driven_bases`. It is a tokeniser, and it answers `['a', 'name']` for `'not a name!!'`
and `['bad']` for `'1bad'`. `check_expression` is the right primitive and already exists.
Measured:

    y, y[3], {a, b}, bus.x, y[3:0]       -> valid
    '', 'not a name!!', '1bad', 'module' -> "it cannot be read as one expression"
    '\\y '                                -> VALID, so the backslash needs its own rule

Rules
    T1  `remove_assign` refuses a target that begins with a backslash. When the module
        assigns the unescaped name, the message names it, so the author learns what to
        write.
    T2  `remove_assign` refuses a target that is not one expression: the empty string, a
        name with a space or punctuation, one that starts with a digit, a bare keyword.
    T3  `remove_assign` refuses a target that is not text.
    T4  `remove_assign` still accepts every form it can act on, as the design writes it.
    T5  `remove_assign` still reports `skipped` for a valid target the module does not
        assign. That is what `skipped` is for.
    T6  `remove_instance` acts on a name the module lists, whatever its characters.
    T7  `remove_instance` refuses a name the module does not list and that cannot be an
        identifier. A valid name the module does not list stays `skipped`.
    T8  The three spellings of an escaped instance name still work. This part must not
        undo #54, and rule G2 is the specification; this is the guard against doing it by
        accident from here.
    T9  Every refusal happens when the operation is called, with nothing queued in
        `vet.log` and no file changed.

Not checked
    A target that names a signal of only one configuration, which is #102. A hierarchical
    target such as `other.sig`, which no operation accepts today.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session

LEAF = "module leaf (input logic d, output logic q);\n  assign q = d;\nendmodule\n"

TOP = (
    "module top (input logic a, input logic b, output logic y, output logic z,\n"
    "            output logic [3:0] wide, output logic p, output logic q);\n"
    "  logic w, v;\n"
    "  leaf \\u/esc  (.d(a), .q(w));\n"
    "  leaf u_plain (.d(a), .q(v));\n"
    "  assign y = w;\n"
    "  assign z = v;\n"
    "  assign wide[3:0] = {a, b, a, b};\n"
    "  assign {p, q} = {a, b};\n"
    "endmodule\n"
)

UNUSABLE = ["", "not a name!!", "1bad", "module", "has space"]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "leaf.sv").write_bytes(LEAF.encode())
    (tmp_path / "rtl" / "top.sv").write_bytes(TOP.encode())
    fl = tmp_path / "design.f"
    fl.write_bytes(b"rtl/leaf.sv\nrtl/top.sv\n")
    return fl


@pytest.fixture
def vet(root: Path) -> Vet:
    return Vet(Session(root, top="top"))


def statuses(vet: Vet) -> list[tuple[str, str]]:
    return [(r.op, r.status) for r in vet.log]


# --- T1: the escaped spelling of a target the module assigns --------------------------


def test_an_escaped_assign_target_is_refused(vet):
    """`y` is assigned, so `\\y ` is the escaped spelling of something that is there."""
    with pytest.raises(VetError) as e:
        vet.module("top").remove_assign("\\y ")
    text = str(e.value)
    assert text.startswith("remove_assign top: "), text
    assert "backslash" in text or "did you mean" in text.lower(), text


def test_an_escaped_target_the_module_does_not_assign_is_refused_too(vet):
    with pytest.raises(VetError):
        vet.module("top").remove_assign("\\nosuch ")


# --- T2, T3: a target that cannot be one expression -----------------------------------


@pytest.mark.parametrize("target", UNUSABLE)
def test_a_target_that_is_not_one_expression_is_refused(vet, target):
    with pytest.raises(VetError) as e:
        vet.module("top").remove_assign(target)
    assert str(e.value).startswith("remove_assign top: "), str(e.value)


def test_a_target_that_is_not_text_is_refused(vet):
    with pytest.raises(VetError):
        vet.module("top").remove_assign(None)


# --- T4: every form it can act on -----------------------------------------------------


@pytest.mark.parametrize("target", ["y", "wide[3:0]", "{p, q}"])
def test_a_target_the_module_assigns_still_works(vet, target):
    """A bit select and a concatenation are legal targets and must keep working.

    Requiring a plain identifier, as `add_assign` does, would refuse both.
    """
    vet.module("top").remove_assign(target)
    assert statuses(vet)[-1][1] in ("applied", "skipped"), statuses(vet)


# --- T5: a valid absent target is still skipped ---------------------------------------


def test_a_valid_target_that_is_not_assigned_is_still_skipped(vet):
    vet.module("top").remove_assign("nosuchsignal")
    assert statuses(vet) == [("remove_assign", "skipped")]


# --- T6, T7: remove_instance --------------------------------------------------------


def test_an_instance_the_module_lists_is_acted_on(vet):
    """`u/esc` is how the adapter reports `\\u/esc `, and it is not a simple identifier.

    A spelling test would make this instance unreachable, which is the mistake the first
    attempt at #103 made with `\\my-net `.
    """
    with pytest.raises(VetError, match="dangling"):
        vet.module("top").remove_instance("u/esc")


@pytest.mark.parametrize("name", ["", "not a name!!"])
def test_an_instance_name_that_cannot_be_an_identifier_is_refused(vet, name):
    with pytest.raises(VetError) as e:
        vet.module("top").remove_instance(name)
    assert str(e.value).startswith("remove_instance top"), str(e.value)


def test_a_valid_instance_name_that_is_absent_is_still_skipped(vet):
    vet.module("top").remove_instance("nosuchinst")
    assert statuses(vet) == [("remove_instance", "skipped")]


# --- T8: G2 is not undone -------------------------------------------------------------


@pytest.mark.parametrize("name", ["u/esc", "\\u/esc", "\\u/esc "])
def test_all_three_spellings_still_reach_the_escaped_instance(vet, name):
    """Rule G2 of `tests/ops/test_manual_findings.py`, guarded from here as well.

    Accepting all three was the fix for #54. This part refuses only where the answer lies,
    and an instance spelling does not lie, so all three must keep working.
    """
    inst = vet.module("top").instance(name)
    assert inst.paths == ["top.\\u/esc "], inst.paths


# --- T9: nothing queued on a refusal --------------------------------------------------


@pytest.mark.parametrize(
    ("op", "bad"),
    [("remove_assign", "\\y "), ("remove_assign", "not a name!!"), ("remove_instance", "not a name!!")],
)
def test_nothing_is_queued_when_the_name_is_refused(vet, root, op, bad):
    source = root.parent / "rtl" / "top.sv"
    before = source.read_bytes()
    with pytest.raises(VetError):
        getattr(vet.module("top"), op)(bad)
    assert vet.log == []
    vet.session.refresh()
    assert source.read_bytes() == before
