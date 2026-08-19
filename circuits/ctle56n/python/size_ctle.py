#!/usr/bin/env python3
"""Size 56 Gb/s NRZ CML CTLE from char LUTs and emit spice/params.inc."""

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

NYQUIST_HZ = 28e9
F_Z_HZ = 10e9  # degeneration zero — below Nyquist for ~6 dB peaking at 28 GHz
MFD = 0.32
RPPD_RSH = 260.0  # Ω/sq typ (cornerRES.lib)
TAIL_VDS_V = 0.35  # target NMOS tail VDS (V), must exceed Vov
MOS_VTH_V = 0.45
MOS_VGS_MIN_V = 0.55
MOS_VGS_MAX_V = 0.70
W_MARGIN = 1.75  # modest W boost for ro / matching (not a triode substitute)


@dataclass
class CtleParams:
    nx: int = 1
    vbe: float = 0.0
    ic: float = 0.0
    ft_hz: float = 0.0
    cin_f: float = 0.0
    gm: float = 0.0
    vdd: float = 1.25
    rd_ohm: float = 0.0
    rs_ohm: float = 0.0
    cs_f: float = 0.0
    l_h: float = 0.0
    cl_f: float = 0.0
    itail_a: float = 0.0
    mos_w_um: float = 0.0
    mos_l_um: float = 0.5
    mos_m: int = 1
    mos_vgs: float = 0.0
    rppd_w_um: float = 0.0
    rppd_l_um: float = 0.0
    cap_w_um: float = 0.0
    cap_l_um: float = 0.0
    cs_ideal: bool = True
    vbase: float = 0.0
    scale: float = 1.0


def _repo_paths() -> dict[str, Path]:
    return {
        "bjt": _REPO / "char/bjt/out/sg13_npn13G2.npz",
        "mos": _REPO / "char/mos/out/lv_core_n.npz",
        "rppd": _REPO / "char/passive/out/sg13_rppd.npz",
        "cmim": _REPO / "char/passive/out/sg13_cap_cmim.npz",
    }


def pick_hbt_bias(
    bjt_path: Path,
    nx_idx: int = 0,
    vce_min: float = 0.9,
    vce_max: float = 1.2,
) -> tuple[float, float, float, float, float, float]:
    """Return (vbe, ic, gm, ft, cin, vce_lut) at max FT within VCE window."""
    arrays, _ = load_lut(bjt_path)
    nx = float(arrays["Nx"][nx_idx])
    ic_limit = 0.003 * nx
    best_ft = -1.0
    best = None
    for vi, vce in enumerate(arrays["VCE"]):
        if vce < vce_min - 0.05 or vce > vce_max + 0.05:
            continue
        for bi, vbe in enumerate(arrays["VBE"]):
            ic = float(arrays["IC"][nx_idx, vi, bi])
            if ic <= 0 or ic >= ic_limit:
                continue
            ft = float(arrays["FT"][nx_idx, vi, bi])
            if ft > best_ft:
                best_ft = ft
                best = (
                    float(vbe),
                    ic,
                    float(arrays["GM"][nx_idx, vi, bi]),
                    ft,
                    float(arrays["CIN"][nx_idx, vi, bi]),
                    float(vce),
                )
    if best is None:
        raise RuntimeError("No valid HBT bias point in VCE window")
    return best


def lut_interp_id(
    mos_arrays: dict[str, np.ndarray],
    l_um: float,
    vgs: float,
    vds: float,
    vsb: float = 0.0,
) -> float:
    """Bilinear-ish ID (A) for W=1 µm reference."""
    l_idx = int(np.argmin(np.abs(mos_arrays["L"] - l_um)))
    vsb_idx = int(np.argmin(np.abs(mos_arrays["VSB"] - vsb)))
    id_surf = mos_arrays["ID"][l_idx, :, :, vsb_idx]
    vgs_vals = mos_arrays["VGS"]
    vds_vals = mos_arrays["VDS"]
    vgs_lo = max(0, np.searchsorted(vgs_vals, vgs) - 1)
    vgs_hi = min(len(vgs_vals) - 1, vgs_lo + 1)
    vds_lo = max(0, np.searchsorted(vds_vals, vds) - 1)
    vds_hi = min(len(vds_vals) - 1, vds_lo + 1)
    tg = (vgs - vgs_vals[vgs_lo]) / max(vgs_vals[vgs_hi] - vgs_vals[vgs_lo], 1e-12)
    td = (vds - vds_vals[vds_lo]) / max(vds_vals[vds_hi] - vds_vals[vds_lo], 1e-12)
    id00 = id_surf[vgs_lo, vds_lo]
    id01 = id_surf[vgs_lo, vds_hi]
    id10 = id_surf[vgs_hi, vds_lo]
    id11 = id_surf[vgs_hi, vds_hi]
    id0 = id00 * (1 - td) + id01 * td
    id1 = id10 * (1 - td) + id11 * td
    return float(id0 * (1 - tg) + id1 * tg)


