#!/usr/bin/env python3
"""End-to-end ngspice verification for term -> CTLE -> VGA RX chain (chain_pdk.cir).

Writes artifacts to circuits/ctle56n/out/chain/.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[3]
_EXP = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ctlelib.plots import (  # noqa: E402
    plot_chain_ac_perstg,
    plot_chain_sbr_perstg,
    plot_chain_tran_perstg,
)
from ctlelib import (  # noqa: E402
    PSRR_MAX_DB,
    SBR_KEEP_FRAC,
    SbrResult,
    compute_ac_peak_metrics,
    compute_eye_metrics,
    extract_sbr,
    group_delay_s,
    interp_db_at,
    parse_ac_raw,
    parse_dc_log,
    parse_psrr_raw,
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
    write_ac_diff_csv,
    write_eye_csvs,
    write_sbr_taps_csv,
    write_tran_csv,
)
from ctlelib.metrics import EYE_SETTLE_UI, EyeMetrics  # noqa: E402
from ctlelib.ngs import apply_params, complex_from_vm_vp  # noqa: E402
from ctlelib.stim import UI_S, write_prbs_stim, write_sbr_stim  # noqa: E402
from size_ctle import CtleParams, size_ctle  # noqa: E402
from size_term import RSRC_LEG_OHM, TermParams, Z0_DIFF_OHM, size_term, to_extra  # noqa: E402
from size_vga import VgaParams, extra_params, size_vga_for_chain  # noqa: E402
from stage_vga import read_vga_headroom  # noqa: E402
from size_driver import DriverParams, extra_params as driver_extra_params, size_driver  # noqa: E402

NYQUIST_HZ = 28e9
CHAIN_DUT_NAME = "chain_dut"
TERM_PREFIX_KEYS = (
    "PAD_W", "PAD_L", "PAD_C", "ESD_M", "RSIL_W", "RSIL_L", "ROUT_SER",
    "VTT_RTOP_W", "VTT_RTOP_L", "VTT_RBOT_W", "VTT_RBOT_L",
    "VTT_CAP_W", "VTT_CAP_L",
)
VGA_TOKEN_KEYS = (
    "Nx", "VBE", "VBASE", "VDD", "RD", "RS", "LLOAD", "CL", "ITAIL",
    "MOS_W", "MOS_L", "MOS_W_m", "MOS_L_m", "MOS_M", "MOS_VGS",
    "RPPD_W", "RPPD_L", "RPPD_W_m", "RPPD_L_m",
    "STEER_W", "STEER_L", "STEER_W_m", "STEER_L_m",
    "VCTRL", "VCTRL_P", "VCTRL_N",
)
DRV_TOKEN_KEYS = (
    "Nx", "VBE", "VBASE", "VDD", "ITAIL", "ITAIL_HALF",
    "MOS_W", "MOS_L", "MOS_W_m", "MOS_L_m", "TAIL_W_m", "MOS_M", "MOS_VGS",
    "RSIL_W", "RSIL_L", "RSIL_R", "RD_ON_CHIP", "LLOAD",
    "PAD_W", "PAD_L", "PAD_C", "ESD_M", "CL_PAD", "RDIFF_TB",
)

CHAIN_DC_SAVE_LINES = (
    "save v(outp) v(outn) v(inp) v(inn) v(vdd)\n"
    "save v(xu1.ctle_inp) v(xu1.ctle_inn) v(xu1.vga_inp) v(xu1.vga_inn)\n"
    "save v(xu1.drv_inp) v(xu1.drv_inn)\n"
    "save v(xu1.xuterm.vtt)\n"
    "save v(xu1.xuctle.e1) v(xu1.xuctle.e2) v(xu1.xuctle.mgate)\n"
    "save v(xu1.xuvga.e1) v(xu1.xuvga.e2) v(xu1.xuvga.ed1) v(xu1.xuvga.ed2)\n"
    "save v(xu1.xuvga.mgate) v(xu1.xuvga.tx1) v(xu1.xuvga.tx2)\n"
    "save v(xu1.xudrv.em) v(xu1.xudrv.mgate)\n"
    "save @q.xu1.xuctle.xq1.qnpn13g2[ic] @q.xu1.xuctle.xq2.qnpn13g2[ic]\n"
    "save @q.xu1.xuvga.xq1.qnpn13g2[ic] @q.xu1.xuvga.xq2.qnpn13g2[ic]\n"
    "save @q.xu1.xuvga.xqd1.qnpn13g2[ic] @q.xu1.xuvga.xqd2.qnpn13g2[ic]\n"
    "save @q.xu1.xudrv.xq1.qnpn13g2[ic] @q.xu1.xudrv.xq2.qnpn13g2[ic]\n"
    "save @n.xu1.xuctle.xtail1.nsg13_lv_nmos[ids] @n.xu1.xuctle.xtail2.nsg13_lv_nmos[ids]\n"
    "save @n.xu1.xuvga.xtail1.nsg13_lv_nmos[ids] @n.xu1.xuvga.xtail2.nsg13_lv_nmos[ids]\n"
    "save @n.xu1.xuvga.xps1.nsg13_lv_nmos[ids] @n.xu1.xuvga.xpd1.nsg13_lv_nmos[ids]\n"
    "save @n.xu1.xuvga.xps2.nsg13_lv_nmos[ids] @n.xu1.xuvga.xpd2.nsg13_lv_nmos[ids]\n"
    "save @n.xu1.xudrv.xtail.nsg13_lv_nmos[ids]"
)
CHAIN_DC_PRINT_LINES = (
    "print v(outp) v(outn) v(inp) v(inn) v(vdd)\n"
    "print v(xu1.ctle_inp) v(xu1.ctle_inn) v(xu1.vga_inp) v(xu1.vga_inn)\n"
    "print v(xu1.drv_inp) v(xu1.drv_inn)\n"
    "print v(xu1.xuterm.vtt)\n"
    "print v(xu1.xuctle.e1) v(xu1.xuctle.e2) v(xu1.xuctle.mgate)\n"
    "print v(xu1.xuvga.e1) v(xu1.xuvga.e2) v(xu1.xuvga.ed1) v(xu1.xuvga.ed2)\n"
    "print v(xu1.xudrv.em) v(xu1.xudrv.mgate)\n"
    "print @q.xu1.xuctle.xq1.qnpn13g2[ic] @q.xu1.xuctle.xq2.qnpn13g2[ic]\n"
    "print @q.xu1.xuvga.xq1.qnpn13g2[ic] @q.xu1.xuvga.xq2.qnpn13g2[ic]\n"
    "print @q.xu1.xuvga.xqd1.qnpn13g2[ic] @q.xu1.xuvga.xqd2.qnpn13g2[ic]\n"
    "print @q.xu1.xudrv.xq1.qnpn13g2[ic] @q.xu1.xudrv.xq2.qnpn13g2[ic]\n"
    "print @n.xu1.xuctle.xtail1.nsg13_lv_nmos[ids] @n.xu1.xuctle.xtail2.nsg13_lv_nmos[ids]\n"
    "print @n.xu1.xuvga.xtail1.nsg13_lv_nmos[ids] @n.xu1.xuvga.xtail2.nsg13_lv_nmos[ids]\n"
    "print @n.xu1.xuvga.xps1.nsg13_lv_nmos[ids] @n.xu1.xuvga.xpd1.nsg13_lv_nmos[ids]\n"
    "print @n.xu1.xuvga.xps2.nsg13_lv_nmos[ids] @n.xu1.xuvga.xpd2.nsg13_lv_nmos[ids]\n"
    "print @n.xu1.xudrv.xtail.nsg13_lv_nmos[ids]"
)


@dataclass
class GainSettingMetrics:
    label: str
    vctrl_v: float
    dc_gain_src_db: float
    ac_gain_src_28g_db: float
    dc_gain_pad_db: float
    ac_gain_pad_28g_db: float
    peaking_28g_db: float
    peak_gain_db: float
    f_peak_hz: float
    f_3db_hz: float
    gain_term_pad_db: float = float("nan")
    gain_ctle_db: float = float("nan")
    gain_vga_db: float = float("nan")
    gain_driver_db: float = float("nan")
    drive_swing_mv: float = float("nan")
    pad_swing_mv: float = float("nan")
    sbr: SbrResult | None = None
    eye: EyeMetrics | None = None


@dataclass
class ChainMetrics:
    vcm_pad_v: float
    vtt_v: float
    ctle_vds_tail_v: float
    vga_ic_signal_a: float
    vga_ic_dummy_a: float
    s11_dc_db: float
    s11_28g_db: float
    settings: list[GainSettingMetrics] = field(default_factory=list)
    cmrr_db: float = float("nan")
    psrr_db: float = float("nan")


def chain_perstg_out() -> Path:
    d = chain_out() / "chain_perstg"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class ChainTranData:
    time_s: np.ndarray
    v_outp: np.ndarray
    v_outn: np.ndarray
    v_inp: np.ndarray
    v_inn: np.ndarray
    v_ctle_inp: np.ndarray
    v_ctle_inn: np.ndarray
    v_vga_inp: np.ndarray
    v_vga_inn: np.ndarray
    v_drv_inp: np.ndarray
    v_drv_inn: np.ndarray


def chain_out() -> Path:
    return _EXP / "out" / "chain"


def _spice_dir() -> Path:
    return _EXP / "spice"


def _copy_spiceinit(work: Path) -> None:
    pdk = os.environ.get("PDK_ROOT", "")
    if pdk:
        spiceinit = Path(pdk) / "ihp-sg13g2/libs.tech/ngspice/.spiceinit"
        if spiceinit.is_file():
            shutil.copy(spiceinit, work / ".spiceinit")


def _copy_params_inc(spice_dir: Path, work: Path) -> None:
    src = spice_dir / "params.inc"
    if src.is_file():
        shutil.copy(src, work / "params.inc")


def build_chain_extra(
    term: TermParams,
    vga: VgaParams,
    driver: DriverParams,
    vctrl: float,
) -> dict[str, str]:
    """Merge term / CTLE(params.inc) / VGA / driver tokens for chain_pdk.cir."""
    ep: dict[str, str] = {}

    term_ep = to_extra(term)
    for key in TERM_PREFIX_KEYS:
        if key in term_ep:
            ep[f"TERM_{key}"] = term_ep[key]
    ep["VDD"] = term_ep["VDD"]
    ep["VBASE"] = term_ep["VBASE"]
    ep["RSRC_LEG"] = term_ep["RSRC_LEG"]
    ep["MOS_VGS"] = term_ep["MOS_VGS"]

    vga_ep = extra_params(vga, vctrl=vctrl)
    for key in VGA_TOKEN_KEYS:
        if key in vga_ep:
            ep[f"VGA_{key}"] = vga_ep[key]
    ep["IND_SHUNT_INC"] = vga_ep["IND_SHUNT_INC"]
    ep["CL_TB"] = "0"
    ep["TMAX"] = "1e-8"

    drv_ep = driver_extra_params(driver)
    for key in DRV_TOKEN_KEYS:
        if key in drv_ep:
            ep[f"DRV_{key}"] = drv_ep[key]
    ep["RDIFF_TB"] = drv_ep["RDIFF_TB"]
    return ep


def _inject_receiver_load(text: str, rdiff: str = "100") -> str:
    """Floating differential receiver termination — testbench only."""
    needle = f"XU1 outp outn inp inn vdd {CHAIN_DUT_NAME}"
    if needle not in text:
        return text
    return text.replace(
        needle,
        needle + f"\n* Floating {rdiff} Ohm differential receiver (TB only)\n"
        f"Rterm outp outn {rdiff}",
        1,
    )


def _write_work_params(work: Path, extra: dict[str, str]) -> None:
    skip = {"IND_SHUNT_INC"}
    lines = ["* chain params — term TERM_*, CTLE from params.inc, VGA VGA_*"]
    for k, v in sorted(extra.items()):
        if k in skip:
            continue
        lines.append(f".param {k}={v}")
    (work / "chain_params.inc").write_text("\n".join(lines) + "\n")


def _prepare_chain_dut(work: Path, models: Path, spice_dir: Path, extra: dict[str, str]) -> Path:
    dut_src = spice_dir / "chain_pdk.cir"
    text = apply_params(dut_src.read_text(), spice_dir, extra)
    text = text.replace("{PDK_MODELS}", str(models))
    text = text.replace("{IND_SHUNT_INC}", extra["IND_SHUNT_INC"])
    dut_local = work / "chain_pdk.cir"
    dut_local.write_text(text)
    return dut_local


def _patch_nodeset(tb_path: Path, term: TermParams, vga: VgaParams, ctle: CtleParams, driver: DriverParams) -> None:
    text = tb_path.read_text()
    tail_vds = ctle.vbase - ctle.vbe
    text = re.sub(
        r"\.nodeset[^\n]*",
        ".nodeset "
        f"v(xu1.xuctle.mgate)={term.extra.get('MOS_VGS', '0.55')} "
        f"v(xu1.xuvga.mgate)={vga.mos_vgs:.4g} "
        f"v(xu1.xudrv.mgate)={driver.mos_vgs:.4g} "
        f"v(xu1.xuctle.e1)={tail_vds:.4g} v(xu1.xuctle.e2)={tail_vds:.4g} "
        f"v(xu1.xuvga.e1)={tail_vds:.4g} v(xu1.xuvga.e2)={tail_vds:.4g} "
        f"v(xu1.xuvga.ed1)={tail_vds:.4g} v(xu1.xuvga.ed2)={tail_vds:.4g} "
        f"v(outp)={driver.vout_cm_est:.4g} v(outn)={driver.vout_cm_est:.4g}",
        text,
        count=1,
    )
    tb_path.write_text(text)


def _prepare_chain_tb(
    template: Path,
    dut_cir: Path,
    work: Path,
    models: Path,
    spice_dir: Path,
    extra: dict[str, str],
    *,
    cl_tb: str,
) -> Path:
    tb = prepare_tb(
        template,
        dut_cir,
        work,
        models,
        spice_dir,
        extra_params=extra,
        dut_name=CHAIN_DUT_NAME,
        cl_tb=cl_tb,
        dc_save_lines=CHAIN_DC_SAVE_LINES,
        dc_print_lines=CHAIN_DC_PRINT_LINES,
    )
    text = _inject_receiver_load(tb.read_text(), extra.get("RDIFF_TB", "100"))
    tb.write_text(text)
    return tb


def _write_ac_chain_tb(
    work: Path,
    dut_cir: Path,
    models: Path,
    spice_dir: Path,
    extra: dict[str, str],
    *,
    cl_tb: str,
    raw_name: str = "ac_diff.raw",
) -> Path:
    """AC with 50 Ohm per-leg source; saves internal nodes for stage-by-stage gain."""
    text = f"""* AC differential — 100 Ohm diff source into bond pads
