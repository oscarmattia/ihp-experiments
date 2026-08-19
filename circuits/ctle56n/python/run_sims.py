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
PRBS9_BITS = 511  # one full PRBS9 period (2^9 − 1)
PRBS9_POLY = "x^9+x^5+1"
EYE_SETTLE_UI = 16
TRAN_ZOOM_START_UI = 32
TRAN_ZOOM_SPAN_UI = 40

# Single-bit response (SBR)
SBR_PRE = 3
SBR_POST = 10
SBR_KEEP_FRAC = 0.025  # keep |h_k| >= 2.5% of |h_0|
SBR_SETTLE_UI = 32
SBR_POST_UI = 24  # post-pulse zeros (covers 10 post-cursors + ringing)
SBR_BASELINE_UI_LO = 16
SBR_BASELINE_UI_HI = 30


@dataclass
class SbrResult:
    """Single-bit pulse response taps and normalized ISI metrics."""

    taps: list[tuple[int, float, bool]]  # (k, h_mV, kept)
    cursor_mV: float
    isi_norm: float  # sum(h_k, k≠0, kept) / h_0 (signed)
    isi_abs: float  # sum(|h_k|, k≠0, kept) / |h_0|
    t_cursor_s: float
    t_cursor_ui: float  # UI after pulse start
    t_pulse_start_s: float


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
    peak_gain_db: float = float("nan")
    f_peak_hz: float = float("nan")
    f_3db_hz: float = float("nan")


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


def _max_run(bits: list[int], val: int) -> int:
    best = cur = 0
    for b in bits:
        if b == val:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def prbs9_bits(n_bits: int = PRBS9_BITS, seed: int = 0x1FF) -> list[int]:
    """PRBS9 (ITU-T O.150 x^9 + x^5 + 1) Fibonacci LFSR; seed must be non-zero.

    Feedback fb = bit9 XOR bit5 = ((state >> 8) ^ (state >> 4)) & 1.
    Shift left and inject fb at LSB (standard O.150 PRBS9; passes run-length checks).
    """
    state = seed & 0x1FF
    if state == 0:
        state = 0x1FF
    bits: list[int] = []
    for _ in range(n_bits):
        bits.append((state >> 8) & 1)
        fb = ((state >> 8) ^ (state >> 4)) & 1
        state = ((state << 1) & 0x1FF) | fb

    if n_bits >= 511:
        seq = bits[:511]
        if 0 not in seq or 1 not in seq:
            raise ValueError("PRBS9 first 511 bits must contain both 0 and 1")
        max1 = _max_run(seq, 1)
        max0 = _max_run(seq, 0)
        if max1 != 9:
            raise ValueError(f"PRBS9 max run of 1s expected 9, got {max1} (check taps)")
        if max0 != 8:
            raise ValueError(f"PRBS9 max run of 0s expected 8, got {max0} (check taps)")
        transitions = sum(1 for i in range(1, len(seq)) if seq[i] != seq[i - 1])
        if transitions > 400:
            raise ValueError(
                f"PRBS9 looks like a clock ({transitions} transitions); check taps"
            )
    return bits


def compute_ac_peak_metrics(
    freq: np.ndarray, h_db: np.ndarray
) -> tuple[float, float, float, bool]:
    """Return (peak_gain_db, f_peak_hz, f_3db_hz, f3db_at_fmax)."""
    peak_idx = int(np.argmax(h_db))
    peak_gain_db = float(h_db[peak_idx])
    f_peak_hz = float(freq[peak_idx])
    target = peak_gain_db - 3.0
    f_3db_hz = float(freq[-1])
    f3db_at_fmax = True
    for i in range(peak_idx + 1, len(h_db)):
        if h_db[i] <= target:
            f_lo, f_hi = float(freq[i - 1]), float(freq[i])
            g_lo, g_hi = float(h_db[i - 1]), float(h_db[i])
            if g_hi != g_lo:
                f_3db_hz = f_lo + (target - g_lo) * (f_hi - f_lo) / (g_hi - g_lo)
            else:
                f_3db_hz = f_hi
            f3db_at_fmax = False
            break
    return peak_gain_db, f_peak_hz, f_3db_hz, f3db_at_fmax


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


