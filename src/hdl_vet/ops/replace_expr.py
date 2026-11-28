"""`Module.replace_expr`: another expression for a continuous assign.

The rules are the docstring of `tests/ops/test_replace_expr.py`. The operation writes the new expression in the place
of the right side of the one assign, and marks the module as a logic call: the sign-off proves it or takes a waiver.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from hdl_vet.ops import args
from hdl_vet.ops.common import VetError, _Check, _Plan
from hdl_vet.ops.override import refuse_loop
from hdl_vet.ops.raw import _check_waiver
from hdl_vet.ops.text import _IDENT, _did_you_mean, _norm, _same
from hdl_vet.slang_adapter import AdapterError, ModuleInfo, driven_bases, value_identifiers

if TYPE_CHECKING:
    from hdl_vet.ops.module import Module

_OP = "replace_expr"
_TO_EQUALS = re.compile(rb"(?:\s|//[^\n]*|/\*.*?\*/)*=", re.DOTALL)


def replace_expr(module: Module, target: str, expr: object, was: object | None, waiver: str | None) -> None:
    """The body of `Module.replace_expr`."""
    v, mod = module.vet, module.name
    label = f"{_OP} {mod}"
    v._refuse_scope(_OP, mod)
    _check_waiver(label, waiver)
    args.name(label, "target", target)
    args.expression(label, "expr", expr)
    module._confirm_instances(_OP)
    if not _IDENT.fullmatch(target):
        raise VetError(f"{label}: the target must be a plain identifier, not {target!r}")
    text = str(expr).strip()
    if target in value_identifiers(text):
        raise VetError(f"{label}: the expression {text!r} reads {target!r}, which would make a loop")
    v.session.refresh()  # drivers depend on everything queued
    v._sync()
    info = v._info(mod)
    assign = module._one_assign(_OP, info, target, was)
    if assign is None:
        raise VetError(f"{label}: {_why_none(module, info, target)}")
    if _same(text, assign.right):
        v._commit(_Plan(_OP, mod, skip=True))
        return
    v._refuse_disabled(_OP, mod, info, target)
    refuse_loop(module, _OP, target, text)
    data = v.session.text(info.path)
    # shortcut: a comment between `=` and the expression is refused, not kept
    eq = _TO_EQUALS.match(data, assign.start + len(target), assign.end)
    if eq is None or _norm(data[eq.end() : assign.end].decode("utf-8", "replace")) != _norm(assign.right):
        raise VetError(f"{label}: cannot find the expression of the assign to {target!r} in the text")
    start = eq.end() + len(data[eq.end() : assign.end]) - len(data[eq.end() : assign.end].lstrip())
    end = assign.end
    plan = _Plan(
        _OP,
        mod,
        path=info.path,
        splices=[(start, end, text.encode())],
        mark=("decls", mod),
        check=_Check((_OP, mod, target), _OP, (mod, target, text)),
    )
    v._logic_commit(plan, label, mod, f"{_OP} {mod}.{target}", waiver)


def _why_none(module: Module, info: ModuleInfo, target: str) -> str:
    """What the module says of `target` when no continuous assign drives it as a whole."""
    names = [*(p.name for p in info.ports), *(d.name for d in info.decls)]
    if target not in names:
        return f"no net, variable or port {target!r}{_did_you_mean(target, names)}"
    parts = [a.left for a in info.assigns if target in driven_bases(a.left)]
    if parts:
        return f"no continuous assign to the whole of {target!r}; it is assigned by selects ({', '.join(parts)})"
    try:
        kinds = {d.kind for _, found in module.vet.session.loaded.driver_details(module.name, target) for d in found}
    except AdapterError as e:
        return f"no continuous assign to {target!r}; cannot analyse it: {e}"
    if "process" in kinds:
        return f"no continuous assign to {target!r}; it is driven in an always block (use a raw splice)"
    if "instance" in kinds:
        by = [i.name for i in info.instances if any(target in driven_bases(c.expr) for c in i.connections)]
        return f"no continuous assign to {target!r}; it is driven by an instance ({', '.join(by) or '?'})"
    return f"no continuous assign to {target!r}" + ("" if kinds else "; it has no driver")