.include chain_params.inc
.include params.inc
.include {dut_cir.resolve()}

Vdd vdd 0 dc {{VDD}}
XU1 outp outn inp inn vdd {CHAIN_DUT_NAME}

Vp vp 0 dc {{VBASE}} ac 0.5 0
Vn vn 0 dc {{VBASE}} ac 0.5 180
Rsrc_p vp inp {{RSRC_LEG}}
Rsrc_n vn inn {{RSRC_LEG}}

* Floating 100 Ohm differential receiver (TB only)
Rterm outp outn {{RDIFF_TB}}

Cload_p outp 0 0
Cload_n outn 0 0

.options gmin=1e-18 abstol=1e-15 reltol=1e-3
.nodeset v(xu1.xuctle.mgate)={{MOS_VGS}} v(outp)=1.40 v(outn)=1.40

.control
save v(outp) v(outn) v(inp) v(inn) v(vp) v(vn)
save v(xu1.ctle_inp) v(xu1.ctle_inn) v(xu1.vga_inp) v(xu1.vga_inn)
save v(xu1.drv_inp) v(xu1.drv_inn)
ac dec 200 1e6 300e9
set wr_singlescale
wrdata {raw_name} frequency vm(outp) vp(outp) vm(outn) vp(outn) vm(inp) vp(inp) vm(inn) vp(inn) vm(vp) vp(vp) vm(vn) vp(vn) vm(xu1.ctle_inp) vp(xu1.ctle_inp) vm(xu1.ctle_inn) vp(xu1.ctle_inn) vm(xu1.vga_inp) vp(xu1.vga_inp) vm(xu1.vga_inn) vp(xu1.vga_inn) vm(xu1.drv_inp) vp(xu1.drv_inp) vm(xu1.drv_inn) vp(xu1.drv_inn)
.endc
.end
"""
    text = apply_params(text, spice_dir, extra)
    text = text.replace("{PDK_MODELS}", str(models))
    out = work / f"tb_{raw_name.replace('.raw', '')}.cir"
    out.write_text(text)
    return out


def _write_dc_chain_tb(
    work: Path,
    dut_cir: Path,
    models: Path,
    spice_dir: Path,
    extra: dict[str, str],
) -> Path:
    text = f"""* DC OP — RX chain