def write_prbs_stim(path: Path, vbase: float, n_bits: int = PRBS9_BITS) -> float:
    """Write prbs_stim.inc with complementary NRZ PWL sources; return tmax (s)."""
    bits = prbs9_bits(n_bits)
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
        f"* PRBS9 ({PRBS9_POLY}) NRZ stimulus — generated by run_sims.py",
        f"* {n_bits} bits @ {BIT_RATE_HZ/1e9:.0f} Gb/s, {SWING_DIFF_V*1e3:.0f} mVpp,diff",
        *fmt_pwl("Vp", "inp", t_inp, v_inp),
        *fmt_pwl("Vn", "inn", t_inn, v_inn),
    ]
    path.write_text("\n".join(lines) + "\n")
    return len(bits) * UI_S


def write_sbr_stim(path: Path, vbase: float) -> float:
    """Write sbr_stim.inc with isolated 1-UI NRZ pulse; return tmax (s)."""
    bits = [0] * SBR_SETTLE_UI + [1] + [0] * SBR_POST_UI
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

    n_ui = len(bits)
    lines = [
        f"* Single-bit NRZ stimulus — generated by run_sims.py",
        f"* {SBR_SETTLE_UI} UI @0 + 1 UI @1 + {SBR_POST_UI} UI @0 "
        f"@ {BIT_RATE_HZ/1e9:.0f} Gb/s, {SWING_DIFF_V*1e3:.0f} mVpp,diff",
        *fmt_pwl("Vp", "inp", t_inp, v_inp),
        *fmt_pwl("Vn", "inn", t_inn, v_inn),
    ]
    path.write_text("\n".join(lines) + "\n")
    return n_ui * UI_S


def extract_sbr(
    time_s: np.ndarray,
    v_outp: np.ndarray,
    v_outn: np.ndarray,
) -> SbrResult:
    """Extract SBR taps from transient vod with baseline subtraction and truncation."""
    vod = v_outp - v_outn

    t_base_lo = SBR_BASELINE_UI_LO * UI_S
    t_base_hi = SBR_BASELINE_UI_HI * UI_S
    base_mask = (time_s >= t_base_lo) & (time_s <= t_base_hi)
    if not np.any(base_mask):
        raise ValueError("No samples for SBR idle baseline window")
    baseline = float(np.mean(vod[base_mask]))
    vod_ac = vod - baseline

    t_pulse_start = SBR_SETTLE_UI * UI_S
    t_cursor_lo = t_pulse_start
    t_cursor_hi = t_pulse_start + 3 * UI_S
    search_mask = (time_s >= t_cursor_lo) & (time_s <= t_cursor_hi)
    if not np.any(search_mask):
        raise ValueError("No samples in SBR cursor search window")

    vod_search = vod_ac[search_mask]
    time_search = time_s[search_mask]
    peak_idx = int(np.argmax(np.abs(vod_search)))
    t_cursor = float(time_search[peak_idx])

    taps_raw: dict[int, float] = {}
    for k in range(-SBR_PRE, SBR_POST + 1):
        t_sample = t_cursor + k * UI_S
        taps_raw[k] = float(np.interp(t_sample, time_s, vod_ac)) * 1e3

    h0_mV = taps_raw[0]
    h0_abs = abs(h0_mV)
    threshold = SBR_KEEP_FRAC * h0_abs

    taps: list[tuple[int, float, bool]] = []
    for k in range(-SBR_PRE, SBR_POST + 1):
        h_mV = taps_raw[k]
        kept = k == 0 or abs(h_mV) >= threshold
        taps.append((k, h_mV, kept))

    isi_sum = sum(h for k, h, kept in taps if kept and k != 0)
    isi_abs_sum = sum(abs(h) for k, h, kept in taps if kept and k != 0)
    isi_norm = isi_sum / h0_mV if h0_mV != 0 else float("nan")
    isi_abs = isi_abs_sum / h0_abs if h0_abs != 0 else float("nan")
    t_cursor_ui = (t_cursor - t_pulse_start) / UI_S

    return SbrResult(
        taps=taps,
        cursor_mV=h0_mV,
        isi_norm=isi_norm,
        isi_abs=isi_abs,
        t_cursor_s=t_cursor,
        t_cursor_ui=t_cursor_ui,
        t_pulse_start_s=t_pulse_start,
    )


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
    peak_gain_db, f_peak_hz, f_3db_hz, f3db_at_fmax = compute_ac_peak_metrics(freq, h_db)

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
        _plot_ac(
            freq,
            h_db,
            group_delay_s(freq, h_diff),
            out_dir / "ac_diff.png",
            peak_gain_db=peak_gain_db,
            f_peak_hz=f_peak_hz,
            f_3db_hz=f_3db_hz,
            f3db_at_fmax=f3db_at_fmax,
            dc_gain_db=dc_gain_db,
            peaking_db=peaking_db,
        )
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
        peak_gain_db=peak_gain_db,
        f_peak_hz=f_peak_hz,
        f_3db_hz=f_3db_hz,
    )


