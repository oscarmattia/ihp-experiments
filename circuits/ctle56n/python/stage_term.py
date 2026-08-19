#!/usr/bin/env python3
"""Run ngspice verification for RX termination stage (term_pdk.cir).

Writes artifacts to circuits/ctle56n/out/term/.
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
    compute_ac_peak_metrics,
    compute_eye_metrics,
    extract_sbr,
    eye_metrics_rows,
    verify_eye_phase_invariance,
    group_delay_s,
    interp_db_at,
    parse_ac_raw,
    parse_dc_log,
    parse_tran_raw,
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
    LEGACY_DUT_PORTS,
    LEGACY_NODESET,
)
from ctlelib.ngs import apply_params, complex_from_vm_vp  # noqa: E402
from ctlelib.metrics import AC_PLOT_FMAX_HZ, AC_PLOT_FMIN_HZ, EyeMetrics  # noqa: E402
from size_term import (  # noqa: E402
    NYQUIST_HZ,
    RSRC_LEG_OHM,
    TermParams,
    Z0_DIFF_OHM,
    measure_esd_leak_a,
    print_summary,
    shunt_cap_accounting_ff,
    size_term,
    to_extra,
)

TERM_DUT_NAME = "term_dut"
TERM_DC_SAVE_LINES = "save v(outp) v(outn) v(inp) v(inn) v(vdd) v(xu1.vtt)"
TERM_DC_PRINT_LINES = (
    "print v(outp) v(outn) v(inp) v(inn) v(vdd) v(xu1.vtt)\n"
    "print (v(vdd)-v(xu1.vtt))/273.7"
)


@dataclass
class TermMetrics:
    vcm_pad_v: float
    vtt_v: float
    idiv_ma: float
    esd_leak_a: float | None
    il_dc_db: float
    il_28g_db: float
    f_il_3db_hz: float
    s11_dc_db: float
    s11_28g_db: float
    f_s11_10db_hz: float
    c_pad_per_side_ff: float
    c_esd_per_side_ff: float
    c_shunt_per_side_ff: float
    c_shunt_diff_ff: float
    c_extracted_diff_ff: float
    f_zin_3db_hz: float
    sbr: SbrResult | None = None
    eye: EyeMetrics | None = None


def term_out() -> Path:
    return _EXP / "out" / "term"


def _write_min_params_inc(work: Path, extra: dict[str, str]) -> None:
    lines = ["* term stage params — from size_term.py extra_params"]
    for k, v in sorted(extra.items()):
        if k in ("CL_TB", "RSRC_LEG", "MOS_VGS"):
            continue
        lines.append(f".param {k}={v}")
    (work / "params.inc").write_text("\n".join(lines) + "\n")


def _write_ac_tb_50ohm(
    work: Path,
    dut_cir: Path,
    models: Path,
    spice_dir: Path,
    extra: dict[str, str],
) -> Path:
    text = f"""* AC differential — 100 Ohm diff source (50 Ohm per leg) into termination
.include params.inc
.include {dut_cir.resolve()}

Vdd vdd 0 dc {{VDD}}
XU1 outp outn inp inn vdd {TERM_DUT_NAME}

Vp vp 0 dc {{VBASE}} ac 0.5 0
Vn vn 0 dc {{VBASE}} ac 0.5 180
Rsrc_p vp inp {{RSRC_LEG}}
Rsrc_n vn inn {{RSRC_LEG}}

Cload_p outp 0 {{CL_TB}}
Cload_n outn 0 {{CL_TB}}

.options gmin=1e-18 abstol=1e-15 reltol=1e-3

.control
save v(outp) v(outn) v(inp) v(inn) v(vp) v(vn)
ac dec 200 1e6 100e9
set wr_singlescale
wrdata ac_diff.raw frequency vm(outp) vp(outp) vm(outn) vp(outn) vm(inp) vp(inp) vm(inn) vp(inn) vm(vp) vp(vp) vm(vn) vp(vn)
.endc
.end
"""
    text = apply_params(text, spice_dir, extra)
    text = text.replace("{PDK_MODELS}", str(models))
    out = work / "tb_ac_diff_term.cir"
    out.write_text(text)
    return out


def _write_dc_tb(
    work: Path,
    dut_cir: Path,
    models: Path,
    spice_dir: Path,
    extra: dict[str, str],
) -> Path:
    text = f"""* DC OP — termination stage
