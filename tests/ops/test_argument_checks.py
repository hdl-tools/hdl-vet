"""The arguments of an operation that are not names (#56).

A name has been checked where the operation is called since phase 5e. Every other argument was
written into the design as it came. A value of a wrong type was accepted in silence, and the run
failed later at the sign-off with the words of the compiler, or did not fail at all. Measured on
`main` at a27f9fe, before this part:

    add_wire('w2', width=True)            -> accepted, ok=True, writes `logic w2;`
    add_wire('w2', width='')              -> accepted, ok=True, writes `logic [-1:0] w2;`
    add_port('np', 'input', type='')      -> accepted, ok=True, writes `input np`
    add_port('np', 'input', type=None)    -> accepted, fails late: `input None np`
    add_assign('spare', None)             -> accepted, fails late: `assign spare = None;`
    connect('clk', 'a), .o2(y')           -> accepted, fails late: DuplicatePortConnection
    vet.module('top', instances=True)     -> accepted (True == 1)
    vet.permit(None)                      -> accepted, permits nothing, says nothing
    replace_module('child2', drop_ports='clk')  -> read as the names 'c', 'l', 'k'
    replace_module('child2', ports=5)     -> TypeError: 'int' object is not iterable
    vet.finish(scripts=None)              -> TypeError: Value after * must be an iterable
    vet.add_file(None)                    -> TypeError: argument should be a str or an os.PathLike
    punch_port('pp', 'input', to='top.u0', signal=5)  -> TypeError: expected string or bytes-like

Owner decision of 2026-11-19: check every such argument where the operation is called. A value
that was accepted before and is refused now is the intent.

RULES

A1  A refusal is a `VetError` at the call. It starts with the operation, and with the module or
    the instance where the operation has one (`add_port top: `, `connect top.u0`). It holds the
    name of the argument as the signature spells it. Nothing is queued in `vet.log`.
A2  A width (`add_port`, `add_wire`, `punch_port`) is None, an int of 1 or more, or a text that
    is not blank. A bool is not an int here. A text that is a whole number is judged as that
    number, so `"0"` and `"-1"` are refused.
A3  A type (`add_port`, `add_wire`, `add_param`, `punch_port`) is a text that is not blank.
    `punch_port` also takes None, its default.
A4  A value (`add_param` default, `propagate_param` default, `add_assign` expr, `connect` expr,
    `set_param` value, and each value of `connect=` and `set_params=` of `replace_module`) is a
    text, an int or a float that is a number (no `nan`, no `inf`: they were written as
    names). A bool, None, bytes, a list, a dict and any other object are refused. A blank
    text is refused, except for the expr of `connect`, which keeps its own answer.
    `propagate_param` also takes None, its default.
A5  The expr of `add_assign` and of `connect` is one expression with no comment
    (`slang_adapter.check_expression`, as `override_net` uses it for its value).
A6  A count (`Vet.module(instances=)`, `Module.instances(count=)`) is an int and no bool.
    `instances=` also takes None.
A7  A flag (`exact=` of `expect` and `permit`, `all_instances=` of `punch_port`) is a bool.
A8  The kind, the path and the name of `expect` and `permit` are texts that are not empty.
A9  `ports=`, `params=`, `connect=` and `set_params=` of `replace_module` are None or a
    mapping (a dict, or a read-only view of one) with text keys. The values of `ports=` and
    `params=` are texts. A list of pairs is no mapping. `drop_ports=`, `drop_params=`,
    `unconnected=` and `accept_types=` are a collection of texts: a list, a tuple, a set, the
    keys of a dict, a generator. One text is refused (it was read as its characters), and so
    are bytes and a mapping.
A10 A path (`add_file`, `save`, `restore`) is a text or a `Path`. The scripts of `finish`,
    `check` and `save` are pairs of two texts, and no text of a pair has a line break, as
    `str.splitlines` counts one (the name goes into a comment of every file that is written).
A11 A name that made a Python error is refused as a name: the signal of `punch_port`, the
    module of `scope`, the name of `Module.instance`, the module and the name of
    `Module.instances`, the name of `Vet.module` and of `Vet.module_paths`, the target of
    `add_assign`, the port of `connect` and of `disconnect`, the name of `set_param`, and the
    module of `replace_module`.

The review found A9 too narrow (a generator and the keys of a dict worked before), A11 too short,
and the `nan` of A4. It also found the first two of the limits below.

STATED LIMITS

- A width that is a text is not computed at the call. A text that the compiler reads as 0 or
  less (`"1-1"`, `"32'd0"`, `"$clog2(N)"` with `N` of 1) is accepted there. Since #130 the
  check of the operation fails the sign-off for it (tests/ops/test_width_check.py). Only a
  text that Python reads as a whole number is judged at the call.
- An argument that is an object whose `repr`, `str` or iteration raises gives that Python
  error and no `VetError`.

- A type and a width in brackets are not parsed at the call. A text such as `logic x1; logic`
  is still caught only at the sign-off, by the check of the operation.
- A value is not parsed at the call, because the value of a type parameter is a type and no
  expression. `set_param('W', '8;')` still fails at the sign-off and not at the call.
- The kind of `expect` and `permit` is not compared with the kinds that exist. A kind that is
  spelled wrong is a text, so it is accepted. For `expect` the sign-off then reports that the
  change was not seen. For `permit` nothing reports it.
- `Vet.restore` still raises `SessionError` for a file that is not there or cannot be read.
"""

