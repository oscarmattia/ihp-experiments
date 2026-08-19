"""Pi-model extraction and S-parameter helpers for inductor EM data."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from char.common.lut import load_lut, save_lut

Z0_DEFAULT = 50.0

# ngspice ``sp`` verification grid (matches committed SP_FREQ in LUT .npz).
SP_FSTART_HZ = 2.5e8
SP_FSTOP_HZ = 100e9
SP_NFREQ = 401

# Gate thresholds for lumped-model vs EM S-parameter agreement.  Reference fit for
# turn1_d40 is ~0.6% mean |S21| (1-50 GHz) and ~0.75% (50-100 GHz); 2.5% / 3.5%
# flags a real regression while tolerating fit noise and coarse-grid cases.
SP_VERIFY_S21_MEAN_REL_FIT = 0.025
SP_VERIFY_S21_MEAN_REL_EXTRAP = 0.035
SP_VERIFY_S21_PHASE_MEAN_DEG_FIT = 1.0
SP_VERIFY_S21_PHASE_MAX_DEG_FIT = 2.5
SP_VERIFY_S21_PHASE_MEAN_DEG_EXTRAP = 1.5
SP_VERIFY_S21_PHASE_MAX_DEG_EXTRAP = 3.5

SP_LUT_KEYS = (
    "SP_FREQ",
    "S11_RE",
    "S11_IM",
    "S21_RE",
    "S21_IM",
    "S22_RE",
    "S22_IM",
)


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


def sparam_grid_hz(
    f_start_hz: float = SP_FSTART_HZ,
    f_stop_hz: float = SP_FSTOP_HZ,
    n_freq: int = SP_NFREQ,
) -> np.ndarray:
    """Linear frequency grid for committed S-parameter LUTs and ngspice ``sp`` runs."""
    return np.linspace(f_start_hz, f_stop_hz, n_freq, dtype=float)


def sparam_arrays_from_s2p(
    path: Path,
    *,
    freq_hz: np.ndarray | None = None,
    z0: float = Z0_DEFAULT,
) -> dict[str, np.ndarray]:
    """Read a Touchstone .s2p and return downsampled/interpolated S-parameter arrays."""
    src_freq, s = read_touchstone_s2p(path, z0=z0)
    grid = np.asarray(freq_hz if freq_hz is not None else sparam_grid_hz(), dtype=float)
    s11 = _interp_complex(src_freq, s[:, 0, 0], grid)
    s21 = _interp_complex(src_freq, s[:, 1, 0], grid)
    s22 = _interp_complex(src_freq, s[:, 1, 1], grid)
    return {
        "SP_FREQ": grid,
        "S11_RE": s11.real.astype(float),
        "S11_IM": s11.imag.astype(float),
        "S21_RE": s21.real.astype(float),
        "S21_IM": s21.imag.astype(float),
        "S22_RE": s22.real.astype(float),
        "S22_IM": s22.imag.astype(float),
    }


def sparam_arrays_to_complex(arrays: dict[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (freq, s11, s21, s22) complex arrays from LUT SP_* keys."""
    freq = np.asarray(arrays["SP_FREQ"], dtype=float)
    s11 = arrays["S11_RE"] + 1j * arrays["S11_IM"]
    s21 = arrays["S21_RE"] + 1j * arrays["S21_IM"]
    s22 = arrays["S22_RE"] + 1j * arrays["S22_IM"]
    return freq, s11, s21, s22


def _interp_complex(
    src_freq: np.ndarray,
    src_val: np.ndarray,
    dst_freq: np.ndarray,
) -> np.ndarray:
    re = np.interp(dst_freq, src_freq, src_val.real, left=np.nan, right=np.nan)
    im = np.interp(dst_freq, src_freq, src_val.imag, left=np.nan, right=np.nan)
    return re + 1j * im


def parse_sp_wrdata(path: Path) -> dict[str, np.ndarray]:
    """Parse ngspice ``sp`` wrdata with complex scale (9 columns).

    Layout: ``freq, freq, freq_imag, re(S11), im(S11), re(S21), im(S21),
    re(S22), im(S22)`` — see MEMORY.md.
    """
    rows: list[list[float]] = []
    with Path(path).open() as f:
        for line in f:
            parts = line.split()
            if parts:
                rows.append([float(x) for x in parts])
    data = np.asarray(rows, dtype=float)
    if data.shape[1] < 9:
        raise RuntimeError(f"{path}: expected >=9 columns for sp wrdata, got {data.shape[1]}")
    return {
        "FREQ": data[:, 0],
        "S11_RE": data[:, 3],
        "S11_IM": data[:, 4],
        "S21_RE": data[:, 5],
        "S21_IM": data[:, 6],
        "S22_RE": data[:, 7],
        "S22_IM": data[:, 8],
    }


