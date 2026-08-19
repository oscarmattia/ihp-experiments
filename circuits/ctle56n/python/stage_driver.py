#!/usr/bin/env python3
"""Run ngspice verification for 56G output pad driver (driver_pdk.cir).

Artifacts: circuits/ctle56n/out/driver/
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[3]
_EXP = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ctlelib import (  # noqa: E402
    SbrResult,
    SimMetrics,
    EyeMetrics,
    compute_ac_peak_metrics,
    compute_eye_metrics,
    extract_sbr,
    eye_metrics_rows,
    group_delay_s,
    interp_db_at,
    parse_ac_raw,
    parse_dc_log,
    parse_tran_raw,
    plot_ac,
    plot_eye_diff,
    plot_eye_se,
    plot_sbr,
    plot_tran_diff,
    plot_tran_se,
    prepare_tb,
    pdk_models,
    run_ngspice,
    write_ac_diff_csv,
    write_eye_csvs,
    write_pass_metrics,
    write_prbs_stim,
    write_sbr_stim,
    write_sbr_taps_csv,
    write_tran_csv,
)
from ctlelib.ngs import apply_params, complex_from_vm_vp  # noqa: E402
from size_driver import DriverParams, extra_params, print_summary, size_driver  # noqa: E402
from size_term import Z0_DIFF_OHM  # noqa: E402

NYQUIST_HZ = 28e9
DRIVER_DUT_NAME = "driver_dut"
EYE_TARGET_MV = 400.0
EYE_HEIGHT_TARGET_MV = 200.0
EYE_WIDTH_TARGET_UI = 0.5

DRIVER_DC_SAVE_LINES = (
    "save v(outp) v(outn) v(inp) v(inn) v(vdd) v(xu1.vtt)\n"
    "save v(xu1.coll_p) v(xu1.coll_n) v(xu1.e1) v(xu1.e2) v(xu1.mgate)\n"
    "save @q.xu1.xq1.qnpn13g2[ic] @q.xu1.xq2.qnpn13g2[ic]\n"
    "save @n.xu1.xtail1.nsg13_lv_nmos[ids] @n.xu1.xtail2.nsg13_lv_nmos[ids]\n"
    "save @n.xu1.xmdiode.nsg13_lv_nmos[ids]"
)
DRIVER_DC_PRINT_LINES = (
    "print v(outp) v(outn) v(inp) v(inn) v(vdd) v(xu1.vtt)\n"
    "print v(xu1.coll_p) v(xu1.coll_n) v(xu1.e1) v(xu1.e2) v(xu1.mgate)\n"
    "print @q.xu1.xq1.qnpn13g2[ic] @q.xu1.xq2.qnpn13g2[ic]\n"
    "print @n.xu1.xtail1.nsg13_lv_nmos[ids] @n.xu1.xtail2.nsg13_lv_nmos[ids]\n"
    "print @n.xu1.xmdiode.nsg13_lv_nmos[ids]"
)


@dataclass
class DriverSimMetrics:
    vpad_cm_v: float
    vcoll_cm_v: float
    vce_v: float
    vds_tail_v: float
    vgs_tail_v: float
    ic_a: float
    id_tail_a: float
    rd_realized_ohm: float
    m_realized: float
    r_eff_se_ohm: float
    swing_pad_mv: float
    eye_height_margin_pct: float
    eye_width_margin_pct: float
    s11_dc_db: float
    s11_28g_db: float
    sbr: SbrResult | None = None
    eye: EyeMetrics | None = None


def driver_out() -> Path:
    return _EXP / "out" / "driver"


def _write_work_params(work: Path, ep: dict[str, str]) -> None:
    skip = {"IND_SHUNT_INC"}
    lines = [f".param {k}={v}" for k, v in sorted(ep.items()) if k not in skip]
    (work / "params.inc").write_text("\n".join(lines) + "\n")


def _patch_tb_nodeset(tb_path: Path, ep: dict[str, str]) -> None:
    import re

    vdd = float(ep["VDD"])
    vbe = float(ep["VBE"])
    vbase = float(ep["VBASE"])
    itail = float(ep["ITAIL"])
    rd = float(ep.get("RPPD_R", ep.get("RD", "50")))
    ve = vbase - vbe
    vcoll = vdd - itail * rd
    vpad = float(ep.get("VBASE", vbase))  # near vtt at DC
    text = tb_path.read_text()
    text = re.sub(
        r"\.nodeset[^\n]*",
        f".nodeset v(xu1.mgate)={ep['MOS_VGS']} "
        f"v(xu1.e1)={ve:.4f} v(xu1.e2)={ve:.4f} "
        f"v(xu1.coll_p)={vcoll:.4f} v(xu1.coll_n)={vcoll:.4f} "
        f"v(outp)={vpad:.4f} v(outn)={vpad:.4f}",
        text,
        count=1,
    )
    tb_path.write_text(text)


def _prepare_driver_tb(
    template: Path,
    dut_cir: Path,
    work: Path,
    models: Path,
    spice_dir: Path,
    ep: dict[str, str],
) -> Path:
    tb = prepare_tb(
        template,
        dut_cir,
        work,
        models,
        spice_dir,
        extra_params=ep,
        cl_tb=ep.get("CL_TB", "0"),
        dut_name=DRIVER_DUT_NAME,
        dc_save_lines=DRIVER_DC_SAVE_LINES,
        dc_print_lines=DRIVER_DC_PRINT_LINES,
    )
    _patch_tb_nodeset(tb, ep)
    return tb


def _parse_zin_pad_raw(raw: Path) -> tuple[np.ndarray, np.ndarray]:
    """Z_diff from small voltage excitation at pads: Z = Vdiff / Idiff."""
    rows = []
    with raw.open() as f:
        for line in f:
            parts = line.split()
            if parts:
                rows.append([float(x) for x in parts])
    arr = np.asarray(rows)
    freq = arr[:, 0]
    if arr.shape[1] >= 9:
        # wrdata may duplicate frequency: freq freq vm(outp) vp(outp) ...
        off = 1 if arr.shape[1] >= 10 else 0
        vop = complex_from_vm_vp(arr[:, 1 + off], arr[:, 2 + off])
        von = complex_from_vm_vp(arr[:, 3 + off], arr[:, 4 + off])
        ivp = complex_from_vm_vp(arr[:, 5 + off], arr[:, 6 + off])
        ivn = complex_from_vm_vp(arr[:, 7 + off], arr[:, 8 + off])
        vdiff = vop - von
        idiff = ivp + ivn
        zdiff = np.abs(vdiff / np.where(np.abs(idiff) > 1e-30, idiff, 1e-30))
        return freq, zdiff
    vop = complex_from_vm_vp(arr[:, 1], arr[:, 2])
    von = complex_from_vm_vp(arr[:, 3], arr[:, 4])
    zdiff = np.abs(vop - von)
    return freq, zdiff


def _s11_db(z: np.ndarray, z0: float = Z0_DIFF_OHM) -> np.ndarray:
    s11 = (z - z0) / (z + z0)
    return 20.0 * np.log10(np.maximum(np.abs(s11), 1e-30))


def plot_s11_pad(freq: np.ndarray, s11_db: np.ndarray, path: Path) -> None:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.semilogx(freq, s11_db, "b-", lw=1.2)
    ax.axhline(-10, color="gray", ls="--", lw=0.8, label="-10 dB")
    ax.axvline(NYQUIST_HZ, color="orange", ls=":", lw=0.8)
    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("S11 (dB, 100 Ohm diff ref)")
    ax.set_title("Pad return loss (outp-outn, 100 Ohm differential reference)")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend(fontsize=8)
    fig.savefig(path, dpi=120)
    plt.close(fig)


def write_s11_csv(path: Path, freq: np.ndarray, s11_db: np.ndarray, z_ohm: np.ndarray) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["freq_Hz", "S11_dB", "Zdiff_ohm"])
        for i in range(len(freq)):
            w.writerow([freq[i], s11_db[i], z_ohm[i]])


def write_driver_metrics(
    path: Path,
    params: DriverParams,
    sim: DriverSimMetrics,
    ac_m: SimMetrics,
) -> None:
    rows = [
        ["parameter", "value"],
        ["VDD_V", f"{params.vdd:.6g}"],
        ["VBASE_V", f"{params.vbase:.6g}"],
        ["Nx", str(params.nx)],
        ["ITAIL_A", f"{params.itail_a:.6g}"],
        ["back_term", "yes" if params.back_term else "no"],
        ["R_eff_se_ohm", f"{params.r_eff_se_ohm:.3f}"],
        ["RD_target_ohm", f"{params.rd_ohm:.3f}"],
        ["RD_realized_ohm", f"{sim.rd_realized_ohm:.3f}"],
        ["L_pH", f"{params.l_h * 1e12:.2f}"],
        ["L_EM_case", params.l_em_case],
        ["CL_pad_fF", f"{params.cl_pad_f * 1e15:.2f}"],
        ["m_realized", f"{sim.m_realized:.4f}"],
        ["m_bessel_target", f"{params.m_bessel_target:.3f}"],
        ["predriver_needed", "yes" if params.predriver_needed else "no"],
        ["driver_Cin_Miller_fF", f"{params.driver_cin_ff:.2f}"],
        ["vga_FO2_fF", f"{params.vga_fo2_ff:.2f}"],
        ["Vpad_CM_V", f"{sim.vpad_cm_v:.4f}"],
        ["Vcoll_CM_V", f"{sim.vcoll_cm_v:.4f}"],
        ["VCE_V", f"{sim.vce_v:.4f}"],
        ["VDS_tail_V", f"{sim.vds_tail_v:.4f}"],
        ["Ic_A", f"{sim.ic_a:.6g}"],
        ["dc_gain_dB", f"{ac_m.dc_gain_db:.3f}"],
        ["ac_gain_28G_dB", f"{ac_m.peaking_db + ac_m.dc_gain_db:.3f}"],
        ["peaking_28G_dB", f"{ac_m.peaking_db:.3f}"],
        ["G_peak_dB", f"{ac_m.peak_gain_db:.3f}"],
        ["f_peak_Hz", f"{ac_m.f_peak_hz:.6g}"],
        ["f_3dB_Hz", f"{ac_m.f_3db_hz:.6g}"],
        ["pad_swing_pp_mV", f"{sim.swing_pad_mv:.2f}"],
        ["S11_dc_dB", f"{sim.s11_dc_db:.3f}"],
        ["S11_28GHz_dB", f"{sim.s11_28g_db:.3f}"],
    ]
    if sim.eye:
        rows += eye_metrics_rows("pad", sim.eye)
        rows.append(
            ["eye_height_margin_pct", f"{sim.eye_height_margin_pct:.1f}"]
        )
        rows.append(
            ["eye_width_margin_pct", f"{sim.eye_width_margin_pct:.1f}"]
        )
    if sim.sbr:
        rows += [
            ["sbr_cursor_mV", f"{sim.sbr.cursor_mV:.4f}"],
            ["sbr_isi_norm", f"{sim.sbr.isi_norm:.6g}"],
        ]
    with path.open("w", newline="") as f:
        csv.writer(f).writerows(rows)


def run(
    out_dir: Path | None = None,
    *,
    no_tran: bool = False,
    back_term: bool = False,
) -> tuple[DriverParams, DriverSimMetrics]:
    spice_dir = _EXP / "spice"
    pout = out_dir or driver_out()
    pout.mkdir(parents=True, exist_ok=True)
    work = pout / "work"
    work.mkdir(parents=True, exist_ok=True)

    pdk = os.environ.get("PDK_ROOT", "")
    if pdk:
        spiceinit = Path(pdk) / "ihp-sg13g2/libs.tech/ngspice/.spiceinit"
        if spiceinit.is_file():
            (work / ".spiceinit").write_bytes(spiceinit.read_bytes())

    params = size_driver(back_term=back_term)
    ep = extra_params(params)
    print_summary(params)
    _write_work_params(work, ep)

    models = pdk_models()
    dut_cir = spice_dir / "driver_pdk.cir"

    # --- DC ---
    tb_dc = _prepare_driver_tb(
        spice_dir / "tb_dc.cir", dut_cir, work, models, spice_dir, ep,
    )
    dc_log = run_ngspice(tb_dc, work, "dc.log")
    dc = parse_dc_log(dc_log)
    (pout / "op.txt").write_text(dc_log.read_text())

    vpad_cm = 0.5 * (dc.get("v(outp)", 0) + dc.get("v(outn)", 0))
    vcoll_cm = 0.5 * (dc.get("v(xu1.coll_p)", 0) + dc.get("v(xu1.coll_n)", 0))
    ve = dc.get("v(xu1.e1)", 0)
    vce = vcoll_cm - ve
    vds_tail = ve
    vgs_tail = dc.get("v(xu1.mgate)", 0.85)
    ic1 = dc.get("@q.xu1.xq1.qnpn13g2[ic]", float("nan"))
    ic2 = dc.get("@q.xu1.xq2.qnpn13g2[ic]", float("nan"))
    ic_a = (ic1 + ic2) / 2.0
    id_tail = dc.get("@n.xu1.xtail1.nsg13_lv_nmos[ids]", float("nan"))
    id_tail = (
        id_tail + dc.get("@n.xu1.xtail2.nsg13_lv_nmos[ids]", 0)
        if not math.isnan(id_tail)
        else float("nan")
    )

    rd_real = float(ep.get("RPPD_R", params.rppd_r_ohm))
    m_real = params.l_h / (rd_real ** 2 * params.cl_pad_f)

    # --- AC differential (inp -> pad) ---
    tb_ac = _prepare_driver_tb(
        spice_dir / "tb_ac_diff.cir", dut_cir, work, models, spice_dir, ep,
    )
    run_ngspice(tb_ac, work, "ac_diff.log")
    freq, voutp, voutn, vin_p, vin_n = parse_ac_raw(work / "ac_diff.raw")
    vod = voutp - voutn
    vid = vin_p - vin_n
    h = np.where(np.abs(vid) > 1e-30, vod / vid, 0.0)
    h_db = 20.0 * np.log10(np.abs(h))
    dc_gain_db = float(h_db[0])
    ac_gain_28g = float(interp_db_at(freq, h_db, NYQUIST_HZ))
    peaking_db = ac_gain_28g - dc_gain_db
    peak_gain_db, f_peak_hz, f_3db_hz, _ = compute_ac_peak_metrics(freq, h_db)
    gd_s = group_delay_s(freq, h)

    plot_ac(
        freq,
        h_db,
        gd_s,
        pout / "ac_diff.png",
        peak_gain_db=peak_gain_db,
        f_peak_hz=f_peak_hz,
        f_3db_hz=f_3db_hz,
        f3db_at_fmax=f_3db_hz >= freq[-1] * 0.99,
        dc_gain_db=dc_gain_db,
        peaking_db=peaking_db,
    )
    write_ac_diff_csv(pout / "ac_diff.csv", freq, h_db, gd_s)

    ac_m = SimMetrics(
        pass_name="driver",
        dc_gain_db=dc_gain_db,
        peaking_db=peaking_db,
        cmrr_db=float("nan"),
        psrr_db=float("nan"),
        vce_v=vce,
        vds_tail_v=vds_tail,
        vgs_tail_v=vgs_tail,
        ic_a=ic_a,
        id_tail_a=id_tail,
        peak_gain_db=peak_gain_db,
        f_peak_hz=f_peak_hz,
        f_3db_hz=f_3db_hz,
        rd_realized_ohm=rd_real,
        m_realized=m_real,
    )

    # --- Pad return loss ---
    tb_pad = prepare_tb(
        spice_dir / "tb_driver_pad.cir",
        dut_cir,
        work,
        models,
        spice_dir,
        extra_params=ep,
        dut_name=DRIVER_DUT_NAME,
        cl_tb="0",
        dc_save_lines=DRIVER_DC_SAVE_LINES,
        dc_print_lines=DRIVER_DC_PRINT_LINES,
    )
    run_ngspice(tb_pad, work, "zin_pad.log")
    freq_z, zdiff = _parse_zin_pad_raw(work / "zin_pad.raw")
    s11_db = _s11_db(zdiff)
    s11_dc = float(s11_db[0])
    s11_28 = float(interp_db_at(freq_z, s11_db, NYQUIST_HZ))
    plot_s11_pad(freq_z, s11_db, pout / "zin.png")
    write_s11_csv(pout / "zin.csv", freq_z, s11_db, zdiff)

    sbr_result: SbrResult | None = None
    eye_result: EyeMetrics | None = None
    swing_pad_mv = float("nan")
    h_margin = float("nan")
    w_margin = float("nan")

    if not no_tran:
        tmax = write_prbs_stim(work / "prbs_stim.inc", params.vbase)
        ep_tran = {**ep, "TMAX": f"{tmax:.6e}"}
        tb_tran = _prepare_driver_tb(
            spice_dir / "tb_tran.cir", dut_cir, work, models, spice_dir, ep_tran,
        )
        # Fix tail save paths for dual-tail driver
        tb_tran.write_text(
            tb_tran.read_text().replace(
                "@n.xu1.xtail.nsg13_lv_nmos[ids]",
                "@n.xu1.xtail1.nsg13_lv_nmos[ids]",
            )
        )
        run_ngspice(tb_tran, work, "tran.log")
        time_s, v_outp, v_outn, v_inp, v_inn = parse_tran_raw(work / "tran.raw")
        write_tran_csv(pout / "tran.csv", time_s, v_outp, v_outn, v_inp, v_inn)
        write_eye_csvs(pout, time_s, v_outp, v_outn)
        plot_tran_se(time_s, v_outp, v_outn, v_inp, v_inn, pout / "tran_se.png")
        plot_tran_diff(time_s, v_outp, v_outn, v_inp, v_inn, pout / "tran_diff.png")
        plot_eye_se(time_s, v_outp, v_outn, pout / "eye_se.png")
        plot_eye_diff(time_s, v_outp, v_outn, pout / "eye_diff.png")
        eye_result = compute_eye_metrics(time_s, v_outp, v_outn)
        swing_pad_mv = eye_result.pp_swing_mV
        h_margin = (eye_result.height_mV / EYE_HEIGHT_TARGET_MV - 1.0) * 100.0
        w_margin = (eye_result.width_ui / EYE_WIDTH_TARGET_UI - 1.0) * 100.0

        tmax_sbr = write_sbr_stim(work / "sbr_stim.inc", params.vbase)
        ep_sbr = {**ep, "TMAX": f"{tmax_sbr:.6e}"}
        tb_sbr = _prepare_driver_tb(
            spice_dir / "tb_sbr.cir", dut_cir, work, models, spice_dir, ep_sbr,
        )
        run_ngspice(tb_sbr, work, "sbr.log")
        time_s, v_outp, v_outn, v_inp, v_inn = parse_tran_raw(work / "sbr.raw")
        sbr_result = extract_sbr(time_s, v_outp, v_outn)
        write_tran_csv(pout / "sbr.csv", time_s, v_outp, v_outn, v_inp, v_inn)
        write_sbr_taps_csv(pout / "sbr_taps.csv", sbr_result)
        plot_sbr(time_s, v_outp, v_outn, v_inp, v_inn, sbr_result, pout / "sbr.png")

    sim = DriverSimMetrics(
        vpad_cm_v=vpad_cm,
        vcoll_cm_v=vcoll_cm,
        vce_v=vce,
        vds_tail_v=vds_tail,
        vgs_tail_v=vgs_tail,
        ic_a=ic_a,
        id_tail_a=id_tail,
        rd_realized_ohm=rd_real,
        m_realized=m_real,
        r_eff_se_ohm=params.r_eff_se_ohm,
        swing_pad_mv=swing_pad_mv,
        eye_height_margin_pct=h_margin,
        eye_width_margin_pct=w_margin,
        s11_dc_db=s11_dc,
        s11_28g_db=s11_28,
        sbr=sbr_result,
        eye=eye_result,
    )
    write_driver_metrics(pout / "metrics.csv", params, sim, ac_m)

    print("\n=== Driver sim summary ===")
    print(f"  Pad CM={vpad_cm:.4f} V  VCE={vce:.3f} V  Ic={ic_a*1e3:.2f} mA")
    print(
        f"  AC DC={dc_gain_db:.2f} dB  28G={ac_gain_28g:.2f} dB  "
        f"peaking={peaking_db:.2f} dB  m={m_real:.3f}"
    )
    print(f"  S11 pad: DC={s11_dc:.2f} dB  28G={s11_28:.2f} dB (100 Ω diff ref)")
    if eye_result:
        print(
            f"  Pad eye: pp={eye_result.pp_swing_mV:.1f} mV  "
            f"height={eye_result.height_mV:.1f} mV ({h_margin:+.0f}% vs 200 mV)  "
            f"width={eye_result.width_ui:.3f} UI ({w_margin:+.0f}% vs 0.5 UI)"
        )
    print(f"  Artifacts: {pout}/")
    return params, sim


def main() -> None:
    parser = argparse.ArgumentParser(description="Run output pad driver ngspice suite")
    parser.add_argument("--no-tran", action="store_true")
    parser.add_argument("--back-term", action="store_true")
    args = parser.parse_args()
    run(no_tran=args.no_tran, back_term=args.back_term)


if __name__ == "__main__":
    main()
