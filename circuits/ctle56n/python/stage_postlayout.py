#!/usr/bin/env python3
"""Run ngspice DC/AC on an external CTLE DUT netlist (post-layout or other view).

The DUT must present the same ``ctle_dut`` port list as ``spice/ctle_pdk.cir``
(``outp outn inp inn vdd vss mgate``) because bias current lives in the testbench.
This runner takes the netlist as a path so KLayout- and Magic-extracted views can
be exercised without importing anything from ``layout/``.

Assumes the caller has sourced ~/.local/share/ihp-eda/env.sh.
"""

from __future__ import annotations

import argparse
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

from ctlelib import (  # noqa: E402
    PSRR_MAX_DB,
    EyeMetrics,
    SbrResult,
    SimMetrics,
    compute_ac_peak_metrics,
    compute_eye_metrics,
    extract_sbr,
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
CTLE_DUT_NAME = "ctle_dut"
_PARAM_INC_RE = re.compile(r"^\.param\s+(\w+)=([\S]+)", re.MULTILINE)


def _spice_dir() -> Path:
    return _EXP / "spice"


def _param_from_inc(inc_path: Path, name: str, default: float = 0.0) -> float:
    if not inc_path.is_file():
        return default
    for key, val in _PARAM_INC_RE.findall(inc_path.read_text()):
        if key == name:
            return float(val)
    return default


CL_MARKER = "* postlayout-cl-model:"


def _declared_cl_model(dut: Path) -> str | None:
    """The load model the netlist itself asks for, if it says.

    A netlist that carries its own interconnect capacitance needs the Miller term
    only; one that carries no parasitics needs the full CL. The generator knows
    which it built and records it, so the right answer does not depend on the caller
    remembering.
    """
    for line in dut.read_text().splitlines()[:8]:
        if line.startswith(CL_MARKER):
            value = line[len(CL_MARKER):].strip()
            if value in ("full", "miller"):
                return value
    return None


def _resolve_cl_tb(spice_dir: Path, mode: str) -> str:
    """TB shunt load on outp/outn for benches that use {CL_TB}.

    Schematic runs use full ``CL`` (Miller + interconnect).  Post-layout extracted
    netlists already include the output routing, so ``CL_INTERCONNECT`` would be
    double-counted if applied again via ``Cload_*``.
    """
    params_inc = spice_dir / "params.inc"
    if mode == "full":
        val = _param_from_inc(params_inc, "CL")
        if val <= 0:
            raise SystemExit("CL missing from spice/params.inc")
        return f"{val:.6g}"
    val = _param_from_inc(params_inc, "CL_MILLER")
    if val <= 0:
        raise SystemExit("CL_MILLER missing from spice/params.inc")
    return f"{val:.6g}"


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


def _dut_extra_params(dut_cir: Path, spice_dir: Path) -> dict[str, str]:
    """Resolve template tokens the external DUT may reference."""
    text = dut_cir.read_text()
    extra: dict[str, str] = {}
    if "{IND_SHUNT_INC}" in text:
        ind_inc = spice_dir / "ind_shunt.inc"
        if not ind_inc.is_file():
            raise SystemExit(
                f"DUT {dut_cir} references {{IND_SHUNT_INC}} but {ind_inc} is missing"
            )
        extra["IND_SHUNT_INC"] = str(ind_inc.resolve())
    return extra


def _is_pdk_dut(dut_cir: Path) -> bool:
    """Heuristic: PDK CTLE uses rppd/rsil and the EM ind_shunt subcircuit."""
    text = dut_cir.read_text().lower()
    return "rppd" in text or "ind_shunt" in text or "{ind_shunt_inc}" in text


def _require_finite(name: str, value: float) -> float:
    if math.isnan(value) or math.isinf(value):
        raise ValueError(f"Could not parse metric {name}")
    return value


def run_postlayout_pass(
    pass_name: str,
    dut_cir: Path,
    spice_dir: Path,
    *,
    cl_tb: str,
    extra_params: dict[str, str] | None = None,
) -> SimMetrics:
    """DC + AC suite for one external CTLE DUT view."""
    models = pdk_models()
    dut_cir = dut_cir.resolve()
    if not dut_cir.is_file():
        raise FileNotFoundError(f"DUT netlist not found: {dut_cir}")

    pout = pass_out(pass_name)
    pout.mkdir(parents=True, exist_ok=True)
    work = pout / "work"
    work.mkdir(parents=True, exist_ok=True)

    ep = {**_dut_extra_params(dut_cir, spice_dir), **(extra_params or {})}
    _copy_params_inc(spice_dir, work)
    tb_kw = dict(dut_name=CTLE_DUT_NAME)

    tb_dc = prepare_tb(
        spice_dir / "tb_dc.cir",
        dut_cir,
        work,
        models,
        spice_dir,
        extra_params=ep,
        **tb_kw,
    )
    dc_log = run_ngspice(tb_dc, work, "dc.log")
    dc_vals = parse_dc_log(dc_log)

    tb_diff = prepare_tb(
        spice_dir / "tb_ac_diff.cir",
        dut_cir,
        work,
        models,
        spice_dir,
        extra_params=ep,
        cl_tb=cl_tb,
        **tb_kw,
    )
    run_ngspice(tb_diff, work, "ac_diff.log")
    freq, voutp, voutn, vin_p, vin_n = parse_ac_raw(work / "ac_diff.raw")
    vod = voutp - voutn
    vid = vin_p - vin_n
    h_diff = np.where(np.abs(vid) > 1e-30, vod / vid, 0.0)
    h_db = 20.0 * np.log10(np.abs(h_diff))
    dc_gain_db = _require_finite("dc_gain_db", float(h_db[0]))
    peaking_db = _require_finite(
        "peaking_db", interp_db_at(freq, h_db, NYQUIST_HZ) - dc_gain_db
    )
    peak_gain_db, f_peak_hz, f_3db_hz, f3db_at_fmax = compute_ac_peak_metrics(freq, h_db)

    tb_cm = prepare_tb(
        spice_dir / "tb_ac_cm.cir",
        dut_cir,
        work,
        models,
        spice_dir,
        extra_params=ep,
        cl_tb=cl_tb,
        **tb_kw,
    )
    run_ngspice(tb_cm, work, "ac_cm.log")
    freq_cm, voutp_cm, voutn_cm, vin_p_cm, vin_n_cm = parse_ac_raw(work / "ac_cm.raw")
    voc_cm = (voutp_cm + voutn_cm) / 2.0
    vic_cm = (vin_p_cm + vin_n_cm) / 2.0
    h_cm = np.where(np.abs(vic_cm) > 1e-30, voc_cm / vic_cm, 0.0)
    acm_db = float(20.0 * np.log10(max(np.abs(h_cm[0]), 1e-30)))
    cmrr_db = _require_finite("cmrr_db", dc_gain_db - acm_db)

    tb_psrr = prepare_tb(
        spice_dir / "tb_ac_psrr.cir",
        dut_cir,
        work,
        models,
        spice_dir,
        extra_params=ep,
        cl_tb=cl_tb,
        **tb_kw,
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
    psrr_db = _require_finite("psrr_db", float(psrr_db_curve[0]))

    v_e1 = dc_vals.get("v(xu1.e1)", 0.0)
    v_mgate = dc_vals.get("v(mgate)", dc_vals.get("v(xu1.mgate)", 0.85))
    v_c1 = dc_vals.get("v(outp)", 0.0)
    vce = v_c1 - v_e1
    ic_a = dc_vals.get("@q.xu1.xq1.qnpn13g2[ic]", float("nan"))
    id_tail1 = dc_vals.get("@n.xu1.xtail1.nsg13_lv_nmos[ids]", float("nan"))
    id_tail2 = dc_vals.get("@n.xu1.xtail2.nsg13_lv_nmos[ids]", float("nan"))
    id_tail = id_tail1
    if not math.isnan(id_tail2):
        id_tail = id_tail1 if math.isnan(id_tail1) else (id_tail1 + id_tail2) / 2.0
    _require_finite("ic_a", ic_a)

    params_inc = work / "params.inc"
    cl_f = _param_from_inc(params_inc, "CL")
    if _is_pdk_dut(dut_cir):
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

    (pout / "op.txt").write_text(dc_log.read_text())

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


def run_tran(
    pass_name: str,
    dut_cir: Path,
    spice_dir: Path,
    vbase: float,
    cl_tb: str,
    extra_params: dict[str, str] | None = None,
) -> EyeMetrics:
    models = pdk_models()
    dut_cir = dut_cir.resolve()
    pout = pass_out(pass_name)
    work = pout / "work"
    work.mkdir(parents=True, exist_ok=True)
    ep = {**_dut_extra_params(dut_cir, spice_dir), **(extra_params or {})}
    _copy_params_inc(spice_dir, work)

    tmax = write_prbs_stim(work / "prbs_stim.inc", vbase)
    tb_tran = prepare_tb(
        spice_dir / "tb_tran.cir",
        dut_cir,
        work,
        models,
        spice_dir,
        extra_params={**ep, "TMAX": f"{tmax:.6e}"},
        cl_tb=cl_tb,
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
    phase_ok, phase_summary, _, _ = verify_eye_phase_invariance(time_s, v_outp, v_outn)
    if not phase_ok:
        raise ValueError(
            f"{pass_name} eye metrics are not phase-invariant: {phase_summary}"
        )
    return eye


def run_sbr(
    pass_name: str,
    dut_cir: Path,
    spice_dir: Path,
    vbase: float,
    cl_tb: str,
    extra_params: dict[str, str] | None = None,
) -> SbrResult:
    models = pdk_models()
    dut_cir = dut_cir.resolve()
    pout = pass_out(pass_name)
    work = pout / "work"
    work.mkdir(parents=True, exist_ok=True)
    ep = {**_dut_extra_params(dut_cir, spice_dir), **(extra_params or {})}
    _copy_params_inc(spice_dir, work)

    tmax = write_sbr_stim(work / "sbr_stim.inc", vbase)
    tb_sbr = prepare_tb(
        spice_dir / "tb_sbr.cir",
        dut_cir,
        work,
        models,
        spice_dir,
        extra_params={**ep, "TMAX": f"{tmax:.6e}"},
        cl_tb=cl_tb,
        dut_name=CTLE_DUT_NAME,
    )
    run_ngspice(tb_sbr, work, "sbr.log")
    time_s, v_outp, v_outn, v_inp, v_inn = parse_tran_raw(work / "sbr.raw")
    sbr = extract_sbr(time_s, v_outp, v_outn)

    write_tran_csv(pout / "sbr.csv", time_s, v_outp, v_outn, v_inp, v_inn)
    write_sbr_taps_csv(pout / "sbr_taps.csv", sbr)
    plot_sbr(time_s, v_outp, v_outn, v_inp, v_inn, sbr, pout / "sbr.png")
    return sbr


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run CTLE testbenches against an external DUT netlist (post-layout view)",
    )
    parser.add_argument(
        "--dut",
        required=True,
        type=Path,
        help="Path to .cir subcircuit (must be .subckt ctle_dut with CTLE port list)",
    )
    parser.add_argument(
        "--pass-name",
        required=True,
        help="Output directory name under out/ (e.g. postlayout_klayout)",
    )
    parser.add_argument(
        "--no-tran",
        action="store_true",
        help="Skip 56G NRZ PRBS9 and SBR transient",
    )
    parser.add_argument(
        "--cl-tb",
        choices=("miller", "full"),
        default=None,
        help="TB Cload on outp/outn: miller=CL_MILLER only (for a netlist that "
        "carries its own extracted routing); full=CL (Miller+route). Defaults to "
        "whatever the netlist declares, falling back to miller.",
    )
    args = parser.parse_args()

    dut_cir = args.dut
    if not dut_cir.is_absolute():
        dut_cir = (_REPO / dut_cir).resolve()

    spice_dir = _spice_dir()
    pass_name = args.pass_name
    # The netlist knows whether it carries its own interconnect; trust it unless
    # told otherwise, because guessing wrong is silent and shifts the peaking.
    mode = args.cl_tb or _declared_cl_model(dut_cir) or "miller"
    cl_tb = _resolve_cl_tb(spice_dir, mode)
    print(f"  load model: {mode} (Cload = {cl_tb} F per output)")

    try:
        metrics = run_postlayout_pass(pass_name, dut_cir, spice_dir, cl_tb=cl_tb)
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)

    sbr: SbrResult | None = None
    eye: EyeMetrics | None = None
    if not args.no_tran:
        vbase_m = re.search(
            r"\.param\s+VBASE=([\S]+)",
            (spice_dir / "params.inc").read_text(),
        )
        if not vbase_m:
            print("VBASE missing from spice/params.inc", file=sys.stderr)
            sys.exit(1)
        vbase = float(vbase_m.group(1))
        try:
            eye = run_tran(pass_name, dut_cir, spice_dir, vbase, cl_tb=cl_tb)
            sbr = run_sbr(pass_name, dut_cir, spice_dir, vbase, cl_tb=cl_tb)
        except (RuntimeError, ValueError) as exc:
            print(exc, file=sys.stderr)
            sys.exit(1)

    write_pass_metrics(pass_out(pass_name) / "metrics.csv", metrics, sbr, eye)
    print(
        f"Post-layout({pass_name}): DC={metrics.dc_gain_db:.2f} dB "
        f"peak={metrics.peaking_db:.2f} dB "
        f"CMRR={metrics.cmrr_db:.2f} dB PSRR={metrics.psrr_db:.2f} dB"
    )


if __name__ == "__main__":
    main()
