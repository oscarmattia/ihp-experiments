#!/usr/bin/env python3
"""Run ngspice DC/AC/tran for 56G CML VGA (ideal + PDK passes).

Assumes the caller has sourced ~/.local/share/ihp-eda/env.sh.
"""

from __future__ import annotations

import argparse
import csv
import math
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[3]
_EXP = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from size_vga import (  # noqa: E402
    VgaParams,
    extra_params,
    print_summary,
    size_vga,
    size_vga_for_chain,
)

from ctlelib import (  # noqa: E402
    PSRR_MAX_DB,
    SbrResult,
    SimMetrics,
    EyeMetrics,
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
VGA_DUT_NAME = "vga_dut"

VGA_DC_SAVE_LINES = (
    "save v(outp) v(outn) v(inp) v(inn) v(vdd)\n"
    "save v(xu1.e1) v(xu1.e2) v(xu1.ed1) v(xu1.ed2) v(xu1.mgate)\n"
    "save v(xu1.tx1) v(xu1.tx2) v(xu1.steerp) v(xu1.steern) v(xu1.ctrl)\n"
    "save @q.xu1.xq1.qnpn13g2[ic] @q.xu1.xq2.qnpn13g2[ic]\n"
    "save @q.xu1.xqd1.qnpn13g2[ic] @q.xu1.xqd2.qnpn13g2[ic]\n"
    "save @n.xu1.xtail1.nsg13_lv_nmos[ids] @n.xu1.xtail2.nsg13_lv_nmos[ids]\n"
    "save @n.xu1.xps1.nsg13_lv_nmos[ids] @n.xu1.xpd1.nsg13_lv_nmos[ids]\n"
    "save @n.xu1.xps2.nsg13_lv_nmos[ids] @n.xu1.xpd2.nsg13_lv_nmos[ids]\n"
    "save @n.xu1.xmdiode.nsg13_lv_nmos[ids]"
)
VGA_DC_PRINT_LINES = (
    "print v(outp) v(outn) v(inp) v(inn) v(vdd)\n"
    "print v(xu1.e1) v(xu1.e2) v(xu1.ed1) v(xu1.ed2) v(xu1.mgate)\n"
    "print v(xu1.tx1) v(xu1.tx2) v(xu1.steerp) v(xu1.steern) v(xu1.ctrl)\n"
    "print @q.xu1.xq1.qnpn13g2[ic] @q.xu1.xq2.qnpn13g2[ic]\n"
    "print @q.xu1.xqd1.qnpn13g2[ic] @q.xu1.xqd2.qnpn13g2[ic]\n"
    "print @n.xu1.xtail1.nsg13_lv_nmos[ids] @n.xu1.xtail2.nsg13_lv_nmos[ids]\n"
    "print @n.xu1.xps1.nsg13_lv_nmos[ids] @n.xu1.xpd1.nsg13_lv_nmos[ids]\n"
    "print @n.xu1.xps2.nsg13_lv_nmos[ids] @n.xu1.xpd2.nsg13_lv_nmos[ids]\n"
    "print @n.xu1.xmdiode.nsg13_lv_nmos[ids]"
)


@dataclass
class VgaSettingMetrics:
    vctrl_v: float
    dc_gain_db: float
    ac_gain_28g_db: float
    peaking_db: float
    peak_gain_db: float
    f_peak_hz: float
    f_3db_hz: float
    vce_v: float
    vds_tail_v: float
    vgs_tail_v: float
    ic_signal_a: float
    ic_dummy_a: float
    id_tail_signal_a: float
    id_tail_dummy_a: float
    id_tail_a: float
    vout_cm_v: float
    steer_ok: bool


def _spice_dir() -> Path:
    return _EXP / "spice"


def _tb_kw() -> dict:
    return dict(
        dut_name=VGA_DUT_NAME,
        dc_save_lines=VGA_DC_SAVE_LINES,
        dc_print_lines=VGA_DC_PRINT_LINES,
    )


def _steering_from_dc(dc: dict[str, float]) -> tuple[float, float, float, float, float, bool]:
    """Return signal/dummy Ic, pass/tail ids, and steering sanity flag."""
    ic1 = dc.get("@q.xu1.xq1.qnpn13g2[ic]", float("nan"))
    ic2 = dc.get("@q.xu1.xq2.qnpn13g2[ic]", float("nan"))
    icd1 = dc.get("@q.xu1.xqd1.qnpn13g2[ic]", float("nan"))
    icd2 = dc.get("@q.xu1.xqd2.qnpn13g2[ic]", float("nan"))
    ids_t1 = dc.get("@n.xu1.xtail1.nsg13_lv_nmos[ids]", float("nan"))
    ids_t2 = dc.get("@n.xu1.xtail2.nsg13_lv_nmos[ids]", float("nan"))
    ids_ps1 = dc.get("@n.xu1.xps1.nsg13_lv_nmos[ids]", float("nan"))
    ids_pd1 = dc.get("@n.xu1.xpd1.nsg13_lv_nmos[ids]", float("nan"))
    ids_ps2 = dc.get("@n.xu1.xps2.nsg13_lv_nmos[ids]", float("nan"))
    ids_pd2 = dc.get("@n.xu1.xpd2.nsg13_lv_nmos[ids]", float("nan"))
    ic_sig = (ic1 + ic2) / 2.0 if not (math.isnan(ic1) or math.isnan(ic2)) else float("nan")
    ic_dum = (icd1 + icd2) / 2.0 if not (math.isnan(icd1) or math.isnan(icd2)) else float("nan")
    id_sig = (
        (abs(ids_ps1) + abs(ids_ps2)) / 2.0
        if not (math.isnan(ids_ps1) or math.isnan(ids_ps2))
        else float("nan")
    )
    id_dum = (
        (abs(ids_pd1) + abs(ids_pd2)) / 2.0
        if not (math.isnan(ids_pd1) or math.isnan(ids_pd2))
        else float("nan")
    )
    id_tail = (
        ids_t1 + ids_t2
        if not (math.isnan(ids_t1) or math.isnan(ids_t2))
        else float("nan")
    )
    steer_ok = True
    if not math.isnan(ic_sig) and ic_sig < 1e-7:
        steer_ok = False
    if not math.isnan(ic_dum) and ic_dum < -1e-9:
        steer_ok = False
    return ic_sig, ic_dum, id_sig, id_dum, id_tail, steer_ok


def _write_work_params(work: Path, ep: dict[str, str]) -> None:
    """Emit work/params.inc so tb_*.cir ``.include params.inc`` resolves."""
    skip = {"IND_SHUNT_INC"}
    lines = [f".param {k}={v}" for k, v in sorted(ep.items()) if k not in skip]
    (work / "params.inc").write_text("\n".join(lines) + "\n")


def _patch_tb_nodeset(tb_path: Path, ep: dict[str, str]) -> None:
    """Symmetry-friendly nodeset for dual-tail VGA (after param substitution)."""
    import re

    vdd = float(ep["VDD"])
    vbe = float(ep["VBE"])
    vbase = float(ep["VBASE"])
    itail = float(ep["ITAIL"])
    rd = float(ep["RD"])
    ve = vbase - vbe
    vout = vdd - itail * rd
    text = tb_path.read_text()
    text = re.sub(
        r"\.nodeset[^\n]*",
        f".nodeset v(xu1.mgate)={ep['MOS_VGS']} v(xu1.e1)={ve:.4f} "
        f"v(xu1.e2)={ve:.4f} v(xu1.ed1)={ve:.4f} v(xu1.ed2)={ve:.4f} "
        f"v(outp)={vout:.4f} v(outn)={vout:.4f}",
        text,
        count=1,
    )
    tb_path.write_text(text)


def _prepare_vga_tb(
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
        cl_tb=ep["CL"],
        **_tb_kw(),
    )
    _patch_tb_nodeset(tb, ep)
    return tb


def run_dc_sweep(
    pass_name: str,
    dut_rel: str,
    params: VgaParams,
) -> tuple[list[VgaSettingMetrics], str]:
    """DC OP at each VCTRL; return per-setting bias metrics and combined op log."""
    models = pdk_models()
    spice_dir = _spice_dir()
    dut_cir = spice_dir / dut_rel
    pout = pass_out(pass_name)
    work = pout / "work"
    work.mkdir(parents=True, exist_ok=True)

    rows: list[VgaSettingMetrics] = []
    op_chunks: list[str] = []

    for vc in params.vctrl_v:
        ep = extra_params(params, vctrl=vc)
        _write_work_params(work, ep)
        tb_dc = _prepare_vga_tb(
            spice_dir / "tb_dc.cir",
            dut_cir,
            work,
            models,
            spice_dir,
            ep,
        )
        dc_log = run_ngspice(tb_dc, work, f"dc_vctrl_{vc:.3f}.log")
        dc = parse_dc_log(dc_log)
        v_c1 = dc.get("v(outp)", 0.0)
        v_c2 = dc.get("v(outn)", 0.0)
        ve1 = dc.get("v(xu1.e1)", 0.0)
        vce = v_c1 - ve1
        vds_tail = ve1
        vgs_tail = dc.get("v(xu1.mgate)", 0.85)
        ic_sig, ic_dum, id_sig, id_dum, id_tail, steer_ok = _steering_from_dc(dc)
        vout_cm = (v_c1 + v_c2) / 2.0

        rows.append(
            VgaSettingMetrics(
                vctrl_v=vc,
                dc_gain_db=float("nan"),
                ac_gain_28g_db=float("nan"),
                peaking_db=float("nan"),
                peak_gain_db=float("nan"),
                f_peak_hz=float("nan"),
                f_3db_hz=float("nan"),
                vce_v=vce,
                vds_tail_v=vds_tail,
                vgs_tail_v=vgs_tail,
                ic_signal_a=ic_sig,
                ic_dummy_a=ic_dum,
                id_tail_signal_a=id_sig,
                id_tail_dummy_a=id_dum,
                id_tail_a=id_tail,
                vout_cm_v=vout_cm,
                steer_ok=steer_ok,
            )
        )
        op_chunks.append(f"\n=== VCTRL = {vc:.3f} V ===\n")
        op_chunks.append(dc_log.read_text())

    (pout / "op.txt").write_text("".join(op_chunks))
    return rows, "".join(op_chunks)


def run_ac_at_vctrl(
    pass_name: str,
    dut_rel: str,
    params: VgaParams,
    vctrl: float,
    tag: str,
) -> tuple[VgaSettingMetrics, np.ndarray, np.ndarray, np.ndarray]:
    models = pdk_models()
    spice_dir = _spice_dir()
    dut_cir = spice_dir / dut_rel
    pout = pass_out(pass_name)
    work = pout / "work"
    work.mkdir(parents=True, exist_ok=True)

    ep = extra_params(params, vctrl=vctrl)
    _write_work_params(work, ep)
    tb_diff = _prepare_vga_tb(
        spice_dir / "tb_ac_diff.cir",
        dut_cir,
        work,
        models,
        spice_dir,
        ep,
    )
    run_ngspice(tb_diff, work, f"ac_diff_{tag}.log")
    freq, voutp, voutn, vin_p, vin_n = parse_ac_raw(work / "ac_diff.raw")
    vod = voutp - voutn
    vid = vin_p - vin_n
    h_diff = np.where(np.abs(vid) > 1e-30, vod / vid, 0.0)
    h_db = 20.0 * np.log10(np.abs(h_diff))
    dc_gain_db = float(h_db[0])
    ac_gain_28g_db = float(interp_db_at(freq, h_db, NYQUIST_HZ))
    peaking_db = ac_gain_28g_db - dc_gain_db
    peak_gain_db, f_peak_hz, f_3db_hz, _ = compute_ac_peak_metrics(freq, h_db)
    gd_s = group_delay_s(freq, h_diff)

    plot_ac(
        freq,
        h_db,
        gd_s,
        pout / f"ac_diff_{tag}.png",
        peak_gain_db=peak_gain_db,
        f_peak_hz=f_peak_hz,
        f_3db_hz=f_3db_hz,
        f3db_at_fmax=f_3db_hz >= freq[-1] * 0.99,
        dc_gain_db=dc_gain_db,
        peaking_db=peaking_db,
    )
    write_ac_diff_csv(pout / f"ac_diff_{tag}.csv", freq, h_db, gd_s)

    # Also write canonical names for mid-gain setting
    mid = params.vctrl_v[len(params.vctrl_v) // 2]
    if abs(vctrl - mid) < 1e-6:
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

    m = VgaSettingMetrics(
        vctrl_v=vctrl,
        dc_gain_db=dc_gain_db,
        ac_gain_28g_db=ac_gain_28g_db,
        peaking_db=peaking_db,
        peak_gain_db=peak_gain_db,
        f_peak_hz=f_peak_hz,
        f_3db_hz=f_3db_hz,
        vce_v=float("nan"),
        vds_tail_v=float("nan"),
        vgs_tail_v=float("nan"),
        ic_signal_a=float("nan"),
        ic_dummy_a=float("nan"),
        id_tail_signal_a=float("nan"),
        id_tail_dummy_a=float("nan"),
        id_tail_a=float("nan"),
        vout_cm_v=float("nan"),
        steer_ok=True,
    )
    return m, freq, h_db, gd_s


def run_cm_psrr(
    pass_name: str,
    dut_rel: str,
    params: VgaParams,
    vctrl: float,
    dc_gain_db: float,
) -> tuple[float, float]:
    models = pdk_models()
    spice_dir = _spice_dir()
    dut_cir = spice_dir / dut_rel
    pout = pass_out(pass_name)
    work = pout / "work"
    ep = extra_params(params, vctrl=vctrl)
    _write_work_params(work, ep)

    tb_cm = _prepare_vga_tb(
        spice_dir / "tb_ac_cm.cir",
        dut_cir,
        work,
        models,
        spice_dir,
        ep,
    )
    run_ngspice(tb_cm, work, "ac_cm.log")
    freq_cm, voutp_cm, voutn_cm, vin_p_cm, vin_n_cm = parse_ac_raw(work / "ac_cm.raw")
    voc_cm = (voutp_cm + voutn_cm) / 2.0
    vic_cm = (vin_p_cm + vin_n_cm) / 2.0
    h_cm = np.where(np.abs(vic_cm) > 1e-30, voc_cm / vic_cm, 0.0)
    acm_db = float(20.0 * np.log10(max(np.abs(h_cm[0]), 1e-30)))
    cmrr_db = dc_gain_db - acm_db

    tb_psrr = _prepare_vga_tb(
        spice_dir / "tb_ac_psrr.cir",
        dut_cir,
        work,
        models,
        spice_dir,
        ep,
    )
    run_ngspice(tb_psrr, work, "ac_psrr.log")
    freq_p, voutp_p, voutn_p, vvdd = parse_psrr_raw(work / "ac_psrr.raw")
    vod_p = voutp_p - voutn_p
    vod_safe = np.maximum(np.abs(vod_p), 10.0 ** (-PSRR_MAX_DB / 20.0))
    h_psrr = np.abs(vvdd) / vod_safe
    psrr_db_curve = np.minimum(20.0 * np.log10(np.maximum(h_psrr, 1e-30)), PSRR_MAX_DB)
    psrr_db = float(psrr_db_curve[0])

    h_diff_db = np.full_like(freq_cm, dc_gain_db)
    cmrr_curve = h_diff_db - 20.0 * np.log10(np.maximum(np.abs(h_cm), 1e-30))
    plot_cmrr(freq_cm, cmrr_curve, pout / "cmrr.png")
    plot_psrr(freq_p, psrr_db_curve, pout / "psrr.png")
    return cmrr_db, psrr_db


def plot_gain_vs_vctrl(settings: list[VgaSettingMetrics], path: Path) -> None:
    import matplotlib.pyplot as plt

    vc = [s.vctrl_v for s in settings]
    g_dc = [s.dc_gain_db for s in settings]
    g_28 = [s.ac_gain_28g_db for s in settings]
    pk = [s.peaking_db for s in settings]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(7, 6), sharex=True)
    ax1.plot(vc, g_dc, "b^-", lw=1.5, ms=6, label="DC")
    ax1.plot(vc, g_28, "go-", lw=1.5, ms=6, label="28 GHz")
    ax1.set_ylabel("Gain (dB)")
    ax1.set_title("VGA gain vs VCTRL")
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    ax2.plot(vc, pk, "rs-", lw=1.5, ms=6)
    ax2.set_ylabel("Peaking @ 28 GHz (dB)")
    ax2.set_xlabel("VCTRL (V)")
    ax2.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def write_gain_table(path: Path, dc_rows: list[VgaSettingMetrics], ac_rows: list[VgaSettingMetrics]) -> None:
    ac_by_vc = {round(r.vctrl_v, 4): r for r in ac_rows}
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "VCTRL_V", "dc_gain_dB", "ac_gain_28G_dB", "peaking_28G_dB", "G_peak_dB",
            "f_peak_Hz", "f_3dB_Hz", "Vout_CM_V",
            "Ic_signal_A", "Ic_dummy_A", "Id_tail_signal_A", "Id_tail_dummy_A",
            "Id_tail_total_A", "steer_ok",
        ])
        for dc in dc_rows:
            ac = ac_by_vc.get(round(dc.vctrl_v, 4))
            w.writerow([
                f"{dc.vctrl_v:.4f}",
                f"{ac.dc_gain_db:.3f}" if ac else "",
                f"{ac.ac_gain_28g_db:.3f}" if ac else "",
                f"{ac.peaking_db:.3f}" if ac else "",
                f"{ac.peak_gain_db:.3f}" if ac else "",
                f"{ac.f_peak_hz:.6g}" if ac else "",
                f"{ac.f_3db_hz:.6g}" if ac else "",
                f"{dc.vout_cm_v:.4f}",
                f"{dc.ic_signal_a:.6g}",
                f"{dc.ic_dummy_a:.6g}",
                f"{dc.id_tail_signal_a:.6g}",
                f"{dc.id_tail_dummy_a:.6g}",
                f"{dc.id_tail_a:.6g}",
                "yes" if dc.steer_ok else "no",
            ])


