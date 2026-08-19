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
SBR_KEEP_FRAC = 0.005
EYE_PHASE_WINDOW = 0.02
EYE_WIDTH_OPEN_FRAC = 0.30
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
class EyeMetrics:
    """Differential eye metrics from post-settle PRBS transient."""

    height_mV: float
    width_ui: float
    width_ps: float
    pp_swing_mV: float
    sample_phase_ui: float


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
    rd_realized_ohm: float = float("nan")
    m_realized: float = float("nan")


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


def _fold_1ui_traces(
    time_s: np.ndarray,
    vod: np.ndarray,
    settle_ui: int = EYE_SETTLE_UI,
    n_pts: int = 400,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (phase_ui, traces) with one UI period per row."""
    t0 = settle_ui * UI_S
    mask = time_s >= t0
    t_rel = time_s[mask] - t0
    sig = vod[mask]
    if t_rel.size < 4:
        return np.linspace(0.0, 1.0, n_pts, endpoint=False), np.empty((0, n_pts))

    period = UI_S
    t_fold = np.linspace(0.0, period, n_pts)
    phases = t_fold / UI_S
    n_ui = int((t_rel[-1] - t_rel[0]) / period)
    traces: list[np.ndarray] = []
    for k in range(n_ui):
        t_start = k * period
        seg_mask = (t_rel >= t_start) & (t_rel < t_start + period)
        if int(np.sum(seg_mask)) < 4:
            continue
        t_seg = t_rel[seg_mask] - t_start
        s_seg = sig[seg_mask]
        traces.append(np.interp(t_fold, t_seg, s_seg))
    if not traces:
        return phases, np.empty((0, n_pts))
    return phases, np.asarray(traces)


def _vertical_opening_at_phase_mv(
    t_ui: np.ndarray,
    vod_v: np.ndarray,
    phases: np.ndarray,
    window_ui: float = EYE_PHASE_WINDOW,
) -> np.ndarray:
    """Vertical opening at 0 V from dense 1-UI-folded samples."""
    heights = np.zeros(len(phases))
    for i, phi in enumerate(phases):
        delta = np.abs(t_ui - phi)
        delta = np.minimum(delta, 1.0 - delta)
        wmask = delta <= window_ui
        if not np.any(wmask):
            continue
        samples = vod_v[wmask]
        above = samples[samples >= 0.0]
        below = samples[samples < 0.0]
        if above.size and below.size:
            heights[i] = (float(np.min(above)) - float(np.max(below))) * 1e3
    return heights


def _vertical_opening_profile_mv(
    traces: np.ndarray,
    phases: np.ndarray,
    window_ui: float = EYE_PHASE_WINDOW,
) -> np.ndarray:
    """Vertical opening at 0 V: min(above 0) − max(below 0) in ±window_ui."""
    heights = np.zeros(len(phases))
    for i, phi in enumerate(phases):
        delta = np.abs(phases - phi)
        delta = np.minimum(delta, 1.0 - delta)
        wmask = delta <= window_ui
        if not np.any(wmask):
            continue
        samples = traces[:, wmask].ravel()
        above = samples[samples >= 0.0]
        below = samples[samples < 0.0]
        if above.size and below.size:
            heights[i] = (float(np.min(above)) - float(np.max(below))) * 1e3
    return heights


def _roll_traces_to_centre(
    traces: np.ndarray,
    phases: np.ndarray,
    centre_ui: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Roll 1-UI folded traces so ``centre_ui`` maps to phase 0."""
    n_pts = len(phases)
    shift_idx = int(round(centre_ui * n_pts)) % n_pts
    rolled = np.roll(traces, -shift_idx, axis=1)
    centered_phases = np.mod(phases - centre_ui + 0.5, 1.0) - 0.5
    order = np.argsort(centered_phases)
    return centered_phases[order], rolled[:, order]


def _trace_zero_crossings_ui(traces: np.ndarray, phases: np.ndarray) -> list[float]:
    crossings: list[float] = []
    for tr in traces:
        for i in range(len(phases) - 1):
            y0, y1 = float(tr[i]), float(tr[i + 1])
            if y0 == 0.0:
                crossings.append(float(phases[i]))
            if y0 * y1 < 0.0:
                frac = abs(y0) / (abs(y0) + abs(y1))
                crossings.append(float(phases[i] + frac * (phases[i + 1] - phases[i])))
    return crossings


def _eye_width_centered_ui(
    phases: np.ndarray,
    heights_mV: np.ndarray,
) -> float:
    """Horizontal opening at 0 V from threshold crossings symmetric about ph = 0.

    ``phases`` must be centred on the optimal sample instant (ph = 0 at the eye
    centre). Width is measured only from the centred contour — no UI-boundary clip.
    """
    h_peak = float(np.max(heights_mV))
    if h_peak <= 0.0:
        return 0.0

    thresh = EYE_WIDTH_OPEN_FRAC * h_peak
    idx0 = int(np.argmin(np.abs(phases)))

    left = float(phases[0])
    for i in range(idx0, -1, -1):
        if heights_mV[i] < thresh:
            left = float(phases[min(i + 1, idx0)])
            break

    right = float(phases[-1])
    for i in range(idx0, len(phases)):
        if heights_mV[i] < thresh:
            right = float(phases[i])
            break

    width_ui = right - left
    if width_ui >= 1.0:
        raise ValueError(
            f"Eye width {width_ui:.4f} UI >= 1 UI "
            f"(left={left:.4f}, right={right:.4f}, centre phase=0)"
        )
    return width_ui


def compute_eye_metrics(
    time_s: np.ndarray,
    v_outp: np.ndarray,
    v_outn: np.ndarray,
) -> EyeMetrics:
    """Eye height/width/pp swing from post-settle differential PRBS transient.

    Phase-invariant: roll settled samples so the optimal sample sits at ph = 0 before
    height and width are extracted. Ideal-vs-PDK passes that differ only in passives
    must agree in width (same channel); height may differ slightly with realized RD.
    """
    vod = v_outp - v_outn
    t0 = EYE_SETTLE_UI * UI_S
    settled_mask = time_s >= t0
    if not np.any(settled_mask):
        raise ValueError("No post-settle samples for eye metrics")

    settled = vod[settled_mask]
    pp_swing_mV = float((np.max(settled) - np.min(settled)) * 1e3)

    tm = np.mod(time_s[settled_mask] - t0, UI_S) / UI_S
    vod_settled = settled

    scan_phases = np.linspace(0.0, 1.0, 800, endpoint=False)
    centered_phases = np.linspace(-0.5, 0.5, 800, endpoint=False)
    idx0 = int(np.argmin(np.abs(centered_phases)))

    heights_scan_mV = np.zeros(len(scan_phases))
    for i, centre_ui in enumerate(scan_phases):
        ph = np.mod(tm - centre_ui + 0.5, 1.0) - 0.5
        heights_at_centre = _vertical_opening_at_phase_mv(
            ph, vod_settled, centered_phases,
        )
        heights_scan_mV[i] = heights_at_centre[idx0]

    # Every candidate centre is scored on a rolled axis — no seam exclusion.
    centre_idx = int(np.argmax(heights_scan_mV))
    centre_ui = float(scan_phases[centre_idx])
    height_mV = float(heights_scan_mV[centre_idx])

    ph = np.mod(tm - centre_ui + 0.5, 1.0) - 0.5
    heights_centered_mV = _vertical_opening_at_phase_mv(
        ph, vod_settled, centered_phases,
    )

    if height_mV > pp_swing_mV:
        height_mV = pp_swing_mV

    width_ui = _eye_width_centered_ui(centered_phases, heights_centered_mV)
    width_ps = width_ui * UI_S * 1e12

    return EyeMetrics(
        height_mV=height_mV,
        width_ui=width_ui,
        width_ps=width_ps,
        pp_swing_mV=pp_swing_mV,
        sample_phase_ui=centre_ui,
    )


def verify_eye_phase_invariance(
    time_s: np.ndarray,
    v_outp: np.ndarray,
    v_outn: np.ndarray,
    n_offsets: int = 24,
    height_tol_frac_pp: float = 0.02,
    width_tol_frac: float = 0.05,
) -> tuple[bool, str, float, float]:
    """Sweep artificial time offsets over one UI; height/width must stay stable.

    Returns (ok, summary, max_height_err_frac_pp, max_width_err_frac).
    """
    base = compute_eye_metrics(time_s, v_outp, v_outn)
    max_h_err = 0.0
    max_w_err = 0.0
    for i in range(1, n_offsets):
        dt = i * UI_S / n_offsets
        eye = compute_eye_metrics(time_s + dt, v_outp, v_outn)
        if base.pp_swing_mV > 1e-6:
            max_h_err = max(max_h_err, abs(eye.height_mV - base.height_mV) / base.pp_swing_mV)
        if base.width_ui > 1e-6:
            max_w_err = max(max_w_err, abs(eye.width_ui - base.width_ui) / base.width_ui)
    ok = max_h_err <= height_tol_frac_pp and max_w_err <= width_tol_frac
    summary = (
        f"height_err/pp={max_h_err:.4f} (tol {height_tol_frac_pp}) "
        f"width_err={max_w_err:.4f} (tol {width_tol_frac})"
    )
    return ok, summary, max_h_err, max_w_err


def eye_metrics_rows(prefix: str, eye: EyeMetrics) -> list[list[str]]:
    return [
        [f"{prefix}_eye_height_mV", f"{eye.height_mV:.2f}"],
        [f"{prefix}_eye_width_UI", f"{eye.width_ui:.4f}"],
        [f"{prefix}_eye_width_ps", f"{eye.width_ps:.3f}"],
        [f"{prefix}_eye_pp_swing_mV", f"{eye.pp_swing_mV:.2f}"],
        [f"{prefix}_eye_sample_phase_UI", f"{eye.sample_phase_ui:.4f}"],
    ]


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
    eye: EyeMetrics | None = None,
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
        [f"{prefix}_RD_realized_ohm", f"{m.rd_realized_ohm:.4f}"],
        [f"{prefix}_m", f"{m.m_realized:.4f}"],
    ]
    if sbr:
        rows += [
            [f"{prefix}_sbr_cursor_mV", f"{sbr.cursor_mV:.4f}"],
            [f"{prefix}_sbr_isi_norm", f"{sbr.isi_norm:.6g}"],
            [f"{prefix}_sbr_isi_abs", f"{sbr.isi_abs:.6g}"],
        ]
    if eye:
        rows += eye_metrics_rows(prefix, eye)
    with path.open("w", newline="") as f:
        csv.writer(f).writerows(rows)


def targets_ok(m: SimMetrics) -> bool:
    return (
        DC_GAIN_MIN_DB <= m.dc_gain_db <= DC_GAIN_MAX_DB
        and PEAK_MIN_DB <= m.peaking_db <= PEAK_MAX_DB
        and m.cmrr_db >= CMRR_MIN_DB
        and m.psrr_db >= PSRR_MIN_DB
    )
