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
from char.passive.ind_validate import (  # noqa: E402
    l_at_freq,
    l_low_freq,
    peak_q,
    srf_ghz,
    validate_ind_lut,
)

# Preferred row order when multiple .npz files are present.
CASE_ORDER = (
    "l2n0",
    "turn1",
    "turn1_d40",
    "turn1_d60",
    "turn1_d80",
    "turn2",
)


def _discover_cases(out_dir: Path) -> list[str]:
    keys = [p.stem.replace("sg13_ind_", "") for p in sorted(out_dir.glob("sg13_ind_*.npz"))]
    order = {k: i for i, k in enumerate(CASE_ORDER)}
    return sorted(keys, key=lambda k: (order.get(k, len(CASE_ORDER)), k))


def summarize_case(path: Path) -> dict:
    arrays, meta = load_lut(path)
    freq = np.asarray(arrays["FREQ"], dtype=float)
    l_series = np.asarray(arrays["L"], dtype=float)
    q_series = np.asarray(arrays["Q"], dtype=float)
    case = meta.get("case", path.stem.replace("sg13_ind_", ""))
    em_completed = bool(meta.get("em_completed", False))
    valid, invalid_reason = validate_ind_lut(
        freq,
        l_series,
        q_series,
        em_completed=em_completed,
    )
    f_peak, q_peak = peak_q(freq, q_series)
    fstop_hz = meta.get("fstop_hz")
    if fstop_hz is None and len(freq) > 1:
        fstop_hz = float(freq[-1])
    return {
        "case": case,
        "nr_r": meta.get("nr_r"),
        "w_um": meta.get("w"),
        "s_um": meta.get("s"),
        "d_um": meta.get("d"),
        "L_DC_nH": l_low_freq(freq, l_series) * 1e9,
        "L_10GHz_nH": l_at_freq(freq, l_series, 10e9) * 1e9,
        "L_28GHz_nH": l_at_freq(freq, l_series, 28e9) * 1e9,
        "Q_peak": q_peak,
        "f_Q_peak_GHz": f_peak / 1e9 if np.isfinite(f_peak) else np.nan,
        "SRF_GHz": srf_ghz(freq, l_series),
        "fstop_GHz": float(fstop_hz) / 1e9 if fstop_hz is not None else np.nan,
        "valid": valid,
        "invalid_reason": invalid_reason or "",
        "em_completed": em_completed,
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
    valid = meta.get("valid")
    if valid is None:
        valid, _ = validate_ind_lut(
            freq,
            l_series,
            q_series,
            em_completed=bool(meta.get("em_completed", False)),
        )
    title_suffix = "" if valid else " [INVALID]"

    fig, (ax_l, ax_q) = plt.subplots(2, 1, figsize=(7, 6), constrained_layout=True, sharex=True)
    ax_l.plot(f_ghz, l_series * 1e9, "k-", linewidth=1.5)
    ax_l.set_ylabel("L [nH]")
    ax_l.set_title(f"{case}: differential series L(f){title_suffix}")
    ax_l.grid(True, alpha=0.3)

    ax_q.plot(f_ghz, q_series, "r-", linewidth=1.5)
    ax_q.set_ylabel("Q")
    ax_q.set_xlabel("Frequency [GHz]")
    ax_q.set_title(f"{case}: differential Q(f){title_suffix}")
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
    for key in _discover_cases(out_dir):
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
        "L_DC_nH",
        "L_10GHz_nH",
        "L_28GHz_nH",
        "Q_peak",
        "f_Q_peak_GHz",
        "SRF_GHz",
        "fstop_GHz",
        "valid",
        "invalid_reason",
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
        print(
            f"{'case':<12} {'N':>3} {'L@10GHz[nH]':>12} {'Q_peak':>8} "
            f"{'valid':>5} {'em':>5}"
        )
        for r in rows:
            print(
                f"{r['case']:<12} {r['nr_r']!s:>3} "
                f"{r['L_10GHz_nH']:12.4g} {r['Q_peak']:8.2f} "
                f"{str(r['valid']):>5} {str(r['em_completed']):>5}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