def size_mos_tail(
    mos_path: Path,
    itail: float,
    vds_target: float = TAIL_VDS_V,
    w_margin: float = W_MARGIN,
    w_max_um: float = 400.0,
) -> tuple[float, float, int, float, float]:
    """Return (w_um, m, vgs, l_um) with tail in saturation (VDS > Vov)."""
    arrays, _ = load_lut(mos_path)
    best: tuple[float, float, int, float, float] | None = None

    for l_um in (1.0, 0.5):
        for vgs in np.linspace(MOS_VGS_MIN_V, MOS_VGS_MAX_V, 40):
            vov = float(vgs) - MOS_VTH_V
            # Saturation: VDS must exceed Vov (with small margin)
            if vov <= 0.05 or vov >= vds_target - 0.08:
                continue
            id_per_um = lut_interp_id(arrays, l_um, float(vgs), vds_target)
            if id_per_um <= 0:
                continue
            w_um = itail / id_per_um * w_margin
            if w_um > w_max_um or w_um < 0.5:
                continue
            # Prefer longer L (higher ro) and comfortable saturation margin
            sat_margin = vds_target - vov
            ro_score = l_um / w_um
            score = sat_margin * 10.0 + ro_score
            if best is None or score > best[0]:
                best = (score, w_um, 1, float(vgs), float(l_um))

    if best is None:
        raise RuntimeError(
            f"Could not size saturated MOS tail (VDS={vds_target} V, "
            f"VGS in [{MOS_VGS_MIN_V}, {MOS_VGS_MAX_V}])"
        )
    _, w_um, m, vgs, l_um = best
    return w_um, m, vgs, l_um


def size_rppd(rppd_path: Path, rd_target: float) -> tuple[float, float, float]:
    """Pick (w_um, l_um, R_lut) from LUT closest to inflated target.

    LUT sheet R is low-side vs silicon (contact R); target ~rd/0.88 so PDK
    DC gain lands in the −6…0 dB window.
    """
    lut_target = rd_target / 0.88
    arrays, _ = load_lut(rppd_path)
    temp_idx = int(np.argmin(np.abs(arrays["TEMP"] - 27.0)))
    best_err = float("inf")
    best = (5.0, 1.0, lut_target)
    for wi, w in enumerate(arrays["W"]):
        for li, l in enumerate(arrays["L"]):
            if float(w) < 0.5 or float(l) < 0.5:
                continue
            r = float(arrays["R"][wi, li, temp_idx])
            err = abs(r - lut_target)
            if err < best_err:
                best_err = err
                best = (float(w), float(l), r)
    return best


def compute_elements(
    gm: float,
    cin_f: float,
    ic: float,
    scale: float = 1.0,
    peaking_db_target: float = 7.0,
) -> tuple[float, float, float, float, float]:
    """Return RD, Rs, Cs, L, VDD suggestion."""
    cl = cin_f
    deg_factor = 10 ** (peaking_db_target / 20.0)
    rs = max(2.0 * (deg_factor - 1.0) / gm, 1.0)
    rd = deg_factor / gm
    rd *= scale
    rs *= scale
    cs = 1.0 / (2.0 * math.pi * F_Z_HZ * rs)
    l_h = MFD * rd ** 2 * cl
    v_em = TAIL_VDS_V
    target_vce = 1.05
    vdd = target_vce + ic * rd + v_em
    vdd = max(1.35, min(vdd, 1.70))
    return rd, rs, cs, l_h, vdd


def size_ctle(
    nx_idx: int = 0,
    scale: float = 1.0,
    peaking_db: float = 7.0,
) -> CtleParams:
    paths = _repo_paths()
    vbe, ic, gm, ft, cin, _ = pick_hbt_bias(paths["bjt"], nx_idx=nx_idx)
    nx = int(load_lut(paths["bjt"])[0]["Nx"][nx_idx])
    rd, rs, cs, l_h, vdd = compute_elements(
        gm, cin, ic, scale=scale, peaking_db_target=peaking_db
    )
    itail = 2.0 * ic
    mos_w, mos_m, mos_vgs, mos_l = size_mos_tail(paths["mos"], itail)
    rppd_w, rppd_l, _ = size_rppd(paths["rppd"], rd)
    vbase = vbe + TAIL_VDS_V

    return CtleParams(
        nx=nx,
        vbe=vbe,
        ic=ic,
        ft_hz=ft,
        cin_f=cin,
        gm=gm,
        vdd=vdd,
        rd_ohm=rd,
        rs_ohm=rs,
        cs_f=cs,
        l_h=l_h,
        cl_f=cin,
        itail_a=itail,
        mos_w_um=mos_w,
        mos_l_um=mos_l,
        mos_m=mos_m,
        mos_vgs=mos_vgs,
        rppd_w_um=rppd_w,
        rppd_l_um=rppd_l,
        cap_w_um=7.0,
        cap_l_um=7.0,
        cs_ideal=True,
        vbase=vbase,
        scale=scale,
    )


