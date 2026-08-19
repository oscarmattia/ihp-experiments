#!/usr/bin/env python3
"""Summarize IHP BJT LUTs: β peak, gm/Ic, Early voltage, fT + Gummel plots."""

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

DEVICES = ("npn13G2", "npn13G2l", "npn13G2v", "pnpMPA")


def nearest_index(axis: np.ndarray, value: float) -> int:
    return int(np.argmin(np.abs(axis - value)))


def geom_label(meta: dict, i: int) -> str:
    geoms = meta.get("geometries") or []
    if i >= len(geoms):
        return f"geo{i}"
    g = geoms[i]
    if "Nx" in g and "El" in g:
        return f"Nx={int(g['Nx'])} El={g['El']}"
    if "Nx" in g:
        return f"Nx={int(g['Nx'])}"
    if "a" in g:
        return f"a={g['a']:.2e} p={g['p']:.2e}"
    return str(g)


def summarize_device(path: Path) -> list[dict]:
    arrays, meta = load_lut(path)
    vbe = np.asarray(arrays["VBE"], dtype=float)
    vce = np.asarray(arrays["VCE"], dtype=float)
    ic = np.asarray(arrays["IC"], dtype=float)  # (n_geo, n_vce, n_vbe)
    beta = np.asarray(arrays["BETA"], dtype=float)
    gm_ic = np.asarray(arrays["GM_IC"], dtype=float)
    va = np.asarray(arrays["VA"], dtype=float)
    ft = np.asarray(arrays.get("FT", np.nan), dtype=float)
    cin = np.asarray(arrays.get("CIN", np.nan), dtype=float)

    k_vce = nearest_index(vce, 1.2 if meta.get("polarity") == "npn" else 1.0)
    rows: list[dict] = []
    for i in range(ic.shape[0]):
        ic_c = ic[i, k_vce, :]
        beta_c = beta[i, k_vce, :]
        gm_ic_c = gm_ic[i, k_vce, :]
        va_c = va[i, k_vce, :]
        ft_c = ft[i, k_vce, :] if ft.shape == ic.shape else np.full_like(ic_c, np.nan)
        cin_c = cin[i, k_vce, :] if cin.shape == ic.shape else np.full_like(ic_c, np.nan)
        # Peak β in forward active (ignore pathological edges)
        mid = (vbe > 0.65) & (vbe < 0.9) & np.isfinite(beta_c)
        peak_beta = float(np.nanmax(beta_c[mid])) if np.any(mid) else float(np.nanmax(beta_c))
        idx = int(np.nanargmax(beta_c[mid])) if np.any(mid) else int(np.nanargmax(beta_c))
        vbe_at_peak = float(vbe[mid][idx]) if np.any(mid) else float(vbe[idx])
        # Peak fT where finite (HBT AC pass)
        ft_valid = np.isfinite(ft_c) & (ft_c > 0)
        if np.any(ft_valid):
            k_ft = int(np.nanargmax(ft_c))
            peak_ft = float(ft_c[k_ft])
            vbe_at_peak_ft = float(vbe[k_ft])
            ic_at_peak_ft = float(ic_c[k_ft])
            cin_at_peak_ft = float(cin_c[k_ft])
        else:
            peak_ft = float("nan")
            vbe_at_peak_ft = float("nan")
            ic_at_peak_ft = float("nan")
            cin_at_peak_ft = float("nan")
        # gm/Ic near VBE=0.8
        j08 = nearest_index(vbe, 0.8)
        rows.append(
            {
                "device": meta.get("device", path.stem),
                "geometry": geom_label(meta, i),
                "VCE_V": float(vce[k_vce]),
                "peak_beta": peak_beta,
                "VBE_at_peak_beta": vbe_at_peak,
                "peak_ft_Hz": peak_ft,
                "VBE_at_peak_ft": vbe_at_peak_ft,
                "Ic_at_peak_ft_A": ic_at_peak_ft,
                "Cin_at_peak_ft_F": cin_at_peak_ft,
                "gm_over_Ic_at_0p8V": float(gm_ic_c[j08]),
                "VA_approx_at_0p8V": float(va_c[j08]),
                "Ic_at_0p8V_A": float(ic_c[j08]),
            }
        )
    return rows