.include params.inc
.include {dut_cir.resolve()}

Vdd vdd 0 dc {{VDD}}
XU1 outp outn inp inn vdd {TERM_DUT_NAME}

Vp inp 0 dc {{VBASE}}
Vn inn 0 dc {{VBASE}}

.options gmin=1e-18 abstol=1e-15 reltol=1e-3

.control
{TERM_DC_SAVE_LINES}
op
echo "--- DC operating point ---"
{TERM_DC_PRINT_LINES}
.endc
.end
"""
    text = apply_params(text, spice_dir, extra)
    out = work / "tb_dc_term.cir"
    out.write_text(text)
    return out


def _patch_stim_source(stim_path: Path, r_leg: float = RSRC_LEG_OHM) -> None:
    """Rewrite Vp inp / Vn inn to drive through r_leg Ohm per leg."""
    text = stim_path.read_text()
    text = text.replace("Vp inp", "Vp vp_drv")
    text = text.replace("Vn inn", "Vn vn_drv")
    extra = (
        f"\n* {r_leg:.0f} Ohm per leg ({2*r_leg:.0f} Ohm differential source)\n"
        f"Rsrc_p vp_drv inp {r_leg:g}\n"
        f"Rsrc_n vn_drv inn {r_leg:g}\n"
    )
    stim_path.write_text(text + extra)


def _write_tran_tb(
    work: Path,
    dut_cir: Path,
    spice_dir: Path,
    extra: dict[str, str],
    stim_name: str,
    raw_name: str,
) -> Path:
    text = f"""* Transient — 50 Ohm source + termination DUT
.include params.inc
.include {dut_cir.resolve()}
.include {stim_name}

Vdd vdd 0 dc {{VDD}}
XU1 outp outn inp inn vdd {TERM_DUT_NAME}

Cload_p outp 0 {{CL_TB}}
Cload_n outn 0 {{CL_TB}}

.options gmin=1e-18 abstol=1e-15 reltol=1e-3

