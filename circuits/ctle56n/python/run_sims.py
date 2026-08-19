#!/usr/bin/env python3
"""Run ngspice DC/AC for 56G CML CTLE; parse results and plot.

Assumes the caller has sourced ~/.local/share/ihp-eda/env.sh (PDK_ROOT, ngspice).
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[3]
_EXP = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from char.common.lut import parse_wrdata  # noqa: E402

# Import sizer from same package directory
sys.path.insert(0, str(Path(__file__).resolve().parent))
from size_ctle import CtleParams, print_summary, size_ctle, write_params_inc  # noqa: E402

NYQUIST_HZ = 28e9
DC_GAIN_MIN_DB = -6.0
DC_GAIN_MAX_DB = 0.0
PEAK_MIN_DB = 3.0
PEAK_MAX_DB = 10.0
CMRR_MIN_DB = 6.0
PSRR_MIN_DB = 20.0
PSRR_MAX_DB = 120.0
AC_FMAX_HZ = 300e9

# 56G NRZ transient
BIT_RATE_HZ = 56e9
UI_S = 1.0 / BIT_RATE_HZ
SWING_DIFF_V = 0.1  # 100 mVpp,diff
SWING_SE_V = SWING_DIFF_V / 2.0  # 50 mVpp per SE leg
EDGE_S = 4.5e-12  # ~0.25 UI rise/fall
PRBS7_MIN_BITS = 254  # ≥2 full PRBS7 sequences (127 bits each)
EYE_SETTLE_UI = 16


@dataclass
class SimMetrics:
    pass_name: str
    dc_gain_db: float
    peaking_db: float
    cmrr_db: float
    psrr_db: float
    vce_v: float
    vds_tail_v: float
    vgs_tail_v: float
    ic_a: float = float("nan")
    id_tail_a: float = float("nan")


def pdk_models() -> Path:
    pdk = os.environ.get("PDK_ROOT")
    if not pdk:
        raise SystemExit("PDK_ROOT not set — source ~/.local/share/ihp-eda/env.sh")
    return Path(pdk) / "ihp-sg13g2" / "libs.tech" / "ngspice" / "models"


def ngspice_exe() -> str:
    exe = shutil.which("ngspice")
    if not exe:
        raise SystemExit("ngspice not found on PATH")
    return exe


def apply_params(text: str, spice_dir: Path, extra: dict[str, str] | None = None) -> str:
    """Replace {PARAM} tokens from spice/params.inc and optional extras."""
    inc = spice_dir / "params.inc"
    params: dict[str, str] = {}
    if inc.is_file():
        for line in inc.read_text().splitlines():
            m = re.match(r"\.param\s+(\w+)=([\S]+)", line, re.I)
            if m:
                params[m.group(1)] = m.group(2)
    if extra:
        params.update(extra)
    for key, val in params.items():
        text = text.replace(f"{{{key}}}", val)
    return text


def prepare_tb(
    template: Path,
    dut_cir: Path,
    work: Path,
    models: Path,
    spice_dir: Path,
    extra_params: dict[str, str] | None = None,
) -> Path:
    text = apply_params(template.read_text(), spice_dir, extra_params)
    text = text.replace("{PDK_MODELS}", str(models))
    dut_local = work / dut_cir.name
    dut_text = apply_params(dut_cir.read_text(), spice_dir, extra_params).replace(
        "{PDK_MODELS}", str(models)
    )
    dut_local.write_text(dut_text)
    cs_src = spice_dir / "cs.inc"
    if cs_src.is_file():
        shutil.copy(cs_src, work / "cs.inc")
    text = text.replace("{DUT_CIR}", str(dut_local.resolve()))
    out = work / template.name
    out.write_text(text)
    return out


def run_ngspice(cir: Path, work: Path, log_name: str = "ngspice.log") -> Path:
    log = work / log_name
    exe = ngspice_exe()
    with log.open("w") as lf:
        proc = subprocess.run(
            [exe, "-b", "-o", str(log), str(cir)],
            cwd=work,
            stdout=lf,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if proc.returncode != 0:
        tail = log.read_text()[-4000:]
        raise RuntimeError(f"ngspice failed on {cir.name}:\n{tail}")
    return log


def parse_dc_log(log: Path) -> dict[str, float]:
    text = log.read_text()
    vals: dict[str, float] = {}
    # ngspice print output: name = value (names may include parens, e.g. v(outp))
    for m in re.finditer(
        r"(@?[\w\.\[\]\(\)]+)\s*=\s*([-+eE0-9.]+)",
        text,
    ):
        key, val = m.group(1), float(m.group(2))
        vals[key] = val
    return vals


def complex_from_vm_vp(vm: np.ndarray, vp: np.ndarray) -> np.ndarray:
    # ngspice wrdata vm/vp phases are in radians (not degrees like print vp())
    phase = vp
    return vm * (np.cos(phase) + 1j * np.sin(phase))


def parse_psrr_raw(raw: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """wrdata frequency vm(outp) vp(outp) vm(outn) vp(outn) vm(vdd) vp(vdd)."""
    rows = []
    with raw.open() as f:
        for line in f:
            parts = line.split()
            if parts:
                rows.append([float(x) for x in parts])
    arr = np.asarray(rows)
    if arr.shape[1] >= 9:
        freq = arr[:, 0]
        voutp = complex_from_vm_vp(arr[:, 3], arr[:, 4])
        voutn = complex_from_vm_vp(arr[:, 5], arr[:, 6])
        vvdd = complex_from_vm_vp(arr[:, 7], arr[:, 8])
        return freq, voutp, voutn, vvdd
    raise RuntimeError(f"{raw}: unexpected PSRR wrdata width {arr.shape[1]}")


def parse_ac_raw(raw: Path) -> tuple[np.ndarray, ...]:
    rows = []
    with raw.open() as f:
        for line in f:
            parts = line.split()
            if parts:
                rows.append([float(x) for x in parts])
    arr = np.asarray(rows)
    # wr_singlescale: freq, freq, 0, vm/vp pairs for outp, outn, inp, inn
    if arr.shape[1] >= 11:
        vm_outp, vp_outp = arr[:, 3], arr[:, 4]
        vm_outn, vp_outn = arr[:, 5], arr[:, 6]
        vm_inp, vp_inp = arr[:, 7], arr[:, 8]
        vm_inn, vp_inn = arr[:, 9], arr[:, 10]
        freq = arr[:, 0]
    else:
        names = [
            "vm_outp", "vp_outp", "vm_outn", "vp_outn",
            "vm_inp", "vp_inp", "vm_inn", "vp_inn",
        ]
        data = parse_wrdata(raw, names)
        freq = None
        rows_arr = np.asarray(rows)
        freq = rows_arr[:, 0]
        vm_outp = data["vm_outp"]
        vp_outp = data["vp_outp"]
        vm_outn = data["vm_outn"]
        vp_outn = data["vp_outn"]
        vm_inp = data["vm_inp"]
        vp_inp = data["vp_inp"]
        vm_inn = data["vm_inn"]
        vp_inn = data["vp_inn"]

    voutp = complex_from_vm_vp(vm_outp, vp_outp)
    voutn = complex_from_vm_vp(vm_outn, vp_outn)
    vin_p = complex_from_vm_vp(vm_inp, vp_inp)
    vin_n = complex_from_vm_vp(vm_inn, vp_inn)
    return freq, voutp, voutn, vin_p, vin_n


def interp_db_at(freq: np.ndarray, h_db: np.ndarray, f_target: float) -> float:
    if f_target <= freq[0]:
        return float(h_db[0])
    if f_target >= freq[-1]:
        return float(h_db[-1])
    return float(np.interp(f_target, freq, h_db))


def prbs7_bits(n_bits: int, seed: int = 0x7F) -> list[int]:
    """PRBS7 (x^7 + x^6 + 1) LFSR; seed must be non-zero."""
    state = seed & 0x7F
    if state == 0:
        state = 0x7F
    bits: list[int] = []
    for _ in range(n_bits):
        bits.append(state & 1)
        fb = ((state >> 6) ^ (state >> 5)) & 1
        state = ((state >> 1) | (fb << 6)) & 0x7F
    return bits


def _pwl_points(bits: list[int], vbase: float, se_hi: float, se_lo: float) -> tuple[list[float], list[float]]:
    """Build monotonic PWL (time, value) for one SE leg given NRZ bits."""
    def val(bit: int) -> float:
        return se_hi if bit else se_lo

    times = [0.0]
    vals = [val(bits[0])]
    for k in range(1, len(bits)):
        t_edge = k * UI_S
        if bits[k] != bits[k - 1]:
            if times[-1] < t_edge - 1e-18:
                times.append(t_edge)
                vals.append(val(bits[k - 1]))
            times.append(t_edge + EDGE_S)
            vals.append(val(bits[k]))
    t_end = len(bits) * UI_S
    if times[-1] < t_end - 1e-18:
        times.append(t_end)
        vals.append(val(bits[-1]))
    return times, vals


def write_prbs_stim(path: Path, vbase: float, n_bits: int = PRBS7_MIN_BITS) -> float:
    """Write prbs_stim.inc with complementary NRZ PWL sources; return tmax (s)."""
    bits = prbs7_bits(n_bits)
    half = SWING_SE_V / 2.0
    t_inp, v_inp = _pwl_points(bits, vbase, vbase + half, vbase - half)
    t_inn, v_inn = _pwl_points(bits, vbase, vbase - half, vbase + half)

    def fmt_pwl(name: str, node: str, times: list[float], vals: list[float]) -> list[str]:
        lines = [f"{name} {node} 0 PWL("]
        row: list[str] = []
        for i, (t, v) in enumerate(zip(times, vals)):
            row.append(f"{t:.6e} {v:.9g}")
            if len(row) == 6 or i == len(times) - 1:
                lines.append("+ " + " ".join(row))
                row = []
        lines.append("+)")
        return lines

    lines = [
        "* PRBS7 NRZ stimulus — generated by run_sims.py",
        f"* {n_bits} bits @ {BIT_RATE_HZ/1e9:.0f} Gb/s, {SWING_DIFF_V*1e3:.0f} mVpp,diff",
        *fmt_pwl("Vp", "inp", t_inp, v_inp),
        *fmt_pwl("Vn", "inn", t_inn, v_inn),
    ]
    path.write_text("\n".join(lines) + "\n")
    return len(bits) * UI_S


def parse_tran_raw(raw: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """wrdata: time v(outp) v(outn) v(inp) v(inn)."""
    rows = []
    with raw.open() as f:
        for line in f:
            parts = line.split()
            if parts:
                rows.append([float(x) for x in parts])
    arr = np.asarray(rows)
    # ngspice wrdata (real): often "time, time, v(outp), v(outn), v(inp), v(inn)"
    # even with wr_singlescale. Without singlescale it interleaves time per vector.
    if arr.shape[1] >= 8:
        return arr[:, 0], arr[:, 1], arr[:, 3], arr[:, 5], arr[:, 7]
    if arr.shape[1] >= 6:
        return arr[:, 0], arr[:, 2], arr[:, 3], arr[:, 4], arr[:, 5]
    if arr.shape[1] >= 5:
        return arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4]
    raise RuntimeError(f"{raw}: expected ≥5 columns, got {arr.shape[1]}")


def group_delay_s(freq: np.ndarray, h: np.ndarray) -> np.ndarray:
    phase = np.unwrap(np.angle(h))
    omega = 2.0 * np.pi * freq
    d_phase = np.gradient(phase, omega)
    return -d_phase


def run_pass(pass_name: str, dut_rel: str, spice_dir: Path, out_dir: Path) -> SimMetrics:
    models = pdk_models()
    dut_cir = spice_dir / dut_rel
    work = out_dir / f"work_{pass_name}"
    work.mkdir(parents=True, exist_ok=True)

    # Copy params.inc and cs.inc into work
    for inc in ("params.inc", "cs.inc"):
        src = spice_dir / inc
        if src.is_file():
            shutil.copy(src, work / inc)

    # DC
    tb_dc = prepare_tb(spice_dir / "tb_dc.cir", dut_cir, work, models, spice_dir)
    dc_log = run_ngspice(tb_dc, work, "dc.log")
    dc_vals = parse_dc_log(dc_log)

    # AC diff
    tb_diff = prepare_tb(spice_dir / "tb_ac_diff.cir", dut_cir, work, models, spice_dir)
    run_ngspice(tb_diff, work, "ac_diff.log")
    freq, voutp, voutn, vin_p, vin_n = parse_ac_raw(work / "ac_diff.raw")
    vod = voutp - voutn
    vid = vin_p - vin_n
    h_diff = np.where(np.abs(vid) > 1e-30, vod / vid, 0.0)
    h_db = 20.0 * np.log10(np.abs(h_diff))
    dc_gain_db = float(h_db[0])
    peaking_db = interp_db_at(freq, h_db, NYQUIST_HZ) - dc_gain_db

    # AC CM (CMRR = Adm/Acm)
    tb_cm = prepare_tb(spice_dir / "tb_ac_cm.cir", dut_cir, work, models, spice_dir)
    run_ngspice(tb_cm, work, "ac_cm.log")
    freq_cm, voutp_cm, voutn_cm, vin_p_cm, vin_n_cm = parse_ac_raw(work / "ac_cm.raw")
    voc_cm = (voutp_cm + voutn_cm) / 2.0
    vic_cm = (vin_p_cm + vin_n_cm) / 2.0
    h_cm = np.where(np.abs(vic_cm) > 1e-30, voc_cm / vic_cm, 0.0)
    acm_db = float(20.0 * np.log10(max(np.abs(h_cm[0]), 1e-30)))
    cmrr_db = dc_gain_db - acm_db

    # AC PSRR — VDD noise to differential output
    tb_psrr = prepare_tb(spice_dir / "tb_ac_psrr.cir", dut_cir, work, models, spice_dir)
    run_ngspice(tb_psrr, work, "ac_psrr.log")
    freq_p, voutp_p, voutn_p, vvdd = parse_psrr_raw(work / "ac_psrr.raw")
    vod_p = voutp_p - voutn_p
    vod_abs = np.abs(vod_p)
    vod_floor = 10.0 ** (-PSRR_MAX_DB / 20.0)
    vod_safe = np.maximum(vod_abs, vod_floor)
    h_psrr = np.abs(vvdd) / vod_safe
    psrr_db_curve = 20.0 * np.log10(np.maximum(h_psrr, 1e-30))
    psrr_db_curve = np.minimum(psrr_db_curve, PSRR_MAX_DB)
    psrr_db = float(psrr_db_curve[0])

    v_em = dc_vals.get("v(xu1.em)", 0.28)
    v_mgate = dc_vals.get("v(xu1.mgate)", 0.85)
    v_c1 = dc_vals.get("v(outp)", 0.0)
    v_e1 = dc_vals.get("v(xu1.e1)", 0.0)
    vce = v_c1 - v_e1
    ic_a = dc_vals.get("@q.xu1.xq1.qnpn13g2[ic]", float("nan"))
    id_tail = dc_vals.get("@n.xu1.xtail.nsg13_lv_nmos[ids]", float("nan"))

    op_path = out_dir / f"op_{pass_name}.txt"
    op_path.write_text(dc_log.read_text())

    if pass_name == "ideal":
        _plot_ac(freq, h_db, group_delay_s(freq, h_diff), out_dir / "ac_diff.png")
        cmrr_curve = 20.0 * np.log10(np.maximum(np.abs(h_diff), 1e-30)) - (
            20.0 * np.log10(np.maximum(np.abs(h_cm), 1e-30))
        )
        _plot_cmrr(freq_cm, cmrr_curve, out_dir / "cmrr.png")
        _plot_psrr(freq_p, psrr_db_curve, out_dir / "psrr.png")

    return SimMetrics(
        pass_name=pass_name,
        dc_gain_db=dc_gain_db,
        peaking_db=peaking_db,
        cmrr_db=cmrr_db,
        psrr_db=psrr_db,
        vce_v=vce,
        vds_tail_v=v_em,
        vgs_tail_v=v_mgate,
        ic_a=ic_a,
        id_tail_a=id_tail,
    )


def run_tran(
    pass_name: str,
    dut_rel: str,
    spice_dir: Path,
    out_dir: Path,
    vbase: float,
) -> None:
    """Run 56G NRZ PRBS transient and plot waveforms + eye diagrams."""
    models = pdk_models()
    dut_cir = spice_dir / dut_rel
    work = out_dir / f"work_{pass_name}"
    work.mkdir(parents=True, exist_ok=True)

    for inc in ("params.inc", "cs.inc"):
        src = spice_dir / inc
        if src.is_file():
            shutil.copy(src, work / inc)

    tmax = write_prbs_stim(work / "prbs_stim.inc", vbase)
    tb_tran = prepare_tb(
        spice_dir / "tb_tran.cir",
        dut_cir,
        work,
        models,
        spice_dir,
        extra_params={"TMAX": f"{tmax:.6e}"},
    )
    run_ngspice(tb_tran, work, "tran.log")
    time_s, v_outp, v_outn, v_inp, v_inn = parse_tran_raw(work / "tran.raw")

    if pass_name == "ideal":
        _plot_tran_se(time_s, v_outp, v_outn, v_inp, v_inn, out_dir / "tran_se.png")
        _plot_tran_diff(time_s, v_outp, v_outn, v_inp, v_inn, out_dir / "tran_diff.png")
        _plot_eye_se(time_s, v_outp, v_outn, out_dir / "eye_se.png")
        _plot_eye_diff(time_s, v_outp, v_outn, out_dir / "eye_diff.png")


def _plot_tran_se(
    time_s: np.ndarray,
    v_outp: np.ndarray,
    v_outn: np.ndarray,
    v_inp: np.ndarray,
    v_inn: np.ndarray,
    path: Path,
) -> None:
    import matplotlib.pyplot as plt

    t_ns = time_s * 1e9
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(t_ns, v_outp, "b-", lw=1.0, label="v(outp)")
    ax.plot(t_ns, v_outn, "r-", lw=1.0, label="v(outn)")
    ax.plot(t_ns, v_inp, color="b", alpha=0.35, lw=0.8, label="v(inp)")
    ax.plot(t_ns, v_inn, color="r", alpha=0.35, lw=0.8, label="v(inn)")
    stacked = np.concatenate([v_outp, v_outn, v_inp, v_inn])
    lo, hi = float(np.min(stacked)), float(np.max(stacked))
    pad = max(0.02, 0.15 * (hi - lo))
    ax.set_ylim(lo - pad, hi + pad)
    ax.set_xlabel("Time (ns)")
    ax.set_ylabel("Voltage (V)")
    ax.set_title("56G NRZ PRBS — single-ended (100 mVpp,diff stimulus)")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _plot_tran_diff(
    time_s: np.ndarray,
    v_outp: np.ndarray,
    v_outn: np.ndarray,
    v_inp: np.ndarray,
    v_inn: np.ndarray,
    path: Path,
) -> None:
    import matplotlib.pyplot as plt

    t_ns = time_s * 1e9
    vod = (v_outp - v_outn) * 1e3
    vid = (v_inp - v_inn) * 1e3
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(t_ns, vod, "b-", lw=1.0, label="vod = outp−outn")
    ax.plot(t_ns, vid, color="orange", alpha=0.7, lw=0.8, label="vid = inp−inn")
    stacked = np.concatenate([vod, vid])
    lo, hi = float(np.min(stacked)), float(np.max(stacked))
    pad = max(10.0, 0.15 * (hi - lo))
    ax.set_ylim(lo - pad, hi + pad)
    ax.set_xlabel("Time (ns)")
    ax.set_ylabel("Differential (mV)")
    ax.set_title("56G NRZ PRBS — differential waveforms")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _eye_traces(
    time_s: np.ndarray,
    signal: np.ndarray,
    ui_s: float,
    settle_ui: int,
) -> tuple[np.ndarray, list[np.ndarray]]:
    """Fold signal into 2-UI eye after skipping settling UIs."""
    t0 = settle_ui * ui_s
    mask = time_s >= t0
    t_rel = time_s[mask] - t0
    sig = signal[mask]
    period = 2.0 * ui_s
    t_mod = np.mod(t_rel, period)
    # Split into individual UI-pair segments for overlay
    n_ui_pairs = int((t_rel[-1] - t_rel[0]) / period)
    traces: list[np.ndarray] = []
    t_axis = None
    n_pts = 200
    t_fold = np.linspace(0, period, n_pts)
    for k in range(n_ui_pairs):
        t_start = k * period
        t_end = (k + 1) * period
        seg_mask = (t_rel >= t_start) & (t_rel < t_end)
        if np.sum(seg_mask) < 4:
            continue
        t_seg = t_rel[seg_mask] - t_start
        s_seg = sig[seg_mask]
        traces.append(np.interp(t_fold, t_seg, s_seg))
    return t_fold * 1e12, traces  # ps


def _plot_eye_se(
    time_s: np.ndarray,
    v_outp: np.ndarray,
    v_outn: np.ndarray,
    path: Path,
) -> None:
    import matplotlib.pyplot as plt

    t_ps, traces_p = _eye_traces(time_s, v_outp, UI_S, EYE_SETTLE_UI)
    _, traces_n = _eye_traces(time_s, v_outn, UI_S, EYE_SETTLE_UI)
    fig, ax = plt.subplots(figsize=(6, 5))
    for tr in traces_p:
        ax.plot(t_ps, tr * 1e3, color="b", alpha=0.08, lw=0.6)
    for tr in traces_n:
        ax.plot(t_ps, tr * 1e3, color="r", alpha=0.08, lw=0.6)
    ui_ps = UI_S * 1e12
    ax.axvline(ui_ps, color="k", ls="--", alpha=0.5, label="1 UI")
    stacked = np.concatenate(traces_p + traces_n) * 1e3 if traces_p or traces_n else np.array([0.0])
    lo, hi = float(np.min(stacked)), float(np.max(stacked))
    pad = max(5.0, 0.15 * (hi - lo))
    ax.set_ylim(lo - pad, hi + pad)
    ax.set_xlabel("Time (ps)")
    ax.set_ylabel("Voltage (mV)")
    ax.set_title("2-UI eye — single-ended (outp blue, outn red)")
    ax.set_xlim(0, 2 * ui_ps)
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _plot_eye_diff(
    time_s: np.ndarray,
    v_outp: np.ndarray,
    v_outn: np.ndarray,
    path: Path,
) -> None:
    import matplotlib.pyplot as plt

    vod = v_outp - v_outn
    t_ps, traces = _eye_traces(time_s, vod, UI_S, EYE_SETTLE_UI)
    fig, ax = plt.subplots(figsize=(6, 5))
    for tr in traces:
        ax.plot(t_ps, tr * 1e3, color="b", alpha=0.08, lw=0.6)
    ui_ps = UI_S * 1e12
    ax.axvline(ui_ps, color="k", ls="--", alpha=0.5, label="1 UI")
    stacked = np.concatenate(traces) * 1e3 if traces else np.array([0.0])
    lo, hi = float(np.min(stacked)), float(np.max(stacked))
    pad = max(5.0, 0.15 * (hi - lo))
    ax.set_ylim(lo - pad, hi + pad)
    ax.set_xlabel("Time (ps)")
    ax.set_ylabel("vod (mV)")
    ax.set_title("2-UI eye — differential (vod)")
    ax.set_xlim(0, 2 * ui_ps)
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)

def _plot_ac(
    freq: np.ndarray,
    h_db: np.ndarray,
    gd_s: np.ndarray,
    path: Path,
) -> None:
    import matplotlib.pyplot as plt

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 6), sharex=True)
    ax1.semilogx(freq, h_db, "b-", lw=1.5)
    ax1.axvline(NYQUIST_HZ, color="r", ls="--", alpha=0.7, label="28 GHz")
    ax1.set_ylabel("Gain (dB)")
    ax1.set_title("Differential AC — |vod/vid|")
    ax1.set_xlim(freq[0], AC_FMAX_HZ)
    ax1.grid(True, which="both", alpha=0.3)
    ax1.legend()

    gd_ps = gd_s * 1e12
    ax2.semilogx(freq, gd_ps, "g-", lw=1.5)
    ax2.axvline(NYQUIST_HZ, color="r", ls="--", alpha=0.7)
    ax2.set_ylabel("Group delay (ps)")
    ax2.set_xlabel("Frequency (Hz)")
    ax2.grid(True, which="both", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _plot_cmrr(freq: np.ndarray, cmrr_db: np.ndarray, path: Path) -> None:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.semilogx(freq, cmrr_db, "m-", lw=1.5)
    ax.axhline(CMRR_MIN_DB, color="k", ls=":", label="6 dB target")
    ax.set_ylabel("CMRR (dB)")
    ax.set_xlabel("Frequency (Hz)")
    ax.set_title("CMRR = Adm − Acm (dB)")
    ax.set_xlim(freq[0], AC_FMAX_HZ)
    ax.grid(True, which="both", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _plot_psrr(freq: np.ndarray, psrr_db: np.ndarray, path: Path) -> None:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.semilogx(freq, psrr_db, "c-", lw=1.5)
    ax.axhline(PSRR_MIN_DB, color="k", ls=":", label="20 dB target")
    ax.axvline(NYQUIST_HZ, color="r", ls="--", alpha=0.7, label="28 GHz")
    ax.set_ylabel("PSRR (dB)")
    ax.set_xlabel("Frequency (Hz)")
    ax.set_title("PSRR = |vdd / vod| (VDD noise → differential out)")
    ax.set_xlim(freq[0], AC_FMAX_HZ)
    ax.grid(True, which="both", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def targets_ok(m: SimMetrics) -> bool:
    return (
        DC_GAIN_MIN_DB <= m.dc_gain_db <= DC_GAIN_MAX_DB
        and PEAK_MIN_DB <= m.peaking_db <= PEAK_MAX_DB
        and m.cmrr_db >= CMRR_MIN_DB
        and m.psrr_db >= PSRR_MIN_DB
    )


def iterate_sizing(spice_dir: Path, max_iter: int = 8) -> CtleParams:
    scales = [1.0, 0.95, 1.05, 0.9, 1.1, 0.85, 1.15, 1.2]
    peaking_opts = [6.0, 6.5, 7.0, 5.5, 7.5, 8.0, 5.0, 4.5]
    best_params: CtleParams | None = None
    best_score = float("inf")
    best_metrics: SimMetrics | None = None

    out_dir = _EXP / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    for i in range(max_iter):
        scale = scales[i % len(scales)]
        peak = peaking_opts[i % len(peaking_opts)]
        params = size_ctle(scale=scale, peaking_db=peak)
        write_params_inc(params, spice_dir / "params.inc")
        print_summary(params)

        try:
            metrics = run_pass("ideal", "ctle_ideal.cir", spice_dir, out_dir)
        except RuntimeError as exc:
            print(f"  sim error at scale={scale}: {exc}")
            continue

        print(
            f"  iter {i}: DC={metrics.dc_gain_db:.2f} dB "
            f"peak@28G={metrics.peaking_db:.2f} dB "
            f"CMRR={metrics.cmrr_db:.2f} dB PSRR={metrics.psrr_db:.2f} dB "
            f"VCE={metrics.vce_v:.3f} V"
        )

        score = 0.0
        if metrics.dc_gain_db < DC_GAIN_MIN_DB:
            score += (DC_GAIN_MIN_DB - metrics.dc_gain_db) ** 2
        elif metrics.dc_gain_db > DC_GAIN_MAX_DB:
            score += (metrics.dc_gain_db - DC_GAIN_MAX_DB) ** 2
        if metrics.peaking_db < PEAK_MIN_DB:
            score += (PEAK_MIN_DB - metrics.peaking_db) ** 2
        elif metrics.peaking_db > PEAK_MAX_DB:
            score += (metrics.peaking_db - PEAK_MAX_DB) ** 2
        if metrics.cmrr_db < CMRR_MIN_DB:
            score += (CMRR_MIN_DB - metrics.cmrr_db) ** 2
        if metrics.psrr_db < PSRR_MIN_DB:
            score += (PSRR_MIN_DB - metrics.psrr_db) ** 2

        if score < best_score:
            best_score = score
            best_params = params
            best_metrics = metrics

        if targets_ok(metrics):
            print("  targets met (ideal pass)")
            return params

    if best_params is None:
        raise RuntimeError("All simulation iterations failed")
    print(f"  using best iteration (score={best_score:.3f})")
    return best_params


def write_summary(
    path: Path,
    params: CtleParams,
    ideal: SimMetrics,
    pdk: SimMetrics | None,
) -> None:
    rows = [
        ["parameter", "value"],
        ["Nx", params.nx],
        ["VBE_lut", f"{params.vbe:.6g}"],
        ["VBASE", f"{params.vbase:.6g}"],
        ["Ic_A", f"{params.ic:.6g}"],
        ["ft_Hz", f"{params.ft_hz:.6g}"],
        ["gm_S", f"{params.gm:.6g}"],
        ["Cin_F", f"{params.cin_f:.6g}"],
        ["VDD", f"{params.vdd:.6g}"],
        ["RD_ohm", f"{params.rd_ohm:.6g}"],
        ["Rs_ohm", f"{params.rs_ohm:.6g}"],
        ["Cs_F", f"{params.cs_f:.6g}"],
        ["L_H", f"{params.l_h:.6g}"],
        ["CL_F", f"{params.cl_f:.6g}"],
        ["I_tail_A", f"{params.itail_a:.6g}"],
        ["MOS_W_um", f"{params.mos_w_um:.6g}"],
        ["MOS_L_um", f"{params.mos_l_um:.6g}"],
        ["MOS_M", params.mos_m],
        ["ideal_dc_gain_dB", f"{ideal.dc_gain_db:.3f}"],
        ["ideal_peaking_28G_dB", f"{ideal.peaking_db:.3f}"],
        ["ideal_CMRR_dB", f"{ideal.cmrr_db:.3f}"],
        ["ideal_PSRR_dB", f"{ideal.psrr_db:.3f}"],
        ["ideal_VCE_V", f"{ideal.vce_v:.4f}"],
        ["ideal_VDS_tail_V", f"{ideal.vds_tail_v:.4f}"],
        ["ideal_VGS_tail_V", f"{ideal.vgs_tail_v:.4f}"],
        ["ideal_Ic_A", f"{ideal.ic_a:.6g}"],
        ["ideal_Id_tail_A", f"{ideal.id_tail_a:.6g}"],
    ]
    if pdk:
        rows += [
            ["pdk_dc_gain_dB", f"{pdk.dc_gain_db:.3f}"],
            ["pdk_peaking_28G_dB", f"{pdk.peaking_db:.3f}"],
            ["pdk_CMRR_dB", f"{pdk.cmrr_db:.3f}"],
            ["pdk_PSRR_dB", f"{pdk.psrr_db:.3f}"],
            ["pdk_VCE_V", f"{pdk.vce_v:.4f}"],
            ["pdk_targets_ok", targets_ok(pdk)],
        ]
    with path.open("w", newline="") as f:
        csv.writer(f).writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-iterate", action="store_true", help="Skip iteration loop")
    parser.add_argument("--no-pdk", action="store_true", help="Skip PDK passive pass")
    parser.add_argument("--no-tran", action="store_true", help="Skip 56G NRZ transient")
    parser.add_argument("--force-size", action="store_true", help="Re-run sizer")
    parser.add_argument("--scale", type=float, default=1.05)
    parser.add_argument("--peaking-db", type=float, default=7.5)
    args = parser.parse_args()

    spice_dir = _EXP / "spice"
    out_dir = _EXP / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.no_iterate:
        if args.force_size or not (spice_dir / "params.inc").is_file():
            params = size_ctle(scale=args.scale, peaking_db=args.peaking_db)
            write_params_inc(params, spice_dir / "params.inc")
            print_summary(params)
        else:
            params = size_ctle(scale=args.scale, peaking_db=args.peaking_db)
            write_params_inc(params, spice_dir / "params.inc")
    else:
        params = iterate_sizing(spice_dir)
        write_params_inc(params, spice_dir / "params.inc")

    ideal = run_pass("ideal", "ctle_ideal.cir", spice_dir, out_dir)
    print(
        f"Ideal: DC={ideal.dc_gain_db:.2f} dB peak={ideal.peaking_db:.2f} dB "
        f"CMRR={ideal.cmrr_db:.2f} dB PSRR={ideal.psrr_db:.2f} dB"
    )

    if not args.no_tran:
        vbase_m = re.search(
            r"\.param\s+VBASE=([\S]+)",
            (spice_dir / "params.inc").read_text(),
        )
        if vbase_m:
            vbase = float(vbase_m.group(1))
            print("Running ideal transient (56G NRZ PRBS)...")
            run_tran("ideal", "ctle_ideal.cir", spice_dir, out_dir, vbase)

    pdk_metrics = None
    if not args.no_pdk:
        try:
            pdk_metrics = run_pass("pdk", "ctle_pdk.cir", spice_dir, out_dir)
            print(
                f"PDK: DC={pdk_metrics.dc_gain_db:.2f} dB peak={pdk_metrics.peaking_db:.2f} dB "
                f"CMRR={pdk_metrics.cmrr_db:.2f} dB PSRR={pdk_metrics.psrr_db:.2f} dB"
            )
        except RuntimeError as exc:
            print(f"PDK pass failed: {exc}")

    # Merge OP files
    op_main = out_dir / "op.txt"
    parts = []
    for name in ("ideal", "pdk"):
        p = out_dir / f"op_{name}.txt"
        if p.is_file():
            parts.append(f"=== {name} ===\n" + p.read_text())
    if parts:
        op_main.write_text("\n".join(parts))

    write_summary(out_dir / "summary.csv", params, ideal, pdk_metrics)
    print(f"Wrote {out_dir / 'summary.csv'}")


if __name__ == "__main__":
    main()
