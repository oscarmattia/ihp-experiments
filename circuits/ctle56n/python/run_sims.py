#!/usr/bin/env python3
"""Run ngspice DC/AC for 56G CML CTLE; parse results and plot.

Assumes the caller has sourced ~/.local/share/ihp-eda/env.sh (PDK_ROOT, ngspice).
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import shutil
import sys
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[3]
_EXP = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from size_ctle import (  # noqa: E402
    CtleParams,
    DC_GAIN_TARGET_DB,
    print_summary,
    read_params_inc,
    size_ctle,
    write_params_inc,
)

from ctlelib import (  # noqa: E402
    DC_GAIN_MAX_DB,
    DC_GAIN_MIN_DB,
    PEAK_MAX_DB,
    PEAK_MIN_DB,
    PRBS9_BITS,
    PRBS9_POLY,
    PSRR_MAX_DB,
    SBR_KEEP_FRAC,
    SBR_POST,
    SBR_PRE,
    SBR_SETTLE_UI,
    SimMetrics,
    SbrResult,
    EyeMetrics,
    compute_ac_peak_metrics,
    compute_eye_metrics,
    extract_sbr,
    eye_metrics_rows,
    group_delay_s,
    interp_db_at,
    parse_ac_raw,
    parse_dc_log,
    parse_psrr_raw,
    parse_tran_raw,
    pass_out,
    pdk_models,
    plot_ac,
    plot_cmrr,
    plot_eye_diff,
    plot_eye_se,
    plot_psrr,
    plot_sbr,
    plot_tran_diff,
    plot_tran_se,
    prepare_tb,
    run_ngspice,
    sbr_tap_label,
    targets_ok,
    verify_eye_pair_width_agreement,
    verify_eye_phase_invariance,
    write_ac_diff_csv,
    write_eye_csvs,
    write_pass_metrics,
    write_prbs_stim,
    write_sbr_stim,
    write_sbr_taps_csv,
    write_tran_csv,
)

NYQUIST_HZ = 28e9
CMRR_MIN_DB = 6.0
PSRR_MIN_DB = 20.0

CTLE_DUT_NAME = "ctle_dut"
_PARAM_INC_RE = re.compile(r"^\.param\s+(\w+)=([\S]+)", re.MULTILINE)


def _param_from_inc(inc_path: Path, name: str, default: float = 0.0) -> float:
    if not inc_path.is_file():
        return default
    for key, val in _PARAM_INC_RE.findall(inc_path.read_text()):
        if key == name:
            return float(val)
    return default


def _ind_shunt_r_dc_ohm(spice_dir: Path) -> float:
    inc = spice_dir / "ind_shunt.inc"
    if not inc.is_file():
        return 0.0
    for line in inc.read_text().splitlines():
        if "R_dc=" in line:
            m = re.search(r"R_dc=([\d.]+)", line)
            if m:
                return float(m.group(1))
    return 0.0


def measure_rd_load_ohm(
    dc_vals: dict[str, float],
    ic_a: float,
    r_coil_ohm: float = 0.0,
) -> float:
    """Realized shunt load R from DC OP (subtract coil DC drop on PDK pass)."""
    vdd = dc_vals.get("v(vdd)", 0.0)
    vout = dc_vals.get("v(outp)", 0.0)
    if ic_a <= 0 or math.isnan(ic_a):
        return float("nan")
    return (vdd - vout - ic_a * r_coil_ohm) / ic_a


def _copy_params_inc(spice_dir: Path, work: Path) -> None:
    src = spice_dir / "params.inc"
    if src.is_file():
        shutil.copy(src, work / "params.inc")


def run_pass(
    pass_name: str,
    dut_rel: str,
    spice_dir: Path,
    extra_params: dict[str, str] | None = None,
) -> SimMetrics:
    models = pdk_models()
    dut_cir = spice_dir / dut_rel
    pout = pass_out(pass_name)
    pout.mkdir(parents=True, exist_ok=True)
    work = pout / "work"
    work.mkdir(parents=True, exist_ok=True)

    _copy_params_inc(spice_dir, work)

    tb_kw = dict(dut_name=CTLE_DUT_NAME)

    tb_dc = prepare_tb(
        spice_dir / "tb_dc.cir", dut_cir, work, models, spice_dir,
        extra_params=extra_params, **tb_kw,
    )
    dc_log = run_ngspice(tb_dc, work, "dc.log")
    dc_vals = parse_dc_log(dc_log)

    tb_diff = prepare_tb(
        spice_dir / "tb_ac_diff.cir", dut_cir, work, models, spice_dir,
        extra_params=extra_params, **tb_kw,
    )
    run_ngspice(tb_diff, work, "ac_diff.log")
    freq, voutp, voutn, vin_p, vin_n = parse_ac_raw(work / "ac_diff.raw")
    vod = voutp - voutn
    vid = vin_p - vin_n
    h_diff = np.where(np.abs(vid) > 1e-30, vod / vid, 0.0)
    h_db = 20.0 * np.log10(np.abs(h_diff))
    dc_gain_db = float(h_db[0])
    peaking_db = interp_db_at(freq, h_db, NYQUIST_HZ) - dc_gain_db
    peak_gain_db, f_peak_hz, f_3db_hz, f3db_at_fmax = compute_ac_peak_metrics(freq, h_db)

    tb_cm = prepare_tb(
        spice_dir / "tb_ac_cm.cir", dut_cir, work, models, spice_dir,
        extra_params=extra_params, **tb_kw,
    )
    run_ngspice(tb_cm, work, "ac_cm.log")
    freq_cm, voutp_cm, voutn_cm, vin_p_cm, vin_n_cm = parse_ac_raw(work / "ac_cm.raw")
    voc_cm = (voutp_cm + voutn_cm) / 2.0
    vic_cm = (vin_p_cm + vin_n_cm) / 2.0
    h_cm = np.where(np.abs(vic_cm) > 1e-30, voc_cm / vic_cm, 0.0)
    acm_db = float(20.0 * np.log10(max(np.abs(h_cm[0]), 1e-30)))
    cmrr_db = dc_gain_db - acm_db

    tb_psrr = prepare_tb(
        spice_dir / "tb_ac_psrr.cir", dut_cir, work, models, spice_dir,
        extra_params=extra_params, **tb_kw,
    )
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

    v_e1 = dc_vals.get("v(xu1.e1)", 0.0)
    v_mgate = dc_vals.get("v(xu1.mgate)", 0.85)
    v_c1 = dc_vals.get("v(outp)", 0.0)
    vce = v_c1 - v_e1
    ic_a = dc_vals.get("@q.xu1.xq1.qnpn13g2[ic]", float("nan"))
    id_tail1 = dc_vals.get("@n.xu1.xtail1.nsg13_lv_nmos[ids]", float("nan"))
    id_tail2 = dc_vals.get("@n.xu1.xtail2.nsg13_lv_nmos[ids]", float("nan"))
    id_tail = id_tail1
    if not math.isnan(id_tail2):
        id_tail = id_tail1 if math.isnan(id_tail1) else (id_tail1 + id_tail2) / 2.0

    params_inc = work / "params.inc"
    cl_f = _param_from_inc(params_inc, "CL")
    if pass_name == "pdk":
        l_load_h = _param_from_inc(params_inc, "L_EM")
        r_coil_ohm = _ind_shunt_r_dc_ohm(spice_dir)
    else:
        l_load_h = _param_from_inc(params_inc, "LLOAD")
        r_coil_ohm = 0.0
    rd_realized = measure_rd_load_ohm(dc_vals, ic_a, r_coil_ohm)
    if rd_realized > 0 and cl_f > 0 and l_load_h > 0:
        m_realized = l_load_h / (rd_realized ** 2 * cl_f)
    else:
        m_realized = float("nan")

    op_path = pout / "op.txt"
    op_path.write_text(dc_log.read_text())

    gd_s = group_delay_s(freq, h_diff)
    plot_ac(
        freq,
        h_db,
        gd_s,
        pout / "ac_diff.png",
        peak_gain_db=peak_gain_db,
        f_peak_hz=f_peak_hz,
        f_3db_hz=f_3db_hz,
        f3db_at_fmax=f3db_at_fmax,
        dc_gain_db=dc_gain_db,
        peaking_db=peaking_db,
    )
    write_ac_diff_csv(pout / "ac_diff.csv", freq, h_db, gd_s)
    cmrr_curve = 20.0 * np.log10(np.maximum(np.abs(h_diff), 1e-30)) - (
        20.0 * np.log10(np.maximum(np.abs(h_cm), 1e-30))
    )
    plot_cmrr(freq_cm, cmrr_curve, pout / "cmrr.png")
    plot_psrr(freq_p, psrr_db_curve, pout / "psrr.png")

    return SimMetrics(
        pass_name=pass_name,
        dc_gain_db=dc_gain_db,
        peaking_db=peaking_db,
        cmrr_db=cmrr_db,
        psrr_db=psrr_db,
        vce_v=vce,
        vds_tail_v=v_e1,
        vgs_tail_v=v_mgate,
        ic_a=ic_a,
        id_tail_a=id_tail,
        peak_gain_db=peak_gain_db,
        f_peak_hz=f_peak_hz,
        f_3db_hz=f_3db_hz,
        rd_realized_ohm=rd_realized,
        m_realized=m_realized,
    )


def _validate_eye_metrics(
    pass_name: str,
    time_s: np.ndarray,
    v_outp: np.ndarray,
    v_outn: np.ndarray,
    eye: EyeMetrics,
) -> list[list[str]]:
    """Run phase-invariance and sanity checks; return summary rows."""
    phase_ok, phase_summary, _, _ = verify_eye_phase_invariance(
        time_s, v_outp, v_outn,
    )
    status = "PASS" if phase_ok else "FAIL"
    print(f"  [{pass_name}] eye phase-invariance: {phase_summary} ({status})")
    if not phase_ok:
        raise ValueError(
            f"{pass_name} eye metrics are not phase-invariant: {phase_summary}"
        )
    return [
        [f"{pass_name}_eye_phase_invariance_ok", "yes"],
        [f"{pass_name}_eye_phase_invariance", phase_summary],
    ]


def run_tran(
    pass_name: str,
    dut_rel: str,
    spice_dir: Path,
    vbase: float,
    extra_params: dict[str, str] | None = None,
) -> tuple[EyeMetrics, list[list[str]]]:
    """Run 56G NRZ PRBS9 transient and plot waveforms + eye diagrams."""
    models = pdk_models()
    dut_cir = spice_dir / dut_rel
    pout = pass_out(pass_name)
    pout.mkdir(parents=True, exist_ok=True)
    work = pout / "work"
    work.mkdir(parents=True, exist_ok=True)

    _copy_params_inc(spice_dir, work)

    tmax = write_prbs_stim(work / "prbs_stim.inc", vbase)
    tb_tran = prepare_tb(
        spice_dir / "tb_tran.cir",
        dut_cir,
        work,
        models,
        spice_dir,
        extra_params={**(extra_params or {}), "TMAX": f"{tmax:.6e}"},
        dut_name=CTLE_DUT_NAME,
    )
    run_ngspice(tb_tran, work, "tran.log")
    time_s, v_outp, v_outn, v_inp, v_inn = parse_tran_raw(work / "tran.raw")

    write_tran_csv(pout / "tran.csv", time_s, v_outp, v_outn, v_inp, v_inn)
    write_eye_csvs(pout, time_s, v_outp, v_outn)
    plot_tran_se(time_s, v_outp, v_outn, v_inp, v_inn, pout / "tran_se.png")
    plot_tran_diff(time_s, v_outp, v_outn, v_inp, v_inn, pout / "tran_diff.png")
    plot_eye_se(time_s, v_outp, v_outn, pout / "eye_se.png")
    plot_eye_diff(time_s, v_outp, v_outn, pout / "eye_diff.png")
    eye = compute_eye_metrics(time_s, v_outp, v_outn)
    extra = _validate_eye_metrics(pass_name, time_s, v_outp, v_outn, eye)
    return eye, extra


def run_sbr(
    pass_name: str,
    dut_rel: str,
    spice_dir: Path,
    vbase: float,
    extra_params: dict[str, str] | None = None,
) -> SbrResult:
    """Run single-bit response transient and extract pulse-response taps."""
    models = pdk_models()
    dut_cir = spice_dir / dut_rel
    pout = pass_out(pass_name)
    pout.mkdir(parents=True, exist_ok=True)
    work = pout / "work"
    work.mkdir(parents=True, exist_ok=True)

    _copy_params_inc(spice_dir, work)

    tmax = write_sbr_stim(work / "sbr_stim.inc", vbase)
    tb_sbr = prepare_tb(
        spice_dir / "tb_sbr.cir",
        dut_cir,
        work,
        models,
        spice_dir,
        extra_params={**(extra_params or {}), "TMAX": f"{tmax:.6e}"},
        dut_name=CTLE_DUT_NAME,
    )
    run_ngspice(tb_sbr, work, "sbr.log")
    time_s, v_outp, v_outn, v_inp, v_inn = parse_tran_raw(work / "sbr.raw")
    sbr = extract_sbr(time_s, v_outp, v_outn)

    write_tran_csv(pout / "sbr.csv", time_s, v_outp, v_outn, v_inp, v_inn)
    write_sbr_taps_csv(pout / "sbr_taps.csv", sbr)
    plot_sbr(time_s, v_outp, v_outn, v_inp, v_inn, sbr, pout / "sbr.png")

    return sbr


def iterate_sizing(spice_dir: Path, max_iter: int = 8) -> CtleParams:
    scales = [1.0, 0.95, 1.05, 0.9, 1.1, 0.85, 1.15, 1.2]
    peaking_opts = [6.0, 6.5, 7.0, 5.5, 7.5, 8.0, 5.0, 4.5]
    best_params: CtleParams | None = None
    best_score = float("inf")

    out_dir = pass_out("ideal")
    out_dir.mkdir(parents=True, exist_ok=True)

    for i in range(max_iter):
        scale = scales[i % len(scales)]
        peak = peaking_opts[i % len(peaking_opts)]
        params = size_ctle(scale=scale, peaking_db=peak)
        write_params_inc(params, spice_dir / "params.inc")
        print_summary(params)

        try:
            metrics = run_pass("ideal", "ctle_ideal.cir", spice_dir)
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
        else:
            score += 0.25 * (metrics.dc_gain_db - DC_GAIN_TARGET_DB) ** 2
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

        if targets_ok(metrics):
            print("  targets met (ideal pass)")
            return params

    if best_params is None:
        raise RuntimeError("All simulation iterations failed")
    print(f"  using best iteration (score={best_score:.3f})")
    return best_params


def _summary_sbr_rows(prefix: str, sbr: SbrResult) -> list[list[str]]:
    rows = [
        [f"{prefix}_sbr_cursor_mV", f"{sbr.cursor_mV:.4f}"],
        [f"{prefix}_sbr_isi_norm", f"{sbr.isi_norm:.6g}"],
        [f"{prefix}_sbr_isi_abs", f"{sbr.isi_abs:.6g}"],
    ]
    for k in range(-SBR_PRE, SBR_POST + 1):
        entry = next((t for t in sbr.taps if t[0] == k), None)
        if entry is None:
            continue
        _, h_mV, kept = entry
        val = f"{h_mV:.4f}" if kept else ""
        rows.append([f"{prefix}_sbr_h{k}_mV", val])
    return rows


def write_summary(
    path: Path,
    params: CtleParams,
    ideal: SimMetrics,
    pdk: SimMetrics | None,
    sbr_ideal: SbrResult | None = None,
    sbr_pdk: SbrResult | None = None,
    eye_ideal: EyeMetrics | None = None,
    eye_pdk: EyeMetrics | None = None,
    extra_rows: list[list[str]] | None = None,
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
        ["RPPD_R_ohm", f"{params.rppd_r_ohm:.6g}"],
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
        ["ideal_RD_realized_ohm", f"{ideal.rd_realized_ohm:.4f}"],
        ["ideal_m", f"{ideal.m_realized:.4f}"],
    ]
    if sbr_ideal:
        rows += _summary_sbr_rows("sbr", sbr_ideal)
    if eye_ideal:
        rows += eye_metrics_rows("ideal", eye_ideal)
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
            ["pdk_RD_realized_ohm", f"{pdk.rd_realized_ohm:.4f}"],
            ["pdk_m", f"{pdk.m_realized:.4f}"],
            ["pdk_targets_ok", targets_ok(pdk)],
        ]
    if sbr_pdk:
        rows += _summary_sbr_rows("pdk_sbr", sbr_pdk)
    if eye_pdk:
        rows += eye_metrics_rows("pdk", eye_pdk)
    if extra_rows:
        rows += extra_rows
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


def _sbr_section_body(title: str, sbr: SbrResult, out_subdir: str) -> str:
    sbr_rows: list[str] = []
    h0 = sbr.cursor_mV
    for k, h_mV, kept in sbr.taps:
        label = sbr_tap_label(k)
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
    return f"""