def write_gain_vs_vctrl_csv(path: Path, settings: list[VgaSettingMetrics]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["VCTRL_V", "dc_gain_dB", "ac_gain_28G_dB", "peaking_28G_dB", "G_peak_dB", "f_peak_Hz", "f_3dB_Hz"])
        for s in settings:
            w.writerow([
                s.vctrl_v, s.dc_gain_db, s.ac_gain_28g_db, s.peaking_db,
                s.peak_gain_db, s.f_peak_hz, s.f_3db_hz,
            ])


def _alias_tran_sbr(pout: Path, src_tag: str, label: str) -> None:
    """Copy transient/SBR artifacts to CTLE-style min/mid/max names."""
    pairs = [
        (f"tran_{src_tag}.csv", f"tran_{label}.csv" if label != "mid" else "tran.csv"),
        (f"tran_se_{src_tag}.png", f"tran_se_{label}.png" if label != "mid" else "tran_se.png"),
        (f"tran_diff_{src_tag}.png", f"tran_diff_{label}.png" if label != "mid" else "tran_diff.png"),
        (f"eye_se_{src_tag}.png", f"eye_se_{label}.png" if label != "mid" else "eye_se.png"),
        (f"eye_diff_{src_tag}.png", f"eye_diff_{label}.png" if label != "mid" else "eye_diff.png"),
        (f"sbr_{src_tag}.csv", f"sbr_{label}.csv" if label != "mid" else "sbr.csv"),
        (f"sbr_taps_{src_tag}.csv", f"sbr_taps_{label}.csv" if label != "mid" else "sbr_taps.csv"),
        (f"sbr_{src_tag}.png", f"sbr_{label}.png" if label != "mid" else "sbr.png"),
    ]
    for src_name, dst_name in pairs:
        src = pout / src_name
        dst = pout / dst_name
        if src.is_file():
            shutil.copy2(src, dst)
    # Mid gain also gets eye CSVs (canonical CTLE names).
    if label == "mid":
        tran_csv = pout / "tran.csv"
        if tran_csv.is_file():
            import numpy as np
            from ctlelib import parse_tran_raw
            from ctlelib.metrics import write_eye_csvs

            work_raw = pout / "work" / f"tran_{src_tag}.raw"
            if work_raw.is_file():
                time_s, v_outp, v_outn, _, _ = parse_tran_raw(work_raw)
                write_eye_csvs(pout, time_s, v_outp, v_outn)


