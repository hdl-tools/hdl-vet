"""The three removals and the name they are given (#103).

`Module.remove_wire`, `Module.remove_port` and `Module.remove_param` never checked the
name. Anything they did not find was reported as `skipped`, which says "the module has no
such thing". That is wrong for two kinds of name. One is a name that cannot be an
identifier at all, where `skipped` hides a mistake in the script. The other is the escaped
spelling of a name the module does hold, where `skipped` is untrue: IEEE 1800 section
5.6.1 says an escaped identifier made only of simple-identifier characters **is** that
simple identifier, so `\\helper ` is `helper`.

The additions have been strict about this all along, so the pair was asymmetric:

    add_wire('\\helper ')    -> VetError: '\\helper ' is not a simple identifier ...
    remove_wire('\\helper ') -> [('remove_wire', 'skipped')]      <- the wire is still there

Measured before this part, with `logic helper;`, a port `spare` and a parameter `P`:

    remove_wire   '\\helper '     -> [('remove_wire', 'skipped')]
    remove_wire   'not a name!!' -> [('remove_wire', 'skipped')]
    remove_wire   ''             -> [('remove_wire', 'skipped')]
    remove_port   '\\spare '      -> [('remove_port', 'skipped')]
    remove_param  '\\P '          -> [('remove_param', 'skipped')]

The empty string is not in the issue text. It behaves the same way and is covered here.

WHY THE TEST FOR THIS IS NOT "THE NAME MUST BE A SIMPLE IDENTIFIER"

A first version of this specification said exactly that, and a review showed it takes away
a working operation. The adapter reports a declaration's **identifier**, already unescaped.
A design may legally declare `logic \\my-net ;`, and the adapter reports that decl as
`my-net`. So `my-net` is the only spelling this API has ever accepted for that wire, and it
is not a simple identifier. The same holds for a port `\\wire ` and a parameter `\\type `,
reported as `wire` and `type`. Measured on the design of this file:

    decls  reported by the adapter: ['helper', 'loose', 'my-net']
    ports  reported by the adapter: ['a', 'spare', 'wire', 'y']
    params reported by the adapter: ['P', 'type']

A spelling test refuses all three and leaves no way to remove them inside the API. It also
refuses a net legally named `logic` in a design that carries `` `begin_keywords ``, which
hdl-vet parses, because the test lexes the name with the default keyword set.

So the name is judged against what the module declares, and the spelling test is only the
fallback for a name the module does not declare.

Owner decision, taken from the issue: refuse the escaped spelling, do not normalise it.
The operations take a name as the design spells it, and a silent rewrite would hide a real
mismatch between the script and the design.

Rules
    M1  A name the module declares is acted on, whatever its characters. `my-net`, `wire`
        and `type` above are removable, because that is how the adapter reports them.
    M2  A name that begins with a backslash is refused. This API takes the identifier, not
        the escaped spelling. When the module declares the unescaped name, the message
        names it, so the script author learns what to write.
    M3  A name the module does not declare and that cannot be an identifier is refused:
        the empty string, a name with a space or another blank, and a value that is not
        text. Changed by #119 (owner decision, 2026-11-22): before it, M3 also refused a
        name with a punctuation mark, a name that starts with a digit and a bare
        keyword. Each of those is a legal escaped identifier (`\\my-net `, `\\1bad `,
        `\\module `), so the refusal said something untrue about a name that could be
        there. The rule is now the one of `remove_instance` and `remove_assign` (#113).
    M4  A name the module does not declare and that could be an identifier is `skipped`.
        That is what `skipped` is for. Since #119 this holds for every name with no
        blank in it, simple identifier or not.
    M5  A name the module declares is still removed, as before.
    M6  Every refusal happens when the operation is called. Nothing is queued in
        `vet.log` and no file changes.

Not checked
    `Module.remove_assign` and `Module.remove_instance` are the same class and are not
    touched here. Both still answer `skipped` for a name they cannot act on, including the
    escaped spelling of a target the design does hold. They are recorded in their own
    issue. `Instance.connect`, `Instance.disconnect` and `Instance.set_param` are a
    different class: they already raise for a name the child does not have.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session

TOP = (
    "module top #(parameter int P = 1, parameter int \\type  = 3)\n"
    "            (input logic a, input logic spare, input logic \\wire , output logic y);\n"
    "  logic helper;\n"
    "  logic loose;\n"
    "  logic \\my-net ;\n"
    "  assign helper = a & spare;\n"
    "  assign y = helper ^ P[0];\n"
    "endmodule\n"
)

OPS = ["remove_wire", "remove_port", "remove_param"]

# a name the module declares, per operation: the adapter reports it unescaped
DECLARED_ESCAPED = {"remove_wire": "my-net", "remove_port": "wire", "remove_param": "type"}

# the escaped spelling of a name the module does declare plainly
ESCAPED = {"remove_wire": "\\helper ", "remove_port": "\\spare ", "remove_param": "\\P "}

# names that cannot be an identifier and that the module does not declare
UNUSABLE = ["", " ", "not a name!!", "has space", "tab\there", "line\nbreak"]

# names the module does not declare and that are legal as an escaped identifier (#119)
ABSENT_ESCAPABLE = ["no-such", "1bad", "module", "logic", "a/b", "not-a-name!!"]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "top.sv").write_bytes(TOP.encode())
    fl = tmp_path / "design.f"
    fl.write_bytes(b"rtl/top.sv\n")
    return fl


@pytest.fixture
def vet(root: Path) -> Vet:
    return Vet(Session(root, top="top"))


def statuses(vet: Vet) -> list[tuple[str, str]]:
    return [(r.op, r.status) for r in vet.log]


# --- M1: a declared name is acted on, whatever its characters -------------------------


def test_the_design_is_clean(vet):
    """The escaped declarations must not themselves upset the baseline."""
    assert vet.design.diagnostics() == []


@pytest.mark.parametrize("op", OPS)
def test_a_declared_name_that_needs_escaping_is_still_removed(vet, op):
    """`\\my-net `, `\\wire ` and `\\type ` are reported as `my-net`, `wire` and `type`.

    That is the only spelling this API accepts for them. A test of the spelling alone
    refuses all three and leaves no way to remove them, which a review found.
    """
    getattr(vet.module("top"), op)(DECLARED_ESCAPED[op])
    assert statuses(vet) == [(op, "applied")]


# --- M2: a backslash spelling is refused, and the message helps ----------------------


@pytest.mark.parametrize("op", OPS)
def test_the_escaped_spelling_of_a_declared_name_is_refused(vet, op):
    with pytest.raises(VetError) as e:
        getattr(vet.module("top"), op)(ESCAPED[op])
    text = str(e.value)
    assert text.startswith(f"{op} top: "), text
    # The repr of the escaped spelling already holds the plain name, so asking only for
    # the plain name would pass on any message at all. Ask for the advice instead.
    assert "backslash" in text or "did you mean" in text.lower(), (
        f"the message must tell the author what to write instead: {text}"
    )


@pytest.mark.parametrize("op", OPS)
def test_a_backslash_name_the_module_does_not_hold_is_also_refused(vet, op):
    with pytest.raises(VetError):
        getattr(vet.module("top"), op)("\\nosuch ")


# --- M3: an unusable name the module does not declare --------------------------------


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("name", UNUSABLE)
def test_a_name_that_cannot_be_an_identifier_is_refused(vet, op, name):
    with pytest.raises(VetError) as e:
        getattr(vet.module("top"), op)(name)
    assert str(e.value).startswith(f"{op} top: "), str(e.value)


@pytest.mark.parametrize("op", OPS)
def test_a_name_that_is_not_text_is_refused(vet, op):
    with pytest.raises(VetError):
        getattr(vet.module("top"), op)(None)


# --- M4: a valid name the module does not declare is still skipped -------------------


@pytest.mark.parametrize("op", OPS)
def test_a_valid_name_that_is_absent_is_still_skipped(vet, op):
    getattr(vet.module("top"), op)("nosuchname")
    assert statuses(vet) == [(op, "skipped")]


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("name", ABSENT_ESCAPABLE)
def test_an_absent_name_that_needs_escaping_is_skipped(vet, op, name):
    """#119: such a name could be in a module, so "absent" is the true answer."""
    getattr(vet.module("top"), op)(name)
    assert statuses(vet) == [(op, "skipped")]


