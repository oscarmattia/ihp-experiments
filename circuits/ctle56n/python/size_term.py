#!/usr/bin/env python3
"""Size 56G RX termination stage (50 Ohm + vtt divider) from char LUTs."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from char.common.lut import load_lut  # noqa: E402

# Magic ihp-sg13g2-extract.tech defaultareacap to substrate (aF/um^2)
CAP_M6_AF_UM2 = 5.649  # TopMetal1 (metal6) -> substrate; bottom plate of stacked pad
ESD_C_FF_PER_PAD = 50.9  # docs/PDK.md [sim] one diodevdd + diodevss pair @ 1.4 V

VTT_TARGET_V = 1.4
VDD_DEFAULT_V = 1.6  # single rail for term, CTLE, VGA, and chain
IDIV_TARGET_A = 1.0e-3
RSIL_W_UM = 0.5
RSIL_L_UM = 2.35
Z0_SE_OHM = 50.0  # per-leg / single-ended
Z0_DIFF_OHM = 100.0  # differential link (50 ohm per leg)
RSRC_LEG_OHM = 50.0  # AC/tran source series R per leg
NYQUIST_HZ = 28e9


@dataclass
class TermParams:
    vdd: float = VDD_DEFAULT_V
    vbase: float = VTT_TARGET_V
    pad_w_um: float = 70.0
    pad_l_um: float = 70.0
    pad_c_f: float = 0.0
    esd_m: int = 1
    rsil_w_um: float = RSIL_W_UM
    rsil_l_um: float = RSIL_L_UM
    rsil_r_ohm: float = 50.0
    rout_ser_ohm: float = 0.0
    vtt_r_top_ohm: float = 0.0
    vtt_r_bot_ohm: float = 0.0
    vtt_r_top_w_um: float = 0.0
    vtt_r_top_l_um: float = 0.0
    vtt_r_bot_w_um: float = 0.0
    vtt_r_bot_l_um: float = 0.0
    vtt_i_a: float = 0.0
    vtt_cap_w_um: float = 7.0
    vtt_cap_l_um: float = 20.0
    vtt_cap_f: float = 0.0
    cl_tb_f: float = 10.3592e-15  # npn13G2 CIN FO1 default from CTLE sizing
    extra: dict[str, str] = field(default_factory=dict)


def _repo_paths() -> dict[str, Path]:
    return {
        "rsil": _REPO / "char/passive/out/sg13_rsil.npz",
        "rppd": _REPO / "char/passive/out/sg13_rppd.npz",
        "cmim": _REPO / "char/passive/out/sg13_cap_cmim.npz",
    }


def pad_capacitance_f(pad_w_um: float, pad_l_um: float) -> float:
    """Pad-to-substrate C from TM1 bottom plate only (stacked TM1+TM2 pad).

    This is the hand area formula for the termination stage only (~27.7 fF for
    70 µm). Magic C-only PEX of the same drawn ``bondpad_70um`` is ~80 fF
    isolated, ~102 fF with the ESD column tied, and ~144 fF in-situ on the pad
    driver — see ``layout/debug_pex/FINDINGS.md``. The pad driver uses the Magic
    in-situ metal value instead; do not use this function for driver ``PAD_C``.
    """
    area_um2 = pad_w_um * pad_l_um
    return area_um2 * CAP_M6_AF_UM2 * 1e-18


def shunt_cap_accounting_ff(pad_c_f: float, esd_c_ff: float = ESD_C_FF_PER_PAD) -> dict[str, float]:
    """Per-side and differential shunt capacitance bookkeeping."""
    pad_ff = pad_c_f * 1e15
    esd_ff = esd_c_ff
    per_side_ff = pad_ff + esd_ff
    diff_ff = per_side_ff / 2.0  # symmetric shunt to vtt: odd-mode C_diff = C_side/2
    return {
        "pad_per_side_fF": pad_ff,
        "esd_per_side_fF": esd_ff,
        "shunt_per_side_fF": per_side_ff,
        "shunt_diff_fF": diff_ff,
    }


def lut_r_at_27(arrays: dict[str, np.ndarray], w_um: float, l_um: float) -> float:
    temp_idx = int(np.argmin(np.abs(arrays["TEMP"] - 27.0)))
    wi = int(np.argmin(np.abs(arrays["W"] - w_um)))
    li = int(np.argmin(np.abs(arrays["L"] - l_um)))
    return float(arrays["R"][wi, li, temp_idx])


def pick_rppd_pair(
    rppd_path: Path,
    vdd: float,
    vtt: float,
    idiv: float,
) -> tuple[float, float, float, float, float, float, float]:
    """Pick (w_top, l_top, w_bot, l_bot, R_top, R_bot, I_div) closest to targets."""
    arrays, _ = load_lut(rppd_path)
    r_total = vdd / idiv
    r_bot_target = vtt / idiv
    r_top_target = r_total - r_bot_target

    best_err = float("inf")
    best: tuple[float, float, float, float, float, float] | None = None
    temp_idx = int(np.argmin(np.abs(arrays["TEMP"] - 27.0)))
    for wi, w in enumerate(arrays["W"]):
        for li, l in enumerate(arrays["L"]):
            if float(w) < 0.5 or float(l) < 0.5:
                continue
            r_bot = float(arrays["R"][wi, li, temp_idx])
            for wti, wt in enumerate(arrays["W"]):
                for lti, lt in enumerate(arrays["L"]):
                    if float(wt) < 0.5 or float(lt) < 0.5:
                        continue
                    r_top = float(arrays["R"][wti, lti, temp_idx])
                    vtt_est = vdd * r_bot / (r_top + r_bot)
                    idiv_est = vdd / (r_top + r_bot)
                    err = (
                        10.0 * abs(vtt_est - vtt) / vtt
                        + abs(idiv_est - idiv) / idiv
                        + 0.02 * abs(r_top - r_top_target) / r_top_target
                    )
                    if err < best_err:
                        best_err = err
                        best = (float(wt), float(lt), float(w), float(l), r_top, r_bot)
    if best is None:
        raise RuntimeError("Could not size vtt divider from rppd LUT")
    wt, lt, wb, lb, r_top, r_bot = best
    return wt, lt, wb, lb, r_top, r_bot, vdd / (r_top + r_bot)


def _cmim_c(arrays: dict[str, np.ndarray], idx: int) -> float:
    return float(arrays["C"][idx])


def pick_cmim(
    cmim_path: Path,
    c_target_f: float = 500e-15,
) -> tuple[float, float, float]:
    """Pick cap_cmim geometry with C >= c_target at low frequency."""
    arrays, _ = load_lut(cmim_path)
    n = len(arrays["C"])
    best: tuple[float, float, float, float] | None = None
    for idx in range(n):
        w = float(arrays["W"][idx] if len(arrays["W"]) == n else arrays["W"][min(idx, len(arrays["W"]) - 1)])
        l = float(arrays["L"][idx] if len(arrays["L"]) == n else arrays["L"][min(idx, len(arrays["L"]) - 1)])
        if "area_um2" in arrays:
            w = l = float(np.sqrt(arrays["area_um2"][idx]))
        c = _cmim_c(arrays, idx)
        if c < c_target_f:
            continue
        area = w * l
        if best is None or area < best[3]:
            best = (w, l, c, area)
    if best is None:
        idx = int(np.argmax(arrays["C"]))
        w = float(np.sqrt(arrays["area_um2"][idx]))
        return w, w, float(arrays["C"][idx])
    return best[0], best[1], best[2]


def measure_esd_leak_a(
    vdd: float = VDD_DEFAULT_V,
    vpad: float = VTT_TARGET_V,
) -> float:
    """Measure total DC pad-network leakage (A) for one ESD diode pair."""
    pdk = os.environ.get("PDK_ROOT")
    if not pdk:
        return float("nan")
    models = Path(pdk) / "ihp-sg13g2/libs.tech/ngspice/models"
    spiceinit = Path(pdk) / "ihp-sg13g2/libs.tech/ngspice/.spiceinit"
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        if spiceinit.is_file():
            (tdir / ".spiceinit").write_bytes(spiceinit.read_bytes())
        cir = tdir / "esd_leak.cir"
        cir.write_text(
            f".lib '{models}/cornerDIO.lib' dio_tt\n"
            f"Vdd vdd 0 dc {vdd:.6g}\n"
            f"Vpad pad 0 dc {vpad:.6g}\n"
            "Xesd_hi vdd pad 0 diodevdd_2kv m=1\n"
            "Xesd_lo vdd pad 0 diodevss_2kv m=1\n"
            ".control\n"
            "op\n"
            "print @vpad[i]\n"
            ".endc\n"
            ".end\n"
        )
        log = tdir / "esd_leak.log"
        subprocess.run(
            ["ngspice", "-b", "-o", str(log), str(cir)],
            cwd=tdir,
            check=False,
            capture_output=True,
        )
        for line in log.read_text().splitlines():
            if "@vpad[i]" in line.lower():
                return abs(float(line.split("=")[-1].strip()))
    return float("nan")


def verify_rsil_ngspice(w_um: float, l_um: float) -> float:
    """Quick ngspice op measurement of rsil R (returns ohm)."""
    pdk = os.environ.get("PDK_ROOT")
    if not pdk:
        return lut_r_at_27(load_lut(_repo_paths()["rsil"])[0], w_um, l_um)
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
        text = log.read_text()
        for line in text.splitlines():
            if "v(1,2)/@v1[i]" in line.lower():
                val = line.split("=")[-1].strip()
                return abs(float(val))
    return lut_r_at_27(load_lut(_repo_paths()["rsil"])[0], w_um, l_um)


DEFAULT_OUT = Path(__file__).resolve().parents[1] / "spice" / "term_params.inc"


def write_term_params_inc(params: TermParams, path: Path) -> None:
    """Emit spice/term_params.inc from the same tokens as to_extra()."""
    d = to_extra(params)
    lines = ["* Termination parameters — generated by size_term.py"]
    for k, v in d.items():
        lines.append(f".param {k}={v}")
    path.write_text("\n".join(lines) + "\n")


def to_extra(params: TermParams) -> dict[str, str]:
    """SPICE token replacements for prepare_tb / term_pdk.cir."""
    d = {
        "VDD": f"{params.vdd:.6g}",
        "VBASE": f"{params.vbase:.6g}",
        "PAD_W": f"{params.pad_w_um:.6g}",
        "PAD_L": f"{params.pad_l_um:.6g}",
        "PAD_C": f"{params.pad_c_f:.6g}",
        "ESD_M": str(params.esd_m),
        "RSIL_W": f"{params.rsil_w_um:.12g}e-6",
        "RSIL_L": f"{params.rsil_l_um:.12g}e-6",
        "ROUT_SER": f"{params.rout_ser_ohm:.6g}",
        "VTT_RTOP_W": f"{params.vtt_r_top_w_um:.12g}e-6",
        "VTT_RTOP_L": f"{params.vtt_r_top_l_um:.12g}e-6",
        "VTT_RBOT_W": f"{params.vtt_r_bot_w_um:.12g}e-6",
        "VTT_RBOT_L": f"{params.vtt_r_bot_l_um:.12g}e-6",
        "VTT_CAP_W": f"{params.vtt_cap_w_um:.12g}e-6",
        "VTT_CAP_L": f"{params.vtt_cap_l_um:.12g}e-6",
        "CL": f"{params.cl_tb_f:.6g}",
        "CL_TB": f"{params.cl_tb_f:.6g}",
        "RSRC_LEG": f"{RSRC_LEG_OHM:.6g}",
        "MOS_VGS": "0.6",
    }
    params.extra = d
    return d


def size_term(
    vdd: float = VDD_DEFAULT_V,
    vtt: float = VTT_TARGET_V,
    idiv: float = IDIV_TARGET_A,
    pad_w_um: float = 70.0,
    pad_l_um: float = 70.0,
    pad_c_override: float | None = None,
    cl_tb_f: float = 10.3592e-15,
    verify_rsil: bool = True,
) -> TermParams:
    paths = _repo_paths()
    pad_c = pad_capacitance_f(pad_w_um, pad_l_um) if pad_c_override is None else pad_c_override

    rsil_r = (
        verify_rsil_ngspice(RSIL_W_UM, RSIL_L_UM)
        if verify_rsil
        else lut_r_at_27(load_lut(paths["rsil"])[0], RSIL_W_UM, RSIL_L_UM)
    )

    wt, lt, wb, lb, r_top, r_bot, i_div = pick_rppd_pair(paths["rppd"], vdd, vtt, idiv)
    cw, cl, c_f = pick_cmim(paths["cmim"], c_target_f=500e-15)
    vtt_actual = vdd * r_bot / (r_top + r_bot)

    return TermParams(
        vdd=vdd,
        vbase=vtt_actual,
        pad_w_um=pad_w_um,
        pad_l_um=pad_l_um,
        pad_c_f=pad_c,
        rout_ser_ohm=1e-3,
        rsil_w_um=RSIL_W_UM,
        rsil_l_um=RSIL_L_UM,
        rsil_r_ohm=rsil_r,
        vtt_r_top_ohm=r_top,
        vtt_r_bot_ohm=r_bot,
        vtt_r_top_w_um=wt,
        vtt_r_top_l_um=lt,
        vtt_r_bot_w_um=wb,
        vtt_r_bot_l_um=lb,
        vtt_i_a=i_div,
        vtt_cap_w_um=cw,
        vtt_cap_l_um=cl,
        vtt_cap_f=c_f,
        cl_tb_f=cl_tb_f,
    )


def print_summary(params: TermParams) -> None:
    cap = shunt_cap_accounting_ff(params.pad_c_f)
    vtt_cap_ff = params.vtt_cap_f * 1e15
    xc_vtt_1ghz = 1.0 / (2.0 * math.pi * 1e9 * params.vtt_cap_f)
    f_pred_ghz = 1.0 / (2.0 * math.pi * Z0_DIFF_OHM * cap["shunt_diff_fF"] * 1e-15) / 1e9
    print("=== Termination stage sizing (term_pdk.cir) ===")
    print(f"  VDD={params.vdd:.3f} V  vtt(VBASE)={params.vbase:.4f} V  I_div={params.vtt_i_a*1e3:.3f} mA")
    print(
        f"  vtt divider: R_top={params.vtt_r_top_ohm:.1f} Ohm "
        f"(rppd w={params.vtt_r_top_w_um:g} l={params.vtt_r_top_l_um:g} um)"
    )
    print(
        f"               R_bot={params.vtt_r_bot_ohm:.1f} Ohm "
        f"(rppd w={params.vtt_r_bot_w_um:g} l={params.vtt_r_bot_l_um:g} um)"
    )
    print(
        f"  vtt decap: cap_cmim w={params.vtt_cap_w_um:g} l={params.vtt_cap_l_um:g} um "
        f"-> {vtt_cap_ff:.0f} fF  (Xc@1GHz={xc_vtt_1ghz:.2f} Ohm)"
    )
    print(
        f"  50 Ohm term: rsil w={params.rsil_w_um:g} l={params.rsil_l_um:g} um "
        f"-> {params.rsil_r_ohm:.2f} Ohm (ngspice op)"
    )
    print(
        f"  Bond pad: {params.pad_w_um:g}x{params.pad_l_um:g} um stacked TM1+TM2 "
        f"(C to sub from TM1 bottom plate) -> {cap['pad_per_side_fF']:.1f} fF per side"
    )
    print(f"  ESD: diodevdd_2kv + diodevss_2kv m={params.esd_m} -> {cap['esd_per_side_fF']:.1f} fF per side")
    print(
        f"  Shunt C accounting: per side={cap['shunt_per_side_fF']:.1f} fF  "
        f"differential={cap['shunt_diff_fF']:.1f} fF  "
        f"(predicted f_3dB ~ {f_pred_ghz:.0f} GHz vs 100 Ohm diff)"
    )
    print(f"  CL_TB (CTLE FO1 load)={params.cl_tb_f:.4e} F")
    print(f"  AC/tran source: {RSRC_LEG_OHM:.0f} Ohm per leg ({Z0_DIFF_OHM:.0f} Ohm diff)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Size RX termination stage from LUTs")
    parser.add_argument("--vdd", type=float, default=VDD_DEFAULT_V)
    parser.add_argument("--vtt", type=float, default=VTT_TARGET_V, help="Target vtt (V)")
    parser.add_argument("--idiv-ma", type=float, default=IDIV_TARGET_A * 1e3, help="Divider current (mA)")
    parser.add_argument("--pad-w-um", type=float, default=70.0)
    parser.add_argument("--pad-l-um", type=float, default=70.0)
    parser.add_argument("--no-pad-cap", action="store_true", help="Set PAD_C=0")
    parser.add_argument("--no-rsil-verify", action="store_true")
    parser.add_argument("--json", type=Path, help="Write JSON summary")
    args = parser.parse_args()

    if not os.environ.get("PDK_ROOT"):
        print("Warning: PDK_ROOT not set — rsil verify falls back to LUT.")

    pad_c_override = 0.0 if args.no_pad_cap else None
    params = size_term(
        vdd=args.vdd,
        vtt=args.vtt,
        idiv=args.idiv_ma * 1e-3,
        pad_w_um=args.pad_w_um,
        pad_l_um=args.pad_l_um,
        pad_c_override=pad_c_override,
        verify_rsil=not args.no_rsil_verify,
    )
    extra = to_extra(params)
    write_term_params_inc(params, DEFAULT_OUT)
    print_summary(params)
    print("\nextra_params tokens:")
    for k, v in sorted(extra.items()):
        print(f"  {k}={v}")
    if args.json:
        payload = {**params.__dict__, "extra_params": extra}
        payload.pop("extra", None)
        args.json.write_text(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