def plot_ft(path: Path, out_dir: Path) -> None:
    arrays, meta = load_lut(path)
    device = meta.get("device", path.stem)
    if "FT" not in arrays:
        return
    vbe = np.asarray(arrays["VBE"], dtype=float)
    vce = np.asarray(arrays["VCE"], dtype=float)
    ic = np.asarray(arrays["IC"], dtype=float)
    ft = np.asarray(arrays["FT"], dtype=float)
    if not np.any(np.isfinite(ft)):
        return

    k = nearest_index(vce, 1.2 if meta.get("polarity") == "npn" else 1.0)
    fig, axes = plt.subplots(1, 2, figsize=(10, 4), constrained_layout=True)
    for i in range(ic.shape[0]):
        label = geom_label(meta, i)
        ft_slice = ft[i, k, :] / 1e9
        ic_slice = ic[i, k, :]
        axes[0].plot(vbe, ft_slice, label=label)
        axes[1].semilogx(np.maximum(ic_slice, 1e-20), ft_slice, label=label)
    axes[0].set_xlabel("|VBE| [V]")
    axes[0].set_ylabel("fT [GHz]")
    axes[0].set_title(f"{device} fT vs |VBE| @ |VCE|={vce[k]:.2f} V")
    axes[0].grid(True, alpha=0.3)
    axes[0].legend(fontsize=7)
    axes[1].set_xlabel("|Ic| [A]")
    axes[1].set_ylabel("fT [GHz]")
    axes[1].set_title(f"{device} fT vs |Ic| @ |VCE|={vce[k]:.2f} V")
    axes[1].grid(True, which="both", alpha=0.3)
    axes[1].legend(fontsize=7)
    fig.savefig(out_dir / f"{device}_ft.png", dpi=140)
    plt.close(fig)


def plot_gummel(path: Path, out_dir: Path) -> None:
    arrays, meta = load_lut(path)
    device = meta.get("device", path.stem)
    vbe = np.asarray(arrays["VBE"], dtype=float)
    vce = np.asarray(arrays["VCE"], dtype=float)
    ic = np.asarray(arrays["IC"], dtype=float)
    ib = np.asarray(arrays["IB"], dtype=float)
    beta = np.asarray(arrays["BETA"], dtype=float)
    k = nearest_index(vce, 1.2 if meta.get("polarity") == "npn" else 1.0)

    fig, axes = plt.subplots(1, 2, figsize=(10, 4), constrained_layout=True)
    for i in range(ic.shape[0]):
        label = geom_label(meta, i)
        axes[0].semilogy(vbe, np.maximum(ic[i, k, :], 1e-20), label=f"Ic {label}")
        axes[0].semilogy(vbe, np.maximum(ib[i, k, :], 1e-20), linestyle="--", alpha=0.7)
        axes[1].plot(vbe, beta[i, k, :], label=label)
    axes[0].set_xlabel("|VBE| [V]")
    axes[0].set_ylabel("|I| [A]")
    axes[0].set_title(f"{device} Gummel @ |VCE|={vce[k]:.2f} V")
    axes[0].grid(True, which="both", alpha=0.3)
    axes[0].legend(fontsize=7)
    axes[1].set_xlabel("|VBE| [V]")
    axes[1].set_ylabel("β = Ic/Ib")
    axes[1].set_title(f"{device} current gain")
    axes[1].grid(True, alpha=0.3)
    axes[1].legend(fontsize=7)
    fig.savefig(out_dir / f"{device}_gummel.png", dpi=140)
    plt.close(fig)


def plot_beta_bar(rows: list[dict], out_dir: Path) -> None:
    if not rows:
        return
    labels = [f"{r['device']}:{r['geometry']}" for r in rows]
    vals = [r["peak_beta"] for r in rows]
    fig, ax = plt.subplots(figsize=(max(8, 0.35 * len(labels)), 4), constrained_layout=True)
    ax.bar(range(len(labels)), vals, color="#2a6f97")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=55, ha="right", fontsize=7)
    ax.set_ylabel("peak β")
    ax.set_title("Peak DC current gain (mid-VBE, |VCE|≈1.2 V)")
    ax.grid(True, axis="y", alpha=0.3)
    fig.savefig(out_dir / "beta_comparison.png", dpi=140)
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
        raise SystemExit(f"missing {out_dir}; run ihp_bjt_sweep.py first")

    all_rows: list[dict] = []
    for key in DEVICES:
        path = out_dir / f"sg13_{key}.npz"
        if not path.exists():
            print(f"skip missing {path.name}")
            continue
        all_rows.extend(summarize_device(path))
        plot_gummel(path, out_dir)
        plot_ft(path, out_dir)

    csv_path = out_dir / "summary.csv"
    fields = list(all_rows[0].keys()) if all_rows else []
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"wrote {csv_path}")

    if all_rows:
        plot_beta_bar(all_rows, out_dir)
        print(f"{'device':<12} {'geometry':<28} {'peak β':>10} {'peak fT GHz':>12} {'gm/Ic':>10}")
        for r in all_rows:
            ft_ghz = r["peak_ft_Hz"] / 1e9 if np.isfinite(r["peak_ft_Hz"]) else float("nan")
            print(
                f"{r['device']:<12} {r['geometry']:<28} "
                f"{r['peak_beta']:10.1f} {ft_ghz:12.1f} {r['gm_over_Ic_at_0p8V']:10.2f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
