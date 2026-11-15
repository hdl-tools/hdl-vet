"""Test helper: regeneration variants of the demo_ss sample.

Each variant is a minimal mutation of <hdl-bag>/tests/golden/demo_ss.ir.yaml, written
under <cache>/_variants/<name>.ir.yaml and generated with `bag generate --style flat`
through corpus.CorpusDesign (same cache and spec-sha256 stamp). They let tests check
that an edit script written against demo_ss still applies, or fails clearly, after the
design is regenerated from a changed spec. Not part of the package.

The IR is read and written with ruamel.yaml (an hdl-bag dependency, so always present
wherever `bag` is installed). The dump is deterministic. A slave or master is repeated
in several IR tables (slaves/masters, fabric ports, addr_rules, decode, memory_map,
port_options, wrapper.ports); every mutation edits all of them. Mutations:

add_slave       APB4 slave `pwm0` (copy of rtc0: D_AON, 12 address bits) on fabric
                aon_apb: slave_port 2, region regs 0x5000_2000..0x5000_2FFF, exposed
                with prefix pwm0_apb.
remove_slave    leaf slave `spi0` (last port, index 3, of periph_apb) removed from
                slaves, slave_ports, addr_rules, memory_map, port_options, wrapper.ports.
width_change    slave uart0 addr_width 12 -> 14 (wider uart0_apb_paddr port on the top).
adapter_change  the cut adapter main_xbar_cut_2_sram0 (bag_axi_cut, in main_wrapper) becomes
                a fifo adapter main_xbar_fifo_2_sram0 (bag_axi_fifo, depth 2): adapters
                and hierarchy.placement entries renamed, port_options sram0 fifo_depth 2,
                slice none. Instance u_main_xbar_cut_2_sram0 -> u_main_xbar_fifo_2_sram0.
rename_slave    slave wdt0 -> wdog0 (slaves, aon_apb slave_ports and addr_rules,
                memory_map, port_options, wrapper.ports incl. prefix wdt0_apb ->
                wdog0_apb).
add_master      AXI4-Lite master `sec1` (copy of sec0) on fabric cfg_bus: master port
                index 2, priority 2, DECERR decode entry, exposed with prefix sec1_axil.
"""

from __future__ import annotations

import copy
import io
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

import corpus

VARIANT_NAMES = ("add_slave", "remove_slave", "width_change", "adapter_change", "rename_slave", "add_master")


def _by(rows: list[dict[str, Any]], key: str, value: str) -> dict[str, Any]:
    return next(r for r in rows if r[key] == value)


def _fabric(spec: dict[str, Any], name: str) -> dict[str, Any]:
    return _by(spec["fabrics"], "name", name)


def _dup(rows: list[dict[str, Any]], key: str, src: str, new: str, **over: Any) -> dict[str, Any]:
    row = copy.deepcopy(_by(rows, key, src))
    row[key] = new
    row.update(over)
    rows.append(row)
    return row


def _add_slave(s: dict[str, Any]) -> None:
    _dup(s["slaves"], "name", "rtc0", "pwm0")
    _dup(s["port_options"], "port", "rtc0", "pwm0")
    _dup(s["wrapper"]["ports"], "port", "rtc0", "pwm0", prefix="pwm0_apb")
    fab = _fabric(s, "aon_apb")
    fab["slave_ports"].append({"index": 2, "port": "pwm0", "priority": None})
    fab["addr_rules"].append(
        {"slave_port": 2, "slave": "pwm0", "region": "regs", "base": "0x5000_2000",
         "end": "0x5000_2FFF", "access": "RW", "secure": False}
    )  # fmt: skip
    mem = _dup(s["memory_map"], "slave", "rtc0", "pwm0", base="0x5000_2000")
    assert mem["fabric"] == "aon_apb"


def _remove_slave(s: dict[str, Any]) -> None:
    name = "spi0"
    s["slaves"][:] = [r for r in s["slaves"] if r["name"] != name]
    s["port_options"][:] = [r for r in s["port_options"] if r["port"] != name]
    s["wrapper"]["ports"][:] = [r for r in s["wrapper"]["ports"] if r["port"] != name]
    s["memory_map"][:] = [r for r in s["memory_map"] if r["slave"] != name]
    fab = _fabric(s, "periph_apb")
    fab["slave_ports"][:] = [r for r in fab["slave_ports"] if r["port"] != name]  # spi0 was last: no reindex
    fab["addr_rules"][:] = [r for r in fab["addr_rules"] if r["slave"] != name]


