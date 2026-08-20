#!/usr/bin/env python3
"""Bisect driver_dut LVS: HBT+tail, +loads, +coils, +tapeout."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from layout.blocks.driver_stage import build_driver_stage
from layout.common.lvs import run_lvs
from layout.common.netlist import write_block_cdl

OUT = Path(__file__).resolve().parent / "bisect_run"
OUT.mkdir(parents=True, exist_ok=True)

STEPS = (
    ("core", dict(with_loads=False, with_coils=False, with_tapeout=False)),
    ("loads", dict(with_loads=True, with_coils=False, with_tapeout=False)),
    ("coils", dict(with_loads=True, with_coils=True, with_tapeout=False)),
    ("full", dict(with_loads=True, with_coils=True, with_tapeout=True)),
)


def _nets_from_extracted(path: Path) -> set[str]:
    if not path.exists():
        return set()
    nets: set[str] = set()
    for line in path.read_text().splitlines():
        if line.startswith(".SUBCKT"):
            nets.update(line.split()[2:])
    return nets


def main() -> int:
    prev_ok = True
    print("step       ports in extracted subckt")
    print("-" * 60)
    for name, flags in STEPS:
        block = build_driver_stage(**flags)
        step_dir = OUT / name
        step_dir.mkdir(parents=True, exist_ok=True)
        gds = block.write(step_dir)
        cdl = write_block_cdl("driver_dut", block.port_nets, block.instances, step_dir / "driver_dut.cdl")
        lvs = run_lvs(gds=gds, cdl=cdl, run_dir=step_dir / "lvs_run", topcell="driver_dut",
                      disable_tap_extraction=True)
        nets = _nets_from_extracted(Path(lvs.extracted_netlist or ""))
        mega = any("|" in n for n in nets)
        ok = lvs.clean and not mega
        mark = "ok" if ok else "FAIL"
        print(f"{name:8}  {mark:4}  {sorted(nets)[:12]}{'...' if len(nets) > 12 else ''}")
        if mega:
            merged = next(n for n in nets if "|" in n)
            print(f"           mega-net: {merged}")
        if prev_ok and not ok:
            print(f"  ^ first merge step: adding {name}")
        prev_ok = ok
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