from __future__ import annotations

import collections
import types
from pathlib import Path

import pytest

from hdl_vet.ops import Vet, VetError
from hdl_vet.session import Session

CHILD = (
    "module {name} #(parameter int W = 1) (input logic clk, input logic [W-1:0] d, output logic [W-1:0] q);\n"
    "  always_ff @(posedge clk) q <= d;\n"
    "endmodule\n"
)
TOP = (
    "module top #(parameter int N = 4) (input logic a, input logic [3:0] b, output logic y);\n"
    "  logic helper;\n"
    "  logic spare;\n"
    "  logic [3:0] z;\n"
    "  child #(.W(4)) u0 (.clk(a), .d(b), .q(z));\n"
    "  assign helper = a;\n"
    "  assign y = helper;\n"
    "endmodule\n"
)


@pytest.fixture
def vet(tmp_path: Path) -> Vet:
    (tmp_path / "rtl").mkdir()
    for name, text in (("child", CHILD.format(name="child")), ("child2", CHILD.format(name="child2")), ("top", TOP)):
        (tmp_path / "rtl" / f"{name}.sv").write_bytes(text.encode())
    filelist = tmp_path / "design.f"
    filelist.write_bytes(b"rtl/child.sv\nrtl/child2.sv\nrtl/top.sv\n")
    return Vet(Session(filelist, top="top"))


def refused(vet: Vet, call, prefix: str, argument: str) -> None:
    """A1: a `VetError` at the call, with the operation first and the argument named."""
    with pytest.raises(VetError) as e:
        call()
    text = str(e.value)
    assert text.startswith(prefix), text
    assert argument in text, f"the message must name {argument!r}: {text}"
    assert vet.log == []


def top(vet: Vet):
    return vet.module("top")


def u0(vet: Vet):
    return vet.module("top").instance("u0")


# --- A2: a width ------------------------------------------------------------------------

BAD_WIDTH = [True, False, [], {}, 1.5, b"x", "", "  ", 0, -1, "0", "-1"]
GOOD_WIDTH = [None, 1, 8, "8", "N", "[N-1:0]", "$clog2(N)"]

WIDTH_OPS = {
    "add_port": (lambda v, w: top(v).add_port("np", "input", width=w), "add_port top: "),
    "add_wire": (lambda v, w: top(v).add_wire("w2", width=w), "add_wire top: "),
    "punch_port": (lambda v, w: v.punch_port("pp", "input", to="top.u0", width=w), "punch_port"),
}


