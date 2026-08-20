#!/usr/bin/env python3
"""Extract the catalog 70 um bond pad alone, with its ESD column, and plate controls.

The driver Magic deck has 144 fF ``outp``–``vss``. A lone ``bondpad_70um`` is
80 fF. This adds the driver's ESD column (``diodevdd`` over ``diodevss``,
9 um gap, Metal2 PAD bar) to see how much of the remaining 64 fF is those
diodes.

Nothing is written into the repo; GDS and PEX go to a scratch directory.

Usage:
    source ~/.local/share/ihp-eda/env.sh
    python layout/debug_pex/probe_standalone_pad.py
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from layout.blocks.draw import place, rect, snap, via_between
from layout.blocks.driver_stage import (
    ESD_BUS_W,
    ESD_PAD_GAP,
    ESD_STACK_GAP,
    PAD_EXIT_LENGTH,
    PAD_FEED_METAL,
    _place_at,
)
from layout.common.devices import build
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


def _catalog():
    by_name = {spec.name: spec for spec in esd_devices()}
    return by_name["bondpad_70um"], by_name["esd_diodevdd_2kv"], by_name["esd_diodevss_2kv"]


def _label_esd(layout, cell, evdd_t, evss_t, pad_name: str) -> None:
    mapping = {"PAD": pad_name, "VDD": "vdd", "VSS": "vss"}
    stamp_net_labels(layout, cell, list(evdd_t.values()), mapping)
    stamp_net_labels(layout, cell, list(evss_t.values()), mapping)


def _connect_esd_to_pad(layout, cell, pad_box, evdd_t, evss_t) -> None:
    """Driver PAD bar: Metal2 in the 9 um gap, stacked up to the pad's Metal5."""
    pad_inner = pad_box.right
    bar_x = snap(pad_inner + ESD_PAD_GAP / 2.0)
    pad_row_y = snap((pad_box.top + pad_box.bottom) / 2.0)
    pad_pin_ys = [evss_t["PAD"].center[1], evdd_t["PAD"].center[1]]
    rect(layout, cell, "Metal2", bar_x - ESD_BUS_W / 2, min(pad_pin_ys),
         bar_x + ESD_BUS_W / 2, max(pad_pin_ys))
    for pin in (evss_t["PAD"], evdd_t["PAD"]):
        px, py = pin.center
        rect(layout, cell, "Metal2", min(px, bar_x) - ESD_BUS_W / 2,
             py - ESD_BUS_W / 2, max(px, bar_x) + ESD_BUS_W / 2, py + ESD_BUS_W / 2)
    via_between(layout, cell, bar_x, pad_row_y, "Metal2", PAD_FEED_METAL)
    feed_from = snap(pad_inner - PAD_EXIT_LENGTH / 2.0)
    sig_w = route_width(PAD_FEED_METAL)
    rect(layout, cell, PAD_FEED_METAL, min(feed_from, bar_x),
         pad_row_y - sig_w / 2, max(feed_from, bar_x), pad_row_y + sig_w / 2)


def pad_plus_esd(out: Path, *, connect: bool) -> dict:
    """One pad and its ESD column at the driver's 9 um gap and stack pitch."""
    pad_spec, evdd_spec, evss_spec = _catalog()
    layout = new_layout()
    name = "pad_esd_tied" if connect else "pad_esd_near"
    cell = layout.create_cell(name)
    pad_t, pad_box = place(layout, cell, pad_spec, 0.0, 0.0)
    stamp_net_labels(layout, cell, list(pad_t.values()), {"PAD": "pad"})

    _, esd_probe = build(evdd_spec)
    esd_h = snap(esd_probe.dbbox().height())
    esd_col_h = snap(2.0 * esd_h + ESD_STACK_GAP)
    cy = (pad_box.top + pad_box.bottom) / 2.0
    band_y0 = snap(cy - esd_col_h / 2.0)
    col_outer = snap(pad_box.right + ESD_PAD_GAP)
    evss_t, _ = _place_at(
        layout, cell, evss_spec.with_name("esd_vss"), "R0",
        bottom=band_y0, left=col_outer,
    )
    evdd_t, _ = _place_at(
        layout, cell, evdd_spec.with_name("esd_vdd"), "R0",
        bottom=snap(band_y0 + esd_h + ESD_STACK_GAP), left=col_outer,
    )
    _label_esd(layout, cell, evdd_t, evss_t, "pad" if connect else "esd_pad")
    if connect:
        _connect_esd_to_pad(layout, cell, pad_box, evdd_t, evss_t)

    result = extract_gds(layout, cell, name, out)
    how = "tied (M2 bar + M5 feed)" if connect else "placed only, no bar"
    return {
        "label": f"bondpad_70um + ESD column, {how}",
        "result": result,
        "bbox": (pad_box.width(), pad_box.height()),
        "expect_shielded_fF": AREA_AF["Metal3"] * OCT_AREA * 1e-3,
        "expect_unshielded_fF": sum(AREA_AF[m] for m in STACK) * OCT_AREA * 1e-3,
        "expect_hand_fF": pad_capacitance_f(70.0, 70.0) * 1e15,
    }


def esd_column_only(out: Path) -> dict:
    """The two diodes and their PAD bar, no bond pad."""
    _, evdd_spec, evss_spec = _catalog()
    layout = new_layout()
    cell = layout.create_cell("esd_col")
    _, esd_probe = build(evdd_spec)
    esd_h = snap(esd_probe.dbbox().height())
    evss_t, evss_box = _place_at(
        layout, cell, evss_spec.with_name("esd_vss"), "R0", bottom=0.0, left=0.0,
    )
    evdd_t, evdd_box = _place_at(
        layout, cell, evdd_spec.with_name("esd_vdd"), "R0",
        bottom=snap(esd_h + ESD_STACK_GAP), left=0.0,
    )
    _label_esd(layout, cell, evdd_t, evss_t, "pad")
    bar_x = snap((evss_t["PAD"].center[0] + evss_box.left) / 2.0)
    pad_pin_ys = [evss_t["PAD"].center[1], evdd_t["PAD"].center[1]]
    rect(layout, cell, "Metal2", bar_x - ESD_BUS_W / 2, min(pad_pin_ys),
         bar_x + ESD_BUS_W / 2, max(pad_pin_ys))
    for pin in (evss_t["PAD"], evdd_t["PAD"]):
        px, py = pin.center
        rect(layout, cell, "Metal2", min(px, bar_x) - ESD_BUS_W / 2,
             py - ESD_BUS_W / 2, max(px, bar_x) + ESD_BUS_W / 2, py + ESD_BUS_W / 2)
    result = extract_gds(layout, cell, "esd_col", out)
    return {
        "label": "ESD column alone (diodevdd + diodevss + PAD bar)",
        "result": result,
        "bbox": (evdd_box.width(), evdd_box.height() + evss_box.height()),
        "expect_shielded_fF": float("nan"),
        "expect_unshielded_fF": float("nan"),
        "expect_hand_fF": float("nan"),
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
    if not math.isnan(case.get("expect_shielded_fF", float("nan"))):
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
    pad_vss = sum(v for a, b, v in items if {a, b} == {"pad", "vss"})
    print(f"  Magic: {len(items)} C  raw-sum={_ff(raw)}  pad-node={_ff(pad_c) if pad_c is not None else '—'}"
          + (f"  pad–vss={_ff(pad_vss)}" if pad_vss else ""))
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
        pad_plus_esd(args.out, connect=False),
        pad_plus_esd(args.out, connect=True),
        esd_column_only(args.out),
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
