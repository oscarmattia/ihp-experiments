#!/usr/bin/env python3
"""Regenerate markdown design reports from committed simulation artifacts.

Parses existing `out/*/metrics.csv`, `sbr_taps.csv`, and related CSVs — does not run ngspice.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_EXP = Path(__file__).resolve().parents[1]
if str(_EXP / "python") not in sys.path:
    sys.path.insert(0, str(_EXP / "python"))

from ctlelib.reports import generate_all_reports  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate stage markdown reports from committed out/ artifacts",
    )
    parser.add_argument(
        "--exp",
        type=Path,
        default=_EXP,
        help="Experiment root (default: circuits/ctle56n)",
    )
    args = parser.parse_args()
    written = generate_all_reports(args.exp)
    for path in written:
        print(f"Wrote {path}")


if __name__ == "__main__":
    main()