.include chain_params.inc
.include params.inc
.include {dut_cir.resolve()}

Vdd vdd 0 dc {{VDD}}
XU1 outp outn inp inn vdd {CHAIN_DUT_NAME}

Vp inp 0 dc {{VBASE}}
Vn inn 0 dc {{VBASE}}

.options gmin=1e-18 abstol=1e-15 reltol=1e-3
.nodeset v(xu1.xuctle.mgate)={{MOS_VGS}} v(outp)=1.40 v(outn)=1.40

.control
{CHAIN_DC_SAVE_LINES}
op
echo "--- DC operating point ---"
{CHAIN_DC_PRINT_LINES}
.endc
.end
"""
    text = apply_params(text, spice_dir, extra)
    text = _inject_receiver_load(text, extra.get("RDIFF_TB", "100"))
    out = work / "tb_dc.cir"
    out.write_text(text)
    return out


def _patch_stim_source(stim_path: Path, r_leg: float = RSRC_LEG_OHM) -> None:
    text = stim_path.read_text()
    text = text.replace("Vp inp", "Vp vp_drv")
    text = text.replace("Vn inn", "Vn vn_drv")
    extra = (
        f"\n* {r_leg:.0f} Ohm per leg ({2 * r_leg:.0f} Ohm differential source)\n"
        f"Rsrc_p vp_drv inp {r_leg:g}\n"
        f"Rsrc_n vn_drv inn {r_leg:g}\n"
    )
    stim_path.write_text(text + extra)


def _write_tran_chain_tb(
    work: Path,
    dut_cir: Path,
    spice_dir: Path,
    extra: dict[str, str],
    stim_name: str,
    raw_name: str,
) -> Path:
    text = f"""* Transient — 50 Ohm source + RX chain
.include chain_params.inc
.include params.inc
.include {dut_cir.resolve()}
.include {stim_name}

Vdd vdd 0 dc {{VDD}}
XU1 outp outn inp inn vdd {CHAIN_DUT_NAME}
* Floating 100 Ohm differential receiver (TB only)
Rterm outp outn {{RDIFF_TB}}

Cload_p outp 0 0
Cload_n outn 0 0

.options gmin=1e-18 abstol=1e-15 reltol=1e-3
.nodeset v(xu1.xuctle.mgate)={{MOS_VGS}} v(outp)=1.40 v(outn)=1.40

.control
save v(outp) v(outn) v(inp) v(inn)
save v(xu1.ctle_inp) v(xu1.ctle_inn) v(xu1.vga_inp) v(xu1.vga_inn)
save v(xu1.drv_inp) v(xu1.drv_inn)
tran 0.5p {{TMAX}} 0 1p
set wr_singlescale
wrdata {raw_name} time v(outp) v(outn) v(inp) v(inn) v(xu1.ctle_inp) v(xu1.ctle_inn) v(xu1.vga_inp) v(xu1.vga_inn) v(xu1.drv_inp) v(xu1.drv_inn)
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
    zdiff = np.abs(vinp - vinn)
    return freq, zdiff


def _s11_db(z: np.ndarray, z0: float = Z0_DIFF_OHM) -> np.ndarray:
    s11 = (z - z0) / (z + z0)
    return 20.0 * np.log10(np.maximum(np.abs(s11), 1e-30))


def _plot_s11(freq: np.ndarray, s11_db: np.ndarray, path: Path) -> None:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.semilogx(freq, s11_db, "b-", lw=1.2)
    ax.axhline(-10, color="gray", ls="--", lw=0.8, label="-10 dB")
    ax.axvline(NYQUIST_HZ, color="orange", ls=":", lw=0.8)
    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("S11 (dB, 100 Ohm diff ref)")
    ax.set_title("Differential return loss at bond pad (100 Ohm reference)")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend(fontsize=8)
    fig.savefig(path, dpi=120)
    plt.close(fig)


def _write_s11_csv(path: Path, freq: np.ndarray, s11_db: np.ndarray, z_ohm: np.ndarray) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["freq_Hz", "S11_dB", "Zdiff_ohm"])
        for i in range(len(freq)):
            w.writerow([freq[i], s11_db[i], z_ohm[i]])


def _ac_gains_from_raw(raw: Path) -> dict[str, np.ndarray]:
    """Parse extended AC wrdata with pad, CTLE, VGA internal nodes."""
    rows = np.loadtxt(raw)
    if rows.ndim == 1:
        rows = rows.reshape(1, -1)
    freq = rows[:, 0]
    voutp = complex_from_vm_vp(rows[:, 3], rows[:, 4])
    voutn = complex_from_vm_vp(rows[:, 5], rows[:, 6])
    vinp = complex_from_vm_vp(rows[:, 7], rows[:, 8])
    vinn = complex_from_vm_vp(rows[:, 9], rows[:, 10])
    vvp = complex_from_vm_vp(rows[:, 11], rows[:, 12])
    vvn = complex_from_vm_vp(rows[:, 13], rows[:, 14])
    vctlep = complex_from_vm_vp(rows[:, 15], rows[:, 16])
    vctleinn = complex_from_vm_vp(rows[:, 17], rows[:, 18])
    vvgap = complex_from_vm_vp(rows[:, 19], rows[:, 20])
    vvgainn = complex_from_vm_vp(rows[:, 21], rows[:, 22])
    vdrvinp = complex_from_vm_vp(rows[:, 23], rows[:, 24])
    vdrvinn = complex_from_vm_vp(rows[:, 25], rows[:, 26])

    vod = voutp - voutn
    vpad = vinp - vinn
    vsrc = vvp - vvn
    vctle = vctlep - vctleinn
    vvga = vvgap - vvgainn
    vdrv = vdrvinp - vdrvinn

    def _h(num: np.ndarray, den: np.ndarray) -> np.ndarray:
        return np.where(np.abs(den) > 1e-30, num / den, 0.0)

    h_term = _h(vctle, vpad)
    h_ctle = _h(vvga, vctle)
    h_vga = _h(vdrv, vvga)
    h_driver = _h(vod, vdrv)

    return {
        "freq": freq,
        "h_src_db": 20.0 * np.log10(np.maximum(np.abs(_h(vod, vsrc)), 1e-30)),
        "h_pad_db": 20.0 * np.log10(np.maximum(np.abs(_h(vod, vpad)), 1e-30)),
        "h_term_db": 20.0 * np.log10(np.maximum(np.abs(h_term), 1e-30)),
        "h_ctle_db": 20.0 * np.log10(np.maximum(np.abs(h_ctle), 1e-30)),
        "h_vga_db": 20.0 * np.log10(np.maximum(np.abs(h_vga), 1e-30)),
        "h_driver_db": 20.0 * np.log10(np.maximum(np.abs(h_driver), 1e-30)),
        "h_src": _h(vod, vsrc),
    }