@pytest.mark.parametrize("op", WIDTH_OPS)
@pytest.mark.parametrize("width", BAD_WIDTH, ids=repr)
def test_a_width_that_cannot_be_one_is_refused(vet, op, width):
    call, prefix = WIDTH_OPS[op]
    refused(vet, lambda: call(vet, width), prefix, "width")


@pytest.mark.parametrize("op", ["add_port", "add_wire"])
@pytest.mark.parametrize("width", GOOD_WIDTH, ids=repr)
def test_a_width_that_can_be_one_is_taken(vet, op, width):
    WIDTH_OPS[op][0](vet, width)
    assert [(r.op, r.status) for r in vet.log] == [(op, "applied")]


# --- A3: a type -------------------------------------------------------------------------

BAD_TYPE = [[], {}, 1.5, True, b"x", 5, "", " "]

TYPE_OPS = {
    "add_port": (lambda v, t: top(v).add_port("np", "input", type=t), "add_port top: ", [None]),
    "add_wire": (lambda v, t: top(v).add_wire("w2", type=t), "add_wire top: ", [None]),
    "add_param": (lambda v, t: top(v).add_param("NP", 1, type=t), "add_param top: ", [None]),
    "punch_port": (lambda v, t: v.punch_port("pp", "input", to="top.u0", type=t), "punch_port", []),
}


@pytest.mark.parametrize("op", TYPE_OPS)
@pytest.mark.parametrize("kind", [*BAD_TYPE, None], ids=repr)
def test_a_type_that_is_no_text_is_refused(vet, op, kind):
    call, prefix, also = TYPE_OPS[op]
    if kind is None and None not in also:
        pytest.skip("None is the default of punch_port")
    refused(vet, lambda: call(vet, kind), prefix, "type")


@pytest.mark.parametrize("kind", ["logic", "wire", "bit"])
def test_a_type_that_is_a_text_is_taken(vet, kind):
    top(vet).add_wire("w2", type=kind)
    assert [(r.op, r.status) for r in vet.log] == [("add_wire", "applied")]


# --- A4: a value ------------------------------------------------------------------------

BAD_VALUE = [True, False, [], {}, b"x", ["a"], object(), float("nan"), float("inf"), float("-inf")]
BLANK = ["", "  "]

VALUE_OPS = {
    "add_param": (lambda v, x: top(v).add_param("NP", x), "add_param top: ", "default", [None, *BLANK]),
    "propagate_param": (
        lambda v, x: v.propagate_param("W", to="top.u0", default=x),
        "propagate_param",
        "default",
        BLANK,
    ),
    "add_assign": (
        lambda v, x: top(v).add_assign("spare", x, waiver="a test"),
        "add_assign top: ",
        "expr",
        [None, *BLANK],
    ),
    "connect": (lambda v, x: u0(v).connect("clk", x, was="a", waiver="a test"), "connect top.u0", "expr", [None]),
    "set_param": (
        lambda v, x: u0(v).set_param("W", x, was=4, waiver="a test"),
        "set_param top.u0",
        "value",
        [None, *BLANK],
    ),
    "replace_module connect": (
        lambda v, x: u0(v).replace_module("child2", connect={"clk": x}, waiver="a test"),
        "replace_module top.u0: ",
        "connect",
        [None, *BLANK],
    ),
    "replace_module set_params": (
        lambda v, x: u0(v).replace_module("child2", set_params={"W": x}, waiver="a test"),
        "replace_module top.u0: ",
        "set_params",
        [None, *BLANK],
    ),
}
VALUE_CASES = [(op, value) for op, spec in VALUE_OPS.items() for value in [*BAD_VALUE, *spec[3]]]


@pytest.mark.parametrize(("op", "value"), VALUE_CASES, ids=lambda x: x if isinstance(x, str) and x.strip() else repr(x))
def test_a_value_that_cannot_be_written_is_refused(vet, op, value):
    call, prefix, argument, _ = VALUE_OPS[op]
    refused(vet, lambda: call(vet, value), prefix, argument)


