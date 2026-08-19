#!/usr/bin/env python3
"""IHP SG13G2 resistor DC characterization → portable .npz LUTs.

Measures R = Vtest / |I| for rsil, rppd, rhigh vs (W, L, T) using
typical-corner models from cornerRES.lib. Substrate pin bn is tied to 0 V.
"""

from __future__ import annotations

import argparse
import json
import os
import re
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

from char.common.lut import load_lut, matrange, parse_wrdata, save_lut  # noqa: E402

VTEST = 1.5  # V across resistor (a → b)

CURRENT_RE = re.compile(
    r"i\s*\(\s*vtest\s*\)\s*=\s*([-+eE0-9.]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ResModel:
    key: str
    subckt: str


MODELS: dict[str, ResModel] = {
    "rsil": ResModel("rsil", "rsil"),
    "rppd": ResModel("rppd", "rppd"),
    "rhigh": ResModel("rhigh", "rhigh"),
}


def _require_env() -> Path:
    pdk_root = os.environ.get("PDK_ROOT")
    if not pdk_root:
        raise SystemExit("PDK_ROOT is not set. Source ~/.local/share/ihp-eda/env.sh first.")
    models = Path(pdk_root) / "ihp-sg13g2" / "libs.tech" / "ngspice" / "models"
    if not (models / "cornerRES.lib").is_file():
        raise SystemExit(f"Missing cornerRES.lib under {models}")
    return models


def _ngspice() -> str:
    exe = shutil.which("ngspice")
    if not exe:
        raise SystemExit("ngspice not found on PATH")
    return exe


def width_grid(quick: bool) -> np.ndarray:
    if quick:
        return np.array([0.5, 5.0], dtype=float)
    return np.array([0.5, 1.0, 2.0, 5.0], dtype=float)


def length_grid(model: str, quick: bool) -> np.ndarray:
    if quick:
        if model == "rhigh":
            return np.array([0.96, 5.0], dtype=float)
        return np.array([0.5, 5.0], dtype=float)
    if model == "rhigh":
        return np.array([0.96, 1.0, 2.0, 5.0], dtype=float)
    return np.array([0.5, 1.0, 2.0, 5.0], dtype=float)


def temp_grid(quick: bool) -> np.ndarray:
    if quick:
        return np.array([-40.0, 27.0], dtype=float)
    return np.array([-40.0, 27.0, 125.0], dtype=float)


def write_netlist(
    path: Path,
    *,
    models: Path,
    corner: str,
    model: ResModel,
    w_um: float,
    l_um: float,
    temps_c: np.ndarray,
) -> None:
    w_m = w_um * 1e-6
    l_m = l_um * 1e-6
    inst = f"XQ1 a b bn {model.subckt} w={w_m:.6g} l={l_m:.6g}"
    temp_ops = []
    for t in temps_c:
        temp_ops.append(f"set temp={float(t):g}")
        temp_ops.append("op")
        temp_ops.append("print i(vtest)")
    control = "\n".join(temp_ops)

    text = f"""* IHP SG13G2 resistor characterization — {model.key}
.lib '{models}/cornerRES.lib' {corner}
{inst}
Vtest a 0 dc {VTEST:g}
Vb b 0 dc 0
Vbn bn 0 dc 0
.options savecurrents
.control
{control}
.endc
.end
"""
    path.write_text(text)


def parse_currents(log_text: str, n_temps: int) -> np.ndarray:
    matches = CURRENT_RE.findall(log_text)
    if len(matches) < n_temps:
        raise RuntimeError(
            f"Expected {n_temps} i(vtest) values, found {len(matches)} in log:\n"
            f"{log_text[-3000:]}"
        )
    currents = np.array([float(x) for x in matches[:n_temps]], dtype=float)
    return np.abs(currents)


def run_geometry(
    *,
    ngspice: str,
    models: Path,
    corner: str,
    model: ResModel,
    w_um: float,
    l_um: float,
    temps_c: np.ndarray,
    work: Path,
) -> np.ndarray:
    """Return R(Ω) vector aligned with temps_c."""
    cir = work / "res.cir"
    log = work / "ngspice.log"
    write_netlist(
        cir,
        models=models,
        corner=corner,
        model=model,
        w_um=w_um,
        l_um=l_um,
        temps_c=temps_c,
    )
    with log.open("w") as lf:
        proc = subprocess.run(
            [ngspice, "-b", "-o", str(log), str(cir)],
            cwd=work,
            stdout=lf,
            stderr=subprocess.STDOUT,
            check=False,
        )
    log_text = log.read_text()
    if proc.returncode != 0:
        raise RuntimeError(f"ngspice failed for {model.key} W={w_um} L={l_um}:\n{log_text[-4000:]}")
    currents = parse_currents(log_text, len(temps_c))
    with np.errstate(divide="raise", invalid="raise"):
        return VTEST / currents


def characterize_model(
    *,
    ngspice: str,
    models: Path,
    corner: str,
    model: ResModel,
    w_um: np.ndarray,
    l_um: np.ndarray,
    temps_c: np.ndarray,
    out_dir: Path,
) -> Path:
    n_w, n_l, n_t = len(w_um), len(l_um), len(temps_c)
    r_cube = np.full((n_w, n_l, n_t), np.nan, dtype=float)
    total = n_w * n_l
    step = 0

    for iw, w in enumerate(w_um):
        for il, l in enumerate(l_um):
            step += 1
            print(f"  [{step}/{total}] {model.key} W={w:g}um L={l:g}um", flush=True)
            with tempfile.TemporaryDirectory(prefix=f"res_{model.key}_") as tmp:
                r_vec = run_geometry(
                    ngspice=ngspice,
                    models=models,
                    corner=corner,
                    model=model,
                    w_um=float(w),
                    l_um=float(l),
                    temps_c=temps_c,
                    work=Path(tmp),
                )
            r_cube[iw, il, :] = r_vec

    lookup: dict[str, Any] = {
        "W": w_um.astype(float),
        "L": l_um.astype(float),
        "TEMP": temps_c.astype(float),
        "R": r_cube,
        "VTEST": np.asarray(VTEST, dtype=float),
    }

    meta = {
        "format": "ihp-res-lut-v1",
        "device": model.key,
        "model": model.subckt,
        "corner": corner,
        "pdk": "ihp-sg13g2",
        "vtest_V": VTEST,
        "measurement": "DC OP: R = Vtest / |I(Vtest)|, bn tied to 0 V",
        "axes": {
            "W": "drawn width (µm)",
            "L": "drawn length (µm)",
            "TEMP": "simulation temperature (°C)",
            "R": "resistance (Ω), shape (n_w, n_l, n_temp)",
        },
        "shape": "R is (n_w, n_l, n_temp); W, L, TEMP are 1-D axes",
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"sg13_{model.key}.npz"
    save_lut(out_path, lookup, meta)
    (out_dir / f"sg13_{model.key}.meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"  wrote {out_path}", flush=True)
    return out_path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--model",
        action="append",
        choices=sorted(MODELS.keys()) + ["all"],
        help="Resistor model (repeatable). Default: all",
    )
    p.add_argument("--corner", default="res_typ", help="cornerRES.lib section (default res_typ)")
    p.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parent / "out")
    p.add_argument("--quick", action="store_true", help="2 W × 2 L × 2 T grid")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    models = _require_env()
    ngspice = _ngspice()

    selected = args.model or ["all"]
    keys = list(MODELS.keys()) if "all" in selected else list(dict.fromkeys(selected))

    w_grid = width_grid(args.quick)
    t_grid = temp_grid(args.quick)

    print(f"Resistor char: models={keys} corner={args.corner}")
    print(f"  W={w_grid[0]:g}..{w_grid[-1]:g}um ({len(w_grid)} pts)")
    print(f"  T={t_grid[0]:g}..{t_grid[-1]:g}C ({len(t_grid)} pts)")

    for key in keys:
        model = MODELS[key]
        l_grid = length_grid(key, args.quick)
        print(f"\n== {key} L={l_grid[0]:g}..{l_grid[-1]:g}um ({len(l_grid)} pts) ==")
        characterize_model(
            ngspice=ngspice,
            models=models,
            corner=args.corner,
            model=model,
            w_um=w_grid,
            l_um=l_grid,
            temps_c=t_grid,
            out_dir=args.out_dir,
        )

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