### {title}

Waveforms: `out/{out_subdir}/sbr.png`, `out/{out_subdir}/sbr.csv`, `out/{out_subdir}/sbr_taps.csv`.

Isolated **1 UI** NRZ pulse (**100 mVpp,diff**, ±50 mV vid), after **{SBR_SETTLE_UI} UI** settle at logic 0.
Sample **{SBR_PRE} pre-cursors + cursor + {SBR_POST} post-cursors** every UI; drop taps with
|h| < **{SBR_KEEP_FRAC * 100:.2g}%** of |cursor| (h_0 always kept).

| Tap | k | h (mV) | h / h_0 | Kept |
| --- | --- | --- | --- | --- |
{chr(10).join(sbr_rows)}

- Main cursor h_0 = **{sbr.cursor_mV:.2f} mV** at t = **{sbr.t_cursor_ui:.3f} UI** after pulse start
- Normalized total ISI = Σ h_k / h_0 = **{sbr.isi_norm:.4f}** (k≠0, kept taps only)
- Σ|h_k|/|h_0| = **{sbr.isi_abs:.4f}** (same taps)
- Taps with |h| < {SBR_KEEP_FRAC * 100:.2g}% of |cursor| are omitted from the ISI sums.
"""


def write_ctle_report(
    path: Path,
    params: CtleParams,
    ideal: SimMetrics,
    pdk: SimMetrics | None,
    sbr_ideal: SbrResult | None = None,
    sbr_pdk: SbrResult | None = None,
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
        row("Tail current", "I_tail", _fmt_a(params.itail_a), _fmt_a(params.itail_a), "Ic per tail (×2 devices)"),
        row("Transition frequency", "f_T", _fmt_hz(params.ft_hz), _fmt_hz(params.ft_hz), "LUT at bias"),
        row("Transconductance", "g_m", f"{params.gm * 1e3:.2f} mS", f"{params.gm * 1e3:.2f} mS", ""),
        row("Input capacitance", "C_in", f"{params.cin_f * 1e15:.2f} fF", f"{params.cin_f * 1e15:.2f} fF", "HBT CIN"),
        row(
            "Load capacitance",
            "C_L",
            f"{params.cl_f * 1e15:.2f} fF",
            f"{params.cl_f * 1e15:.2f} fF",
            "Miller + route (no coil port C)",
        ),
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
Load C_L = Miller-aware FO1 VGA input + interconnect (not raw LUT CIN; coil port C excluded).
Bessel shunt-peaking **m = L/(R_D² C_L) ≈ {m_bessel:.2f}**.

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

Plots and waveforms: `out/ideal/` (ideal passives) and `out/pdk/` (PDK R/C passives).
Each pass includes AC PNGs/CSVs, transient CSVs, eye PNGs/CSVs, and SBR when `--no-tran` is not set.
Combined metrics: `out/summary.csv`; per-pass: `out/ideal/metrics.csv`, `out/pdk/metrics.csv`.
"""
    if sbr_ideal or sbr_pdk:
        body += "\n## Single-bit response\n"
        if sbr_ideal:
            body += _sbr_section_body("Ideal", sbr_ideal, "ideal")
        if sbr_pdk:
            body += _sbr_section_body("PDK", sbr_pdk, "pdk")
    path.write_text(body)


