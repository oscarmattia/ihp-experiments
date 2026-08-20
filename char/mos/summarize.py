"""Summarize IHP MOSFET LUTs: Vth, Ion, peak gm/ID + comparison plots."""

from __future__ import annotations

import argparse
import csv
import pickle
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from char.common.lut import load_lut as load_npz  # noqa: E402

FAMILIES = ("lv_core", "lv_rf", "hv_core", "hv_rf")
POLARITIES = ("n", "p")


def load_device_lut(path: Path) -> dict:
    """Load `.npz` (preferred) or pygmid `.pkl`."""
    if path.suffix == ".npz":
        arrays, _meta = load_npz(path)
        return arrays
    with path.open("rb") as f:
        return pickle.load(f)


def resolve_lut(out_dir: Path, family: str, pol: str) -> Path | None:
    npz = out_dir / f"{family}_{pol}.npz"
    pkl = out_dir / f"{family}_{pol}.pkl"
    if npz.exists():
        return npz
    if pkl.exists():
        return pkl
    return None


def nearest_index(axis: np.ndarray, value: float) -> int:
    return int(np.argmin(np.abs(axis - value)))


def summarize_device(name: str, lut: dict) -> list[dict]:
    """One row per L at VSB=0, VDS≈VDD (last point), mid-L bias summary."""
    rows = []
    lengths = np.asarray(lut["L"], dtype=float)
    vds = np.asarray(lut["VDS"], dtype=float)
    vsb = np.asarray(lut["VSB"], dtype=float)
    w = float(lut["W"])

    j_vsb0 = nearest_index(vsb, 0.0)
    k_vds = len(vds) - 1  # ~VDD
    id_ = lut["ID"]  # (L, VGS, VDS, VSB)
    vt = lut["VT"]
    gm = lut["GM"]
    gds = lut["GDS"]

    for i_l, length in enumerate(lengths):
        id_curve = id_[i_l, :, k_vds, j_vsb0]
        vt_curve = vt[i_l, :, k_vds, j_vsb0]
        gm_curve = gm[i_l, :, k_vds, j_vsb0]
        gds_curve = np.maximum(np.abs(gds[i_l, :, k_vds, j_vsb0]), 1e-30)

        # Model Vth: median of |VT| over strong-inversion points
        strong = np.abs(id_curve) > (1e-6 * w)  # >1 µA/µm
        if np.any(strong):
            vth = float(np.median(np.abs(vt_curve[strong])))
        else:
            vth = float(np.median(np.abs(vt_curve)))

        ion = float(np.abs(id_curve[-1]) / w)  # A/µm at VGS=VDD, VDS=VDD
        gm_id = np.abs(gm_curve) / np.maximum(np.abs(id_curve), 1e-30)
        peak_gm_id = float(np.nanmax(gm_id[np.isfinite(gm_id)]))
        # Intrinsic gain proxy near peak gm/ID
        idx = int(np.nanargmax(gm_id))
        gain = float(np.abs(gm_curve[idx] / gds_curve[idx]))

        rows.append(
            {
                "device": name,
                "L_um": float(length),
                "W_um": w,
                "VDS_V": float(vds[k_vds]),
                "VSB_V": float(vsb[j_vsb0]),
                "Vth_V": vth,
                "Ion_A_per_um": ion,
                "peak_gm_over_ID_1_per_V": peak_gm_id,
                "gm_over_gds_at_peak_gmID": gain,
            }
        )
    return rows