.control
save v(outp) v(outn) v(inp) v(inn)
tran 0.5p {{TMAX}} 0 1p
set wr_singlescale
wrdata {raw_name} time v(outp) v(outn) v(inp) v(inn)
.endc
.end
"""
    text = apply_params(text, spice_dir, extra)
    out = work / f"tb_{raw_name.replace('.raw', '')}.cir"
    out.write_text(text)
    return out


def _parse_zin_raw(raw: Path) -> tuple[np.ndarray, np.ndarray]:
    rows = []
    with raw.open() as f:
        for line in f:
            parts = line.split()
            if parts:
                rows.append([float(x) for x in parts])
    arr = np.asarray(rows)
    if arr.shape[1] >= 7:
        freq = arr[:, 0]
        vinp = complex_from_vm_vp(arr[:, 3], arr[:, 4])
        vinn = complex_from_vm_vp(arr[:, 5], arr[:, 6])
    elif arr.shape[1] >= 5:
        freq = arr[:, 0]
        vinp = complex_from_vm_vp(arr[:, 1], arr[:, 2])
        vinn = complex_from_vm_vp(arr[:, 3], arr[:, 4])
    else:
        raise RuntimeError(f"{raw}: unexpected zin wrdata width {arr.shape[1]}")
    zdiff = np.abs(vinp - vinn)  # passive diff input magnitude (Iac = 1 A)
    return freq, zdiff


def _s11_db(z: np.ndarray, z0: float = Z0_DIFF_OHM) -> np.ndarray:
    s11 = (z - z0) / (z + z0)
    return 20.0 * np.log10(np.maximum(np.abs(s11), 1e-30))


def _crossing_freq_worse(
    freq: np.ndarray, curve_db: np.ndarray, level_db: float
) -> float:
    """First frequency where S11 degrades past level_db (curve rises above threshold)."""
    for i in range(1, len(freq)):
        if curve_db[i - 1] <= level_db and curve_db[i] > level_db:
            f_lo, f_hi = freq[i - 1], freq[i]
            y_lo, y_hi = curve_db[i - 1], curve_db[i]
            if y_hi != y_lo:
                return float(f_lo + (level_db - y_lo) * (f_hi - f_lo) / (y_hi - y_lo))
            return float(f_hi)
    return float("nan")


def _estimate_c_diff_from_z(
    freq: np.ndarray, z: np.ndarray, r_diff: float = Z0_DIFF_OHM
) -> tuple[float, float]:
    """Return (C_diff_F, f_3dB_Hz) from |Z_diff| rolloff above 100 Ohm plateau."""
    z0 = float(np.median(z[freq < 5e9]))
    target = z0 / math.sqrt(2.0)
    f_3db = float("nan")
    for i in range(1, len(freq)):
        if z[i] <= target:
            f_3db = float(freq[i])
            break
    if math.isnan(f_3db) or f_3db <= 0:
        return float("nan"), float("nan")
    c_diff = 1.0 / (2.0 * math.pi * f_3db * r_diff)
    return c_diff, f_3db


def plot_insertion_loss(
    freq: np.ndarray,
    h_db: np.ndarray,
    path: Path,
    *,
    il_dc: float,
    il_28: float,
) -> None:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.semilogx(freq, h_db, "b-", lw=1.2)
    ax.axhline(-6, color="gray", ls="--", lw=0.8, label="-6 dB (100 Ohm diff matched)")
    ax.axvline(NYQUIST_HZ, color="orange", ls=":", lw=0.8)
    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("Insertion loss (dB)")
    ax.set_title(f"vod/vid @ output — DC={il_dc:.1f} dB, 28 GHz={il_28:.1f} dB")
    ax.set_xlim(AC_PLOT_FMIN_HZ, AC_PLOT_FMAX_HZ)
    ax.grid(True, which="both", alpha=0.3)
    ax.legend(fontsize=8, loc="lower left")
    fig.savefig(path, dpi=120)
    plt.close(fig)


def plot_s11(freq: np.ndarray, s11_db: np.ndarray, path: Path) -> None:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.semilogx(freq, s11_db, "b-", lw=1.2)
    ax.axhline(-10, color="gray", ls="--", lw=0.8, label="-10 dB")
    ax.axvline(NYQUIST_HZ, color="orange", ls=":", lw=0.8)
    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("S11 (dB, 100 Ohm diff ref)")
    ax.set_title("Differential return loss at bond pad (100 Ohm reference)")
    ax.set_xlim(AC_PLOT_FMIN_HZ, AC_PLOT_FMAX_HZ)
    ax.grid(True, which="both", alpha=0.3)
    ax.legend(fontsize=8, loc="lower left")
    fig.savefig(path, dpi=120)
    plt.close(fig)


def write_metrics_csv(path: Path, params: TermParams, m: TermMetrics) -> None:
    rows = [
        ["parameter", "value"],
        ["VDD_V", f"{params.vdd:.6g}"],
        ["vtt_V", f"{m.vtt_v:.6g}"],
        ["VBASE_V", f"{params.vbase:.6g}"],
        ["pad_w_um", f"{params.pad_w_um:g}"],
        ["pad_l_um", f"{params.pad_l_um:g}"],
        ["PAD_C_fF", f"{params.pad_c_f * 1e15:.2f}"],
        ["rsil_w_um", f"{params.rsil_w_um:g}"],
        ["rsil_l_um", f"{params.rsil_l_um:g}"],
        ["rsil_R_ohm", f"{params.rsil_r_ohm:.3f}"],
        ["vtt_R_top_ohm", f"{params.vtt_r_top_ohm:.2f}"],
        ["vtt_R_bot_ohm", f"{params.vtt_r_bot_ohm:.2f}"],
        ["Idiv_mA", f"{m.idiv_ma:.4f}"],
        [
            "esd_leak_pA",
            f"{m.esd_leak_a * 1e12:.3f}" if m.esd_leak_a is not None else "",
        ],
        ["vcm_pad_V", f"{m.vcm_pad_v:.4f}"],
        ["IL_dc_dB", f"{m.il_dc_db:.3f}"],
        ["IL_28GHz_dB", f"{m.il_28g_db:.3f}"],
        ["f_IL_3dB_Hz", f"{m.f_il_3db_hz:.6g}"],
        ["S11_dc_dB", f"{m.s11_dc_db:.3f}"],
        ["S11_28GHz_dB", f"{m.s11_28g_db:.3f}"],
        ["f_S11_-10dB_Hz", f"{m.f_s11_10db_hz:.6g}"],
        ["C_pad_per_side_fF", f"{m.c_pad_per_side_ff:.1f}"],
        ["C_esd_per_side_fF", f"{m.c_esd_per_side_ff:.1f}"],
        ["C_shunt_per_side_fF", f"{m.c_shunt_per_side_ff:.1f}"],
        ["C_shunt_diff_fF", f"{m.c_shunt_diff_ff:.1f}"],
        [
            "C_extracted_diff_fF",
            f"{m.c_extracted_diff_ff:.1f}" if not math.isnan(m.c_extracted_diff_ff) else "nan",
        ],
        ["f_Zin_3dB_Hz", f"{m.f_zin_3db_hz:.6g}"],
    ]
    if m.sbr:
        rows += [
            ["sbr_cursor_mV", f"{m.sbr.cursor_mV:.4f}"],
            ["sbr_isi_norm", f"{m.sbr.isi_norm:.6g}"],
            ["sbr_isi_abs", f"{m.sbr.isi_abs:.6g}"],
        ]
    if m.eye:
        rows += eye_metrics_rows("term", m.eye)
    with path.open("w", newline="") as f:
        csv.writer(f).writerows(rows)


def write_s11_csv(path: Path, freq: np.ndarray, s11_db: np.ndarray, z_ohm: np.ndarray) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["freq_Hz", "S11_dB", "Zdiff_ohm"])
        for i in range(len(freq)):
            w.writerow([freq[i], s11_db[i], z_ohm[i]])


def run(
    out_dir: Path | None = None,
    *,
    no_tran: bool = False,
    pad_w_um: float = 70.0,
    pad_l_um: float = 70.0,
    no_pad_cap: bool = False,
) -> tuple[TermParams, TermMetrics]:
    spice_dir = _EXP / "spice"
    pout = out_dir or term_out()
    pout.mkdir(parents=True, exist_ok=True)
    work = pout / "work"
    work.mkdir(parents=True, exist_ok=True)

    pdk = os.environ.get("PDK_ROOT", "")
    if pdk:
        spiceinit = Path(pdk) / "ihp-sg13g2/libs.tech/ngspice/.spiceinit"
        if spiceinit.is_file():
            (work / ".spiceinit").write_bytes(spiceinit.read_bytes())

    pad_override = 0.0 if no_pad_cap else None
    params = size_term(pad_w_um=pad_w_um, pad_l_um=pad_l_um, pad_c_override=pad_override)
    extra = to_extra(params)
    print_summary(params)

    models = pdk_models()
    dut_cir = spice_dir / "term_pdk.cir"
    _write_min_params_inc(work, extra)

    dut_local = work / dut_cir.name
    dut_local.write_text(apply_params(dut_cir.read_text(), spice_dir, extra).replace(
        "{PDK_MODELS}", str(models)
    ))

    # --- DC ---
    tb_dc = _write_dc_tb(work, dut_local, models, spice_dir, extra)
    dc_log = run_ngspice(tb_dc, work, "dc.log")
    dc_vals = parse_dc_log(dc_log)
    (pout / "op.txt").write_text(dc_log.read_text())

    vcm = 0.5 * (dc_vals.get("v(inp)", params.vbase) + dc_vals.get("v(inn)", params.vbase))
    vtt = dc_vals.get("v(xu1.vtt)", params.vbase)
    idiv_meas = dc_vals.get("(v(vdd)-v(xu1.vtt))/273.7", (params.vdd - vtt) / params.vtt_r_top_ohm)
    idiv = float(idiv_meas) * 1e3  # mA
    esd_leak = measure_esd_leak_a(params.vdd, params.vbase)
    if math.isnan(esd_leak):
        esd_leak = None

    cap_acct = shunt_cap_accounting_ff(params.pad_c_f)

    # --- AC insertion loss (100 Ohm diff source, 50 Ohm per leg) ---
    tb_ac = _write_ac_tb_50ohm(work, dut_local, models, spice_dir, extra)
    run_ngspice(tb_ac, work, "ac_diff.log")
    freq, voutp, voutn, vin_p, vin_n = parse_ac_raw(work / "ac_diff.raw")
    # wrdata includes vp/vn after inp/inn (columns 11-14)
    rows = np.loadtxt(work / "ac_diff.raw")
    if rows.ndim == 1:
        rows = rows.reshape(1, -1)
    vvp = complex_from_vm_vp(rows[:, 11], rows[:, 12])
    vvn = complex_from_vm_vp(rows[:, 13], rows[:, 14])
    vsrc = vvp - vvn
    vod = voutp - voutn
    h = np.where(np.abs(vsrc) > 1e-30, vod / vsrc, 0.0)
    h_db = 20.0 * np.log10(np.maximum(np.abs(h), 1e-30))
    il_dc = float(h_db[0])
    il_28 = interp_db_at(freq, h_db, NYQUIST_HZ)
    _, _, f_il_3db, _ = compute_ac_peak_metrics(freq, h_db)
    gd_s = group_delay_s(freq, h)
    plot_insertion_loss(
        freq,
        h_db,
        pout / "ac_diff.png",
        il_dc=il_dc,
        il_28=il_28,
    )
    write_ac_diff_csv(pout / "ac_diff.csv", freq, h_db, gd_s)

    # --- Zin / return loss ---
    tb_zin = prepare_tb(
        spice_dir / "tb_zin.cir",
        dut_cir,
        work,
        models,
        spice_dir,
        extra_params=extra,
        dut_name=TERM_DUT_NAME,
        cl_tb=extra["CL_TB"],
        dc_save_lines=TERM_DC_SAVE_LINES,
        dc_print_lines=TERM_DC_PRINT_LINES,
        dut_ports=LEGACY_DUT_PORTS,
        dut_bias="",
        dut_nodeset=LEGACY_NODESET,
    )
    run_ngspice(tb_zin, work, "zin.log")
    freq_z, zdiff = _parse_zin_raw(work / "zin.raw")
    s11_db = _s11_db(zdiff)
    s11_dc = float(s11_db[0])
    s11_28 = interp_db_at(freq_z, s11_db, NYQUIST_HZ)
    f_s11_10 = _crossing_freq_worse(freq_z, s11_db, -10.0)
    c_extracted, f_zin_3db = _estimate_c_diff_from_z(freq_z, zdiff)
    c_extracted_ff = c_extracted * 1e15 if not math.isnan(c_extracted) else float("nan")

    plot_s11(freq_z, s11_db, pout / "zin.png")
    write_s11_csv(pout / "zin.csv", freq_z, s11_db, zdiff)

    # --- Transient + SBR ---
    sbr_result: SbrResult | None = None
    eye_result: EyeMetrics | None = None
    if not no_tran:
        tmax = write_prbs_stim(work / "prbs_stim.inc", params.vbase)
        _patch_stim_source(work / "prbs_stim.inc")
        extra_tran = {**extra, "TMAX": f"{tmax:.6e}"}
        tb_tran = _write_tran_tb(work, dut_local, spice_dir, extra_tran, "prbs_stim.inc", "tran.raw")
        run_ngspice(tb_tran, work, "tran.log")
        time_s, v_outp, v_outn, v_inp, v_inn = parse_tran_raw(work / "tran.raw")
        write_tran_csv(pout / "tran.csv", time_s, v_outp, v_outn, v_inp, v_inn)
        write_eye_csvs(pout, time_s, v_outp, v_outn)
        plot_tran_se(time_s, v_outp, v_outn, v_inp, v_inn, pout / "tran_se.png")
        plot_tran_diff(time_s, v_outp, v_outn, v_inp, v_inn, pout / "tran_diff.png")
        plot_eye_se(time_s, v_outp, v_outn, pout / "eye_se.png")
        plot_eye_diff(time_s, v_outp, v_outn, pout / "eye_diff.png")
        eye_result = compute_eye_metrics(time_s, v_outp, v_outn)
        phase_ok, phase_summary, _, _ = verify_eye_phase_invariance(
            time_s, v_outp, v_outn,
        )
        if not phase_ok:
            raise ValueError(
                f"term eye metrics are not phase-invariant: {phase_summary}"
            )
        print(f"  eye phase-invariance: {phase_summary} (PASS)")

        tmax_sbr = write_sbr_stim(work / "sbr_stim.inc", params.vbase)
        _patch_stim_source(work / "sbr_stim.inc")
        extra_sbr = {**extra, "TMAX": f"{tmax_sbr:.6e}"}
        tb_sbr = _write_tran_tb(work, dut_local, spice_dir, extra_sbr, "sbr_stim.inc", "sbr.raw")
        run_ngspice(tb_sbr, work, "sbr.log")
        time_s, v_outp, v_outn, v_inp, v_inn = parse_tran_raw(work / "sbr.raw")
        sbr_result = extract_sbr(time_s, v_outp, v_outn)
        write_tran_csv(pout / "sbr.csv", time_s, v_outp, v_outn, v_inp, v_inn)
        write_sbr_taps_csv(pout / "sbr_taps.csv", sbr_result)
        plot_sbr(time_s, v_outp, v_outn, v_inp, v_inn, sbr_result, pout / "sbr.png")

    metrics = TermMetrics(
        vcm_pad_v=vcm,
        vtt_v=vtt,
        idiv_ma=idiv,
        esd_leak_a=esd_leak,
        il_dc_db=il_dc,
        il_28g_db=il_28,
        f_il_3db_hz=f_il_3db,
        s11_dc_db=s11_dc,
        s11_28g_db=s11_28,
        f_s11_10db_hz=f_s11_10,
        c_pad_per_side_ff=cap_acct["pad_per_side_fF"],
        c_esd_per_side_ff=cap_acct["esd_per_side_fF"],
        c_shunt_per_side_ff=cap_acct["shunt_per_side_fF"],
        c_shunt_diff_ff=cap_acct["shunt_diff_fF"],
        c_extracted_diff_ff=c_extracted_ff,
        f_zin_3db_hz=f_zin_3db,
        sbr=sbr_result,
        eye=eye_result,
    )
    write_metrics_csv(pout / "metrics.csv", params, metrics)

    print("\n=== Termination sim summary ===")
    print(f"  Pad CM={vcm:.4f} V  vtt={vtt:.4f} V  I_div={idiv:.3f} mA")
    esd_str = f"{esd_leak * 1e12:.2f}" if esd_leak is not None else "not measured"
    print(f"  ESD leak={esd_str} pA (one pad, diode pair, @vpad[i])")
    print(f"  IL DC={il_dc:.2f} dB  IL@28G={il_28:.2f} dB  f_IL,-3dB={f_il_3db/1e9:.2f} GHz")
    print(
        f"  S11 DC={s11_dc:.2f} dB  S11@28G={s11_28:.2f} dB  "
        f"f(S11=-10dB)={f_s11_10/1e9:.2f} GHz  (100 Ohm diff ref, trend worsens with f)"
    )
    ext_str = (
        f"{c_extracted_ff:.1f}"
        if not math.isnan(c_extracted_ff)
        else "nan"
    )
    print(
        f"  Cap per side: pad={cap_acct['pad_per_side_fF']:.1f} fF  "
        f"ESD={cap_acct['esd_per_side_fF']:.1f} fF  "
        f"sum={cap_acct['shunt_per_side_fF']:.1f} fF"
    )
    print(
        f"  Cap differential: accounted={cap_acct['shunt_diff_fF']:.1f} fF  "
        f"extracted={ext_str} fF  f_Zin,3dB={f_zin_3db/1e9:.1f} GHz"
    )
    if sbr_result:
        print(
            f"  SBR cursor={sbr_result.cursor_mV:.2f} mV  "
            f"ISI norm={sbr_result.isi_norm:.4f}"
        )
    if eye_result:
        print(
            f"  Eye height={eye_result.height_mV:.1f} mV  "
            f"width={eye_result.width_ui:.3f} UI ({eye_result.width_ps:.1f} ps)  "
            f"pp={eye_result.pp_swing_mV:.1f} mV"
        )
    print(f"  Artifacts: {pout}/")
    return params, metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Run termination stage ngspice verification")
    parser.add_argument("--no-tran", action="store_true")
    parser.add_argument("--pad-w-um", type=float, default=70.0)
    parser.add_argument("--pad-l-um", type=float, default=70.0)
    parser.add_argument("--no-pad-cap", action="store_true")
    args = parser.parse_args()
    run(
        no_tran=args.no_tran,
        pad_w_um=args.pad_w_um,
        pad_l_um=args.pad_l_um,
        no_pad_cap=args.no_pad_cap,
    )


if __name__ == "__main__":
    main()
