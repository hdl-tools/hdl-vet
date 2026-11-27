"""The post-conditions `Vet.finish` evaluates on the final design."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hdl_vet.archive import need_dict, need_list, need_str
from hdl_vet.ops.common import OpRecord, _Check
from hdl_vet.ops.text import _same
from hdl_vet.session import Session
from hdl_vet.slang_adapter import AdapterError, Design, ModuleInfo, driven_bases, tokens
from hdl_vet.splice import SpliceError

_MISSING = "{}: module not found in the final design"


def _module_of(design: Design, mod: str) -> ModuleInfo | None:
    try:
        return design.module(mod)
    except AdapterError:
        return None


def _width_failure(label: str, design: Design, mod: str, name: str, width: str | None) -> str | None:
    """A width the operation wrote as `[W-1:0]` must not run upward in the last packed range of any instance (#130).

    A range that cannot be read (no packed dimension) fails too, and says so: an error is better than a wrong ok.
    """
    if width is None:
        return None
    bad, unread = [], []
    for path, dims in design.packed_ranges(mod, name):
        if not dims:
            unread.append(path)
        elif dims[-1][0] < dims[-1][1]:  # the operation writes its range after the type, so it is the last one
            bad.append(f"{path} (the range is [{dims[-1][0]}:{dims[-1][1]}])")
    found = []
    if unread:
        found.append(f"the range of the width {width!r} cannot be read in {', '.join(unread)}")
    if bad:
        found.append(f"the width {width!r} is 0 or less in {', '.join(bad)}")
    return f"{label}: {'; '.join(found)}" if found else None


def _ev_add_file(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    name, path = a
    mods = set(design.defined_in(Path(path)))
    if any("." in p and s.module in mods for p, s in design.snapshot().items()):
        return None
    return f"add_file {name}: none of its modules is instantiated"


def _ev_add_port(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, name, decl, width = a
    m = _module_of(design, mod)
    if m is None:
        return _MISSING.format(f"add_port {mod} {name}")
    port = next((p for p in m.ports if p.name == name), None)
    if port is None:
        return f"add_port {mod} {name}: port not found in the final design"
    text = session.text(m.path)[port.start : port.end].decode("utf-8", errors="replace")
    if not _same(text, decl):
        return f"add_port {mod} {name}: expected {decl!r}, found {text!r}"
    return _width_failure(f"add_port {mod} {name}", design, mod, name, width)


def _ev_add_param(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, name, default, type = a
    m = _module_of(design, mod)
    if m is None:
        return _MISSING.format(f"add_param {mod} {name}")
    p = next((p for p in m.params if p.name == name), None)
    if p is None or m.param_kinds.get(name) != "value":
        return f"add_param {mod} {name}: parameter not found in the final design"
    if not _same(p.default or "", default):
        return f"add_param {mod} {name}: expected default {default!r}, found {p.default!r}"
    if not _same(p.type_text, type):
        return f"add_param {mod} {name}: expected type {type!r}, found {p.type_text!r}"
    return None


def _ev_add_wire(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, name, type_text, width = a
    m = _module_of(design, mod)
    if m is None:
        return _MISSING.format(f"add_wire {mod} {name}")
    d = next((d for d in m.decls if d.name == name), None)
    if d is None:
        return f"add_wire {mod} {name}: declaration not found in the final design"
    if not _same(d.type_text, type_text):
        return f"add_wire {mod} {name}: expected type {type_text!r}, found {d.type_text!r}"
    return _width_failure(f"add_wire {mod} {name}", design, mod, name, width)


def _ev_add_assign(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, target, expr = a
    m = _module_of(design, mod)
    if m is None:
        return _MISSING.format(f"add_assign {mod} {target}")
    if not any(x.left == target and _same(x.right, expr) for x in m.assigns):
        return f"add_assign {mod} {target}: the assignment 'assign {target} = {expr};' is missing"
    return None


def _ev_override_net(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, net, enable, _value, keep, _waiver = a
    m = _module_of(design, mod)
    if m is None:
        return _MISSING.format(f"override_net {mod} {net}")
    # The text may have been edited by hand since (the proof judges what it does), so only the mux is looked for.
    if not any(x.left == net and {enable, keep} <= set(tokens(x.right)) for x in m.assigns):
        return f"override_net {mod} {net}: no assignment of {net} uses {enable} and {keep} any more"
    return None


def _ev_insert_cell_on_net(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, net, _enable, _waiver, name, cell, keep, wires, params, keep_type = a
    label = f"insert_cell_on_net {mod}.{net}"
    m = _module_of(design, mod)
    if m is None:
        return _MISSING.format(label)
    inst = next((i for i in m.instances if i.name == name), None)
    if inst is None:
        return f"{label}: the instance {name!r} is not in the final design"
    if inst.module != cell:
        return f"{label}: the instance {name!r} is of {inst.module!r}, expected {cell!r}"
    have = {c.port: c.expr for c in inst.connections}
    for pin, expr in json.loads(wires).items():
        if pin not in have or not _same(have[pin], expr):
            return f"{label}: pin {pin!r} of {name!r} expected {expr!r}, found {have.get(pin)!r}"
    expected = json.loads(params)
    over = {o.name: o.value for o in inst.param_overrides}
    if over.keys() != expected.keys() or not all(_same(over[k], x) for k, x in expected.items()):
        return f"{label}: the parameters of {name!r} expected {expected!r}, found {over!r}"
    decl = next((d for d in m.decls if d.name == keep), None)
    if decl is None:
        return f"{label}: the declaration of {keep!r} is not in the final design"
    if not _same(decl.type_text, keep_type):
        return f"{label}: {keep!r} expected type {keep_type!r}, found {decl.type_text!r}"
    return None


def _ev_propagate_param(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    name, to, before = a
    found = design.snapshot().get(to)
    after = None if found is None else found.params.get(name)
    if after != before:
        return f"propagate_param {name} {to}: the value in use changed from {before!r} to {after!r}"
    return None


def _ev_remove_instance(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, name = a
    now = _module_of(design, mod)
    if now is None:
        return _MISSING.format(f"remove_instance {mod}.{name}")
    if any(i.name == name for i in now.instances):
        return f"remove_instance {mod}.{name}: the instance is still there in the final design"
    return None


def _ev_remove_wire(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, name = a
    now = _module_of(design, mod)
    if now is None:
        return _MISSING.format(f"remove_wire {mod} {name}")
    if any(d.name == name for d in now.decls):
        return f"remove_wire {mod} {name}: the declaration is still there in the final design"
    return None


def _ev_remove_assign(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, target = a
    now = _module_of(design, mod)
    if now is None:
        return _MISSING.format(f"remove_assign {mod} {target}")
    # The text, as before, OR the base name: `assign t[3:0] = ...` still drives `t`, and this
    # check is the only one left once `remove_assign` has retired the check of an `add_assign`
    # that it undid (#57). The text test has to stay, because `remove_assign` takes a target
    # as the design spells it, and `driven_bases` answers only for a plain identifier, so on
    # its own it would make the check vacuous for a target such as `w[3:0]` or an escaped name.
    if any(x.left == target or target in driven_bases(x.left) for x in now.assigns):
        return f"remove_assign {mod} {target}: the assignment is still there in the final design"
    return None


def _ev_remove_port(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, name = a
    now = _module_of(design, mod)
    if now is None:
        return _MISSING.format(f"remove_port {mod} {name}")
    if any(p.name == name for p in now.ports):
        return f"remove_port {mod} {name}: the port is still there in the final design"
    return None


def _ev_remove_param(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, name = a
    now = _module_of(design, mod)
    if now is None:
        return _MISSING.format(f"remove_param {mod} {name}")
    if any(p.name == name for p in now.params):
        return f"remove_param {mod} {name}: the parameter is still there in the final design"
    return None


def _ev_conn(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    """`text` is the expression a `connect` wants; None: a `disconnect` wants no connection."""
    mod, name, port, text = a
    op = "disconnect" if text is None else "connect"
    try:
        inst = design.module(mod).instance(name)
    except AdapterError:
        return f"{op} {mod}.{name} {port}: instance not found in the final design"
    c = next((c for c in inst.connections if c.port == port), None)
    found = None if c is None else c.expr
    if text is None:
        ok, expect = found is None or not tokens(found), "no connection"
    else:
        ok, expect = found is not None and _same(found, text), repr(text)
    return None if ok else f"{op} {mod}.{name} {port}: expected {expect}, found {found!r}"


def _ev_set_param(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, iname, name, text = a
    try:
        found = design.module(mod).instance(iname).param_override(name).value
    except AdapterError:
        return f"set_param {mod}.{iname} {name}: override not found in the final design"
    if not _same(found, text):
        return f"set_param {mod}.{iname} {name}: expected {text!r}, found {found!r}"
    return None


def _ev_replace_module(session: Session, a: tuple[Any, ...], design: Design) -> str | None:
    mod, inst, module = a
    try:
        found = design.module(mod).instance(inst).module
    except AdapterError:
        return f"replace_module {mod}.{inst}: instance not found in the final design"
    if found != module:
        return f"replace_module {mod}.{inst}: expected module {module!r}, found {found!r}"
    return None


# kind -> (evaluator, number of arguments)
_EVALUATORS: dict[str, tuple[Callable[[Session, tuple[Any, ...], Design], str | None], int]] = {
    "add_file": (_ev_add_file, 2),
    "add_port": (_ev_add_port, 4),
    "add_param": (_ev_add_param, 4),
    "add_wire": (_ev_add_wire, 4),
    "add_assign": (_ev_add_assign, 3),
    "override_net": (_ev_override_net, 6),
    "insert_cell_on_net": (_ev_insert_cell_on_net, 10),
    "propagate_param": (_ev_propagate_param, 3),
    "remove_instance": (_ev_remove_instance, 2),
    "remove_wire": (_ev_remove_wire, 2),
    "remove_assign": (_ev_remove_assign, 2),
    "remove_port": (_ev_remove_port, 2),
    "remove_param": (_ev_remove_param, 2),
    "conn": (_ev_conn, 4),
    "param": (_ev_set_param, 4),
    "replace_module": (_ev_replace_module, 3),
}


_DAMAGE = (KeyError, TypeError, ValueError, AttributeError, IndexError, SpliceError, RecursionError)


def _stored_check(data: object) -> _Check:
    """A post-condition from an archive's index; TypeError or ValueError when it is not one."""
    fields = need_dict(data)
    kind = need_str(fields["kind"])
    if kind not in _EVALUATORS:
        raise ValueError(f"unknown post-condition {kind!r}")
    key = tuple(need_str(x) for x in need_list(fields["key"]))
    args = tuple(None if x is None else need_str(x) for x in need_list(fields["args"], _EVALUATORS[kind][1]))
    if not key:
        raise ValueError(f"post-condition {kind!r} has no key")
    return _Check(key, kind, args)


def _read_vet_state(
    index: dict[str, Any],
) -> tuple[list[OpRecord], list[str], list[_Check], dict[tuple[str, str], set[str]]]:
    """The Vet's own part of an archive's index, type-checked."""
    log = [OpRecord(*(need_str(x) for x in need_list(e, 3))) for e in need_list(index["log"])]
    notes = [need_str(n) for n in need_list(index["notes"])]
    checks = [_stored_check(c) for c in need_list(index["checks"])]
    if len({c.key for c in checks}) != len(checks):
        raise ValueError("two post-conditions have the same key")
    swapped: dict[tuple[str, str], set[str]] = {}
    for entry in need_list(index["swapped"]):
        mod, name, mods = need_list(entry, 3)
        swapped[(need_str(mod), need_str(name))] = {need_str(m) for m in need_list(mods)}
    return log, notes, checks, swapped


def _evaluate(session: Session, check: _Check, design: Design) -> str | None:
    """The message of a failed post-condition on `design`, or None."""
    return _EVALUATORS[check.kind][0](session, check.args, design)
