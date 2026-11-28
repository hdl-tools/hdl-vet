"""`Module.override_net`: a mux on a net, the first operation that changes logic.

The rules are the docstring of `tests/ops/test_override.py`. The operation writes a declaration of the kept
signal, makes the driver of the net drive the kept signal, and adds `assign net = enable ? value : keep;`.
The sign-off proves the edited module with `hdl_vet.prove`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hdl_vet import equiv
from hdl_vet.ops.common import DriftError, VetError, _Check, _Plan
from hdl_vet.ops.text import (
    _did_you_mean,
    _eol,
    _header_line_in_comment,
    _line_start,
    _norm,
    _refuse_directive,
    _same,
    _segment,
)
from hdl_vet.slang_adapter import (
    AdapterError,
    ConnInfo,
    DeclInfo,
    ModuleInfo,
    PortInfo,
    check_expression,
    driven_bases,
    is_simple_identifier,
    tokens,
    value_head_identifiers,
    value_identifiers,
)

if TYPE_CHECKING:
    from hdl_vet.ops.module import Module
    from hdl_vet.ops.vet import Vet

_OP = "override_net"
_SCALARS = frozenset({"logic", "wire", "reg", "bit", "tri", "tri0", "tri1", "uwire", "wand", "wor", "signed"})
_DIMENSION = re.compile(r"\[([^\]]*)\]")
_ONE_BIT = re.compile(r"\s*(\d+)\s*:\s*(\d+)\s*")
_FILL = re.compile(r"'[01xXzZ]")
_Fail = Callable[[str], VetError]


@dataclass(frozen=True)
class _Found:
    """The driver of the net as text: an assign statement or the connection of an instance pin."""

    kind: str  # "assign" | "instance"
    stmt_start: int
    stmt_end: int
    left: int = -1  # assign: where the net is written on the left side
    conn: ConnInfo | None = None  # instance: the connection of the pin
    port: str = ""  # instance: the port of the pin
    instance: str = ""


def override_net(module: Module, net: str, value: object, enable: str, keep: str | None, waiver: str | None) -> None:
    """The body of `Module.override_net`."""
    v, mod = module.vet, module.name
    v._refuse_scope(_OP, mod)
    if not isinstance(net, str) or not isinstance(enable, str):
        raise VetError(f"{_OP} {mod}: the net and the enable must be names (text)")
    if waiver is not None and (not isinstance(waiver, str) or not waiver.strip()):
        raise VetError(f"{_OP} {mod}: the waiver must be a text that says why the check is not needed")
    if waiver is not None and waiver.splitlines() != [waiver]:
        raise VetError(f"{_OP} {mod}: the waiver must be one line")
    if "\n" in str(value) or "\r" in str(value):
        raise VetError(f"{_OP} {mod}: the value must be on one line")
    text = str(value).strip()
    if not text:
        raise VetError(f"{_OP} {mod}: the value is empty")
    reason = check_expression(text)
    if reason is not None:
        raise VetError(f"{_OP} {mod}: the value {text!r} is not one expression: {reason}")
    if net in value_identifiers(text):
        raise VetError(f"{_OP} {mod}: the value {text!r} reads {net!r}, which would make a loop")
    if keep is not None and (not isinstance(keep, str) or not is_simple_identifier(keep)):
        raise VetError(f"{_OP} {mod}: {keep!r} is not a simple identifier (or is a SystemVerilog keyword)")
    module._confirm_instances(_OP)
    refuse_loop(module, _OP, net, text)
    v._checked(_OP, f"{mod}.{net}", lambda: _run(module, net, text, enable, keep, waiver))


def refuse_loop(module: Module, op: str, net: str, text: str) -> None:
    """Refuse `text` when it reads what cannot be followed, or a net that `net` drives inside the module (a loop)."""
    if bad := module.vet.session.loaded.unfollowable_heads(module.name, value_head_identifiers(text)):
        raise VetError(
            f"{op} {module.name}: the value {text!r} reads {bad[0]!r}, which hdl-vet cannot follow: "
            "give a value over the nets and the parameters of the module"
        )
    try:
        closure = module.vet.session.loaded.combinational_closure(module.name, net)
    except AdapterError:
        closure = frozenset()  # a missing net is refused by the run with its own message
    if hit := min(closure & value_head_identifiers(text), default=None):
        raise VetError(
            f"{op} {module.name}: the value {text!r} reads {hit!r}, which {net!r} drives inside "
            "the module, so it would make a loop"
        )


def _run(module: Module, net: str, value: str, enable: str, keep: str | None, waiver: str | None) -> None:
    v, mod = module.vet, module.name
    target = f"{mod}.{net}"
    fail = _failer(_OP, mod)
    info, port, decls, keep_name = _site(module, _OP, net, enable, keep, fail)
    ports = {p.name: p for p in info.ports}
    _refuse_other_pattern(module, net, ("insert_cell_on_net",), fail)

    # A second run: the net is driven by the assign that this operation writes.
    wanted = f"{enable} ? {value} : {keep_name}"
    mine = [a for a in info.assigns if a.left == net]
    old_ports = _old_ports(v, mod)
    # One override for a net. What the run registered decides. A mux of the input design counts only
    # when it looks like the output of an earlier run: its select is a port that the input design has.
    key = ("override_net", mod, net)
    known = next((c for c in v._checks if c.key == key and c.kind == _OP), None)
    if known is not None:
        if known.args[2] == enable and known.args[4] == keep_name and _same(known.args[3], value):
            _set_waiver(module, net, waiver)  # the waiver of the latest call counts
            v._record(_OP, target, "skipped")
            return
        raise DriftError(
            f"{_OP} {mod}: {net!r} has an override already (enable {known.args[2]!r}, value {known.args[3]!r}), "
            f"and a net gets one override (this call asks for enable {enable!r}, value {value!r})"
        )
    if len(mine) == 1:
        if _same(mine[0].right, wanted):
            v._record(_OP, target, "skipped")
            return
        cond = _mux_condition(mine[0].right)
        shape = _mux_else(mine[0].right)
        if (
            cond is not None
            and cond in old_ports
            and ports.get(cond) is not None
            and ports[cond].direction == "input"
            and shape in (f"{net}_pre", keep_name)
        ):
            raise DriftError(
                f"{_OP} {mod}: {net!r} has an override already, 'assign {net} = {mine[0].right};', "
                f"and a net gets one override (this call asks for 'assign {net} = {wanted};')"
            )
    _guards(module, _OP, info, enable, keep_name, waiver, old_ports, fail)
    found = _find_driver(module, info, net, fail, _OP)
    _check_width(module, info, net, value, fail, _OP)
    splices, expects, type_text = _rewire(
        module, _OP, info, net, keep_name, port, decls, found, [f"assign {net} = {wanted};"], fail
    )

    plan = _Plan(
        _OP,
        target,
        path=info.path,
        splices=splices,
        mark=("decls", mod),
        added=(mod, keep_name, ("decl", _norm(type_text))),
        expects=expects,
        check=_Check(("override_net", mod, net), _OP, (mod, net, enable, value, keep_name, waiver)),
    )
    # Every refusal comes before the commit: `_retarget` only decides here and moves the checks after it.
    moves = _retarget(module, net, keep_name, found, _OP)
    v._commit_all(_OP, [plan])
    if found.kind == "instance":
        v._mark("conns", mod, found.instance)
    v._checks = [moves.get(id(c), c) for c in v._checks]  # cannot refuse


def _refuse_other_pattern(module: Module, net: str, kinds: tuple[str, ...], fail: _Fail) -> None:
    """One logic pattern on a net in a run: refuse a net that an operation of this run (one of `kinds`) changed."""
    mod = module.name
    for c in module.vet._checks:
        if c.kind in kinds and c.args[:2] == (mod, net):
            raise fail(f"{net!r} is changed by {c.kind} {mod}.{net} already: a net gets one cell or one override")


def _failer(op: str, mod: str) -> _Fail:
    return lambda message: VetError(f"{op} {mod}: {message}")


def _old_ports(v: Vet, mod: str) -> set[str]:
    try:
        return {p.name for p in v.session.baseline.module(mod).ports}
    except AdapterError:
        return set()


def _site(
    module: Module, op: str, net: str, enable: str, keep: str | None, fail: _Fail
) -> tuple[ModuleInfo, PortInfo | None, list[DeclInfo], str]:
    """The net as the module declares it, and the name of `keep`: the refusals that both operations share."""
    v, mod = module.vet, module.name
    v._refresh_all_dirty()  # the drivers depend on everything queued
    info = v._info(mod)
    ports = {p.name: p for p in info.ports}
    decls = [d for d in info.decls if d.name == net]
    port = ports.get(net)
    if port is None and not decls:
        if net in info.generate_decls:
            raise fail(f"{net!r} is declared inside a generate block, which is not supported")
        other = v._other_use(info, net, op)
        if other is not None:
            raise fail(f"{net!r} is {other}, not a net or variable")
        raise fail(f"no signal {net!r} in the module{_did_you_mean(net, [*ports, *(d.name for d in info.decls)])}")
    if port is not None:
        if port.direction == "input":
            raise fail(f"{net!r} is an input port, which the module cannot drive")
        if port.direction != "output":
            raise fail(f"{net!r} is an {port.direction} port, which is not supported")
        if info.port_list_kind != "ansi" or not port.type_text or port.unpacked:
            raise fail(f"the type of port {net!r} cannot be copied (an ANSI port without unpacked dimensions)")
    if len(decls) > 1:
        raise fail(f"{net!r} is declared {len(decls)} times")
    if decls and v.session.text(info.path)[decls[0].start : decls[0].end] != net.encode():
        raise fail(f"the declaration of {net!r} has an unpacked dimension or an initial value, which is not supported")
    _check_enable(fail, ports, enable)
    _refuse_inout(module, info, net, fail)
    keep_name = f"{net}_pre" if keep is None else keep
    if not is_simple_identifier(keep_name):
        raise fail(f"{keep_name!r} is not a simple identifier (or is a SystemVerilog keyword)")
    return info, port, decls, keep_name


def _guards(
    module: Module,
    op: str,
    info: ModuleInfo,
    enable: str,
    keep_name: str,
    waiver: str | None,
    old_ports: set[str],
    fail: _Fail,
) -> None:
    """What both operations refuse once a second run is ruled out: the enable, the names, a missing Yosys."""
    v, mod = module.vet, module.name
    if enable in old_ports:
        raise fail(
            f"the enable {enable!r} is a port of the input design. The proof ties only an enable that this run "
            f"adds (add_port or punch_port), so that the module is as before when it is 0"
        )
    other = v._other_use(info, keep_name, op)
    if other is not None:
        raise fail(f"the name {keep_name!r} is already used by {other}")
    v._refuse_disabled(op, mod, info, keep_name)
    if waiver is None and not equiv.available():
        raise fail(
            "the equivalence check needs Yosys, which is not installed (install the extra hdl-vet[equiv]); "
            "give the operation a waiver to go on without the check"
        )


def _rewire(
    module: Module,
    op: str,
    info: ModuleInfo,
    net: str,
    keep_name: str,
    port: PortInfo | None,
    decls: list[DeclInfo],
    found: _Found,
    lines: list[str],
    fail: _Fail,
) -> tuple[list[tuple[int, int, bytes]], list[tuple[str, str, str]], str]:
    """The splices that declare `keep`, make the driver drive it and write `lines` after the driver.

    Also the expectations of the structure comparison, and the type text of `keep`.
    """
    v, mod = module.vet, module.name
    v._refuse_disabled(op, mod, info, net)
    data = v.session.text(info.path)
    eol = _eol(data)
    splices: list[tuple[int, int, bytes]] = []

    # The declaration of the kept signal: after the declaration of the net, or after the header for a port.
    if port is not None:
        type_text = port.type_text
        nl = data.find(b"\n", info.header_end)
        if nl < 0 or nl + 1 > info.body_end:
            raise fail("the module header shares its last line with the body")
        if _header_line_in_comment(data, info.header_end, nl):
            raise fail("the module header line ends inside a block comment")
        at, indent = nl + 1, info.body_indent.encode()
    else:
        decl = decls[0]
        type_text = decl.type_text
        if decl.stmt_start < 0:
            raise fail(f"the declaration of {net!r} comes from a macro or an include")
        if data[decl.start : decl.end] != net.encode():
            raise fail(f"the declaration of {net!r} has an unpacked dimension or an initial value")
        at, indent = _after_line(data, decl.stmt_start, decl.stmt_end, info)
    if at < 0:
        raise fail(f"the declaration of {net!r} is on the last line of the file")
    splices.append((at, at, indent + f"{type_text} {keep_name};".encode() + eol))

    # The driver drives the kept signal, and the new statement follows the driver.
    expects: list[tuple[str, str, str]] = []
    if found.kind == "assign":
        if data[found.left : found.left + len(net)] != net.encode():
            raise fail(f"cannot find {net!r} on the left side of its assign statement")
        splices.append((found.left, found.left + len(net), keep_name.encode()))
    else:
        splices.append(_pin_splice(data, found, net, keep_name, fail))
        expects = [("connection-changed", f"{base}.{_segment(found.instance)}", found.port) for base in module.paths]
    at, indent = _after_line(data, found.stmt_start, found.stmt_end, info)
    if at < 0:
        raise fail("the driver of the net is on the last line of the file")
    splices.append((at, at, b"".join(indent + line.encode() + eol for line in lines)))
    return sorted(splices, key=lambda s: (s[0], s[1])), expects, type_text


def _retarget(module: Module, net: str, keep: str, found: _Found, op: str) -> dict[int, _Check]:
    """The driver now drives `keep`: the post-condition that named `net` for the same driver names `keep`.

    This is the check of an earlier `add_assign` of `net`, or of an earlier `connect` of the output pin.
    """
    v, mod = module.vet, module.name
    out: dict[int, _Check] = {}  # id of a check now registered -> the check that replaces it
    for c in v._checks:
        if found.kind == "assign" and c.kind == "add_assign" and c.args[:2] == (mod, net):
            moved = (c.key[0], c.key[1], keep, *c.key[3:])
            # This rewrite does not go through `_set_check`, so it has to refuse a key that
            # another post-condition already holds. An assignment and its removal share one
            # key, so a `keep` name an earlier operation of this run touched would give two
            # checks under one key. `save` accepts that and `restore` refuses it as damaged.
            if any(other.key == moved for other in v._checks if other is not c):
                raise VetError(
                    f"{op} {mod}: the keep name {keep!r} already has a post-condition from an "
                    f"earlier operation of this run; choose a name this run has not touched"
                )
            out[id(c)] = _Check(moved, c.kind, (mod, keep, *c.args[2:]))
        elif (
            found.kind == "instance"
            and c.kind == "conn"
            and c.args[:3] == (mod, found.instance, found.port)
            and c.args[3] == net
        ):
            out[id(c)] = _Check(c.key, c.kind, (*c.args[:3], keep))
    return out


def _set_waiver(module: Module, net: str, waiver: str | None) -> None:
    """Replace the waiver in the record of the override of `net`, in its place in the list."""
    v = module.vet
    key = ("override_net", module.name, net)
    v._checks = [
        _Check(c.key, c.kind, (*c.args[:5], waiver)) if c.key == key and c.kind == _OP else c for c in v._checks
    ]


def _refuse_inout(module: Module, info: ModuleInfo, net: str, fail: _Fail) -> None:
    """An inout pin is not an output pin: refuse a net that one connects."""
    v = module.vet
    for inst in info.instances:
        try:
            child = v._info(inst.module)
        except VetError:
            continue
        inouts = {p.name for p in child.ports if p.direction == "inout"}
        for i, conn in enumerate(inst.connections):
            if conn.port == "*":
                continue
            name = conn.port if conn.port is not None else (child.ports[i].name if i < len(child.ports) else None)
            if name in inouts and net in driven_bases(conn.expr):
                raise fail(f"{net!r} is connected to the inout pin {name!r} of {inst.name!r}, which is not supported")


def _check_width(module: Module, info: ModuleInfo, net: str, value: str, fail: _Fail, op: str) -> None:
    """The value has the width of the net. A fill literal (`'0`, `'1`, `'x`, `'z`) takes the width of the net."""
    if _FILL.fullmatch(value):
        return
    for have, want in _widths(module, info, [value, net], fail, op):
        if have is None or want is None:
            raise fail(f"the width of the value {value!r} cannot be found: it is not an expression of the module")
        if have != want:
            raise fail(f"the value {value!r} has a width of {have} bits, and {net!r} has {want}: they must be equal")


def _widths(module: Module, info: ModuleInfo, exprs: list[str], fail: _Fail, op: str) -> list[list[int | None]]:
    """`$bits` of each expression in the module, for each elaboration of it (None: not found).

    The adapter binds the probes in the module. They are added for the moment the design is loaded, and taken back.
    """
    v, mod = module.vet, module.name
    data = v.session.text(info.path)
    start = _line_start(data, info.body_end)
    at = start if not data[start : info.body_end].strip() else info.body_end
    names = [f"__vet_{i}" for i in range(len(exprs))]
    probe = " ".join(f"localparam int {n} = $bits(( {e} ));" for n, e in zip(names, exprs, strict=True))
    v._need_editable(f"{op} {mod}", info.path)
    state = v._begin(f"{op} {mod}")
    try:
        v._queue(info.path, [(at, at, probe.encode() + _eol(data))])
        v.session.refresh()
        sizes = v.session.loaded.parameter_values(mod, names)
    except AdapterError as e:
        raise fail(f"the width of {exprs[0]!r} cannot be found: {e}") from e
    finally:
        v._rollback(state)
    return [[found[n] for n in names] for found in sizes]


def _check_enable(fail: _Fail, ports: dict[str, PortInfo], enable: str) -> None:
    port = ports.get(enable)
    if port is None or port.direction != "input":
        raise fail(
            f"the enable {enable!r} is not an input port of the module{_did_you_mean(enable, ports)}; "
            f"add it first (add_port or punch_port)"
        )
    if not _one_bit(port.type_text):
        raise fail(f"the enable {enable!r} is not one bit wide (it is {port.type_text!r})")


def _one_bit(type_text: str) -> bool:
    """True when the type text of a port says one bit: a scalar type, or one dimension of two equal numbers."""
    dims = _DIMENSION.findall(type_text)
    if not dims:
        toks = tokens(type_text)
        return not toks or toks[0] in _SCALARS
    if len(dims) > 1:
        return False
    found = _ONE_BIT.fullmatch(dims[0])
    return found is not None and found.group(1) == found.group(2)


def _mux_else(right: str) -> str | None:
    """The name after the last colon of `a ? ... : b` when it is a plain name, else None."""
    toks = tokens(right)
    if len(toks) < 5 or toks[1] != "?" or toks[-2] != ":" or not is_simple_identifier(toks[-1]):
        return None
    return toks[-1]


def _mux_condition(right: str) -> str | None:
    """The name `a` of an expression that starts with `a ?`, else None."""
    toks = tokens(right)
    if len(toks) < 3 or toks[1] != "?" or not is_simple_identifier(toks[0]):
        return None
    return toks[0]


def _after_line(data: bytes, start: int, end: int, info: ModuleInfo) -> tuple[int, bytes]:
    """Where the line after the statement `start`..`end` begins (-1 if there is none), and the statement's indent."""
    prefix = data[_line_start(data, start) : start]
    indent = prefix if not prefix.strip(b" \t") else info.body_indent.encode()
    nl = data.find(b"\n", end)
    return (nl + 1 if nl >= 0 else -1), indent


