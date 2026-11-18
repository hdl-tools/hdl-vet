"""The checks of an `inert` child, on the netlists that `hdl_vet.equiv` reads (no Yosys, no pyslang).

`equiv.check(..., inert={"child": ["en_i"]})` keeps the child a black box on both sides. The gate's box has
the inputs that the gold's box lacks. Two things make the comparison sound, and both are decided here from the
netlists, not from the text of the design:

- The interface (`interfaces`): each instance of the child is on both sides, and apart from the named inputs
  the two boxes have the same ports (name, direction, width). The named inputs are only in the gate's box.
- The pins (`pin_problem`): after the ties and the flattening of the other children, each named input of each
  instance is the constant 0. A bit counts as 0 only when it is the constant 0 or a tied input at 0, or is
  reached from one of them through buffers alone. An inverter, a gate, a flop, a free input, x and z do not
  count. A pin that is left out or empty does not count either: the reader gives it the value 0, but in the
  design it floats.

With `--keep-hierarchy` the reader makes a module `CHILD$<path of the instance>` for each instance, so each
instance has a netlist of its own. The parameter values are not in the netlist: `hdl_vet.equiv` asks the
reader of the design for them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["Refusal", "interfaces", "pin_problem"]

_BUFFERS = ("$buf", "$_BUF_")
_DEPTH = 200
_HANDLED = re.compile(r"[A-Za-z_][A-Za-z0-9_$.]*\Z")  # a name that a Yosys selection takes as it is


def _named(kind: str, names: Sequence[str]) -> str | None:
    """The module of `names` that the cell type `kind` is a copy of, or None."""
    for name in names:
        if kind == name or kind.startswith(name + "$"):
            return name
    return None


def _walk(
    modules: Mapping[str, Any], top: str, same: Sequence[str], inert: Mapping[str, Sequence[str]]
) -> tuple[dict[str, tuple[str, str, Mapping[str, Any]]], dict[str, list[tuple[str, Mapping[str, Any]]]]]:
    """The inert boxes that the flattened `top` holds, and the way down to each.

    The boxes are instance name -> (cell type, module of `inert`, cell). The way is the list of (module, cell)
    from `top` down to the cell of the box: the cell is in the module and holds the next module. Instances of a
    `same` module and of an inert module are not entered. Any other child is flattened, so its instances count
    with the names that the flattening gives them.
    """
    found: dict[str, tuple[str, str, Mapping[str, Any]]] = {}
    ways: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    names = list(inert)

    def visit(name: str, prefix: str, depth: int, way: list[tuple[str, Mapping[str, Any]]]) -> None:
        if depth > _DEPTH or name not in modules:
            return
        for cell_name, cell in modules[name].get("cells", {}).items():
            kind = str(cell["type"])
            if kind not in modules:
                continue  # an internal cell, or something that is not looked into
            module = _named(kind, names)
            if module is not None:
                found[prefix + cell_name] = (kind, module, cell)
                ways[prefix + cell_name] = [*way, (name, cell)]
            elif _named(kind, same) is None:
                visit(kind, f"{prefix}{cell_name}.", depth + 1, [*way, (name, cell)])

    visit(top, "", 0, [])
    return found, ways


def _ports(module: Mapping[str, Any]) -> dict[str, tuple[str, int]]:
    return {str(n): (str(p["direction"]), len(p["bits"])) for n, p in module.get("ports", {}).items()}


def _drivers(module: Mapping[str, Any]) -> dict[int, list[tuple[str, Any]]]:
    """For each bit of the module: its drivers as ("port", port name and index) or ("cell", cell)."""
    found: dict[int, list[tuple[str, Any]]] = {}
    for name, port in module.get("ports", {}).items():
        if port["direction"] != "output":
            for index, bit in enumerate(port["bits"]):
                if isinstance(bit, int):
                    found.setdefault(bit, []).append(("port", (name, index)))
    for cell in module.get("cells", {}).values():
        directions = cell.get("port_directions", {})
        for name, bits in cell.get("connections", {}).items():
            if directions.get(name) != "input":
                for bit in bits:
                    if isinstance(bit, int):
                        found.setdefault(bit, []).append(("cell", cell))
    return found


def _cone_problem(modules: Mapping[str, Any], way: list[tuple[str, Mapping[str, Any]]], pin: str) -> bool:
    """True when a bit in the cone of the pin has more than one driver.

    The cone is the pin bit and each bit that it is reached from through buffers and through the ports of the
    flattened modules, up to a constant, a cell that is not a buffer, or an input port of the top. A bit
    elsewhere is not looked at.
    """
    level = len(way) - 1
    todo = [(level, bit) for bit in way[level][1].get("connections", {}).get(pin, ())]
    seen: set[tuple[int, Any]] = set()
    tables: dict[int, dict[int, list[tuple[str, Any]]]] = {}
    while todo:
        level, bit = todo.pop()
        if not isinstance(bit, int) or (level, bit) in seen:
            continue
        seen.add((level, bit))
        found = tables.setdefault(level, _drivers(modules[way[level][0]])).get(bit, [])
        if len(found) > 1:
            return True
        if len(found) == 1:
            kind, what = found[0]
            if kind == "cell" and what["type"] in _BUFFERS:
                todo += [(level, feeding) for feeding in what["connections"]["A"]]
            elif kind == "port" and level > 0:
                port, index = what
                above = way[level - 1][1].get("connections", {}).get(port, ())
                if index < len(above):
                    todo.append((level - 1, above[index]))
    return False


class Refusal(Exception):
    """The engine cannot take an inert child. `interface` is True when only its interface is the reason."""

    def __init__(self, detail: str, interface: bool = False) -> None:
        super().__init__(detail)
        self.interface = interface


def interfaces(
    gold: Mapping[str, Any], gate: Mapping[str, Any], top: str, same: Sequence[str], inert: Mapping[str, Sequence[str]]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Check the boxes of the inert children on the netlists before the flattening (rule I3).

    Return (the pins, the boxes). A pin is (instance name, input name). A box is (module, path of the instance
    in the design), for the check of the parameter values. Raise `Refusal` with the complaint. Its `interface`
    is True when the interface of the child is the reason (a port that is not as the caller said, a name
    that cannot be handled): the caller can compare the parent with the child inside. It is False for what
    the child does not decide (an instance on one side only, a pin that is not connected, a net with two
    drivers): that stays an answer.
    """
    (before, _), (after, ways) = _walk(gold, top, same, inert), _walk(gate, top, same, inert)
    pins: list[tuple[str, str]] = []
    boxes: list[tuple[str, str]] = []
    for path in sorted(set(before) | set(after)):
        if path not in after:
            raise Refusal(f"the instance {path} is only in the gold, not in the gate")
        if path not in before:
            raise Refusal(f"the instance {path} is only in the gate, not in the gold")
        (was, module, _), (now, _, cell) = before[path], after[path]
        if was != now:
            raise Refusal(f"the instance {path} is of {was} in the gold and of {now} in the gate")
        named = list(inert[module])
        old, new = _ports(gold[was]), _ports(gate[now])
        for pin in named:
            if pin in old:
                raise Refusal(f"the instance {path} has the input {pin} in the gold: it must be new in the gate", True)
            if pin not in new:
                raise Refusal(f"the instance {path} has no input {pin} in the gate", True)
            if new[pin][0] != "input":
                raise Refusal(f"the port {pin} of the instance {path} is an {new[pin][0]} in the gate", True)
        if {n: v for n, v in new.items() if n not in named} != old:
            raise Refusal(f"the instance {path} of {module} has another interface in the gate than in the gold", True)
        for pin in named:
            if not _HANDLED.match(f"{path}.{pin}"):
                raise Refusal(f"the pin {pin} of the instance {path} has a name that cannot be handled", True)
            # A pin that is left out or empty is not a constant: the reader reads it as 0, which a wire does not.
            if len(cell.get("connections", {}).get(pin, ())) != new[pin][1]:
                raise Refusal(f"the pin {pin} of the instance {path} is not connected")
            if _cone_problem(gate, ways[path], pin):
                raise Refusal(f"the pin {pin} of the instance {path} is on a net with more than one driver")
        pins += [(path, pin) for pin in named]
        if "$" not in was:
            raise Refusal(f"the instance {path} of {module} has no path of its own in the netlist")
        boxes.append((module, was.split("$", 1)[1]))
    return pins, boxes


