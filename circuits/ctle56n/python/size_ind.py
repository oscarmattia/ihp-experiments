#!/usr/bin/env python3
"""EM-based shunt inductor model for CTLE/VGA PDK netlists.

Selects the nearest usable openEMS 1-turn octagon case, derives series R from
TopMetal2 sheet resistance plus offset-corrected EM pi-model R(f), port shunt
capacitance from committed 2-port pi-model extraction, and emits
``spice/ind_shunt.inc`` with documented provenance.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from char.common.lut import load_lut  # noqa: E402
from char.passive.ind_pimodel import (  # noqa: E402
    SP_FSTART_HZ,
    SP_FSTOP_HZ,
    SP_NFREQ,
    SpVerifyResult,
    compare_sparams,
    load_em_sparams,
    parse_sp_wrdata,
)
from char.passive.ind_validate import validate_ind_lut  # noqa: E402

# TopMetal2 from openEMS SG13G2.xml: sigma=30.3 MS/m, thickness=3 µm [model]
TM2_CONDUCTIVITY_S_PER_M = 30.3e6
TM2_THICKNESS_M = 3.0e-6
TM2_RSH_OHM_PER_SQ = 1.0 / (TM2_CONDUCTIVITY_S_PER_M * TM2_THICKNESS_M)

# Magic ihp-sg13g2-extract.tech typ: defaultareacap allm7/metal7 (aF/µm²) [model]
TM2_AREACAP_AF_UM2 = 3.233

# EM de-embedding makes Re{Z_series} ~0.4 Ohm too low below ~15 GHz; add when fitting skin.
EM_R_OFFSET_OHM = 0.4

EXCLUDED_CASES = frozenset({"turn2"})
CASE_ORDER = ("turn1_d40", "turn1_d60", "turn1_d80", "turn1", "l2n0")

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "spice" / "ind_shunt.inc"


@dataclass
class IndShuntModel:
    case: str
    d_um: float
    w_um: float
    s_um: float
    nr_r: int
    l_target_h: float
    l_band_h: float
    l_mismatch_pct: float
    loop_length_um: float
    metal_area_um2: float
    r_dc_ohm: float
    r_28g_ohm: float
    r_em_28g_raw_ohm: float
    r_offset_ohm: float
    r_skin_r1_ohm: float
    r_skin_r2_ohm: float
    l_skin_h: float
    cox_p_f: float
    cox_n_f: float
    c_plate_bound_f: float
    c_rule_thumb_f: float
    em_completed: bool
    gds: str
    solver: str


def _ind_npz_paths() -> dict[str, Path]:
    out = _REPO / "char/passive/out"
    paths: dict[str, Path] = {}
    for p in sorted(out.glob("sg13_ind_*.npz")):
        case = p.stem.replace("sg13_ind_", "")
        paths[case] = p
    return paths


def _read_summary_valid(case: str) -> tuple[bool, str]:
    summary = _REPO / "char/passive/out/ind_summary.csv"
    if not summary.is_file():
        return True, ""
    with summary.open() as f:
        for row in csv.DictReader(f):
            if row.get("case") == case:
                valid = str(row.get("valid", "")).lower() in ("true", "1", "yes")
                reason = row.get("invalid_reason") or ""
                return valid, reason
    return True, ""


def _usable_case(
    case: str,
    arrays: dict[str, np.ndarray],
    meta: dict,
) -> bool:
    if case in EXCLUDED_CASES:
        return False
    if not meta.get("em_completed", False):
        return False
    freq = np.asarray(arrays["FREQ"], dtype=float)
    l_src = np.asarray(
        arrays["L_PI"] if "L_PI" in arrays else arrays["L"], dtype=float
    )
    q_series = np.asarray(arrays["Q"], dtype=float)
    band = (freq >= 1e9) & (freq <= 50e9)
    if not np.any(band) or float(np.mean(l_src[band])) <= 0:
        return False
    valid, reason = validate_ind_lut(freq, arrays["L"], q_series, em_completed=True)
    if valid:
        return True
    if reason == "Q_peak_nonpositive":
        return True
    csv_valid, csv_reason = _read_summary_valid(case)
    if not csv_valid and csv_reason == "Q_peak_nonpositive":
        return True
    return False


def in_band_l_h(freq: np.ndarray, l_series: np.ndarray) -> float:
    band = (freq >= 1e9) & (freq <= 50e9) & np.isfinite(l_series)
    if not np.any(band):
        band = np.isfinite(l_series) & (freq > 0)
    return float(np.mean(l_series[band]))


def octagon_segment_length_um(d_um: float, w_um: float) -> float:
    return (d_um - w_um) / (1.0 + math.sqrt(2.0))


def octagon_loop_length_um(d_um: float, w_um: float) -> float:
    """Drawn TopMetal2 loop length (8 segments + width extensions per quadrant)."""
    seg = octagon_segment_length_um(d_um, w_um)
    return 8.0 * seg + 4.0 * w_um


def physical_r_dc_ohm(d_um: float, w_um: float) -> tuple[float, float]:
    """Return (R_dc, loop_length_um) from openEMS TopMetal2 sheet R."""
    length_um = octagon_loop_length_um(d_um, w_um)
    r_dc = TM2_RSH_OHM_PER_SQ * (length_um * 1e-6) / (w_um * 1e-6)
    return r_dc, length_um


def plate_area_cox_bound_f(w_um: float, loop_length_um: float) -> float:
    """Lower bound: coil strip area × TopMetal2 areacap (aF/µm² → F)."""
    area_um2 = w_um * loop_length_um
    return area_um2 * TM2_AREACAP_AF_UM2 * 1e-18


def rule_of_thumb_cox_f(l_h: float) -> float:
    """Designer rule: 5–10 fF per 100 pH of series L (mid-band used)."""
    l_ph = l_h * 1e12
    ff_per_100ph = 7.5  # mid of 5–10 band
    return (ff_per_100ph * 1e-15) * (l_ph / 100.0)


def fit_skin_branches(
    r_dc_ohm: float,
    r_hf_ohm: float,
    f_transition_hz: float = 15e9,
) -> tuple[float, float, float]:
    """Parallel R1 || (R2 + jωL2) between series nodes; Re{Z} rises toward R1."""
    r1 = max(r_hf_ohm, r_dc_ohm * 1.05)
    r2 = r1 * r_dc_ohm / (r1 - r_dc_ohm)
    l_skin = r2 / (2.0 * math.pi * f_transition_hz)
    return r1, r2, l_skin


def offset_corrected_r_series(
    freq: np.ndarray,
    r_series: np.ndarray,
    offset_ohm: float = EM_R_OFFSET_OHM,
) -> np.ndarray:
    """Add de-embedding offset; EM Re{Z12} is ~0.4 Ohm too low below ~15 GHz."""
    return r_series + offset_ohm


def pimodel_parasitics(
    arrays: dict[str, np.ndarray],
    meta: dict,
    l_band_h: float,
    loop_length_um: float,
    w_um: float,
) -> tuple[float, float, float, float]:
    """Return Cox per terminal, plate bound, rule-of-thumb C from LUT pi-model."""
    pimodel = meta.get("pimodel") or {}
    if "C_PORT1" in arrays:
        freq = np.asarray(arrays["FREQ"], dtype=float)
        band = (freq >= 5e9) & (freq <= 50e9)
        c1 = float(np.mean(arrays["C_PORT1"][band]))
        c2 = float(np.mean(arrays["C_PORT2"][band]))
        cox = float(np.mean([c1, c2]))
    elif pimodel.get("C_port_fF") is not None:
        cox = float(pimodel["C_port_fF"]) * 1e-15
    else:
        raise RuntimeError("LUT missing pi-model port capacitance — run ihp_ind_em --refresh-pimodel")

    c_plate = plate_area_cox_bound_f(w_um, loop_length_um)
    c_rule = rule_of_thumb_cox_f(l_band_h)
    return cox, cox, c_plate, c_rule


def pick_em_case(l_target_h: float) -> tuple[str, dict[str, np.ndarray], dict]:
    paths = _ind_npz_paths()
    best: tuple[float, str, dict[str, np.ndarray], dict] | None = None
    for case in CASE_ORDER:
        path = paths.get(case)
        if path is None:
            continue
        arrays, meta = load_lut(path)
        if not _usable_case(case, arrays, meta):
            continue
        l_arr = arrays["L_PI"] if "L_PI" in arrays else arrays["L"]
        l_band = in_band_l_h(arrays["FREQ"], l_arr)
        err = abs(l_band - l_target_h)
        if best is None or err < best[0]:
            best = (err, case, arrays, meta)
    if best is None:
        raise RuntimeError("No usable EM inductor LUT case found")
    _, case, arrays, meta = best
    return case, arrays, meta


def build_ind_shunt_model_for_case(case: str) -> IndShuntModel:
    """Build lumped model from a specific EM LUT case (self-consistent L target)."""
    paths = _ind_npz_paths()
    path = paths.get(case)
    if path is None:
        raise RuntimeError(f"Unknown or missing LUT case: {case}")
    arrays, meta = load_lut(path)
    if not _usable_case(case, arrays, meta):
        raise RuntimeError(f"LUT case {case} is not usable for lumped-model extraction")
    l_arr = arrays["L_PI"] if "L_PI" in arrays else arrays["L"]
    l_target = in_band_l_h(arrays["FREQ"], l_arr)
    return _build_ind_shunt_model_from_lut(case, arrays, meta, l_target)


def build_ind_shunt_model(l_target_h: float) -> IndShuntModel:
    case, arrays, meta = pick_em_case(l_target_h)
    return _build_ind_shunt_model_from_lut(case, arrays, meta, l_target_h)


def _build_ind_shunt_model_from_lut(
    case: str,
    arrays: dict[str, np.ndarray],
    meta: dict,
    l_target_h: float,
) -> IndShuntModel:
    freq = np.asarray(arrays["FREQ"], dtype=float)
    l_pi = np.asarray(arrays["L_PI"] if "L_PI" in arrays else arrays["L"], dtype=float)
    r_series = np.asarray(arrays["R_SERIES"], dtype=float) if "R_SERIES" in arrays else None

    d_um = float(meta.get("d", 0.0))
    w_um = float(meta.get("w", 4.0))
    s_um = float(meta.get("s", 2.1))
    nr_r = int(meta.get("nr_r", 1))

    l_band = in_band_l_h(freq, l_pi)
    r_dc, loop_len = physical_r_dc_ohm(d_um, w_um)

    if r_series is None:
        raise RuntimeError(f"LUT case {case} missing R_SERIES pi-model array")

    r_corr = offset_corrected_r_series(freq, r_series)
    idx28 = int(np.argmin(np.abs(freq - 28e9)))
    r_em_28_raw = float(r_series[idx28])
    r_hf = float(r_corr[idx28])
    r1, r2, l_skin = fit_skin_branches(r_dc, r_hf)

    cox_p, cox_n, c_plate, c_rule = pimodel_parasitics(
        arrays, meta, l_band, loop_len, w_um
    )

    mismatch = 100.0 * (l_band - l_target_h) / l_target_h if l_target_h > 0 else 0.0

    return IndShuntModel(
        case=case,
        d_um=d_um,
        w_um=w_um,
        s_um=s_um,
        nr_r=nr_r,
        l_target_h=l_target_h,
        l_band_h=l_band,
        l_mismatch_pct=mismatch,
        loop_length_um=loop_len,
        metal_area_um2=w_um * loop_len,
        r_dc_ohm=r_dc,
        r_28g_ohm=r_hf,
        r_em_28g_raw_ohm=r_em_28_raw,
        r_offset_ohm=EM_R_OFFSET_OHM,
        r_skin_r1_ohm=r1,
        r_skin_r2_ohm=r2,
        l_skin_h=l_skin,
        cox_p_f=cox_p,
        cox_n_f=cox_n,
        c_plate_bound_f=c_plate,
        c_rule_thumb_f=c_rule,
        em_completed=bool(meta.get("em_completed", False)),
        gds=str(meta.get("gds", "")),
        solver=str(meta.get("solver", "")),
    )


def _header_lines(m: IndShuntModel) -> list[str]:
    skin_ratio = m.r_28g_ohm / m.r_dc_ohm if m.r_dc_ohm > 0 else float("nan")
    return [
        "* ind_shunt — generated by size_ind.py (EM lumped TopMetal2 1-turn octagon)",
        f"* EM case: {m.case}  nr_r={m.nr_r}  D={m.d_um:g}um  w={m.w_um:g}um  s={m.s_um:g}um",
        f"* em_completed={m.em_completed}  solver={m.solver}  gds={m.gds}",
        f"* L target={m.l_target_h*1e12:.3f} pH  EM pi-model L(1-50GHz)={m.l_band_h*1e12:.3f} pH"
        f"  mismatch={m.l_mismatch_pct:+.1f}%",
        "* Series L from 2-port pi-model imag(Z12)/omega averaged 1-50 GHz.",
        (
            f"* R_dc={m.r_dc_ohm:.4f} Ohm from openEMS sigma={TM2_CONDUCTIVITY_S_PER_M:.3g} S/m"
            f" t={TM2_THICKNESS_M*1e6:.0f}um -> Rsh={TM2_RSH_OHM_PER_SQ:.4f} Ohm/sq"
        ),
        f"*   loop length={m.loop_length_um:.2f} um (8*seg+4*w octagon) / w={m.w_um:g} um",
        (
            f"* Skin ladder: R1={m.r_skin_r1_ohm:.4f} Ohm || (R2={m.r_skin_r2_ohm:.4f} Ohm"
            f" + Ls={m.l_skin_h*1e12:.3f} pH); offset-corrected EM R@28GHz"
            f" raw={m.r_em_28g_raw_ohm:.4f} + {m.r_offset_ohm:.3f} -> {m.r_28g_ohm:.4f} Ohm"
            f" ({skin_ratio:.2f}x R_dc)"
        ),
        (
            f"* Port shunt Cox={m.cox_p_f*1e15:.3f} fF/terminal (capacitance-only branch p/n->sub)"
        ),
        (
            "*   Lossless dielectric in ITF/openEMS stack (ER=4.1, no conductivity);"
            " G_port in LUT is substrate coupling, not modeled here."
        ),
        (
            "*   Future substrate coupling: coupled inductors + parallel R"
            " (substrate current loops), not a lumped port resistor."
        ),
        (
            f"*   plate-area lower bound={m.c_plate_bound_f*1e15:.2f} fF"
            f" ({TM2_AREACAP_AF_UM2} aF/um^2); rule-of-thumb ~{m.c_rule_thumb_f*1e15:.1f} fF"
            f" (5-10 fF per 100 pH)"
        ),
        (
            "*   Inter-terminal mutual C omitted (< Cox; not separable from"
            " series branch in 2-port pi decomposition)"
        ),
    ]


def write_ind_shunt_inc(model: IndShuntModel, path: Path) -> Path:
    path = Path(path)
    lines = _header_lines(model)
    lines += [
        ".subckt ind_shunt p n sub",
        f"Lp p ns1 {model.l_band_h:.12g}",
        f"R1 ns1 n {model.r_skin_r1_ohm:.12g}",
        f"R2 ns1 nmid {model.r_skin_r2_ohm:.12g}",
        f"Lsk nmid n {model.l_skin_h:.12g}",
        f"Coxp p sub {model.cox_p_f:.12g}",
        f"Coxn n sub {model.cox_n_f:.12g}",
        ".ends ind_shunt",
        "",
    ]
    path.write_text("\n".join(lines))
    return path


def generate_ind_shunt(l_target_h: float, out_path: Path | None = None) -> IndShuntModel:
    out = Path(out_path or DEFAULT_OUT)
    model = build_ind_shunt_model(l_target_h)
    write_ind_shunt_inc(model, out)
    return model


def verify_ind_shunt_ngspice(
    inc_path: Path,
    f_start_hz: float = 100e6,
    f_stop_hz: float = 300e9,
) -> dict[str, float]:
    """AC one-port series Z of ind_shunt: 1 V source, 1 nOhm load, L = imag(Z)/omega."""
    pdk = os.environ.get("PDK_ROOT")
    if not pdk:
        raise RuntimeError("PDK_ROOT not set")
    inc_path = Path(inc_path).resolve()
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        spiceinit = Path(pdk) / "ihp-sg13g2/libs.tech/ngspice/.spiceinit"
        if spiceinit.is_file():
            (tdir / ".spiceinit").write_bytes(spiceinit.read_bytes())
        cir = tdir / "ind_z.cir"
        cir.write_text(
            f".include '{inc_path}'\n"
            "V1 inp 0 dc 0 ac 1\n"
            "X1 inp out 0 ind_shunt\n"
            "Rload out 0 1e-9\n"
            f".ac dec 50 {f_start_hz:.6g} {f_stop_hz:.6g}\n"
            ".control\n"
            "run\n"
            "set wr_singlescale\n"
            "let zre = real(-v(inp)/i(v1))\n"
            "let zim = imag(-v(inp)/i(v1))\n"
            "wrdata ind_z.dat frequency zre zim\n"
            ".endc\n"
            ".end\n"
        )
        log = tdir / "ind_z.log"
        subprocess.run(
            ["ngspice", "-b", "-o", str(log), str(cir)],
            cwd=tdir,
            check=False,
            capture_output=True,
        )
        data_path = tdir / "ind_z.dat"
        if not data_path.is_file():
            raise RuntimeError(f"ngspice inductor verify failed: {log.read_text()[:500]}")
        rows = []
        with data_path.open() as f:
            for line in f:
                parts = line.split()
                if parts:
                    rows.append([float(x) for x in parts])
        data = np.asarray(rows)
        if data.shape[1] >= 5:
            freq = data[:, 0]
            zre = data[:, 3]
            zim = data[:, 4]
        else:
            freq = data[:, 0]
            zre = data[:, 1]
            zim = data[:, 2]
        omega = 2.0 * math.pi * freq
        l_h = zim / omega
        q = np.where(np.abs(zre) > 1e-18, np.abs(zim) / np.abs(zre), 0.0)

        def at_f(target: float) -> tuple[float, float, float]:
            idx = int(np.argmin(np.abs(freq - target)))
            return float(freq[idx]), float(l_h[idx]), float(q[idx])

        band = (freq >= 1e9) & (freq <= 50e9)
        l_band = float(np.mean(l_h[band]))
        f1, l1, q1 = at_f(1e9)
        f10, l10, q10 = at_f(10e9)
        f28, l28, q28 = at_f(28e9)
        f50, l50, q50 = at_f(50e9)
        neg_l = freq[(freq > 1e9) & (l_h <= 0)]
        srf_ghz = float(neg_l[0] / 1e9) if len(neg_l) else float("nan")
        return {
            "l_band_ph": l_band * 1e12,
            "l_1g_ph": l1 * 1e12,
            "l_10g_ph": l10 * 1e12,
            "l_28g_ph": l28 * 1e12,
            "l_50g_ph": l50 * 1e12,
            "f_28g_hz": f28,
            "q_28g": q28,
            "srf_ghz": srf_ghz,
            "q_min_band": float(np.min(q[band])),
            "q_max_band": float(np.max(q[band])),
        }


def _ngspice_workdir(inc_path: Path) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
    pdk = os.environ.get("PDK_ROOT")
    if not pdk:
        raise RuntimeError("PDK_ROOT not set")
    td = tempfile.TemporaryDirectory()
    tdir = Path(td.name)
    spiceinit = Path(pdk) / "ihp-sg13g2/libs.tech/ngspice/.spiceinit"
    if spiceinit.is_file():
        (tdir / ".spiceinit").write_bytes(spiceinit.read_bytes())
    return tdir, td


def run_ind_shunt_sp(
    inc_path: Path,
    *,
    f_start_hz: float = SP_FSTART_HZ,
    f_stop_hz: float = SP_FSTOP_HZ,
    n_freq: int = SP_NFREQ,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Run ngspice 2-port ``sp`` analysis on ``ind_shunt``; return freq, S11, S21, S22."""
    inc_path = Path(inc_path).resolve()
    tdir, td = _ngspice_workdir(inc_path)
    try:
        cir = tdir / "ind_sp.cir"
        cir.write_text(
            f".include '{inc_path}'\n"
            "V1 p 0 dc 0 ac 1 portnum 1 z0 50\n"
            "V2 n 0 dc 0 ac 1 portnum 2 z0 50\n"
            "Xl p n 0 ind_shunt\n"
            ".control\n"
            f"sp lin {n_freq} {f_start_hz:.6g} {f_stop_hz:.6g}\n"
            "set wr_singlescale\n"
            "wrdata sp_model.raw frequency real(S_1_1) imag(S_1_1)"
            " real(S_2_1) imag(S_2_1) real(S_2_2) imag(S_2_2)\n"
            ".endc\n"
            ".end\n"
        )
        log = tdir / "ind_sp.log"
        subprocess.run(
            ["ngspice", "-b", "-o", str(log), str(cir)],
            cwd=tdir,
            check=False,
            capture_output=True,
        )
        raw = tdir / "sp_model.raw"
        if not raw.is_file():
            raise RuntimeError(f"ngspice sp verify failed: {log.read_text()[:800]}")
        parsed = parse_sp_wrdata(raw)
        freq = parsed["FREQ"]
        s11 = parsed["S11_RE"] + 1j * parsed["S11_IM"]
        s21 = parsed["S21_RE"] + 1j * parsed["S21_IM"]
        s22 = parsed["S22_RE"] + 1j * parsed["S22_IM"]
        return freq, s11, s21, s22
    finally:
        td.cleanup()