def _parse_chain_tran_raw(raw: Path) -> ChainTranData:
    """Parse chain transient wrdata with internal tap nodes when present."""
    rows = np.loadtxt(raw)
    if rows.ndim == 1:
        rows = rows.reshape(1, -1)
    ncol = rows.shape[1]
    time_s = rows[:, 0]

    def _col(i: int, fallback: np.ndarray) -> np.ndarray:
        return rows[:, i] if ncol > i else fallback

    if ncol >= 12:
        # wr_singlescale duplicates time in columns 0 and 1
        return ChainTranData(
            time_s=time_s,
            v_outp=rows[:, 2],
            v_outn=rows[:, 3],
            v_inp=rows[:, 4],
            v_inn=rows[:, 5],
            v_ctle_inp=rows[:, 6],
            v_ctle_inn=rows[:, 7],
            v_vga_inp=rows[:, 8],
            v_vga_inn=rows[:, 9],
            v_drv_inp=rows[:, 10],
            v_drv_inn=rows[:, 11],
        )
    if ncol >= 11:
        return ChainTranData(
            time_s=time_s,
            v_outp=rows[:, 1],
            v_outn=rows[:, 2],
            v_inp=rows[:, 3],
            v_inn=rows[:, 4],
            v_ctle_inp=rows[:, 5],
            v_ctle_inn=rows[:, 6],
            v_vga_inp=rows[:, 7],
            v_vga_inn=rows[:, 8],
            v_drv_inp=rows[:, 9],
            v_drv_inn=rows[:, 10],
        )
    if ncol >= 8:
        z = np.zeros(rows.shape[0])
        return ChainTranData(
            time_s=time_s,
            v_outp=rows[:, 2],
            v_outn=rows[:, 3],
            v_inp=rows[:, 4],
            v_inn=rows[:, 5],
            v_ctle_inp=rows[:, 4],
            v_ctle_inn=rows[:, 5],
            v_vga_inp=z,
            v_vga_inn=z,
            v_drv_inp=_col(6, z),
            v_drv_inn=_col(7, z),
        )
    if ncol >= 7:
        z = np.zeros(rows.shape[0])
        return ChainTranData(
            time_s=time_s,
            v_outp=rows[:, 1],
            v_outn=rows[:, 2],
            v_inp=rows[:, 3],
            v_inn=rows[:, 4],
            v_ctle_inp=rows[:, 3],
            v_ctle_inn=rows[:, 4],
            v_vga_inp=z,
            v_vga_inn=z,
            v_drv_inp=_col(5, z),
            v_drv_inn=_col(6, z),
        )
    if ncol >= 5:
        z = np.zeros(rows.shape[0])
        return ChainTranData(
            time_s=time_s,
            v_outp=rows[:, 1],
            v_outn=rows[:, 2],
            v_inp=rows[:, 3],
            v_inn=rows[:, 4],
            v_ctle_inp=rows[:, 3],
            v_ctle_inn=rows[:, 4],
            v_vga_inp=z,
            v_vga_inn=z,
            v_drv_inp=z,
            v_drv_inn=z,
        )
    raise RuntimeError(f"{raw}: expected ≥5 columns, got {ncol}")


def _write_ac_perstg_csv(path: Path, ac: dict[str, np.ndarray]) -> None:
    freq = ac["freq"]
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "freq_Hz",
                "h_src_dB",
                "h_pad_dB",
                "h_term_dB",
                "h_ctle_dB",
                "h_vga_dB",
                "h_driver_dB",
            ]
        )
        for i in range(len(freq)):
            w.writerow(
                [
                    freq[i],
                    ac["h_src_db"][i],
                    ac["h_pad_db"][i],
                    ac["h_term_db"][i],
                    ac["h_ctle_db"][i],
                    ac["h_vga_db"][i],
                    ac["h_driver_db"][i],
                ]
            )


def _chain_tran_perstg_vod_mv(tr: ChainTranData) -> dict[str, np.ndarray]:
    return {
        "term": (tr.v_ctle_inp - tr.v_ctle_inn) * 1e3,
        "ctle": (tr.v_vga_inp - tr.v_vga_inn) * 1e3,
        "vga": (tr.v_drv_inp - tr.v_drv_inn) * 1e3,
        "driver": (tr.v_outp - tr.v_outn) * 1e3,
    }


def _chain_sbr_perstg_stages(tr: ChainTranData) -> dict[str, tuple[np.ndarray, np.ndarray, SbrResult]]:
    out: dict[str, tuple[np.ndarray, np.ndarray, SbrResult]] = {}
    for key, vp, vn in (
        ("term", tr.v_ctle_inp, tr.v_ctle_inn),
        ("ctle", tr.v_vga_inp, tr.v_vga_inn),
        ("vga", tr.v_drv_inp, tr.v_drv_inn),
        ("driver", tr.v_outp, tr.v_outn),
    ):
        if np.max(np.abs(vp - vn)) < 1e-15:
            continue
        sbr = extract_sbr(tr.time_s, vp, vn)
        out[key] = (vp, vn, sbr)
    return out


def _pp_mv_from_tran(time_s: np.ndarray, sig: np.ndarray) -> float:
    from ctlelib.stim import UI_S

    mask = time_s >= EYE_SETTLE_UI * UI_S
    if not np.any(mask):
        return float("nan")
    s = sig[mask]
    return float((np.max(s) - np.min(s)) * 1e3)


def extract_eye_metrics(
    time_s: np.ndarray,
    v_outp: np.ndarray,
    v_outn: np.ndarray,
) -> EyeMetrics:
    """Chain eye metrics — same phase-invariant definition as standalone passes."""
    return compute_eye_metrics(time_s, v_outp, v_outn)


def write_isi_analysis(
    path: Path,
    ctle_sbr: SbrResult | None,
    vga_sbr: SbrResult | None,
    chain_sbr: SbrResult | None,
) -> None:
    """Document per-stage SBR taps and whether VGA reshaping explains chain ISI."""
    rows: list[list[str]] = [
        ["stage", "k", "h_mV", "h_over_h0", "kept", "note"],
    ]
    for label, sbr in (
        ("ctle_standalone", ctle_sbr),
        ("vga_standalone_mid", vga_sbr),
        ("chain_mid", chain_sbr),
    ):
        if sbr is None:
            continue
        h0 = sbr.cursor_mV
        for k, h_mV, kept in sbr.taps:
            ratio = h_mV / h0 if h0 != 0 else ""
            note = ""
            if label == "chain_mid" and k != 0 and kept:
                note = "chain tap"
            rows.append([label, str(k), f"{h_mV:.4f}", f"{ratio:.6g}", "yes" if kept else "no", note])
    if ctle_sbr and chain_sbr:
        rows.append([])
        rows.append(["metric", "ctle_standalone", "chain_mid", "comment"])
        rows.append([
            "isi_norm",
            f"{ctle_sbr.isi_norm:.6g}",
            f"{chain_sbr.isi_norm:.6g}",
            "chain ISI vs CTLE-only",
        ])
        rows.append([
            "isi_abs",
            f"{ctle_sbr.isi_abs:.6g}",
            f"{chain_sbr.isi_abs:.6g}",
            "sum |h_k|/|h_0|",
        ])
    with path.open("w", newline="") as f:
        csv.writer(f).writerows(rows)


