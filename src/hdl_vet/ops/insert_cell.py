"""`Module.insert_cell_on_net`: an instance of a cell between the driver of a net and its readers.

The rules are the docstring of `tests/ops/test_insert_cell.py`. The net, its driver, the signal `keep`, the enable
and the waiver are as for `override_net` (`hdl_vet.ops.override` holds the code that both use). The operation writes
the declaration of `keep`, makes the driver drive `keep`, and adds the instance of the cell after the driver. The
sign-off proves the edited module with `hdl_vet.prove`, with the enable tied to 0.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import TYPE_CHECKING

from hdl_vet.ops import args
from hdl_vet.ops.common import DriftError, VetError, _Check, _Plan
from hdl_vet.ops.override import (
    _Fail,
    _failer,
    _find_driver,
    _guards,
    _old_ports,
    _one_bit,
    _refuse_other_pattern,
    _retarget,
    _rewire,
    _site,
    _widths,
)
from hdl_vet.ops.raw import _check_waiver
from hdl_vet.ops.text import _did_you_mean, _norm, _same, _segment
from hdl_vet.slang_adapter import (
    AdapterError,
    ModuleInfo,
    is_simple_identifier,
    tokens,
    value_head_identifiers,
    value_identifiers,
)

if TYPE_CHECKING:
    from hdl_vet.ops.module import Module

_OP = "insert_cell_on_net"


def insert_cell_on_net(
    module: Module,
    net: str,
    cell: str,
    name: str,
    data_in: str,
    data_out: str,
    enable_pin: str,
    enable: str,
    connect: dict[str, object] | None,
    unconnected: Iterable[str],
    params: dict[str, object] | None,
    keep: str | None,
    waiver: str | None,
) -> None:
    """The body of `Module.insert_cell_on_net`."""
    v, mod = module.vet, module.name
    v._refuse_scope(_OP, mod)
    label = f"{_OP} {mod}"
    for arg, value in (
        ("net", net),
        ("cell", cell),
        ("name", name),
        ("data_in", data_in),
        ("data_out", data_out),
        ("enable_pin", enable_pin),
        ("enable", enable),
    ):
        args.name(label, arg, value)
    args.name(label, "keep", keep, none=True)
    _check_waiver(label, waiver)
    args.mapping(label, "connect", connect, values=True)
    args.mapping(label, "params", params, values=True)
    open_pins = args.texts(label, "unconnected", unconnected)
    pins = {k: str(x) for k, x in (connect or {}).items()}
    given = {k: str(x) for k, x in (params or {}).items()}
    for arg, texts in (("connect", pins), ("params", given)):
        for key, expr in texts.items():
            args.expression(label, f"{arg}[{key!r}]", expr)
            if len(expr.splitlines()) > 1 or expr.endswith((chr(10), chr(13))):
                raise VetError(f"{label}: {arg}[{key!r}] must be on one line")
    if keep is not None and not is_simple_identifier(keep):
        raise VetError(f"{label}: {keep!r} is not a simple identifier (or is a SystemVerilog keyword)")
    if not is_simple_identifier(name):
        raise VetError(
            f"{label}: the instance name {name!r} is not a simple identifier (or is a SystemVerilog keyword)"
        )
    module._confirm_instances(_OP)
    v._checked(
        _OP,
        f"{mod}.{net}",
        lambda: _run(
            module, net, cell, name, (data_in, data_out, enable_pin), enable, pins, open_pins, given, keep, waiver
        ),
        tried=True,
    )


def _run(
    module: Module,
    net: str,
    cell: str,
    name: str,
    three: tuple[str, str, str],
    enable: str,
    pins: dict[str, str],
    open_pins: list[str],
    given: dict[str, str],
    keep: str | None,
    waiver: str | None,
) -> None:
    v, mod = module.vet, module.name
    data_in, data_out, enable_pin = three
    target = f"{mod}.{net}"
    fail = _failer(_OP, mod)
    info, port, decls, keep_name = _site(module, _OP, net, enable, keep, fail)
    wires = {data_in: keep_name, enable_pin: enable, **pins, data_out: net, **dict.fromkeys(open_pins, "")}

    # A second run: the instance is there, with this cell and these connections.
    there = next((i for i in info.instances if i.name == name), None)
    if there is not None:
        have = {c.port: c.expr for c in there.connections}
        over = {o.name: o.value for o in there.param_overrides}
        if (
            there.module == cell
            and have.keys() == wires.keys()
            and all(_same(have[k], x) for k, x in wires.items())
            and over.keys() == given.keys()
            and all(_same(over[k], x) for k, x in given.items())
        ):
            key = (_OP, mod, name)  # the waiver of the latest call counts, as for an override
            v._checks = [
                _Check(c.key, c.kind, (*c.args[:3], waiver, *c.args[4:])) if c.key == key else c for c in v._checks
            ]
            v._record(_OP, target, "skipped")
            return
        if there.module != cell and not any(c.key == (_OP, mod, name) for c in v._checks):
            raise fail(f"the module already has an instance {name!r}, of {there.module!r}")
        raise DriftError(
            f"{_OP} {mod}: the instance {name!r} is there already, of {there.module!r} and not as this call "
            f"writes it (cell {cell!r}, connections {wires}, parameters {given})"
        )
    _refuse_other_pattern(module, net, (_OP, "override_net"), fail)
    _refuse_loop(module, net, keep_name, pins, fail)

    cinfo = _cell_info(module, cell, fail)
    _check_pins(cinfo, cell, three, pins, open_pins, given, fail)

    _guards(module, _OP, info, enable, keep_name, waiver, _old_ports(v, mod), fail)
    if (other := v._other_use(info, name, "instance")) is not None:
        raise fail(f"the name {name!r} is already used by {other}")
    v._refuse_disabled(_OP, mod, info, name)
    found = _find_driver(module, info, net, fail, _OP)
    widths = {w for (w,) in _widths(module, info, [net], fail, _OP)}
    if len(widths) != 1 or None in widths:
        raise fail(f"the width of {net!r} cannot be found, or it is not the same in every elaboration of the module")
    (width,) = widths

    shown = f" #({', '.join(f'.{k}({x})' for k, x in given.items())})" if given else ""
    lines = [f"{cell}{shown} {name} ("]
    lines += [f"  .{pin}({expr})" + ("," if i < len(wires) - 1 else "") for i, (pin, expr) in enumerate(wires.items())]
    lines.append(");")
    splices, expects, type_text = _rewire(module, _OP, info, net, keep_name, port, decls, found, lines, fail)

    where = [f"{base}.{_segment(name)}" for base in module.paths]
    plan = _Plan(
        _OP,
        target,
        path=info.path,
        splices=splices,
        mark=("decls", mod),
        added=(mod, keep_name, ("decl", _norm(type_text))),
        expects=[*expects, *(("instance-added", p, "*") for p in where)],
        check=_Check(
            (_OP, mod, name),
            _OP,
            (mod, net, enable, waiver, name, cell, keep_name, json.dumps(wires), json.dumps(given), type_text),
        ),
    )
    moves = _retarget(module, net, keep_name, found, _OP)
    v._commit_all(_OP, [plan])
    if found.kind == "instance":
        v._mark("conns", mod, found.instance)
    v._mark("conns", mod, name)
    v._checks = [moves.get(id(c), c) for c in v._checks]  # cannot refuse
    for p in where:
        v.session.permit("instance-added", p, "*", below=True)  # what the cell holds

    # The pins that carry the net have its width. The edited design knows the width of each pin of the cell.
    try:
        v.session.refresh()
    except AdapterError as e:
        raise fail(f"the edited design cannot be loaded: {e}") from e
    v._sync()
    for p in where:
        snap = v.session.loaded.instance_snap(p)
        if snap is None:
            raise fail(f"there is no instance at {p} in the edited design")
        for arg, pin in (("data_in", data_in), ("data_out", data_out)):
            got = snap.ports.get(pin, ("", 0))[1]
            if got != width:
                raise fail(f"the width of {arg} {pin!r} is {got} bits, and {net!r} has {width}: they must be equal")


def _refuse_loop(module: Module, net: str, keep: str, pins: dict[str, str], fail: _Fail) -> None:
    """A connection must not read the net, which the cell drives: directly, or through other nets of the module."""
    try:
        closure = module.vet.session.loaded.combinational_closure(module.name, net)
    except AdapterError:
        closure = frozenset()  # a missing net is refused by the run with its own message
    for pin, expr in pins.items():
        if bad := module.vet.session.loaded.unfollowable_heads(
            module.name,
            value_head_identifiers(expr) - {keep},  # `keep` is declared by this call
        ):
            raise fail(
                f"connect[{pin!r}] {expr!r} reads {bad[0]!r}, which hdl-vet cannot follow: "
                "give an expression over the nets and the parameters of the module"
            )
        if net in value_identifiers(expr):
            raise fail(f"connect[{pin!r}] {expr!r} reads {net!r}, which would make a loop through the cell")
        if hit := min(closure & value_head_identifiers(expr), default=None):
            raise fail(
                f"connect[{pin!r}] {expr!r} reads {hit!r}, which {net!r} drives inside the module, "
                "so it would make a loop through the cell"
            )


def _cell_info(module: Module, cell: str, fail: _Fail) -> ModuleInfo:
    v = module.vet
    known = v.session.loaded.modules()
    if cell == module.name:
        raise fail(f"the cell {cell!r} is the module itself")
    if cell not in known:
        raise fail(f"no module {cell!r} in the design{_did_you_mean(cell, known)}")
    cinfo = v._info(cell)
    if cinfo.port_list_kind != "ansi" or cinfo.header_has_macros:
        raise fail(
            f"the cell {cell!r} has a non-ANSI port list, or one that comes from a macro, which is not supported"
        )
    if odd := [p.name for p in cinfo.ports if p.direction not in ("input", "output", "inout")]:
        raise fail(
            f"the cell {cell!r} has an interface or ref port ({', '.join(map(repr, odd))}), which is not supported"
        )
    return cinfo


def _check_pins(
    cinfo: ModuleInfo,
    cell: str,
    three: tuple[str, str, str],
    pins: dict[str, str],
    open_pins: list[str],
    given: dict[str, str],
    fail: _Fail,
) -> None:
    """The pins of the cell and its parameters: every problem at once, as `replace_module` says them."""
    data_in, data_out, enable_pin = three
    ports = {p.name: p for p in cinfo.ports}
    problems: list[str] = []
    for arg, pin, want in (
        ("data_in", data_in, "input"),
        ("data_out", data_out, "output"),
        ("enable_pin", enable_pin, "input"),
    ):
        p = ports.get(pin)
        if p is None:
            problems.append(f"{arg} {pin!r} is not a port of {cell!r}{_did_you_mean(pin, ports)}")
        elif p.direction != want:
            problems.append(f"{arg} {pin!r} is an {p.direction} port of {cell!r}, not an {want}")
        elif arg == "enable_pin" and not _one_bit(p.type_text):
            problems.append(f"enable_pin {pin!r} is not one bit wide (it is {p.type_text!r})")
    problems += [
        f"the pin {pin!r} is named twice among data_in, data_out and enable_pin"
        for pin in sorted(set(three))
        if three.count(pin) > 1
    ]
    for pin, expr in pins.items():
        if pin not in ports:
            problems.append(f"connect names {pin!r}, which is not a port of {cell!r}{_did_you_mean(pin, ports)}")
        elif pin in three:
            problems.append(f"connect names {pin!r}, which is one of data_in, data_out and enable_pin")
        elif ports[pin].direction == "output":
            problems.append(f"connect names {pin!r}, an output pin, which cannot take an expression")
        elif not tokens(expr):
            problems.append(f"connect gives pin {pin!r} an empty expression; use unconnected= for an output")
    for pin in open_pins:
        if pin not in ports:
            problems.append(f"unconnected names {pin!r}, which is not a port of {cell!r}{_did_you_mean(pin, ports)}")
        elif pin == data_out:
            problems.append(f"unconnected names {pin!r}, the data output, which drives the net")
        elif ports[pin].direction == "input":
            problems.append(f"unconnected names {pin!r}, an input pin, which cannot be left open")
        elif pin in pins:
            problems.append(f"pin {pin!r} is in both connect and unconnected")
    for pin, p in ports.items():
        if pin in three or pin in pins or pin in open_pins:
            continue
        how = "connect it with connect=" if p.direction == "input" else "leave it open by naming it in unconnected="
        problems.append(f"pin {pin!r} of {cell!r} is not accounted for: {how}")
    values = [n for n, k in cinfo.param_kinds.items() if k == "value"]
    problems += [
        f"params names {n!r}, which is not an overridable value parameter of {cell!r}{_did_you_mean(n, values)}"
        for n in given
        if n not in values
    ]
    if problems:
        raise fail("; ".join(problems))