def _width_change(s: dict[str, Any]) -> None:
    _by(s["slaves"], "name", "uart0")["addr_width"] = 14


def _adapter_change(s: dict[str, Any]) -> None:
    old, new = "main_xbar_cut_2_sram0", "main_xbar_fifo_2_sram0"
    ad = _by(s["adapters"], "name", old)
    ad.update(name=new, type="fifo", template="bag_fifo", params={"depth": 2}, rtl_name="u_" + new)
    pl = _by(s["hierarchy"]["placement"], "instance", old)
    pl.update(instance=new, rtl_name="u_" + new)
    opt = _by(s["port_options"], "port", "sram0")
    opt.update(fifo_depth=2, slice="none")


def _rename_slave(s: dict[str, Any]) -> None:
    old, new = "wdt0", "wdog0"
    _by(s["slaves"], "name", old)["name"] = new
    _by(s["port_options"], "port", old)["port"] = new
    wp = _by(s["wrapper"]["ports"], "port", old)
    wp["port"], wp["prefix"] = new, "wdog0_apb"
    _by(s["memory_map"], "slave", old)["slave"] = new
    fab = _fabric(s, "aon_apb")
    _by(fab["slave_ports"], "port", old)["port"] = new
    _by(fab["addr_rules"], "slave", old)["slave"] = new


def _add_master(s: dict[str, Any]) -> None:
    _dup(s["masters"], "name", "sec0", "sec1")
    _dup(s["port_options"], "port", "sec0", "sec1")
    _dup(s["wrapper"]["ports"], "port", "sec0", "sec1", prefix="sec1_axil")
    fab = _fabric(s, "cfg_bus")
    fab["master_ports"].append({"index": 2, "port": "sec1", "priority": 2})
    _dup(fab["decode"], "master", "sec0", "sec1")


_MUTATIONS = {
    "add_slave": _add_slave,
    "remove_slave": _remove_slave,
    "width_change": _width_change,
    "adapter_change": _adapter_change,
    "rename_slave": _rename_slave,
    "add_master": _add_master,
}
assert tuple(_MUTATIONS) == VARIANT_NAMES


def _yaml() -> YAML:
    yaml = YAML(typ="rt", pure=True)
    yaml.default_flow_style = False
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.representer.add_representer(type(None), lambda r, _: r.represent_scalar("tag:yaml.org,2002:null", "null"))
    return yaml


def _mutated_text(name: str, original: Path) -> str:
    spec = YAML(typ="safe", pure=True).load(original.read_text(encoding="utf-8"))
    _MUTATIONS[name](spec)
    buf = io.StringIO()
    _yaml().dump(spec, buf)
    return buf.getvalue()


def _design(name: str, spec: Path | None) -> corpus.CorpusDesign:
    if name not in VARIANT_NAMES:
        raise KeyError(f"unknown variant {name!r}; choose from {VARIANT_NAMES}")
    return corpus.CorpusDesign(f"demo_ss-var-{name}-flat", "flat", spec)


def variants() -> list[corpus.CorpusDesign]:
    """Cheap: enumerates only. Empty without an hdl-bag checkout."""
    if corpus._checkout() is None:
        return []
    return [_design(n, None) for n in VARIANT_NAMES]


def build(name: str, cache: Path) -> corpus.Built:
    """Write the mutated spec under CACHE/_variants/ (same bytes every time), then build it."""
    repo = corpus._checkout()
    if repo is None:
        raise RuntimeError("no hdl-bag checkout (set HDL_BAG_REPO); variants need demo_ss.ir.yaml")
    _design(name, None)
    golden = repo / "tests" / "golden"
    text = _mutated_text(name, golden / "demo_ss.ir.yaml")
    spec = Path(cache) / "_variants" / f"{name}.ir.yaml"
    spec.parent.mkdir(parents=True, exist_ok=True)
    # Wrapper > File header (header.txt) is resolved next to the spec: copy it alongside.
    for extra, data in (("header.txt", (golden / "header.txt").read_bytes()), (spec.name, text.encode("utf-8"))):
        target = spec.with_name(extra)
        if not target.is_file() or target.read_bytes() != data:
            target.write_bytes(data)  # LF, no platform newline translation
    return _design(name, spec).build(Path(cache))