def _load_standalone_sbr(pass_name: str) -> SbrResult | None:
    """Load SBR taps from a committed standalone metrics pass."""
    taps_path = _EXP / "out" / pass_name / "sbr_taps.csv"
    if not taps_path.is_file():
        return None
    taps: list[tuple[int, float, bool]] = []
    cursor_mV = 0.0
    with taps_path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            k = int(row["k"])
            h = float(row["h_mV"])
            kept = row["kept"].strip().lower() == "yes"
            taps.append((k, h, kept))
            if k == 0:
                cursor_mV = h
    isi_sum = sum(h for k, h, kept in taps if kept and k != 0)
    h0_abs = abs(cursor_mV)
    isi_abs_sum = sum(abs(h) for k, h, kept in taps if kept and k != 0)
    isi_norm = isi_sum / cursor_mV if cursor_mV != 0 else float("nan")
    isi_abs = isi_abs_sum / h0_abs if h0_abs != 0 else float("nan")
    return SbrResult(
        taps=taps,
        cursor_mV=cursor_mV,
        isi_norm=isi_norm,
        isi_abs=isi_abs,
        t_cursor_s=0.0,
        t_cursor_ui=0.0,
        t_pulse_start_s=0.0,
    )


def _vce_from_dc(dc: dict[str, float], qpath: str, out_node: str = "v(outp)") -> float:
    ve = dc.get(qpath.replace("[ic]", "").replace("@q.", "v(xu1.").replace(".qnpn13g2", ".e1)"), float("nan"))
    if "xuctle" in qpath:
        ve = dc.get("v(xu1.xuctle.e1)", float("nan"))
    elif "xuvga.xq" in qpath and "xqd" not in qpath:
        ve = dc.get("v(xu1.xuvga.e1)", float("nan"))
    elif "xqd" in qpath:
        ve = dc.get("v(xu1.xuvga.ed1)", float("nan"))
    vout = dc.get(out_node, float("nan"))
    return vout - ve if not (math.isnan(vout) or math.isnan(ve)) else float("nan")


def write_op_table(
    path: Path,
    dc: dict[str, float],
    term: TermParams,
    vga: VgaParams,
    ctle: CtleParams,
    driver: DriverParams,
) -> None:
    vcm = 0.5 * (dc.get("v(inp)", term.vbase) + dc.get("v(inn)", term.vbase))
    vtt = dc.get("v(xu1.xuterm.vtt)", term.vbase)
    ctle_inp = dc.get("v(xu1.ctle_inp)", float("nan"))
    ctle_inn = dc.get("v(xu1.ctle_inn)", float("nan"))
    ctle_in_cm = 0.5 * (ctle_inp + ctle_inn) if not math.isnan(ctle_inp) else float("nan")
    vga_inp = dc.get("v(xu1.vga_inp)", float("nan"))
    vga_inn = dc.get("v(xu1.vga_inn)", float("nan"))
    vga_in_cm = 0.5 * (vga_inp + vga_inn) if not math.isnan(vga_inp) else float("nan")
    ctle_ve = dc.get("v(xu1.xuctle.e1)", float("nan"))
    ctle_vds = ctle_ve
    vga_ic_sig = (
        dc.get("@q.xu1.xuvga.xq1.qnpn13g2[ic]", float("nan"))
        + dc.get("@q.xu1.xuvga.xq2.qnpn13g2[ic]", float("nan"))
    ) / 2.0
    vga_ic_dum = (
        dc.get("@q.xu1.xuvga.xqd1.qnpn13g2[ic]", float("nan"))
        + dc.get("@q.xu1.xuvga.xqd2.qnpn13g2[ic]", float("nan"))
    ) / 2.0
    drv_ve = dc.get("v(xu1.xudrv.em)", float("nan"))
    drv_vout = 0.5 * (dc.get("v(outp)", 0) + dc.get("v(outn)", 0))
    drv_ic = dc.get("@q.xu1.xudrv.xq1.qnpn13g2[ic]", float("nan"))
    drv_vce = drv_vout - drv_ve if not math.isnan(drv_ve) else float("nan")
    rows = [
        ["quantity", "value", "sized_for", "note"],
        ["VDD", f"{dc.get('v(vdd)', term.vdd):.4f}", "shared", "chain rail"],
        ["vcm_pad", f"{vcm:.4f}", f"{term.vbase:.4f}", "bond pad CM"],
        ["vtt", f"{vtt:.4f}", f"{term.vbase:.4f}", "on-chip termination CM (CTLE input target)"],
        ["ctle_in_CM", f"{ctle_in_cm:.4f}", f"{ctle.vbase:.4f}", "CTLE base CM post-term"],
        ["ctle_VDS_tail", f"{ctle_vds:.4f}", f"{ctle.vbase - ctle.vbe:.4f}", "emitter VDS"],
        ["ctle_Vout_CM", f"{vga_in_cm:.4f}", f"{ctle.vout_cm:.4f}", "CTLE collector CM → VGA input"],
        ["ctle_Ic", f"{dc.get('@q.xu1.xuctle.xq1.qnpn13g2[ic]', float('nan')):.6g}", "", "per HBT"],
        ["ctle_VCE", f"{_vce_from_dc(dc, '@q.xu1.xuctle.xq1.qnpn13g2[ic]'):.4f}", "", "signal HBT"],
        ["vga_VBASE", f"{vga.vbase:.4f}", f"{vga.vbase:.4f}", "dummy-pair + input CM reference"],
        ["vga_Vout_CM", f"{0.5 * (dc.get('v(xu1.drv_inp)', 0) + dc.get('v(xu1.drv_inn)', 0)):.4f}", "", "VGA → driver interface"],
        ["vga_Ic_signal", f"{vga_ic_sig:.6g}", "", "avg signal pair @ mid VCTRL"],
        ["vga_Ic_dummy", f"{vga_ic_dum:.6g}", "", "avg dummy pair @ mid VCTRL"],
        ["vga_VCE_signal", f"{_vce_from_dc(dc, '@q.xu1.xuvga.xq1.qnpn13g2[ic]'):.4f}", "", ""],
        ["vga_VCE_dummy", f"{_vce_from_dc(dc, '@q.xu1.xuvga.xqd1.qnpn13g2[ic]'):.4f}", "", ""],
        ["driver_Vout_CM", f"{drv_vout:.4f}", f"{driver.vout_cm_est:.4f}", "output pad CM"],
        ["driver_Ic", f"{drv_ic:.6g}", f"{driver.itail_a / 2:.6g}", "per HBT @ ITAIL/2"],
        ["driver_VCE", f"{drv_vce:.4f}", f"{driver.vce_est:.4f}", "signal HBT"],
    ]
    with path.open("w", newline="") as f:
        csv.writer(f).writerows(rows)


