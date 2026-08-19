#!/usr/bin/env python3
"""Hold the geometry fixed and vary only Magic's extraction settings.

This is the probe that found the cause of the CTLE stage's negative substrate
capacitance: hierarchy, not `cthresh`, and not anything about the layout. See
FINDINGS.md.

Keeping it runnable matters because the same question recurs whenever an
extraction result looks wrong -- is it the geometry or is it how we asked. Taking
the GDS from git rather than from the working tree means a concurrent regeneration
cannot change the answer half way through.

Nothing is written into the repo; extractions go to a scratch directory.

Usage:
    source ~/.local/share/ihp-eda/env.sh
    python layout/debug_pex/probe_extract_settings.py
    python layout/debug_pex/probe_extract_settings.py --gds some.gds --cell name
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

from layout.common.paths import pdk_paths

#: Committed stage layout, used when no --gds is given.
DEFAULT_GDS = "layout/blocks/out/ctle_stage/ctle_dut.gds"
DEFAULT_CELL = "ctle_dut"

_C_LINE = re.compile(r"^C\S*\s+(\S+)\s+(\S+)\s+([-\d.eE+]+)(\w*)", re.M)
_SI = {"m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15, "a": 1e-18}


def tcl(gds: Path, cell: str, out_spice: Path, cthresh: str, hierarchy: bool) -> str:
    lines = [
        "crashbackups stop", "drc off", f"gds read {gds}",
        f"load {cell} -dereference", "select top cell",
        "extract path .", "extract all",
        "ext2spice lvs", f"ext2spice cthresh {cthresh}", "ext2spice rthresh 0",
        "ext2spice subcircuit on",
    ]
    if not hierarchy:
        lines.append("ext2spice hierarchy off")
    lines += [f"ext2spice -o {out_spice}", "quit -noprompt"]
    return "\n".join(lines) + "\n"


def run(label: str, gds: Path, cell: str, out: Path, cthresh: str = "0",
        hierarchy: bool = True) -> dict:
    run_dir = out / f"set_{label}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    out_spice = run_dir / f"{cell}_pex.spice"
    script = run_dir / "extract.tcl"
    script.write_text(tcl(gds, cell, out_spice, cthresh, hierarchy))

    env = dict(os.environ)
    env.setdefault("PDK_ROOT", str(pdk_paths().root))
    completed = subprocess.run(
        [shutil.which("magic"), "-dnull", "-noconsole",
         "-rcfile", str(pdk_paths().magic_rcfile), str(script)],
        cwd=run_dir, capture_output=True, text=True, timeout=1800, check=False, env=env,
    )
    (run_dir / "magic.log").write_text(f"{completed.stdout}\n{completed.stderr}")
    if not out_spice.is_file():
        return {"label": label, "ok": False, "why": (completed.stderr or "no output")[-120:]}

    caps = [
        (a, b, float(v) * _SI.get(s[:1].lower(), 1.0))
        for a, b, v, s in _C_LINE.findall(out_spice.read_text())
    ]
    negatives = sorted((c for c in caps if c[2] < 0), key=lambda c: c[2])
    return {
        "label": label, "ok": True, "count": len(caps),
        "total": sum(c[2] for c in caps), "negatives": negatives,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gds", default=None, help="defaults to the committed stage GDS, from git")
    parser.add_argument("--cell", default=DEFAULT_CELL)
    parser.add_argument("--out", type=Path, default=Path("/tmp/debug_pex"))
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    if args.gds:
        gds = Path(args.gds).resolve()
    else:
        # From git, so a concurrent regeneration cannot move the geometry mid-run.
        gds = args.out / "from_git.gds"
        gds.write_bytes(
            subprocess.run(["git", "show", f"HEAD:{DEFAULT_GDS}"], cwd=REPO_ROOT,
                           capture_output=True, check=True).stdout
        )

    cases = [
        run("cthresh0", gds, args.cell, args.out),
        run("cthresh0p01", gds, args.cell, args.out, cthresh="0.01"),
        run("cthresh1", gds, args.cell, args.out, cthresh="1"),
        run("flat", gds, args.cell, args.out, hierarchy=False),
        run("flat_cthresh1", gds, args.cell, args.out, cthresh="1", hierarchy=False),
    ]

    print(f"\ngeometry: {gds}\n")
    print(f"{'setting':<16}{'#C':>6}{'total fF':>11}{'#neg':>6}{'worst fF':>11}")
    print("-" * 50)
    for case in cases:
        if not case["ok"]:
            print(f"{case['label']:<16}  failed: {case['why'][:40]}")
            continue
        worst = case["negatives"][0][2] * 1e15 if case["negatives"] else 0.0
        print(f"{case['label']:<16}{case['count']:>6}{case['total'] * 1e15:>11.2f}"
              f"{len(case['negatives']):>6}{worst:>11.2f}")

    for case in cases:
        if case.get("ok") and case["negatives"]:
            print(f"\nnegative terms under {case['label']}:")
            for a, b, value in case["negatives"]:
                print(f"   {a:<24} {b:<12} {value * 1e15:>10.3f} fF")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