@pytest.mark.parametrize("value", [5, 0, -1, 1.5, "4", "N + 1"], ids=repr)
def test_a_number_and_a_text_are_values(vet, value):
    top(vet).add_param("NP", value)
    assert [(r.op, r.status) for r in vet.log] == [("add_param", "applied")]


@pytest.mark.parametrize("value", [8, "8", "N"], ids=repr)
def test_a_number_and_a_text_are_values_of_an_instance(vet, value):
    u0(vet).set_param("W", value, was=4, waiver="a test")
    assert [(r.op, r.status) for r in vet.log] == [("set_param", "applied")]


# --- A5: one expression -----------------------------------------------------------------

NOT_ONE_EXPRESSION = ["a;", "a b", "a // c", "a /* c */", "a), .o2(y", "a; assign y = 1"]


@pytest.mark.parametrize("expr", NOT_ONE_EXPRESSION)
def test_an_assignment_takes_one_expression(vet, expr):
    refused(vet, lambda: top(vet).add_assign("spare", expr, waiver="a test"), "add_assign top: ", "expr")


@pytest.mark.parametrize("expr", NOT_ONE_EXPRESSION)
def test_a_connection_takes_one_expression(vet, expr):
    refused(vet, lambda: u0(vet).connect("clk", expr, was="a", waiver="a test"), "connect top.u0", "expr")


@pytest.mark.parametrize("expr", ["~a", "a & b[0]", "{b[1], b[0]} == 2'b01", "1'b1", 0, "N > 2"], ids=repr)
def test_one_expression_is_taken(vet, expr):
    top(vet).add_assign("spare", expr, waiver="a test")
    u0(vet).connect("clk", expr, was="a", waiver="a test")
    assert [(r.op, r.status) for r in vet.log] == [("add_assign", "applied"), ("connect", "applied")]


# --- A6: a count ------------------------------------------------------------------------

BAD_COUNT = [True, False, 1.5, "1", [], b"x"]


@pytest.mark.parametrize("count", BAD_COUNT, ids=repr)
def test_the_count_of_a_module_is_an_int(vet, count):
    refused(vet, lambda: vet.module("top", instances=count), "module top: ", "instances")


@pytest.mark.parametrize("count", [*BAD_COUNT, None], ids=repr)
def test_the_count_of_instances_is_an_int(vet, count):
    module = top(vet)
    refused(vet, lambda: module.instances(module="child", count=count), "instances top: ", "count")


def test_a_count_that_is_an_int_is_compared(vet):
    assert vet.module("top", instances=1) is not None
    assert vet.module("top", instances=None) is not None
    assert len(top(vet).instances(module="child", count=1)) == 1
    with pytest.raises(VetError):
        vet.module("top", instances=2)
    with pytest.raises(VetError):
        top(vet).instances(module="child", count=2)


# --- A7: a flag -------------------------------------------------------------------------

BAD_FLAG = [None, 1, 0, "yes", []]


@pytest.mark.parametrize("flag", BAD_FLAG, ids=repr)
def test_exact_is_a_bool(vet, flag):
    refused(vet, lambda: vet.expect("port-added", exact=flag), "expect: ", "exact")
    refused(vet, lambda: vet.permit("port-added", exact=flag), "permit: ", "exact")


@pytest.mark.parametrize("flag", BAD_FLAG, ids=repr)
def test_all_instances_is_a_bool(vet, flag):
    refused(vet, lambda: vet.punch_port("pp", "input", to="top.u0", all_instances=flag), "punch_port", "all_instances")


# --- A8: the texts of expect and permit -------------------------------------------------

BAD_TEXT = [None, 5, [], b"x", ""]


@pytest.mark.parametrize("op", ["expect", "permit"])
@pytest.mark.parametrize("argument", ["kind", "path", "name"])
@pytest.mark.parametrize("value", BAD_TEXT, ids=repr)
def test_the_kind_the_path_and_the_name_are_texts(vet, op, argument, value):
    given = {"kind": "port-added", "path": "top", "name": "np", argument: value}
    refused(vet, lambda: getattr(vet, op)(given["kind"], given["path"], given["name"]), f"{op}: ", argument)


