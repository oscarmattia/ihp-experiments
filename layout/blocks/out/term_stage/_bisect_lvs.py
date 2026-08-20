#!/usr/bin/env python3
"""Stepwise LVS extract for term_dut bisection — not part of the deliverable."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from layout.blocks.term_stage import build_term_stage  # noqa: E402
from layout.common.sizing import read_params  # noqa: E402

OUT = Path(__file__).resolve().parent / "_bisect"
OUT.mkdir(parents=True, exist_ok=True)


def extract_subckt_line(cir: Path) -> str:
    for line in cir.read_text().splitlines():
        if line.startswith(".SUBCKT"):
            return line.strip()
    return "(no subckt)"


def extract_devices(cir: Path) -> list[str]:
    lines = []
    for line in cir.read_text().splitlines():
        if line.startswith(("D$", "R$", "C$")):
            lines.append(line.strip())
    return lines


def run_lvs_extract(gds: Path, cdl: Path, tag: str) -> None:
    run_dir = OUT / tag
    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "python3",
        str(Path.home() / ".local/share/ihp-eda/IHP-Open-PDK/ihp-sg13g2/libs.tech/klayout/tech/lvs/run_lvs.py"),
        f"--layout={gds}",
        f"--netlist={cdl}",
        f"--run_dir={run_dir}",
        "--run_mode=deep",
        "--topcell=term_dut",
        "--implicit_nets=sub,sub!",
        "--ignore_top_ports_mismatch",
        "--disable_tap_extraction",
    ]
    subprocess.run(cmd, check=False, capture_output=True)
    ext = run_dir / f"{tag}_extracted.cir"
    print(f"\n=== {tag} ===")
    print(extract_subckt_line(ext))
    for d in extract_devices(ext)[:6]:
        print(" ", d)
    if len(extract_devices(ext)) > 6:
        print("  ...")


if __name__ == "__main__":
    # Import after path setup; build with stage flags patched on module
    import layout.blocks.term_stage as ts

    stages = ("pads_esd", "plus_divider", "plus_clamp", "plus_ring", "full")
    for stage in stages:
        ts._BISECT_STAGE = stage  # type: ignore[attr-defined]
        block = build_term_stage(read_params(ts.PARAMS_INC))
        gds = OUT / f"{stage}.gds"
        cdl = OUT / f"{stage}.cdl"
        block.layout.write(str(gds))
        from layout.common.netlist import write_block_cdl

        write_block_cdl(block.name, block.port_nets, block.instances, cdl)
        run_lvs_extract(gds, cdl, stage)
