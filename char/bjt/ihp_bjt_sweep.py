#!/usr/bin/env python3
"""IHP SG13G2 BJT / HBT DC + AC characterization → portable .npz LUTs.

Builds Gummel-style tables of Ic, Ib, β, gm, go vs (|VBE|, |VCE|) for each
geometry. HBT devices also get a separate OP/AC pass for Cbe/Cbc, Cin, and
fT (10 GHz |h21|). Intended as quick design-space LUTs so later browsing
does not re-run SPICE.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from char.common.lut import matrange, parse_wrdata, save_lut  # noqa: E402

# Nested-dc wrdata: only [ic]/[ib] track bias reliably. gm/go/caps freeze
# under wrdata — derive gm/go numerically; leave caps as NaN for now.
PROBE_NPN = ["IC", "IB"]
PROBE_PNP = ["IC", "IB"]
AC_PROBE_NAMES = ["VBE", "VCE", "IC_AC", "FT", "CIN", "CBE", "CBC"]
AC_FREQ_HZ = 10e9


@dataclass(frozen=True)
class BjtDevice:
    key: str
    model: str
    polarity: str  # "npn" | "pnp"
    kind: str  # "hbt" | "lateral_pnp"
    geo: str  # "nx" | "nx_el" | "area_peri"


DEVICES: dict[str, BjtDevice] = {
    "npn13G2": BjtDevice("npn13G2", "npn13G2", "npn", "hbt", "nx"),
    "npn13G2l": BjtDevice("npn13G2l", "npn13G2l", "npn", "hbt", "nx_el"),
    "npn13G2v": BjtDevice("npn13G2v", "npn13G2v", "npn", "hbt", "nx_el"),
    "pnpMPA": BjtDevice("pnpMPA", "pnpMPA", "pnp", "lateral_pnp", "area_peri"),
}


def _require_env() -> Path:
    pdk_root = os.environ.get("PDK_ROOT")
    if not pdk_root:
        raise SystemExit("PDK_ROOT is not set. Source ~/.local/share/ihp-eda/env.sh first.")
    models = Path(pdk_root) / "ihp-sg13g2" / "libs.tech" / "ngspice" / "models"
    if not (models / "cornerHBT.lib").is_file():
        raise SystemExit(f"Missing cornerHBT.lib under {models}")
    return models


def _ngspice() -> str:
    exe = shutil.which("ngspice")
    if not exe:
        raise SystemExit("ngspice not found on PATH")
    return exe


def _instance(dev: BjtDevice, geom: dict[str, float]) -> tuple[str, str]:
    """Return (instance_line, lowercase probe device name)."""
    if dev.geo == "nx":
        nx = int(geom["Nx"])
        line = f"XQ1 c b e s {dev.model} Nx={nx}"
    elif dev.geo == "nx_el":
        nx = int(geom["Nx"])
        el = float(geom["El"])
        line = f"XQ1 c b e s {dev.model} Nx={nx} El={el}"
    else:
        a = float(geom["a"])
        p = float(geom["p"])
        # pnpMPA is 3-terminal (c b e); no substrate pin.
        line = f"XQ1 c b e {dev.model} a={a} p={p}"
    probe = f"q.xq1.q{dev.model.lower()}"
    return line, probe


def write_netlist(
    path: Path,
    *,
    models: Path,
    corner: str,
    dev: BjtDevice,
    geom: dict[str, float],
    vbe: np.ndarray,
    vce: np.ndarray,
) -> list[str]:
    """Write nested-DC netlist. Returns probe names used in wrdata order."""
    inst, probe = _instance(dev, geom)
    # Emitter (and NPN substrate) must be grounded — otherwise the DUT floats.
    commons = "Ve e 0 dc 0\nVs s 0 dc 0" if dev.polarity == "npn" else "Ve e 0 dc 0"

    if dev.polarity == "npn":
        # Inner VBE (fast), outer VCE (slow). Axes stored as positive volts.
        nested = (
            f"dc Vbe {vbe[0]:.6g} {vbe[-1]:.6g} {vbe[1] - vbe[0]:.6g} "
            f"Vce {vce[0]:.6g} {vce[-1]:.6g} {vce[1] - vce[0]:.6g}"
        )
        names = PROBE_NPN
        probes = f"@{probe}[ic] @{probe}[ib]"
    else:
        nested = (
            f"dc Vbe {-vbe[0]:.6g} {-vbe[-1]:.6g} {-(vbe[1] - vbe[0]):.6g} "
            f"Vce {-vce[0]:.6g} {-vce[-1]:.6g} {-(vce[1] - vce[0]):.6g}"
        )
        names = PROBE_PNP
        # Currents are negative under PNP bias; reshape_nested takes abs().
        probes = f"@{probe}[ic] @{probe}[ib]"

    text = f"""* IHP SG13G2 BJT characterization — {dev.key}