def test_expect_and_permit_take_texts(vet):
    vet.permit("port-added", "top", "np", exact=True)
    vet.permit("*")
    vet.expect("port-added", "top", "np")
    top(vet).add_port("np", "input")
    assert [(r.op, r.status) for r in vet.log] == [("add_port", "applied")]


# --- A9: the mappings and the lists of replace_module -----------------------------------

BAD_MAPPING = [0, "", [], "ab", 5, True, [("clk", "clk")], {1: "x"}]


@pytest.mark.parametrize("argument", ["ports", "params", "connect", "set_params"])
@pytest.mark.parametrize("value", BAD_MAPPING, ids=repr)
def test_a_mapping_of_replace_module_is_a_dict_with_text_keys(vet, argument, value):
    call = lambda: u0(vet).replace_module("child2", waiver="a test", **{argument: value})  # noqa: E731
    refused(vet, call, "replace_module top.u0: ", argument)


@pytest.mark.parametrize("argument", ["ports", "params"])
@pytest.mark.parametrize("value", [None, 5, True, ["clk"], b"clk"], ids=repr)
def test_a_new_name_in_a_mapping_is_a_text(vet, argument, value):
    key = "clk" if argument == "ports" else "W"
    call = lambda: u0(vet).replace_module("child2", waiver="a test", **{argument: {key: value}})  # noqa: E731
    refused(vet, call, "replace_module top.u0: ", argument)


BAD_LIST = [None, 5, True, "clk", b"x", [1], [None], {"clk": 1}]


@pytest.mark.parametrize("argument", ["drop_ports", "drop_params", "unconnected", "accept_types"])
@pytest.mark.parametrize("value", BAD_LIST, ids=repr)
def test_a_list_of_replace_module_is_a_list_of_texts(vet, argument, value):
    call = lambda: u0(vet).replace_module("child2", waiver="a test", **{argument: value})  # noqa: E731
    refused(vet, call, "replace_module top.u0: ", argument)


@pytest.mark.parametrize(
    "make",
    [
        lambda: types.MappingProxyType({"W": 4}),
        lambda: collections.OrderedDict(W=4),
    ],
    ids=["read-only view", "dict subclass"],
)
def test_a_mapping_that_is_no_plain_dict_is_taken(vet, make):
    u0(vet).replace_module("child2", set_params=make(), waiver="a test")
    assert [(r.op, r.status) for r in vet.log] == [("replace_module", "applied")]


@pytest.mark.parametrize(
    "make",
    [
        lambda: iter(()),
        lambda: (name for name in ()),
        lambda: {}.keys(),
        lambda: frozenset(),
    ],
    ids=["iterator", "generator", "keys of a dict", "frozenset"],
)
@pytest.mark.parametrize("argument", ["drop_ports", "drop_params", "unconnected", "accept_types"])
def test_a_collection_that_is_no_list_is_taken(vet, argument, make):
    u0(vet).replace_module("child2", waiver="a test", **{argument: make()})
    assert [(r.op, r.status) for r in vet.log] == [("replace_module", "applied")]


@pytest.mark.parametrize("make", [lambda: (name for name in ["clk", 5]), lambda: {"clk": 1, 5: 2}.keys()])
def test_a_collection_is_judged_by_what_it_holds(vet, make):
    call = lambda: u0(vet).replace_module("child2", waiver="a test", drop_ports=make())  # noqa: E731
    refused(vet, call, "replace_module top.u0: ", "drop_ports")


def test_replace_module_takes_what_the_rules_allow(vet):
    u0(vet).replace_module(
        "child2",
        ports={},
        params=None,
        connect={},
        set_params={"W": 4},
        drop_ports=[],
        drop_params=(),
        unconnected=set(),
        accept_types=[],
        waiver="a test",
    )
    assert [(r.op, r.status) for r in vet.log] == [("replace_module", "applied")]


# --- A10: paths and scripts -------------------------------------------------------------

BAD_PATH = [None, 5, [], b"x", 1.5]


