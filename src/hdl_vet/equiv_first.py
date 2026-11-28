"""What the first step tells `sat` about the first state of the flops (#139, rules I9 to I15).

The rules are in tests/equiv/test_equiv_first_shared.py.

The first step compares the two sides for one step. A flop that both sides have starts with one shared, defined
value; a flop that only one side has, or that is not safe to share, starts undefined. This is decided from the
netlists that `prepared.json` holds (no Yosys, no pyslang), and it is told to `sat` through the names of the nets:

- The state bits of a side are the outputs of its state cells. A net is a flop when it is public and all its bits
  are state bits. The names of the same bits are one flop (rule I14); a flop whose bits are all in a larger flop
  is a part of it. Two flops that share some bits and are not one inside the other cannot be told apart: `None`.
- A flop is shared when the other side has a net of the same name, with the same number of bits, that is a flop,
  and when each bit has the same kind on both sides: a flop without an asynchronous reset, or one with the same
  reset polarity and the same reset value for that bit. The reader makes a reset an asynchronous load of a
  constant (`$aldff`), which counts as a reset. A load of a net is shared when the polarity is the same (the
  value is not compared: the induction has taken flops of one name as equal too). A set and reset together, a
  latch and a memory are not shared.
- A flop that is not shared and has no asynchronous reset starts undefined on each side. The net of a flop with a
  reset is the output of a mux that the reset forces while it is active (measured: `-set-all-undef-at` on it
  excludes every input that has the reset active, so a hole that shows only under reset is not looked at). That
  flop, and any state cell that is not a plain flop, is left free instead: defined or undefined, which holds
  the undefined start and more, so it can only give more answers of "refuted".
- Each bit is told to `sat` once. A second constraint on the same bits would silently replace the first
  (measured), so the flops that are told are disjoint, and a name that cannot be matched in one way only is not
  shared. The nets are renamed to plain names first, so a name like `stage[1].r` needs no care.
- A state bit that no public net names is not told: `sat` leaves it free, defined or undefined (measured), which
  only gives more answers of "refuted".

`hdl_vet.equiv` runs a `sat` with these options and no goal first: its log must say that a model was found, or
the constraints could not be met and every proof would pass with nothing checked (rule I13). That guard cannot
see two constraints on the same bits (the last one wins), which is why they are never made.
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
# A name that a script line takes as one word. `equiv._FILES` would also put a folder in front of `gold.f`.
_WORD = re.compile(r"(?!(?:f|json|il)(?!\w))[A-Za-z_][A-Za-z0-9_$.\[\]:]*\Z")
_Kind = tuple[Any, ...] | None


def _kind(kind: str, cell: Mapping[str, Any], pin: str, index: int) -> _Kind:
    """What a state bit is, for the comparison of the two sides: None when it is not safe to share."""
    if pin != "Q":
        return None
    if kind in _PLAIN:
        return ("plain",)
    params = cell.get("parameters", {})
    if kind in _RESET:
        value, polarity = str(params.get("ARST_VALUE", "")), str(params.get("ARST_POLARITY", ""))
        if re.fullmatch(r"[01]+", value) and re.fullmatch(r"[01]+", polarity) and index < len(value):
            return ("reset", int(polarity, 2), value[len(value) - 1 - index])
    if kind in _LOAD:
        # The reader makes an asynchronous reset an async load of a constant. A load of a net is shared too, when
        # the polarity is the same: its value cannot be compared here, and the induction has taken the flops of
        # one name as equal, as this does (a difference of the load itself is "refuted" there).
        value, polarity = cell["connections"].get("AD", []), str(params.get("ALOAD_POLARITY", ""))
        if re.fullmatch(r"[01]+", polarity) and index < len(value):
            return (
                ("reset", int(polarity, 2), value[index]) if value[index] in ("0", "1") else ("load", int(polarity, 2))
            )
    return None


def _state_bits(side: Mapping[str, Any], keeps_state: Callable[[str], bool]) -> dict[int, _Kind]:
    found: dict[int, _Kind] = {}
    for cell in side["cells"].values():
        kind = str(cell["type"])
        if keeps_state(kind):
            for pin, bits in cell["connections"].items():
                if cell.get("port_directions", {}).get(pin) == "output":
                    found.update({b: _kind(kind, cell, pin, i) for i, b in enumerate(bits) if isinstance(b, int)})
    return found


def _flops(side: Mapping[str, Any], state: Mapping[int, _Kind]) -> list[tuple[tuple[int, ...], list[str]]] | None:
    """The flops of a side as (bits, names), or None when two of them share some bits only."""
    nets: dict[tuple[int, ...], list[str]] = {}
    for name, net in side.get("netnames", {}).items():
        bits = tuple(net["bits"])
        ok = not net.get("hide_name") and bits and len(set(bits)) == len(bits) and _WORD.match(str(name))
        if ok and all(isinstance(b, int) and b in state for b in bits):
            nets.setdefault(bits, []).append(str(name))
    sets = {bits: frozenset(bits) for bits in nets}
    top = [(bits, sorted(names)) for bits, names in nets.items() if not any(sets[bits] < s for s in sets.values())]
    covered: set[int] = set()
    for bits, _ in top:
        if covered & sets[bits]:
            return None
        covered |= sets[bits]
    return sorted(top)


def constraints(
    gold: Mapping[str, Any], gate: Mapping[str, Any], keeps_state: Callable[[str], bool]
) -> tuple[list[str], str] | str:
    """The `rename` lines for the miter and the `sat` options of the first state, or the reason there are none."""
    gold_kinds, gate_kinds = _state_bits(gold, keeps_state), _state_bits(gate, keeps_state)
    gold_flops, gate_flops = _flops(gold, gold_kinds), _flops(gate, gate_kinds)
    if gold_flops is None or gate_flops is None:
        return "two nets have some of their bits in common"
    by_name = {name: j for j, (_, names) in enumerate(gate_flops) for name in names}
    claim: dict[int, list[int]] = {}  # gate flop -> gold flops that name it
    pick: dict[int, tuple[int, str]] = {}  # gold flop -> (gate flop, name), when only one gate flop fits
    for i, (bits, names) in enumerate(gold_flops):
        fits = {by_name[n] for n in names if n in by_name and len(gate_flops[by_name[n]][0]) == len(bits)}
        if len(fits) == 1:
            j = fits.pop()
            pick[i] = (j, next(n for n in names if n in gate_flops[j][1]))
            claim.setdefault(j, []).append(i)
    shared = {
        i: (j, name)
        for i, (j, name) in pick.items()
        if len(claim[j]) == 1
        and all(
            (kind := gold_kinds[x]) is not None and kind == gate_kinds[y]
            for x, y in zip(gold_flops[i][0], gate_flops[j][0], strict=True)
        )
    }
    taken = {j for j, _ in shared.values()}
    renames: list[str] = []
    options: list[str] = []
    for i, (_, names) in enumerate(gold_flops):
        new = f"hv_first_gold{i}"
        if i in shared:
            j, name = shared[i]
            other = f"hv_first_gate{j}"
            renames += [f"rename gold.{name} {new}", f"rename gate.{name} {other}"]
            options.append(f"-set-def-at 1 {new} -set-def-at 1 {other} -set-at 1 {new} {other}")
        elif all(gold_kinds[bit] == ("plain",) for bit in gold_flops[i][0]):
            renames.append(f"rename gold.{names[0]} {new}")
            options.append(f"-set-all-undef-at 1 {new}")
    for j, (bits, names) in enumerate(gate_flops):
        if j not in taken and all(gate_kinds[bit] == ("plain",) for bit in bits):
            renames.append(f"rename gate.{names[0]} hv_first_gate{j}")
            options.append(f"-set-all-undef-at 1 hv_first_gate{j}")
    return (["cd miter", *renames, "cd .."] if renames else []), " ".join(options)