def write_metrics_csv(path: Path, m: ChainMetrics, term: TermParams, vga: VgaParams) -> None:
    rows: list[list[str]] = [
        ["parameter", "value"],
        ["VDD_V", f"{term.vdd:.6g}"],
        ["vtt_V", f"{m.vtt_v:.6g}"],
        ["vcm_pad_V", f"{m.vcm_pad_v:.6g}"],
        ["ctle_VDS_tail_V", f"{m.ctle_vds_tail_v:.6g}"],
        ["vga_Ic_signal_A", f"{m.vga_ic_signal_a:.6g}"],
        ["vga_Ic_dummy_A", f"{m.vga_ic_dummy_a:.6g}"],
        ["S11_dc_dB", f"{m.s11_dc_db:.3f}"],
        ["S11_28GHz_dB", f"{m.s11_28g_db:.3f}"],
        ["CMRR_dB", f"{m.cmrr_db:.3f}"],
        ["PSRR_dB", f"{m.psrr_db:.3f}"],
        ["SBR_KEEP_FRAC", f"{SBR_KEEP_FRAC:.6g}"],
    ]
    for s in m.settings:
        rows += [
            [f"{s.label}_VCTRL_V", f"{s.vctrl_v:.4f}"],
            [f"{s.label}_gain_src_DC_dB", f"{s.dc_gain_src_db:.3f}"],
            [f"{s.label}_gain_src_28G_dB", f"{s.ac_gain_src_28g_db:.3f}"],
            [f"{s.label}_gain_pad_DC_dB", f"{s.dc_gain_pad_db:.3f}"],
            [f"{s.label}_gain_pad_28G_dB", f"{s.ac_gain_pad_28g_db:.3f}"],
            [f"{s.label}_peaking_28G_dB", f"{s.peaking_28g_db:.3f}"],
            [f"{s.label}_gain_term_28G_dB", f"{s.gain_term_pad_db:.3f}"],
            [f"{s.label}_gain_ctle_28G_dB", f"{s.gain_ctle_db:.3f}"],
            [f"{s.label}_gain_vga_28G_dB", f"{s.gain_vga_db:.3f}"],
            [f"{s.label}_gain_driver_28G_dB", f"{s.gain_driver_db:.3f}"],
            [f"{s.label}_drive_swing_mV", f"{s.drive_swing_mv:.2f}"],
            [f"{s.label}_pad_swing_mV", f"{s.pad_swing_mv:.2f}"],
            [f"{s.label}_f_peak_Hz", f"{s.f_peak_hz:.6g}"],
            [f"{s.label}_f_3dB_Hz", f"{s.f_3db_hz:.6g}"],
        ]
        if s.eye:
            rows += [
                [f"{s.label}_eye_height_mV", f"{s.eye.height_mV:.3f}"],
                [f"{s.label}_eye_width_UI", f"{s.eye.width_ui:.4f}"],
                [f"{s.label}_eye_width_ps", f"{s.eye.width_ps:.3f}"],
                [f"{s.label}_eye_sample_phase_UI", f"{s.eye.sample_phase_ui:.4f}"],
            ]
        if s.sbr:
            rows += [
                [f"{s.label}_sbr_isi_norm", f"{s.sbr.isi_norm:.6g}"],
                [f"{s.label}_sbr_isi_abs", f"{s.sbr.isi_abs:.6g}"],
            ]
    with path.open("w", newline="") as f:
        csv.writer(f).writerows(rows)


def write_stage_compare_csv(path: Path, chain: dict[str, float], standalone: dict[str, float]) -> None:
    rows = [
        ["stage", "metric", "chain_28G_dB", "standalone_28G_dB", "delta_dB"],
    ]
    for stage, key in (
        ("term_IL", "term"),
        ("ctle", "ctle"),
        ("vga_mid", "vga"),
        ("chain_e2e_pad", "chain_pad"),
        ("chain_e2e_src", "chain_src"),
    ):
        c = chain.get(key, float("nan"))
        s = standalone.get(key, float("nan"))
        delta = c - s if not (math.isnan(c) or math.isnan(s)) else float("nan")
        rows.append([stage, "gain_28GHz", f"{c:.3f}", f"{s:.3f}", f"{delta:.3f}"])
    with path.open("w", newline="") as f:
        csv.writer(f).writerows(rows)