@pytest.mark.parametrize("path", BAD_PATH, ids=repr)
def test_a_path_is_a_text_or_a_path(vet, path):
    refused(vet, lambda: vet.add_file(path), "add_file: ", "path")
    refused(vet, lambda: vet.save(path), "save: ", "path")
    with pytest.raises(VetError) as e:
        Vet.restore(path)
    assert str(e.value).startswith("restore: "), str(e.value)


BAD_SCRIPTS = [
    None,
    5,
    "ab",
    ["a"],
    [("a",)],
    [(1, 2)],
    [("a", "b", "c")],
    [("a\nb", "c")],
    [("a", "c\rd")],
    [("a" + chr(0x2028) + "b", "c")],
    [("a\x0bb", "c")],
    [("a", "c\x85d")],
]


@pytest.mark.parametrize("scripts", BAD_SCRIPTS, ids=repr)
def test_the_scripts_are_pairs_of_one_line_texts(vet, tmp_path, scripts):
    top(vet).add_wire("w2")
    for call, prefix in (
        (lambda: vet.check(scripts), "check: "),
        (lambda: vet.save(tmp_path / "s.hvz", scripts), "save: "),
        (lambda: vet.finish(scripts), "finish: "),
    ):
        with pytest.raises(VetError) as e:
            call()
        assert str(e.value).startswith(prefix), str(e.value)
        assert "scripts" in str(e.value), str(e.value)
    assert not (tmp_path / "s.hvz").exists()


def test_scripts_that_are_pairs_are_taken(vet):
    top(vet).add_wire("w2")
    result = vet.check([("apply.py", "0" * 64)])
    assert result is not None
    assert vet.check(()) is not None


# --- A11: a name that made a Python error -----------------------------------------------

NO_NAME = [5, [], b"x", True]


@pytest.mark.parametrize("signal", NO_NAME, ids=repr)
def test_the_signal_of_punch_port_is_a_text(vet, signal):
    refused(vet, lambda: vet.punch_port("pp", "output", to="top.u0", signal=signal), "punch_port", "signal")


@pytest.mark.parametrize("module", [None, *NO_NAME], ids=repr)
def test_the_module_of_a_scope_is_a_text(vet, module):
    with pytest.raises(VetError) as e, vet.scope(module):
        pass
    assert str(e.value).startswith("scope"), str(e.value)
    assert vet.log == []


@pytest.mark.parametrize("name", [None, *NO_NAME], ids=repr)
def test_the_name_of_an_instance_is_a_text(vet, name):
    module = top(vet)
    refused(vet, lambda: module.instance(name), "instance top: ", "name")


@pytest.mark.parametrize("argument", ["module", "name"])
@pytest.mark.parametrize("value", NO_NAME, ids=repr)
def test_the_filters_of_instances_are_texts(vet, argument, value):
    module = top(vet)
    refused(vet, lambda: module.instances(count=1, **{argument: value}), "instances top: ", argument)


NAME_CALLS = {
    "module": (lambda v, x: v.module(x), "module", "name"),
    "module_paths": (lambda v, x: v.module_paths(x), "module_paths", "module"),
    "add_assign": (lambda v, x: top(v).add_assign(x, "a", waiver="a test"), "add_assign top: ", "target"),
    "connect": (lambda v, x: u0(v).connect(x, "a", waiver="a test"), "connect top.u0", "port"),
    "disconnect": (lambda v, x: u0(v).disconnect(x), "disconnect top.u0", "port"),
    "set_param": (lambda v, x: u0(v).set_param(x, 5, waiver="a test"), "set_param top.u0", "name"),
    "replace_module": (
        lambda v, x: u0(v).replace_module(x, waiver="a test"),
        "replace_module top.u0: ",
        "module",
    ),
}


@pytest.mark.parametrize("op", NAME_CALLS)
@pytest.mark.parametrize("value", [None, *NO_NAME], ids=repr)
def test_a_name_that_is_no_text_is_refused(vet, op, value):
    call, prefix, argument = NAME_CALLS[op]
    refused(vet, lambda: call(vet, value), prefix, argument)