def pin_problem(
    gate: Mapping[str, Any], pins: Sequence[tuple[str, str]], ties: Mapping[str, tuple[int, int]]
) -> str | None:
    """None when each pin is the constant 0 in `gate` once the ties are applied, else the complaint.

    `gate` is the netlist of the gate, as it was read (the flattening and the exposing of the pins done, the
    ties not applied). The pin of an instance is the wire `<instance>.<input>` that the exposing made. It is
    the same net as the signal that the instance is connected to. A bit is 0 when it is the constant 0, or a
    bit of an input port that `ties` sets to 0, or is reached from one of these through buffers alone.
    `ties` maps a port name to (width, value), as `equiv.prepare_script` takes it.
    """
    zero: set[int] = set()
    for name, (_, value) in ties.items():
        for index, bit in enumerate(gate["ports"][name]["bits"]):
            if isinstance(bit, int) and not (value >> index) & 1:
                zero.add(bit)
    source: dict[int, Any] = {}  # a bit that a buffer drives -> the bit that feeds the buffer
    for cell in gate.get("cells", {}).values():
        if cell["type"] in _BUFFERS:
            for driven, feeding in zip(cell["connections"]["Y"], cell["connections"]["A"], strict=True):
                if isinstance(driven, int):
                    source[driven] = feeding
    nets = gate.get("netnames", {})
    for path, pin in pins:
        net = nets.get(f"{path}.{pin}")
        if net is None or not net["bits"]:
            return f"the pin {pin} of the instance {path} is not in the netlist"
        for bit in net["bits"]:
            for _ in range(len(source) + 1):
                if not isinstance(bit, int) or bit not in source:
                    break
                bit = source[bit]
            if bit != "0" and bit not in zero:
                return f"the pin {pin} of the instance {path} is not the constant 0"
    return None
