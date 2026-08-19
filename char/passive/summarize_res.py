#!/usr/bin/env python3
"""Summarize IHP resistor LUTs: CSV table + R vs L / R vs T plots."""

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

MODELS = ("rsil", "rppd", "rhigh")


def nearest_index(axis: np.ndarray, value: float) -> int:
    return int(np.argmin(np.abs(axis - value)))


def summarize_model(path: Path) -> list[dict]:
    arrays, meta = load_lut(path)
    w = np.asarray(arrays["W"], dtype=float)
    l = np.asarray(arrays["L"], dtype=float)
    temp = np.asarray(arrays["TEMP"], dtype=float)
    r = np.asarray(arrays["R"], dtype=float)
    device = meta.get("device", path.stem.replace("sg13_", ""))

    rows: list[dict] = []
    for iw, wv in enumerate(w):
        for il, lv in enumerate(l):
            for it, tv in enumerate(temp):
                rows.append(
                    {
                        "model": device,
                        "W_um": float(wv),
                        "L_um": float(lv),
                        "TEMP_C": float(tv),
                        "R_ohm": float(r[iw, il, it]),
                    }
                )
    return rows


def plot_r_vs_l(path: Path, out_dir: Path) -> None:
    arrays, meta = load_lut(path)
    device = meta.get("device", path.stem.replace("sg13_", ""))
    w = np.asarray(arrays["W"], dtype=float)
    l = np.asarray(arrays["L"], dtype=float)
    temp = np.asarray(arrays["TEMP"], dtype=float)
    r = np.asarray(arrays["R"], dtype=float)
    k_t = nearest_index(temp, 27.0)

    fig, ax = plt.subplots(figsize=(7, 4.5), constrained_layout=True)
    for iw, wv in enumerate(w):
        ax.plot(l, r[iw, :, k_t], marker="o", label=f"W={wv:g} µm")
    ax.set_xlabel("L [µm]")
    ax.set_ylabel("R [Ω]")
    ax.set_title(f"{device}: R vs L @ {temp[k_t]:g} °C")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)
    fig.savefig(out_dir / f"{device}_r_vs_l_27C.png", dpi=140)
    plt.close(fig)


def plot_r_vs_t(path: Path, out_dir: Path) -> None:
    arrays, meta = load_lut(path)
    device = meta.get("device", path.stem.replace("sg13_", ""))
    w = np.asarray(arrays["W"], dtype=float)
    l = np.asarray(arrays["L"], dtype=float)
    temp = np.asarray(arrays["TEMP"], dtype=float)
    r = np.asarray(arrays["R"], dtype=float)

    iw = len(w) // 2
    il = len(l) // 2
    w_mid = float(w[iw])
    l_mid = float(l[il])

    fig, ax = plt.subplots(figsize=(6, 4), constrained_layout=True)
    ax.plot(temp, r[iw, il, :], marker="o")
    ax.set_xlabel("Temperature [°C]")
    ax.set_ylabel("R [Ω]")
    ax.set_title(f"{device}: R vs T @ W={w_mid:g} µm, L={l_mid:g} µm")
    ax.grid(True, alpha=0.3)
    fig.savefig(out_dir / f"{device}_r_vs_temp_midgeo.png", dpi=140)
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
        raise SystemExit(f"missing {out_dir}; run ihp_res_sweep.py first")

    all_rows: list[dict] = []
    for key in MODELS:
        path = out_dir / f"sg13_{key}.npz"
        if not path.exists():
            print(f"skip missing {path.name}")
            continue
        all_rows.extend(summarize_model(path))
        plot_r_vs_l(path, out_dir)
        plot_r_vs_t(path, out_dir)

    csv_path = out_dir / "res_summary.csv"
    fields = ["model", "W_um", "L_um", "TEMP_C", "R_ohm"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"wrote {csv_path}")

    if all_rows:
        print(f"{'model':<8} {'W':>6} {'L':>6} {'T':>6} {'R[Ω]':>12}")
        for r in all_rows[:8]:
            print(
                f"{r['model']:<8} {r['W_um']:6g} {r['L_um']:6g} "
                f"{r['TEMP_C']:6g} {r['R_ohm']:12.4g}"
            )
        if len(all_rows) > 8:
            print(f"... ({len(all_rows)} rows total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
