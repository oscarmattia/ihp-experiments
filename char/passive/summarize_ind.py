#!/usr/bin/env python3
"""Summarize IHP inductor EM LUTs: ind_summary.csv + L(f) / Q(f) plots."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from char.common.lut import load_lut  # noqa: E402

CASE_KEYS = ("l2n0", "turn1", "turn2")


def _peak_q(freq: np.ndarray, q: np.ndarray) -> tuple[float, float]:
    mask = np.isfinite(q) & np.isfinite(freq)
    if not np.any(mask):
        return np.nan, np.nan
    qi = int(np.nanargmax(q[mask]))
    f_valid = freq[mask]
    q_valid = q[mask]
    return float(f_valid[qi]), float(q_valid[qi])


def _l_at_freq(freq: np.ndarray, l: np.ndarray, target_hz: float) -> float:
    mask = np.isfinite(freq) & np.isfinite(l)
    if not np.any(mask):
        return np.nan
    f_valid = freq[mask]
    l_valid = l[mask]
    idx = int(np.searchsorted(f_valid, target_hz))
    idx = min(idx, len(f_valid) - 1)
    return float(l_valid[idx])


def summarize_case(path: Path) -> dict:
    arrays, meta = load_lut(path)
    freq = np.asarray(arrays["FREQ"], dtype=float)
    l_series = np.asarray(arrays["L"], dtype=float)
    q_series = np.asarray(arrays["Q"], dtype=float)
    case = meta.get("case", path.stem.replace("sg13_ind_", ""))
    f_peak, q_peak = _peak_q(freq, q_series)
    return {
        "case": case,
        "nr_r": meta.get("nr_r"),
        "w_um": meta.get("w"),
        "s_um": meta.get("s"),
        "d_um": meta.get("d"),
        "L_10GHz_nH": _l_at_freq(freq, l_series, 10e9) * 1e9,
        "L_DC_nH": _l_at_freq(freq, l_series, 0.0) * 1e9 if freq[0] == 0 else np.nan,
        "Q_peak": q_peak,
        "f_Q_peak_GHz": f_peak / 1e9 if np.isfinite(f_peak) else np.nan,
        "em_completed": meta.get("em_completed", False),
        "solver": meta.get("solver", ""),
        "gds": meta.get("gds", ""),
    }


def plot_l_q(path: Path, out_dir: Path) -> None:
    arrays, meta = load_lut(path)
    case = meta.get("case", path.stem.replace("sg13_ind_", ""))
    freq = np.asarray(arrays["FREQ"], dtype=float)
    l_series = np.asarray(arrays["L"], dtype=float)
    q_series = np.asarray(arrays["Q"], dtype=float)
    f_ghz = freq / 1e9

    fig, (ax_l, ax_q) = plt.subplots(2, 1, figsize=(7, 6), constrained_layout=True, sharex=True)
    ax_l.plot(f_ghz, l_series * 1e9, "k-", linewidth=1.5)
    ax_l.set_ylabel("L [nH]")
    ax_l.set_title(f"{case}: differential series L(f)")
    ax_l.grid(True, alpha=0.3)

    ax_q.plot(f_ghz, q_series, "r-", linewidth=1.5)
    ax_q.set_ylabel("Q")
    ax_q.set_xlabel("Frequency [GHz]")
    ax_q.set_title(f"{case}: differential Q(f)")
    ax_q.grid(True, alpha=0.3)

    fig.savefig(out_dir / f"ind_{case}_LQ.png", dpi=140)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "out",
    )
    args = parser.parse_args()
    out_dir = args.out_dir
    if not out_dir.exists():
        raise SystemExit(f"missing {out_dir}; run ihp_ind_em.py first")

    rows: list[dict] = []
    for key in CASE_KEYS:
        path = out_dir / f"sg13_ind_{key}.npz"
        if not path.exists():
            print(f"skip missing {path.name}")
            continue
        rows.append(summarize_case(path))
        plot_l_q(path, out_dir)

    csv_path = out_dir / "ind_summary.csv"
    fields = [
        "case",
        "nr_r",
        "w_um",
        "s_um",
        "d_um",
        "L_10GHz_nH",
        "L_DC_nH",
        "Q_peak",
        "f_Q_peak_GHz",
        "em_completed",
        "solver",
        "gds",
    ]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {csv_path}")

    if rows:
        print(f"{'case':<8} {'N':>3} {'L@10GHz[nH]':>12} {'Q_peak':>8} {'em':>5}")
        for r in rows:
            print(
                f"{r['case']:<8} {r['nr_r']!s:>3} "
                f"{r['L_10GHz_nH']:12.4g} {r['Q_peak']:8.2f} "
                f"{str(r['em_completed']):>5}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
