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
from char.passive.ind_pimodel import (  # noqa: E402
    SP_FSTOP_HZ,
    SpVerifyResult,
    load_em_sparams,
)
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
    r28 = np.nan
    if "R_SERIES" in arrays:
        idx28 = int(np.argmin(np.abs(freq - 28e9)))
        r28 = float(arrays["R_SERIES"][idx28])
    pimodel = meta.get("pimodel") or {}
    return {
        "case": case,
        "nr_r": meta.get("nr_r"),
        "w_um": meta.get("w"),
        "s_um": meta.get("s"),
        "d_um": meta.get("d"),
        "L_DC_nH": l_low_freq(freq, l_series) * 1e9,
        "L_10GHz_nH": l_at_freq(freq, l_series, 10e9) * 1e9,
        "L_28GHz_nH": l_at_freq(freq, l_series, 28e9) * 1e9,
        "L_PI_10GHz_nH": (
            l_at_freq(freq, np.asarray(arrays.get("L_PI", l_series), dtype=float), 10e9) * 1e9
            if "L_PI" in arrays
            else np.nan
        ),
        "C_port_fF": pimodel.get("C_port_fF", np.nan),
        "G_port_mS": pimodel.get("G_port_mS", np.nan),
        "R_sub_ohm": pimodel.get("R_sub_ohm", np.nan),
        "R_series_28GHz_ohm": r28,
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


def write_sp_validation_artifacts(
    out_dir: Path,
    artifact_dir: Path,
    results: list[SpVerifyResult] | None = None,
) -> tuple[Path, list[Path]]:
    """Write SP validation summary CSV, per-case CSVs, and overlay plots."""
    size_ind_dir = _REPO / "circuits/ctle56n/python"
    if str(size_ind_dir) not in sys.path:
        sys.path.insert(0, str(size_ind_dir))
    from size_ind import (  # noqa: WPS433
        build_ind_shunt_model_for_case,
        run_ind_shunt_sp,
        write_ind_shunt_inc,
    )

    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    out_dir = Path(out_dir)

    if results is None:
        if str(size_ind_dir) not in sys.path:
            sys.path.insert(0, str(size_ind_dir))
        from size_ind import verify_all_em_cases

        results = verify_all_em_cases(out_dir=out_dir, artifact_dir=None)

    summary_path = artifact_dir / "ind_sp_validate_summary.csv"
    summary_fields = [
        "case",
        "source",
        "passed",
        "fit_s21_mean_rel_pct",
        "fit_s21_max_rel_pct",
        "fit_s21_phase_mean_deg",
        "fit_s21_phase_max_deg",
        "fit_s11_mean_rel_pct",
        "extrap_s21_mean_rel_pct",
        "extrap_s21_max_rel_pct",
        "extrap_s21_phase_mean_deg",
        "failures",
    ]
    summary_rows: list[dict] = []
    detail_paths: list[Path] = []

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        for res in results:
            case = res.case
            npz_path = out_dir / f"sg13_ind_{case}.npz"
            _, meta = load_lut(npz_path)
            fstop = float(meta.get("fstop_hz", SP_FSTOP_HZ))
            model = build_ind_shunt_model_for_case(case)
            inc = work / f"ind_shunt_{case}.inc"
            write_ind_shunt_inc(model, inc)
            freq_m, s11_m, s21_m, _ = run_ind_shunt_sp(inc, f_stop_hz=min(fstop, SP_FSTOP_HZ))
            em_freq, s11_e, s21_e, _, _ = load_em_sparams(npz_path)

            # Align on EM grid.
            s11_mi = np.array([s11_m[int(np.argmin(np.abs(freq_m - f)))] for f in em_freq])
            s21_mi = np.array([s21_m[int(np.argmin(np.abs(freq_m - f)))] for f in em_freq])

            csv_path = artifact_dir / f"ind_sp_validate_{case}.csv"
            with csv_path.open("w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "freq_Hz",
                    "S21_mag_model",
                    "S21_mag_em",
                    "S21_ang_deg_model",
                    "S21_ang_deg_em",
                    "S11_mag_model",
                    "S11_mag_em",
                ])
                for i, f_hz in enumerate(em_freq):
                    writer.writerow([
                        f"{f_hz:.6e}",
                        f"{abs(s21_mi[i]):.8f}",
                        f"{abs(s21_e[i]):.8f}",
                        f"{np.degrees(np.angle(s21_mi[i])):.6f}",
                        f"{np.degrees(np.angle(s21_e[i])):.6f}",
                        f"{abs(s11_mi[i]):.8f}",
                        f"{abs(s11_e[i]):.8f}",
                    ])
            detail_paths.append(csv_path)

            f_ghz = em_freq / 1e9
            fig, axes = plt.subplots(3, 1, figsize=(8, 8), constrained_layout=True, sharex=True)
            axes[0].plot(f_ghz, np.abs(s21_mi), "b-", label="model", linewidth=1.2)
            axes[0].plot(f_ghz, np.abs(s21_e), "r--", label="EM", linewidth=1.0)
            axes[0].set_ylabel("|S21|")
            axes[0].legend(loc="best")
            axes[0].grid(True, alpha=0.3)
            axes[0].set_title(f"{case}: lumped model vs EM S-parameters")

            axes[1].plot(f_ghz, np.degrees(np.angle(s21_mi)), "b-", linewidth=1.2)
            axes[1].plot(f_ghz, np.degrees(np.angle(s21_e)), "r--", linewidth=1.0)
            axes[1].set_ylabel("ang(S21) [deg]")
            axes[1].grid(True, alpha=0.3)

            axes[2].plot(f_ghz, np.abs(s11_mi), "b-", linewidth=1.2)
            axes[2].plot(f_ghz, np.abs(s11_e), "r--", linewidth=1.0)
            axes[2].set_ylabel("|S11|")
            axes[2].set_xlabel("Frequency [GHz]")
            axes[2].grid(True, alpha=0.3)

            plot_path = artifact_dir / f"ind_sp_validate_{case}.png"
            fig.savefig(plot_path, dpi=140)
            plt.close(fig)
            detail_paths.append(plot_path)

            summary_rows.append({
                "case": case,
                "source": res.source,
                "passed": res.passed,
                "fit_s21_mean_rel_pct": res.fit.s21_mag_mean_rel_pct,
                "fit_s21_max_rel_pct": res.fit.s21_mag_max_rel_pct,
                "fit_s21_phase_mean_deg": res.fit.s21_phase_mean_abs_deg,
                "fit_s21_phase_max_deg": res.fit.s21_phase_max_abs_deg,
                "fit_s11_mean_rel_pct": res.fit.s11_mag_mean_rel_pct,
                "extrap_s21_mean_rel_pct": (
                    res.extrap.s21_mag_mean_rel_pct if res.extrap else ""
                ),
                "extrap_s21_max_rel_pct": (
                    res.extrap.s21_mag_max_rel_pct if res.extrap else ""
                ),
                "extrap_s21_phase_mean_deg": (
                    res.extrap.s21_phase_mean_abs_deg if res.extrap else ""
                ),
                "failures": ";".join(res.failures),
            })

    with summary_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=summary_fields)
        writer.writeheader()
        writer.writerows(summary_rows)
    print(f"wrote {summary_path}")
    return summary_path, detail_paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "out",
    )
    parser.add_argument(
        "--sp-validate",
        action="store_true",
        help="Run ngspice SP validation and write ind_sp_validate_* artifacts",
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
        "L_PI_10GHz_nH",
        "C_port_fF",
        "G_port_mS",
        "R_sub_ohm",
        "R_series_28GHz_ohm",
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

    if args.sp_validate:
        write_sp_validation_artifacts(out_dir, out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
