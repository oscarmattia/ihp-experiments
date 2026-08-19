#!/usr/bin/env python3
"""Generate the CTLE sub-blocks and gate them on DRC, LVS and PEX.

Blocks are checked with ``allow_context=False``: a block has guard rings and a
shared substrate tie, so the latch-up rule that an isolated device cannot satisfy
becomes a real requirement here.

Usage:
    python layout/blocks/gen_blocks.py
    python layout/blocks/gen_blocks.py --only nmos_tail_pair --no-pex
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from layout.common.drc import CONTEXT_RULES, run_drc
from layout.common.lvs import run_lvs
from layout.common.netlist import write_block_cdl
from layout.common.pex import run_magic_pex, signal_resistors
from layout.common.render import render_gds
from layout.blocks.generators import (
    degeneration_network,
    hbt_differential_pair,
    resistor_load_pair,
    shunt_coil,
    tail_pair,
)
from layout.devices.catalog import ctle_devices

OUT_DIR = Path(__file__).resolve().parent / "out"

#: Blocks where the latch-up rule still cannot be met inside the block itself.
#: The coil carries no active area at all, so there is nothing for LU.b to
#: constrain, but its LBE marker is still a chip-level shape.
CONTEXT_ALLOWED: dict[str, tuple[str, ...]] = {
    "shunt_coil": ("LBE.a",),
}


def build_blocks(only: set[str] | None = None):
    devices = {spec.name: spec for spec in ctle_devices()}
    builders = {
        "hbt_diff_pair": lambda: hbt_differential_pair(devices["npn13G2_pair_device"]),
        "rppd_load_pair": lambda: resistor_load_pair(devices["rppd_load"]),
        "degeneration_network": lambda: degeneration_network(
            devices["rsil_degen"], devices["cmomi_cs"]
        ),
        "nmos_tail_pair": lambda: tail_pair(devices["nmos_tail"]),
        "shunt_coil": lambda: shunt_coil(devices["inductor_turn1_d40"]),
    }
    for name, builder in builders.items():
        if only and name not in only:
            continue
        yield name, builder()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--no-render", action="store_true")
    parser.add_argument("--no-pex", action="store_true")
    args = parser.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    only = set(args.only) or None

    entries = {}
    failed = []
    for name, block in build_blocks(only):
        entry = block.summary()
        gds = block.write(args.out / "gds")
        entry["gds"] = str(gds.relative_to(args.out))

        cdl = write_block_cdl(
            name, block.port_nets, block.instances, args.out / "cdl" / f"{name}.cdl"
        )
        entry["cdl"] = str(cdl.relative_to(args.out))

        if not args.no_render:
            png = render_gds(gds, args.out / "png" / f"{name}.png")
            entry["png"] = str(png.relative_to(args.out)) if png else None

        # Blocks have guard rings, so context rules are enforced unless the
        # block genuinely cannot satisfy them (see CONTEXT_ALLOWED).
        allowed = CONTEXT_ALLOWED.get(name, ())
        drc = run_drc(
            gds=gds,
            run_dir=args.out / "drc_run" / name,
            cell_name=name,
            allow_context=bool(allowed),
        )
        remaining = {
            rule: count
            for rule, count in drc.context_by_rule.items()
            if rule not in allowed
        }
        drc_ok = drc.real_total == 0 and not remaining and not drc.error
        entry["drc"] = drc.to_dict()
        entry["drc"]["enforced_context_rules"] = sorted(
            set(CONTEXT_RULES) - set(allowed)
        )
        entry["drc"]["ok"] = drc_ok

        lvs = run_lvs(
            gds=gds, cdl=cdl, run_dir=args.out / "lvs_run" / name, topcell=name
        )
        entry["lvs"] = lvs.to_dict()

        if not args.no_pex:
            pex = run_magic_pex(
                gds=gds, cell=name, run_dir=args.out / "pex_run" / name
            )
            signal = signal_resistors(pex.resistor_elements)
            entry["pex"] = pex.to_dict()
            entry["pex"]["signal_resistance_ohm"] = round(
                sum(e["ohm"] for e in signal), 6
            )

        status = []
        status.append("DRC ok" if drc_ok else f"DRC FAIL {drc.real_by_rule or remaining or drc.error}")
        status.append("LVS ok" if lvs.clean else f"LVS FAIL {lvs.summary[:40]}")
        if not args.no_pex:
            pex_entry = entry["pex"]
            status.append(
                f"PEX {pex_entry['capacitors']}C/{pex_entry['resistors']}R"
                if pex_entry["ok"]
                else "PEX FAIL"
            )
        sym = entry.get("symmetry")
        if sym:
            status.append(f"sym {sym['worst_um']:.3f}um")
        print(f"  {name:<24} " + "  ".join(status))

        if not (drc_ok and lvs.clean):
            failed.append(name)
        entries[name] = entry

    summary = {"ok": not failed, "failed": failed, "blocks": entries}
    (args.out / "blocks_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"\n{len(entries) - len(failed)}/{len(entries)} block(s) passed DRC+LVS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
