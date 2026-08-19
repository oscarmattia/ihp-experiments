#!/usr/bin/env python3
"""Generate the single-device layout catalog: GDS, CDL, ports, manifest, PNGs.

Usage:
    python layout/devices/gen_devices.py
    python layout/devices/gen_devices.py --only rppd_load --no-render
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # allow running as a plain script
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from layout.common.devices import build, kind_of
from layout.common.gds import layer_summary, stamp_net_labels, write_gds
from layout.common.netlist import write_cdl
from layout.common.render import render_gds
from layout.common.spec import DeviceSpec, write_specs
from layout.common.wrap import derive_terminals
from layout.devices.catalog import full_catalog

OUT_DIR = Path(__file__).resolve().parent / "out"


def generate(spec: DeviceSpec, out_dir: Path, render: bool = True) -> dict:
    """Build one device and write its artifacts; returns a manifest entry."""
    kind = kind_of(spec)
    layout, cell = build(spec)
    terminals = derive_terminals(spec, layout, cell)

    # Net names double as terminal names for a standalone device, which is what
    # makes the LVS compare able to check the port mapping.
    skipped = stamp_net_labels(layout, cell, terminals)

    gds_dir = out_dir / "gds"
    cdl_dir = out_dir / "cdl"
    gds_path = write_gds(layout, cell, gds_dir / f"{spec.name}.gds")
    cdl_path = write_cdl(spec, cdl_dir / f"{spec.name}.cdl")

    png_path = None
    if render:
        png_path = render_gds(gds_path, out_dir / "png" / f"{spec.name}.png")

    bbox = cell.dbbox()
    entry = {
        "name": spec.name,
        "kind": spec.kind,
        "pcell": kind.pcell,
        "spice_model": kind.spice_model,
        "note": spec.note,
        "params": spec.params,
        "pcell_params": kind.to_pcell(spec.params),
        "cdl_params": kind.to_cdl(spec.params),
        "bbox_um": {
            "width": round(bbox.width(), 4),
            "height": round(bbox.height(), 4),
            "area": round(bbox.width() * bbox.height(), 4),
        },
        "terminals": [t.to_dict() for t in terminals],
        "unlabelled_terminals": skipped,
        "layers": layer_summary(layout, cell),
        "gds": str(gds_path.relative_to(out_dir)),
        "cdl": str(cdl_path.relative_to(out_dir)),
        "png": str(png_path.relative_to(out_dir)) if png_path else None,
    }
    return entry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT_DIR, help="output directory")
    parser.add_argument("--only", action="append", default=[], help="generate only these devices")
    parser.add_argument("--no-render", action="store_true", help="skip PNG rendering")
    args = parser.parse_args(argv)

    specs = full_catalog()
    if args.only:
        wanted = set(args.only)
        specs = [s for s in specs if s.name in wanted]
        missing = wanted - {s.name for s in specs}
        if missing:
            parser.error(f"unknown device(s): {sorted(missing)}")

    args.out.mkdir(parents=True, exist_ok=True)
    entries = []
    failures = []
    for spec in specs:
        try:
            entry = generate(spec, args.out, render=not args.no_render)
            entries.append(entry)
            terminals = ", ".join(t["name"] for t in entry["terminals"])
            print(
                f"  {spec.name:<26} {entry['bbox_um']['width']:8.2f} x "
                f"{entry['bbox_um']['height']:8.2f} um   [{terminals}]"
            )
        except Exception as exc:  # noqa: BLE001 - report and continue the batch
            failures.append({"name": spec.name, "error": f"{type(exc).__name__}: {exc}"})
            print(f"  {spec.name:<26} FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)

    write_specs(specs, args.out / "specs.json")
    manifest = {"devices": entries, "failures": failures}
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"\n{len(entries)} device(s) generated, {len(failures)} failed -> {args.out}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
