#!/usr/bin/env python3
"""IHP SG13G2 passive capacitor characterization → portable .npz LUTs.

Devices:
  - cap_cmim   (MIM, cornerCAP.lib / cap_typ)
  - cap_cmomi  (interdigitated MoM OSDI, needs ngspice .spiceinit)
  - sg13_moscap_n / sg13_moscap_p (cornerMOSCAP.lib / moscap_tt)
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

from char.common.lut import parse_wrdata, save_lut  # noqa: E402


def _parse_ac_wrdata(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Parse ngspice AC ``wrdata`` (frequency + one complex magnitude column)."""
    rows: list[list[float]] = []
    with Path(path).open() as f:
        for line in f:
            parts = line.split()
            if parts:
                rows.append([float(x) for x in parts])
    data = np.asarray(rows, dtype=float)
    if data.ndim != 2 or data.shape[1] < 2:
        raise RuntimeError(f"{path}: unexpected wrdata shape {data.shape}")
    # AC wrdata rows are frequency interleaved with the Y column (see PDK ac_mim_cap).
    freq = data[:, 0]
    ycol = 4 if data.shape[1] >= 5 else 1
    return freq, data[:, ycol]

F_CHAR_HZ = 100e6
R_SERIES_OHM = 100e3
PI = np.pi


@dataclass(frozen=True)
class MimDevice:
    key: str
    subckt: str
    corner_lib: str
    corner_section: str
    geometries_um: tuple[tuple[float, float], ...]
    cmomi: bool = False


MIM_CMIM = MimDevice(
    key="cap_cmim",
    subckt="cap_cmim",
    corner_lib="cornerCAP.lib",
    corner_section="cap_typ",
    geometries_um=((7.0, 7.0), (10.0, 10.0), (20.0, 20.0), (50.0, 50.0)),
)

MIM_CMOMI = MimDevice(
    key="cap_cmomi",
    subckt="cap_cmomi",
    corner_lib="cornerCAP.lib",
    corner_section="cap_typ",
    geometries_um=((5.0, 5.0), (10.0, 10.0), (30.0, 30.0)),
    cmomi=True,
)


@dataclass(frozen=True)
class MoscapDevice:
    key: str
    subckt: str
    bulk_pin: str
    geometries_um: tuple[float, ...]
    v_biases: tuple[float, ...]


MOSCAP_N = MoscapDevice(
    key="moscap_n",
    subckt="sg13_moscap_n",
    bulk_pin="SUB",
    geometries_um=(1.0, 3.0, 10.0, 30.0),
    v_biases=(-1.0, -0.5, 0.0, 0.5, 1.0),
)

MOSCAP_P = MoscapDevice(
    key="moscap_p",
    subckt="sg13_moscap_p",
    bulk_pin="NW",
    geometries_um=(1.0, 3.0, 10.0, 30.0),
    v_biases=(-1.0, -0.5, 0.0, 0.5, 1.0),
)


def _require_models(pdk_root: Path | None = None) -> Path:
    root = pdk_root or Path(os.environ.get("PDK_ROOT", ""))
    if not root:
        raise SystemExit("PDK_ROOT is not set. Source ~/.local/share/ihp-eda/env.sh first.")
    models = root / "ihp-sg13g2" / "libs.tech" / "ngspice" / "models"
    if not (models / "cornerCAP.lib").is_file():
        raise SystemExit(f"Missing cornerCAP.lib under {models}")
    return models


def _ngspice() -> str:
    exe = shutil.which("ngspice")
    if not exe:
        raise SystemExit("ngspice not found on PATH")
    return exe


def _spiceinit_src(pdk_root: Path) -> Path:
    return pdk_root / "ihp-sg13g2" / "libs.tech" / "ngspice" / ".spiceinit"


