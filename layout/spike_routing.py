#!/usr/bin/env python3
"""Stage 0 spike: prove PCell placement plus gdsfactory electrical routing.

The one combination the plan could not verify up front was a foundry PCell GDS
imported into gdsfactory, annotated with ports, and wired with
``route_bundle_electrical``. This script does exactly that on two resistors and
then puts the result through the PDK DRC deck, so the answer is a signoff
verdict rather than a picture.

Usage:
    python layout/spike_routing.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from layout.common.drc import run_drc
from layout.common.render import render_gds
from layout.common.route import device_component, route_electrical
from layout.common.spec import DeviceSpec
from layout.common.xsection import activate_pdk, gf_module

OUT_DIR = Path(__file__).resolve().parent / "out" / "spike"


def build_spike():
    """Two rppd resistors with their inner terminals wired together."""
    activate_pdk()
    gf = gf_module()

    spec = DeviceSpec("rppd_spike", "rppd", params={"w": 5e-6, "l": 1.4e-6})
    device = device_component(spec)

    top = gf.Component("rppd_route_spike")
    left = top << device
    right = top << device
    # Leave room for the route to turn; rppd terminals face up and down.
    left.move((0, 0))
    right.move((20, 12))

    # rppd terminals are Metal1 pins, so the bundle stays on Metal1; changing
    # metal would need a via_stack rather than a taper.
    routes = route_electrical(
        top,
        [left.ports["MINUS"]],
        [right.ports["PLUS"]],
        metal="Metal1",
        separation=2.0,
    )

    top.add_port(name="A", port=left.ports["PLUS"])
    top.add_port(name="B", port=right.ports["MINUS"])
    return top, routes


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    top, routes = build_spike()

    gds = OUT_DIR / "rppd_route_spike.gds"
    top.write_gds(gds)
    print(f"routed cell: {len(top.ports)} port(s), {len(routes)} route(s) -> {gds}")

    png = render_gds(gds, OUT_DIR / "rppd_route_spike.png")
    if png:
        print(f"rendered {png}")

    result = run_drc(gds=gds, run_dir=OUT_DIR / "drc_run", cell_name=gds.stem)
    result.write(OUT_DIR / "spike_drc.json")

    summary = {
        "ports": sorted(str(p.name) for p in top.ports),
        "routes": len(routes),
        "gds": str(gds),
        "png": str(png) if png else None,
        "drc_clean": result.clean,
        "drc_real_violations": result.real_total,
        "drc_context_violations": result.context_by_rule,
        "drc_real_by_rule": result.real_by_rule,
    }
    (OUT_DIR / "spike_summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    if result.clean:
        print(f"DRC clean (context-only: {result.context_by_rule or 'none'})")
        return 0
    print(f"DRC FAILED: {result.real_by_rule or result.error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
