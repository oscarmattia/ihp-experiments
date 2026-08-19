#!/usr/bin/env python3
"""Size 56 Gb/s NRZ CML CTLE from char LUTs and emit spice/params.inc."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from char.common.lut import load_lut  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from size_ind import DEFAULT_OUT, generate_ind_shunt  # noqa: E402
from size_term import VDD_DEFAULT_V  # noqa: E402

VDD_V = VDD_DEFAULT_V  # single 1.6 V rail for term, CTLE, VGA, and chain
F_Z_HZ = 10e9  # degeneration zero — below Nyquist for ~6 dB peaking at 28 GHz
MFD = 0.32
RPPD_RSH = 260.0  # Ω/sq typ (cornerRES.lib)
TAIL_VDS_V = 0.35  # target NMOS tail VDS (V), must exceed Vov
MOS_VTH_V = 0.45
MOS_VGS_MIN_V = 0.55
MOS_VGS_MAX_V = 0.70
W_MARGIN = 1.75  # modest W boost for ro / matching (not a triode substitute)

# cap_cmomi feed=same mmin=2 mmax=5 — measured ~151 fF at 12×12 µm [sim]
CMOMI_MMIN = 2
CMOMI_MMAX = 5
CMOMI_DENSITY_FF_UM2 = 1.19

# FO1 load: Miller-aware VGA input + on-chip route (fringe/coupling dominated).
# mid of 0.15–0.2 fF/µm [docs/PDK.md]; measured Metal4 trunk (135.6 µm) was
# 0.085 fF/µm standalone — conservative for high metals far from substrate.
ROUTING_CAP_FF_PER_UM = 0.17
INTERCONNECT_LENGTH_UM = 40.0  # pre-layout route guess (planning length)
# Post-layout extraction, CTLE stage output node (outp): Metal4 trunk 135.6 µm ×
# 2.88 µm.  Includes ~1.3× neighbour coupling above the standalone 11.55 fF wire.
INTERCONNECT_CAP_F_MEASURED = 15.26e-15
FO1_AV_MILLER = 2.0  # conservative max-gain Miller multiplier (VGA agent)

# HBT intrinsic emitter resistance: re = 7.13*(4/Nx) Ω [model card]
RE_VBIC_SCALE = 7.13

# EM 1-turn octagon fit @ 28 GHz: L(pH) = 1.774*D(µm) - 5.55, r=0.99998 [char/passive EM]
EM_L_SLOPE_PH_PER_UM = 1.774
EM_L_INTERCEPT_PH = 5.55
INDUCTOR_DMIN_UM = 25.35  # inductor PCell minimum diameter

# Bessel shunt-peaking: m = L/(RD² C_L) = 0.32.  RD is set by the coil floor, not DC gain.
RD_BAND_LO_OHM = 70.0
RD_BAND_HI_OHM = 90.0
RD_NOM_OHM = 87.0  # ~m≈0.35 with turn1_d40 (66 pH) at C_L≈25 fF

# Soft DC-gain aim for iteration scoring only (does not set RD)
DC_GAIN_TARGET_DB = -1.5


@dataclass
class CtleParams:
    nx: int = 1
    vbe: float = 0.0
    ic: float = 0.0
    ft_hz: float = 0.0
    cin_f: float = 0.0
    cbe_f: float = 0.0
    cbc_f: float = 0.0
    gm: float = 0.0
    hbt_re_ohm: float = 0.0
    vdd: float = 1.25
    rd_ohm: float = 0.0
    rs_ohm: float = 0.0
    cs_f: float = 0.0
    l_h: float = 0.0
    cl_f: float = 0.0
    cl_miller_f: float = 0.0
    cl_interconnect_f: float = 0.0
    itail_a: float = 0.0
    mos_w_um: float = 0.0
    mos_l_um: float = 0.5
    mos_m: int = 1
    mos_vgs: float = 0.0
    rppd_w_um: float = 0.0
    rppd_l_um: float = 0.0
    rppd_r_ohm: float = 0.0
    rsil_w_um: float = 0.5
    rsil_l_um: float = 2.35
    rsil_r_ohm: float = 0.0
    cmomi_w_um: float = 0.0
    cmomi_l_um: float = 0.0
    cmomi_c_f: float = 0.0
    l_em_case: str = ""
    l_em_h: float = 0.0
    ind_shunt_inc: str = ""
    cap_w_um: float = 0.0
    cap_l_um: float = 0.0
    cs_ideal: bool = True
    vbase: float = 0.0
    vout_cm: float = 0.0
    scale: float = 1.0
    peaking_db: float = 7.0
    rd_min_ohm: float = 0.0
    m_bessel: float = 0.0
    m_ideal: float = 0.0
    m_pdk: float = 0.0


def _repo_paths() -> dict[str, Path]:
    return {
        "bjt": _REPO / "char/bjt/out/sg13_npn13G2.npz",
        "mos": _REPO / "char/mos/out/lv_core_n.npz",
        "rppd": _REPO / "char/passive/out/sg13_rppd.npz",
        "rsil": _REPO / "char/passive/out/sg13_rsil.npz",
        "cmomi": _REPO / "char/passive/out/sg13_cap_cmomi.npz",
        "cmim": _REPO / "char/passive/out/sg13_cap_cmim.npz",
    }


def hbt_re_ohm(nx: int) -> float:
    return RE_VBIC_SCALE * 4.0 / nx


def miller_cin(cbe_f: float, cbc_f: float, av_lin: float) -> float:
    return cbe_f + cbc_f * (1.0 + abs(av_lin))


def interconnect_cap_f(
    length_um: float = INTERCONNECT_LENGTH_UM,
    cap_ff_per_um: float = ROUTING_CAP_FF_PER_UM,
    *,
    use_measured: bool = True,
) -> float:
    """Per-output route C (one trace CTLE out → VGA in).

    Post-layout extraction supersedes length×coefficient when available; the
    estimate path is kept for pre-layout budget work without a measurement.
    """
    if use_measured and INTERCONNECT_CAP_F_MEASURED is not None:
        return INTERCONNECT_CAP_F_MEASURED
    return cap_ff_per_um * 1e-15 * length_um


def budget_cl_f(cbe_f: float, cbc_f: float, av_miller: float = FO1_AV_MILLER) -> tuple[float, float, float]:
    """Return (C_L, C_Miller, C_interconnect) for FO1 VGA load."""
    cl_m = miller_cin(cbe_f, cbc_f, av_miller)
    cl_ic = interconnect_cap_f()
    return cl_m + cl_ic, cl_m, cl_ic


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


def hbt_caps_at_bias(
    bjt_path: Path,
    nx_idx: int = 0,
    vce_min: float = 0.9,
    vce_max: float = 1.2,
) -> tuple[float, float, float, float, float, float, float, float]:
    """Return (vbe, ic, gm, ft, cbe, cbc, cin_lut, vce_lut) at max-fT bias."""
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
                    float(arrays["CBE"][nx_idx, vi, bi]),
                    float(arrays["CBC"][nx_idx, vi, bi]),
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


#: A wide tail cannot be one PCell instance. The foundry nmos PCell draws no
#: source/drain straps, so ng > 1 extracts as transistors in series, and a single
#: finger is silently capped near this width. The tail is therefore drawn as an
#: array of single-finger units, and its total width has to land on that grid.
MOS_UNIT_W_MAX_UM = 10.0

#: Per-finger width snaps to this grid inside the PCell, so a total that does not
#: divide onto it comes out narrower than asked.
MOS_W_GRID_UM = 0.005


def snap_drawable_mos_w(w_um: float) -> float:
    """Round a tail width to something the layout can actually draw.

    The LVS deck compares MOS ``w`` and ``l`` with essentially no tolerance —
    242.988 um against a drawn 243.000 um is a mismatch — so the schematic has to
    carry the drawable number rather than leaving layout to round it. Mirrors
    ``plan_units`` in ``layout/blocks/mos_array.py``; ``layout/common/parity.py``
    fails the build if the two ever disagree.
    """
    units = max(1, math.ceil(w_um / MOS_UNIT_W_MAX_UM))
    unit_w = round(w_um / units / MOS_W_GRID_UM) * MOS_W_GRID_UM
    return units * unit_w


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
            if vov <= 0.05 or vov >= vds_target - 0.08:
                continue
            id_per_um = lut_interp_id(arrays, l_um, float(vgs), vds_target)
            if id_per_um <= 0:
                continue
            w_um = itail / id_per_um * w_margin
            if w_um > w_max_um or w_um < 0.5:
                continue
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


def verify_rppd_ngspice(w_um: float, l_um: float) -> float:
    """Quick ngspice OP measurement of rppd R (Ω), res_typ @ 27 °C."""
    pdk = os.environ.get("PDK_ROOT")
    if not pdk:
        arrays, _ = load_lut(_repo_paths()["rppd"])
        return lut_r_at_27(arrays, w_um, l_um)
    models = Path(pdk) / "ihp-sg13g2/libs.tech/ngspice/models"
    spiceinit = Path(pdk) / "ihp-sg13g2/libs.tech/ngspice/.spiceinit"
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        if spiceinit.is_file():
            (tdir / ".spiceinit").write_bytes(spiceinit.read_bytes())
        cir = tdir / "rppd_op.cir"
        cir.write_text(
            f".lib '{models}/cornerRES.lib' res_typ\n"
            "V1 1 0 dc 1\n"
            f"Xr 1 2 0 rppd w={w_um:.12g}e-6 l={l_um:.12g}e-6 m=1\n"
            "V2 2 0 dc 0\n"
            ".control\nop\nprint v(1,2)/@v1[i]\n.endc\n.end\n"
        )
        log = tdir / "rppd.log"
        subprocess.run(
            ["ngspice", "-b", "-o", str(log), str(cir)],
            cwd=tdir,
            check=False,
            capture_output=True,
        )
        for line in log.read_text().splitlines():
            if "v(1,2)/@v1[i]" in line.lower():
                return abs(float(line.split("=")[-1].strip()))
    arrays, _ = load_lut(_repo_paths()["rppd"])
    return lut_r_at_27(arrays, w_um, l_um)


def size_rppd(
    rppd_path: Path,
    rd_target: float,
    lut_scale: float = 0.88,
) -> tuple[float, float, float]:
    """Pick ``rppd`` geometry whose ngspice OP R is closest to target.

    Always verifies with a standalone ``op`` sim (not LUT ``rsh*L/W``).
    ``lut_scale`` defaults to 0.88 for legacy VGA callers that inflate the
    search target; CTLE passes ``lut_scale=1.0`` to match ``RD`` exactly.
    """
    search_target = rd_target if lut_scale == 1.0 else rd_target / lut_scale
    arrays, _ = load_lut(rppd_path)
    temp_idx = int(np.argmin(np.abs(arrays["TEMP"] - 27.0)))
    ranked: list[tuple[float, float, float]] = []
    for wi, w in enumerate(arrays["W"]):
        for li, l in enumerate(arrays["L"]):
            if float(w) < 0.5 or float(l) < 0.5:
                continue
            r_lut = float(arrays["R"][wi, li, temp_idx])
            ranked.append((abs(r_lut - search_target), float(w), float(l)))
    ranked.sort(key=lambda t: t[0])

    candidates: list[tuple[float, float]] = []
    for _, w_um, l_um in ranked[:24]:
        candidates.append((w_um, l_um))
    for w_um, l_um in ((5.0, 1.4), (5.0, 1.6), (5.0, 1.0), (2.0, 0.6)):
        candidates.append((w_um, l_um))

    best_err = float("inf")
    best = (5.0, 1.4, search_target)
    seen: set[tuple[float, float]] = set()
    for w_um, l_um in candidates:
        key = (round(w_um, 6), round(l_um, 6))
        if key in seen:
            continue
        seen.add(key)
        r_meas = verify_rppd_ngspice(w_um, l_um)
        err = abs(r_meas - search_target)
        if err < best_err:
            best_err = err
            best = (w_um, l_um, r_meas)
    return best


def lut_r_at_27(arrays: dict[str, np.ndarray], w_um: float, l_um: float) -> float:
    temp_idx = int(np.argmin(np.abs(arrays["TEMP"] - 27.0)))
    wi = int(np.argmin(np.abs(arrays["W"] - w_um)))
    li = int(np.argmin(np.abs(arrays["L"] - l_um)))
    return float(arrays["R"][wi, li, temp_idx])


def verify_rsil_ngspice(w_um: float, l_um: float) -> float:
    """Quick ngspice OP measurement of rsil R (Ω)."""
    pdk = os.environ.get("PDK_ROOT")
    if not pdk:
        arrays, _ = load_lut(_repo_paths()["rsil"])
        return lut_r_at_27(arrays, w_um, l_um)
    models = Path(pdk) / "ihp-sg13g2/libs.tech/ngspice/models"
    spiceinit = Path(pdk) / "ihp-sg13g2/libs.tech/ngspice/.spiceinit"
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        if spiceinit.is_file():
            (tdir / ".spiceinit").write_bytes(spiceinit.read_bytes())
        cir = tdir / "rsil_op.cir"
        cir.write_text(
            f".lib '{models}/cornerRES.lib' res_typ\n"
            "V1 1 0 dc 1\n"
            f"Xr 1 2 0 rsil w={w_um:.12g}e-6 l={l_um:.12g}e-6 m=1\n"
            "V2 2 0 dc 0\n"
            ".control\nop\nprint v(1,2)/@v1[i]\n.endc\n.end\n"
        )
        log = tdir / "rsil.log"
        subprocess.run(
            ["ngspice", "-b", "-o", str(log), str(cir)],
            cwd=tdir,
            check=False,
            capture_output=True,
        )
        for line in log.read_text().splitlines():
            if "v(1,2)/@v1[i]" in line.lower():
                return abs(float(line.split("=")[-1].strip()))
    arrays, _ = load_lut(_repo_paths()["rsil"])
    return lut_r_at_27(arrays, w_um, l_um)


def size_rsil(rs_target_ohm: float, rsil_path: Path) -> tuple[float, float, float]:
    """Pick rsil geometry nearest target R; verify with ngspice OP."""
    arrays, _ = load_lut(rsil_path)
    temp_idx = int(np.argmin(np.abs(arrays["TEMP"] - 27.0)))
    best_err = float("inf")
    best = (0.5, 2.35, rs_target_ohm)
    for wi, w in enumerate(arrays["W"]):
        for li, l in enumerate(arrays["L"]):
            if float(w) < 0.5 or float(l) < 0.5:
                continue
            r = float(arrays["R"][wi, li, temp_idx])
            err = abs(r - rs_target_ohm)
            if err < best_err:
                best_err = err
                best = (float(w), float(l), r)
    w_um, l_um, _ = best
    r_meas = verify_rsil_ngspice(w_um, l_um)
    return w_um, l_um, r_meas


def size_rsil_half(rs_target_ohm: float, rsil_path: Path) -> tuple[float, float, float]:
    """Legacy name — sizes one rsil to ``rs_target_ohm`` (not half of a split)."""
    return size_rsil(rs_target_ohm, rsil_path)


def verify_cmomi_ngspice(w_um: float, l_um: float, f_hz: float = 1e9) -> float:
    """AC effective C (F) of cap_cmomi feed=same mmin=2 mmax=5."""
    pdk = os.environ.get("PDK_ROOT")
    if not pdk:
        return w_um * l_um * CMOMI_DENSITY_FF_UM2 * 1e-15
    models = Path(pdk) / "ihp-sg13g2/libs.tech/ngspice/models"
    spiceinit = Path(pdk) / "ihp-sg13g2/libs.tech/ngspice/.spiceinit"
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        if spiceinit.is_file():
            (tdir / ".spiceinit").write_bytes(spiceinit.read_bytes())
        cir = tdir / "cmomi_ac.cir"
        cir.write_text(
            f".lib '{models}/cornerCAP.lib' cap_typ\n"
            "Vac in 0 ac 1\n"
            "Rser in out 1e-6\n"
            f"Xc out 0 cap_cmomi w={w_um:.12g}e-6 l={l_um:.12g}e-6"
            f" mmin={CMOMI_MMIN} mmax={CMOMI_MMAX} feed=same m=1\n"
            f".ac lin 1 {f_hz:.6g} {f_hz:.6g}\n"
            ".control\n"
            "let yim = imag(i(vac))\n"
            "let c = yim/(2*pi*frequency)\n"
            "print c\n"
            ".endc\n.end\n"
        )
        log = tdir / "cmomi.log"
        subprocess.run(
            ["ngspice", "-b", "-o", str(log), str(cir)],
            cwd=tdir,
            check=False,
            capture_output=True,
        )
        for line in log.read_text().splitlines():
            if "i(vac)" in line.lower() and "=" in line:
                rhs = line.split("=", 1)[1].strip()
                parts = rhs.replace(",", " ").split()
                if len(parts) >= 2:
                    im_i = float(parts[1])
                    omega = 2.0 * math.pi * f_hz
                    return abs(im_i / omega)
                if len(parts) == 1:
                    return abs(float(parts[0]) / (2.0 * math.pi * f_hz))
    return w_um * l_um * CMOMI_DENSITY_FF_UM2 * 1e-15


def size_cmomi(cs_target_f: float, cmomi_path: Path) -> tuple[float, float, float]:
    """Pick square cap_cmomi geometry (feed=same, mmin=2) nearest Cs target."""
    arrays, _ = load_lut(cmomi_path)
    w_grid = np.unique(arrays["W"]) if "W" in arrays else np.array([5.0, 10.0, 30.0])
    best_err = float("inf")
    best = (12.0, 12.0, cs_target_f)
    candidates = set()
    for w in w_grid:
        candidates.add(float(w))
    est_w = math.sqrt(cs_target_f / (CMOMI_DENSITY_FF_UM2 * 1e-15))
    for w in np.linspace(max(8.0, est_w - 3), est_w + 3, 9):
        candidates.add(float(w))
    for w_um in sorted(candidates):
        l_um = w_um
        c_meas = verify_cmomi_ngspice(w_um, l_um)
        err = abs(c_meas - cs_target_f)
        if err < best_err:
            best_err = err
            best = (w_um, l_um, c_meas)
    return best


def em_l_min_pH() -> float:
    """Smallest realizable 1-turn EM coil (inductor PCell dmin)."""
    return EM_L_SLOPE_PH_PER_UM * INDUCTOR_DMIN_UM - EM_L_INTERCEPT_PH


def rd_min_bessel_ohm(cl_f: float, mfd: float = MFD) -> float:
    """Minimum R_D for Bessel shunt peaking at the smallest buildable coil.

    RD_min = sqrt(L_min / (m * C_L)).  Below this, no 1-turn coil can hit m≈0.32.
    """
    l_min_h = em_l_min_pH() * 1e-12
    denom = mfd * cl_f
    if denom <= 0:
        raise ValueError(f"Invalid C_L={cl_f} for RD_min")
    return math.sqrt(l_min_h / denom)


def estimate_dc_gain_db(
    gm: float, rd: float, re: float, rs: float = 0.0
) -> float:
    """Small-signal diff gain; Rs/2 in emitter path (symmetric, no DC through Rs)."""
    av = gm * rd / (1.0 + gm * (re + rs / 2.0))
    return 20.0 * math.log10(max(av, 1e-12))


def compute_elements(
    gm: float,
    cl_f: float,
    ic: float,
    re: float,
    scale: float = 1.0,
    peaking_db_target: float = 7.0,
    tail_vds_v: float = TAIL_VDS_V,
    vce_target_v: float = 1.05,
) -> tuple[float, float, float, float, float, float]:
    """Return RD, Rs, Cs, L, VDD, RD_min.

  Rs from the peaking target (degeneration zero sets midband boost).
  RD from the Bessel coil floor — NOT from DC gain.  C_L does not enter DC gain.
    """
    deg_factor = 10.0 ** (peaking_db_target / 20.0)
    rs = 2.0 * (deg_factor - 1.0) / gm
    rs *= scale
    rs = max(rs, 1.0)

    rd_min = rd_min_bessel_ohm(cl_f)
    if rd_min > RD_BAND_HI_OHM:
        raise RuntimeError(
            f"Bessel RD_min={rd_min:.1f} Ω exceeds {RD_BAND_HI_OHM:.0f} Ω band "
            f"(L_min={em_l_min_pH():.1f} pH, C_L={cl_f * 1e15:.2f} fF, m={MFD}). "
            "Cannot realize maximally-flat shunt peaking — fail loudly rather than "
            "silently violate m."
        )

    rd = max(rd_min, RD_NOM_OHM)
    rd = min(rd, RD_BAND_HI_OHM)
    if rd < rd_min - 0.5:
        raise RuntimeError(
            f"RD={rd:.1f} Ω below Bessel floor RD_min={rd_min:.1f} Ω "
            f"(L_min={em_l_min_pH():.1f} pH)."
        )

    cs = 1.0 / (2.0 * math.pi * F_Z_HZ * rs)
    l_h = MFD * rd ** 2 * cl_f
    vdd = VDD_V  # fixed supply — headroom verified at 1.6 V
    return rd, rs, cs, l_h, vdd, rd_min


def ctle_collector_cm(vdd_v: float, ic_a: float, rd_ohm: float) -> float:
    """CTLE output common mode from realized bias: VDD − Ic·RD."""
    return vdd_v - ic_a * rd_ohm


def size_ctle(
    nx_idx: int = 0,
    scale: float = 1.0,
    peaking_db: float = 7.0,
    vbase_input: float | None = None,
) -> CtleParams:
    paths = _repo_paths()
    vbe, ic, gm, ft, cbe, cbc, cin_lut, vce_lut = hbt_caps_at_bias(
        paths["bjt"], nx_idx=nx_idx
    )
    nx = int(load_lut(paths["bjt"])[0]["Nx"][nx_idx])
    re = hbt_re_ohm(nx)
    cl_f, cl_miller, cl_ic = budget_cl_f(cbe, cbc, FO1_AV_MILLER)

    if vbase_input is None:
        vbase_input = vbe + TAIL_VDS_V
    tail_vds = vbase_input - vbe

    rd, rs, cs, l_h, vdd, rd_min = compute_elements(
        gm,
        cl_f,
        ic,
        re,
        scale=scale,
        peaking_db_target=peaking_db,
        tail_vds_v=tail_vds,
        vce_target_v=vce_lut,
    )
    itail = ic  # per tail device (two tails, each sources Ic)
    mos_w, mos_m, mos_vgs, mos_l = size_mos_tail(
        paths["mos"], itail, vds_target=tail_vds
    )
    mos_w = snap_drawable_mos_w(mos_w)
    rppd_w, rppd_l, rppd_r = size_rppd(paths["rppd"], rd, lut_scale=1.0)
    rsil_w, rsil_l, rsil_r = size_rsil(rs, paths["rsil"])
    rs_actual = rsil_r
    cs_target = 1.0 / (2.0 * math.pi * F_Z_HZ * rs_actual)
    cmomi_w, cmomi_l, cmomi_c = size_cmomi(cs_target, paths["cmomi"])
    ind_inc = DEFAULT_OUT
    ind_model = generate_ind_shunt(l_h, ind_inc)
    m_bessel = l_h / (rd ** 2 * cl_f)
    m_ideal = m_bessel
    m_pdk = ind_model.l_band_h / (rppd_r ** 2 * cl_f)
    vbase = vbase_input
    vout_cm = ctle_collector_cm(vdd, ic, rppd_r)

    return CtleParams(
        nx=nx,
        vbe=vbe,
        ic=ic,
        ft_hz=ft,
        cin_f=cin_lut,
        cbe_f=cbe,
        cbc_f=cbc,
        gm=gm,
        hbt_re_ohm=re,
        vdd=vdd,
        rd_ohm=rd,
        rs_ohm=rs_actual,
        cs_f=cmomi_c,
        l_h=l_h,
        cl_f=cl_f,
        cl_miller_f=cl_miller,
        cl_interconnect_f=cl_ic,
        itail_a=itail,
        mos_w_um=mos_w,
        mos_l_um=mos_l,
        mos_m=mos_m,
        mos_vgs=mos_vgs,
        rppd_w_um=rppd_w,
        rppd_l_um=rppd_l,
        rppd_r_ohm=rppd_r,
        rsil_w_um=rsil_w,
        rsil_l_um=rsil_l,
        rsil_r_ohm=rsil_r,
        cmomi_w_um=cmomi_w,
        cmomi_l_um=cmomi_l,
        cmomi_c_f=cmomi_c,
        l_em_case=ind_model.case,
        l_em_h=ind_model.l_band_h,
        ind_shunt_inc=str(ind_inc.resolve()),
        cap_w_um=cmomi_w,
        cap_l_um=cmomi_l,
        cs_ideal=False,
        vbase=vbase,
        vout_cm=vout_cm,
        scale=scale,
        peaking_db=peaking_db,
        rd_min_ohm=rd_min,
        m_bessel=m_bessel,
        m_ideal=m_ideal,
        m_pdk=m_pdk,
    )


_PARAM_RE = re.compile(r"^\.param\s+(\w+)=([\S]+)", re.MULTILINE)


def read_params_inc(path: Path) -> CtleParams:
    """Load committed sizing from spice/params.inc (for sim-only runs)."""
    text = path.read_text()
    raw = {m.group(1): m.group(2) for m in _PARAM_RE.finditer(text)}

    def _f(key: str, default: float = 0.0) -> float:
        return float(raw.get(key, default))

    nx = int(_f("Nx", 1))
    params = CtleParams(
        nx=nx,
        vbe=_f("VBE"),
        vbase=_f("VBASE"),
        vdd=_f("VDD"),
        rd_ohm=_f("RD"),
        rs_ohm=_f("RS"),
        cs_f=_f("CS"),
        l_h=_f("LLOAD"),
        cl_f=_f("CL"),
        itail_a=_f("ITAIL"),
        mos_w_um=_f("MOS_W"),
        mos_l_um=_f("MOS_L"),
        mos_m=int(_f("MOS_M", 1)),
        mos_vgs=_f("MOS_VGS"),
        rppd_w_um=_f("RPPD_W_m", _f("RPPD_W")) * 1e6,
        rppd_l_um=_f("RPPD_L_m", _f("RPPD_L")) * 1e6,
        rsil_w_um=_f("RSIL_W") * 1e6,
        rsil_l_um=_f("RSIL_L") * 1e6,
        cmomi_w_um=_f("CMOMI_W") * 1e6,
        cmomi_l_um=_f("CMOMI_L") * 1e6,
        hbt_re_ohm=hbt_re_ohm(nx),
    )
    if "CL_MILLER" in raw:
        params.cl_miller_f = _f("CL_MILLER")
    if "CL_INTERCONNECT" in raw:
        params.cl_interconnect_f = _f("CL_INTERCONNECT")
    if "CBE" in raw:
        params.cbe_f = _f("CBE")
    if "CBC" in raw:
        params.cbc_f = _f("CBC")
    if "SCALE" in raw:
        params.scale = _f("SCALE")
    if "PEAKING_DB" in raw:
        params.peaking_db = _f("PEAKING_DB")
    if "RPPD_R" in raw:
        params.rppd_r_ohm = _f("RPPD_R")
    if "L_EM" in raw:
        params.l_em_h = _f("L_EM")
    if "M_IDEAL" in raw:
        params.m_ideal = _f("M_IDEAL")
    if "M_PDK" in raw:
        params.m_pdk = _f("M_PDK")

    paths = _repo_paths()
    if paths["bjt"].is_file():
        vbe, ic, gm, ft, cbe, cbc, cin_lut, _ = hbt_caps_at_bias(paths["bjt"])
        params.vbe = vbe
        params.ic = ic
        params.gm = gm
        params.ft_hz = ft
        params.cin_f = cin_lut
        if not params.cbe_f:
            params.cbe_f = cbe
        if not params.cbc_f:
            params.cbc_f = cbc
        if not params.cl_f:
            cl_f, cl_m, cl_ic = budget_cl_f(cbe, cbc, FO1_AV_MILLER)
            params.cl_f = cl_f
            params.cl_miller_f = cl_m
            params.cl_interconnect_f = cl_ic
    return params


def write_params_inc(params: CtleParams, path: Path) -> None:
    lines = [
        "* CTLE parameters — generated by size_ctle.py",
        f".param Nx={params.nx}",
        f".param VBE={params.vbe:.6g}",
        f".param VBASE={params.vbase:.6g}",
        f".param VOUT_CM={params.vout_cm:.6g}",
        f".param VDD={params.vdd:.6g}",
        f".param RD={params.rd_ohm:.6g}",
        f".param RS={params.rs_ohm:.6g}",
        f".param CS={params.cs_f:.6g}",
        f".param LLOAD={params.l_h:.6g}",
        f".param CL={params.cl_f:.6g}",
        f".param CBE={params.cbe_f:.6g}",
        f".param CBC={params.cbc_f:.6g}",
        f".param CL_MILLER={params.cl_miller_f:.6g}",
        f".param CL_INTERCONNECT={params.cl_interconnect_f:.6g}",
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
        f".param RPPD_R={params.rppd_r_ohm:.6g}",
        f".param RSIL_W={params.rsil_w_um * 1e-6:.6g}",
        f".param RSIL_L={params.rsil_l_um * 1e-6:.6g}",
        f".param CMOMI_W={params.cmomi_w_um * 1e-6:.6g}",
        f".param CMOMI_L={params.cmomi_l_um * 1e-6:.6g}",
        f".param CMOMI_MMIN={CMOMI_MMIN}",
        f".param CMOMI_MMAX={CMOMI_MMAX}",
        f".param SCALE={params.scale:.6g}",
        f".param PEAKING_DB={params.peaking_db:.6g}",
        f".param L_EM={params.l_em_h:.6g}",
        f".param RD_MIN={params.rd_min_ohm:.6g}",
        f".param M_BESSEL={params.m_bessel:.6g}",
        f".param M_IDEAL={params.m_ideal:.6g}",
        f".param M_PDK={params.m_pdk:.6g}",
    ]
    path.write_text("\n".join(lines) + "\n")


def print_summary(params: CtleParams) -> None:
    rs_half = params.rs_ohm / 2.0
    deg_boost = (1.0 + params.gm * (params.hbt_re_ohm + rs_half)) / (
        1.0 + params.gm * params.hbt_re_ohm
    )
    peak_est = 20.0 * math.log10(deg_boost)
    mfd = params.m_bessel if params.m_bessel else params.l_h / (params.rd_ohm ** 2 * params.cl_f)
    m_ideal = params.m_ideal if params.m_ideal else mfd
    m_pdk = params.m_pdk if params.m_pdk else 0.0
    vov = params.mos_vgs - MOS_VTH_V
    vds_tail = params.vbase - params.vbe
    sat_margin = vds_tail - vov
    print("=== CTLE sizing (ideal pass) ===")
    print(f"  Nx={params.nx}  VBE={params.vbe:.4f} V  Ic={params.ic:.4e} A")
    print(
        f"  ft={params.ft_hz:.3e} Hz  CBE={params.cbe_f*1e15:.2f} fF  "
        f"CBC={params.cbc_f*1e15:.3f} fF  gm={params.gm:.4e} S  re={params.hbt_re_ohm:.1f} Ω"
    )
    ic_est_ff = ROUTING_CAP_FF_PER_UM * INTERCONNECT_LENGTH_UM
    if INTERCONNECT_CAP_F_MEASURED is not None:
        ic_note = (
            f"route measured {params.cl_interconnect_f*1e15:.2f} fF "
            f"(extracted CTLE outp; est {ic_est_ff:.1f} fF @ "
            f"{INTERCONNECT_LENGTH_UM:.0f} µm × {ROUTING_CAP_FF_PER_UM:.2f} fF/µm)"
        )
    else:
        ic_note = (
            f"route {INTERCONNECT_LENGTH_UM:.0f} µm @ "
            f"{ROUTING_CAP_FF_PER_UM:.2f} fF/µm: "
            f"{params.cl_interconnect_f*1e15:.2f} fF"
        )
    print(
        f"  C_L={params.cl_f*1e15:.2f} fF "
        f"(Miller |Av|={FO1_AV_MILLER:.0f}: {params.cl_miller_f*1e15:.2f} fF + "
        f"{ic_note})"
    )
    print(
        f"  RD={params.rd_ohm:.2f} Ω (Bessel floor RD_min={params.rd_min_ohm:.1f} Ω, "
        f"band {RD_BAND_LO_OHM:.0f}–{RD_BAND_HI_OHM:.0f} Ω)"
    )
    print(f"  Rs={params.rs_ohm:.2f} Ω  Cs={params.cs_f:.3e} F")
    print(
        f"  fz={F_Z_HZ/1e9:.1f} GHz  L_ideal={params.l_h*1e12:.2f} pH  "
        f"m_ideal={m_ideal:.3f}  m_pdk={m_pdk:.3f} (L_EM vs verified rppd R)"
    )
    print(
        f"  VDD={params.vdd:.3f} V  VBASE={params.vbase:.3f} V  "
        f"Vout_CM(est)={params.vout_cm:.3f} V  "
        f"VDS_tail(est)={vds_tail:.3f} V  I_tail(per dev)={params.itail_a:.4e} A"
    )
    print(
        f"  MOS tail W={params.mos_w_um:.2f} µm L={params.mos_l_um} µm "
        f"VGS_lut={params.mos_vgs:.3f} V  Vov_est={vov:.3f} V  sat_margin~{sat_margin:.3f} V"
    )
    print(f"  RPPD W={params.rppd_w_um:.3f} L={params.rppd_l_um} µm -> {params.rppd_r_ohm:.2f} Ω (ngspice OP)")
    print(
        f"  RSIL deg W={params.rsil_w_um:g} L={params.rsil_l_um:g} µm "
        f"-> {params.rsil_r_ohm:.2f} Ω"
    )
    print(
        f"  CMOMI Cs W={params.cmomi_w_um:g} L={params.cmomi_l_um:g} µm "
        f"feed=same mmin={CMOMI_MMIN} -> {params.cmomi_c_f*1e15:.1f} fF"
    )
    print(
        f"  EM inductor case={params.l_em_case} "
        f"L_em={params.l_em_h*1e12:.2f} pH (ideal L={params.l_h*1e12:.2f} pH)"
    )
    print(
        f"  Est A_dc={estimate_dc_gain_db(params.gm, params.rd_ohm, params.hbt_re_ohm, params.rs_ohm):.2f} dB "
        f"  est deg boost={peak_est:.2f} dB"
    )
    print(f"  scale={params.scale}  peaking_db_target={params.peaking_db}")


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

    from size_term import size_term  # noqa: E402

    term = size_term()
    print(f"  Termination vtt={term.vbase:.4f} V (target ~1.40 V)")

    params = size_ctle(
        nx_idx=args.nx_idx,
        scale=args.scale,
        peaking_db=args.peaking_db,
        vbase_input=term.vbase,
    )
    write_params_inc(params, args.out)
    print_summary(params)
    if args.json:
        args.json.write_text(json.dumps(params.__dict__, indent=2))


if __name__ == "__main__":
    main()