def _read_pass_metrics(path: Path) -> list[tuple[str, str]]:
    if not path.is_file():
        return []
    rows: list[tuple[str, str]] = []
    with path.open(newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) >= 2:
                rows.append((row[0], row[1]))
    return rows


def aggregate_front_end_summary(exp: Path) -> None:
    """Append term/VGA pass metrics to out/summary.csv after stage runs."""
    summary_path = exp / "out" / "summary.csv"
    if not summary_path.is_file():
        print(f"No CTLE summary at {summary_path}; skipping front-end aggregate")
        return

    existing: list[list[str]] = []
    with summary_path.open(newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header:
            existing.append(header)
        for row in reader:
            existing.append(row)

    index = {row[0]: i for i, row in enumerate(existing[1:], start=1) if row}
    extra_passes = [
        ("term", exp / "out" / "term" / "metrics.csv"),
        ("vga_ideal", exp / "out" / "vga_ideal" / "metrics.csv"),
        ("vga_pdk", exp / "out" / "vga_pdk" / "metrics.csv"),
    ]
    for prefix, mpath in extra_passes:
        for key, val in _read_pass_metrics(mpath):
            out_key = key if key.startswith(f"{prefix}_") else f"{prefix}_{key}"
            if out_key in index:
                existing[index[out_key]] = [out_key, val]
            else:
                existing.append([out_key, val])
                index[out_key] = len(existing) - 1

    with summary_path.open("w", newline="") as f:
        csv.writer(f).writerows(existing)
    print(f"Updated front-end summary: {summary_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-iterate", action="store_true", help="Skip iteration loop")
    parser.add_argument("--no-pdk", action="store_true", help="Skip PDK passive pass")
    parser.add_argument("--no-tran", action="store_true", help="Skip 56G NRZ PRBS9 and SBR transient")
    parser.add_argument("--force-size", action="store_true", help="Re-run sizer")
    parser.add_argument(
        "--aggregate-summary",
        action="store_true",
        help="Merge term/VGA metrics.csv into out/summary.csv and exit",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="RD/Rs scale (only with --force-size or iterate loop)",
    )
    parser.add_argument(
        "--peaking-db",
        type=float,
        default=7.0,
        help="Degeneration peaking target dB (only with --force-size or iterate loop)",
    )
    args = parser.parse_args()

    if args.aggregate_summary:
        aggregate_front_end_summary(_EXP)
        return

    spice_dir = _EXP / "spice"
    params_inc = spice_dir / "params.inc"
    out_dir = _EXP / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.no_iterate:
        # Use committed spice/params.inc from size_ctle.py (see run.sh); do not re-size here.
        if args.force_size or not params_inc.is_file():
            params = size_ctle(scale=args.scale, peaking_db=args.peaking_db)
            write_params_inc(params, params_inc)
            print_summary(params)
        else:
            params = read_params_inc(params_inc)
    else:
        params = iterate_sizing(spice_dir)
        write_params_inc(params, params_inc)

    ideal = run_pass("ideal", "ctle_ideal.cir", spice_dir)
    write_pass_metrics(pass_out("ideal") / "metrics.csv", ideal)
    print(
        f"Ideal: DC={ideal.dc_gain_db:.2f} dB peak={ideal.peaking_db:.2f} dB "
        f"CMRR={ideal.cmrr_db:.2f} dB PSRR={ideal.psrr_db:.2f} dB"
    )

    sbr_ideal: SbrResult | None = None
    sbr_pdk: SbrResult | None = None
    eye_ideal: EyeMetrics | None = None
    eye_pdk: EyeMetrics | None = None
    eye_extra_rows: list[list[str]] = []
    vbase: float | None = None
    if not args.no_tran:
        vbase_m = re.search(
            r"\.param\s+VBASE=([\S]+)",
            (spice_dir / "params.inc").read_text(),
        )
        if vbase_m:
            vbase = float(vbase_m.group(1))
            print("Running ideal transient (56G NRZ PRBS9)...")
            eye_ideal, extra_ideal = run_tran("ideal", "ctle_ideal.cir", spice_dir, vbase)
            eye_extra_rows.extend(extra_ideal)
            print("Running ideal single-bit response (1 UI pulse)...")
            sbr_ideal = run_sbr("ideal", "ctle_ideal.cir", spice_dir, vbase)
            write_pass_metrics(
                pass_out("ideal") / "metrics.csv", ideal, sbr_ideal, eye_ideal
            )

    pdk_metrics = None
    pdk_extra: dict[str, str] | None = None
    if not args.no_pdk:
        pdk_extra = {
            "IND_SHUNT_INC": str((spice_dir / "ind_shunt.inc").resolve()),
        }
    if not args.no_pdk:
        try:
            from size_ind import generate_ind_shunt  # noqa: E402

            ind_inc = spice_dir / "ind_shunt.inc"
            l_target = float(
                re.search(
                    r"\.param\s+LLOAD=([\S]+)",
                    (spice_dir / "params.inc").read_text(),
                ).group(1)
            )
            generate_ind_shunt(l_target, ind_inc)
            pdk_extra["IND_SHUNT_INC"] = str(ind_inc.resolve())
            pdk_metrics = run_pass(
                "pdk", "ctle_pdk.cir", spice_dir, extra_params=pdk_extra
            )
            write_pass_metrics(pass_out("pdk") / "metrics.csv", pdk_metrics)
            print(
                f"PDK: DC={pdk_metrics.dc_gain_db:.2f} dB peak={pdk_metrics.peaking_db:.2f} dB "
                f"CMRR={pdk_metrics.cmrr_db:.2f} dB PSRR={pdk_metrics.psrr_db:.2f} dB"
            )
            if not args.no_tran and vbase is not None:
                print("Running PDK transient (56G NRZ PRBS9)...")
                eye_pdk, extra_pdk = run_tran(
                    "pdk", "ctle_pdk.cir", spice_dir, vbase, extra_params=pdk_extra
                )
                eye_extra_rows.extend(extra_pdk)
                print("Running PDK single-bit response (1 UI pulse)...")
                sbr_pdk = run_sbr(
                    "pdk", "ctle_pdk.cir", spice_dir, vbase, extra_params=pdk_extra
                )
                write_pass_metrics(
                    pass_out("pdk") / "metrics.csv", pdk_metrics, sbr_pdk, eye_pdk
                )
        except RuntimeError as exc:
            print(f"PDK pass failed: {exc}")

    if eye_ideal is not None and eye_pdk is not None:
        pair_ok, pair_summary = verify_eye_pair_width_agreement(
            eye_ideal, eye_pdk, "ideal", "pdk",
        )
        print(f"  CTLE ideal/pdk eye width agreement: {pair_summary} ({'PASS' if pair_ok else 'FAIL'})")
        if not pair_ok:
            raise ValueError(f"CTLE ideal/pdk eye widths disagree: {pair_summary}")
        eye_extra_rows += [
            ["ctle_eye_width_agreement_ok", "yes"],
            ["ctle_eye_width_agreement", pair_summary],
        ]

    write_summary(
        out_dir / "summary.csv",
        params,
        ideal,
        pdk_metrics,
        sbr_ideal,
        sbr_pdk,
        eye_ideal,
        eye_pdk,
        extra_rows=eye_extra_rows or None,
    )
    write_ctle_report(
        _EXP / "ctle_report.md", params, ideal, pdk_metrics, sbr_ideal, sbr_pdk
    )
    print(f"Wrote {out_dir / 'summary.csv'}")
    print(f"Wrote {_EXP / 'ctle_report.md'}")


if __name__ == "__main__":
    main()
