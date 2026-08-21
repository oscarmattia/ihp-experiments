#!/usr/bin/env python3
"""Why the pad-driver Magic BW is 35 GHz against a 91 GHz schematic.

The 819 fF Magic total is not the load. This splits the committed Magic deck
by net, then reruns the schematic AC with three hand ``PAD_C`` values so the
bandwidth can be blamed on a specific capacitor, not on a missing ESD model
or on the collector-to-pad feed.

Nothing is written into the repo; ngspice work goes to a scratch directory.

Usage:
    source ~/.local/share/ihp-eda/env.sh
    python layout/debug_pex/probe_driver_pad_bw.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
EXP = REPO / "circuits" / "ctle56n"
sys.path[:0] = [str(REPO), str(EXP / "python")]

from ctlelib.metrics import compute_ac_peak_metrics, interp_db_at  # noqa: E402
from ctlelib.ngs import parse_ac_raw  # noqa: E402
from layout.debug_pex.probe_signal_net_caps import caps  # noqa: E402
from size_driver import extra_params, size_driver  # noqa: E402
from size_term import ESD_C_FF_PER_PAD, pad_capacitance_f  # noqa: E402
from stage_driver import (  # noqa: E402
    NYQUIST_HZ,
    _prepare_driver_tb,
    _write_work_params,
    pdk_models,
    run_ngspice,
)

MAGIC_CORE = REPO / "layout/blocks/out/postlayout_driver/driver_dut_core_magic.cir"
SCHEMATIC = EXP / "spice" / "driver_pdk.cir"

# Magic C18 outp-vss (the pad-node term that sets C_L).
MAGIC_OUT_VSS_F = 143.56e-15


def _si(x: float) -> str:
    if abs(x) >= 1e-12:
        return f"{x * 1e15:.2f} fF"
    return f"{x * 1e18:.2f} aF"


def attribute_magic(deck: Path) -> None:
    rows = caps(deck)
    by_net: dict[str, float] = {}
    out_terms: list[tuple[str, str, float]] = []
    total = 0.0
    for a, b, c in rows:
        total += c
        by_net[a] = by_net.get(a, 0.0) + c
        by_net[b] = by_net.get(b, 0.0) + c
        if "outp" in (a, b) or "outn" in (a, b):
            out_terms.append((a, b, c))

    print(f"Magic deck: {deck}")
    print(f"  capacitors: {len(rows)}  raw sum: {_si(total)}")
    print("  per-net sum (each C counted on both terminals):")
    for net, val in sorted(by_net.items(), key=lambda kv: -kv[1]):
        print(f"    {net:8s}  {_si(val)}")
    print("  terms on outp / outn:")
    for a, b, c in sorted(out_terms, key=lambda t: -t[2]):
        print(f"    {a:6s} — {b:6s}  {_si(c)}")

    hand = pad_capacitance_f(70.0, 70.0)
    print()
    print("  schematic load model (both pads):")
    print(f"    hand PAD_C (TM1 area only)     {_si(hand)} / side")
    print(f"    ESD compact pair               {ESD_C_FF_PER_PAD:.1f} fF / side")
    print(f"    C_L = pad + ESD                {_si(hand + ESD_C_FF_PER_PAD * 1e-15)} / side")
    print("  Magic load model (PAD_C = 0, ESD compact models stay):")
    print(f"    extracted outp-vss             {_si(MAGIC_OUT_VSS_F)}")
    print(f"    + ESD compact pair             {ESD_C_FF_PER_PAD:.1f} fF")
    print(f"    effective C_L                  {_si(MAGIC_OUT_VSS_F + ESD_C_FF_PER_PAD * 1e-15)}")
    wiring = sum(c for a, b, c in out_terms if {a, b} != {"outp", "vss"} and {a, b} != {"outn", "vss"})
    print(f"    other outp/outn terms (feeds)  {_si(wiring)}  (both sides, all couplings)")


def run_schematic_ac(pad_c_f: float, work: Path) -> dict[str, float]:
    params = size_driver()
    ep = extra_params(params)
    ep["PAD_C"] = f"{pad_c_f:.6g}"
    work.mkdir(parents=True, exist_ok=True)
    _write_work_params(work, ep)
    spice_dir = EXP / "spice"
    tb = _prepare_driver_tb(
        spice_dir / "tb_ac_diff.cir", SCHEMATIC, work, pdk_models(), spice_dir, ep
    )
    run_ngspice(tb, work, "ac_diff.log")
    freq, voutp, voutn, vin_p, vin_n = parse_ac_raw(work / "ac_diff.raw")
    vod = voutp - voutn
    vid = vin_p - vin_n
    h = np.where(np.abs(vid) > 1e-30, vod / vid, 0.0)
    h_db = 20.0 * np.log10(np.abs(h))
    dc = float(h_db[0])
    g28 = float(interp_db_at(freq, h_db, NYQUIST_HZ))
    peak, fpeak, f3, _ = compute_ac_peak_metrics(freq, h_db)
    return {
        "pad_c_fF": pad_c_f * 1e15,
        "dc_gain_dB": dc,
        "ac_gain_28G_dB": g28,
        "peaking_dB": g28 - dc,
        "f_3dB_GHz": f3 / 1e9,
        "f_peak_GHz": fpeak / 1e9,
        "G_peak_dB": peak,
    }


def main() -> int:
    if not MAGIC_CORE.is_file():
        print(f"missing {MAGIC_CORE}", file=sys.stderr)
        return 1
    attribute_magic(MAGIC_CORE)
    cases = [
        ("ESD only (PAD_C=0)", 0.0),
        ("schematic hand pad (27.68 fF)", 27.6801e-15),
        ("Magic outp-vss (143.56 fF)", MAGIC_OUT_VSS_F),
    ]
    print()
    print("Schematic AC, ESD compact models kept, PAD_C swept:")
    print(f"{'case':<32} {'PAD_C':>8} {'DC':>8} {'28G':>8} {'f-3dB':>8}")
    with tempfile.TemporaryDirectory(prefix="driver_pad_bw_") as tmp:
        root = Path(tmp)
        for name, pad_c in cases:
            m = run_schematic_ac(pad_c, root / name.replace(" ", "_"))
            print(
                f"{name:<32} {m['pad_c_fF']:7.2f}  "
                f"{m['dc_gain_dB']:7.2f} {m['ac_gain_28G_dB']:7.2f} "
                f"{m['f_3dB_GHz']:7.2f}"
            )
    print()
    print("Magic extracted DUT (committed):  DC=-0.83  28G=-1.67  f-3dB=34.88 GHz")
    print("If the last schematic row lands near 35 GHz, the BW gap is the pad")
    print("metal model, not a missing ESD device and not the collector feed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
