#!/usr/bin/env python3
"""Round-5 VGA analysis: large-signal gain, eye metrics, Ron, fT LUT lookup."""

from __future__ import annotations

import csv
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from char.common.lut import load_lut  # noqa: E402

from ctlelib import pass_out, parse_tran_raw, compute_eye_metrics  # noqa: E402
from ctlelib.stim import SWING_DIFF_V, UI_S  # noqa: E402

from size_vga import VgaParams, size_vga  # noqa: E402
from stage_vga import run_ac_at_vctrl, run_dc_sweep, run_sbr, run_tran  # noqa: E402


@dataclass
class TranMetrics:
    vctrl_v: float
    vid_pp_v: float
    vod_pp_v: float
    ls_gain_db: float
    eye_height_mv: float
    eye_width_ui: float
    eye_width_ps: float
    eye_pp_swing_mv: float
    ac_gain_28g_db: float


def parse_op_chunks(op_path: Path) -> dict[float, dict[str, float]]:
    text = op_path.read_text()
    chunks: dict[float, dict[str, float]] = {}
    parts = re.split(r"=== VCTRL = ([0-9.]+) V ===", text)
    for i in range(1, len(parts), 2):
        vc = float(parts[i])
        body = parts[i + 1]
        d: dict[str, float] = {}
        for line in body.splitlines():
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip().lower()
            try:
                d[key] = float(val.strip())
            except ValueError:
                continue
        chunks[vc] = d
    return chunks


def ron_from_dc(dc: dict[str, float]) -> dict[str, float]:
    def _leg(d_key: str, s_key: str, ids_key: str) -> float:
        vd = dc.get(d_key, float("nan"))
        vs = dc.get(s_key, float("nan"))
        ids = abs(dc.get(ids_key, float("nan")))
        if any(math.isnan(x) for x in (vd, vs, ids)) or ids < 1e-9:
            return float("nan")
        return (vd - vs) / ids

    r_ps1 = _leg("v(xu1.e1)", "v(xu1.tx1)", "@n.xu1.xps1.nsg13_lv_nmos[ids]")
    r_ps2 = _leg("v(xu1.e2)", "v(xu1.tx2)", "@n.xu1.xps2.nsg13_lv_nmos[ids]")
    r_pd1 = _leg("v(xu1.ed1)", "v(xu1.tx1)", "@n.xu1.xpd1.nsg13_lv_nmos[ids]")
    r_pd2 = _leg("v(xu1.ed2)", "v(xu1.tx2)", "@n.xu1.xpd2.nsg13_lv_nmos[ids]")
    r_ps = float(np.nanmean([r_ps1, r_ps2]))
    r_pd = float(np.nanmean([r_pd1, r_pd2]))
    return {
        "ron_signal_ohm": r_ps,
        "ron_dummy_ohm": r_pd,
        "ron_avg_ohm": float(np.nanmean([r_ps, r_pd])),
    }


def extract_tran_metrics(
    time_s: np.ndarray,
    v_outp: np.ndarray,
    v_outn: np.ndarray,
    v_inp: np.ndarray,
    v_inn: np.ndarray,
    ac_gain_28g_db: float,
) -> TranMetrics:
    vid = v_inp - v_inn
    vod = v_outp - v_outn
    from ctlelib.metrics import EYE_SETTLE_UI

    t0 = EYE_SETTLE_UI * UI_S
    settled = time_s >= t0
    vid_pp = float(np.percentile(vid[settled], 99) - np.percentile(vid[settled], 1))
    vod_pp = float(np.percentile(vod[settled], 99) - np.percentile(vod[settled], 1))
    if vid_pp < 1e-6:
        vid_pp = SWING_DIFF_V
    ls_gain_db = 20.0 * math.log10(max(vod_pp / vid_pp, 1e-12))

    eye = compute_eye_metrics(time_s, v_outp, v_outn)

    return TranMetrics(
        vctrl_v=0.0,
        vid_pp_v=vid_pp,
        vod_pp_v=vod_pp,
        ls_gain_db=ls_gain_db,
        eye_height_mv=eye.height_mV,
        eye_width_ui=eye.width_ui,
        eye_width_ps=eye.width_ps,
        eye_pp_swing_mv=eye.pp_swing_mV,
        ac_gain_28g_db=ac_gain_28g_db,
    )


def ft_at_ic(bjt_path: Path, ic_target: float, nx_idx: int = 0) -> tuple[float, float]:
    arrays, _ = load_lut(bjt_path)
    vce_tgt = 1.05
    best_ft = float("nan")
    best_ic = float("nan")
    best_err = float("inf")
    for vi, vce in enumerate(arrays["VCE"]):
        for bi in range(len(arrays["VBE"])):
            ic = float(arrays["IC"][nx_idx, vi, bi])
            if ic <= 0:
                continue
            err = abs(ic - ic_target) + 0.05 * abs(float(vce) - vce_tgt)
            if err < best_err:
                best_err = err
                best_ft = float(arrays["FT"][nx_idx, vi, bi])
                best_ic = ic
    return best_ft, best_ic


