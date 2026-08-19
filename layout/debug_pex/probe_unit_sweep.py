#!/usr/bin/env python3
"""Does a node's substrate capacitance go negative as parallel devices are added?

Written to test one hypothesis about Magic reporting negative substrate
capacitance on the CTLE stage: that it subtracts device area from a node's
substrate term because the compact model will supply it through AS/AD/PS/PD, and
that with enough parallel units the subtraction exceeds the node's own
metal-to-substrate capacitance. If that held, the substrate term would fall
monotonically with unit count and cross zero.

It does not hold -- see FINDINGS.md. Kept because the shape of the experiment is
reusable: build the same cell at several sizes, extract each, and watch one number.

Nothing is written into the repo; cells and extractions go to a scratch directory.

Usage:
    source ~/.local/share/ihp-eda/env.sh
    python layout/debug_pex/probe_unit_sweep.py [--out DIR] [--units 1,5,25]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from layout.blocks.mos_array import build_mos_array
from layout.common.gds import stamp_net_labels, write_for_magic, write_gds
from layout.common.layers import layer_map
from layout.common.pdk import new_layout, pya_module
from layout.common.pex import run_magic_pex

#: One array unit's width, matching layout/blocks/mos_array.py's finger cap.
UNIT_W = 9.72e-6

_C_LINE = re.compile(r"^C\S*\s+(\S+)\s+(\S+)\s+([-\d.eE+]+)(\w*)", re.M)
_SI = {"m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15, "a": 1e-18}


def capacitors(netlist: Path) -> list[tuple[str, str, float]]:
    """Every capacitor as ``(net_a, net_b, farads)``.

    Matches Magic's trailing ``$ **FLOATING`` annotation too: a stricter pattern
    anchored at end of line silently dropped eight of them and undercounted the
    flat total by a factor of three.
    """
    out = []
    for a, b, value, suffix in _C_LINE.findall(netlist.read_text()):
        out.append((a, b, float(value) * _SI.get(suffix[:1].lower(), 1.0)))
    return out


def per_net(items: list[tuple[str, str, float]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for a, b, value in items:
        for net in (a, b):
            totals[net] = totals.get(net, 0.0) + value
    return totals


def array_case(units: int, out: Path) -> dict:
    name = f"arr{units}"
    array = build_mos_array(name, UNIT_W * units, 1e-6, current_a=2.9e-3 * units / 25)
    stamp_net_labels(
        array.layout, array.cell, list(array.ports.values()),
        {"D": "drain", "S": "source", "G": "gate"},
    )
    raw = write_gds(array.layout, array.cell, out / f"{name}.gds", name=name)
    gds, cell = write_for_magic(raw, out / f"{name}_magic.gds", cell=name)
    result = run_magic_pex(gds=gds, cell=cell, run_dir=out / f"pex_{name}", resistance=False)
    return {"label": f"{units:>2} unit array", "result": result}


def metal_control(side_um: float, out: Path) -> dict:
    """A bare metal plate over substrate. Its substrate term must be positive."""
    pya = pya_module()
    lm = layer_map()
    layout = new_layout()
    cell = layout.create_cell("mctl")
    for key in ("metal2_drw", "metal2_pin"):
        ld = lm[key]
        cell.shapes(layout.layer(ld[0], ld[1])).insert(pya.DBox(0.0, 0.0, side_um, side_um))
    pin = lm["metal2_pin"]
    cell.shapes(layout.layer(pin[0], pin[1])).insert(
        pya.DText("plate", side_um / 2, side_um / 2)
    )
    raw = write_gds(layout, cell, out / "mctl.gds", name="mctl")
    gds, name = write_for_magic(raw, out / "mctl_magic.gds", cell="mctl")
    result = run_magic_pex(gds=gds, cell=name, run_dir=out / "pex_mctl", resistance=False)
    return {"label": f"metal plate {side_um:g} um sq", "result": result}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("/tmp/debug_pex"))
    parser.add_argument("--units", default="1,5,25")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    counts = [int(part) for part in args.units.split(",") if part.strip()]
    cases = [array_case(n, args.out) for n in counts]
    cases.append(metal_control(15.588, args.out))

    print(f"\n{'case':<24}{'#C':>5}{'total fF':>11}{'drain fF':>11}{'#neg':>6}")
    print("-" * 57)
    failed = False
    for case in cases:
        result = case["result"]
        if not result.ok:
            print(f"{case['label']:<24}  extraction failed: {result.error[:40]}")
            failed = True
            continue
        items = capacitors(Path(result.netlist))
        totals = per_net(items)
        negatives = [v for v in totals.values() if v < 0]
        drain = totals.get("drain")
        print(f"{case['label']:<24}{len(items):>5}"
              f"{sum(v for _, _, v in items) * 1e15:>11.2f}"
              f"{(f'{drain * 1e15:.2f}' if drain is not None else '-'):>11}"
              f"{len(negatives):>6}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
