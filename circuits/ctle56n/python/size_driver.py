#!/usr/bin/env python3
"""Size 56 Gb/s NRZ output pad driver from char LUTs.

CML shunt-peaked pair (no degeneration), single MOS tail, on-chip 50 Ohm/leg
back-termination (rsil), pad+ESD inside DUT. Floating 100 Ohm diff load is TB-only.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from char.common.lut import load_lut  # noqa: E402

from size_ctle import (  # noqa: E402
    MFD,
    TAIL_VDS_V,
    ctle_collector_cm,
    hbt_caps_at_bias,
    miller_cin,
    size_mos_tail,
    snap_drawable_mos_w,
)
from size_vga import pick_em_inductor, size_vga_for_chain  # noqa: E402
from size_term import (  # noqa: E402
    ESD_C_FF_PER_PAD,
    RSIL_L_UM,
    RSIL_W_UM,
    Z0_DIFF_OHM,
    Z0_SE_OHM,
    pad_capacitance_f,
    verify_rsil_ngspice,
)

NYQUIST_HZ = 28e9
VDD_V = 1.6
ITAIL_TARGET_A = 8.0e-3
RD_ON_CHIP_OHM = 50.0
SWING_DIFF_V = 0.400
R_EFF_AC_SE_OHM = RD_ON_CHIP_OHM * Z0_DIFF_OHM / (2.0 * RD_ON_CHIP_OHM + Z0_DIFF_OHM)
L_COIL_MIN_PH = 39.0
PAD_W_UM = 70.0
PAD_L_UM = 70.0


@dataclass
class DriverParams:
    nx: int = 3
    nx_idx: int = 2
    vbe: float = 0.0
    ic: float = 0.0
    ft_hz: float = 0.0
    gm: float = 0.0
    cbe_f: float = 0.0
    cbc_f: float = 0.0
    cin_lut_f: float = 0.0
    cin_miller_f: float = 0.0
    vdd: float = VDD_V
    rd_on_chip_ohm: float = RD_ON_CHIP_OHM
    rsil_r_ohm: float = 0.0
    rsil_w_um: float = RSIL_W_UM
    rsil_l_um: float = RSIL_L_UM
    l_h: float = 0.0
    l_em_case: str = ""
    l_em_nh: float = 0.0
    cl_pad_f: float = 0.0
    m_realized: float = 0.0
    m_bessel_target: float = MFD
    l_bessel_target_ph: float = 0.0
    itail_a: float = ITAIL_TARGET_A
    mos_w_um: float = 0.0
    tail_w_um: float = 0.0
    mos_l_um: float = 0.5
    mos_m: int = 2
    mos_vgs: float = 0.0
    vbase: float = 0.0
    vout_cm_est: float = 0.0
    vce_est: float = 0.0
    pad_w_um: float = PAD_W_UM
    pad_l_um: float = PAD_L_UM
    pad_c_f: float = 0.0
    esd_m: int = 1
    driver_cin_ff: float = 0.0
    vga_fo2_ff: float = 0.0
    vga_bw_penalty_pct: float = 0.0


def _repo_paths() -> dict[str, Path]:
    return {
        "bjt": _REPO / "char/bjt/out/sg13_npn13G2.npz",
        "mos": _REPO / "char/mos/out/lv_core_n.npz",
        "rsil": _REPO / "char/passive/out/sg13_rsil.npz",
    }


def vga_output_cm_v() -> float:
    """VGA output CM at max gain from sized chain (VDD − Ic·RD_realized)."""
    from size_ctle import verify_rppd_ngspice  # noqa: E402
    from size_vga import extra_params as vga_extra_params  # noqa: E402

    vga = size_vga_for_chain()
    ep = vga_extra_params(vga, vctrl=max(vga.vctrl_v))
    ic_max = float(ep.get("ITAIL", vga.itail_a))
    rd_real = verify_rppd_ngspice(
        float(ep["RPPD_W"]) * 1e6,
        float(ep["RPPD_L"]) * 1e6,
    )
    return ctle_collector_cm(vga.vdd, ic_max, rd_real)


def pad_cl_per_side_f(pad_w_um: float = PAD_W_UM, pad_l_um: float = PAD_L_UM) -> float:
    """Per-side pad + ESD shunt C at the output node."""
    return pad_capacitance_f(pad_w_um, pad_l_um) + ESD_C_FF_PER_PAD * 1e-15


def pick_nx_for_itail(bjt_path: Path, itail_a: float) -> tuple[int, int]:
    """Smallest Nx index whose per-device Ic limit supports ITAIL/2 per side."""
    arrays, _ = load_lut(bjt_path)
    ic_side = itail_a / 2.0
    for nx_idx, nx in enumerate(arrays["Nx"]):
        if ic_side <= 0.003 * float(nx) * 0.98:
            return nx_idx, int(nx)
    return len(arrays["Nx"]) - 1, int(arrays["Nx"][-1])


def size_rsil_to_target(
    rsil_path: Path,
    target_ohm: float = RD_ON_CHIP_OHM,
) -> tuple[float, float, float]:
    """Pick rsil geometry whose ngspice OP R is closest to target."""
    arrays, _ = load_lut(rsil_path)
    temp_idx = int(np.argmin(np.abs(arrays["TEMP"] - 27.0)))
    ranked: list[tuple[float, float, float]] = []
    for wi, w in enumerate(arrays["W"]):
        for li, l in enumerate(arrays["L"]):
            if float(w) < 0.3 or float(l) < 0.5:
                continue
            r_lut = float(arrays["R"][wi, li, temp_idx])
            ranked.append((abs(r_lut - target_ohm), float(w), float(l)))
    ranked.sort(key=lambda t: t[0])

    best = (RSIL_W_UM, RSIL_L_UM, target_ohm)
    best_err = float("inf")
    seen: set[tuple[float, float]] = set()
    for _, w_um, l_um in ranked[:20]:
        key = (round(w_um, 6), round(l_um, 6))
        if key in seen:
            continue
        seen.add(key)
        r_meas = verify_rsil_ngspice(w_um, l_um)
        err = abs(r_meas - target_ohm)
        if err < best_err:
            best_err = err
            best = (w_um, l_um, r_meas)
    # Always try the documented 50 Ohm geometry
    for w_um, l_um in ((RSIL_W_UM, RSIL_L_UM), (0.5, 2.35), (0.5, 2.5)):
        r_meas = verify_rsil_ngspice(w_um, l_um)
        err = abs(r_meas - target_ohm)
        if err < best_err:
            best_err = err
            best = (w_um, l_um, r_meas)
    return best


def driver_gain_lin(gm: float, rd_ohm: float) -> float:
    """Small-signal gain; LUT gm already includes intrinsic re."""
    return gm * rd_ohm


def size_driver(
    itail_a: float = ITAIL_TARGET_A,
    vdd: float = VDD_V,
    pad_w_um: float = PAD_W_UM,
    pad_l_um: float = PAD_L_UM,
    vbase: float | None = None,
) -> DriverParams:
    paths = _repo_paths()
    if vbase is None:
        vbase = vga_output_cm_v()

    rsil_w, rsil_l, rsil_r = size_rsil_to_target(paths["rsil"], RD_ON_CHIP_OHM)
    cl_pad = pad_cl_per_side_f(pad_w_um, pad_l_um)

    nx_idx, nx = pick_nx_for_itail(paths["bjt"], itail_a)
    vbe, ic_lut, gm, ft, cbe, cbc, cin_lut, vce_lut = hbt_caps_at_bias(
        paths["bjt"], nx_idx=nx_idx, vce_min=0.9, vce_max=1.15,
    )

    av_lin = driver_gain_lin(gm, rsil_r)
    cin_m = miller_cin(cbe, cbc, av_lin)

    # Bessel L at m=0.32 using on-chip RD and per-side C_L at pad
    l_bessel_h = MFD * rsil_r ** 2 * cl_pad
    l_bessel_ph = l_bessel_h * 1e12
    # Minimum buildable coil — target below this is unreachable
    em_case, l_nh = pick_em_inductor(l_bessel_h)
    l_h = l_nh * 1e-9
    m_real = l_h / (rsil_r ** 2 * cl_pad)

    tail_vds = vbase - vbe
    mos_w, mos_m_unit, mos_vgs, mos_l = size_mos_tail(
        paths["mos"], itail_a, vds_target=max(tail_vds, TAIL_VDS_V),
    )
    mos_w = snap_drawable_mos_w(mos_w)
    tail_w = snap_drawable_mos_w(2.0 * mos_w)

    vout_cm = vdd - (itail_a / 2.0) * rsil_r
    vce = vout_cm - (vbase - vbe)

    vga = size_vga_for_chain()
    vga_fo2_ff = vga.cl_f * 1e15
    driver_cin_ff = cin_m * 1e15
    vga_bw_penalty = (driver_cin_ff / vga_fo2_ff - 1.0) * 100.0 if vga_fo2_ff > 0 else float("nan")

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
        rd_on_chip_ohm=RD_ON_CHIP_OHM,
        rsil_r_ohm=rsil_r,
        rsil_w_um=rsil_w,
        rsil_l_um=rsil_l,
        l_h=l_h,
        l_em_case=em_case,
        l_em_nh=l_nh,
        cl_pad_f=cl_pad,
        m_realized=m_real,
        l_bessel_target_ph=l_bessel_ph,
        itail_a=itail_a,
        mos_w_um=mos_w,
        tail_w_um=tail_w,
        mos_l_um=mos_l,
        mos_m=1,
        mos_vgs=mos_vgs,
        vbase=vbase,
        vout_cm_est=vout_cm,
        vce_est=vce,
        pad_w_um=pad_w_um,
        pad_l_um=pad_l_um,
        pad_c_f=pad_capacitance_f(pad_w_um, pad_l_um),
        esd_m=1,
        driver_cin_ff=driver_cin_ff,
        vga_fo2_ff=vga_fo2_ff,
        vga_bw_penalty_pct=vga_bw_penalty,
    )


DEFAULT_OUT = Path(__file__).resolve().parents[1] / "spice" / "driver_params.inc"


def write_driver_params_inc(params: DriverParams, path: Path) -> None:
    """Emit spice/driver_params.inc from the same tokens as extra_params()."""
    d = extra_params(params)
    lines = ["* Pad driver parameters — generated by size_driver.py"]
    for k, v in d.items():
        lines.append(f".param {k}={v}")
    path.write_text("\n".join(lines) + "\n")


def extra_params(params: DriverParams) -> dict[str, str]:
    spice_dir = Path(__file__).resolve().parents[1] / "spice"
    ind_inc = spice_dir / "ind_shunt.inc"
    return {
        "Nx": str(params.nx),
        "VBE": f"{params.vbe:.6g}",
        "VBASE": f"{params.vbase:.6g}",
        "VDD": f"{params.vdd:.6g}",
        "ITAIL": f"{params.itail_a:.6g}",
        "ITAIL_HALF": f"{params.itail_a / 2.0:.6g}",
        "MOS_W": f"{params.mos_w_um:.6g}",
        "MOS_L": f"{params.mos_l_um:.6g}",
        "MOS_W_m": f"{params.mos_w_um * 1e-6:.12g}",
        "MOS_L_m": f"{params.mos_l_um * 1e-6:.12g}",
        "TAIL_W_m": f"{params.tail_w_um * 1e-6:.12g}",
        "MOS_M": "1",
        "MOS_VGS": f"{params.mos_vgs:.6g}",
        "RSIL_W": f"{params.rsil_w_um * 1e-6:.12g}",
        "RSIL_L": f"{params.rsil_l_um * 1e-6:.12g}",
        "RSIL_R": f"{params.rsil_r_ohm:.6g}",
        "RD_ON_CHIP": f"{params.rd_on_chip_ohm:.6g}",
        "RDIFF_TB": f"{Z0_DIFF_OHM:.6g}",
        "R_EFF_AC_SE": f"{R_EFF_AC_SE_OHM:.6g}",
        "LLOAD": f"{params.l_h:.6g}",
        "CL_TB": "0",
        "PAD_W": f"{params.pad_w_um:.6g}",
        "PAD_L": f"{params.pad_l_um:.6g}",
        "PAD_C": f"{params.pad_c_f:.6g}",
        "ESD_M": str(params.esd_m),
        "CL_PAD": f"{params.cl_pad_f:.6g}",
        "M_REALIZED": f"{params.m_realized:.6g}",
        "M_BESSEL": f"{MFD:.6g}",
        "L_BESSEL_PH": f"{params.l_bessel_target_ph:.6g}",
        "IND_SHUNT_INC": str(ind_inc.resolve()),
    }


def print_summary(params: DriverParams) -> None:
    pad_ff = params.pad_c_f * 1e15
    esd_ff = ESD_C_FF_PER_PAD
    cl_ff = params.cl_pad_f * 1e15
    swing_est = 2.0 * params.itail_a * R_EFF_AC_SE_OHM * 1e3
    l_bessel_ac_ph = MFD * R_EFF_AC_SE_OHM ** 2 * params.cl_pad_f * 1e12

    print("=== Output pad driver sizing ===")
    print("  Topology: CML pair, no Rs, single MOS tail (2W), shunt-peaked rsil load")
    print(f"  VDD={params.vdd:.3f} V  VBASE (VGA out CM)={params.vbase:.4f} V")
    print(f"  ITAIL={params.itail_a*1e3:.2f} mA  Nx={params.nx}  VBE={params.vbe:.4f} V")
    print(
        f"  RD_on_chip target={params.rd_on_chip_ohm:.1f} Ω  "
        f"rsil realized={params.rsil_r_ohm:.2f} Ω  "
        f"({params.rsil_w_um:.3f}×{params.rsil_l_um:.3f} µm)"
    )
    print(
        f"  R_eff AC half-circuit = RD||(100/2) = {R_EFF_AC_SE_OHM:.1f} Ω  "
        f"→ swing ≈ {swing_est:.0f} mVpp,diff"
    )
    print(f"  Vout_CM(est)={params.vout_cm_est:.3f} V  VCE(est)={params.vce_est:.3f} V")
    print(f"  Pad C={pad_ff:.1f} fF + ESD={esd_ff:.1f} fF → C_L={cl_ff:.1f} fF/side")
    print(
        f"  L={params.l_h*1e12:.1f} pH ({params.l_em_case})  "
        f"m=L/(RD²·C_L)={params.m_realized:.3f}  (Bessel target {MFD})"
    )
    print(
        f"  Bessel L@m=0.32: {params.l_bessel_target_ph:.1f} pH (on-chip RD) — "
        f"NOT reachable; coil floor ≈{L_COIL_MIN_PH:.0f} pH"
    )
    print(
        f"  AC-effective Bessel L@R_eff={R_EFF_AC_SE_OHM:.0f}Ω would need "
        f"{l_bessel_ac_ph:.1f} pH (also below coil floor)"
    )
    print(
        f"  Driver C_in(Miller)={params.driver_cin_ff:.1f} fF  "
        f"VGA FO2 budget={params.vga_fo2_ff:.1f} fF  "
        f"(+{params.vga_bw_penalty_pct:.0f}% load → VGA BW cost)"
    )
    print(f"  MOS tail W={params.tail_w_um:.3f} µm (2× mirror W={params.mos_w_um:.3f} µm), m=1")


def main() -> None:
    parser = argparse.ArgumentParser(description="Size 56G output pad driver")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    if not os.environ.get("PDK_ROOT"):
        print("Warning: PDK_ROOT not set — rsil verification may use LUT.")

    params = size_driver()
    write_driver_params_inc(params, DEFAULT_OUT)
    print_summary(params)
    if args.json:
        d = {k: v for k, v in params.__dict__.items()}
        args.json.write_text(json.dumps(d, indent=2, default=str))


if __name__ == "__main__":
    main()
