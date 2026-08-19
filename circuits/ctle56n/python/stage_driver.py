#!/usr/bin/env python3
"""Run ngspice verification for 56G output pad driver (driver_pdk.cir).

Artifacts: circuits/ctle56n/out/driver/
Receiver: floating 100 Ohm differential across outp/outn (testbench only).
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[3]
_EXP = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from char.common.lut import load_lut  # noqa: E402

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
    write_prbs_stim,
    write_sbr_stim,
    write_sbr_taps_csv,
    write_tran_csv,
)
from ctlelib.ngs import apply_params, complex_from_vm_vp  # noqa: E402
from ctlelib.metrics import AC_PLOT_FMAX_HZ, AC_PLOT_FMIN_HZ, verify_eye_phase_invariance  # noqa: E402
from ctlelib.stim import (  # noqa: E402
    BIT_RATE_HZ,
    EDGE_S,
    PRBS9_BITS,
    PRBS9_POLY,
    UI_S,
    prbs9_bits,
)
from size_ctle import RE_VBIC_SCALE, hbt_caps_at_bias, miller_cin  # noqa: E402
from size_driver import (  # noqa: E402
    DriverParams,
    ITAIL_TARGET_A,
    R_EFF_AC_SE_OHM,
    _repo_paths,
    extra_params,
    print_summary,
    size_driver,
)
from size_term import Z0_DIFF_OHM, measure_esd_leak_a  # noqa: E402

NYQUIST_HZ = 28e9
DRIVER_DUT_NAME = "driver_dut"
EYE_TARGET_MV = 400.0
EYE_HEIGHT_TARGET_MV = 200.0
EYE_WIDTH_TARGET_UI = 0.5
DRIVE_SWEEP_MV = [100, 150, 200, 300, 400, 500]
VT_V = 0.02585
PAD_CM_OFFSET_ACCEPTED = True  # Ic ~5% above nominal; no ITAIL trim (characterization pass)

DRIVER_DC_SAVE_LINES = (
    "save v(outp) v(outn) v(inp) v(inn) v(vdd)\n"
    "save v(xu1.em) v(xu1.mgate) v(xu1.nlp1) v(xu1.nlp2)\n"
    "save @q.xu1.xq1.qnpn13g2[ic] @q.xu1.xq2.qnpn13g2[ic]\n"
    "save @n.xu1.xtail.nsg13_lv_nmos[ids]\n"
    "save @n.xu1.xmdiode.nsg13_lv_nmos[ids]"
)
DRIVER_DC_PRINT_LINES = (
    "print v(outp) v(outn) v(inp) v(inn) v(vdd)\n"
    "print v(xu1.em) v(xu1.mgate) v(xu1.nlp1) v(xu1.nlp2)\n"
    "print @q.xu1.xq1.qnpn13g2[ic] @q.xu1.xq2.qnpn13g2[ic]\n"
    "print @n.xu1.xtail.nsg13_lv_nmos[ids]\n"
    "print @n.xu1.xmdiode.nsg13_lv_nmos[ids]"
)


@dataclass
class DriverSimMetrics:
    vpad_cm_v: float
    ve_v: float
    vce_q1_v: float
    vce_q2_v: float
    vds_tail_v: float
    vgs_tail_v: float
    ic_q1_a: float
    ic_q2_a: float
    id_tail_a: float
    rd_on_chip_ohm: float
    m_realized: float
    r_eff_ac_se_ohm: float
    swing_pad_mv: float
    eye_height_margin_pct: float
    eye_width_margin_pct: float
    s11_dc_db: float
    s11_28g_db: float
    esd_leak_pa: float | None
    driver_cin_sim_ff: float | None
    sbr: SbrResult | None = None
    eye: EyeMetrics | None = None


def driver_out() -> Path:
    return _EXP / "out" / "driver"


def _pwl_points(bits: list[int], vbase: float, se_hi: float, se_lo: float) -> tuple[list[float], list[float]]:
    def val(bit: int) -> float:
        return se_hi if bit else se_lo

    times = [0.0]
    vals = [val(bits[0])]
    for k in range(1, len(bits)):
        t_edge = k * UI_S
        if bits[k] != bits[k - 1]:
            if times[-1] < t_edge - 1e-18:
                times.append(t_edge)
                vals.append(val(bits[k - 1]))
            times.append(t_edge + EDGE_S)
            vals.append(val(bits[k]))
    t_end = len(bits) * UI_S
    if times[-1] < t_end - 1e-18:
        times.append(t_end)
        vals.append(val(bits[-1]))
    return times, vals


def _fmt_pwl(name: str, node: str, times: list[float], vals: list[float]) -> list[str]:
    lines = [f"{name} {node} 0 PWL("]
    row: list[str] = []
    for i, (t, v) in enumerate(zip(times, vals)):
        row.append(f"{t:.6e} {v:.9g}")
        if len(row) == 6 or i == len(times) - 1:
            lines.append("+ " + " ".join(row))
            row = []
    lines.append("+)")
    return lines


def write_prbs_stim_swing(
    path: Path,
    vbase: float,
    swing_diff_mv: float,
    n_bits: int = PRBS9_BITS,
) -> float:
    """PRBS9 NRZ with arbitrary differential pp amplitude (mV)."""
    bits = prbs9_bits(n_bits)
    half = (swing_diff_mv * 1e-3) / 4.0
    t_inp, v_inp = _pwl_points(bits, vbase, vbase + half, vbase - half)
    t_inn, v_inn = _pwl_points(bits, vbase, vbase - half, vbase + half)
    lines = [
        f"* PRBS9 ({PRBS9_POLY}) NRZ — drive sweep {swing_diff_mv:.0f} mVpp,diff",
        f"* {n_bits} bits @ {BIT_RATE_HZ / 1e9:.0f} Gb/s",
        *_fmt_pwl("Vp", "inp", t_inp, v_inp),
        *_fmt_pwl("Vn", "inn", t_inn, v_inn),
    ]
    path.write_text("\n".join(lines) + "\n")
    return len(bits) * UI_S


def _eye_margins(eye: EyeMetrics) -> tuple[float, float, float]:
    swing_m = (eye.pp_swing_mV / EYE_TARGET_MV - 1.0) * 100.0
    height_m = (eye.height_mV / EYE_HEIGHT_TARGET_MV - 1.0) * 100.0
    width_m = (eye.width_ui / EYE_WIDTH_TARGET_UI - 1.0) * 100.0
    return swing_m, height_m, width_m


def _run_tran_eye(
    swing_in_mv: float,
    params: DriverParams,
    ep: dict[str, str],
    work: Path,
    spice_dir: Path,
    dut_cir: Path,
    models: Path,
    tag: str,
) -> EyeMetrics:
    tmax = write_prbs_stim_swing(work / f"prbs_{tag}.inc", params.vbase, swing_in_mv)
    ep_tran = {**ep, "TMAX": f"{tmax:.6e}"}
    tb_tran = _prepare_driver_tb(
        spice_dir / "tb_tran.cir", dut_cir, work, models, spice_dir, ep_tran,
    )
    tb_tran.write_text(
        tb_tran.read_text().replace(
            "prbs_stim.inc",
            f"prbs_{tag}.inc",
        )
    )
    run_ngspice(tb_tran, work, f"tran_{tag}.log")
    time_s, v_outp, v_outn, _, _ = parse_tran_raw(work / "tran.raw")
    return compute_eye_metrics(time_s, v_outp, v_outn)


def plot_drive_sweep(rows: list[dict[str, float]], path: Path) -> None:
    import matplotlib.pyplot as plt

    x = [r["input_swing_mv"] for r in rows]
    y = [r["pad_swing_mv"] for r in rows]
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.plot(x, y, "o-", lw=1.5, ms=7, label="Pad pp swing (sim)")
    ax.plot(x, x, "--", color="gray", lw=0.9, label="Ideal 0 dB")
    ax.axhline(EYE_TARGET_MV, color="orange", ls=":", lw=0.9, label="400 mVpp target")
    ax.set_xlabel("Input differential swing (mVpp)")
    ax.set_ylabel("Pad differential swing (mVpp)")
    ax.set_title("Driver large-signal transfer (floating 100 Ω load)")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)
    fig.savefig(path, dpi=120)
    plt.close(fig)


def run_drive_sweep(
    params: DriverParams,
    ep: dict[str, str],
    work: Path,
    spice_dir: Path,
    dut_cir: Path,
    models: Path,
    pout: Path,
    sweep_mv: list[float] | None = None,
) -> list[dict[str, float]]:
    """Large-signal PRBS sweep; writes drive_sweep.csv and drive_sweep.png."""
    sweep_mv = sweep_mv or DRIVE_SWEEP_MV
    rows: list[dict[str, float]] = []
    for mv in sweep_mv:
        tag = f"{int(mv)}mv"
        eye = _run_tran_eye(mv, params, ep, work, spice_dir, dut_cir, models, tag)
        sm, hm, wm = _eye_margins(eye)
        rows.append({
            "input_swing_mv": float(mv),
            "pad_swing_mv": eye.pp_swing_mV,
            "eye_height_mv": eye.height_mV,
            "eye_width_ui": eye.width_ui,
            "swing_margin_pct": sm,
            "height_margin_pct": hm,
            "width_margin_pct": wm,
        })
        print(
            f"  drive {mv:.0f} mVpp in → pad {eye.pp_swing_mV:.1f} mVpp  "
            f"h={eye.height_mV:.1f} mV  w={eye.width_ui:.3f} UI"
        )

    csv_path = pout / "drive_sweep.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "input_swing_mVpp", "pad_swing_mVpp", "eye_height_mV", "eye_width_UI",
            "swing_margin_pct_vs_400mV", "height_margin_pct_vs_200mV",
            "width_margin_pct_vs_0p5UI",
        ])
        for r in rows:
            w.writerow([
                f"{r['input_swing_mv']:.1f}",
                f"{r['pad_swing_mv']:.2f}",
                f"{r['eye_height_mv']:.2f}",
                f"{r['eye_width_ui']:.4f}",
                f"{r['swing_margin_pct']:.1f}",
                f"{r['height_margin_pct']:.1f}",
                f"{r['width_margin_pct']:.1f}",
            ])
    plot_drive_sweep(rows, pout / "drive_sweep.png")
    return rows


def _interp_input_for_pad_mv(rows: list[dict[str, float]], target_mv: float) -> float | None:
    """Linear interpolate input mVpp for target pad swing."""
    pts = sorted(rows, key=lambda r: r["input_swing_mv"])
    for i in range(len(pts) - 1):
        y0, y1 = pts[i]["pad_swing_mv"], pts[i + 1]["pad_swing_mv"]
        if (y0 - target_mv) * (y1 - target_mv) <= 0 and y1 != y0:
            x0, x1 = pts[i]["input_swing_mv"], pts[i + 1]["input_swing_mv"]
            return x0 + (target_mv - y0) * (x1 - x0) / (y1 - y0)
    if pts and pts[-1]["pad_swing_mv"] >= target_mv:
        return pts[-1]["input_swing_mv"]
    return None


def compute_nx_trade(
    ic_a: float = ITAIL_TARGET_A / 2.0,
    r_eff_ohm: float = R_EFF_AC_SE_OHM,
    pad_target_mv: float = EYE_TARGET_MV,
) -> list[dict[str, float]]:
    """Arithmetic Nx trade (no netlist change): re, gain, drive for 400 mVpp pad."""
    paths = _repo_paths()
    arrays, _ = load_lut(paths["bjt"])
    rows: list[dict[str, float]] = []
    for nx in (2, 4, 8):
        nx_idx = int(np.argmin(np.abs(arrays["Nx"] - nx)))
        re = RE_VBIC_SCALE * 4.0 / nx
        vbe, ic_lut, gm_lut, _, cbe, cbc, _, _ = hbt_caps_at_bias(
            paths["bjt"], nx_idx=nx_idx, vce_min=0.9, vce_max=1.15,
        )
        ic = ic_a
        gm_i = ic / VT_V
        gm_eff = gm_i / (1.0 + gm_i * re)
        gain = gm_eff * r_eff_ohm
        drive_mv = pad_target_mv / gain if gain > 0 else float("inf")
        av = gm_lut * r_eff_ohm
        cin_ff = miller_cin(cbe, cbc, av) * 1e15
        rows.append({
            "nx": float(nx),
            "re_ohm": re,
            "gm_i_mS": gm_i * 1e3,
            "gm_eff_mS": gm_eff * 1e3,
            "gain_linear": gain,
            "gain_db": 20.0 * math.log10(max(gain, 1e-12)),
            "drive_for_400mVpp_mv": drive_mv,
            "cin_miller_ff": cin_ff,
            "ic_mA": ic * 1e3,
        })
    return rows


def print_nx_trade(rows: list[dict[str, float]]) -> None:
    print("\n=== Nx trade (same ITAIL, not implemented) ===")
    print(
        "  Nx   re(Ω)  gm_i(mS)  gm_eff(mS)  gain   drive@400mVpp   C_in(Miller)"
    )
    for r in rows:
        print(
            f"  {int(r['nx']):1d}   {r['re_ohm']:5.2f}   "
            f"{r['gm_i_mS']:6.1f}    {r['gm_eff_mS']:6.1f}     "
            f"{r['gain_linear']:.2f}   {r['drive_for_400mVpp_mv']:6.0f} mVpp    "
            f"{r['cin_miller_ff']:.1f} fF"
        )


def write_nx_trade_csv(rows: list[dict[str, float]], path: Path) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "Nx", "re_ohm", "Ic_mA", "gm_i_mS", "gm_eff_mS", "gain_linear", "gain_dB",
            "drive_for_400mVpp_mV", "Cin_Miller_fF",
        ])
        for r in rows:
            w.writerow([
                int(r["nx"]), f"{r['re_ohm']:.3f}", f"{r['ic_mA']:.3f}",
                f"{r['gm_i_mS']:.2f}", f"{r['gm_eff_mS']:.2f}",
                f"{r['gain_linear']:.4f}", f"{r['gain_db']:.3f}",
                f"{r['drive_for_400mVpp_mv']:.1f}", f"{r['cin_miller_ff']:.2f}",
            ])


def _write_work_params(work: Path, ep: dict[str, str]) -> None:
    skip = {"IND_SHUNT_INC"}
    lines = [f".param {k}={v}" for k, v in sorted(ep.items()) if k not in skip]
    (work / "params.inc").write_text("\n".join(lines) + "\n")


def _inject_floating_load(text: str, rdiff: str = "100") -> str:
    """Insert floating differential termination after DUT instance."""
    if "Rterm" in text:
        return text
    return re.sub(
        r"(XU1 outp outn inp inn vdd \{DUT_NAME\}|XU1 outp outn inp inn vdd driver_dut)\n",
        r"\1\n* Floating 100 Ohm differential receiver (TB only)\nRterm outp outn "
        + rdiff
        + "\n",
        text,
        count=1,
    )


def _patch_tb_nodeset(tb_path: Path, ep: dict[str, str]) -> None:
    vdd = float(ep["VDD"])
    vbe = float(ep["VBE"])
    vbase = float(ep["VBASE"])
    itail = float(ep["ITAIL"])
    rd = float(ep.get("RSIL_R", "50"))
    ve = vbase - vbe
    vcoll = vdd - (itail / 2.0) * rd
    text = tb_path.read_text()
    text = re.sub(
        r"\.nodeset[^\n]*",
        f".nodeset v(xu1.mgate)={ep['MOS_VGS']} "
        f"v(xu1.em)={ve:.4f} "
        f"v(outp)={vcoll:.4f} v(outn)={vcoll:.4f}",
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
    text = _inject_floating_load(tb.read_text(), ep.get("RDIFF_TB", "100"))
    tb.write_text(text)
    _patch_tb_nodeset(tb, ep)
    return tb


def _parse_zin_pad_raw(raw: Path) -> tuple[np.ndarray, np.ndarray]:
    """Z_diff = |V(outp)-V(outn)| with 1 A AC current source I(Iac)."""
    rows = []
    with raw.open() as f:
        for line in f:
            parts = line.split()
            if parts:
                rows.append([float(x) for x in parts])
    arr = np.asarray(rows)
    freq = arr[:, 0]
    if arr.shape[1] >= 7:
        vop = complex_from_vm_vp(arr[:, 3], arr[:, 4])
        von = complex_from_vm_vp(arr[:, 5], arr[:, 6])
    elif arr.shape[1] >= 5:
        vop = complex_from_vm_vp(arr[:, 1], arr[:, 2])
        von = complex_from_vm_vp(arr[:, 3], arr[:, 4])
    else:
        raise RuntimeError(f"{raw}: unexpected pad Z wrdata width {arr.shape[1]}")
    vdiff = vop - von
    zdiff = np.abs(vdiff)  # Iac = 1 A
    return freq, zdiff


def _return_loss_db(z: np.ndarray, z0: float = Z0_DIFF_OHM) -> np.ndarray:
    """Return loss in dB referenced to z0 differential (-20*log10|S11|)."""
    s11 = (z - z0) / (z + z0)
    return -20.0 * np.log10(np.maximum(np.abs(s11), 1e-30))


def plot_s11_pad(freq: np.ndarray, s11_db: np.ndarray, path: Path) -> None:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.semilogx(freq, s11_db, "b-", lw=1.2)
    ax.axhline(-10, color="gray", ls="--", lw=0.8, label="-10 dB")
    ax.axvline(NYQUIST_HZ, color="orange", ls=":", lw=0.8)
    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("Return loss (dB, 100 Ohm diff ref)")
    ax.set_title("Pad return loss (outp-outn, floating 100 Ohm load)")
    ax.set_xlim(AC_PLOT_FMIN_HZ, AC_PLOT_FMAX_HZ)
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


def _measure_driver_cin_ff(work: Path, models: Path, dut_cir: Path, ep: dict[str, str]) -> float | None:
    """Y11-based input C at inp with pads terminated (floating 100 Ohm)."""
    cir = work / "zin_driver_in.cir"
    dut_local = work / dut_cir.name
    text = (
        ".include params.inc\n"
        f".include {dut_local.resolve()}\n"
        f"Vdd vdd 0 dc {ep['VDD']}\n"
        f"XU1 outp outn inp inn vdd {DRIVER_DUT_NAME}\n"
        f"Rterm outp outn {ep.get('RDIFF_TB', '100')}\n"
        f"Vp inp 0 dc {ep['VBASE']} ac 1 0\n"
        f"Vn inn 0 dc {ep['VBASE']} ac 1 180\n"
        ".options gmin=1e-18\n"
        ".control\n"
        "save v(inp) v(inn) i(vp)\n"
        "ac dec 50 1e9 50e9\n"
        "set wr_singlescale\n"
        "wrdata zin_in.raw frequency vm(inp) vp(inp) vm(i(vp)) vp(i(vp))\n"
        ".endc\n.end\n"
    )
    cir.write_text(apply_params(text, work.parent.parent / "spice", ep).replace(
        "{PDK_MODELS}", str(models)
    ))
    try:
        run_ngspice(cir, work, "zin_in.log")
    except RuntimeError:
        return None
    rows = []
    with (work / "zin_in.raw").open() as f:
        for line in f:
            parts = line.split()
            if parts:
                rows.append([float(x) for x in parts])
    arr = np.asarray(rows)
    if arr.shape[1] < 5:
        return None
    freq = arr[:, 0]
    vm = arr[:, 1]
    vp = arr[:, 2]
    im = arr[:, 3]
    ip = arr[:, 4]
    vin = complex_from_vm_vp(vm, vp)
    iin = complex_from_vm_vp(im, ip)
    idx = int(np.argmin(np.abs(freq - 1e9)))
    y11 = iin[idx] / vin[idx] if abs(vin[idx]) > 1e-30 else 0.0
    return float(np.imag(y11) / (2.0 * math.pi * freq[idx]) * 1e15)


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
        ["RD_on_chip_target_ohm", f"{params.rd_on_chip_ohm:.3f}"],
        ["RD_on_chip_realized_ohm", f"{sim.rd_on_chip_ohm:.3f}"],
        ["R_eff_AC_half_circuit_ohm", f"{sim.r_eff_ac_se_ohm:.3f}"],
        ["L_pH", f"{params.l_h * 1e12:.2f}"],
        ["L_EM_case", params.l_em_case],
        ["CL_pad_fF", f"{params.cl_pad_f * 1e15:.2f}"],
        ["m_realized", f"{sim.m_realized:.4f}"],
        ["m_bessel_target", f"{params.m_bessel_target:.3f}"],
        ["L_bessel_target_pH", f"{params.l_bessel_target_ph:.2f}"],
        ["driver_Cin_Miller_fF", f"{params.driver_cin_ff:.2f}"],
        ["driver_Cin_sim_fF", f"{sim.driver_cin_sim_ff:.2f}" if sim.driver_cin_sim_ff else ""],
        ["vga_FO2_fF", f"{params.vga_fo2_ff:.2f}"],
        ["vga_bw_penalty_pct", f"{params.vga_bw_penalty_pct:.1f}"],
        ["Vpad_CM_V", f"{sim.vpad_cm_v:.4f}"],
        ["Vem_V", f"{sim.ve_v:.4f}"],
        ["VCE_Q1_V", f"{sim.vce_q1_v:.4f}"],
        ["VCE_Q2_V", f"{sim.vce_q2_v:.4f}"],
        ["VDS_tail_V", f"{sim.vds_tail_v:.4f}"],
        ["Ic_Q1_A", f"{sim.ic_q1_a:.6g}"],
        ["Ic_Q2_A", f"{sim.ic_q2_a:.6g}"],
        ["Id_tail_A", f"{sim.id_tail_a:.6g}"],
        ["ESD_leak_pA", f"{sim.esd_leak_pa:.3f}" if sim.esd_leak_pa is not None else ""],
        ["dc_gain_dB", f"{ac_m.dc_gain_db:.3f}"],
        ["ac_gain_28G_dB", f"{ac_m.peaking_db + ac_m.dc_gain_db:.3f}"],
        ["peaking_28G_dB", f"{ac_m.peaking_db:.3f}"],
        ["G_peak_dB", f"{ac_m.peak_gain_db:.3f}"],
        ["f_peak_Hz", f"{ac_m.f_peak_hz:.6g}"],
        ["f_3dB_Hz", f"{ac_m.f_3db_hz:.6g}"],
        ["pad_swing_pp_mV", f"{sim.swing_pad_mv:.2f}"],
        ["return_loss_dc_dB", f"{sim.s11_dc_db:.3f}"],
        ["return_loss_28GHz_dB", f"{sim.s11_28g_db:.3f}"],
    ]
    if sim.eye:
        rows += eye_metrics_rows("pad", sim.eye)
        rows.append(["eye_height_margin_pct", f"{sim.eye_height_margin_pct:.1f}"])
        rows.append(["eye_width_margin_pct", f"{sim.eye_width_margin_pct:.1f}"])
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

    params = size_driver()
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
    ve = dc.get("v(xu1.em)", float("nan"))
    vout_p = dc.get("v(outp)", 0)
    vout_n = dc.get("v(outn)", 0)
    ic1 = dc.get("@q.xu1.xq1.qnpn13g2[ic]", float("nan"))
    ic2 = dc.get("@q.xu1.xq2.qnpn13g2[ic]", float("nan"))
    vce1 = vout_p - ve if not math.isnan(ve) else float("nan")
    vce2 = vout_n - ve if not math.isnan(ve) else float("nan")
    vds_tail = ve if not math.isnan(ve) else float("nan")
    vgs_tail = dc.get("v(xu1.mgate)", 0.85)
    id_tail = dc.get("@n.xu1.xtail.nsg13_lv_nmos[ids]", float("nan"))

    rd_real = float(ep.get("RSIL_R", params.rsil_r_ohm))
    m_real = params.l_h / (rd_real ** 2 * params.cl_pad_f)

    esd_leak = measure_esd_leak_a(params.vdd, vpad_cm)
    esd_leak_pa = esd_leak * 1e12 if not math.isnan(esd_leak) else None

    cin_sim = _measure_driver_cin_ff(work, models, dut_cir, ep)

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
        vce_v=min(vce1, vce2) if not math.isnan(vce1) else float("nan"),
        vds_tail_v=vds_tail,
        vgs_tail_v=vgs_tail,
        ic_a=(ic1 + ic2) / 2.0,
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
    s11_db = _return_loss_db(zdiff)
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
        eye_ok, eye_inv_summary, _, _ = verify_eye_phase_invariance(
            time_s, v_outp, v_outn,
        )
        print(f"  Eye phase invariance: {'PASS' if eye_ok else 'FAIL'} — {eye_inv_summary}")

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
        ve_v=ve,
        vce_q1_v=vce1,
        vce_q2_v=vce2,
        vds_tail_v=vds_tail,
        vgs_tail_v=vgs_tail,
        ic_q1_a=ic1,
        ic_q2_a=ic2,
        id_tail_a=id_tail,
        rd_on_chip_ohm=rd_real,
        m_realized=m_real,
        r_eff_ac_se_ohm=R_EFF_AC_SE_OHM,
        swing_pad_mv=swing_pad_mv,
        eye_height_margin_pct=h_margin,
        eye_width_margin_pct=w_margin,
        s11_dc_db=s11_dc,
        s11_28g_db=s11_28,
        esd_leak_pa=esd_leak_pa,
        driver_cin_sim_ff=cin_sim,
        sbr=sbr_result,
        eye=eye_result,
    )
    write_driver_metrics(pout / "metrics.csv", params, sim, ac_m)

    print("\n=== Driver sim summary ===")
    print(f"  Pad CM={vpad_cm:.4f} V  VCE Q1={vce1:.3f} V  Q2={vce2:.3f} V")
    print(f"  Ic Q1={ic1*1e3:.2f} mA  Q2={ic2*1e3:.2f} mA  Id_tail={id_tail*1e3:.2f} mA")
    print(f"  RD_on_chip={rd_real:.2f} Ω  R_eff_AC={R_EFF_AC_SE_OHM:.1f} Ω")
    print(
        f"  AC DC={dc_gain_db:.2f} dB  28G={ac_gain_28g:.2f} dB  "
        f"peaking={peaking_db:.2f} dB  m={m_real:.3f}"
    )
    print(f"  Return loss pad: DC={s11_dc:.2f} dB  28G={s11_28:.2f} dB (100 Ω diff ref)")
    if esd_leak_pa is not None:
        print(f"  ESD leak={esd_leak_pa:.2f} pA/pad (reverse-biased)")
    if cin_sim:
        print(f"  Driver C_in(sim)={cin_sim:.1f} fF  (Miller budget {params.driver_cin_ff:.1f} fF)")
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
    parser.add_argument(
        "--drive-sweep",
        action="store_true",
        help="Run input amplitude sweep (implies transient sims)",
    )
    args = parser.parse_args()

    params, sim = run(no_tran=args.no_tran and not args.drive_sweep)

    if args.drive_sweep or not args.no_tran:
        spice_dir = _EXP / "spice"
        pout = driver_out()
        work = pout / "work"
        ep = extra_params(params)
        models = pdk_models()
        dut_cir = spice_dir / "driver_pdk.cir"

        print("\n=== Drive amplitude sweep ===")
        sweep_rows = run_drive_sweep(
            params, ep, work, spice_dir, dut_cir, models, pout,
        )
        drive_400 = _interp_input_for_pad_mv(sweep_rows, EYE_TARGET_MV)
        if drive_400 is not None:
            closest = min(sweep_rows, key=lambda r: abs(r["input_swing_mv"] - drive_400))
            print(
                f"\n  Pad reaches ~{EYE_TARGET_MV:.0f} mVpp at "
                f"~{drive_400:.0f} mVpp input (interp)"
            )
            print(
                f"  At nearest sweep point ({closest['input_swing_mv']:.0f} mVpp in): "
                f"pad={closest['pad_swing_mv']:.1f} mVpp  "
                f"height margin={closest['height_margin_pct']:+.0f}%  "
                f"width margin={closest['width_margin_pct']:+.0f}%  "
                f"width={closest['eye_width_ui']:.3f} UI"
            )
        else:
            last = sweep_rows[-1]
            print(
                f"\n  Pad does not reach {EYE_TARGET_MV:.0f} mVpp by "
                f"{last['input_swing_mv']:.0f} mVpp input "
                f"(max pad {last['pad_swing_mv']:.1f} mVpp)"
            )

        nx_rows = compute_nx_trade(ic_a=sim.ic_q1_a)
        print_nx_trade(nx_rows)
        write_nx_trade_csv(nx_rows, pout / "nx_trade.csv")

        chain_note = (
            f"\n=== Chain gain budget note ===\n"
            f"  PRBS at driver input is 100 mVpp,diff today → pad ~{sweep_rows[0]['pad_swing_mv']:.0f} mVpp "
            f"(~0 dB stage gain).\n"
        )
        if drive_400:
            chain_note += (
                f"  Full 400 mVpp pad swing needs ~{drive_400:.0f} mVpp at the driver input "
                f"(intrinsic re degenerates the pair; not ~200 mVpp ideal tanh).\n"
                f"  End-to-end chain gain must rise by ~{20 * math.log10(drive_400 / 100):.1f} dB "
                f"before the driver can deliver 400 mVpp — optimization-pass item, not fixed here."
            )
        print(chain_note)

        if PAD_CM_OFFSET_ACCEPTED:
            print(
                f"\n  Pad CM offset accepted: {sim.vpad_cm_v:.4f} V vs 1.400 V target "
                f"(Ic {sim.ic_q1_a * 1e3:.2f} mA/side vs 4.00 mA nominal; no ITAIL trim)."
            )


if __name__ == "__main__":
    main()
