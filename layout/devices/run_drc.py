#!/usr/bin/env python3
"""Run the PDK DRC deck over the generated device catalog.

Density and antenna checks stay off: both need the surrounding chip context, so
an isolated device cell fails them by construction and the result would say
nothing about the device. They belong at block or top level.

Usage:
    python layout/devices/run_drc.py
    python layout/devices/run_drc.py --only rppd_load
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from layout.common.drc import run_drc

OUT_DIR = Path(__file__).resolve().parent / "out"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--density", action="store_true", help="also run density checks")
    parser.add_argument("--antenna", action="store_true", help="also run antenna checks")
    args = parser.parse_args(argv)

    manifest_path = args.out / "manifest.json"
    if not manifest_path.is_file():
        parser.error(f"{manifest_path} missing; run gen_devices.py first")
    manifest = json.loads(manifest_path.read_text())

    devices = manifest["devices"]
    if args.only:
        wanted = set(args.only)
        devices = [d for d in devices if d["name"] in wanted]

    results = {}
    failed = []
    for device in devices:
        name = device["name"]
        gds = args.out / device["gds"]
        result = run_drc(
            gds=gds,
            run_dir=args.out / "drc_run" / name,
            cell_name=name,
            density=args.density,
            antenna=args.antenna,
        )
        result.write(args.out / "drc" / f"{name}_drc.json")
        results[name] = result
        if result.clean:
            context = result.context_by_rule
            note = ""
            if context:
                note = "  (context-only: " + ", ".join(
                    f"{r}={c}" for r, c in context.items()
                ) + ")"
            print(f"  {name:<26} DRC clean{note}")
        else:
            rules = ", ".join(f"{r}={c}" for r, c in list(result.real_by_rule.items())[:6])
            detail = result.error or rules or "unknown"
            print(f"  {name:<26} DRC FAIL  {result.real_total} violation(s): {detail}")
            failed.append(name)

    summary = {
        "clean": not failed,
        "total_devices": len(results),
        "failed": failed,
        "density_checked": args.density,
        "antenna_checked": args.antenna,
        "context_rules_allowed": True,
        "by_device": {n: r.to_dict() for n, r in results.items()},
    }
    (args.out / "drc_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"\n{len(results) - len(failed)}/{len(results)} device(s) DRC clean")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
