"""A width that the compiler reads as 0 or less (#130).

`Module.add_port`, `Module.add_wire` and `Vet.punch_port` take a width as an int or as a text. A
text is written into the range as `[TEXT-1:0]`. When the compiler reads the text as 0 or less, the
declaration gets a range such as `[-1:0]`, which is legal and is 2 bits wide, and the sign-off
said ok. Measured on `main` at c1d31b0:

    top.add_wire("w2", width="$clog2(N)")      with `parameter int N = 1`
    -> ok=True, writes `logic [$clog2(N)-1:0] w2;`      a net of 2 bits, asked for 0

Rule A2 of tests/ops/test_argument_checks.py judges a text at the call only when Python reads it
as a whole number. Any other text is an expression of the design. Its value depends on the
parameters of each instance, so it is known after the elaboration and not before.

Owner decision of 2026-11-27: the check of the operation asks the elaborated design.

RULES

W1  The check that `add_port`, `add_wire` and `punch_port` register fails the sign-off when the
    range that the operation wrote as `[W-1:0]` has a left bound below its right bound in an
    instance of the module. So a width that the compiler reads as 0 or less fails.
W2  It is asked for every instance of the module. One instance with a width of 0 fails, also
    when another instance has a good width. The message names the path of the instance.
W3  The message starts with the operation and the module, as the other failed checks do, and it
    has the width as the script gave it and the range that the design has.
W4  A width that the script gives in brackets (`"[0:3]"`, `"[-1:0]"`) is the range of the script
    and is not judged. A width that is an int, or None, was judged at the call.
W5  A saved session keeps the check. The check stores the width, so the format of the archive
    is 6 (owner decision of 2026-11-27). An archive of format 5 is refused, as the older ones.
W6  Only the range that the operation wrote is judged. A type that brings a range of its own
    (`type="logic [0:3]"`) keeps it, whatever its direction: the range of the width comes after
    it, and that one is asked.
    A type that is a typedef with a range inside (`type="pk::nib_t"`) is the other way round: the
    range of the width is the outer one there. In both cases the range that is asked is the
    last one that the declaration has in its text.

The review found that the range did not always have the width that the text means:

W7  The width is the value of the text. A text of more than one token is written in parentheses,
    `[(W)-1:0]`, so that an operator of the text that binds less than `-` does not take the
    `-1` (`"N >> 1"` was written `[N >> 1-1:0]`, which is `[N >> 0:0]`; `"N ? 0 : 1"` was
    `[N ? 0 : 1-1:0]`). A text that is one name or one number is written as before:
    `[N-1:0]`, `[8-1:0]`, and `[pk::FOUR-1:0]` for a name with a package in front. That is
    decided from the expression, not from a search in the text.
W8  When the range of a declaration cannot be read from the design, the check fails and says
    that: "the range cannot be read". It does not say that the width is 0.
W9  A width text that is not in brackets is one expression with no comment, and the call
    refuses any other text. Without this rule the parentheses of W7 made a text such as
    `"1) + (N"` into a legal range, `[(1) + (N)-1:0]`, whose `-1` is again taken by the last
    part (found by the second review; before W7 such a text did not compile).

STATED LIMITS

- A width in front of a type whose range the design does not give, such as a net of the type
  `interconnect`, fails the check (W8), also when the width is good. Give the range in
  brackets there.

- The check looks at a declaration that an operation wrote. A raw splice that writes
  `logic [N-2:0] w;` is not judged.
- A width that is good in every instance of this run can be 0 after the design is generated
  again with other parameters. The script is then run again, and the check with it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_vet import equiv
from hdl_vet.ops import Vet
from hdl_vet.session import Session

PK = "package pk;\n  typedef logic [0:3] nib_t;\n  localparam int FOUR = 4;\nendpackage\n"
MID = "module mid #(parameter int N = 4) (input logic a, output logic y);\n  assign y = a;\nendmodule\n"
TOP = (
    "module top #(parameter int N = {n}) (input logic a, output logic y, output logic z);\n"
    "  mid #(.N(1)) u_one (.a(a), .y(y));\n"
    "  mid #(.N(4)) u_four (.a(a), .y(z));\n"
    "endmodule\n"
)


def vet_of(tmp_path: Path, n: int = 1) -> Vet:
    root = tmp_path / "design"
    (root / "rtl").mkdir(parents=True)
    (root / "rtl" / "pk.sv").write_bytes(PK.encode())
    (root / "rtl" / "mid.sv").write_bytes(MID.encode())
    (root / "rtl" / "top.sv").write_bytes(TOP.format(n=n).encode())
    (root / "design.f").write_bytes(b"rtl/pk.sv\nrtl/mid.sv\nrtl/top.sv\n")
    return Vet(Session(root / "design.f", top="top"))


@pytest.fixture(autouse=True)
def no_yosys(monkeypatch):
    monkeypatch.setattr(equiv, "available", lambda: False)


def failed(result, start: str) -> list[str]:
    return [str(c) for c in result.failed_checks if str(c).startswith(start)]


ZERO = ["$clog2(N)", "N-1", "1-1", "(0)", "32'd0", "'d0", "0_0", "1/2", "N - 2", "-(1)"]


# --- W1, W3: a width of 0 or less -----------------------------------------------------------------


@pytest.mark.parametrize("width", ZERO)
def test_a_wire_whose_width_is_0_or_less_fails_the_sign_off(tmp_path, width):
    vet = vet_of(tmp_path, n=1)
    vet.module("top").add_wire("w2", width=width)
    result = vet.finish()
    assert not result.ok
    (message,) = failed(result, "add_wire top w2: ")
    assert "width" in message, message
    assert width in message, message


@pytest.mark.parametrize("width", ["$clog2(N)", "1-1"])
def test_a_port_whose_width_is_0_or_less_fails_the_sign_off(tmp_path, width):
    vet = vet_of(tmp_path, n=1)
    vet.module("top").add_port("np", "output", width=width)
    result = vet.finish()
    assert not result.ok
    (message,) = failed(result, "add_port top np: ")
    assert "width" in message, message


def test_a_punched_port_whose_width_is_0_or_less_fails_the_sign_off(tmp_path):
    vet = vet_of(tmp_path)
    vet.module("mid", instances=2)
    vet.punch_port("pp", "input", to="top.u_one", width="1-1", all_instances=True)
    result = vet.finish()
    assert not result.ok
    assert any("width" in message for message in failed(result, "add_port ")), result.report()


@pytest.mark.parametrize("width", ["$clog2(N)", "N-1", "N", "N / 2", "2"])
def test_a_width_that_is_1_or_more_signs_off(tmp_path, width):
    vet = vet_of(tmp_path, n=4)
    vet.module("top").add_wire("w2", width=width)
    result = vet.finish()
    assert result.failed_checks == [], result.report()
    assert result.ok, result.report()


# --- W2: every instance ---------------------------------------------------------------------------


def test_one_instance_with_a_width_of_0_fails_and_is_named(tmp_path):
    """`mid` is elaborated with N of 1 and with N of 4. `$clog2(N)` is 0 in `top.u_one` only."""
    vet = vet_of(tmp_path)
    vet.module("mid", instances=2).add_wire("w2", width="$clog2(N)")
    result = vet.finish()
    assert not result.ok
    (message,) = failed(result, "add_wire mid w2: ")
    assert "top.u_one" in message, message
    assert "top.u_four" not in message, message


def test_a_width_that_is_good_in_every_instance_signs_off(tmp_path):
    vet = vet_of(tmp_path)
    vet.module("mid", instances=2).add_wire("w2", width="N + 1")
    result = vet.finish()
    assert result.ok, result.report()


# --- W4: what is not judged -----------------------------------------------------------------------


@pytest.mark.parametrize("width", ["[0:3]", "[-1:0]", "[N-2:0]", "[3:0][1:0]", 1, 8, None])
def test_a_range_of_the_script_and_a_number_are_not_judged(tmp_path, width):
    vet = vet_of(tmp_path, n=1)
    vet.module("top").add_wire("w2", width=width)
    result = vet.finish()
    assert result.failed_checks == [], result.report()
    assert result.ok, result.report()


# --- W6: only the range of the width --------------------------------------------------------------


def test_a_range_that_the_type_brings_is_not_judged(tmp_path):
    vet = vet_of(tmp_path, n=4)
    vet.module("top").add_wire("w2", width="N", type="logic [0:3]")
    result = vet.finish()
    assert result.failed_checks == [], result.report()
    assert result.ok, result.report()


def test_the_range_of_the_width_is_judged_behind_a_range_of_the_type(tmp_path):
    vet = vet_of(tmp_path, n=1)
    vet.module("top").add_wire("w2", width="$clog2(N)", type="logic [0:3]")
    result = vet.finish()
    assert not result.ok
    (message,) = failed(result, "add_wire top w2: ")
    assert "[-1:0]" in message, message


@pytest.mark.parametrize(("n", "width", "good"), [(4, "N", True), (1, "$clog2(N)", False), (4, "N - 4", False)])
def test_the_range_of_the_width_in_front_of_a_typedef(tmp_path, n, width, good):
    """`pk::nib_t` is `logic [0:3]`. `pk::nib_t [W-1:0] w2;`: the range of the width is the outer one."""
    vet = vet_of(tmp_path, n=n)
    vet.module("top").add_wire("w2", width=width, type="pk::nib_t")
    result = vet.finish()
    assert bool(failed(result, "add_wire top w2: ")) == (not good), result.report()
    assert result.ok == good, result.report()


# --- W7: the width is the value of the text -------------------------------------------------------


def text_of(vet: Vet) -> str:
    vet.session.refresh()
    vet._sync()
    return vet.text(next(p for p in vet.session.files if p.name == "top.sv")).decode()


@pytest.mark.parametrize(
    ("n", "width"),
    [(1, "N ? 0 : 1"), (1, "N == 1 ? 0 : 4"), (1, "N >> 1"), (1, "N & 0"), (4, "N < 8 ? 0 : 2")],
)
def test_a_width_with_an_operator_that_binds_less_than_minus_is_judged_as_a_whole(tmp_path, n, width):
    vet = vet_of(tmp_path, n=n)
    vet.module("top").add_wire("w2", width=width)
    assert f"[({width})-1:0]" in text_of(vet)
    result = vet.finish()
    assert not result.ok
    assert failed(result, "add_wire top w2: "), result.report()


@pytest.mark.parametrize(
    ("n", "width"), [(8, "N >> 1"), (4, "N << 1"), (4, "N ? 3 : 1"), (4, "N + 1"), (4, "$clog2(N)")]
)
def test_a_width_of_more_than_one_token_is_written_in_parentheses(tmp_path, n, width):
    vet = vet_of(tmp_path, n=n)
    vet.module("top").add_wire("w2", width=width)
    vet.module("top").add_port("np", "output", width=width)
    text = text_of(vet)
    assert text.count(f"[({width})-1:0]") == 2, text
    result = vet.finish()
    assert result.failed_checks == [], result.report()
    assert result.ok, result.report()


@pytest.mark.parametrize("width", ["N", "8", " N ", "pk::FOUR", "4'd8"])
def test_a_width_of_one_token_is_written_as_before(tmp_path, width):
    vet = vet_of(tmp_path, n=4)
    vet.module("top").add_wire("w2", width=width)
    text = text_of(vet)
    assert f"[{width.strip()}-1:0]" in text, text
    assert "(" + width.strip() + ")" not in text
    assert vet.finish().ok


# --- W9: one expression ---------------------------------------------------------------------------

NOT_ONE = [
    "1) + (N",
    "N) >> (1",
    "N) ? (0) : (1",
    "(N",
    "N)",
    "N -",
    "N, 2",
    "N ; wire q",
    "N // c",
    "N /* c */",
    "1 2",
]


@pytest.mark.parametrize("width", NOT_ONE)
def test_a_width_that_is_not_one_expression_is_refused(tmp_path, width):
    from hdl_vet.ops import VetError

    vet = vet_of(tmp_path, n=4)
    calls = {
        "add_wire top: ": lambda: vet.module("top").add_wire("w2", width=width),
        "add_port top: ": lambda: vet.module("top").add_port("np", "output", width=width),
        "punch_port": lambda: vet.punch_port("pp", "input", to="top.u_one", width=width),
    }
    for prefix, call in calls.items():
        with pytest.raises(VetError) as e:
            call()
        assert str(e.value).startswith(prefix), str(e.value)
        assert "width" in str(e.value), str(e.value)
    assert vet.log == []


@pytest.mark.parametrize("width", ["[N-1:0] /* a range of the script */", "[3:0]", "(N)", "N + (2 * N)", "{N}"])
def test_a_range_in_brackets_and_one_expression_are_taken(tmp_path, width):
    vet = vet_of(tmp_path, n=4)
    vet.module("top").add_wire("w2", width=width)
    assert [(r.op, r.status) for r in vet.log] == [("add_wire", "applied")]


# --- W8: a range that cannot be read --------------------------------------------------------------


def test_a_range_that_cannot_be_read_is_said_so(tmp_path):
    vet = vet_of(tmp_path, n=4)
    vet.module("top").add_wire("w2", width="N", type="interconnect")
    result = vet.finish()
    assert not result.ok
    (message,) = failed(result, "add_wire top w2: ")
    assert "cannot be read" in message, message
    assert "0 or less" not in message, message


# --- W5: a saved session --------------------------------------------------------------------------


def test_the_format_of_the_archive_is_6():
    from hdl_vet import archive

    assert archive.FORMAT == 6


def test_a_saved_session_keeps_the_check(tmp_path):
    vet = vet_of(tmp_path, n=1)
    vet.module("top").add_wire("w2", width="$clog2(N)")
    vet.save(tmp_path / "s.zip")
    back = Vet.restore(tmp_path / "s.zip")
    result = back.finish()
    assert not result.ok
    assert failed(result, "add_wire top w2: "), result.report()