def run_tran(
    pass_name: str,
    dut_rel: str,
    params: VgaParams,
    vctrl: float,
    tag: str,
) -> None:
    models = pdk_models()
    spice_dir = _spice_dir()
    dut_cir = spice_dir / dut_rel
    pout = pass_out(pass_name)
    work = pout / "work"
    work.mkdir(parents=True, exist_ok=True)

    ep = extra_params(params, vctrl=vctrl)
    _write_work_params(work, ep)
    tmax = write_prbs_stim(work / "prbs_stim.inc", params.vbase)
    ep["TMAX"] = f"{tmax:.6e}"
    tb_tran = _prepare_vga_tb(
        spice_dir / "tb_tran.cir",
        dut_cir,
        work,
        models,
        spice_dir,
        ep,
    )
    run_ngspice(tb_tran, work, f"tran_{tag}.log")
    time_s, v_outp, v_outn, v_inp, v_inn = parse_tran_raw(work / "tran.raw")

    write_tran_csv(pout / f"tran_{tag}.csv", time_s, v_outp, v_outn, v_inp, v_inn)
    plot_tran_se(time_s, v_outp, v_outn, v_inp, v_inn, pout / f"tran_se_{tag}.png")
    plot_tran_diff(time_s, v_outp, v_outn, v_inp, v_inn, pout / f"tran_diff_{tag}.png")
    plot_eye_se(time_s, v_outp, v_outn, pout / f"eye_se_{tag}.png")
    plot_eye_diff(time_s, v_outp, v_outn, pout / f"eye_diff_{tag}.png")