def read_tran_csv(path: Path) -> tuple[np.ndarray, ...]:
    rows = []
    with path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append([
                float(row["time_s"]),
                float(row["v_outp"]),
                float(row["v_outn"]),
                float(row["v_inp"]),
                float(row["v_inn"]),
            ])
    data = np.asarray(rows)
    return data[:, 0], data[:, 1], data[:, 2], data[:, 3], data[:, 4]


def analyze_pass(
    pass_name: str,
    dut_rel: str,
    params: VgaParams,
    *,
    run_tran_all: bool = False,
) -> tuple[list[TranMetrics], list[dict]]:
    pout = pass_out(pass_name)
    tran_rows: list[TranMetrics] = []
    ron_rows: list[dict] = []

    run_dc_sweep(pass_name, dut_rel, params)
    op_chunks = parse_op_chunks(pout / "op.txt")

    for vc in params.vctrl_v:
        tag = f"vctrl_{vc:.2f}".replace(".", "p")
        ac_m, _, _, _ = run_ac_at_vctrl(pass_name, dut_rel, params, vc, tag)

        if run_tran_all:
            run_tran(pass_name, dut_rel, params, vc, tag)
            run_sbr(pass_name, dut_rel, params, vc, tag)

        csv_path = pout / f"tran_{tag}.csv"
        if not csv_path.is_file():
            raise FileNotFoundError(f"Missing transient CSV: {csv_path}")
        time_s, v_outp, v_outn, v_inp, v_inn = read_tran_csv(csv_path)
        tm = extract_tran_metrics(
            time_s, v_outp, v_outn, v_inp, v_inn, ac_m.ac_gain_28g_db
        )
        tm.vctrl_v = vc
        tran_rows.append(tm)

        dc = op_chunks.get(vc, {})
        if dc:
            ron = ron_from_dc(dc)
            ron["vctrl_v"] = vc
            ron["ic_signal_a"] = dc.get("@q.xu1.xq1.qnpn13g2[ic]", float("nan"))
            ron["ac_gain_28g_db"] = ac_m.ac_gain_28g_db
            ron_rows.append(ron)

    write_tables(pass_name, tran_rows, ron_rows)
    return tran_rows, ron_rows


def write_tables(
    pass_name: str,
    tran_rows: list[TranMetrics],
    ron_rows: list[dict],
) -> None:
    pout = pass_out(pass_name)
    with (pout / "gain_small_vs_large.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "VCTRL_V", "ac_gain_28G_dB", "ls_gain_dB", "delta_dB",
            "vid_pp_mV", "vod_pp_mV", "eye_height_mV", "eye_width_UI",
            "eye_width_ps", "eye_pp_swing_mV",
        ])
        for r in tran_rows:
            w.writerow([
                f"{r.vctrl_v:.4f}",
                f"{r.ac_gain_28g_db:.3f}",
                f"{r.ls_gain_db:.3f}",
                f"{r.ls_gain_db - r.ac_gain_28g_db:.3f}",
                f"{r.vid_pp_v * 1e3:.2f}",
                f"{r.vod_pp_v * 1e3:.2f}",
                f"{r.eye_height_mv:.2f}",
                f"{r.eye_width_ui:.4f}",
                f"{r.eye_width_ps:.2f}",
                f"{r.eye_pp_swing_mv:.2f}",
            ])

    with (pout / "ron_vs_vctrl.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "VCTRL_V", "Ic_signal_A", "ron_signal_ohm", "ron_dummy_ohm",
            "ron_avg_ohm", "ac_gain_28G_dB",
        ])
        for r in ron_rows:
            w.writerow([
                f"{r['vctrl_v']:.4f}",
                f"{r['ic_signal_a']:.6g}",
                f"{r['ron_signal_ohm']:.2f}",
                f"{r['ron_dummy_ohm']:.2f}",
                f"{r['ron_avg_ohm']:.2f}",
                f"{r['ac_gain_28g_db']:.3f}",
            ])


def print_ft_table(ic_min: float, ic_max: float) -> None:
    bjt = _REPO / "char/bjt/out/sg13_npn13G2.npz"
    ft_lo, ic_lo = ft_at_ic(bjt, ic_min)
    ft_hi, ic_hi = ft_at_ic(bjt, ic_max)
    print(f"  fT @ Ic={ic_lo*1e6:.0f} µA (target {ic_min*1e6:.0f} µA): {ft_lo/1e9:.2f} GHz")
    print(f"  fT @ Ic={ic_hi*1e3:.2f} mA (target {ic_max*1e3:.2f} mA): {ft_hi/1e9:.1f} GHz")
    print(f"  fT ratio (max/min): {ft_hi/ft_lo:.1f}×")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--pass-name", default="vga_ideal")
    parser.add_argument("--dut", default="vga_ideal.cir")
    parser.add_argument("--run-tran-all", action="store_true")
    parser.add_argument("--rs", type=float, default=None)
    args = parser.parse_args()

    params = size_vga(rs_ohm=args.rs)
    tran, ron = analyze_pass(
        args.pass_name, args.dut, params, run_tran_all=args.run_tran_all
    )
    ic_min = ron[0]["ic_signal_a"]
    ic_max = ron[-1]["ic_signal_a"]
    print_ft_table(ic_min, ic_max)
