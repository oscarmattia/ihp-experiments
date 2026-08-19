"""AC/SBR metric extraction, CSV writers, and target checks."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .stim import UI_S

NYQUIST_HZ = 28e9
DC_GAIN_MIN_DB = -6.0
DC_GAIN_MAX_DB = 0.0
PEAK_MIN_DB = 3.0
PEAK_MAX_DB = 10.0
CMRR_MIN_DB = 6.0
PSRR_MIN_DB = 20.0
PSRR_MAX_DB = 120.0
AC_FMAX_HZ = 300e9

EYE_SETTLE_UI = 16

SBR_PRE = 3
SBR_POST = 10
SBR_KEEP_FRAC = 0.025
SBR_BASELINE_UI_LO = 16
SBR_BASELINE_UI_HI = 30


@dataclass
class SbrResult:
    """Single-bit pulse response taps and normalized ISI metrics."""

    taps: list[tuple[int, float, bool]]
    cursor_mV: float
    isi_norm: float
    isi_abs: float
    t_cursor_s: float
    t_cursor_ui: float
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


def sbr_tap_label(k: int) -> str:
    if k < 0:
        return f"h_{{{k}}} pre"
    if k == 0:
        return "h_0 cursor"
    return f"h_{k} post"


def interp_db_at(freq: np.ndarray, h_db: np.ndarray, f_target: float) -> float:
    if f_target <= freq[0]:
        return float(h_db[0])
    if f_target >= freq[-1]:
        return float(h_db[-1])
    return float(np.interp(f_target, freq, h_db))


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


def group_delay_s(freq: np.ndarray, h: np.ndarray) -> np.ndarray:
    phase = np.unwrap(np.angle(h))
    omega = 2.0 * np.pi * freq
    d_phase = np.gradient(phase, omega)
    return -d_phase


def extract_sbr(
    time_s: np.ndarray,
    v_outp: np.ndarray,
    v_outn: np.ndarray,
) -> SbrResult:
    """Extract SBR taps from transient vod with baseline subtraction and truncation."""
    from .stim import SBR_SETTLE_UI

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


def write_tran_csv(
    path: Path,
    time_s: np.ndarray,
    v_outp: np.ndarray,
    v_outn: np.ndarray,
    v_inp: np.ndarray,
    v_inn: np.ndarray,
) -> None:
    vod = v_outp - v_outn
    vid = v_inp - v_inn
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time_s", "v_outp", "v_outn", "v_inp", "v_inn", "vod", "vid"])
        for i in range(len(time_s)):
            w.writerow([
                time_s[i], v_outp[i], v_outn[i], v_inp[i], v_inn[i], vod[i], vid[i],
            ])


def write_ac_diff_csv(
    path: Path,
    freq: np.ndarray,
    h_db: np.ndarray,
    gd_s: np.ndarray,
) -> None:
    gd_ps = gd_s * 1e12
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["freq_Hz", "gain_dB", "gd_ps"])
        for i in range(len(freq)):
            w.writerow([freq[i], h_db[i], gd_ps[i]])


def write_eye_csvs(
    pass_dir: Path,
    time_s: np.ndarray,
    v_outp: np.ndarray,
    v_outn: np.ndarray,
) -> None:
    """Dump post-settle samples folded into 0–2 UI for eye CSVs."""
    t0 = EYE_SETTLE_UI * UI_S
    mask = time_s >= t0
    t_rel = time_s[mask] - t0
    period = 2.0 * UI_S
    t_ui = np.mod(t_rel, period) / UI_S
    vod_mV = (v_outp[mask] - v_outn[mask]) * 1e3

    with (pass_dir / "eye_diff.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_ui", "vod_mV"])
        for t, v in zip(t_ui, vod_mV):
            w.writerow([t, v])

    with (pass_dir / "eye_se.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_ui", "v_outp", "v_outn"])
        for t, p, n in zip(t_ui, v_outp[mask], v_outn[mask]):
            w.writerow([t, p, n])


def write_sbr_taps_csv(path: Path, sbr: SbrResult) -> None:
    h0 = sbr.cursor_mV
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["k", "h_mV", "h_over_h0", "kept"])
        for k, h_mV, kept in sbr.taps:
            ratio = h_mV / h0 if h0 != 0 else ""
            w.writerow([k, h_mV, ratio, "yes" if kept else "no"])


def write_pass_metrics(
    path: Path,
    m: SimMetrics,
    sbr: SbrResult | None = None,
) -> None:
    prefix = m.pass_name
    rows: list[list[str]] = [
        ["parameter", "value"],
        [f"{prefix}_dc_gain_dB", f"{m.dc_gain_db:.3f}"],
        [f"{prefix}_peaking_28G_dB", f"{m.peaking_db:.3f}"],
        [f"{prefix}_G_peak_dB", f"{m.peak_gain_db:.3f}"],
        [f"{prefix}_f_peak_Hz", f"{m.f_peak_hz:.6g}"],
        [f"{prefix}_f_3dB_Hz", f"{m.f_3db_hz:.6g}"],
        [f"{prefix}_CMRR_dB", f"{m.cmrr_db:.3f}"],
        [f"{prefix}_PSRR_dB", f"{m.psrr_db:.3f}"],
        [f"{prefix}_VCE_V", f"{m.vce_v:.4f}"],
        [f"{prefix}_VDS_tail_V", f"{m.vds_tail_v:.4f}"],
        [f"{prefix}_VGS_tail_V", f"{m.vgs_tail_v:.4f}"],
        [f"{prefix}_Ic_A", f"{m.ic_a:.6g}"],
        [f"{prefix}_Id_tail_A", f"{m.id_tail_a:.6g}"],
    ]
    if sbr:
        rows += [
            [f"{prefix}_sbr_cursor_mV", f"{sbr.cursor_mV:.4f}"],
            [f"{prefix}_sbr_isi_norm", f"{sbr.isi_norm:.6g}"],
            [f"{prefix}_sbr_isi_abs", f"{sbr.isi_abs:.6g}"],
        ]
    with path.open("w", newline="") as f:
        csv.writer(f).writerows(rows)


def targets_ok(m: SimMetrics) -> bool:
    return (
        DC_GAIN_MIN_DB <= m.dc_gain_db <= DC_GAIN_MAX_DB
        and PEAK_MIN_DB <= m.peaking_db <= PEAK_MAX_DB
        and m.cmrr_db >= CMRR_MIN_DB
        and m.psrr_db >= PSRR_MIN_DB
    )