def run_sbr(
    pass_name: str,
    dut_rel: str,
    params: VgaParams,
    vctrl: float,
    tag: str,
) -> SbrResult:
    models = pdk_models()
    spice_dir = _spice_dir()
    dut_cir = spice_dir / dut_rel
    pout = pass_out(pass_name)
    work = pout / "work"
    work.mkdir(parents=True, exist_ok=True)

    ep = extra_params(params, vctrl=vctrl)
    _write_work_params(work, ep)
    tmax = write_sbr_stim(work / "sbr_stim.inc", params.vbase)
    ep["TMAX"] = f"{tmax:.6e}"
    tb_sbr = _prepare_vga_tb(
        spice_dir / "tb_sbr.cir",
        dut_cir,
        work,
        models,
        spice_dir,
        ep,
    )
    run_ngspice(tb_sbr, work, f"sbr_{tag}.log")
    time_s, v_outp, v_outn, v_inp, v_inn = parse_tran_raw(work / "sbr.raw")
    sbr = extract_sbr(time_s, v_outp, v_outn)

    write_tran_csv(pout / f"sbr_{tag}.csv", time_s, v_outp, v_outn, v_inp, v_inn)
    write_sbr_taps_csv(pout / f"sbr_taps_{tag}.csv", sbr)
    plot_sbr(time_s, v_outp, v_outn, v_inp, v_inn, sbr, pout / f"sbr_{tag}.png")

    return sbr