.lib '{models}/cornerHBT.lib' {corner}
{inst}
Vbe b 0 dc 0
Vce c 0 dc 0
{commons}
.options savecurrents
.control
{nested}
wrdata out.raw {probes}
.endc
.end
"""
    path.write_text(text)
    return names


def _compose_values(name: str, values: np.ndarray) -> str:
    parts = " ".join(f"{v:.12g}" for v in values)
    return f"compose {name} values {parts}"


def write_ac_netlist(
    path: Path,
    *,
    models: Path,
    corner: str,
    dev: BjtDevice,
    geom: dict[str, float],
    vbe: np.ndarray,
    vce: np.ndarray,
) -> None:
    """Grounded-emitter CE with Vbe ac=1; per-bias OP + 10 GHz AC in dowhile loops."""
    if len(vbe) < 2 or len(vce) < 2:
        # ngspice compose with one value is a scalar (not indexable).
        raise ValueError("AC pass requires at least two VBE and VCE points")

    inst, probe = _instance(dev, geom)
    commons = "Ve e 0 dc 0\nVs s 0 dc 0"
    vbe_compose = _compose_values("vbe_list", vbe)
    vce_compose = _compose_values("vce_list", vce)
    freq = AC_FREQ_HZ

    text = f"""* IHP SG13G2 BJT AC characterization — {dev.key}
.lib '{models}/cornerHBT.lib' {corner}
{inst}
Vbe b 0 dc {vbe[0]:.12g} ac 1
Vce c 0 dc {vce[0]:.12g}
{commons}
.control
{vbe_compose}
{vce_compose}
let ivce = 0
dowhile ivce < length(vce_list)
  let vce_set = vce_list[ivce]
  alter Vce dc = vce_set
  let ivbe = 0
  dowhile ivbe < length(vbe_list)
    let vbe_set = vbe_list[ivbe]
    alter Vbe dc = vbe_set
    op
    ac lin 1 {freq:.12g} {freq:.12g}
    let icv = abs(@{probe}[ic])
    let cbev = abs(@{probe}[cbe])
    let cbcv = abs(@{probe}[cbc])
    let h21 = abs(i(Vce)/i(Vbe))
    let ftv = {freq:.12g} * h21
    let cinv = -imag(i(Vbe))/(2*pi*{freq:.12g})
    wrdata ac.raw vbe_set vce_set icv ftv cinv cbev cbcv
    set appendwrite
    let ivbe = ivbe + 1
  end
  let ivce = ivce + 1