def run_tran(
    pass_name: str,
    dut_rel: str,
    spice_dir: Path,
    out_dir: Path,
    vbase: float,
) -> None:
    """Run 56G NRZ PRBS9 transient and plot waveforms + eye diagrams."""
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


def run_sbr(
    pass_name: str,
    dut_rel: str,
    spice_dir: Path,
    out_dir: Path,
    vbase: float,
) -> SbrResult:
    """Run single-bit response transient and extract pulse-response taps."""
    models = pdk_models()
    dut_cir = spice_dir / dut_rel
    work = out_dir / f"work_{pass_name}"
    work.mkdir(parents=True, exist_ok=True)

    for inc in ("params.inc", "cs.inc"):
        src = spice_dir / inc
        if src.is_file():
            shutil.copy(src, work / inc)

    tmax = write_sbr_stim(work / "sbr_stim.inc", vbase)
    tb_sbr = prepare_tb(
        spice_dir / "tb_sbr.cir",
        dut_cir,
        work,
        models,
        spice_dir,
        extra_params={"TMAX": f"{tmax:.6e}"},
    )
    run_ngspice(tb_sbr, work, "sbr.log")
    time_s, v_outp, v_outn, v_inp, v_inn = parse_tran_raw(work / "sbr.raw")
    sbr = extract_sbr(time_s, v_outp, v_outn)

    if pass_name == "ideal":
        _plot_sbr(time_s, v_outp, v_outn, v_inp, v_inn, sbr, out_dir / "sbr.png")

    return sbr


def _tran_zoom_mask(time_s: np.ndarray) -> np.ndarray:
    t0 = TRAN_ZOOM_START_UI * UI_S
    t1 = (TRAN_ZOOM_START_UI + TRAN_ZOOM_SPAN_UI) * UI_S
    return (time_s >= t0) & (time_s <= t1)


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
    zoom = _tran_zoom_mask(time_s)
    caption = (
        f"PRBS9 ({PRBS9_POLY}), {PRBS9_BITS} bits, 100 mVpp,diff "
        "— zoom shows irregular run lengths"
    )
    fig, (ax_full, ax_zoom) = plt.subplots(2, 1, figsize=(10, 7))

    for ax, mask, title in (
        (ax_full, slice(None), f"56G NRZ PRBS9 — full {PRBS9_BITS} UI (~{PRBS9_BITS * UI_S * 1e9:.2f} ns)"),
        (ax_zoom, zoom, caption),
    ):
        t_plot = t_ns[mask]
        ax.plot(t_plot, v_outp[mask], "b-", lw=1.0, label="v(outp)")
        ax.plot(t_plot, v_outn[mask], "r-", lw=1.0, label="v(outn)")
        ax.plot(t_plot, v_inp[mask], color="b", alpha=0.35, lw=0.8, label="v(inp)")
        ax.plot(t_plot, v_inn[mask], color="r", alpha=0.35, lw=0.8, label="v(inn)")
        stacked = np.concatenate([v_outp[mask], v_outn[mask], v_inp[mask], v_inn[mask]])
        lo, hi = float(np.min(stacked)), float(np.max(stacked))
        pad = max(0.02, 0.15 * (hi - lo))
        ax.set_ylim(lo - pad, hi + pad)
        ax.set_ylabel("Voltage (V)")
        ax.set_title(title)
        ax.grid(True, alpha=0.3)
        ax.legend(loc="upper right", fontsize=8)

    ax_zoom.set_xlabel("Time (ns)")
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
    zoom = _tran_zoom_mask(time_s)
    caption = (
        f"PRBS9 ({PRBS9_POLY}), {PRBS9_BITS} bits, 100 mVpp,diff "
        "— zoom shows irregular run lengths"
    )
    fig, (ax_full, ax_zoom) = plt.subplots(2, 1, figsize=(10, 7))

    for ax, mask, title in (
        (ax_full, slice(None), f"56G NRZ PRBS9 differential — full {PRBS9_BITS} UI"),
        (ax_zoom, zoom, caption),
    ):
        t_plot = t_ns[mask]
        ax.plot(t_plot, vod[mask], "b-", lw=1.0, label="vod = outp−outn")
        ax.plot(t_plot, vid[mask], color="orange", alpha=0.7, lw=0.8, label="vid = inp−inn")
        stacked = np.concatenate([vod[mask], vid[mask]])
        lo, hi = float(np.min(stacked)), float(np.max(stacked))
        pad = max(10.0, 0.15 * (hi - lo))
        ax.set_ylim(lo - pad, hi + pad)
        ax.set_ylabel("Differential (mV)")
        ax.set_title(title)
        ax.grid(True, alpha=0.3)
        ax.legend(loc="upper right", fontsize=8)

    ax_zoom.set_xlabel("Time (ns)")
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


