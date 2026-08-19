"""Validation helpers for IHP inductor EM LUTs."""

from __future__ import annotations

import numpy as np


def l_low_freq(freq: np.ndarray, l: np.ndarray) -> float:
    """Inductance at the lowest simulated non-DC frequency."""
    mask = np.isfinite(freq) & np.isfinite(l) & (freq > 0)
    if not np.any(mask):
        return np.nan
    f_valid = freq[mask]
    l_valid = l[mask]
    idx = int(np.argmin(f_valid))
    return float(l_valid[idx])


def l_at_freq(freq: np.ndarray, l: np.ndarray, target_hz: float) -> float:
    mask = np.isfinite(freq) & np.isfinite(l)
    if not np.any(mask):
        return np.nan
    f_valid = freq[mask]
    l_valid = l[mask]
    idx = int(np.searchsorted(f_valid, target_hz))
    idx = min(idx, len(f_valid) - 1)
    return float(l_valid[idx])


def peak_q(freq: np.ndarray, q: np.ndarray) -> tuple[float, float]:
    mask = np.isfinite(q) & np.isfinite(freq)
    if not np.any(mask):
        return np.nan, np.nan
    f_valid = freq[mask]
    q_valid = q[mask]
    qi = int(np.nanargmax(q_valid))
    return float(f_valid[qi]), float(q_valid[qi])


def srf_ghz(freq: np.ndarray, l: np.ndarray) -> float:
    """First frequency (GHz) where differential L crosses zero above 1 GHz."""
    mask = np.isfinite(freq) & np.isfinite(l) & (freq > 1e9)
    if not np.any(mask):
        return np.nan
    f_valid = freq[mask]
    l_valid = l[mask]
    neg = np.where(l_valid <= 0)[0]
    if len(neg) == 0:
        return np.nan
    return float(f_valid[int(neg[0])] / 1e9)


def validate_ind_lut(
    freq: np.ndarray,
    l_series: np.ndarray,
    q_series: np.ndarray,
    *,
    em_completed: bool = True,
) -> tuple[bool, str | None]:
    """Return (valid, invalid_reason). Flags unphysical EM results."""
    if not em_completed:
        return False, "em_not_completed"

    reasons: list[str] = []

    mask = np.isfinite(freq) & np.isfinite(l_series) & (freq > 0)
    finite_l = l_series[mask]
    if finite_l.size == 0 or not np.all(np.isfinite(finite_l)):
        reasons.append("L_not_finite")

    l_low = l_low_freq(freq, l_series)
    if not np.isfinite(l_low) or l_low <= 0:
        reasons.append("L_low_freq_nonpositive")

    _, q_peak = peak_q(freq, q_series)
    if not np.isfinite(q_peak) or q_peak <= 0:
        reasons.append("Q_peak_nonpositive")

    if reasons:
        return False, ";".join(reasons)
    return True, None