def _load_standalone_metrics() -> dict[str, float]:
    """Read committed standalone stage metrics for comparison."""
    out: dict[str, float] = {}
    term_csv = _EXP / "out" / "term" / "metrics.csv"
    ctle_csv = _EXP / "out" / "pdk" / "metrics.csv"
    vga_csv = _EXP / "out" / "vga_pdk" / "gain_vs_vctrl.csv"
    if term_csv.is_file():
        for row in csv.DictReader(term_csv.open()):
            if row["parameter"] == "IL_28GHz_dB":
                out["term"] = -float(row["value"])  # IL -> gain through term
    if ctle_csv.is_file():
        for row in csv.DictReader(ctle_csv.open()):
            if row["parameter"] == "pdk_dc_gain_dB":
                out["ctle_dc"] = float(row["value"])
            if row["parameter"] == "pdk_peaking_28G_dB":
                out["ctle_peak"] = float(row["value"])
            if row["parameter"] == "pdk_sbr_isi_norm":
                out["ctle_sbr"] = float(row["value"])
        out["ctle"] = out.get("ctle_dc", float("nan")) + out.get("ctle_peak", float("nan"))
    if vga_csv.is_file():
        rows = list(csv.DictReader(vga_csv.open()))
        mid = rows[len(rows) // 2]
        out["vga"] = float(mid["ac_gain_28G_dB"])
        out["vga_min"] = float(rows[0]["ac_gain_28G_dB"])
        out["vga_max"] = float(rows[-1]["ac_gain_28G_dB"])
    return out


def run(
    out_dir: Path | None = None,
    *,
    no_tran: bool = False,
) -> ChainMetrics:
    spice_dir = _spice_dir()
    pout = out_dir or chain_out()
    pstg = chain_perstg_out()
    pout.mkdir(parents=True, exist_ok=True)
    work = pout / "work"
    work.mkdir(parents=True, exist_ok=True)
    _copy_spiceinit(work)
    _copy_params_inc(spice_dir, work)

    if not (spice_dir / "ind_shunt.inc").is_file():
        raise SystemExit("spice/ind_shunt.inc missing — run size_ctle.py / size_ind.py first")

    term = size_term()
    ctle = size_ctle(vbase_input=term.vbase)
    vga = size_vga_for_chain(term, ctle)
    driver = size_driver()
    v_min = min(vga.vctrl_v)
    v_mid = vga.vctrl_v[len(vga.vctrl_v) // 2]
    headroom = read_vga_headroom("vga_pdk")
    v_max_nom = max(vga.vctrl_v)
    v_max_usable = headroom.get("usable_vctrl_max")
    if v_max_usable is None:
        v_max_usable = v_mid
    gain_settings = [
        ("min", v_min),
        ("mid", v_mid),
        ("max", float(v_max_usable)),
    ]
    print(
        f"  Chain VGA gain points: min={v_min:.2f} mid={v_mid:.2f} "
        f"max_usable={v_max_usable:.2f} (nominal max={v_max_nom:.2f}, "
        f"VCTRL=1.0 OP {'ok' if headroom.get('vctrl_max_op_ok') else 'FAILED'})"
    )

    models = pdk_models()
    mid_extra = build_chain_extra(term, vga, driver, v_mid)
    _write_work_params(work, mid_extra)
    dut_cir = _prepare_chain_dut(work, models, spice_dir, mid_extra)

    # --- DC OP at mid VGA gain ---
    dc_extra = build_chain_extra(term, vga, driver, v_mid)
    _write_work_params(work, dc_extra)
    dut_cir = _prepare_chain_dut(work, models, spice_dir, dc_extra)
    tb_dc = _write_dc_chain_tb(work, dut_cir, models, spice_dir, dc_extra)
    dc_log = run_ngspice(tb_dc, work, "dc.log")
    dc_vals = parse_dc_log(dc_log)
    (pout / "op.txt").write_text(dc_log.read_text())
    write_op_table(pout / "op_table.csv", dc_vals, term, vga, ctle, driver)

    vcm = 0.5 * (dc_vals.get("v(inp)", term.vbase) + dc_vals.get("v(inn)", term.vbase))
    vtt = dc_vals.get("v(xu1.xuterm.vtt)", term.vbase)
    ctle_vds = dc_vals.get("v(xu1.xuctle.e1)", float("nan"))
    vga_ic_sig = (
        dc_vals.get("@q.xu1.xuvga.xq1.qnpn13g2[ic]", 0.0)
        + dc_vals.get("@q.xu1.xuvga.xq2.qnpn13g2[ic]", 0.0)
    ) / 2.0
    vga_ic_dum = (
        dc_vals.get("@q.xu1.xuvga.xqd1.qnpn13g2[ic]", 0.0)
        + dc_vals.get("@q.xu1.xuvga.xqd2.qnpn13g2[ic]", 0.0)
    ) / 2.0

    # --- Zin / return loss (mid gain) ---
    tb_zin = _prepare_chain_tb(
        spice_dir / "tb_zin.cir",
        dut_cir,
        work,
        models,
        spice_dir,
        dc_extra,
        cl_tb=dc_extra["CL_TB"],
    )
    _patch_nodeset(tb_zin, term, vga, ctle, driver)
    run_ngspice(tb_zin, work, "zin.log")
    freq_z, zdiff = _parse_zin_raw(work / "zin.raw")
    s11 = _s11_db(zdiff)
    s11_dc = float(s11[0])
    s11_28 = interp_db_at(freq_z, s11, NYQUIST_HZ)
    _plot_s11(freq_z, s11, pout / "zin.png")
    _write_s11_csv(pout / "zin.csv", freq_z, s11, zdiff)

    setting_metrics: list[GainSettingMetrics] = []

    for label, vctrl in gain_settings:
        extra = build_chain_extra(term, vga, driver, vctrl)
        _write_work_params(work, extra)
        dut_cir = _prepare_chain_dut(work, models, spice_dir, extra)
        tag = label
        raw_name = f"ac_diff_{tag}.raw"

        tb_ac = _write_ac_chain_tb(
            work, dut_cir, models, spice_dir, extra, cl_tb=extra["CL_TB"], raw_name=raw_name
        )
        run_ngspice(tb_ac, work, f"ac_diff_{tag}.log")
        ac = _ac_gains_from_raw(work / raw_name)
        freq = ac["freq"]
        h_src_db = ac["h_src_db"]
        h_pad_db = ac["h_pad_db"]
        dc_src = float(h_src_db[0])
        dc_pad = float(h_pad_db[0])
        ac_src_28 = interp_db_at(freq, h_src_db, NYQUIST_HZ)
        ac_pad_28 = interp_db_at(freq, h_pad_db, NYQUIST_HZ)
        peaking = ac_src_28 - dc_src
        peak_gain, f_peak, f_3db, f3_at_max = compute_ac_peak_metrics(freq, h_src_db)
        gd_s = group_delay_s(freq, ac["h_src"])

        plot_ac(
            freq,
            h_src_db,
            gd_s,
            pout / (f"ac_diff_{tag}.png" if tag != "mid" else "ac_diff.png"),
            peak_gain_db=peak_gain,
            f_peak_hz=f_peak,
            f_3db_hz=f_3db,
            f3db_at_fmax=f3_at_max,
            dc_gain_db=dc_src,
            peaking_db=peaking,
        )
        write_ac_diff_csv(
            pout / (f"ac_diff_{tag}.csv" if tag != "mid" else "ac_diff.csv"),
            freq,
            h_src_db,
            gd_s,
        )
        ac_tag = tag if tag != "mid" else "mid"
        plot_chain_ac_perstg(
            freq,
            ac,
            pstg / f"ac_diff_{ac_tag}.png",
            title=f"Chain AC per-stage ({label} VCTRL={vctrl:.2f} V)",
        )
        _write_ac_perstg_csv(pstg / f"ac_diff_{ac_tag}.csv", ac)

        gm = GainSettingMetrics(
            label=label,
            vctrl_v=vctrl,
            dc_gain_src_db=dc_src,
            ac_gain_src_28g_db=ac_src_28,
            dc_gain_pad_db=dc_pad,
            ac_gain_pad_28g_db=ac_pad_28,
            peaking_28g_db=peaking,
            peak_gain_db=peak_gain,
            f_peak_hz=f_peak,
            f_3db_hz=f_3db,
            gain_term_pad_db=interp_db_at(freq, ac["h_term_db"], NYQUIST_HZ),
            gain_ctle_db=interp_db_at(freq, ac["h_ctle_db"], NYQUIST_HZ),
            gain_vga_db=interp_db_at(freq, ac["h_vga_db"], NYQUIST_HZ),
            gain_driver_db=interp_db_at(freq, ac["h_driver_db"], NYQUIST_HZ),
        )
        setting_metrics.append(gm)

        if not no_tran:
            tmax = write_prbs_stim(work / "prbs_stim.inc", term.vbase)
            _patch_stim_source(work / "prbs_stim.inc")
            extra["TMAX"] = f"{tmax:.6e}"
            tb_tran = _write_tran_chain_tb(
                work, dut_cir, spice_dir, extra, "prbs_stim.inc", f"tran_{tag}.raw"
            )
            run_ngspice(tb_tran, work, f"tran_{tag}.log")
            tr = _parse_chain_tran_raw(work / f"tran_{tag}.raw")
            write_tran_csv(
                pout / (f"tran_{tag}.csv" if tag != "mid" else "tran.csv"),
                tr.time_s,
                tr.v_outp,
                tr.v_outn,
                tr.v_inp,
                tr.v_inn,
            )
            gm.drive_swing_mv = _pp_mv_from_tran(tr.time_s, tr.v_drv_inp - tr.v_drv_inn)
            gm.pad_swing_mv = _pp_mv_from_tran(tr.time_s, tr.v_outp - tr.v_outn)
            if tag == "mid":
                write_eye_csvs(pout, tr.time_s, tr.v_outp, tr.v_outn)
            plot_tran_se(
                tr.time_s, tr.v_outp, tr.v_outn, tr.v_inp, tr.v_inn,
                pout / (f"tran_se_{tag}.png" if tag != "mid" else "tran_se.png"),
            )
            plot_tran_diff(
                tr.time_s, tr.v_outp, tr.v_outn, tr.v_inp, tr.v_inn,
                pout / (f"tran_diff_{tag}.png" if tag != "mid" else "tran_diff.png"),
            )
            plot_chain_tran_perstg(
                tr.time_s,
                _chain_tran_perstg_vod_mv(tr),
                pstg / f"tran_diff_{tag}.png",
                title=f"Chain PRBS per-stage ({label} VCTRL={vctrl:.2f} V)",
            )
            plot_eye_se(
                tr.time_s, tr.v_outp, tr.v_outn,
                pout / (f"eye_se_{tag}.png" if tag != "mid" else "eye_se.png"),
            )
            plot_eye_diff(
                tr.time_s, tr.v_outp, tr.v_outn,
                pout / (f"eye_diff_{tag}.png" if tag != "mid" else "eye_diff.png"),
            )
            eye = extract_eye_metrics(tr.time_s, tr.v_outp, tr.v_outn)
            gm.eye = eye

            tmax_sbr = write_sbr_stim(work / "sbr_stim.inc", term.vbase)
            _patch_stim_source(work / "sbr_stim.inc")
            extra["TMAX"] = f"{tmax_sbr:.6e}"
            tb_sbr = _write_tran_chain_tb(
                work, dut_cir, spice_dir, extra, "sbr_stim.inc", f"sbr_{tag}.raw"
            )
            run_ngspice(tb_sbr, work, f"sbr_{tag}.log")
            tr_sbr = _parse_chain_tran_raw(work / f"sbr_{tag}.raw")
            sbr = extract_sbr(tr_sbr.time_s, tr_sbr.v_outp, tr_sbr.v_outn)
            gm.sbr = sbr
            write_tran_csv(
                pout / (f"sbr_{tag}.csv" if tag != "mid" else "sbr.csv"),
                tr_sbr.time_s,
                tr_sbr.v_outp,
                tr_sbr.v_outn,
                tr_sbr.v_inp,
                tr_sbr.v_inn,
            )
            write_sbr_taps_csv(
                pout / (f"sbr_taps_{tag}.csv" if tag != "mid" else "sbr_taps.csv"),
                sbr,
            )
            plot_sbr(
                tr_sbr.time_s,
                tr_sbr.v_outp,
                tr_sbr.v_outn,
                tr_sbr.v_inp,
                tr_sbr.v_inn,
                sbr,
                pout / (f"sbr_{tag}.png" if tag != "mid" else "sbr.png"),
            )
            perstg_sbr = _chain_sbr_perstg_stages(tr_sbr)
            if perstg_sbr:
                plot_chain_sbr_perstg(
                    tr_sbr.time_s,
                    perstg_sbr,
                    pstg / f"sbr_{tag}.png",
                    title=f"Chain SBR per-stage ({label} VCTRL={vctrl:.2f} V)",
                )

    # CMRR / PSRR at mid gain
    mid_extra = build_chain_extra(term, vga, driver, v_mid)
    _write_work_params(work, mid_extra)
    dut_cir = _prepare_chain_dut(work, models, spice_dir, mid_extra)
    mid_ac = next(s for s in setting_metrics if s.label == "mid")

    tb_cm = _prepare_chain_tb(
        spice_dir / "tb_ac_cm.cir", dut_cir, work, models, spice_dir, mid_extra,
        cl_tb=mid_extra["CL_TB"],
    )
    _patch_nodeset(tb_cm, term, vga, ctle, driver)
    run_ngspice(tb_cm, work, "ac_cm.log")
    freq_cm, voutp_cm, voutn_cm, vin_p_cm, vin_n_cm = parse_ac_raw(work / "ac_cm.raw")
    voc_cm = (voutp_cm + voutn_cm) / 2.0
    vic_cm = (vin_p_cm + vin_n_cm) / 2.0
    h_cm = np.where(np.abs(vic_cm) > 1e-30, voc_cm / vic_cm, 0.0)
    acm_db = float(20.0 * np.log10(max(np.abs(h_cm[0]), 1e-30)))
    cmrr_db = mid_ac.dc_gain_pad_db - acm_db
    h_diff_db = np.full_like(freq_cm, mid_ac.dc_gain_pad_db)
    cmrr_curve = h_diff_db - 20.0 * np.log10(np.maximum(np.abs(h_cm), 1e-30))
    plot_cmrr(freq_cm, cmrr_curve, pout / "cmrr.png")

    tb_psrr = _prepare_chain_tb(
        spice_dir / "tb_ac_psrr.cir", dut_cir, work, models, spice_dir, mid_extra,
        cl_tb=mid_extra["CL_TB"],
    )
    _patch_nodeset(tb_psrr, term, vga, ctle, driver)
    run_ngspice(tb_psrr, work, "ac_psrr.log")
    freq_p, voutp_p, voutn_p, vvdd = parse_psrr_raw(work / "ac_psrr.raw")
    vod_p = voutp_p - voutn_p
    vod_safe = np.maximum(np.abs(vod_p), 10.0 ** (-PSRR_MAX_DB / 20.0))
    h_psrr = np.abs(vvdd) / vod_safe
    psrr_curve = np.minimum(20.0 * np.log10(np.maximum(h_psrr, 1e-30)), PSRR_MAX_DB)
    psrr_db = float(psrr_curve[0])
    plot_psrr(freq_p, psrr_curve, pout / "psrr.png")

    ctle_sbr = _load_standalone_sbr("pdk")
    vga_sbr = _load_standalone_sbr("vga_pdk")
    mid_chain_sbr = next((s.sbr for s in setting_metrics if s.label == "mid"), None)
    write_isi_analysis(pout / "isi_analysis.csv", ctle_sbr, vga_sbr, mid_chain_sbr)

    metrics = ChainMetrics(
        vcm_pad_v=vcm,
        vtt_v=vtt,
        ctle_vds_tail_v=ctle_vds,
        vga_ic_signal_a=vga_ic_sig,
        vga_ic_dummy_a=vga_ic_dum,
        s11_dc_db=s11_dc,
        s11_28g_db=s11_28,
        settings=setting_metrics,
        cmrr_db=cmrr_db,
        psrr_db=psrr_db,
    )
    write_metrics_csv(pout / "metrics.csv", metrics, term, vga)

    standalone = _load_standalone_metrics()
    chain_cmp = {
        "term": setting_metrics[0].gain_term_pad_db if setting_metrics else float("nan"),
        "ctle": setting_metrics[1].gain_ctle_db if len(setting_metrics) > 1 else float("nan"),
        "vga": mid_ac.gain_vga_db,
        "chain_pad": mid_ac.ac_gain_pad_28g_db,
        "chain_src": mid_ac.ac_gain_src_28g_db,
    }
    write_stage_compare_csv(pout / "stage_compare.csv", chain_cmp, standalone)

    print("\n=== RX chain summary (term → CTLE → VGA → pad driver) ===")
    print(f"  Pad CM={vcm:.4f} V  vtt={vtt:.4f} V  CTLE VDS_tail={ctle_vds:.4f} V")
    print(f"  S11 DC={s11_dc:.1f} dB  S11@28G={s11_28:.1f} dB")
    print(
        f"  VGA headroom @ VDD=1.6 V: usable VCTRL <= {v_max_usable:.2f} "
        f"(VCTRL=1.0 {'ok' if headroom.get('vctrl_max_op_ok') else 'OP FAILED'})"
    )
    for s in setting_metrics:
        shortfall_db = 20.0 * math.log10(
            max(s.drive_swing_mv / 405.0, 1e-6)
        ) if not math.isnan(s.drive_swing_mv) else float("nan")
        print(
            f"  [{s.label}] VCTRL={s.vctrl_v:.2f} V: "
            f"E2E src {s.ac_gain_src_28g_db:.2f} dB (pad {s.ac_gain_pad_28g_db:.2f} dB) "
            f"drive {s.drive_swing_mv:.1f} mVpp → pad {s.pad_swing_mv:.1f} mVpp "
            f"(~{shortfall_db:.1f} dB vs 405 mVpp driver need)"
            f"  VGA@28G {s.gain_vga_db:.2f} dB  driver@28G {s.gain_driver_db:.2f} dB"
            + (f"  SBR ISI={s.sbr.isi_norm:.4f}" if s.sbr else "")
            + (
                f"  eye={s.eye.height_mV:.1f}mV x {s.eye.width_ui:.3f}UI"
                if s.eye
                else ""
            )
        )
    print(f"  CMRR={cmrr_db:.1f} dB  PSRR={psrr_db:.1f} dB")
    print(f"  Artifacts: {pout}/")
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Run RX chain ngspice verification")
    parser.add_argument("--no-tran", action="store_true")
    args = parser.parse_args()
    run(no_tran=args.no_tran)


if __name__ == "__main__":
    main()