def _sbr_tap_label(k: int) -> str:
    if k < 0:
        return f"h_{{{k}}} pre"
    if k == 0:
        return "h_0 cursor"
    return f"h_{k} post"


def _plot_sbr(
    time_s: np.ndarray,
    v_outp: np.ndarray,
    v_outn: np.ndarray,
    v_inp: np.ndarray,
    v_inn: np.ndarray,
    sbr: SbrResult,
    path: Path,
) -> None:
    import matplotlib.pyplot as plt

    vod = v_outp - v_outn
    vid = v_inp - v_inn
    t_base_lo = SBR_BASELINE_UI_LO * UI_S
    t_base_hi = SBR_BASELINE_UI_HI * UI_S
    base_mask = (time_s >= t_base_lo) & (time_s <= t_base_hi)
    baseline = float(np.mean(vod[base_mask]))
    vod_ac = (vod - baseline) * 1e3
    vid_mV = vid * 1e3

    t_cursor = sbr.t_cursor_s
    x_ui = (time_s - t_cursor) / UI_S

    # Show pre-cursors through post-cursors with margin
    x_lo = -SBR_PRE - 2
    x_hi = SBR_POST + 4
    mask = (x_ui >= x_lo) & (x_ui <= x_hi)

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(x_ui[mask], vod_ac[mask], "b-", lw=1.2, label="vod (baseline-subtracted)")
    ax.plot(
        x_ui[mask],
        vid_mV[mask],
        color="orange",
        alpha=0.45,
        lw=0.9,
        label="vid (input)",
    )

    kept_n = sum(1 for k, _, kept in sbr.taps if kept and k != 0)
    for k, h_mV, kept in sbr.taps:
        if not kept:
            continue
        x_k = k
        color = "green" if k == 0 else ("purple" if k < 0 else "teal")
        marker = "D" if k == 0 else "o"
        size = 9 if k == 0 else 6
        ax.plot(x_k, h_mV, marker=marker, color=color, ms=size, zorder=5)
        weight = "bold" if k == 0 else "normal"
        ax.annotate(
            f"{_sbr_tap_label(k)} = {h_mV:.2f} mV",
            xy=(x_k, h_mV),
            xytext=(x_k + 0.15, h_mV + (8 if k >= 0 else -8)),
            fontsize=7,
            fontweight=weight,
            arrowprops=dict(arrowstyle="->", color=color, lw=0.6, alpha=0.7),
        )

    ax.axvline(0, color="k", ls="--", alpha=0.4, label="cursor (h_0)")
    ax.axvline(
        (sbr.t_pulse_start_s - t_cursor) / UI_S,
        color="gray",
        ls=":",
        alpha=0.5,
        label="pulse start",
    )
    ax.set_xlabel("Time (UI relative to cursor)")
    ax.set_ylabel("Differential (mV)")
    ax.set_title("Single-bit response — 1 UI pulse, 100 mVpp,diff")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right", fontsize=7)

    info = (
        f"h_0 = {sbr.cursor_mV:.2f} mV\n"
        f"Normalized total ISI = {sbr.isi_norm:.4f}\n"
        f"Σ|h_k|/|h_0| = {sbr.isi_abs:.4f}\n"
        f"Kept ISI taps (k≠0): {kept_n}"
    )
    ax.text(
        0.02,
        0.98,
        info,
        transform=ax.transAxes,
        fontsize=8,
        va="top",
        bbox=dict(boxstyle="round", facecolor="wheat", alpha=0.85),
    )
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _plot_ac(
    freq: np.ndarray,
    h_db: np.ndarray,
    gd_s: np.ndarray,
    path: Path,
    *,
    peak_gain_db: float,
    f_peak_hz: float,
    f_3db_hz: float,
    f3db_at_fmax: bool,
    dc_gain_db: float,
    peaking_db: float,
) -> None:
    import matplotlib.pyplot as plt

    def _ghz(f_hz: float) -> str:
        return f"{f_hz / 1e9:.2f}"

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 6), sharex=True)
    ax1.semilogx(freq, h_db, "b-", lw=1.5)
    ax1.axvline(NYQUIST_HZ, color="r", ls="--", alpha=0.7, label="28 GHz Nyquist")
    ax1.axvline(f_peak_hz, color="g", ls="--", alpha=0.8, label=f"f_peak={_ghz(f_peak_hz)} GHz")
    f3_label = f"f_{{-3dB}}={_ghz(f_3db_hz)} GHz"
    if f3db_at_fmax:
        f3_label += " (no crossing to 300 GHz)"
    ax1.axvline(f_3db_hz, color="orange", ls="--", alpha=0.8, label=f3_label)
    peak_idx = int(np.argmax(h_db))
    ax1.plot(f_peak_hz, peak_gain_db, "go", ms=6, zorder=5)
    ax1.annotate(
        f"G_peak={peak_gain_db:.2f} dB @ f_peak",
        xy=(f_peak_hz, peak_gain_db),
        xytext=(f_peak_hz * 1.5, peak_gain_db - 2.0),
        fontsize=8,
        arrowprops=dict(arrowstyle="->", color="green", lw=0.8),
    )
    info = (
        f"DC={dc_gain_db:.2f} dB  G_peak={peak_gain_db:.2f} dB  "
        f"f_peak={_ghz(f_peak_hz)} GHz  f_{{-3dB}}={_ghz(f_3db_hz)} GHz  "
        f"peak@28G={peaking_db:.2f} dB"
    )
    ax1.text(
        0.02,
        0.02,
        info,
        transform=ax1.transAxes,
        fontsize=7,
        va="bottom",
        bbox=dict(boxstyle="round", facecolor="wheat", alpha=0.8),
    )
    ax1.set_ylabel("Gain (dB)")
    ax1.set_title("Differential AC — |vod/vid|")
    ax1.set_xlim(freq[0], AC_FMAX_HZ)
    ax1.grid(True, which="both", alpha=0.3)
    ax1.legend(fontsize=7, loc="upper right")

    gd_ps = gd_s * 1e12
    ax2.semilogx(freq, gd_ps, "g-", lw=1.5)
    ax2.axvline(NYQUIST_HZ, color="r", ls="--", alpha=0.7)
    ax2.axvline(f_peak_hz, color="g", ls="--", alpha=0.5)
    ax2.axvline(f_3db_hz, color="orange", ls="--", alpha=0.5)
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
    sbr: SbrResult | None = None,
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
        ["ideal_G_peak_dB", f"{ideal.peak_gain_db:.3f}"],
        ["ideal_f_peak_Hz", f"{ideal.f_peak_hz:.6g}"],
        ["ideal_f_3dB_Hz", f"{ideal.f_3db_hz:.6g}"],
        ["ideal_CMRR_dB", f"{ideal.cmrr_db:.3f}"],
        ["ideal_PSRR_dB", f"{ideal.psrr_db:.3f}"],
        ["ideal_VCE_V", f"{ideal.vce_v:.4f}"],
        ["ideal_VDS_tail_V", f"{ideal.vds_tail_v:.4f}"],
        ["ideal_VGS_tail_V", f"{ideal.vgs_tail_v:.4f}"],
        ["ideal_Ic_A", f"{ideal.ic_a:.6g}"],
        ["ideal_Id_tail_A", f"{ideal.id_tail_a:.6g}"],
    ]
    if sbr:
        rows += [
            ["sbr_cursor_mV", f"{sbr.cursor_mV:.4f}"],
            ["sbr_isi_norm", f"{sbr.isi_norm:.6g}"],
            ["sbr_isi_abs", f"{sbr.isi_abs:.6g}"],
        ]
        for k in range(-SBR_PRE, SBR_POST + 1):
            entry = next((t for t in sbr.taps if t[0] == k), None)
            if entry is None:
                continue
            _, h_mV, kept = entry
            val = f"{h_mV:.4f}" if kept else ""
            rows.append([f"sbr_h{k}_mV", val])
    if pdk:
        rows += [
            ["pdk_dc_gain_dB", f"{pdk.dc_gain_db:.3f}"],
            ["pdk_peaking_28G_dB", f"{pdk.peaking_db:.3f}"],
            ["pdk_G_peak_dB", f"{pdk.peak_gain_db:.3f}"],
            ["pdk_f_peak_Hz", f"{pdk.f_peak_hz:.6g}"],
            ["pdk_f_3dB_Hz", f"{pdk.f_3db_hz:.6g}"],
            ["pdk_CMRR_dB", f"{pdk.cmrr_db:.3f}"],
            ["pdk_PSRR_dB", f"{pdk.psrr_db:.3f}"],
            ["pdk_VCE_V", f"{pdk.vce_v:.4f}"],
            ["pdk_targets_ok", targets_ok(pdk)],
        ]
    with path.open("w", newline="") as f:
        csv.writer(f).writerows(rows)