end
.endc
.end
"""
    path.write_text(text)


def reshape_ac(
    params: dict[str, np.ndarray],
    n_vbe: int,
    n_vce: int,
) -> dict[str, np.ndarray]:
    """Reshape AC wrdata vectors → (n_vce, n_vbe); inner VBE, outer VCE."""
    expected = n_vbe * n_vce
    out: dict[str, np.ndarray] = {}
    for key, vec in params.items():
        if vec.size != expected:
            raise RuntimeError(f"{key}: expected {expected} AC points, got {vec.size}")
        out[key] = vec.reshape(n_vce, n_vbe)
    return out


def run_ac_one(
    *,
    ngspice: str,
    models: Path,
    corner: str,
    dev: BjtDevice,
    geom: dict[str, float],
    vbe: np.ndarray,
    vce: np.ndarray,
    work: Path,
) -> dict[str, np.ndarray]:
    cir = work / "bjt_ac.cir"
    write_ac_netlist(
        cir, models=models, corner=corner, dev=dev, geom=geom, vbe=vbe, vce=vce
    )
    log = work / "ngspice_ac.log"
    with log.open("w") as lf:
        proc = subprocess.run(
            [ngspice, "-b", "-o", str(log), str(cir)],
            cwd=work,
            stdout=lf,
            stderr=subprocess.STDOUT,
            check=False,
        )
    raw = work / "ac.raw"
    if proc.returncode != 0 or not raw.is_file():
        raise RuntimeError(
            f"ngspice AC failed for {dev.key} {geom}:\n{log.read_text()[-4000:]}"
        )
    params = parse_wrdata(raw, AC_PROBE_NAMES)
    shaped = reshape_ac(params, n_vbe=len(vbe), n_vce=len(vce))
    return {
        "FT": shaped["FT"],
        "CIN": shaped["CIN"],
        "CBE": shaped["CBE"],
        "CBC": shaped["CBC"],
    }


def merge_ac_into_cube(cube: dict[str, np.ndarray], ac: dict[str, np.ndarray]) -> None:
    cube["CBE"] = ac["CBE"]
    cube["CBC"] = ac["CBC"]
    cube["FT"] = ac["FT"]
    cube["CIN"] = ac["CIN"]


def add_ac_placeholders(cube: dict[str, np.ndarray]) -> None:
    ic = cube["IC"]
    cube["FT"] = np.full_like(ic, np.nan)
    cube["CIN"] = np.full_like(ic, np.nan)


def reshape_nested(
    params: dict[str, np.ndarray],
    n_vbe: int,
    n_vce: int,
) -> dict[str, np.ndarray]:
    """Reshape control-section nested dc vectors → (n_vce, n_vbe)."""
    expected = n_vbe * n_vce
    out: dict[str, np.ndarray] = {}
    for key, vec in params.items():
        if vec.size != expected:
            raise RuntimeError(f"{key}: expected {expected} points, got {vec.size}")
        out[key] = np.abs(vec.reshape(n_vce, n_vbe))
    return out


def add_derived(
    cube: dict[str, np.ndarray],
    vbe: np.ndarray,
    vce: np.ndarray,
) -> dict[str, np.ndarray]:
    """β from currents; gm/go from finite differences (reliable vs frozen @q)."""
    ic = cube["IC"]
    ib = np.maximum(cube["IB"], 1e-30)
    cube["BETA"] = ic / ib

    # ∂Ic/∂VBE along axis=-1, ∂Ic/∂VCE along axis=-2
    gm_num = np.gradient(ic, vbe, axis=-1)
    go_num = np.gradient(ic, vce, axis=-2)
    # Prefer numerical gm/go; keep probe values only if numerical failed
    cube["GM"] = np.maximum(gm_num, 0.0)
    cube["GO"] = np.maximum(go_num, 0.0)
    cube["VA"] = np.where(cube["GO"] > 0, ic / cube["GO"], np.nan)
    cube["GM_IC"] = np.where(ic > 0, cube["GM"] / ic, np.nan)

    # Capacitances need per-bias OP (not filled by nested-dc wrdata).
    cube["CBE"] = np.full_like(ic, np.nan)
    cube["CBC"] = np.full_like(ic, np.nan)
    return cube


def run_one(
    *,
    ngspice: str,
    models: Path,
    corner: str,
    dev: BjtDevice,
    geom: dict[str, float],
    vbe: np.ndarray,
    vce: np.ndarray,
    work: Path,
    skip_ac: bool,
) -> dict[str, np.ndarray]:
    cir = work / "bjt.cir"
    names = write_netlist(
        cir, models=models, corner=corner, dev=dev, geom=geom, vbe=vbe, vce=vce
    )
    log = work / "ngspice.log"
    with log.open("w") as lf:
        proc = subprocess.run(
            [ngspice, "-b", "-o", str(log), str(cir)],
            cwd=work,
            stdout=lf,
            stderr=subprocess.STDOUT,
            check=False,
        )
    raw = work / "out.raw"
    if proc.returncode != 0 or not raw.is_file():
        raise RuntimeError(f"ngspice failed for {dev.key} {geom}:\n{log.read_text()[-4000:]}")

    params = parse_wrdata(raw, names)
    cube = reshape_nested(params, n_vbe=len(vbe), n_vce=len(vce))
    cube = add_derived(cube, vbe, vce)

    if dev.kind == "hbt" and not skip_ac:
        ac = run_ac_one(
            ngspice=ngspice,
            models=models,
            corner=corner,
            dev=dev,
            geom=geom,
            vbe=vbe,
            vce=vce,
            work=work,
        )
        merge_ac_into_cube(cube, ac)
    else:
        add_ac_placeholders(cube)

    return cube


def stack_geometries(cubes: list[dict[str, np.ndarray]]) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    for k in cubes[0]:
        out[k] = np.stack([c[k] for c in cubes], axis=0)
    return out


def characterize_device(
    *,
    ngspice: str,
    models: Path,
    corner: str,
    dev: BjtDevice,
    geometries: list[dict[str, float]],
    vbe: np.ndarray,
    vce: np.ndarray,
    out_dir: Path,
    skip_ac: bool,
) -> Path:
    cubes: list[dict[str, np.ndarray]] = []
    for i, geom in enumerate(geometries):
        print(f"  [{i + 1}/{len(geometries)}] {dev.key} {geom}", flush=True)
        with tempfile.TemporaryDirectory(prefix=f"bjt_{dev.key}_") as tmp:
            cube = run_one(
                ngspice=ngspice,
                models=models,
                corner=corner,
                dev=dev,
                geom=geom,
                vbe=vbe,
                vce=vce,
                work=Path(tmp),
                skip_ac=skip_ac,
            )
        cubes.append(cube)

    stacked = stack_geometries(cubes)

    if dev.geo == "nx":
        nx = np.array([g["Nx"] for g in geometries], dtype=float)
        geo_arrays = {"Nx": nx, "L": nx, "W": np.ones_like(nx)}
    elif dev.geo == "nx_el":
        nx = np.array([g["Nx"] for g in geometries], dtype=float)
        el = np.array([g["El"] for g in geometries], dtype=float)
        geo_arrays = {"Nx": nx, "El": el, "L": el, "W": nx}
    else:
        a = np.array([g["a"] for g in geometries], dtype=float)
        p = np.array([g["p"] for g in geometries], dtype=float)
        geo_arrays = {"a": a, "p": p, "L": a, "W": p}

    lookup: dict[str, Any] = {
        # Shared browser aliases (VGS←VBE, VDS←VCE) + native names
        "VGS": vbe.astype(float),
        "VDS": vce.astype(float),
        "VBE": vbe.astype(float),
        "VCE": vce.astype(float),
        **geo_arrays,
        **stacked,
    }

    meta = {
        "format": "ihp-bjt-lut-v1",
        "device": dev.key,
        "model": dev.model,
        "polarity": dev.polarity,
        "kind": dev.kind,
        "geo_mode": dev.geo,
        "corner": corner,
        "pdk": "ihp-sg13g2",
        "geometries": geometries,
        "axes": {
            "VBE": "base-emitter |V| (V)",
            "VCE": "collector-emitter |V| (V)",
            "geometry_axis0": "stacked geometries in file order",
        },
        "signals": {
            "ID": "alias of IC",
            "IC": "collector |Ic| (A)",
            "IB": "base |Ib| (A)",
            "BETA": "Ic/Ib",
            "GM": "∂Ic/∂VBE (S), numerical",
            "GO": "∂Ic/∂VCE (S), numerical",
            "CBE": "Cbe (F) from per-bias OP (HBT AC pass)",
            "CBC": "Cbc (F) from per-bias OP (HBT AC pass)",
            "CIN": "input capacitance (F) from Y11 @ 10 GHz (HBT AC pass)",
            "FT": "transition frequency (Hz) = 10 GHz × |h21| (HBT AC pass)",
            "VA": "Early approx Ic/go (V)",
            "GM_IC": "gm/Ic (1/V)",
        },
        "shape": "signal arrays are (n_geo, n_vce, n_vbe); axes are 1-D",
        "notes": (
            "gm/go/beta recomputed from Ic,Ib after DC; VBIC @q[gm] etc. "
            "often freeze under nested dc wrdata. HBT Cbe/Cbc/Cin/fT come from "
            "a separate OP+AC pass (10 GHz |h21|, not nested-dc wrdata). "
            "PNP/lateral devices skip AC; FT/CIN remain NaN."
        ),
    }
    # Friendly alias used by MOS browsers
    lookup["ID"] = lookup["IC"]

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"sg13_{dev.key}.npz"
    save_lut(out_path, lookup, meta)
    (out_dir / f"sg13_{dev.key}.meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"  wrote {out_path}", flush=True)
    return out_path


def default_geometries(dev: BjtDevice, quick: bool) -> list[dict[str, float]]:
    if quick:
        if dev.geo == "nx":
            return [{"Nx": 1.0}, {"Nx": 2.0}]
        if dev.geo == "nx_el":
            return [{"Nx": 1.0, "El": 1.0}, {"Nx": 1.0, "El": 2.5}]
        # a [m²], p [m] — symbol defaults ~0.81e-12 / 3.6e-6
        return [{"a": 0.81e-12, "p": 3.6e-6}, {"a": 1.0e-12, "p": 5.2e-6}]

    if dev.geo == "nx":
        return [{"Nx": float(n)} for n in (1, 2, 4, 8)]
    if dev.geo == "nx_el":
        out: list[dict[str, float]] = []
        for nx in (1, 2):
            for el in (1.0, 2.5, 5.0):
                out.append({"Nx": float(nx), "El": el})
        return out
    return [
        {"a": 0.81e-12, "p": 3.6e-6},
        {"a": 1.0e-12, "p": 5.2e-6},
        {"a": 2.0e-12, "p": 7.0e-6},
        {"a": 4.0e-12, "p": 10.0e-6},
    ]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--device",
        action="append",
        choices=sorted(DEVICES.keys()) + ["all"],
        help="Device key (repeatable). Default: all",
    )
    p.add_argument("--corner", default="hbt_typ", help="cornerHBT.lib section (default hbt_typ)")
    p.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parent / "out")
    p.add_argument("--vbe-start", type=float, default=0.5)
    p.add_argument("--vbe-stop", type=float, default=0.95)
    p.add_argument("--vbe-step", type=float, default=0.025)
    p.add_argument("--vce-start", type=float, default=0.3)
    p.add_argument("--vce-stop", type=float, default=1.8)
    p.add_argument("--vce-step", type=float, default=0.15)
    p.add_argument("--quick", action="store_true", help="Coarse geometry + bias grid")
    p.add_argument(
        "--skip-ac",
        action="store_true",
        help="Skip HBT OP/AC pass (FT/CIN/Cbe/Cbc remain NaN)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    models = _require_env()
    ngspice = _ngspice()

    devices = args.device or ["all"]
    keys = list(DEVICES.keys()) if "all" in devices else list(dict.fromkeys(devices))

    if args.quick:
        # matrange(start, step, stop)
        vbe = matrange(0.6, 0.05, 0.9)
        vce = matrange(0.5, 0.25, 1.5)
    else:
        vbe = matrange(args.vbe_start, args.vbe_step, args.vbe_stop)
        vce = matrange(args.vce_start, args.vce_step, args.vce_stop)

    print(f"BJT char: devices={keys} corner={args.corner} skip_ac={args.skip_ac}")
    print(f"  |VBE|={vbe[0]:.3g}..{vbe[-1]:.3g} ({len(vbe)} pts)")
    print(f"  |VCE|={vce[0]:.3g}..{vce[-1]:.3g} ({len(vce)} pts)")

    for key in keys:
        dev = DEVICES[key]
        geoms = default_geometries(dev, args.quick)
        print(f"\n== {key} ({len(geoms)} geometries) ==")
        characterize_device(
            ngspice=ngspice,
            models=models,
            corner=args.corner,
            dev=dev,
            geometries=geoms,
            vbe=vbe,
            vce=vce,
            out_dir=args.out_dir,
            skip_ac=args.skip_ac,
        )

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