def _prepare_work(work: Path, pdk_root: Path, need_osdi: bool) -> None:
    if need_osdi:
        src = _spiceinit_src(pdk_root)
        if not src.is_file():
            raise SystemExit(f"Missing ngspice .spiceinit at {src}")
        dst = work / ".spiceinit"
        if not dst.exists():
            dst.symlink_to(src)


def _run_ngspice(ngspice: str, cir: Path, work: Path) -> Path:
    log = work / "ngspice.log"
    with log.open("w") as lf:
        proc = subprocess.run(
            [ngspice, "-b", "-o", str(log), str(cir.name)],
            cwd=work,
            stdout=lf,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if proc.returncode != 0:
        raise RuntimeError(f"ngspice failed for {cir.name}:\n{log.read_text()[-4000:]}")
    return log


def _c_from_mag_at_f(freq: np.ndarray, mag: np.ndarray, f_hz: float) -> float:
    """Extract C from high-pass |H(f)| with series R to ground (PDK ac_mim_cap topology)."""
    m = float(np.interp(f_hz, freq, mag))
    m = min(max(m, 1e-12), 1.0 - 1e-12)
    omega_r = m / np.sqrt(1.0 - m * m)
    return omega_r / (2.0 * PI * f_hz * R_SERIES_OHM)


def _interp_crossing(x: np.ndarray, y: np.ndarray, level: float) -> float:
    """Linear interpolation of x where y first crosses level (ascending x)."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    order = np.argsort(x)
    x, y = x[order], y[order]
    above = y >= level
    if not np.any(above):
        return float("nan")
    idx = int(np.argmax(above))
    if idx == 0:
        return float(x[0])
    x0, x1 = x[idx - 1], x[idx]
    y0, y1 = y[idx - 1], y[idx]
    if y1 == y0:
        return float(x1)
    return float(x0 + (level - y0) * (x1 - x0) / (y1 - y0))


def write_mim_netlist(
    path: Path,
    *,
    models: Path,
    dev: MimDevice,
    w_m: float,
    l_m: float,
    f_start: float,
    f_stop: float,
    n_pts: int,
) -> None:
    inst = f"Xc1 in out {dev.subckt} w={w_m:g} l={l_m:g}"
    if dev.cmomi:
        inst += " mmin=1 mmax=5 feed=double subblock=0 m=1 mm_ok=1"
    text = f"""* IHP SG13G2 {dev.key} AC characterization
.lib '{models}/{dev.corner_lib}' {dev.corner_section}
{inst}
Vin in 0 dc 0 ac 1
R1 out 0 {R_SERIES_OHM:g}
.control
save all
ac dec {n_pts} {f_start:g} {f_stop:g}
let mag = abs(out)
wrdata out_mag.raw frequency mag
.endc
.end
"""
    path.write_text(text)


def run_mim_geometry(
    *,
    ngspice: str,
    models: Path,
    pdk_root: Path,
    dev: MimDevice,
    w_um: float,
    l_um: float,
    f_start: float,
    f_stop: float,
    n_pts: int,
    work: Path,
) -> tuple[float, float, float, np.ndarray, np.ndarray]:
    _prepare_work(work, pdk_root, need_osdi=dev.cmomi)
    cir = work / "mim.cir"
    write_mim_netlist(
        cir,
        models=models,
        dev=dev,
        w_m=w_um * 1e-6,
        l_m=l_um * 1e-6,
        f_start=f_start,
        f_stop=f_stop,
        n_pts=n_pts,
    )
    _run_ngspice(ngspice, cir, work)
    mag_raw = work / "out_mag.raw"
    if not mag_raw.is_file():
        raise RuntimeError(f"Missing AC wrdata under {work}")
    freq, mag = _parse_ac_wrdata(mag_raw)
    c_at_100mhz = _c_from_mag_at_f(freq, mag, F_CHAR_HZ)
    f_3db = _interp_crossing(freq, mag, 0.707)
    c_3db = 1.0 / (2.0 * PI * f_3db * R_SERIES_OHM) if np.isfinite(f_3db) else float("nan")
    # PDK ac_mim_cap uses the -3dB corner; it tracks LF C for MoM caps where |H|@100MHz
    # is distorted by distributed parasitics. For MIM both methods agree near 100 MHz.
    c_primary = c_3db if np.isfinite(c_3db) else c_at_100mhz
    cvec = np.array([_c_from_mag_at_f(freq, mag, f) for f in freq])
    return c_primary, c_3db, c_at_100mhz, freq, cvec


def characterize_mim(
    *,
    ngspice: str,
    models: Path,
    pdk_root: Path,
    dev: MimDevice,
    geometries: list[tuple[float, float]],
    out_dir: Path,
    f_start: float,
    f_stop: float,
    n_pts: int,
    store_f_sweep: bool,
) -> Path:
    c_list: list[float] = []
    c3db_list: list[float] = []
    c100_list: list[float] = []
    freq_ref: np.ndarray | None = None
    c_vs_f: list[np.ndarray] = []

    for i, (w_um, l_um) in enumerate(geometries):
        print(f"  [{i + 1}/{len(geometries)}] {dev.key} w=l={w_um:g} µm", flush=True)
        with tempfile.TemporaryDirectory(prefix=f"cap_{dev.key}_") as tmp:
            c_primary, c3db, c100, freq, cvec = run_mim_geometry(
                ngspice=ngspice,
                models=models,
                pdk_root=pdk_root,
                dev=dev,
                w_um=w_um,
                l_um=l_um,
                f_start=f_start,
                f_stop=f_stop,
                n_pts=n_pts,
                work=Path(tmp),
            )
        c_list.append(c_primary)
        c3db_list.append(c3db)
        c100_list.append(c100)
        if store_f_sweep:
            if freq_ref is None:
                freq_ref = freq
            elif not np.allclose(freq, freq_ref):
                cvec = np.interp(freq_ref, freq, cvec)
            c_vs_f.append(cvec)

    w_um = np.array([g[0] for g in geometries], dtype=float)
    l_um = np.array([g[1] for g in geometries], dtype=float)
    area_um2 = w_um * l_um
    arrays: dict[str, np.ndarray] = {
        "W": w_um,
        "L": l_um,
        "area_um2": area_um2,
        "C": np.asarray(c_list, dtype=float),
        "C_3db": np.asarray(c3db_list, dtype=float),
        "C_at_100MHz": np.asarray(c100_list, dtype=float),
        "C_density_fF_um2": np.asarray(c_list, dtype=float) / area_um2 * 1e15,
    }
    if store_f_sweep and freq_ref is not None:
        arrays["F"] = freq_ref.astype(float)
        arrays["C_vs_F"] = np.stack(c_vs_f, axis=0)

    meta: dict[str, Any] = {
        "format": "ihp-cap-lut-v1",
        "device": dev.key,
        "subckt": dev.subckt,
        "corner": dev.corner_section,
        "corner_lib": dev.corner_lib,
        "pdk": "ihp-sg13g2",
        "f_char_hz": F_CHAR_HZ,
        "r_series_ohm": R_SERIES_OHM,
        "geometries_um": [{"w": w, "l": l} for w, l in geometries],
        "extraction": (
            "AC high-pass (Vin–C–R) per xschem ac_mim_cap; primary C from -3dB "
            f"corner (C=1/(2πf_3dB·{R_SERIES_OHM:.0f}Ω)); C_at_100MHz from |H(100MHz)|"
        ),
        "signals": {
            "C": "primary capacitance from RC -3dB corner (F), PDK ac_mim_cap style",
            "C_3db": "same as C (F)",
            "C_at_100MHz": "|H|-derived C at 100 MHz (F); may diverge for RF MoM",
            "C_density_fF_um2": "C/area in fF/µm²",
        },
    }
    if dev.cmomi:
        meta["cmomi_params"] = {"mmin": 1, "mmax": 5, "feed": "double"}

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"sg13_{dev.key}.npz"
    save_lut(out_path, arrays, meta)
    (out_dir / f"sg13_{dev.key}.meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"  wrote {out_path}", flush=True)
    return out_path


def write_moscap_netlist(
    path: Path,
    *,
    models: Path,
    dev: MoscapDevice,
    w_um: float,
    l_um: float,
) -> None:
    bulk = dev.bulk_pin.lower()
    text = f"""* IHP SG13G2 {dev.key} C(V) via slow gate ramp
.lib '{models}/cornerMOSCAP.lib' moscap_tt
Xc1 g {bulk} {dev.subckt} w={w_um}u l={l_um}u m=1
V{bulk} {bulk} 0 dc 0
V1 g 0 pwl 0 -1.2 400n 1.2
.control
save all
tran 0.1n 400n
let Cn_abs = abs(i(v1)) / deriv(v(g))
wrdata out.raw v(g) Cn_abs
.endc
.end
"""
    path.write_text(text)


def sample_cv(
    v: np.ndarray,
    c: np.ndarray,
    biases: tuple[float, ...],
) -> np.ndarray:
    v = np.asarray(v, dtype=float)
    c = np.abs(np.asarray(c, dtype=float))
    # Drop duplicate voltage samples from tran breakpoints.
    order = np.argsort(v)
    v, c = v[order], c[order]
    uniq_v, idx = np.unique(v, return_index=True)
    uniq_c = c[idx]
    return np.interp(biases, uniq_v, uniq_c, left=np.nan, right=np.nan)


def run_moscap_geometry(
    *,
    ngspice: str,
    models: Path,
    pdk_root: Path,
    dev: MoscapDevice,
    w_um: float,
    l_um: float,
    biases: tuple[float, ...],
    work: Path,
) -> np.ndarray:
    _prepare_work(work, pdk_root, need_osdi=True)
    cir = work / "moscap.cir"
    write_moscap_netlist(path=cir, models=models, dev=dev, w_um=w_um, l_um=l_um)
    _run_ngspice(ngspice, cir, work)
    raw = work / "out.raw"
    data = parse_wrdata(raw, ["VG", "C"])
    return sample_cv(data["VG"], data["C"], biases)


def characterize_moscap(
    *,
    ngspice: str,
    models: Path,
    pdk_root: Path,
    dev: MoscapDevice,
    geometries_um: list[float],
    biases: tuple[float, ...],
    out_dir: Path,
) -> Path:
    v_axis = np.asarray(biases, dtype=float)
    c_cube: list[np.ndarray] = []
    for i, size_um in enumerate(geometries_um):
        print(f"  [{i + 1}/{len(geometries_um)}] {dev.key} W=L={size_um:g} µm", flush=True)
        with tempfile.TemporaryDirectory(prefix=f"cap_{dev.key}_") as tmp:
            c_v = run_moscap_geometry(
                ngspice=ngspice,
                models=models,
                pdk_root=pdk_root,
                dev=dev,
                w_um=size_um,
                l_um=size_um,
                biases=biases,
                work=Path(tmp),
            )
        c_cube.append(c_v)

    w_um = np.asarray(geometries_um, dtype=float)
    arrays = {
        "W": w_um,
        "L": w_um.copy(),
        "V": v_axis,
        "C": np.stack(c_cube, axis=0),
    }
    meta: dict[str, Any] = {
        "format": "ihp-cap-lut-v1",
        "device": dev.key,
        "subckt": dev.subckt,
        "corner": "moscap_tt",
        "corner_lib": "cornerMOSCAP.lib",
        "pdk": "ihp-sg13g2",
        "geometries_um": [{"w": s, "l": s} for s in geometries_um],
        "v_biases_v": list(biases),
        "extraction": "tran ramp C(V)=|i(Vg)|/deriv(v(g)) per xschem tran_moscap_*",
        "signals": {"C": "capacitance vs bias (F); shape (n_geo, n_v)"},
        "shape": "C is (n_geo, n_v); W,L,V are 1-D",
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"sg13_{dev.key}.npz"
    save_lut(out_path, arrays, meta)
    (out_dir / f"sg13_{dev.key}.meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"  wrote {out_path}", flush=True)
    return out_path


def quick_geometries_mim(dev: MimDevice) -> list[tuple[float, float]]:
    if dev.key == "cap_cmim":
        return [(7.0, 7.0), (20.0, 20.0)]
    return [(5.0, 5.0), (10.0, 10.0)]


def quick_geometries_moscap(dev: MoscapDevice) -> list[float]:
    return [1.0, 10.0]


def quick_biases_moscap() -> tuple[float, ...]:
    return (-1.0, 0.0, 1.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pdk-root",
        type=Path,
        default=None,
        help="PDK root (default: $PDK_ROOT)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "out",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Fewer geometries / bias points for smoke tests",
    )
    parser.add_argument(
        "--device",
        choices=["cmim", "cmomi", "moscap_n", "moscap_p", "all"],
        default="all",
    )
    parser.add_argument(
        "--no-f-sweep",
        action="store_true",
        help="Skip storing C vs frequency arrays for MIM caps",
    )
    args = parser.parse_args()

    pdk_root = args.pdk_root or Path(os.environ.get("PDK_ROOT", ""))
    models = _require_models(pdk_root)
    ngspice = _ngspice()
    out_dir = args.out_dir

    f_start, f_stop = (1e5, 1e9)
    n_pts = 50 if args.quick else 200
    store_f = not args.no_f_sweep

    run_cmim = args.device in ("cmim", "all")
    run_cmomi = args.device in ("cmomi", "all")
    run_mn = args.device in ("moscap_n", "all")
    run_mp = args.device in ("moscap_p", "all")

    if run_cmim:
        geoms = quick_geometries_mim(MIM_CMIM) if args.quick else list(MIM_CMIM.geometries_um)
        print(f"==> {MIM_CMIM.key} ({len(geoms)} geometries)", flush=True)
        characterize_mim(
            ngspice=ngspice,
            models=models,
            pdk_root=pdk_root,
            dev=MIM_CMIM,
            geometries=geoms,
            out_dir=out_dir,
            f_start=f_start,
            f_stop=f_stop,
            n_pts=n_pts,
            store_f_sweep=store_f,
        )

    if run_cmomi:
        geoms = quick_geometries_mim(MIM_CMOMI) if args.quick else list(MIM_CMOMI.geometries_um)
        print(f"==> {MIM_CMOMI.key} ({len(geoms)} geometries)", flush=True)
        characterize_mim(
            ngspice=ngspice,
            models=models,
            pdk_root=pdk_root,
            dev=MIM_CMOMI,
            geometries=geoms,
            out_dir=out_dir,
            f_start=f_start,
            f_stop=f_stop,
            n_pts=n_pts,
            store_f_sweep=store_f,
        )

    moscap_biases = quick_biases_moscap() if args.quick else MOSCAP_N.v_biases

    if run_mn:
        geoms = quick_geometries_moscap(MOSCAP_N) if args.quick else list(MOSCAP_N.geometries_um)
        print(f"==> {MOSCAP_N.key} ({len(geoms)} geometries)", flush=True)
        characterize_moscap(
            ngspice=ngspice,
            models=models,
            pdk_root=pdk_root,
            dev=MOSCAP_N,
            geometries_um=geoms,
            biases=moscap_biases,
            out_dir=out_dir,
        )

    if run_mp:
        geoms = quick_geometries_moscap(MOSCAP_P) if args.quick else list(MOSCAP_P.geometries_um)
        print(f"==> {MOSCAP_P.key} ({len(geoms)} geometries)", flush=True)
        characterize_moscap(
            ngspice=ngspice,
            models=models,
            pdk_root=pdk_root,
            dev=MOSCAP_P,
            geometries_um=geoms,
            biases=moscap_biases,
            out_dir=out_dir,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
