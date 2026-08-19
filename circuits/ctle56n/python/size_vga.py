#!/usr/bin/env python3
"""Size 56 Gb/s NRZ CML VGA (post-CTLE) — tail-current steering architecture.

Ratiometric to the CTLE: same HBT unit (FO1 signal input only), FO2 load
(2× Miller-aware next-stage input C + one interconnect route per output side).
Gain control steers tail current between signal and dummy pairs (gm ~ Ic).
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
    CtleParams,
    MFD,
    MOS_VTH_V,
    TAIL_VDS_V,
    ctle_collector_cm,
    hbt_caps_at_bias,
    size_ctle,
    size_mos_tail,
    size_rppd,
)
from size_term import VDD_DEFAULT_V  # noqa: E402

VDD_V = VDD_DEFAULT_V  # 1.65 V — same rail as term / CTLE / chain

NYQUIST_HZ = 28e9

EM_INDUCTORS_NH = {
    "turn1_d40": 0.0629,
    "turn1_d60": 0.0995,
    "turn1_d80": 0.1349,
    "turn1": 0.2145,
}

ROUTING_CAP_FF_PER_UM = 0.17
INTERCONNECT_LENGTH_UM = 40.0

FO2_AV_MILLER = 2.0
STEER_RATIO_TARGET = 10.0
STEER_W_UM = 200.0
STEER_L_UM = 0.5
STEER_VOV_ON_V = 0.45
STEER_VGS_OFF_V = 0.08

RS_SIGNAL_OHM = 0.01


@dataclass
class VgaParams:
    """Sized VGA parameters (ideal + PDK tokens)."""

    nx: int = 1
    vbe: float = 0.0
    ic: float = 0.0
    ft_hz: float = 0.0
    gm: float = 0.0
    cbe_f: float = 0.0
    cbc_f: float = 0.0
    cin_lut_f: float = 0.0
    av_miller: float = FO2_AV_MILLER
    cin_miller_f: float = 0.0
    cl_miller_f: float = 0.0
    cl_interconnect_f: float = 0.0
    cl_f: float = 0.0
    vdd: float = 1.25
    rd_ohm: float = 0.0
    rs_ohm: float = 0.0
    l_h: float = 0.0
    l_target_nh: float = 0.0
    l_em_case: str = ""
    l_eff_boost_pct: float = 0.0
    mfd: float = 0.0
    itail_a: float = 0.0
    mos_w_um: float = 0.0
    mos_l_um: float = 0.5
    mos_m: int = 1
    mos_vgs: float = 0.0
    vbase: float = 0.0
    rppd_w_um: float = 0.0
    rppd_l_um: float = 0.0
    steer_w_um: float = 0.0
    steer_l_um: float = 0.5
    gain_ceiling_db: float = 0.0
    steer_ratio: float = 0.0
    vctrl_v: list[float] = field(default_factory=list)
    vctrl_p_v: list[float] = field(default_factory=list)
    vctrl_n_v: list[float] = field(default_factory=list)


def _repo_paths() -> dict[str, Path]:
    return {
        "bjt": _REPO / "char/bjt/out/sg13_npn13G2.npz",
        "mos": _REPO / "char/mos/out/lv_core_n.npz",
        "rppd": _REPO / "char/passive/out/sg13_rppd.npz",
    }


def miller_cin(cbe_f: float, cbc_f: float, av_lin: float) -> float:
    return cbe_f + cbc_f * (1.0 + abs(av_lin))


def interconnect_cap_per_side_f(
    length_um: float = INTERCONNECT_LENGTH_UM,
    cap_ff_per_um: float = ROUTING_CAP_FF_PER_UM,
) -> float:
    """One differential route to the next stage (per output-side C_L term)."""
    return cap_ff_per_um * 1e-15 * length_um


def pick_em_inductor(l_target_h: float) -> tuple[str, float]:
    best_case = ""
    best_l = 0.0
    best_err = float("inf")
    for case, l_nh in EM_INDUCTORS_NH.items():
        err = abs(l_nh * 1e-9 - l_target_h)
        if err < best_err:
            best_err = err
            best_case = case
            best_l = l_nh
    return best_case, best_l


def gain_ceiling_db(gm: float, rd: float) -> float:
    """Max small-signal gain with Rs→0. LUT gm already includes re degeneration."""
    av = gm * rd
    return 20.0 * math.log10(max(av, 1e-12))


def ind_eff_boost_pct(l_h: float, cox_f: float = 8.344e-15, f_hz: float = 74e9) -> float:
    """L_eff/L = 1/(1 - ω²LC) at frequency f (coil port C on nlp, not output load)."""
    w = 2.0 * math.pi * f_hz
    denom = 1.0 - w * w * l_h * cox_f
    if abs(denom) < 1e-6:
        return float("inf")
    return (1.0 / denom - 1.0) * 100.0


def derive_steering_gates(
    vctrl: float,
    ve_emitter_v: float = TAIL_VDS_V + 0.03,
) -> tuple[float, float]:
    """Map normalized VCTRL 0..1 to complementary steering gates (emitter-referenced)."""
    t = max(0.0, min(1.0, vctrl))
    v_on = ve_emitter_v + MOS_VTH_V + STEER_VOV_ON_V
    v_off = ve_emitter_v + STEER_VGS_OFF_V
    vp = v_off + t * (v_on - v_off)
    vn = v_on - t * (v_on - v_off)
    return round(vp, 3), round(vn, 3)


def derive_vctrl_list() -> list[float]:
    """Five settings spanning ~12 dB at 28 GHz (calibrated on ideal steering VGA)."""
    return [0.15, 0.28, 0.42, 0.65, 1.0]


def size_vga(
    nx_idx: int = 0,
    rs_ohm: float | None = None,
    steer_ratio: float = STEER_RATIO_TARGET,
    av_miller: float = FO2_AV_MILLER,
    vbase: float | None = None,
    tail_vds_v: float | None = None,
) -> VgaParams:
    paths = _repo_paths()
    vbe, ic, gm, ft, cbe, cbc, cin_lut, vce_lut = hbt_caps_at_bias(
        paths["bjt"], nx_idx=nx_idx
    )
    nx = int(load_lut(paths["bjt"])[0]["Nx"][nx_idx])

    if rs_ohm is None:
        rs_ohm = RS_SIGNAL_OHM

    if vbase is None:
        vbase = vbe + TAIL_VDS_V
    if tail_vds_v is None:
        tail_vds = vbase - vbe
    else:
        tail_vds = tail_vds_v

    cin_m = miller_cin(cbe, cbc, av_miller)
    cl_miller = 2.0 * cin_m
    cl_ic = interconnect_cap_per_side_f()
    cl = cl_miller + cl_ic

    rd_gain = 2.0 / gm
    l_guess = MFD * rd_gain ** 2 * cl
    em_case, l_nh = pick_em_inductor(l_guess)
    l_h = l_nh * 1e-9
    rd = math.sqrt(l_h / (MFD * cl))

    mos_w, mos_m, mos_vgs, mos_l = size_mos_tail(
        paths["mos"], ic, vds_target=tail_vds
    )
    rppd_w, rppd_l, _ = size_rppd(paths["rppd"], rd)

    vdd = vce_lut + ic * rd + tail_vds
    vdd = max(1.35, min(vdd, VDD_V))

    vctrl_list = derive_vctrl_list()
    vp_list: list[float] = []
    vn_list: list[float] = []
    for vc in vctrl_list:
        vp, vn = derive_steering_gates(vc, ve_emitter_v=tail_vds + 0.03)
        vp_list.append(vp)
        vn_list.append(vn)

    g_ceil = gain_ceiling_db(gm, rd)
    mfd = l_h / (rd ** 2 * cl)
    l_boost = ind_eff_boost_pct(l_h)

    return VgaParams(
        nx=nx,
        vbe=vbe,
        ic=ic,
        ft_hz=ft,
        gm=gm,
        cbe_f=cbe,
        cbc_f=cbc,
        cin_lut_f=cin_lut,
        av_miller=av_miller,
        cin_miller_f=cin_m,
        cl_miller_f=cl_miller,
        cl_interconnect_f=cl_ic,
        cl_f=cl,
        vdd=vdd,
        rd_ohm=rd,
        rs_ohm=rs_ohm,
        l_h=l_h,
        l_target_nh=l_nh,
        l_em_case=em_case,
        l_eff_boost_pct=l_boost,
        mfd=mfd,
        itail_a=ic,
        mos_w_um=mos_w,
        mos_l_um=mos_l,
        mos_m=mos_m,
        mos_vgs=mos_vgs,
        vbase=vbase,
        rppd_w_um=rppd_w,
        rppd_l_um=rppd_l,
        steer_w_um=STEER_W_UM,
        steer_l_um=STEER_L_UM,
        gain_ceiling_db=g_ceil,
        steer_ratio=steer_ratio,
        vctrl_v=vctrl_list,
        vctrl_p_v=vp_list,
        vctrl_n_v=vn_list,
    )


def size_vga_for_chain(
    term_params: object | None = None,
    ctle_params: CtleParams | None = None,
) -> VgaParams:
    """Size VGA with VBASE = CTLE collector CM (VDD − Ic·RD_realized)."""
    from size_term import size_term  # noqa: E402

    if term_params is None:
        term_params = size_term()
    if ctle_params is None:
        ctle_params = size_ctle(vbase_input=term_params.vbase)
    vbase = ctle_collector_cm(
        ctle_params.vdd, ctle_params.ic, ctle_params.rppd_r_ohm,
    )
    tail_vds = vbase - ctle_params.vbe
    return size_vga(vbase=vbase, tail_vds_v=tail_vds)


def extra_params(params: VgaParams, vctrl: float | None = None) -> dict[str, str]:
    vc = params.vctrl_v[len(params.vctrl_v) // 2] if vctrl is None else vctrl
    idx = min(
        range(len(params.vctrl_v)),
        key=lambda i: abs(params.vctrl_v[i] - vc),
    )
    vp = params.vctrl_p_v[idx]
    vn = params.vctrl_n_v[idx]
    spice_dir = Path(__file__).resolve().parents[1] / "spice"
    ind_inc = spice_dir / "ind_shunt.inc"
    return {
        "Nx": str(params.nx),
        "VBE": f"{params.vbe:.6g}",
        "VBASE": f"{params.vbase:.6g}",
        "VDD": f"{params.vdd:.6g}",
        "RD": f"{params.rd_ohm:.6g}",
        "RS": f"{params.rs_ohm:.6g}",
        "LLOAD": f"{params.l_h:.6g}",
        "CL": f"{params.cl_f:.6g}",
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
        "STEER_W": f"{params.steer_w_um:.6g}",
        "STEER_L": f"{params.steer_l_um:.6g}",
        "STEER_W_m": f"{params.steer_w_um * 1e-6:.12g}",
        "STEER_L_m": f"{params.steer_l_um * 1e-6:.12g}",
        "VCTRL": f"{vc:.6g}",
        "VCTRL_P": f"{vp:.6g}",
        "VCTRL_N": f"{vn:.6g}",
        "IND_SHUNT_INC": str(ind_inc.resolve()),
    }


def print_summary(params: VgaParams) -> None:
    vov = params.mos_vgs - MOS_VTH_V
    print("=== VGA sizing (tail-current steering) ===")
    print(f"  Nx={params.nx} (FO1 — signal pair only; dummy bases at VBASE)")
    print(f"  VBE={params.vbe:.4f} V  Ic_max={params.ic:.4e} A  gm={params.gm:.4e} S")
    print(f"  ft={params.ft_hz:.3e} Hz")
    print(
        f"  Gain ceiling (Rs→0, LUT gm already re-degenerated): "
        f"{params.gain_ceiling_db:.2f} dB  [gm*RD, sim ref ≈+3.88 dB]"
    )
    print(f"  CBE={params.cbe_f*1e15:.2f} fF  CBC={params.cbc_f*1e15:.3f} fF")
    print(
        f"  CIN_lut={params.cin_lut_f*1e15:.2f} fF "
        f"(Y11 RF input C; fan-out uses Miller CBE+CBC*(1+|Av|))"
    )
    print(
        f"  C_in(Miller, |Av|={params.av_miller:.1f})="
        f"{params.cin_miller_f*1e15:.2f} fF"
    )
    print(f"  C_L per side: FO2 2×Miller = {params.cl_miller_f*1e15:.2f} fF")
    print(
        f"  + interconnect (1×{INTERCONNECT_LENGTH_UM:.0f} µm "
        f"@ {ROUTING_CAP_FF_PER_UM:.2f} fF/µm) = "
        f"{params.cl_interconnect_f*1e15:.2f} fF"
    )
    print(
        f"  C_L total (per side) = {params.cl_f*1e15:.2f} fF  "
        f"(coil port C on nlp — not in C_L)"
    )
    print(
        f"  L_eff boost from coil port C ≈ +{params.l_eff_boost_pct:.0f}% @ 74 GHz"
    )
    print(f"  RD={params.rd_ohm:.2f} Ω  RS(signal)={params.rs_ohm:.2f} Ω  (fixed)")
    print(
        f"  L={params.l_h*1e12:.2f} pH  EM case={params.l_em_case} "
        f"({params.l_target_nh*1e3:.1f} pH nominal)"
    )
    print(f"  m = L/(RD² C_L) = {params.mfd:.3f}  (target {MFD})")
    print(f"  VDD={params.vdd:.3f} V  VBASE={params.vbase:.3f} V  ITAIL={params.itail_a:.4e} A")
    print(
        f"  MOS tail W={params.mos_w_um:.1f} µm  steer NMOS W={params.steer_w_um:.1f} µm"
    )
    print(f"  Steering ratio target: {params.steer_ratio:.1f}:1 (~20 dB)")
    print(f"  VCTRL (norm): {params.vctrl_v}")
    print(f"  Nyquist = {NYQUIST_HZ/1e9:.0f} GHz")


def main() -> None:
    parser = argparse.ArgumentParser(description="Size 56G CML VGA from LUTs")
    parser.add_argument("--nx-idx", type=int, default=0)
    parser.add_argument("--rs", type=float, default=None, help="Signal-pair Rs (Ω)")
    parser.add_argument("--json", type=Path, help="Write JSON summary")
    args = parser.parse_args()

    if not os.environ.get("PDK_ROOT"):
        print("Warning: PDK_ROOT not set — assume env.sh was sourced for SPICE runs.")

    params = size_vga_for_chain()
    if args.rs is not None:
        params = size_vga(
            vbase=params.vbase,
            tail_vds_v=params.vbase - params.vbe,
            rs_ohm=args.rs,
        )
    print_summary(params)
    if args.json:
        d = params.__dict__.copy()
        d["vctrl_v"] = list(params.vctrl_v)
        d["vctrl_p_v"] = list(params.vctrl_p_v)
        d["vctrl_n_v"] = list(params.vctrl_n_v)
        args.json.write_text(json.dumps(d, indent=2))


if __name__ == "__main__":
    main()
