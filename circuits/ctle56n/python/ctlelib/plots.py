"""Matplotlib figure writers for AC, transient, eye, and SBR plots."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .metrics import (
    AC_FMAX_HZ,
    AC_PLOT_FMAX_HZ,
    AC_PLOT_FMIN_HZ,
    CMRR_MIN_DB,
    EYE_SETTLE_UI,
    PSRR_MIN_DB,
    SBR_BASELINE_UI_HI,
    SBR_BASELINE_UI_LO,
    SBR_PRE,
    SBR_POST,
    SbrResult,
    sbr_tap_label,
)
from .stim import PRBS9_BITS, PRBS9_POLY, UI_S

NYQUIST_HZ = 28e9
TRAN_ZOOM_START_UI = 32
TRAN_ZOOM_SPAN_UI = 40


def _tran_zoom_mask(time_s: np.ndarray) -> np.ndarray:
    t0 = TRAN_ZOOM_START_UI * UI_S
    t1 = (TRAN_ZOOM_START_UI + TRAN_ZOOM_SPAN_UI) * UI_S
    return (time_s >= t0) & (time_s <= t1)


def plot_tran_se(
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


def plot_tran_diff(
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
    t0 = settle_ui * ui_s
    mask = time_s >= t0
    t_rel = time_s[mask] - t0
    sig = signal[mask]
    period = 2.0 * ui_s
    n_ui_pairs = int((t_rel[-1] - t_rel[0]) / period)
    traces: list[np.ndarray] = []
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
    return t_fold * 1e12, traces


def plot_eye_se(
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


def plot_eye_diff(
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


def plot_sbr(
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
            f"{sbr_tap_label(k)} = {h_mV:.2f} mV",
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


def plot_ac(
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
    ax1.plot(f_peak_hz, peak_gain_db, "go", ms=6, zorder=5)
    info = (
        f"DC={dc_gain_db:.2f} dB  G_peak={peak_gain_db:.2f} dB\n"
        f"f_peak={_ghz(f_peak_hz)} GHz  f_{{-3dB}}={_ghz(f_3db_hz)} GHz\n"
        f"peak@28G={peaking_db:.2f} dB"
    )
    ax1.text(
        0.5,
        0.5,
        info,
        transform=ax1.transAxes,
        fontsize=7,
        ha="center",
        va="center",
        bbox=dict(boxstyle="round", facecolor="wheat", alpha=0.8),
    )
    ax1.set_ylabel("Gain (dB)")
    ax1.set_title("Differential AC — |vod/vid|")
    ax1.set_xlim(AC_PLOT_FMIN_HZ, AC_PLOT_FMAX_HZ)
    ax1.grid(True, which="both", alpha=0.3)
    ax1.legend(fontsize=7, loc="lower left")

    gd_ps = gd_s * 1e12
    gd_valid = np.isfinite(gd_ps) & (freq >= 100e6)
    if np.any(gd_valid):
        ax2.semilogx(freq[gd_valid], gd_ps[gd_valid], "g-", lw=1.5)
        y = gd_ps[gd_valid]
        y_margin = max(0.5, 0.12 * (float(np.max(y)) - float(np.min(y))))
        ax2.set_ylim(float(np.min(y)) - y_margin, float(np.max(y)) + y_margin)
    ax2.axvline(NYQUIST_HZ, color="r", ls="--", alpha=0.7)
    ax2.axvline(f_peak_hz, color="g", ls="--", alpha=0.5)
    ax2.axvline(f_3db_hz, color="orange", ls="--", alpha=0.5)
    ax2.set_ylabel("Group delay (ps)")
    ax2.set_xlabel("Frequency (Hz)")
    ax2.set_xlim(AC_PLOT_FMIN_HZ, AC_PLOT_FMAX_HZ)
    ax2.grid(True, which="both", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


CHAIN_STAGE_COLORS = {
    "term": "#666666",
    "ctle": "#1f77b4",
    "vga": "#ff7f0e",
    "driver": "#9467bd",
    "e2e_src": "#000000",
    "e2e_pad": "#2ca02c",
}


def plot_chain_ac_perstg(
    freq: np.ndarray,
    stage_db: dict[str, np.ndarray],
    path: Path,
    *,
    title: str = "Chain AC — per-stage incremental gain",
) -> None:
    """Overlay term/CTLE/VGA/driver incremental |H| (dB) from chain AC wrdata."""
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 4.5))
    order = [
        ("term", "term (pad→CTLE in)"),
        ("ctle", "CTLE (CTLE in→VGA in)"),
        ("vga", "VGA (VGA in→drv in)"),
        ("driver", "driver (drv in→pad out)"),
    ]
    for key, label in order:
        db_key = f"h_{key}_db"
        if db_key in stage_db:
            ax.semilogx(
                freq,
                stage_db[db_key],
                lw=1.3,
                color=CHAIN_STAGE_COLORS[key],
                label=label,
            )
    if "h_src_db" in stage_db:
        ax.semilogx(
            freq,
            stage_db["h_src_db"],
            "k--",
            lw=1.0,
            alpha=0.7,
            label="E2E (source→pad out)",
        )
    ax.axvline(NYQUIST_HZ, color="r", ls=":", alpha=0.6, label="28 GHz")
    ax.set_xlim(AC_PLOT_FMIN_HZ, AC_PLOT_FMAX_HZ)
    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("Gain (dB)")
    ax.set_title(title)
    ax.grid(True, which="both", alpha=0.3)
    ax.legend(fontsize=7, loc="lower left")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_chain_tran_perstg(
    time_s: np.ndarray,
    stage_vod_mv: dict[str, np.ndarray],
    path: Path,
    *,
    title: str = "Chain PRBS — per-stage differential",
) -> None:
    """Overlay differential waveforms at each chain tap (zoomed post-settle)."""
    import matplotlib.pyplot as plt

    zoom = _tran_zoom_mask(time_s)
    t_ns = time_s[zoom] * 1e9
    fig, ax = plt.subplots(figsize=(10, 5))
    order = [
        ("term", "CTLE in (post-term)"),
        ("ctle", "VGA in (CTLE out)"),
        ("vga", "drv in (VGA out)"),
        ("driver", "pad out"),
    ]
    for key, label in order:
        if key not in stage_vod_mv:
            continue
        sig = stage_vod_mv[key][zoom]
        ax.plot(
            t_ns,
            sig,
            lw=1.0,
            color=CHAIN_STAGE_COLORS.get(key, CHAIN_STAGE_COLORS.get("ctle", "#1f77b4")),
            label=label,
        )
    ax.set_xlabel("Time (ns)")
    ax.set_ylabel("vod (mV)")
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8, loc="lower left")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_chain_sbr_perstg(
    time_s: np.ndarray,
    stages: dict[str, tuple[np.ndarray, np.ndarray, SbrResult]],
    path: Path,
    *,
    title: str = "Chain SBR — per-stage single-bit response",
) -> None:
    """Grid of SBR waveforms at each chain tap."""
    import matplotlib.pyplot as plt

    from .stim import SBR_SETTLE_UI

    names = [k for k in ("term", "ctle", "vga", "driver") if k in stages]
    if not names:
        return
    fig, axes = plt.subplots(len(names), 1, figsize=(10, 2.8 * len(names)), sharex=True)
    if len(names) == 1:
        axes = [axes]
    labels = {
        "term": "CTLE in (post-term)",
        "ctle": "VGA in (CTLE out)",
        "vga": "drv in (VGA out)",
        "driver": "pad out (outp−outn)",
    }
    for ax, name in zip(axes, names):
        v_outp, v_outn, sbr = stages[name]
        vod = v_outp - v_outn
        t_base_lo = SBR_BASELINE_UI_LO * UI_S
        t_base_hi = SBR_BASELINE_UI_HI * UI_S
        base_mask = (time_s >= t_base_lo) & (time_s <= t_base_hi)
        baseline = float(np.mean(vod[base_mask]))
        vod_ac = (vod - baseline) * 1e3
        t_cursor = sbr.t_cursor_s
        x_ui = (time_s - t_cursor) / UI_S
        x_lo, x_hi = -SBR_PRE - 2, SBR_POST + 4
        mask = (x_ui >= x_lo) & (x_ui <= x_hi)
        ax.plot(x_ui[mask], vod_ac[mask], color=CHAIN_STAGE_COLORS.get(name, "b"), lw=1.1)
        h0 = sbr.cursor_mV
        for k, h_mV, kept in sbr.taps:
            if not kept:
                continue
            color = "green" if k == 0 else ("purple" if k < 0 else "teal")
            ax.plot(k, h_mV, "o", color=color, ms=5 if k == 0 else 4, zorder=5)
        ax.axvline(0, color="k", ls="--", alpha=0.35)
        ax.set_ylabel("mV")
        ax.set_title(
            f"{labels.get(name, name)}  h₀={h0:.1f} mV  ISI={sbr.isi_norm:.3f}",
            fontsize=9,
        )
        ax.grid(True, alpha=0.3)
    axes[-1].set_xlabel("Time (UI relative to cursor)")
    fig.suptitle(title, fontsize=10, y=1.01)
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def plot_cmrr(freq: np.ndarray, cmrr_db: np.ndarray, path: Path) -> None:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.semilogx(freq, cmrr_db, "m-", lw=1.5)
    ax.axhline(CMRR_MIN_DB, color="k", ls=":", label="6 dB target")
    ax.set_ylabel("CMRR (dB)")
    ax.set_xlabel("Frequency (Hz)")
    ax.set_title("CMRR = Adm − Acm (dB)")
    ax.set_xlim(AC_PLOT_FMIN_HZ, AC_PLOT_FMAX_HZ)
    ax.grid(True, which="both", alpha=0.3)
    ax.legend(loc="lower left")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_psrr(freq: np.ndarray, psrr_db: np.ndarray, path: Path) -> None:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.semilogx(freq, psrr_db, "c-", lw=1.5)
    ax.axhline(PSRR_MIN_DB, color="k", ls=":", label="20 dB target")
    ax.axvline(NYQUIST_HZ, color="r", ls="--", alpha=0.7, label="28 GHz")
    ax.set_ylabel("PSRR (dB)")
    ax.set_xlabel("Frequency (Hz)")
    ax.set_title("PSRR = |vdd / vod| (VDD noise → differential out)")
    ax.set_xlim(AC_PLOT_FMIN_HZ, AC_PLOT_FMAX_HZ)
    ax.grid(True, which="both", alpha=0.3)
    ax.legend(loc="lower left")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