def verify_ind_shunt_sp(
    inc_path: Path,
    npz_path: Path,
    *,
    case: str = "",
    fstop_hz: float = SP_FSTOP_HZ,
) -> SpVerifyResult:
    """Compare ngspice ``sp`` of lumped model against committed / fallback EM S-parameters."""
    em_freq, s11_em, s21_em, _, source = load_em_sparams(npz_path)
    freq_m, s11_m, s21_m, _ = run_ind_shunt_sp(
        inc_path,
        f_stop_hz=min(fstop_hz, SP_FSTOP_HZ),
    )
    s11_mi = np.zeros_like(em_freq, dtype=complex)
    s21_mi = np.zeros_like(em_freq, dtype=complex)
    for i, f in enumerate(em_freq):
        j = int(np.argmin(np.abs(freq_m - f)))
        s11_mi[i] = s11_m[j]
        s21_mi[i] = s21_m[j]
    return compare_sparams(
        em_freq,
        s11_mi,
        s21_mi,
        s11_em,
        s21_em,
        case=case or npz_path.stem.replace("sg13_ind_", ""),
        source=source,
        fstop_hz=fstop_hz,
    )


def _format_sp_result(res: SpVerifyResult) -> str:
    lines = [
        f"  SP verify case={res.case} source={res.source} pass={res.passed}",
        (
            f"    fit 1-50 GHz: |S21| mean/max rel="
            f"{res.fit.s21_mag_mean_rel_pct:.2f}%/{res.fit.s21_mag_max_rel_pct:.2f}%"
            f"  phase mean/max={res.fit.s21_phase_mean_abs_deg:.2f}/"
            f"{res.fit.s21_phase_max_abs_deg:.2f} deg"
            f"  |S11| mean/max rel={res.fit.s11_mag_mean_rel_pct:.2f}%/"
            f"{res.fit.s11_mag_max_rel_pct:.2f}% (report only)"
        ),
    ]
    if res.extrap is not None:
        lines.append(
            f"    extrap 50-100 GHz: |S21| mean/max rel="
            f"{res.extrap.s21_mag_mean_rel_pct:.2f}%/{res.extrap.s21_mag_max_rel_pct:.2f}%"
            f"  phase mean/max={res.extrap.s21_phase_mean_abs_deg:.2f}/"
            f"{res.extrap.s21_phase_max_abs_deg:.2f} deg"
        )
    if res.failures:
        lines.append(f"    FAIL: {'; '.join(res.failures)}")
    return "\n".join(lines)


