#!/usr/bin/env python3
"""Extract the catalog 70 um bond pad alone, and two plate controls.

The driver Magic deck has 144 fF ``outp``–``vss``. The hand ``PAD_C`` is
27.68 fF of TM1 area-to-sub. This asks whether a standalone ``bondpad_70um``
(Metal3–TM2 stacked octagon, PDK defaults) really extracts at 144 fF, or
whether that number is the pad sitting inside the driver's vss ring.

Controls:
  * TM1 70×70 square — the geometry the hand formula assumes
  * Metal3 70×70 square — the stack's actual bottom plate if Magic shields

Nothing is written into the repo; GDS and PEX go to a scratch directory.

Usage:
    source ~/.local/share/ihp-eda/env.sh
    python layout/debug_pex/probe_standalone_pad.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from layout.blocks.draw import place
from layout.common.gds import stamp_net_labels, write_for_magic, write_gds
from layout.common.layers import layer_map
from layout.common.pdk import new_layout, pya_module
from layout.common.pex import run_magic_pex
from layout.common.rules import route_width
from layout.debug_pex.probe_unit_sweep import capacitors, per_net
from layout.devices.catalog import esd_devices

sys.path.insert(0, str(REPO / "circuits/ctle56n/python"))
from size_term import pad_capacitance_f  # noqa: E402

AREA_AF = {
    "Metal1": 35.015,
    "Metal2": 18.180,
    "Metal3": 11.994,
    "Metal4": 8.948,
    "Metal5": 7.136,
    "TopMetal1": 5.649,
    "TopMetal2": 3.233,
}
STACK = ("Metal3", "Metal4", "Metal5", "TopMetal1", "TopMetal2")
# Regular octagon, flat-to-flat = diameter.
OCT_AREA = 2.0 * 70.0 ** 2 / (1.0 + 2.0 ** 0.5)
SQ_AREA = 70.0 * 70.0


def _ff(farads: float) -> str:
    return f"{farads * 1e15:.2f} fF"


def extract_gds(layout, cell, name: str, out: Path):
    raw = write_gds(layout, cell, out / f"{name}.gds", name=name)
    gds, top = write_for_magic(raw, out / f"{name}_magic.gds", cell=name)
    result = run_magic_pex(
        gds=gds, cell=top, run_dir=out / f"pex_{name}", resistance=False
    )
    return result


def catalog_pad(out: Path):
    pad = next(spec for spec in esd_devices() if spec.name == "bondpad_70um")
    layout = new_layout()
    cell = layout.create_cell("pad_alone")
    placed, bbox = place(layout, cell, pad, 0.0, 0.0)
    stamp_net_labels(layout, cell, list(placed.values()), {"PAD": "pad"})
    result = extract_gds(layout, cell, "pad_alone", out)
    return {
        "label": "bondpad_70um PCell (M3–TM2 octagon)",
        "result": result,
        "bbox": (bbox.width(), bbox.height()),
        "expect_shielded_fF": AREA_AF["Metal3"] * OCT_AREA * 1e-3,
        "expect_unshielded_fF": sum(AREA_AF[m] for m in STACK) * OCT_AREA * 1e-3,
        "expect_hand_fF": pad_capacitance_f(70.0, 70.0) * 1e15,
    }


def pad_in_vss_ring(out: Path, clearance_um: float = 6.0):
    """Catalog pad inside a TopMetal2 vss frame, driver-like clearance."""
    pad = next(spec for spec in esd_devices() if spec.name == "bondpad_70um")
    layout = new_layout()
    cell = layout.create_cell("pad_ring")
    placed, bbox = place(layout, cell, pad, 0.0, 0.0)
    stamp_net_labels(layout, cell, list(placed.values()), {"PAD": "pad"})
    w = route_width("TopMetal2")
    inner = (
        bbox.left - clearance_um,
        bbox.bottom - clearance_um,
        bbox.right + clearance_um,
        bbox.top + clearance_um,
    )
    outer = (inner[0] - w, inner[1] - w, inner[2] + w, inner[3] + w)
    pya = pya_module()
    lm = layer_map()
    for metal in ("TopMetal2",):
        ld = lm[f"{metal.lower()}_drw"]
        layer = layout.layer(ld[0], ld[1])
        cell.shapes(layer).insert(pya.DBox(outer[0], outer[1], outer[2], inner[1]))
        cell.shapes(layer).insert(pya.DBox(outer[0], inner[3], outer[2], outer[3]))
        cell.shapes(layer).insert(pya.DBox(outer[0], inner[1], inner[0], inner[3]))
        cell.shapes(layer).insert(pya.DBox(inner[2], inner[1], outer[2], inner[3]))
    pin = lm["topmetal2_pin"]
    cell.shapes(layout.layer(pin[0], pin[1])).insert(
        pya.DText("vss", (outer[0] + outer[2]) / 2.0, (outer[1] + inner[1]) / 2.0)
    )
    result = extract_gds(layout, cell, "pad_ring", out)
    return {
        "label": f"bondpad_70um + TM2 vss ring ({clearance_um:g} um gap)",
        "result": result,
        "bbox": (bbox.width(), bbox.height()),
        "expect_shielded_fF": AREA_AF["Metal3"] * OCT_AREA * 1e-3,
        "expect_unshielded_fF": sum(AREA_AF[m] for m in STACK) * OCT_AREA * 1e-3,
        "expect_hand_fF": pad_capacitance_f(70.0, 70.0) * 1e15,
    }


def plate(metal: str, side_um: float, out: Path, name: str):
    pya = pya_module()
    lm = layer_map()
    layout = new_layout()
    cell = layout.create_cell(name)
    for key in (f"{metal.lower()}_drw", f"{metal.lower()}_pin"):
        ld = lm[key]
        cell.shapes(layout.layer(ld[0], ld[1])).insert(
            pya.DBox(0.0, 0.0, side_um, side_um)
        )
    pin = lm[f"{metal.lower()}_pin"]
    cell.shapes(layout.layer(pin[0], pin[1])).insert(
        pya.DText("pad", side_um / 2.0, side_um / 2.0)
    )
    result = extract_gds(layout, cell, name, out)
    return {
        "label": f"{metal} {side_um:g} um square",
        "result": result,
        "bbox": (side_um, side_um),
        "expect_shielded_fF": AREA_AF[metal] * side_um * side_um * 1e-3,
        "expect_unshielded_fF": AREA_AF[metal] * side_um * side_um * 1e-3,
        "expect_hand_fF": pad_capacitance_f(side_um, side_um) * 1e15 if metal == "TopMetal1" else float("nan"),
    }


def summarize(case: dict) -> None:
    result = case["result"]
    print(f"\n{case['label']}")
    print(f"  bbox {case['bbox'][0]:.2f} × {case['bbox'][1]:.2f} um")
    print(f"  area-only predict  shielded={case['expect_shielded_fF']:.2f} fF"
          f"  unshielded-stack={case['expect_unshielded_fF']:.2f} fF"
          f"  hand-TM1={case['expect_hand_fF']:.2f} fF")
    if not result.ok:
        print(f"  EXTRACTION FAILED: {result.error}")
        return
    items = capacitors(Path(result.netlist))
    totals = per_net(items)
    raw = sum(v for _, _, v in items)
    pad_c = totals.get("pad")
    print(f"  Magic: {len(items)} C  raw-sum={_ff(raw)}  pad-node={_ff(pad_c) if pad_c is not None else '—'}")
    for a, b, v in sorted(items, key=lambda t: -t[2]):
        print(f"    {a:16s} — {b:16s}  {_ff(v)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("/tmp/debug_pex_pad"))
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    print("Hand PAD_C is TM1 area only: 70×70 × 5.649 aF/µm² = "
          f"{pad_capacitance_f(70.0, 70.0) * 1e15:.2f} fF")
    print(f"Octagon area at 70 um flat-to-flat: {OCT_AREA:.0f} µm² "
          f"(square {SQ_AREA:.0f} µm²)")
    print("Driver in-situ Magic: 143.56 fF outp–vss")

    cases = [
        catalog_pad(args.out),
        pad_in_vss_ring(args.out),
        plate("TopMetal1", 70.0, args.out, "tm1_sq70"),
        plate("Metal3", 70.0, args.out, "m3_sq70"),
    ]
    failed = False
    for case in cases:
        summarize(case)
        if not case["result"].ok:
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