def _find_driver(module: Module, info: ModuleInfo, net: str, fail: _Fail, op: str) -> _Found:
    """The one driver of the whole net, as text of the module; VetError for every other case."""
    v, mod = module.vet, module.name
    try:
        bodies = v.session.loaded.driver_details(mod, net)
    except AdapterError as e:
        raise fail(f"cannot analyse {net!r}: {e}") from e
    kinds: set[str] = set()
    for width, drivers in bodies:
        if not drivers:
            raise fail(f"{net!r} has no driver")
        seen = {d.kind for d in drivers}
        if "process" in seen:
            raise fail(f"{net!r} is driven in an always block, which is not supported")
        if seen - {"assign", "instance"}:
            raise fail(f"{net!r} has a driver that is no assign statement and no instance output pin")
        if len(drivers) > 1:
            raise fail(f"{net!r} has {len(drivers)} drivers: more than one driver, or drivers for parts of the net")
        first = drivers[0]
        if first.low != 0 or first.high != width - 1:
            raise fail(f"{net!r} has a driver for a part of it only (bits {first.low} to {first.high} of {width})")
        kinds |= seen
    if len(kinds) != 1:
        raise fail(f"the driver of {net!r} is not the same in every elaboration of the module")
    if kinds == {"assign"}:
        return _find_assign(info, net, fail)
    return _find_pin(module, info, net, fail, op)


