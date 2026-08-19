"""Pi-model extraction from openEMS 2-port Touchstone (.s2p) inductor EM data."""

from __future__ import annotations

from pathlib import Path

import numpy as np

Z0_DEFAULT = 50.0


def read_touchstone_s2p(path: Path, z0: float = Z0_DEFAULT) -> tuple[np.ndarray, np.ndarray]:
    """Read RI-format 2-port .s2p → (freq_hz, S) with S shape (n, 2, 2)."""
    freqs: list[float] = []
    s_rows: list[np.ndarray] = []
    with Path(path).open() as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("!") or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 9:
                continue
            freq = float(parts[0])
            s11 = complex(float(parts[1]), float(parts[2]))
            s21 = complex(float(parts[3]), float(parts[4]))
            s12 = complex(float(parts[5]), float(parts[6]))
            s22 = complex(float(parts[7]), float(parts[8]))
            s_rows.append(np.array([[s11, s12], [s21, s22]], dtype=complex))
            freqs.append(freq)
    if not freqs:
        raise ValueError(f"No S-parameter data in {path}")
    return np.asarray(freqs, dtype=float), np.stack(s_rows, axis=0)


def y_from_s(s: np.ndarray, z0: float = Z0_DEFAULT) -> np.ndarray:
    """2×2 admittance matrix: Y = (1/Z0) inv(I+S)(I−S)."""
    i_mat = np.eye(2, dtype=complex)
    return (1.0 / z0) * np.linalg.inv(i_mat + s) @ (i_mat - s)


def pi_model_from_s2p(
    path: Path,
    z0: float = Z0_DEFAULT,
) -> dict[str, np.ndarray]:
    """Extract pi-model element arrays vs frequency from a Touchstone .s2p file."""
    freq, s = read_touchstone_s2p(path, z0=z0)
    n = len(freq)
    z_series = np.full(n, np.nan, dtype=complex)
    c_port1 = np.full(n, np.nan, dtype=float)
    c_port2 = np.full(n, np.nan, dtype=float)
    g_port1 = np.full(n, np.nan, dtype=float)
    g_port2 = np.full(n, np.nan, dtype=float)
    omega = 2.0 * np.pi * freq
    for i in range(n):
        y = y_from_s(s[i], z0=z0)
        y12 = y[0, 1]
        if abs(y12) > 1e-30:
            z_series[i] = -1.0 / y12
        y11p = y[0, 0] + y[0, 1]
        y22p = y[1, 1] + y[1, 0]
        if omega[i] > 0:
            c_port1[i] = np.imag(y11p) / omega[i]
            c_port2[i] = np.imag(y22p) / omega[i]
        g_port1[i] = np.real(y11p)
        g_port2[i] = np.real(y22p)
    l_series = np.where(freq > 0, np.imag(z_series) / omega, np.nan)
    r_series = np.real(z_series)
    return {
        "FREQ": freq,
        "L": l_series,
        "R_SERIES": r_series,
        "C_PORT1": c_port1,
        "C_PORT2": c_port2,
        "G_PORT1": g_port1,
        "G_PORT2": g_port2,
    }


def band_mean(
    freq: np.ndarray,
    values: np.ndarray,
    f_lo_hz: float,
    f_hi_hz: float,
) -> float:
    mask = (freq >= f_lo_hz) & (freq <= f_hi_hz) & np.isfinite(values)
    if not np.any(mask):
        return float("nan")
    return float(np.mean(values[mask]))


def pi_model_summary(
    pimodel: dict[str, np.ndarray],
    f_lo_hz: float = 5e9,
    f_hi_hz: float = 50e9,
) -> dict[str, float]:
    """Mean pi-model scalars over an in-band window."""
    freq = pimodel["FREQ"]
    c1 = band_mean(freq, pimodel["C_PORT1"], f_lo_hz, f_hi_hz)
    c2 = band_mean(freq, pimodel["C_PORT2"], f_lo_hz, f_hi_hz)
    g1 = band_mean(freq, pimodel["G_PORT1"], f_lo_hz, f_hi_hz)
    g2 = band_mean(freq, pimodel["G_PORT2"], f_lo_hz, f_hi_hz)
    g_port = float(np.mean([g1, g2])) if np.isfinite(g1) and np.isfinite(g2) else float("nan")
    r_sub = 1.0 / g_port if g_port > 1e-12 else float("inf")
    return {
        "L_series_nH": band_mean(freq, pimodel["L"], f_lo_hz, f_hi_hz) * 1e9,
        "R_series_ohm": band_mean(freq, pimodel["R_SERIES"], f_lo_hz, f_hi_hz),
        "C_port1_fF": c1 * 1e15,
        "C_port2_fF": c2 * 1e15,
        "C_port_fF": (
            float(np.mean([c1, c2]) * 1e15)
            if np.isfinite(c1) and np.isfinite(c2)
            else float("nan")
        ),
        "G_port_mS": g_port * 1e3,
        "R_sub_ohm": r_sub,
    }
