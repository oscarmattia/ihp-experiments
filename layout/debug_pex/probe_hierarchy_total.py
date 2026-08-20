#!/usr/bin/env python3
"""Does flat extraction really find more capacitance than hierarchical?

The CTLE stage extracted hierarchically summed to 135 fF over 98 capacitor lines
and flat to 700 fF over 34, which was read as hierarchy losing capacitance. That
reading needs testing, because a hierarchical netlist states a subcell's parasitics
**once** in its `.subckt` definition and then instantiates it N times. A textual
sum over capacitor lines therefore counts them once instead of N times, and would
under-report by roughly the instance count regardless of whether anything is wrong.

Minimal case: one NMOS in a subcell, instantiated N times in a top cell, and three
extractions to compare.

    sub alone (flat)        -> the parasitics of one instance
    top hierarchical        -> textual sum vs sum expanded by instance count
    top flat                -> everything expanded by the extractor

If the textual hierarchical sum is about 1/N of the flat sum while the *expanded*
hierarchical sum agrees with it, the 5x was an artefact of the summing and not lost
capacitance. If the expanded sums still disagree, hierarchy is genuinely losing
something.

Nothing is written into the repo.

Usage:
    source ~/.local/share/ihp-eda/env.sh
    python layout/debug_pex/probe_hierarchy_total.py [--units 4] [--out DIR]
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from layout.common.devices import build
from layout.common.layers import layer_map
from layout.common.paths import pdk_paths
from layout.common.pdk import new_layout, pya_module
from layout.common.spec import DeviceSpec

SUB_CELL = "nmos_sub"
TOP_CELL = "nmos_top"

_C_LINE = re.compile(r"^C\S*\s+(\S+)\s+(\S+)\s+([-\d.eE+]+)(\w*)", re.M)
_SUBCKT = re.compile(r"^\.subckt\s+(\S+)", re.I)
_ENDS = re.compile(r"^\.ends", re.I)
_INST = re.compile(r"^X\S+\s+(.*)$")
_SI = {"m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15, "a": 1e-18}


def _si(value: str, suffix: str) -> float:
    return float(value) * _SI.get(suffix[:1].lower(), 1.0)


def build_hierarchy(units: int, out: Path) -> Path:
    """One labelled NMOS in a subcell, instantiated `units` times in a top cell.

    Labels live in the subcell, so every instance shares the same three nets —
    which is what a strapped array does, and what makes the instance-count question
    bite.
    """
    pya = pya_module()
    lm = layer_map()
    layout = new_layout()

    unit = DeviceSpec(name="u", kind="nmos_lv", params={"w": 9.72e-6, "l": 1e-6, "ng": 1, "m": 1})
    _, device_cell = build(unit)

    sub = layout.create_cell(SUB_CELL)
    index = layout.add_cell(f"{SUB_CELL}_pcell")
    layout.cell(index).copy_tree(device_cell)
    sub.insert(pya.DCellInstArray(index, pya.DTrans(pya.DVector(0.0, 0.0))))

    # Terminal labels on metal1_pin, the layer Magic maps to node names here.
    pin = lm["metal1_pin"]
    pin_layer = layout.layer(pin[0], pin[1])
    box = device_cell.dbbox()
    for name, x in (("drain", 0.15), ("source", 1.53)):
        sub.shapes(pin_layer).insert(pya.DBox(x - 0.08, 0.0, x + 0.08, box.top))
        sub.shapes(pin_layer).insert(pya.DText(name, x, box.top / 2))

    top = layout.create_cell(TOP_CELL)
    sub_index = layout.cell_by_name(SUB_CELL)
    pitch = box.width() + 0.6
    for i in range(units):
        top.insert(pya.DCellInstArray(sub_index, pya.DTrans(pya.DVector(i * pitch, 0.0))))

    gds = out / "hier.gds"
    layout.write(str(gds))
    return gds


def extract(gds: Path, cell: str, out: Path, label: str, hierarchy: bool) -> Path | None:
    run_dir = out / f"ext_{label}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    spice = run_dir / f"{cell}.spice"
    lines = [
        "crashbackups stop", "drc off", f"gds read {gds}",
        f"load {cell} -dereference", "select top cell",
        "extract path .", "extract all",
        "ext2spice lvs", "ext2spice cthresh 0", "ext2spice rthresh 0",
        "ext2spice subcircuit on",
    ]
    if not hierarchy:
        lines.append("ext2spice hierarchy off")
    lines += [f"ext2spice -o {spice}", "quit -noprompt"]
    script = run_dir / "extract.tcl"
    script.write_text("\n".join(lines) + "\n")

    env = dict(os.environ)
    env.setdefault("PDK_ROOT", str(pdk_paths().root))
    subprocess.run(
        [shutil.which("magic"), "-dnull", "-noconsole",
         "-rcfile", str(pdk_paths().magic_rcfile), str(script)],
        cwd=run_dir, capture_output=True, text=True, timeout=1800, check=False, env=env,
    )
    return spice if spice.is_file() else None


def textual_total(netlist: Path) -> tuple[int, float]:
    """Sum every capacitor line once, however many times its cell is instantiated."""
    caps = _C_LINE.findall(netlist.read_text())
    return len(caps), sum(_si(v, s) for _, _, v, s in caps)


def expanded_total(netlist: Path, top: str) -> float:
    """Sum capacitors with each subcircuit counted once per instantiation.

    This is what the circuit actually contains, and what a textual sum misses.
    """
    per_cell_caps: dict[str, float] = {}
    per_cell_insts: dict[str, list[str]] = {}
    current = top
    per_cell_caps.setdefault(current, 0.0)
    per_cell_insts.setdefault(current, [])
    for raw in netlist.read_text().splitlines():
        line = raw.strip()
        opened = _SUBCKT.match(line)
        if opened:
            current = opened.group(1)
            per_cell_caps.setdefault(current, 0.0)
            per_cell_insts.setdefault(current, [])
            continue
        if _ENDS.match(line):
            current = top
            continue
        cap = _C_LINE.match(line)
        if cap:
            per_cell_caps[current] += _si(cap.group(3), cap.group(4))
            continue
        inst = _INST.match(line)
        if inst:
            tokens = inst.group(1).split()
            if tokens:
                # ext2spice puts the subcircuit name last on an instance line.
                per_cell_insts[current].append(tokens[-1])

    def total(cell: str, depth: int = 0) -> float:
        if depth > 12:
            return 0.0
        value = per_cell_caps.get(cell, 0.0)
        for child in per_cell_insts.get(cell, []):
            if child in per_cell_caps:
                value += total(child, depth + 1)
        return value

    return total(top)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--units", type=int, default=4)
    parser.add_argument("--out", type=Path, default=Path("/tmp/debug_pex_hier"))
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    gds = build_hierarchy(args.units, args.out)
    print(f"built {TOP_CELL} with {args.units} x {SUB_CELL}\n")

    results = {}
    for label, cell, hierarchy in (
        ("sub_flat", SUB_CELL, False),
        ("top_hier", TOP_CELL, True),
        ("top_flat", TOP_CELL, False),
    ):
        spice = extract(gds, cell, args.out, label, hierarchy)
        if spice is None:
            print(f"{label}: extraction produced no netlist")
            continue
        count, textual = textual_total(spice)
        expanded = expanded_total(spice, cell)
        negatives = sum(
            1 for _, _, v, s in _C_LINE.findall(spice.read_text()) if _si(v, s) < 0
        )
        results[label] = (count, textual, expanded, negatives)

    print(f"{'extraction':<12}{'#C lines':>10}{'textual fF':>13}{'expanded fF':>14}{'#neg':>7}")
    print("-" * 56)
    for label, (count, textual, expanded, negatives) in results.items():
        print(f"{label:<12}{count:>10}{textual * 1e15:>13.3f}{expanded * 1e15:>14.3f}{negatives:>7}")

    if {"sub_flat", "top_hier", "top_flat"} <= results.keys():
        sub = results["sub_flat"][2]
        hier = results["top_hier"]
        flat = results["top_flat"][2]
        print(f"\n{args.units} x sub_flat expanded      = {sub * args.units * 1e15:.3f} fF")
        print(f"top_hier textual              = {hier[1] * 1e15:.3f} fF")
        print(f"top_hier expanded by instance = {hier[2] * 1e15:.3f} fF")
        print(f"top_flat                      = {flat * 1e15:.3f} fF")
        if hier[1] > 0:
            print(f"\nflat / hier-textual  = {flat / hier[1]:.2f}x")
        if hier[2] > 0:
            print(f"flat / hier-expanded = {flat / hier[2]:.2f}x")
        print(
            "\nIf flat/hier-textual is near the instance count while flat/hier-expanded\n"
            "is near 1, the apparent loss was the textual sum, not the extractor."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
