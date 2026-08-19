#!/usr/bin/env python3
"""Regenerate AC frequency PNGs from saved CSV (no ngspice).

Sim sweeps remain 1 MHz–300 GHz; plot x-axis is limited to 100 MHz–200 GHz.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np

_EXP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ctlelib.metrics import (  # noqa: E402
    NYQUIST_HZ,
    compute_ac_peak_metrics,
    interp_db_at,
)
from ctlelib.plots import plot_ac, plot_chain_ac_perstg  # noqa: E402
from stage_chain import _plot_s11  # noqa: E402
from stage_driver import plot_s11_pad  # noqa: E402
from stage_term import plot_insertion_loss, plot_s11  # noqa: E402

OUT = _EXP / "out"


def _load_ac_diff_csv(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    data = np.loadtxt(path, delimiter=",", skiprows=1)
    if data.ndim == 1:
        data = data.reshape(1, -1)
    freq = data[:, 0]
    h_db = data[:, 1]
    gd_s = data[:, 2] * 1e-12
    return freq, h_db, gd_s


def _load_zin_csv(path: Path) -> tuple[np.ndarray, np.ndarray]:
    data = np.loadtxt(path, delimiter=",", skiprows=1)
    if data.ndim == 1:
        data = data.reshape(1, -1)
    return data[:, 0], data[:, 1]


def _load_perstg_csv(path: Path) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    with path.open(newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        cols = [[] for _ in header]
        for row in reader:
            for i, val in enumerate(row):
                cols[i].append(float(val))
    freq = np.asarray(cols[0])
    stage_db = {}
    for name, col in zip(header[1:], cols[1:]):
        stage_db[name.replace("_dB", "_db")] = np.asarray(col)
    return freq, stage_db


def _replot_standard_ac(csv_path: Path, png_path: Path) -> None:
    freq, h_db, gd_s = _load_ac_diff_csv(csv_path)
    dc_gain_db = float(h_db[0])
    peaking_db = interp_db_at(freq, h_db, NYQUIST_HZ) - dc_gain_db
    peak_gain_db, f_peak_hz, f_3db_hz, f3db_at_fmax = compute_ac_peak_metrics(freq, h_db)
    plot_ac(
        freq,
        h_db,
        gd_s,
        png_path,
        peak_gain_db=peak_gain_db,
        f_peak_hz=f_peak_hz,
        f_3db_hz=f_3db_hz,
        f3db_at_fmax=f3db_at_fmax,
        dc_gain_db=dc_gain_db,
        peaking_db=peaking_db,
    )


def _replot_term_ac(csv_path: Path, png_path: Path) -> None:
    freq, h_db, _gd_s = _load_ac_diff_csv(csv_path)
    il_dc = float(h_db[0])
    il_28 = interp_db_at(freq, h_db, NYQUIST_HZ)
    plot_insertion_loss(freq, h_db, png_path, il_dc=il_dc, il_28=il_28)


def _replot_zin(csv_path: Path, png_path: Path) -> None:
    freq, s11_db = _load_zin_csv(csv_path)
    parent = csv_path.parent.name
    if parent == "driver":
        plot_s11_pad(freq, s11_db, png_path)
    elif parent == "term":
        plot_s11(freq, s11_db, png_path)
    else:
        _plot_s11(freq, s11_db, png_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-root",
        type=Path,
        default=OUT,
        help="Experiment out/ directory (default: circuits/ctle56n/out)",
    )
    args = parser.parse_args()
    root: Path = args.out_root
    n = 0

    for csv_path in sorted(root.rglob("ac_diff*.csv")):
        if csv_path.parent.name == "chain_perstg":
            png_path = csv_path.with_suffix(".png")
            freq, stage_db = _load_perstg_csv(csv_path)
            tag = csv_path.stem.replace("ac_diff_", "")
            plot_chain_ac_perstg(
                freq,
                stage_db,
                png_path,
                title=f"Chain AC — per-stage incremental gain ({tag})",
            )
            n += 1
            continue

        png_path = csv_path.with_suffix(".png")
        if csv_path.parent.name == "term" and csv_path.name == "ac_diff.csv":
            _replot_term_ac(csv_path, png_path)
        else:
            _replot_standard_ac(csv_path, png_path)
        n += 1

    for csv_path in sorted(root.rglob("zin.csv")):
        _replot_zin(csv_path, csv_path.with_suffix(".png"))
        n += 1

    print(f"Replotted {n} AC frequency PNG(s) under {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
