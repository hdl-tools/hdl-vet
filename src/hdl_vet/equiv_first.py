"""What the first step tells `sat` about the first state of the flops (#139, rules I9 to I18).

The rules are in tests/equiv/test_equiv_first_shared.py and test_equiv_first_shared_review.py.

The first step compares the two sides for one step. A flop that both sides have starts with one shared, defined
value; a flop that only one side has starts undefined. This is decided from the netlists that `prepared.json`
holds (no Yosys, no pyslang), and it is told to `sat` through the names of the nets:

- A flop is one state cell. Its names are the public nets whose bits are exactly the output of that cell (the
  names of the same bits are one flop, rule I14). A net made of bits of several flops, or of a part of one,
  names no flop and is ignored.
- A flop is shared when the other side has a flop of the same name (one that fits only one flop on each side)
  and each bit has the same kind on both sides: no asynchronous pin, or an asynchronous reset to a constant with
  the same polarity and the same constant. The control of that reset must be a function of the inputs alone
  (`_inputs_only`): a reset that comes from a flop can make the net undefined, and "defined" then pins the state
  of the flop to the reset value (measured), which rules out a defined value. The reader writes such a reset as
  an asynchronous load of a constant (`$aldff`), which counts as a reset. The net of a shared flop depends on
  nothing but the state of that flop and on the inputs, which are defined,
  so the constraint on it does not rule out a value of another flop.
- A flop that is not shared and has no asynchronous pin starts undefined (`-set-all-undef-at`). A flop that is
  not shared and has an asynchronous reset is not constrained: its net is forced by the reset while it is
  active, so an undefined constraint would rule out the inputs that have the reset active. Left free, it may
  start with any value, defined or undefined, which holds the undefined start. Nothing is said about it, so
  that cannot rule out a value of another flop.
- Fallback (rule I17, `constraints` returns None): when either side has any other state (a load of a net, a
  state cell of another kind, a flop with no name that the solver can be given), nothing is constrained for the
  pair. The first step runs as rule I1 had it: `-set-init-undef`, every flop undefined, nothing shared. A shared
  constraint on a net that depends on another flop could rule out a value of that flop (measured). The limit: in
  a fallback pair a gate that adds a flop of its own to an output that the gold takes from a flop is a wrong
  "proved".
- Each net is told to `sat` once (a second constraint on the same bits replaces the first, silently, measured).
  The flops are separate cells, so their bits are disjoint. A flop is renamed to a plain name first, so a name
  like `stage[1].r` needs no care. A name that is not one word of a script line (space, quote, `#`, `;`,
  backslash) is not used. `equiv._FILES` puts a folder in front of `gold.f`, `gate.json` and the like when the
  run is mounted, so a flop is renamed in its own module, by its bare name, before the miter. A flop that only
  has names of ports is renamed in the miter, which the rewrite does not touch for a name other than those.

`hdl_vet.equiv` runs a `sat` with the options and no goal first: its log must say that a model was found, or the
constraints could not be met and every proof would pass with nothing checked (rule I13). That guard cannot see two
constraints on the same bits (the last one wins), which is why they are never made.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

__all__ = ["GUARD_OK", "constraints"]

GUARD_OK = "SAT solving finished - model found:"

_PLAIN = frozenset({"$dff", "$dffe", "$sdff", "$sdffe", "$sdffce"})
_RESET = frozenset({"$adff", "$adffe"})
_LOAD = frozenset({"$aldff", "$aldffe"})
_SAFE = re.compile(r"[^\s\"'\\#;]+\Z")  # one word of a script line
_MOUNT = re.compile(r"(?:f|json|il)(?!\w)")  # `gold.` + this is text that `equiv._FILES` rewrites
_Kind = tuple[Any, ...]
# A flop: the output bits of one cell, the kind of each bit, the public names of exactly those bits, and the bits
# of its asynchronous control (none for a flop without one).
_Flop = tuple[tuple[int, ...], tuple[_Kind, ...], list[str], list[Any]]


def _kind(kind: str, cell: Mapping[str, Any], index: int) -> _Kind | None:
    """What a state bit is: None when it is not a plain flop or a flop with an asynchronous reset to a constant."""
    if kind in _PLAIN:
        return ("plain",)
    params = cell.get("parameters", {})
    if kind in _RESET:
        value, polarity = str(params.get("ARST_VALUE", "")), str(params.get("ARST_POLARITY", ""))
        if re.fullmatch(r"[01]+", value) and re.fullmatch(r"[01]+", polarity) and index < len(value):
            return ("reset", int(polarity, 2), value[len(value) - 1 - index])
    if kind in _LOAD:
        # The reader writes an asynchronous reset as a load of a constant. A load of a net is no reset.
        value, polarity = cell["connections"].get("AD", []), str(params.get("ALOAD_POLARITY", ""))
        if re.fullmatch(r"[01]+", polarity) and index < len(value) and value[index] in ("0", "1"):
            return ("reset", int(polarity, 2), value[index])
    return None


def _flops(side: Mapping[str, Any], keeps_state: Callable[[str], bool]) -> list[_Flop] | None:
    """The flops of a side, or None when it has state that is not a named flop of a known kind (the fallback)."""
    nets: dict[tuple[Any, ...], list[str]] = {}
    for name, net in side.get("netnames", {}).items():
        if not net.get("hide_name") and _SAFE.match(str(name)):
            nets.setdefault(tuple(net["bits"]), []).append(str(name))
    ports = set(side["ports"])
    found: list[_Flop] = []
    for cell in side["cells"].values():
        kind = str(cell["type"])
        if not keeps_state(kind):
            continue
        if [p for p, d in cell.get("port_directions", {}).items() if d == "output"] != ["Q"]:
            return None
        bits = tuple(cell["connections"]["Q"])
        kinds = tuple(_kind(kind, cell, i) for i in range(len(bits)))
        if not all(isinstance(b, int) for b in bits) or any(k is None for k in kinds):
            return None
        names = sorted(n for n in nets.get(bits, []) if n not in ports or not _MOUNT.match(n))
        if not names:
            return None
        control = cell["connections"].get("ARST", []) + cell["connections"].get("ALOAD", [])
        found.append((bits, kinds, names, control))  # type: ignore[arg-type]
    return found


def _inputs_only(side: Mapping[str, Any], bits: list[Any], keeps_state: Callable[[str], bool]) -> bool:
    """True when each bit is a constant or a function of the input ports alone (so it is defined in the first step)."""
    inputs = {b for port in side["ports"].values() if port["direction"] == "input" for b in port["bits"]}
    driver: dict[int, Mapping[str, Any]] = {}
    for cell in side["cells"].values():
        for pin, cell_bits in cell["connections"].items():
            if cell.get("port_directions", {}).get(pin) == "output":
                driver.update({b: cell for b in cell_bits if isinstance(b, int)})
    memo: dict[int, bool] = {}

    def clean(bit: Any) -> bool:
        if bit in ("0", "1") or bit in inputs:
            return True
        if not isinstance(bit, int):
            return False  # x or z
        if bit not in memo:
            memo[bit] = False  # a loop is not clean
            cell = driver.get(bit)
            if cell is not None and str(cell["type"]).startswith("$") and not keeps_state(str(cell["type"])):
                directions = cell.get("port_directions", {})
                memo[bit] = all(
                    clean(b) for pin, bs in cell["connections"].items() if directions.get(pin) == "input" for b in bs
                )
        return memo[bit]

    try:
        return all(clean(bit) for bit in bits)
    except RecursionError:
        return False  # a cone too deep to walk is not shown clean


def constraints(
    gold: Mapping[str, Any], gate: Mapping[str, Any], keeps_state: Callable[[str], bool]
) -> tuple[list[str], list[str], str] | None:
    """What the first step adds to the script: lines before the miter, lines after it, the `sat` options.

    None is the fallback: nothing is constrained for the pair (rule I17).
    """
    gold_flops, gate_flops = _flops(gold, keeps_state), _flops(gate, keeps_state)
    if gold_flops is None or gate_flops is None:
        return None
    by_name = {name: j for j, (_, _, names, _) in enumerate(gate_flops) for name in names}
    claim: dict[int, list[int]] = {}  # gate flop -> gold flops that name it
    pick: dict[int, tuple[int, str]] = {}  # gold flop -> (gate flop, name), when only one gate flop fits
    for i, (bits, _, names, _) in enumerate(gold_flops):
        fits = {by_name[n] for n in names if n in by_name and len(gate_flops[by_name[n]][0]) == len(bits)}
        if len(fits) == 1:
            j = fits.pop()
            pick[i] = (j, next(n for n in names if n in gate_flops[j][2]))
            claim.setdefault(j, []).append(i)
    shared = {
        i: (j, name) for i, (j, name) in pick.items() if len(claim[j]) == 1 and gold_flops[i][1] == gate_flops[j][1]
    }
    if not all(
        _inputs_only(gold, gold_flops[i][3], keeps_state) and _inputs_only(gate, gate_flops[j][3], keeps_state)
        for i, (j, _) in shared.items()
    ):
        return None
    taken = {j for j, _ in shared.values()}
    before: dict[str, list[str]] = {"gold": [], "gate": []}
    after: list[str] = []
    options: list[str] = []
    ports = {"gold": set(gold["ports"]), "gate": set(gate["ports"])}

    def tell(side: str, names: list[str], new: str) -> str:
        """Rename the flop to `new` and return the name that `sat` is given."""
        name = next((n for n in names if n not in ports[side]), names[0])
        if name in ports[side]:
            after.append(f"rename {side}.{name} {new}")
            return new
        before[side].append(f"rename {name} {new}")
        return f"{side}.{new}"

    for i, (_, kinds, names, _) in enumerate(gold_flops):
        if i in shared:
            j, name = shared[i]
            a, b = tell("gold", [name], f"hv_first_gold{i}"), tell("gate", [name], f"hv_first_gate{j}")
            options.append(f"-set-def-at 1 {a} -set-def-at 1 {b} -set-at 1 {a} {b}")
        elif all(k == ("plain",) for k in kinds):
            options.append(f"-set-all-undef-at 1 {tell('gold', names, f'hv_first_gold{i}')}")
    for j, (_, kinds, names, _) in enumerate(gate_flops):
        if j not in taken and all(k == ("plain",) for k in kinds):
            options.append(f"-set-all-undef-at 1 {tell('gate', names, f'hv_first_gate{j}')}")
    first = [x for s in ("gold", "gate") if before[s] for x in (f"cd {s}", *before[s], "cd ..")]
    return first, (["cd miter", *after, "cd .."] if after else []), " ".join(options)