def _find_assign(info: ModuleInfo, net: str, fail: _Fail) -> _Found:
    found = [a for a in info.assigns if net in driven_bases(a.left)]
    if not found:
        where = "in a generate block" if net in info.generate_assigned else "not plain text of the module"
        raise fail(f"the assign statement that drives {net!r} is {where}, which is not supported")
    if len(found) > 1:
        raise fail(f"{len(found)} assign statements drive {net!r}")
    assign = found[0]
    if assign.left != net:
        raise fail(f"the assign statement drives {net!r} as a part of {assign.left!r}, which is not supported")
    if assign.stmt_start < 0:
        raise fail(f"the assign statement that drives {net!r} comes from a macro or an include")
    return _Found("assign", assign.stmt_start, assign.stmt_end, assign.start)


def _find_pin(module: Module, info: ModuleInfo, net: str, fail: _Fail, op: str) -> _Found:
    v = module.vet
    cands = []
    for inst in info.instances:
        try:
            child = v._info(inst.module)
        except VetError:
            continue
        outputs = {p.name for p in child.ports if p.direction in ("output", "inout")}
        explicit = set()
        for i, conn in enumerate(inst.connections):
            if conn.port == "*":
                continue
            name = conn.port if conn.port is not None else (child.ports[i].name if i < len(child.ports) else None)
            explicit.add(name)
            if name in outputs and net in driven_bases(conn.expr):
                cands.append((inst, conn, name))
        if any(c.port == "*" for c in inst.connections) and net in outputs - explicit:
            raise fail(f"{net!r} is connected to the instance {inst.name!r} by .*, which is not supported")
    if any(inst.in_generate or inst.is_array for inst, _, _ in cands):
        raise fail(f"the instance that drives {net!r} is in a generate block or an instance array")
    if len(cands) != 1:
        raise fail(f"the instance pin that drives {net!r} is not plain text of the module")
    inst, conn, name = cands[0]
    if inst.from_macro or inst.path != info.path:
        raise fail(f"the instance {inst.name!r} comes from a macro or an include")
    _refuse_directive(f"{op} {module.name}", "connection", inst.conn_directive)
    return _Found("instance", inst.stmt_start, inst.stmt_end, -1, conn, str(name), inst.name)


def _pin_splice(data: bytes, found: _Found, net: str, keep: str, fail: _Fail) -> tuple[int, int, bytes]:
    conn = found.conn
    assert conn is not None  # noqa: S101 - a pin driver has its connection
    if conn.expr.strip() != net:
        raise fail(f"the pin {found.port!r} is connected to {conn.expr!r}, not to the whole net {net!r}")
    if conn.open is not None:
        return conn.open + 1, conn.end - 1, keep.encode()
    if conn.port is not None:  # `.port`: the implicit connection becomes explicit
        return conn.start, conn.end, f".{conn.port}({keep})".encode()
    return conn.start, conn.end, keep.encode()