def _resolve_s2p_path(meta: dict[str, Any], out_dir: Path) -> Path | None:
    run_log = meta.get("run_log") or {}
    if isinstance(run_log, dict):
        for key in ("s2p", "from_s2p"):
            if run_log.get(key):
                p = Path(run_log[key])
                if not p.is_absolute():
                    p = out_dir.parent / p
                if p.is_file():
                    return p
    if meta.get("pimodel_s2p"):
        p = Path(meta["pimodel_s2p"])
        if p.is_file():
            return p
    if meta.get("sparam_s2p"):
        p = Path(meta["sparam_s2p"])
        if p.is_file():
            return p
    return None


def load_em_sparams(
    npz_path: Path,
    *,
    out_dir: Path | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, str]:
    """Load EM S-parameters: committed SP_* arrays first, else raw .s2p.

    Returns (freq, s11, s21, s22, source_tag).
    """
    arrays, meta = load_lut(npz_path)
    out_dir = out_dir or npz_path.parent
    if all(k in arrays for k in SP_LUT_KEYS):
        freq, s11, s21, s22 = sparam_arrays_to_complex(arrays)
        return freq, s11, s21, s22, "npz"

    s2p = _resolve_s2p_path(meta, out_dir)
    if s2p is not None:
        sp_arrays = sparam_arrays_from_s2p(s2p)
        freq, s11, s21, s22 = sparam_arrays_to_complex(sp_arrays)
        return freq, s11, s21, s22, f"s2p:{s2p.name}"

    raise FileNotFoundError(
        f"No committed SP_* arrays or .s2p for {npz_path.name}; run ihp_ind_em.py --refresh-sparams"
    )


def merge_sparams_into_lut(
    out_path: Path,
    s2p_path: Path | None,
    *,
    freq_hz: np.ndarray | None = None,
) -> bool:
    """Attach downsampled S-parameter arrays from .s2p into an existing .npz."""
    if s2p_path is None or not Path(s2p_path).is_file():
        return False
    arrays, meta = load_lut(out_path)
    sp_arrays = sparam_arrays_from_s2p(Path(s2p_path), freq_hz=freq_hz)
    arrays.update(sp_arrays)
    axes = meta.get("axes") or {}
    axes.update({
        "SP_FREQ": "S-parameter verification frequency (Hz)",
        "S11_RE": "EM S11 real part",
        "S11_IM": "EM S11 imaginary part",
        "S21_RE": "EM S21 real part",
        "S21_IM": "EM S21 imaginary part",
        "S22_RE": "EM S22 real part",
        "S22_IM": "EM S22 imaginary part",
    })
    meta["axes"] = axes
    meta["sparam_s2p"] = str(s2p_path)
    meta["sparam_nfreq"] = int(len(sp_arrays["SP_FREQ"]))
    save_lut(out_path, arrays, meta)
    (out_path.parent / f"{out_path.stem}.meta.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n"
    )
    return True


def refresh_sparam_luts(out_dir: Path) -> int:
    """Re-extract SP_* arrays from on-disk .s2p paths referenced in LUT meta."""
    updated = 0
    for npz_path in sorted(out_dir.glob("sg13_ind_*.npz")):
        _, meta = load_lut(npz_path)
        s2p = _resolve_s2p_path(meta, out_dir)
        if merge_sparams_into_lut(npz_path, s2p):
            print(f"  sparam merged into {npz_path.name} from {s2p.name}", flush=True)
            updated += 1
    return updated


@dataclass
class SpBandMetrics:
    band: str
    f_lo_hz: float
    f_hi_hz: float
    n_points: int
    s21_mag_mean_rel_pct: float
    s21_mag_max_rel_pct: float
    s21_phase_mean_abs_deg: float
    s21_phase_max_abs_deg: float
    s11_mag_mean_rel_pct: float
    s11_mag_max_rel_pct: float


@dataclass
class SpVerifyResult:
    case: str
    source: str
    fit: SpBandMetrics
    extrap: SpBandMetrics | None
    passed: bool
    failures: list[str]


def _band_metrics(
    freq: np.ndarray,
    s_model: np.ndarray,
    s_em: np.ndarray,
    f_lo_hz: float,
    f_hi_hz: float,
    band: str,
) -> SpBandMetrics | None:
    mask = (
        (freq >= f_lo_hz)
        & (freq <= f_hi_hz)
        & np.isfinite(s_model)
        & np.isfinite(s_em)
    )
    if not np.any(mask):
        return None
    sm = s_model[mask]
    se = s_em[mask]
    mag_m = np.abs(sm)
    mag_e = np.abs(se)
    mag_denom = np.maximum(mag_e, 1e-12)
    mag_rel = 100.0 * np.abs(mag_m - mag_e) / mag_denom
    phase_err = np.degrees(np.angle(sm * np.conj(se)))
    return SpBandMetrics(
        band=band,
        f_lo_hz=f_lo_hz,
        f_hi_hz=f_hi_hz,
        n_points=int(np.sum(mask)),
        s21_mag_mean_rel_pct=float(np.mean(mag_rel)),
        s21_mag_max_rel_pct=float(np.max(mag_rel)),
        s21_phase_mean_abs_deg=float(np.mean(np.abs(phase_err))),
        s21_phase_max_abs_deg=float(np.max(np.abs(phase_err))),
        s11_mag_mean_rel_pct=float("nan"),
        s11_mag_max_rel_pct=float("nan"),
    )