def _fmt_hz(hz: float) -> str:
    if math.isnan(hz):
        return "—"
    if hz >= 1e9:
        return f"{hz / 1e9:.2f} GHz"
    if hz >= 1e6:
        return f"{hz / 1e6:.1f} MHz"
    return f"{hz:.3g} Hz"


def _fmt_db(val: float) -> str:
    return "—" if math.isnan(val) else f"{val:.2f} dB"


def _fmt_v(val: float) -> str:
    return "—" if math.isnan(val) else f"{val:.3f} V"


def _fmt_a(val: float) -> str:
    return "—" if math.isnan(val) else f"{val * 1e3:.3f} mA"


def write_ctle_report(
    path: Path,
    params: CtleParams,
    ideal: SimMetrics,
    pdk: SimMetrics | None,
    sbr: SbrResult | None = None,
) -> None:
    """Regenerate circuits/ctle56n/ctle_report.md from sizing + sim metrics."""
    m_bessel = params.l_h / (params.rd_ohm**2 * params.cl_f)
    l_ph = params.l_h * 1e12
    mos_vgs = params.mos_vgs
    rppd_note = f"{params.rppd_w_um:.1f}×{params.rppd_l_um:.1f} µm" if params.rppd_w_um else "ideal RD"

    def row(param: str, symbol: str, ideal_val: str, pdk_val: str, notes: str = "") -> str:
        if pdk is None:
            return f"| {param} | {symbol} | {ideal_val} | {notes} |"
        return f"| {param} | {symbol} | {ideal_val} | {pdk_val} | {notes} |"

    pdk_dc = _fmt_db(pdk.dc_gain_db) if pdk else "—"
    pdk_peak28 = _fmt_db(pdk.peaking_db) if pdk else "—"
    pdk_gpeak = _fmt_db(pdk.peak_gain_db) if pdk else "—"
    pdk_fpeak = _fmt_hz(pdk.f_peak_hz) if pdk else "—"
    pdk_f3 = _fmt_hz(pdk.f_3db_hz) if pdk else "—"
    pdk_cmrr = _fmt_db(pdk.cmrr_db) if pdk else "—"
    pdk_psrr = _fmt_db(pdk.psrr_db) if pdk else "—"
    pdk_vce = _fmt_v(pdk.vce_v) if pdk else "—"
    pdk_vds = _fmt_v(pdk.vds_tail_v) if pdk else "—"

    table_header = (
        "| Parameter | Symbol | Ideal | PDK | Notes |\n"
        "| --- | --- | --- | --- | --- |"
        if pdk
        else "| Parameter | Symbol | Value | Notes |\n| --- | --- | --- | --- |"
    )

    rows = [
        row("Emitter multiplier", "Nx", str(params.nx), str(params.nx), "HBT LUT index"),
        row("HBT VBE (LUT)", "VBE", f"{params.vbe:.3f} V", f"{params.vbe:.3f} V", "max-fT bias"),
        row("Input common-mode", "VBASE", f"{params.vbase:.3f} V", f"{params.vbase:.3f} V", "inp/inn DC"),
        row("Supply", "VDD", f"{params.vdd:.3f} V", f"{params.vdd:.3f} V", "below BVceo ~1.6 V"),
        row("HBT collector current", "Ic", _fmt_a(ideal.ic_a), _fmt_a(pdk.ic_a) if pdk else "—", "per side"),
        row("Tail current", "I_tail", _fmt_a(params.itail_a), _fmt_a(params.itail_a), "2×Ic nominal"),
        row("Transition frequency", "f_T", _fmt_hz(params.ft_hz), _fmt_hz(params.ft_hz), "LUT at bias"),
        row("Transconductance", "g_m", f"{params.gm * 1e3:.2f} mS", f"{params.gm * 1e3:.2f} mS", ""),
        row("Input capacitance", "C_in", f"{params.cin_f * 1e15:.2f} fF", f"{params.cin_f * 1e15:.2f} fF", "HBT CIN"),
        row("Load capacitance", "C_L", f"{params.cl_f * 1e15:.2f} fF", f"{params.cl_f * 1e15:.2f} fF", "FO1 = C_in"),
        row("Load resistor", "R_D", f"{params.rd_ohm:.1f} Ω", rppd_note if pdk else f"{params.rd_ohm:.1f} Ω", "shunt peak"),
        row("Emitter degeneration", "R_s", f"{params.rs_ohm:.1f} Ω", f"{params.rs_ohm:.1f} Ω", ""),
        row("Degeneration cap", "C_s", f"{params.cs_f * 1e15:.1f} fF", f"{params.cs_f * 1e15:.1f} fF", "ideal or MIM"),
        row(
            "Drain inductor",
            "L",
            f"{l_ph:.2f} pH",
            f"{l_ph:.2f} pH",
            "ideal; VDD→L→R_D→collector; no PDK spiral (l2n0 ~2 nH)",
        ),
        row("Bessel MFD", "m", f"{m_bessel:.2f}", f"{m_bessel:.2f}", "L/(R_D² C_L)"),
        row(
            "MOS tail W/L/VGS",
            "W/L/VGS",
            f"{params.mos_w_um:.0f}/{params.mos_l_um:.1f}/{mos_vgs:.3f} V",
            f"{params.mos_w_um:.0f}/{params.mos_l_um:.1f}/{mos_vgs:.3f} V",
            "LV NMOS + mirror",
        ),
        row("RPPD load", "W/L", "ideal R", rppd_note, "LUT ≈ R_D/0.88"),
        row("DC gain", "A_v0", _fmt_db(ideal.dc_gain_db), pdk_dc, "−6…0 dB target"),
        row("Peaking @ 28 GHz", "—", _fmt_db(ideal.peaking_db), pdk_peak28, "3–10 dB target"),
        row("Peak AC gain", "G_peak", _fmt_db(ideal.peak_gain_db), pdk_gpeak, ""),
        row("Peak frequency", "f_peak", _fmt_hz(ideal.f_peak_hz), pdk_fpeak, ""),
        row("−3 dB bandwidth", "f_{−3dB}", _fmt_hz(ideal.f_3db_hz), pdk_f3, "after peak"),
        row("CMRR", "—", _fmt_db(ideal.cmrr_db), pdk_cmrr, "> 6 dB"),
        row("PSRR", "—", _fmt_db(ideal.psrr_db), pdk_psrr, "> 20 dB (clipped 120 dB)"),
        row("HBT VCE", "V_CE", _fmt_v(ideal.vce_v), pdk_vce, ""),
        row("MOS tail VDS", "V_DS,tail", _fmt_v(ideal.vds_tail_v), pdk_vds, ""),
    ]

    stim_note = (
        f"Transient stimulus: **PRBS9** ({PRBS9_POLY}), **{PRBS9_BITS} UI** "
        f"(one full period), **100 mVpp,diff**, ~4.5 ps edges. "
        f"AC sweep **1 MHz–300 GHz**. CMRR **> 6 dB**, PSRR **> 20 dB**."
    )

    body = f"""# 56 Gb/s NRZ CML CTLE — design report

Auto-generated by `python/run_sims.py` — do not hand-edit numbers.

## Topology

CML continuous-time linear equalizer (CTLE) for **56 Gb/s NRZ** (Nyquist **28 GHz**).
HBT differential pair (`npn13G2`) with **shunt-peaked loads** (R_D + ideal L from VDD),
**emitter degeneration** (R_s + C_s), and an **LV NMOS tail** with 1:1 diode-connected mirror.

Sizing uses characterization LUTs (`char/bjt`, `char/mos`, `char/passive`) at max-f_T HBT bias.
Load C_L = C_in of one FO1 input. Bessel shunt-peaking **m = L/(R_D² C_L) ≈ {m_bessel:.2f}**.

The drain inductor **L = {l_ph:.2f} pH** ({params.l_h:.6g} H) is physically tiny (via / short-trace scale).
No PDK spiral is used — minimum EM cell `l2n0` is ~2 nH, far too large. L remains **ideal** in ngspice.

## Targets

- DC gain **−6 … 0 dB** (aim 0 dB)
- Peaking **3–10 dB at 28 GHz**
- CMRR **> 6 dB**, PSRR **> 20 dB** at low frequency
- {stim_note}

## Sizing summary

{table_header}
{chr(10).join(rows)}
"""
    if sbr:
        sbr_rows: list[str] = []
        h0 = sbr.cursor_mV
        for k, h_mV, kept in sbr.taps:
            label = _sbr_tap_label(k)
            ratio = h_mV / h0 if h0 != 0 else float("nan")
            kept_str = "yes" if kept else "no"
            if k == 0:
                sbr_rows.append(
                    f"| **{label}** | {k} | {h_mV:.3f} | 1.000 | {kept_str} |"
                )
            else:
                sbr_rows.append(
                    f"| {label} | {k} | {h_mV:.3f} | {ratio:.4f} | {kept_str} |"
                )
        kept_isi = [h for k, h, kept in sbr.taps if kept and k != 0]
        body += f"""
## Single-bit response

Isolated **1 UI** NRZ pulse (**100 mVpp,diff**, ±50 mV vid), after **{SBR_SETTLE_UI} UI** settle at logic 0.
Sample **{SBR_PRE} pre-cursors + cursor + {SBR_POST} post-cursors** every UI; drop taps with
|h| < **{SBR_KEEP_FRAC * 100:.1f}%** of |cursor| (h_0 always kept).

| Tap | k | h (mV) | h / h_0 | Kept |
| --- | --- | --- | --- | --- |
{chr(10).join(sbr_rows)}

- Main cursor h_0 = **{sbr.cursor_mV:.2f} mV** at t = **{sbr.t_cursor_ui:.3f} UI** after pulse start
- Normalized total ISI = Σ h_k / h_0 = **{sbr.isi_norm:.4f}** (k≠0, kept taps only)
- Σ|h_k|/|h_0| = **{sbr.isi_abs:.4f}** (same taps)
- Taps with |h| < {SBR_KEEP_FRAC * 100:.1f}% of |cursor| are omitted from the ISI sums.
"""
    path.write_text(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-iterate", action="store_true", help="Skip iteration loop")
    parser.add_argument("--no-pdk", action="store_true", help="Skip PDK passive pass")
    parser.add_argument("--no-tran", action="store_true", help="Skip 56G NRZ PRBS9 and SBR transient")
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

    sbr_result: SbrResult | None = None
    if not args.no_tran:
        vbase_m = re.search(
            r"\.param\s+VBASE=([\S]+)",
            (spice_dir / "params.inc").read_text(),
        )
        if vbase_m:
            vbase = float(vbase_m.group(1))
            print("Running ideal transient (56G NRZ PRBS9)...")
            run_tran("ideal", "ctle_ideal.cir", spice_dir, out_dir, vbase)
            print("Running ideal single-bit response (1 UI pulse)...")
            sbr_result = run_sbr("ideal", "ctle_ideal.cir", spice_dir, out_dir, vbase)

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

    write_summary(out_dir / "summary.csv", params, ideal, pdk_metrics, sbr_result)
    write_ctle_report(_EXP / "ctle_report.md", params, ideal, pdk_metrics, sbr_result)
    print(f"Wrote {out_dir / 'summary.csv'}")
    print(f"Wrote {_EXP / 'ctle_report.md'}")


if __name__ == "__main__":
    main()
