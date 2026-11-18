"""The way of an enable: from the module of a logic operation up to the top of the edited design.

A logic operation (`override_net`) has an enable. The proof ties it to 0, so the module is as before. This only
helps when, in the design as it is built, the enable is 0 in fact. `follow` looks at each instance of the module
in the edited design and at the pin of the enable. The rules are the docstring of
`tests/ops/test_override_levels.py` (H1 to H6). A pin is

- "off" when it is connected to a constant 0,
- "carried" when it is connected to the plain name of an input port of the parent that the parent of the input
  design lacks. The way goes on with the instances of the parent, for that port is an enable of the parent now,
- "not off" otherwise. The way ends there, and the sign-off fails unless every operation that uses the enable
  has a waiver (then the record is "waived").

This is the connection as the adapter reads it from the edited design (`hdl_vet.slang_adapter`), by the
connection only: a gate, a register or a wire on the way makes a pin "not off". No Yosys is needed.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from hdl_vet.slang_adapter import AdapterError, Design

if TYPE_CHECKING:
    from hdl_vet.session import Session

__all__ = ["Enable", "Ways", "follow"]


class _Op(Protocol):
    """What `follow` needs of a logic operation (`hdl_vet.prove.LogicOp`)."""

    @property
    def module(self) -> str: ...

    @property
    def enable(self) -> str: ...

    @property
    def waiver(self) -> str | None: ...

    @property
    def label(self) -> str: ...


@dataclass(frozen=True)
class Enable:
    """The pin of an enable on one instance, and what became of it.

    `status` is "off", "carried", "not off" or "waived". `operations` are the labels of the logic operations
    that use the enable on the way, in the order of the script.
    """

    module: str
    instance: str
    pin: str
    connection: str
    status: str
    operations: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status != "not off"

    def line(self) -> str | None:
        """The line of the report, or None for a pin that is fine."""
        if self.status not in ("not off", "waived"):
            return None
        says = f"is connected to {self.connection}" if self.connection else "is not connected"
        what = f"{self.module}.{self.instance}.{self.pin} {says} ({', '.join(self.operations)})"
        return f"  enable not off: {what}" if self.status == "not off" else f"  waived: enable {what}"

    def manifest(self) -> dict[str, str]:
        return {
            "module": self.module,
            "instance": self.instance,
            "pin": self.pin,
            "connection": self.connection,
            "status": self.status,
        }


@dataclass(frozen=True)
class Ways:
    """`records`: each pin that was looked at. `carried`: for a module, the names of its ports that carry an enable."""

    records: list[Enable] = field(default_factory=list)
    carried: dict[str, list[str]] = field(default_factory=dict)


def follow(session: Session, design: Design, ops: Sequence[_Op]) -> Ways:
    """The way of each enable of `ops` up to the top, over every elaborated instance of the edited `design`.

    The adapter resolves each connection: a name is the port of the parent only when the compiler resolves it
    to that port (a local signal of a generate block with the same name is not). Each elaboration of the parent
    counts, so one instance of the module can give several records.
    """
    old_ports: dict[str, set[str]] = {}

    def baseline_ports(module: str) -> set[str] | None:
        if module not in old_ports:
            try:
                old_ports[module] = {p.name for p in session.baseline.module(module).ports}
            except AdapterError:
                return None  # a module that the input design lacks: its ports are not the parent's
        return old_ports[module]

    order = {op.label: number for number, op in enumerate(ops)}
    waivers = {op.label: op.waiver for op in ops}
    work: deque[tuple[str, str, list[str]]] = deque((op.module, op.enable, [op.label]) for op in ops)
    found: dict[tuple[str, str, str, str], tuple[str, list[str]]] = {}  # (parent, instance, pin, text) -> status, ops
    carried: dict[str, list[str]] = {}
    seen: set[tuple[str, str, str, str]] = set()
    while work:
        child, pin, labels = work.popleft()
        for conn in design.pin_connections(child, pin):
            key = (conn.parent, conn.instance, pin, conn.text)
            if key not in found:
                old = baseline_ports(conn.parent)
                if conn.kind == "zero":
                    status = "off"
                elif conn.kind == "port" and old is not None and conn.port not in old:
                    status = "carried"
                else:
                    status = "not off"
                found[key] = (status, [])
            status, operations = found[key]
            new = [name for name in labels if name not in operations]
            if not new and key in seen:
                continue
            seen.add(key)
            operations.extend(new)
            if status == "carried":
                if conn.port not in carried.setdefault(conn.parent, []):
                    carried[conn.parent].append(conn.port)
                work.append((conn.parent, conn.port, new))
    records: list[Enable] = []
    for (parent, instance, pin, text), (status, operations) in found.items():
        operations.sort(key=order.__getitem__)
        if status == "not off" and all(waivers[name] is not None for name in operations):
            status = "waived"
        records.append(Enable(parent, instance, pin, text, status, operations))
    return Ways(records, carried)