def test_one_name_is_removed_where_it_is_declared_and_skipped_where_it_is_not(vet):
    """`my-net` is a wire of `top` and no port of it: the two halves must not drift apart."""
    vet.module("top").remove_port("my-net")
    vet.module("top").remove_wire("my-net")
    assert statuses(vet) == [("remove_port", "skipped"), ("remove_wire", "applied")]


@pytest.mark.parametrize("op", OPS)
def test_a_case_difference_is_still_skipped(vet, op):
    """SystemVerilog is case sensitive, so another case is another name."""
    getattr(vet.module("top"), op)("HELPER")
    assert statuses(vet) == [(op, "skipped")]


# --- M5: a plain declared name still works -------------------------------------------


def test_a_wire_the_design_holds_is_still_removed(vet):
    """`loose` is declared and nothing reads it, so the removal is clean."""
    vet.module("top").remove_wire("loose")
    assert statuses(vet) == [("remove_wire", "applied")]


# --- M6: nothing is queued and nothing is written -------------------------------------


@pytest.mark.parametrize("op", OPS)
def test_nothing_is_queued_when_the_name_is_refused(vet, op, root):
    source = root.parent / "rtl" / "top.sv"
    before = source.read_bytes()
    with pytest.raises(VetError):
        getattr(vet.module("top"), op)(ESCAPED[op])
    assert vet.log == []
    vet.session.refresh()
    assert source.read_bytes() == before