def plot_family(out_dir: Path, family: str) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(10, 4), constrained_layout=True)
    for pol, ax in zip(POLARITIES, axes):
        path = resolve_lut(out_dir, family, pol)
        if path is None:
            continue
        lut = load_device_lut(path)
        lengths = np.asarray(lut["L"], dtype=float)
        vgs = np.asarray(lut["VGS"], dtype=float)
        vds = np.asarray(lut["VDS"], dtype=float)
        vsb = np.asarray(lut["VSB"], dtype=float)
        j0 = nearest_index(vsb, 0.0)
        k = len(vds) - 1
        i_l = 0  # shortest L
        id_curve = np.abs(lut["ID"][i_l, :, k, j0])
        gm_curve = np.abs(lut["GM"][i_l, :, k, j0])
        gm_id = gm_curve / np.maximum(id_curve, 1e-30)
        ax.semilogx(id_curve / float(lut["W"]), gm_id, label=f"L={lengths[i_l]}µm")
        # also longest L
        if len(lengths) > 1:
            i_l = -1
            id_curve = np.abs(lut["ID"][i_l, :, k, j0])
            gm_curve = np.abs(lut["GM"][i_l, :, k, j0])
            gm_id = gm_curve / np.maximum(id_curve, 1e-30)
            ax.semilogx(
                id_curve / float(lut["W"]), gm_id, label=f"L={lengths[i_l]}µm"
            )
        ax.set_xlabel("|Id|/W [A/µm]")
        ax.set_ylabel("gm/ID [1/V]")
        ax.set_title(f"{family} {pol.upper()}MOS")
        ax.grid(True, which="both", alpha=0.3)
        ax.legend(fontsize=8)
    fig.suptitle(f"gm/ID methodology — {family}")
    fig.savefig(out_dir / f"{family}_gm_id.png", dpi=140)
    plt.close(fig)

    # Id-Vg lin/log at VDS=VDD, VSB=0, shortest L
    fig, axes = plt.subplots(1, 2, figsize=(10, 4), constrained_layout=True)
    for pol, ax in zip(POLARITIES, axes):
        path = resolve_lut(out_dir, family, pol)
        if path is None:
            continue
        lut = load_device_lut(path)
        vgs = np.asarray(lut["VGS"], dtype=float)
        vds = np.asarray(lut["VDS"], dtype=float)
        vsb = np.asarray(lut["VSB"], dtype=float)
        j0 = nearest_index(vsb, 0.0)
        k = len(vds) - 1
        id_curve = np.abs(lut["ID"][0, :, k, j0])
        ax.plot(vgs, id_curve * 1e3, label="lin")
        ax.set_xlabel("|VGS| [V]")
        ax.set_ylabel("|Id| [mA] (W=1µm)")
        ax.set_title(f"{family} {pol.upper()}MOS Id–Vg")
        ax.grid(True, alpha=0.3)
        ax2 = ax.twinx()
        ax2.semilogy(vgs, np.maximum(id_curve, 1e-20), color="C1", alpha=0.7)
        ax2.set_ylabel("|Id| [A] log", color="C1")
    fig.savefig(out_dir / f"{family}_idvg.png", dpi=140)
    plt.close(fig)


def plot_vth_comparison(rows: list[dict], out_dir: Path) -> None:
    # Group by device, use shortest L
    by_dev: dict[str, dict] = {}
    for row in rows:
        key = row["device"]
        if key not in by_dev or row["L_um"] < by_dev[key]["L_um"]:
            by_dev[key] = row
    labels = sorted(by_dev)
    vals = [by_dev[k]["Vth_V"] for k in labels]
    fig, ax = plt.subplots(figsize=(9, 4), constrained_layout=True)
    ax.bar(labels, vals, color="#2a6f97")
    ax.set_ylabel("|Vth| [V]")
    ax.set_title("Threshold comparison (shortest L, VSB=0, VDS≈VDD)")
    ax.tick_params(axis="x", rotation=30)
    ax.grid(True, axis="y", alpha=0.3)
    fig.savefig(out_dir / "vth_comparison.png", dpi=140)
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
        raise SystemExit(f"missing {out_dir}; run ihp_sweep.py first")

    all_rows: list[dict] = []
    for family in FAMILIES:
        for pol in POLARITIES:
            path = resolve_lut(out_dir, family, pol)
            if path is None:
                print(f"skip missing {family}_{pol}.npz/.pkl")
                continue
            lut = load_device_lut(path)
            all_rows.extend(summarize_device(f"{family}_{pol}", lut))
        plot_family(out_dir, family)

    csv_path = out_dir / "summary.csv"
    fields = list(all_rows[0].keys()) if all_rows else []
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"wrote {csv_path}")

    if all_rows:
        plot_vth_comparison(all_rows, out_dir)
        # Print compact table
        print(
            f"{'device':<16} {'L':>5} {'Vth':>8} {'Ion':>12} {'peak gm/ID':>11}"
        )
        for r in all_rows:
            print(
                f"{r['device']:<16} {r['L_um']:5.2f} {r['Vth_V']:8.3f} "
                f"{r['Ion_A_per_um']:12.3e} {r['peak_gm_over_ID_1_per_V']:11.2f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