def run_pass(
    pass_name: str,
    dut_rel: str,
    params: VgaParams,
    *,
    run_tran_sbr: bool = True,
) -> list[VgaSettingMetrics]:
    """Full VGA test suite for one pass (vga_ideal or vga_pdk)."""
    pout = pass_out(pass_name)
    pout.mkdir(parents=True, exist_ok=True)

    print(f"  [{pass_name}] DC sweep over VCTRL …")
    dc_rows, _ = run_dc_sweep(pass_name, dut_rel, params)

    ac_rows: list[VgaSettingMetrics] = []
    print(f"  [{pass_name}] AC differential at each VCTRL …")
    for vc in params.vctrl_v:
        tag = f"vctrl_{vc:.2f}".replace(".", "p")
        ac_m, _, _, _ = run_ac_at_vctrl(pass_name, dut_rel, params, vc, tag)
        ac_rows.append(ac_m)
        print(
            f"    VCTRL={vc:.2f} V: DC={ac_m.dc_gain_db:.2f} dB "
            f"28G={ac_m.ac_gain_28g_db:.2f} dB "
            f"peak@28G={ac_m.peaking_db:.2f} dB "
            f"f_-3dB={ac_m.f_3db_hz/1e9:.1f} GHz"
        )

    # Merge DC bias into AC rows for table
    merged: list[VgaSettingMetrics] = []
    dc_by_vc = {round(d.vctrl_v, 4): d for d in dc_rows}
    for ac in ac_rows:
        dc = dc_by_vc.get(round(ac.vctrl_v, 4))
        if dc:
            ac.vce_v = dc.vce_v
            ac.vds_tail_v = dc.vds_tail_v
            ac.vgs_tail_v = dc.vgs_tail_v
            ac.ic_signal_a = dc.ic_signal_a
            ac.ic_dummy_a = dc.ic_dummy_a
            ac.id_tail_signal_a = dc.id_tail_signal_a
            ac.id_tail_dummy_a = dc.id_tail_dummy_a
            ac.id_tail_a = dc.id_tail_a
            ac.vout_cm_v = dc.vout_cm_v
            ac.steer_ok = dc.steer_ok
        merged.append(ac)

    write_gain_table(pout / "gain_vs_vctrl_table.csv", dc_rows, ac_rows)
    write_gain_vs_vctrl_csv(pout / "gain_vs_vctrl.csv", merged)
    plot_gain_vs_vctrl(merged, pout / "gain_vs_vctrl.png")

    mid_vc = params.vctrl_v[len(params.vctrl_v) // 2]
    mid_ac = next(s for s in merged if abs(s.vctrl_v - mid_vc) < 1e-6)
    print(f"  [{pass_name}] CMRR/PSRR at mid VCTRL={mid_vc:.2f} V …")
    cmrr_db, psrr_db = run_cm_psrr(pass_name, dut_rel, params, mid_vc, mid_ac.dc_gain_db)

    sim_m = SimMetrics(
        pass_name=pass_name,
        dc_gain_db=mid_ac.dc_gain_db,
        peaking_db=mid_ac.peaking_db,
        cmrr_db=cmrr_db,
        psrr_db=psrr_db,
        vce_v=mid_ac.vce_v,
        vds_tail_v=mid_ac.vds_tail_v,
        vgs_tail_v=mid_ac.vgs_tail_v,
        ic_a=mid_ac.ic_signal_a,
        id_tail_a=mid_ac.id_tail_a,
        peak_gain_db=mid_ac.peak_gain_db,
        f_peak_hz=mid_ac.f_peak_hz,
        f_3db_hz=mid_ac.f_3db_hz,
    )
    write_pass_metrics(pout / "metrics.csv", sim_m)

    eye_mid: EyeMetrics | None = None
    phase_ok: tuple[bool, str] | None = None
    if run_tran_sbr:
        v_min = min(params.vctrl_v)
        v_max = max(params.vctrl_v)
        for vc in params.vctrl_v:
            tag = f"vctrl_{vc:.2f}".replace(".", "p")
            print(f"  [{pass_name}] transient + SBR @ VCTRL={vc:.2f} V …")
            run_tran(pass_name, dut_rel, params, vc, tag)
            run_sbr(pass_name, dut_rel, params, vc, tag)
            # CTLE-style aliases for min / mid / max gain settings.
            if vc == v_min:
                _alias_tran_sbr(pout, tag, "min")
            elif vc == v_max:
                _alias_tran_sbr(pout, tag, "max")
            elif abs(vc - mid_vc) < 1e-6:
                _alias_tran_sbr(pout, tag, "mid")
                tran_csv = pout / f"tran_{tag}.csv"
                if tran_csv.is_file():
                    from vga_analysis import read_tran_csv

                    time_s, v_outp, v_outn, v_inp, v_inn = read_tran_csv(tran_csv)
                    eye_mid = compute_eye_metrics(time_s, v_outp, v_outn)
                    phase_ok, phase_summary, _, _ = verify_eye_phase_invariance(
                        time_s, v_outp, v_outn,
                    )
                    print(f"    eye phase-invariance: {phase_summary} ({'PASS' if phase_ok else 'FAIL'})")

        if eye_mid is not None:
            rows = []
            if phase_ok is not None:
                rows = [
                    ["eye_phase_invariance_ok", "yes" if phase_ok else "no"],
                    ["eye_phase_invariance", phase_summary],
                ]
            write_pass_metrics(pout / "metrics.csv", sim_m, eye=eye_mid)
            if rows:
                with (pout / "metrics.csv").open("a", newline="") as f:
                    csv.writer(f).writerows(rows)

    return merged


def _wait_for_ind_shunt(spice_dir: Path, timeout_s: float = 600.0, poll_s: float = 5.0) -> bool:
    target = spice_dir / "ind_shunt.inc"
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if target.is_file() and target.stat().st_size > 50:
            return True
        time.sleep(poll_s)
    return target.is_file()


def run(
    *,
    ideal: bool = True,
    pdk: bool = True,
    no_tran: bool = False,
    params: VgaParams | None = None,
) -> VgaParams:
    """Run VGA simulations; returns sizing used."""
    if params is None:
        params = size_vga_for_chain()
    print_summary(params)

    spice_dir = _spice_dir()
    if ideal:
        print("=== VGA ideal pass ===")
        run_pass("vga_ideal", "vga_ideal.cir", params, run_tran_sbr=not no_tran)

    if pdk:
        if not _wait_for_ind_shunt(spice_dir):
            print("WARNING: spice/ind_shunt.inc not found — skipping vga_pdk pass")
        else:
            print("=== VGA PDK pass ===")
            run_pass("vga_pdk", "vga_pdk.cir", params, run_tran_sbr=not no_tran)

    return params


def main() -> None:
    parser = argparse.ArgumentParser(description="Run 56G CML VGA ngspice suite")
    parser.add_argument("--no-ideal", action="store_true")
    parser.add_argument("--no-pdk", action="store_true")
    parser.add_argument("--no-tran", action="store_true", help="Skip PRBS + SBR transient")
    parser.add_argument("--rs", type=float, default=None, help="Override RS (Ω)")
    args = parser.parse_args()

    params = size_vga_for_chain()
    if args.rs is not None:
        params = size_vga(
            vbase=params.vbase,
            tail_vds_v=params.vbase - params.vbe,
            rs_ohm=args.rs,
        )
    run(
        ideal=not args.no_ideal,
        pdk=not args.no_pdk,
        no_tran=args.no_tran,
        params=params,
    )


if __name__ == "__main__":
    main()
