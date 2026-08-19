#!/usr/bin/env python3
"""Size 56 Gb/s NRZ output pad driver from char LUTs.

Derives VBASE from the VGA realized output CM (DC-coupled chain). Sizes shunt
peaking against pad+ESD capacitance, evaluates pre-driver need vs VGA FO2 load,
and documents back-termination trade (R_eff vs Bessel coil floor).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from char.common.lut import load_lut  # noqa: E402

from size_ctle import (  # noqa: E402
    MFD,
    TAIL_VDS_V,
    budget_cl_f,
    ctle_collector_cm,
    hbt_caps_at_bias,
    miller_cin,
    size_mos_tail,
    size_rppd,
    verify_rppd_ngspice,
)
from size_term import (  # noqa: E402
    ESD_C_FF_PER_PAD,
    pad_capacitance_f,
    size_term,
    to_extra as term_to_extra,
    VDD_DEFAULT_V,
    verify_rsil_ngspice,
    RSIL_W_UM,
    RSIL_L_UM,
)
from size_vga import (  # noqa: E402
    FO2_AV_MILLER,
    extra_params as vga_extra_params,
    pick_em_inductor,
    size_vga_for_chain,
)

NYQUIST_HZ = 28e9
SWING_DIFF_V = 0.400  # 400 mVpp differential at pad
ROUT_SER_OHM = 1e-3
BACK_RT_DISABLED_OHM = 1e12
VGA_FO2_CL_FF = 43.5  # VGA C_L budget (2×Miller + route) from size_vga


@dataclass
class DriverParams:
    nx: int = 2
    nx_idx: int = 1
    vbe: float = 0.0
    ic: float = 0.0
    ft_hz: float = 0.0
    gm: float = 0.0
    cbe_f: float = 0.0
    cbc_f: float = 0.0
    cin_lut_f: float = 0.0
    cin_miller_f: float = 0.0
    vdd: float = VDD_DEFAULT_V
    rd_ohm: float = 0.0
    rppd_r_ohm: float = 0.0
    l_h: float = 0.0
    l_em_case: str = ""
    l_em_nh: float = 0.0
    cl_pad_f: float = 0.0
    cl_f: float = 0.0
    mfd: float = 0.0
    m_bessel_target: float = MFD
    itail_a: float = 0.0
    mos_w_um: float = 0.0
    mos_l_um: float = 0.5
    mos_m: int = 1
    mos_vgs: float = 0.0
    vbase: float = 0.0  # driver input CM (= VGA output CM)
    vout_cm_est: float = 0.0
    rppd_w_um: float = 0.0
    rppd_l_um: float = 0.0
    back_term: bool = False
    r_eff_se_ohm: float = 50.0
    swing_target_v: float = SWING_DIFF_V
    predriver_needed: bool = False
    predriver_nx: int = 1
    predriver_cin_ff: float = 0.0
    driver_cin_ff: float = 0.0
    vga_fo2_ff: float = VGA_FO2_CL_FF
    pad_w_um: float = 70.0
    pad_l_um: float = 70.0
    pad_c_f: float = 0.0
    term_extra: dict[str, str] = field(default_factory=dict)


def _repo_paths() -> dict[str, Path]:
    return {
        "bjt": _REPO / "char/bjt/out/sg13_npn13G2.npz",
        "mos": _REPO / "char/mos/out/lv_core_n.npz",
        "rppd": _REPO / "char/passive/out/sg13_rppd.npz",
        "cmim": _REPO / "char/passive/out/sg13_cap_cmim.npz",
    }


def vga_output_cm_v() -> float:
    """VGA output CM at max gain from sized chain (VDD − Ic·RD_realized)."""
    vga = size_vga_for_chain()
    ep = vga_extra_params(vga, vctrl=max(vga.vctrl_v))
    ic_max = float(ep.get("ITAIL", vga.itail_a))
    rd_real = verify_rppd_ngspice(
        float(ep["RPPD_W"]) * 1e6,
        float(ep["RPPD_L"]) * 1e6,
    )
    return ctle_collector_cm(vga.vdd, ic_max, rd_real)


def pad_cl_f(pad_w_um: float = 70.0, pad_l_um: float = 70.0) -> float:
    """Per-side pad + ESD shunt C at output (before external 50 Ω term)."""
    pad_c = pad_capacitance_f(pad_w_um, pad_l_um)
    esd_c = ESD_C_FF_PER_PAD * 1e-15
    return pad_c + esd_c


def r_eff_single_ended(back_term: bool) -> float:
    """Effective per-leg load to vtt with 50 Ω far-end + optional on-chip back-term."""
    r_far = verify_rsil_ngspice(RSIL_W_UM, RSIL_L_UM)
    if not back_term:
        return r_far
    r_back = r_far
    return r_far * r_back / (r_far + r_back)


def itail_for_swing(swing_diff_v: float, r_eff_se_ohm: float, rd_shunt_ohm: float = 40.0) -> float:
    """Tail current for pad swing accounting for shunt-RD vs termination current split."""
    # Fraction of tail current reaching the 50 Ω termination (~R_term / (R_term + RD)).
    r_term = r_eff_se_ohm
    frac = r_term / (r_term + rd_shunt_ohm)
    i_ideal = swing_diff_v / (2.0 * r_eff_se_ohm)
    return i_ideal / max(frac, 0.35)


def pick_nx_idx_for_itail(
    bjt_path: Path,
    itail_target: float,
    vce_min: float = 0.9,
) -> tuple[int, int, float, float, float, float, float, float, float]:
    """Return smallest Nx that supports itail_target at max-fT bias."""
    arrays, _ = load_lut(bjt_path)
    nx_idx_pick = len(arrays["Nx"]) - 1
    for nx_idx, nx in enumerate(arrays["Nx"]):
        nx_i = int(nx)
        if itail_target <= 0.003 * nx_i * 1.02:
            nx_idx_pick = nx_idx
            break
    vbe, ic, gm, ft, cbe, cbc, cin_lut, vce_lut = hbt_caps_at_bias(
        bjt_path, nx_idx=nx_idx_pick, vce_min=vce_min, vce_max=1.15,
    )
    nx_i = int(arrays["Nx"][nx_idx_pick])
    return nx_idx_pick, nx_i, vbe, ic, gm, ft, cbe, cbc, cin_lut, vce_lut


def driver_gain_lin(gm: float, rd: float) -> float:
    """Small-signal gain (LUT gm already includes re)."""
    return gm * rd


def evaluate_predriver(driver_cin_f: float, vga_fo2_f: float) -> bool:
    return driver_cin_f > vga_fo2_f


def size_predriver(
    vbase: float,
    vbe: float,
    cl_driver_cin_f: float,
) -> tuple[int, float, float, float, float, float, float, float]:
    """Size Nx=1 pre-driver FO1 into driver Miller input."""
    paths = _repo_paths()
    nx_idx = 0
    vbe_p, ic, gm, ft, cbe, cbc, cin_lut, vce_lut = hbt_caps_at_bias(
        paths["bjt"], nx_idx=nx_idx,
    )
    cl_m, _, _ = budget_cl_f(cbe, cbc, FO2_AV_MILLER)
    # Replace FO2 with driver input C as load
    cl = cl_driver_cin_f
    av = driver_gain_lin(gm, 50.0)  # rough RD for predriver
    l_guess = MFD * 50.0 ** 2 * cl
    em_case, l_nh = pick_em_inductor(l_guess)
    l_h = l_nh * 1e-9
    rd = math.sqrt(l_h / (MFD * cl))
    return nx_idx, vbe_p, ic, gm, rd, l_h, em_case, l_nh


def size_driver(
    back_term: bool = False,
    swing_diff_v: float = SWING_DIFF_V,
    pad_w_um: float = 70.0,
    pad_l_um: float = 70.0,
    vbase: float | None = None,
) -> DriverParams:
    paths = _repo_paths()
    term = size_term()
    term_extra = term_to_extra(term)

    if vbase is None:
        vbase = vga_output_cm_v()

    r_eff = r_eff_single_ended(back_term)
    # Initial RD guess for current-split estimate, then refine after coil pick.
    rd_guess = 50.0
    itail = itail_for_swing(swing_diff_v, r_eff, rd_shunt_ohm=rd_guess)
    pad_cl = pad_cl_f(pad_w_um, pad_l_um)

    nx_idx, nx, vbe, ic_lut, gm, ft, cbe, cbc, cin_lut, vce_lut = pick_nx_idx_for_itail(
        paths["bjt"], itail,
    )
    itail = min(itail, 0.003 * nx * 0.98)

    tail_vds = vbase - vbe
    av_lin = driver_gain_lin(gm, 50.0)
    cin_m = miller_cin(cbe, cbc, av_lin)
    driver_cin_ff = cin_m * 1e15

    # Pad driver: lower RD (≈25 Ω realized) passes more current to the 50 Ω termination
    # while keeping Vcoll high for VCE headroom at VDD=1.65 V.
    rd_target = 25.0
    l_target = MFD * rd_target ** 2 * pad_cl
    em_case, l_nh = pick_em_inductor(l_target)
    l_h = l_nh * 1e-9
    rd = math.sqrt(l_h / (MFD * pad_cl))
    rd = min(rd, rd_target)
    rppd_w, rppd_l, rppd_r = size_rppd(paths["rppd"], rd, lut_scale=1.0)
    # Tail for 400 mVpp at pad with current split; cap by VCE headroom.
    itail = itail_for_swing(swing_diff_v, r_eff, rd_shunt_ohm=rppd_r)
    vce_min = 0.9
    tail_vds_est = vbase - vbe
    itail_vce_max = (VDD_DEFAULT_V - tail_vds_est - vce_min) / max(rppd_r, 1.0)
    itail = min(itail, itail_vce_max * 0.95, 0.003 * nx * 0.98)

    mos_w, mos_m, mos_vgs, mos_l = size_mos_tail(
        paths["mos"], itail, vds_target=max(tail_vds, TAIL_VDS_V),
    )

    vdd = vce_lut + itail * rppd_r + tail_vds
    vdd = max(1.35, min(vdd, VDD_DEFAULT_V))
    vout_cm = vdd - itail * rppd_r

    mfd = l_h / (rppd_r ** 2 * pad_cl)

    vga = size_vga_for_chain()
    vga_fo2 = vga.cl_f * 1e15
    predriver_needed = evaluate_predriver(cin_m, vga.cl_f)

    predriver_nx = 1
    if predriver_needed:
        _, _, _, _, _, _, em_p, _ = size_predriver(vbase, vbe, cin_m)

    return DriverParams(
        nx=nx,
        nx_idx=nx_idx,
        vbe=vbe,
        ic=ic_lut,
        ft_hz=ft,
        gm=gm,
        cbe_f=cbe,
        cbc_f=cbc,
        cin_lut_f=cin_lut,
        cin_miller_f=cin_m,
        vdd=vdd,
        rd_ohm=rd,
        rppd_r_ohm=rppd_r,
        l_h=l_h,
        l_em_case=em_case,
        l_em_nh=l_nh,
        cl_pad_f=pad_cl,
        cl_f=pad_cl,
        mfd=mfd,
        itail_a=itail,
        mos_w_um=mos_w,
        mos_l_um=mos_l,
        mos_m=mos_m,
        mos_vgs=mos_vgs,
        vbase=vbase,
        vout_cm_est=vout_cm,
        rppd_w_um=rppd_w,
        rppd_l_um=rppd_l,
        back_term=back_term,
        r_eff_se_ohm=r_eff,
        swing_target_v=swing_diff_v,
        predriver_needed=predriver_needed,
        predriver_nx=predriver_nx,
        driver_cin_ff=driver_cin_ff,
        vga_fo2_ff=vga_fo2,
        pad_w_um=pad_w_um,
        pad_l_um=pad_l_um,
        pad_c_f=pad_capacitance_f(pad_w_um, pad_l_um),
        term_extra=term_extra,
    )


def extra_params(params: DriverParams) -> dict[str, str]:
    spice_dir = Path(__file__).resolve().parents[1] / "spice"
    ind_inc = spice_dir / "ind_shunt.inc"
    te = dict(params.term_extra)
    back_r = (
        verify_rsil_ngspice(RSIL_W_UM, RSIL_L_UM)
        if params.back_term
        else BACK_RT_DISABLED_OHM
    )
    ep = {
        "Nx": str(params.nx),
        "VBE": f"{params.vbe:.6g}",
        "VBASE": f"{params.vbase:.6g}",
        "VDD": f"{params.vdd:.6g}",
        "RD": f"{params.rd_ohm:.6g}",
        "ITAIL": f"{params.itail_a:.6g}",
        "MOS_W": f"{params.mos_w_um:.6g}",
        "MOS_L": f"{params.mos_l_um:.6g}",
        "MOS_W_m": f"{params.mos_w_um * 1e-6:.12g}",
        "MOS_L_m": f"{params.mos_l_um * 1e-6:.12g}",
        "MOS_M": str(params.mos_m),
        "MOS_VGS": f"{params.mos_vgs:.6g}",
        "RPPD_W": f"{params.rppd_w_um * 1e-6:.6g}",
        "RPPD_L": f"{params.rppd_l_um * 1e-6:.6g}",
        "RPPD_W_m": f"{params.rppd_w_um * 1e-6:.12g}",
        "RPPD_L_m": f"{params.rppd_l_um * 1e-6:.12g}",
        "RPPD_R": f"{params.rppd_r_ohm:.6g}",
        "LLOAD": f"{params.l_h:.6g}",
        "CL": "0",
        "CL_TB": "0",
        "ROUT_SER": f"{ROUT_SER_OHM:.6g}",
        "BACK_RT_OHM": f"{back_r:.6g}",
        "PAD_W": f"{params.pad_w_um:.6g}",
        "PAD_L": f"{params.pad_l_um:.6g}",
        "PAD_C": f"{params.pad_c_f:.6g}",
        "ESD_M": te.get("ESD_M", "1"),
        "RSIL_W": te.get("RSIL_W", f"{RSIL_W_UM:.12g}e-6"),
        "RSIL_L": te.get("RSIL_L", f"{RSIL_L_UM:.12g}e-6"),
        "VTT_RTOP_W": te["VTT_RTOP_W"],
        "VTT_RTOP_L": te["VTT_RTOP_L"],
        "VTT_RBOT_W": te["VTT_RBOT_W"],
        "VTT_RBOT_L": te["VTT_RBOT_L"],
        "VTT_CAP_W": te["VTT_CAP_W"],
        "VTT_CAP_L": te["VTT_CAP_L"],
        "IND_SHUNT_INC": str(ind_inc.resolve()),
        "M_REALIZED": f"{params.mfd:.6g}",
        "R_EFF_SE": f"{params.r_eff_se_ohm:.6g}",
        "CL_PAD": f"{params.cl_pad_f:.6g}",
    }
    return ep


def print_summary(params: DriverParams) -> None:
    pad_ff = params.pad_c_f * 1e15
    esd_ff = ESD_C_FF_PER_PAD
    cl_ff = params.cl_pad_f * 1e15
    l_min_ph = 39.0
    m_at_lmin = l_min_ph * 1e-12 / (params.r_eff_se_ohm ** 2 * params.cl_pad_f)
    itail_back = itail_for_swing(params.swing_target_v, r_eff_single_ended(True))
    itail_no = itail_for_swing(params.swing_target_v, r_eff_single_ended(False))

    print("=== Output pad driver sizing ===")
    print(
        f"  Back-termination: {'ON (50||50=25 Ω/leg)' if params.back_term else 'OFF (50 Ω/leg far-end only)'}"
    )
    print(
        f"  R_eff per leg = {params.r_eff_se_ohm:.2f} Ω  "
        f"ITAIL target = {params.itail_a*1e3:.2f} mA for {params.swing_target_v*1e3:.0f} mVpp,diff"
    )
    print(
        f"  Arithmetic: with back-term ITAIL={itail_back*1e3:.1f} mA; "
        f"without={itail_no*1e3:.1f} mA"
    )
    print(f"  Nx={params.nx}  VBE={params.vbe:.4f} V  gm={params.gm:.4e} S")
    print(f"  VBASE (VGA out CM) = {params.vbase:.4f} V  VDD={params.vdd:.3f} V")
    print(
        f"  Pad C: {pad_ff:.1f} fF + ESD {esd_ff:.1f} fF = {cl_ff:.1f} fF/side"
    )
    print(
        f"  RD target={params.rd_ohm:.2f} Ω  realized rppd={params.rppd_r_ohm:.2f} Ω"
    )
    print(
        f"  L={params.l_h*1e12:.2f} pH ({params.l_em_case}, {params.l_em_nh*1e3:.1f} pH nominal)"
    )
    print(f"  m = L/(RD²·C_L) = {params.mfd:.3f}  (Bessel target {MFD})")
    if params.back_term:
        print(
            f"  Bessel floor: L_min≈{l_min_ph:.0f} pH → m_min≈{m_at_lmin:.2f} "
            f"(cannot reach {MFD} at R_eff=25 Ω)"
        )
    print(
        f"  Driver input C(Miller)={params.driver_cin_ff:.1f} fF  "
        f"VGA FO2 budget={params.vga_fo2_ff:.1f} fF"
    )
    print(
        f"  Pre-driver: {'NEEDED' if params.predriver_needed else 'not needed'} "
        f"(Nx=1 buffer if used)"
    )
    print(f"  Vout_CM(est)={params.vout_cm_est:.3f} V")


def main() -> None:
    parser = argparse.ArgumentParser(description="Size 56G output pad driver")
    parser.add_argument("--back-term", action="store_true", help="Enable on-chip 50 Ω back-termination")
    parser.add_argument("--swing-mv", type=float, default=400.0)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    if not os.environ.get("PDK_ROOT"):
        print("Warning: PDK_ROOT not set — rppd/rsil verification may use LUT.")

    params = size_driver(
        back_term=args.back_term,
        swing_diff_v=args.swing_mv * 1e-3,
    )
    print_summary(params)
    if args.json:
        d = {k: v for k, v in params.__dict__.items()}
        args.json.write_text(json.dumps(d, indent=2, default=str))


if __name__ == "__main__":
    main()
