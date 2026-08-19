#!/usr/bin/env python3
"""Run the PDK LVS deck over the generated device catalog.

Each device is compared against the CDL that ``gen_devices.py`` wrote from the
same DeviceSpec, and the extracted geometry is then diffed against the spec.
The second check is the one that catches a PCell ``Calculate`` mistake: LVS can
match topology while the device is the wrong size.

Usage:
    python layout/devices/run_lvs.py
    python layout/devices/run_lvs.py --only rppd_load
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from layout.common.lvs import check_extracted_params, run_lvs
from layout.common.spec import read_specs

OUT_DIR = Path(__file__).resolve().parent / "out"

#: Devices with no LVS device class to compare against. ``via_stack`` is pure
#: interconnect, so there is nothing for the deck to extract as a device.
SKIP = {"via_stack"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--only", action="append", default=[])
    args = parser.parse_args(argv)

    manifest_path = args.out / "manifest.json"
    if not manifest_path.is_file():
        parser.error(f"{manifest_path} missing; run gen_devices.py first")
    manifest = json.loads(manifest_path.read_text())
    specs = {s.name: s for s in read_specs(args.out / "specs.json")}

    devices = [d for d in manifest["devices"] if d["kind"] not in SKIP]
    if args.only:
        wanted = set(args.only)
        devices = [d for d in devices if d["name"] in wanted]

    results = {}
    failed = []
    for device in devices:
        name = device["name"]
        result = run_lvs(
            gds=args.out / device["gds"],
            cdl=args.out / device["cdl"],
            run_dir=args.out / "lvs_run" / name,
            topcell=name,
        )
        if result.clean and result.extracted_netlist:
            ok, checks = check_extracted_params(specs[name], Path(result.extracted_netlist))
            result.params_ok = ok
            result.param_checks = checks
        result.write(args.out / "lvs" / f"{name}_lvs.json")
        results[name] = result

        if result.clean and result.params_ok:
            worst = max((c.get("rel_error", 0) for c in result.param_checks), default=0)
            print(f"  {name:<26} LVS clean   params match (max rel err {worst:.4f})")
        elif result.clean:
            bad = [c for c in result.param_checks if not c.get("ok", True)]
            detail = ", ".join(
                f"{c.get('param')}: want {c.get('intended')} got {c.get('extracted')}"
                for c in bad
            ) or result.param_checks
            print(f"  {name:<26} LVS clean   PARAM MISMATCH: {detail}")
            failed.append(name)
        else:
            print(f"  {name:<26} LVS FAIL    {result.summary or result.error}")
            failed.append(name)

    summary = {
        "clean": not failed,
        "total_devices": len(results),
        "failed": failed,
        "by_device": {n: r.to_dict() for n, r in results.items()},
    }
    (args.out / "lvs_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"\n{len(results) - len(failed)}/{len(results)} device(s) LVS clean")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