def compare_sparams(
    freq: np.ndarray,
    s11_model: np.ndarray,
    s21_model: np.ndarray,
    s11_em: np.ndarray,
    s21_em: np.ndarray,
    *,
    case: str = "",
    source: str = "",
    fstop_hz: float = SP_FSTOP_HZ,
) -> SpVerifyResult:
    """Compare model vs EM S-parameters over fit (1-50 GHz) and extrap bands."""
    fit_s21 = _band_metrics(freq, s21_model, s21_em, 1e9, 50e9, "fit")
    extrap_s21 = None
    if fstop_hz > 50e9:
        extrap_s21 = _band_metrics(freq, s21_model, s21_em, 50e9, min(100e9, fstop_hz), "extrap")

    # |S11| is small (0.004-0.24) and carries a known de-embedding offset below ~15 GHz;
    # report relative error but do not gate on it.
    for band_obj, s11_m, s11_e, f_lo, f_hi in (
        (fit_s21, s11_model, s11_em, 1e9, 50e9),
        (extrap_s21, s11_model, s11_em, 50e9, min(100e9, fstop_hz)),
    ):
        if band_obj is None:
            continue
        mask = (freq >= f_lo) & (freq <= f_hi) & np.isfinite(s11_m) & np.isfinite(s11_e)
        if not np.any(mask):
            continue
        mag_m = np.abs(s11_m[mask])
        mag_e = np.abs(s11_e[mask])
        mag_rel = 100.0 * np.abs(mag_m - mag_e) / np.maximum(mag_e, 1e-12)
        band_obj.s11_mag_mean_rel_pct = float(np.mean(mag_rel))
        band_obj.s11_mag_max_rel_pct = float(np.max(mag_rel))

    failures: list[str] = []
    if fit_s21 is None:
        failures.append("no_fit_band_overlap")
    else:
        if fit_s21.s21_mag_mean_rel_pct > 100.0 * SP_VERIFY_S21_MEAN_REL_FIT:
            failures.append(
                f"|S21| mean rel {fit_s21.s21_mag_mean_rel_pct:.2f}% > "
                f"{100*SP_VERIFY_S21_MEAN_REL_FIT:.1f}% (fit)"
            )
        if fit_s21.s21_phase_mean_abs_deg > SP_VERIFY_S21_PHASE_MEAN_DEG_FIT:
            failures.append(
                f"ang(S21) mean {fit_s21.s21_phase_mean_abs_deg:.2f} deg > "
                f"{SP_VERIFY_S21_PHASE_MEAN_DEG_FIT:.1f} deg (fit)"
            )
        if fit_s21.s21_phase_max_abs_deg > SP_VERIFY_S21_PHASE_MAX_DEG_FIT:
            failures.append(
                f"ang(S21) max {fit_s21.s21_phase_max_abs_deg:.2f} deg > "
                f"{SP_VERIFY_S21_PHASE_MAX_DEG_FIT:.1f} deg (fit)"
            )

    if extrap_s21 is not None:
        if extrap_s21.s21_mag_mean_rel_pct > 100.0 * SP_VERIFY_S21_MEAN_REL_EXTRAP:
            failures.append(
                f"|S21| mean rel {extrap_s21.s21_mag_mean_rel_pct:.2f}% > "
                f"{100*SP_VERIFY_S21_MEAN_REL_EXTRAP:.1f}% (extrap)"
            )
        if extrap_s21.s21_phase_mean_abs_deg > SP_VERIFY_S21_PHASE_MEAN_DEG_EXTRAP:
            failures.append(
                f"ang(S21) mean {extrap_s21.s21_phase_mean_abs_deg:.2f} deg > "
                f"{SP_VERIFY_S21_PHASE_MEAN_DEG_EXTRAP:.1f} deg (extrap)"
            )
        if extrap_s21.s21_phase_max_abs_deg > SP_VERIFY_S21_PHASE_MAX_DEG_EXTRAP:
            failures.append(
                f"ang(S21) max {extrap_s21.s21_phase_max_abs_deg:.2f} deg > "
                f"{SP_VERIFY_S21_PHASE_MAX_DEG_EXTRAP:.1f} deg (extrap)"
            )

    return SpVerifyResult(
        case=case,
        source=source,
        fit=fit_s21 or SpBandMetrics("fit", 1e9, 50e9, 0, float("nan"), float("nan"), float("nan"), float("nan"), float("nan"), float("nan")),
        extrap=extrap_s21,
        passed=len(failures) == 0,
        failures=failures,
    )


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
