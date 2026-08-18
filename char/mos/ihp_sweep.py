"""IHP SG13G2 MOSFET characterization (pygmid-compatible LUTs).

Upstream pygmid's ngspice netlist hardcodes LV probe names. This runner
keeps the same LUT schema (dicts pickled for pygmid.Lookup) while supporting
LV/HV core and RF devices via ``rfmode``.
"""

from __future__ import annotations

import argparse
import pickle
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
from char.common.lut import matrange, parse_wrdata as _parse_wrdata, save_lut

OUTVARS = [
    "ID",
    "VT",
    "IGD",
    "IGS",
    "GM",
    "GMB",
    "GDS",
    "CGG",
    "CGS",
    "CSG",
    "CGD",
    "CDG",
    "CGB",
    "CDD",
    "CSS",
]

# ngspice device parameters written by wrdata (order matters)
DEVICE_PARAMS = [
    "ids",
    "vth",
    "igd",
    "igs",
    "gm",
    "gmb",
    "gds",
    "cgg",
    "cgs",
    "cgd",
    "cgb",
    "cdd",
    "cdg",
    "css",
    "csg",
    "cjd",
    "cjs",
]

# Map DEVICE_PARAMS -> OUTVARS contribution (same convention as pygmid)
N_MAP = [
    ("ids", [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
    ("vth", [0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
    ("igd", [0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
    ("igs", [0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
    ("gm", [0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
    ("gmb", [0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
    ("gds", [0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0]),
    ("cgg", [0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0]),
    ("cgs", [0, 0, 0, 0, 0, 0, 0, 0, -1, 0, 0, 0, 0, 0, 0]),
    ("csg", [0, 0, 0, 0, 0, 0, 0, 0, 0, -1, 0, 0, 0, 0, 0]),
    ("cgd", [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, -1, 0, 0, 0, 0]),
    ("cdg", [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, -1, 0, 0, 0]),
    ("cgb", [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, -1, 0, 0]),
    ("cdd", [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0]),
    ("css", [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1]),
]

P_SIGNS = {
    "ids": -1,
    "vth": -1,
    "igd": -1,
    "igs": -1,
    "gm": 1,
    "gmb": 1,
    "gds": 1,
    "cgg": 1,
    "cgs": 1,
    "cgd": 1,
    "cgb": 1,
    "cdd": 1,
    "cdg": 1,
    "css": 1,
    "csg": 1,
    "cjd": 1,
    "cjs": 1,
}


@dataclass(frozen=True)
class DeviceFamily:
    name: str
    label: str
    corner_lib: str  # absolute path
    corner_section: str
    model_n: str
    model_p: str
    probe_n: str  # e.g. nsg13_lv_nmos
    probe_p: str
    rfmode: int
    vdd: float
    lengths_um: tuple[float, ...]
    vgs_step: float
    vds_step: float
    vsb: tuple[float, ...]
    width_um: float = 1.0


def parse_wrdata(path: Path) -> dict[str, np.ndarray]:
    return _parse_wrdata(path, DEVICE_PARAMS)


def probe_exprs(inst: str, probe: str) -> list[str]:
    return [f"@n.{inst}.{probe}[{p}]" for p in DEVICE_PARAMS]


def write_netlist(
    path: Path,
    family: DeviceFamily,
    length_um: float,
    vsb: float,
    temp_c: float = 27.0,
) -> None:
    vgs = matrange(0.0, family.vgs_step, family.vdd)
    vds = matrange(0.0, family.vds_step, family.vdd)
    vgs_max, vgs_step = float(vgs[-1]), family.vgs_step
    vds_max, vds_step = float(vds[-1]), family.vds_step

    n_probes = probe_exprs("xm1", family.probe_n)
    p_probes = probe_exprs("xm2", family.probe_p)
    n_save = " ".join(n_probes)
    p_save = " ".join(p_probes)

    # PMOS swept with negative voltages (pygmid convention)
    content = f"""* IHP characterization netlist — {family.name}
.lib '{family.corner_lib}' {family.corner_section}
.param length={length_um}e-6
.param sb={vsb}
.param temp={temp_c}

Vgs_n gate_n 0 0
Vds_n drain_n 0 {family.vdd}
Vb_n bulk_n 0 {{-sb}}
Vgs_p gate_p 0 0
Vds_p drain_p 0 {-family.vdd}
Vb_p bulk_p 0 {{sb}}

XM1 drain_n gate_n 0 bulk_n {family.model_n} w={family.width_um}u l={{length}} ng=1 m=1 rfmode={family.rfmode}
XM2 drain_p gate_p 0 bulk_p {family.model_p} w={family.width_um}u l={{length}} ng=1 m=1 rfmode={family.rfmode}

.control
save {n_save}
dc Vgs_n 0 {vgs_max} {vgs_step} Vds_n 0 {vds_max} {vds_step}
wrdata mn.csv {n_save}
reset
save {p_save}
dc Vgs_p 0 {-vgs_max} {-vgs_step} Vds_p 0 {-vds_max} {-vds_step}
wrdata mp.csv {p_save}
.endc
.GLOBAL GND
.end
"""
    path.write_text(content)


def run_ngspice(netlist: Path, workdir: Path) -> None:
    result = subprocess.run(
        ["ngspice", "-b", str(netlist.name)],
        cwd=workdir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ngspice failed in {workdir}\n"
            f"stdout:\n{result.stdout[-2000:]}\n"
            f"stderr:\n{result.stderr[-2000:]}"
        )


def reshape_param(
    flat: np.ndarray, n_vgs: int, n_vds: int
) -> np.ndarray:
    # ngspice nested dc: outer VDS varies slowest in wrdata rows → (n_vds, n_vgs)
    return flat.reshape((n_vds, n_vgs)).T  # -> (n_vgs, n_vds)


def empty_lut(family: DeviceFamily, lengths: np.ndarray, vsbs: np.ndarray) -> dict:
    vgs = matrange(0.0, family.vgs_step, family.vdd)
    vds = matrange(0.0, family.vds_step, family.vdd)
    shape = (len(lengths), len(vgs), len(vds), len(vsbs))
    lut = {
        "INFO": family.label,
        "CORNER": "NOM",
        "TEMP": 300,
        "NFING": 1,
        "L": lengths.copy(),
        "W": family.width_um,
        "VGS": vgs,
        "VDS": vds,
        "VSB": vsbs.copy(),
    }
    for name in OUTVARS:
        lut[name] = np.zeros(shape, order="F")
    for name in ("STH", "SFL"):
        lut[name] = np.zeros(shape, order="F")
    return lut


def accumulate(
    lut: dict,
    i_l: int,
    j_vsb: int,
    params: dict[str, np.ndarray],
    polarity: str,
) -> None:
    n_vgs = len(lut["VGS"])
    n_vds = len(lut["VDS"])
    shaped = {k: reshape_param(v, n_vgs, n_vds) for k, v in params.items()}
    if polarity == "p":
        shaped = {k: P_SIGNS[k] * v for k, v in shaped.items()}

    for param, coeffs in N_MAP:
        values = shaped[param]
        for m, outvar in enumerate(OUTVARS):
            lut[outvar][i_l, :, :, j_vsb] += values * coeffs[m]


def characterize_family(
    family: DeviceFamily,
    out_dir: Path,
    keep_raw: bool = False,
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    lengths = np.asarray(family.lengths_um, dtype=float)
    vsbs = np.asarray(family.vsb, dtype=float)
    nch = empty_lut(family, lengths, vsbs)
    pch = empty_lut(family, lengths, vsbs)

    work_root = Path(tempfile.mkdtemp(prefix=f"ihp_char_{family.name}_"))
    try:
        for i_l, length in enumerate(lengths):
            for j_vsb, vsb in enumerate(vsbs):
                sim_dir = work_root / f"L{i_l}_VSB{j_vsb}"
                sim_dir.mkdir()
                netlist = sim_dir / "char.sp"
                write_netlist(netlist, family, float(length), float(vsb))
                print(
                    f"[{family.name}] L={length} µm  VSB={vsb} V  →  ngspice…",
                    flush=True,
                )
                run_ngspice(netlist, sim_dir)
                n_params = parse_wrdata(sim_dir / "mn.csv")
                p_params = parse_wrdata(sim_dir / "mp.csv")
                accumulate(nch, i_l, j_vsb, n_params, "n")
                accumulate(pch, i_l, j_vsb, p_params, "p")
                if keep_raw:
                    raw_dest = out_dir / "raw" / family.name / sim_dir.name
                    raw_dest.parent.mkdir(parents=True, exist_ok=True)
                    if raw_dest.exists():
                        shutil.rmtree(raw_dest)
                    shutil.copytree(sim_dir, raw_dest)
    finally:
        shutil.rmtree(work_root, ignore_errors=True)

    def _export(pol: str, lut: dict) -> Path:
        pkl_path = out_dir / f"{family.name}_{pol}.pkl"
        with pkl_path.open("wb") as f:
            pickle.dump(lut, f)
        arrays = {
            k: np.asarray(v)
            for k, v in lut.items()
            if isinstance(v, np.ndarray)
        }
        meta = {
            "device_class": "mos",
            "family": family.name,
            "polarity": pol,
            "label": family.label,
            "corner": lut.get("CORNER"),
            "temp_K": lut.get("TEMP"),
            "width_um": float(lut.get("W", family.width_um)),
            "nfing": int(lut.get("NFING", 1)),
            "rfmode": family.rfmode,
            "format": "pygmid_compatible",
        }
        save_lut(out_dir / f"{family.name}_{pol}.npz", arrays, meta)
        return pkl_path

    n_path = _export("n", nch)
    p_path = _export("p", pch)
    print(
        f"[{family.name}] wrote {n_path.name}/{p_path.name} (+ .npz)",
        flush=True,
    )
    return n_path, p_path


def default_families(pdk_root: Path) -> list[DeviceFamily]:
    models = pdk_root / "ihp-sg13g2" / "libs.tech" / "ngspice" / "models"
    lv = models / "cornerMOSlv.lib"
    hv = models / "cornerMOShv.lib"
    return [
        DeviceFamily(
            name="lv_core",
            label="SG13G2 LV 1.2V core CMOS (rfmode=0)",
            corner_lib=str(lv),
            corner_section="mos_tt",
            model_n="sg13_lv_nmos",
            model_p="sg13_lv_pmos",
            probe_n="nsg13_lv_nmos",
            probe_p="nsg13_lv_pmos",
            rfmode=0,
            vdd=1.2,
            lengths_um=(0.13, 0.18, 0.25, 0.5),
            vgs_step=0.05,
            vds_step=0.1,
            vsb=(0.0, 0.3, 0.6),
        ),
        DeviceFamily(
            name="lv_rf",
            label="SG13G2 LV 1.2V RF CMOS (rfmode=1)",
            corner_lib=str(lv),
            corner_section="mos_tt",
            model_n="sg13_lv_nmos",
            model_p="sg13_lv_pmos",
            probe_n="nsg13_lv_nmos",
            probe_p="nsg13_lv_pmos",
            rfmode=1,
            vdd=1.2,
            lengths_um=(0.13, 0.18, 0.25, 0.5),
            vgs_step=0.05,
            vds_step=0.1,
            vsb=(0.0, 0.3, 0.6),
        ),
        DeviceFamily(
            name="hv_core",
            label="SG13G2 HV 3.3V core CMOS (rfmode=0)",
            corner_lib=str(hv),
            corner_section="mos_tt",
            model_n="sg13_hv_nmos",
            model_p="sg13_hv_pmos",
            probe_n="nsg13_hv_nmos",
            probe_p="nsg13_hv_pmos",
            rfmode=0,
            vdd=3.3,
            lengths_um=(0.45, 0.5, 1.0, 2.0),
            vgs_step=0.1,
            vds_step=0.2,
            vsb=(0.0, 0.5, 1.0),
        ),
        DeviceFamily(
            name="hv_rf",
            label="SG13G2 HV 3.3V RF CMOS (rfmode=1)",
            corner_lib=str(hv),
            corner_section="mos_tt",
            model_n="sg13_hv_nmos",
            model_p="sg13_hv_pmos",
            probe_n="nsg13_hv_nmos",
            probe_p="nsg13_hv_pmos",
            rfmode=1,
            vdd=3.3,
            lengths_um=(0.45, 0.5, 1.0, 2.0),
            vgs_step=0.1,
            vds_step=0.2,
            vsb=(0.0, 0.5, 1.0),
        ),
    ]


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pdk-root",
        type=Path,
        default=Path.home() / ".local/share/ihp-eda/IHP-Open-PDK",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "out",
    )
    parser.add_argument(
        "--only",
        nargs="*",
        help="Optional subset of family names (lv_core lv_rf hv_core hv_rf)",
    )
    parser.add_argument("--keep-raw", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    families = default_families(args.pdk_root)
    if args.only:
        wanted = set(args.only)
        families = [f for f in families if f.name in wanted]
        missing = wanted - {f.name for f in families}
        if missing:
            raise SystemExit(f"Unknown families: {sorted(missing)}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    for family in families:
        characterize_family(family, args.out_dir, keep_raw=args.keep_raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
