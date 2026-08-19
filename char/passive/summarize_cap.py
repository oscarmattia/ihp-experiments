#!/usr/bin/env python3
"""Summarize IHP capacitor LUTs: cap_summary.csv + geometry / C(V) plots."""

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

MIM_FILES = ("sg13_cap_cmim.npz", "sg13_cap_cmomi.npz")
MOSCAP_FILES = ("sg13_moscap_n.npz", "sg13_moscap_p.npz")


def _fmt_cap(val: float) -> str:
    if not np.isfinite(val):
        return "nan"
    if val >= 1e-9:
        return f"{val * 1e9:.3f} nF"
    if val >= 1e-12:
        return f"{val * 1e12:.2f} pF"
    if val >= 1e-15:
        return f"{val * 1e15:.2f} fF"
    return f"{val * 1e18:.2f} aF"


def summarize_mim(path: Path) -> list[dict]:
    arrays, meta = load_lut(path)
    w = np.asarray(arrays["W"], dtype=float)
    l = np.asarray(arrays["L"], dtype=float)
    c = np.asarray(arrays["C"], dtype=float)
    dens = np.asarray(arrays.get("C_density_fF_um2", c / (w * l) * 1e15), dtype=float)
    rows: list[dict] = []
    for i in range(len(w)):
        rows.append(
            {
                "device": meta.get("device", path.stem),
                "geometry": f"w={w[i]:g}µm l={l[i]:g}µm",
                "w_um": w[i],
                "l_um": l[i],
                "area_um2": w[i] * l[i],
                "C_F": c[i],
                "C": _fmt_cap(c[i]),
                "C_density_fF_um2": dens[i],
                "bias_V": "",
            }
        )
    return rows


def summarize_moscap(path: Path) -> list[dict]:
    arrays, meta = load_lut(path)
    w = np.asarray(arrays["W"], dtype=float)
    v = np.asarray(arrays["V"], dtype=float)
    c = np.asarray(arrays["C"], dtype=float)
    rows: list[dict] = []
    for i in range(c.shape[0]):
        for j, vb in enumerate(v):
            rows.append(
                {
                    "device": meta.get("device", path.stem),
                    "geometry": f"W=L={w[i]:g}µm",
                    "w_um": w[i],
                    "l_um": w[i],
                    "area_um2": w[i] * w[i],
                    "C_F": c[i, j],
                    "C": _fmt_cap(c[i, j]),
                    "C_density_fF_um2": c[i, j] / (w[i] * w[i]) * 1e15 if w[i] > 0 else np.nan,
                    "bias_V": vb,
                }
            )
    return rows


def plot_mim_area(path: Path, out_dir: Path) -> None:
    arrays, meta = load_lut(path)
    device = meta.get("device", path.stem)
    area = np.asarray(arrays["W"], dtype=float) * np.asarray(arrays["L"], dtype=float)
    c_ff = np.asarray(arrays["C"], dtype=float) * 1e15
    fig, ax = plt.subplots(figsize=(6, 4), constrained_layout=True)
    ax.plot(area, c_ff, "o-", color="#2a6f97")
    for a, c in zip(area, c_ff):
        ax.annotate(f"{c:.1f} fF", (a, c), textcoords="offset points", xytext=(4, 4), fontsize=7)
    ax.set_xlabel("area (µm²)")
    ax.set_ylabel("C (fF)")
    ax.set_title(f"{device}: C vs area (AC -3dB)")
    ax.grid(True, alpha=0.3)
    fig.savefig(out_dir / f"{device}_c_vs_area.png", dpi=140)
    plt.close(fig)


def plot_moscap_cv(path: Path, out_dir: Path) -> None:
    arrays, meta = load_lut(path)
    device = meta.get("device", path.stem)
    w = np.asarray(arrays["W"], dtype=float)
    v = np.asarray(arrays["V"], dtype=float)
    c = np.asarray(arrays["C"], dtype=float) * 1e15
    fig, ax = plt.subplots(figsize=(6, 4), constrained_layout=True)
    for i in range(c.shape[0]):
        ax.plot(v, c[i, :], "o-", label=f"W=L={w[i]:g} µm")
    ax.set_xlabel("gate bias V (V)")
    ax.set_ylabel("C (fF)")
    ax.set_title(f"{device}: C(V)")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=7)
    fig.savefig(out_dir / f"{device}_cv.png", dpi=140)
    plt.close(fig)


def plot_density_bar(rows: list[dict], out_dir: Path) -> None:
    mim_rows = [r for r in rows if r.get("bias_V") == ""]
    if not mim_rows:
        return
    labels = [f"{r['device']}:{r['geometry']}" for r in mim_rows]
    vals = [r["C_density_fF_um2"] for r in mim_rows]
    fig, ax = plt.subplots(figsize=(max(7, 0.4 * len(labels)), 4), constrained_layout=True)
    ax.bar(range(len(labels)), vals, color="#e76f51")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=50, ha="right", fontsize=7)
    ax.set_ylabel("C density (fF/µm²)")
    ax.set_title("MIM / MoM capacitance density (AC -3dB)")
    ax.axhline(1.5, color="gray", linestyle="--", linewidth=0.8, label="nominal 1.5 fF/µm²")
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend(fontsize=8)
    fig.savefig(out_dir / "cap_density_comparison.png", dpi=140)
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
        raise SystemExit(f"missing {out_dir}; run ihp_cap_sweep.py first")

    all_rows: list[dict] = []
    for name in MIM_FILES:
        path = out_dir / name
        if not path.exists():
            print(f"skip missing {name}")
            continue
        all_rows.extend(summarize_mim(path))
        plot_mim_area(path, out_dir)

    for name in MOSCAP_FILES:
        path = out_dir / name
        if not path.exists():
            print(f"skip missing {name}")
            continue
        all_rows.extend(summarize_moscap(path))
        plot_moscap_cv(path, out_dir)

    csv_path = out_dir / "cap_summary.csv"
    fields = [
        "device",
        "geometry",
        "w_um",
        "l_um",
        "area_um2",
        "bias_V",
        "C_F",
        "C",
        "C_density_fF_um2",
    ]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"wrote {csv_path}")

    if all_rows:
        plot_density_bar(all_rows, out_dir)
        print(f"{'device':<12} {'geometry':<22} {'bias':>6} {'C':>12} {'dens fF/µm²':>12}")
        for r in all_rows[:12]:
            bias = f"{r['bias_V']:.2g}" if r["bias_V"] != "" else "—"
            print(
                f"{r['device']:<12} {r['geometry']:<22} {bias:>6} "
                f"{r['C']:>12} {r['C_density_fF_um2']:>12.3f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