def write_params_inc(params: CtleParams, path: Path) -> None:
    rs_half = params.rs_ohm / 2.0
    lines = [
        "* CTLE parameters — generated by size_ctle.py",
        f".param Nx={params.nx}",
        f".param VBE={params.vbe:.6g}",
        f".param VBASE={params.vbase:.6g}",
        f".param VDD={params.vdd:.6g}",
        f".param RD={params.rd_ohm:.6g}",
        f".param RS={params.rs_ohm:.6g}",
        f".param RSHALF={rs_half:.6g}",
        f".param CS={params.cs_f:.6g}",
        f".param LLOAD={params.l_h:.6g}",
        f".param CL={params.cl_f:.6g}",
        f".param ITAIL={params.itail_a:.6g}",
        f".param MOS_W={params.mos_w_um:.6g}",
        f".param MOS_L={params.mos_l_um:.6g}",
        f".param MOS_W_m={params.mos_w_um * 1e-6:.12g}",
        f".param MOS_L_m={params.mos_l_um * 1e-6:.12g}",
        f".param MOS_M={params.mos_m}",
        f".param MOS_VGS={params.mos_vgs:.6g}",
        f".param RPPD_W={params.rppd_w_um * 1e-6:.6g}",
        f".param RPPD_L={params.rppd_l_um * 1e-6:.6g}",
        f".param RPPD_W_m={params.rppd_w_um * 1e-6:.12g}",
        f".param RPPD_L_m={params.rppd_l_um * 1e-6:.12g}",
    ]
    path.write_text("\n".join(lines) + "\n")


def print_summary(params: CtleParams) -> None:
    adc_est = params.gm * params.rd_ohm / (1.0 + params.gm * params.rs_ohm / 2.0)
    peak_est = 20.0 * math.log10(1.0 + params.gm * params.rs_ohm / 2.0)
    mfd = params.l_h / (params.rd_ohm ** 2 * params.cl_f)
    vov = params.mos_vgs - MOS_VTH_V
    print("=== CTLE sizing (ideal pass) ===")
    print(f"  Nx={params.nx}  VBE={params.vbe:.4f} V  Ic={params.ic:.4e} A")
    print(f"  ft={params.ft_hz:.3e} Hz  Cin={params.cin_f:.3e} F  gm={params.gm:.4e} S")
    print(f"  RD={params.rd_ohm:.2f} Ω  Rs={params.rs_ohm:.2f} Ω  Cs={params.cs_f:.3e} F")
    print(f"  fz={F_Z_HZ/1e9:.1f} GHz  L={params.l_h:.3e} H  CL={params.cl_f:.3e} F  m={mfd:.3f}")
    print(f"  VDD={params.vdd:.3f} V  VBASE={params.vbase:.3f} V  I_tail={params.itail_a:.4e} A")
    print(
        f"  MOS tail W={params.mos_w_um:.2f} µm L={params.mos_l_um} µm "
        f"VGS_lut={params.mos_vgs:.3f} V  Vov_est={vov:.3f} V"
    )
    print(f"  RPPD W={params.rppd_w_um:.3f} L={params.rppd_l_um} µm")
    print(f"  Est A_dc={20*math.log10(adc_est):.2f} dB  est deg boost={peak_est:.2f} dB")
    print(f"  scale={params.scale}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Size 56G CML CTLE from LUTs")
    parser.add_argument("--scale", type=float, default=1.0, help="RD/Rs scale factor")
    parser.add_argument("--peaking-db", type=float, default=7.0)
    parser.add_argument("--nx-idx", type=int, default=0)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "spice" / "params.inc",
    )
    parser.add_argument("--json", type=Path, help="Also write JSON summary")
    args = parser.parse_args()

    if not os.environ.get("PDK_ROOT"):
        print("Warning: PDK_ROOT not set — assume env.sh was sourced for SPICE runs.")

    params = size_ctle(nx_idx=args.nx_idx, scale=args.scale, peaking_db=args.peaking_db)
    write_params_inc(params, args.out)
    print_summary(params)
    if args.json:
        args.json.write_text(json.dumps(params.__dict__, indent=2))


if __name__ == "__main__":
    main()
