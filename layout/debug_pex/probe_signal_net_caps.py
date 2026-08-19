#!/usr/bin/env python3
"""Where the extracted capacitance actually goes, net by net.

A total over every capacitor in a deck is not a load on anything. On the CTLE stage
that total is dominated by the power grid -- two nets running side by side all the
way round the cell -- and quoting it against `CL_INTERCONNECT`, which is the routing
allowance on the *output node*, compares unrelated quantities.

This splits the extracted capacitance by net, isolates the four signal nets, and
compares each against two independent references:

* the sizing script's allowance, `CL_INTERCONNECT` = 40 um x `ROUTING_CAP_FF_PER_UM`
* a standalone wire of the same layer, width and drawn length, extracted the same
  way, so the difference is what the *surroundings* add rather than the wire

Nothing is written into the repo.

Usage:
    source ~/.local/share/ihp-eda/env.sh
    python layout/debug_pex/probe_signal_net_caps.py [--deck PATH] [--out DIR]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from layout.common.gds import write_gds
from layout.common.layers import layer_map
from layout.common.pdk import new_layout, pya_module
from layout.common.pex import run_magic_pex
from layout.common.sizing import read_params

SIGNAL_NETS = ("outp", "outn", "inp", "inn")

#: A signal trunk is a narrow vertical run. Anything wider is a plate or a bus.
MAX_TRUNK_WIDTH = 6.0
TRUNK_X_TOLERANCE = 1.0
SUPPLY_NETS = ("vdd", "vss")

_C_LINE = re.compile(r"^C\S*\s+(\S+)\s+(\S+)\s+([-\d.eE+]+)(\w*)", re.M)
_SI = {"m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15, "a": 1e-18}


def caps(deck: Path) -> list[tuple[str, str, float]]:
    return [
        (a, b, float(v) * _SI.get(s[:1].lower(), 1.0))
        for a, b, v, s in _C_LINE.findall(deck.read_text())
    ]


def trunk_geometry(block, metals=("Metal4", "Metal5")) -> dict[str, list[tuple[str, float, float]]]:
    """Vertical trunk runs per signal net, as ``(metal, width_um, length_um)``.

    Located by x, using the trunk positions the generator already records in
    ``Block.symmetry``, so this cannot drift from the placement.
    """
    pya = pya_module()
    lm = layer_map()
    pairs = block.symmetry["pairs"]
    xs = {
        "outp": pairs["out_trunks"][0], "outn": pairs["out_trunks"][1],
        "inp": pairs["in_trunks"][0], "inn": pairs["in_trunks"][1],
    }

    found: dict[str, list[tuple[str, float, float]]] = {net: [] for net in SIGNAL_NETS}
    for metal in metals:
        ld = lm[f"{metal.lower()}_drw"]
        layer = block.layout.layer(ld[0], ld[1])
        region = pya.Region(block.cell.begin_shapes_rec(layer)).merged()
        for polygon in region.each():
            box = polygon.bbox().to_dtype(block.layout.dbu)
            if box.height() <= box.width():
                continue  # only the vertical runs
            # A trunk is narrow and centred on its x. Without the width bound, a
            # tolerance scaled to the polygon's own width matched anything wide
            # enough to span the trunk position -- the degeneration capacitor's
            # 37 um Metal5 plate was being reported as four different nets' trunk.
            if box.width() > MAX_TRUNK_WIDTH:
                continue
            for net, x in xs.items():
                if abs(box.center().x - x) < TRUNK_X_TOLERANCE:
                    found[net].append((metal, round(box.width(), 4), round(box.height(), 4)))
    return found


def standalone_wire(metal: str, width: float, length: float, out: Path, tag: str) -> float | None:
    """Capacitance of one isolated wire of this geometry, extracted the same way."""
    pya = pya_module()
    lm = layer_map()
    layout = new_layout()
    cell = layout.create_cell(f"wire_{tag}")
    for key in (f"{metal.lower()}_drw", f"{metal.lower()}_pin"):
        ld = lm[key]
        cell.shapes(layout.layer(ld[0], ld[1])).insert(
            pya.DBox(0.0, 0.0, width, length)
        )
    pin = lm[f"{metal.lower()}_pin"]
    cell.shapes(layout.layer(pin[0], pin[1])).insert(
        pya.DText("w", width / 2, length / 2)
    )
    gds = write_gds(layout, cell, out / f"wire_{tag}.gds", name=f"wire_{tag}")
    result = run_magic_pex(
        gds=gds, cell=f"wire_{tag}", run_dir=out / f"pex_wire_{tag}", resistance=False
    )
    if not result.ok:
        return None
    return sum(abs(v) for _, _, v in caps(Path(result.netlist)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deck", type=Path, default=Path("/tmp/plfix2/pex_run/ctle_dut_pex.spice"))
    parser.add_argument("--out", type=Path, default=Path("/tmp/debug_pex_signal"))
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    if not args.deck.is_file():
        print(f"no deck at {args.deck}; run layout/blocks/run_postlayout.py first")
        return 1

    items = caps(args.deck)
    total = sum(v for _, _, v in items)

    print(f"deck: {args.deck}")
    print(f"total over every capacitor: {total * 1e15:.2f} fF  ({len(items)} terms)\n")

    supply_only = sum(
        v for a, b, v in items if {a, b} <= set(SUPPLY_NETS) | {"sub"}
    )
    print("Where it goes")
    print("-" * 62)
    print(f"  {'between supply nets and substrate only':<44}{supply_only * 1e15:>9.2f} fF"
          f"  {supply_only / total * 100:>5.1f}%")
    signal_touching = sum(v for a, b, v in items if a in SIGNAL_NETS or b in SIGNAL_NETS)
    print(f"  {'touching a signal net (outp/outn/inp/inn)':<44}{signal_touching * 1e15:>9.2f} fF"
          f"  {signal_touching / total * 100:>5.1f}%")
    other = total - supply_only - signal_touching
    print(f"  {'other internal nets (e1/e2/mgate/nlp)':<44}{other * 1e15:>9.2f} fF"
          f"  {other / total * 100:>5.1f}%")

    print("\nLargest single terms")
    print("-" * 62)
    for a, b, v in sorted(items, key=lambda t: -abs(t[2]))[:6]:
        print(f"  {a:<20} {b:<20} {v * 1e15:>9.2f} fF")

    print("\nNode capacitance on each signal net (every term touching it)")
    print("-" * 62)
    per_net = {}
    for net in SIGNAL_NETS:
        per_net[net] = sum(v for a, b, v in items if net in (a, b))
        print(f"  {net:<8}{per_net[net] * 1e15:>9.3f} fF")

    params = read_params()
    cl = float(params["CL"])
    cl_inter = float(params["CL_INTERCONNECT"])
    cl_miller = float(params["CL_MILLER"])
    print(f"\nSchematic's load model on an output node: CL = {cl * 1e15:.2f} fF"
          f"  (Miller {cl_miller * 1e15:.2f} + interconnect {cl_inter * 1e15:.2f})")
    print("The testbench still applies CL post-layout, so extracted parasitics add to it.")
    for net in ("outp", "outn"):
        print(f"  extracted on {net:<5} {per_net[net] * 1e15:>8.3f} fF  vs the "
              f"{cl_inter * 1e15:.2f} fF interconnect allowance -> "
              f"{per_net[net] / cl_inter:.1f}x")

    # Drawn geometry, then the same wire on its own.
    from layout.blocks.ctle_stage import build_ctle_stage
    from layout.common import simview

    block = build_ctle_stage(black_box=simview.BLACK_BOX_KINDS)
    geometry = trunk_geometry(block)
    per_um = 0.17  # size_ctle.ROUTING_CAP_FF_PER_UM

    print("\nDrawn trunk geometry, and the same wire extracted standalone")
    print("-" * 96)
    print(f"  {'net':<6}{'metal':<9}{'w um':>7}{'len um':>9}{'standalone fF':>15}"
          f"{'fF/um':>9}{'sizing 0.17 fF/um':>19}")
    for net, runs in geometry.items():
        for metal, width, length in runs:
            alone = standalone_wire(metal, width, length, args.out, f"{net}_{metal}")
            if alone is None:
                print(f"  {net:<6}{metal:<9}{width:>7.2f}{length:>9.2f}   extraction failed")
                continue
            print(f"  {net:<6}{metal:<9}{width:>7.2f}{length:>9.2f}{alone * 1e15:>15.3f}"
                  f"{alone * 1e15 / length:>9.4f}{length * per_um:>19.2f}")

    print("\nSum of standalone wires vs what the extraction attributes to each net")
    print("-" * 96)
    for net, runs in geometry.items():
        alone_total = 0.0
        for metal, width, length in runs:
            value = standalone_wire(metal, width, length, args.out, f"{net}_{metal}")
            if value:
                alone_total += value
        ratio = per_net[net] / alone_total if alone_total else float("nan")
        print(f"  {net:<6} standalone {alone_total * 1e15:>8.3f} fF   in situ "
              f"{per_net[net] * 1e15:>8.3f} fF   {ratio:>5.1f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
