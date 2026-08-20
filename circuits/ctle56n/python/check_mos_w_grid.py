#!/usr/bin/env python3
"""Pin schematic ``snap_drawable_mos_w`` to layout ``plan_units``.

The circuit side cannot import from ``layout/``, so both carry a copy of the
same arithmetic. Run this after changing either side.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from layout.blocks.mos_array import plan_units

from size_ctle import snap_drawable_mos_w


def _check_one(w_um: float) -> None:
    units, unit_w = plan_units(w_um * 1e-6)
    layout_total = units * unit_w * 1e6
    schematic_total = snap_drawable_mos_w(w_um)
    if abs(layout_total - schematic_total) > 1e-9:
        raise AssertionError(
            f"w={w_um} um: plan_units -> {layout_total} um, "
            f"snap_drawable_mos_w -> {schematic_total} um"
        )


def main() -> int:
    probes = [
        243.0,
        246.875,
        200.0,
        392.0,
        784.0,
        784.075,
        783.68,
        9.925,
        9.72,
        123.456,
        400.0,
    ]
    for w_um in probes:
        _check_one(w_um)
    print(f"ok: {len(probes)} widths agree between snap_drawable_mos_w and plan_units")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