def verify_all_em_cases(
    *,
    out_dir: Path | None = None,
    artifact_dir: Path | None = None,
) -> list[SpVerifyResult]:
    """Run SP verification for every usable EM case (skips turn2)."""
    out_dir = out_dir or (_REPO / "char/passive/out")
    results: list[SpVerifyResult] = []
    skipped: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        for case in CASE_ORDER:
            if case in EXCLUDED_CASES:
                skipped.append(f"{case} (excluded)")
                continue
            npz_path = out_dir / f"sg13_ind_{case}.npz"
            if not npz_path.is_file():
                skipped.append(f"{case} (missing npz)")
                continue
            arrays, meta = load_lut(npz_path)
            if not _usable_case(case, arrays, meta):
                skipped.append(f"{case} (invalid LUT)")
                continue
            if "R_SERIES" not in arrays:
                skipped.append(f"{case} (no pi-model)")
                continue
            try:
                model = build_ind_shunt_model_for_case(case)
            except RuntimeError as exc:
                skipped.append(f"{case} ({exc})")
                continue
            inc = work / f"ind_shunt_{case}.inc"
            write_ind_shunt_inc(model, inc)
            try:
                fstop = float(meta.get("fstop_hz", SP_FSTOP_HZ))
                res = verify_ind_shunt_sp(inc, npz_path, case=case, fstop_hz=fstop)
            except FileNotFoundError as exc:
                skipped.append(f"{case} ({exc})")
                continue
            results.append(res)
            print(_format_sp_result(res), flush=True)

    if skipped:
        print("  skipped:", ", ".join(skipped), flush=True)

    if artifact_dir is not None:
        from char.passive.summarize_ind import write_sp_validation_artifacts

        write_sp_validation_artifacts(out_dir, artifact_dir, results)

    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate EM-based ind_shunt.inc for CTLE/VGA PDK netlists"
    )
    parser.add_argument(
        "--l-target",
        type=float,
        default=25.3e-12,
        help="Target inductance (H); default ~25 pH CTLE shunt L from size_ctle",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="Output include path (default: spice/ind_shunt.inc)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Alias for --verify-sp (ngspice S-parameter check vs EM)",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip ngspice verification",
    )
    parser.add_argument(
        "--verify-sp",
        action="store_true",
        help="Run ngspice sp verification vs EM Touchstone (default unless --no-verify)",
    )
    parser.add_argument(
        "--verify-lq",
        action="store_true",
        help="Also run legacy AC L/Q one-port check",
    )
    parser.add_argument(
        "--verify-all-cases",
        action="store_true",
        help="Run SP verification for every usable EM LUT case; write artifacts to char/passive/out/",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=_REPO / "char/passive/out",
        help="Directory for SP validation CSV/plot (with --verify-all-cases)",
    )
    args = parser.parse_args()

    if args.verify_all_cases:
        results = verify_all_em_cases(artifact_dir=args.artifact_dir)
        if not results:
            raise SystemExit("No cases verified (missing SP data or pi-model?)")
        if any(not r.passed for r in results):
            raise SystemExit(1)
        return

    if not os.environ.get("PDK_ROOT"):
        print("Warning: PDK_ROOT not set — verification will fail.")

    model = generate_ind_shunt(args.l_target, args.out)
    print(
        f"Wrote {args.out}  case={model.case}  L_band={model.l_band_h*1e12:.2f} pH"
        f"  mismatch={model.l_mismatch_pct:+.1f}%"
    )
    print(
        f"  R_dc={model.r_dc_ohm:.4f} Ohm  R@28G={model.r_28g_ohm:.4f} Ohm"
        f"  Cox={model.cox_p_f*1e15:.2f} fF/term"
    )

    do_sp = args.verify_sp or args.verify or not args.no_verify
    if do_sp:
        npz_path = _REPO / "char/passive/out" / f"sg13_ind_{model.case}.npz"
        try:
            res = verify_ind_shunt_sp(args.out, npz_path, case=model.case)
            print(_format_sp_result(res))
            if not res.passed:
                raise SystemExit(1)
        except Exception as exc:
            print(f"  ngspice SP verify failed: {exc}")
            if args.verify or args.verify_sp:
                raise

    if args.verify_lq:
        try:
            v = verify_ind_shunt_ngspice(args.out)
            print(
                f"  ngspice L/Q verify: L(1G)={v['l_1g_ph']:.2f} pH"
                f" L(10G)={v['l_10g_ph']:.2f} pH"
                f" L(28G)={v['l_28g_ph']:.2f} pH"
                f" L(50G)={v['l_50g_ph']:.2f} pH"
                f" Q@28G={v['q_28g']:.1f}"
            )
        except Exception as exc:
            print(f"  ngspice L/Q verify failed: {exc}")
            raise


if __name__ == "__main__":
    main()
