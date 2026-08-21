#!/usr/bin/env python3
"""IHP SG13G2 inductor EM characterization via openEMS → portable .npz LUTs.

Cases:
  - l2n0   : canonical smoke GDS (L_2n0_twoport.gds)
  - turn1  : 1-turn synthesized octagon (N=1, D≈120 µm, w=3, s=3, TopMetal2)
  - turn2  : 2-turn synthesized octagon (N=2, D≈150 µm, w=3, s=3, TopMetal1)

Adapted from PDK ``run_inductor_2port.py``; geometry synthesis from
``synthesize_ihp_inductor_v3.symmetric_octa_IHP`` (forEM=True).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from char.common.lut import load_lut, save_lut  # noqa: E402
from char.passive.ind_pimodel import (  # noqa: E402
    pi_model_from_s2p,
    pi_model_summary,
    refresh_sparam_luts,
    sparam_arrays_from_s2p,
)
from char.passive.ind_validate import validate_ind_lut  # noqa: E402


@dataclass(frozen=True)
class IndCase:
    key: str
    # geometry meta (None = canonical smoke, not synthesized)
    nr_r: int | None
    w_um: float | None
    s_um: float | None
    d_um: float | None
    from_layer: str
    to_layer: str
    gds_name: str
    synthesize: bool = False
    synth_n: int | None = None
    synth_d: float | None = None
    synth_w: float | None = None
    synth_s: float | None = None
    fstop_hz: float = 30e9


CASES: dict[str, IndCase] = {
    "l2n0": IndCase(
        key="l2n0",
        nr_r=2,
        w_um=3.0,
        s_um=3.0,
        d_um=114.0,
        from_layer="SUBGND",
        to_layer="TopMetal1",
        gds_name="L_2n0_twoport.gds",
        synthesize=False,
    ),
    "turn1": IndCase(
        key="turn1",
        nr_r=1,
        w_um=3.0,
        s_um=3.0,
        d_um=120.0,
        # Match L_2n0 via-port convention: SUBGND → top metal (synthesize adds Metal1
        # frame; we also inject a SUBGND polygon for openEMS).
        from_layer="SUBGND",
        to_layer="TopMetal2",
        gds_name="ind_turn1_em.gds",
        synthesize=True,
        synth_n=1,
        synth_d=120.0,
        synth_w=3.0,
        synth_s=3.0,
    ),
    "turn2": IndCase(
        key="turn2",
        nr_r=2,
        w_um=3.0,
        s_um=3.0,
        d_um=150.0,
        from_layer="SUBGND",
        to_layer="TopMetal1",
        gds_name="ind_turn2_em.gds",
        synthesize=True,
        synth_n=2,
        synth_d=150.0,
        synth_w=3.0,
        synth_s=3.0,
    ),
    # Small 1-turn octagons for CTLE shunt-peaking (40–120 pH target band).
    # Same SUBGND → TopMetal2 port convention as turn1 (known-good de-embedding).
    "turn1_d40": IndCase(
        key="turn1_d40",
        nr_r=1,
        w_um=4.0,
        s_um=2.1,
        d_um=40.0,
        from_layer="SUBGND",
        to_layer="TopMetal2",
        gds_name="ind_turn1_d40_em.gds",
        synthesize=True,
        synth_n=1,
        synth_d=40.0,
        synth_w=4.0,
        synth_s=2.1,
        fstop_hz=100e9,
    ),
    "turn1_d60": IndCase(
        key="turn1_d60",
        nr_r=1,
        w_um=4.0,
        s_um=2.1,
        d_um=60.0,
        from_layer="SUBGND",
        to_layer="TopMetal2",
        gds_name="ind_turn1_d60_em.gds",
        synthesize=True,
        synth_n=1,
        synth_d=60.0,
        synth_w=4.0,
        synth_s=2.1,
        fstop_hz=100e9,
    ),
    "turn1_d80": IndCase(
        key="turn1_d80",
        nr_r=1,
        w_um=4.0,
        s_um=2.1,
        d_um=80.0,
        from_layer="SUBGND",
        to_layer="TopMetal2",
        gds_name="ind_turn1_d80_em.gds",
        synthesize=True,
        synth_n=1,
        synth_d=80.0,
        synth_w=4.0,
        synth_s=2.1,
        fstop_hz=100e9,
    ),
}


def _pdk_paths(pdk_root: Path) -> tuple[Path, Path, Path]:
    workflow = (
        pdk_root
        / "ihp-sg13g2/libs.tech/openems/openems_ihp_sg13g2/workflow"
    )
    synth_dir = (
        pdk_root
        / "ihp-sg13g2/libs.tech/palace/more_examples/inductor_synthesis_no_external_library"
    )
    synth_script = synth_dir / "synthesize_ihp_inductor_v3.py"
    if not workflow.is_dir():
        raise SystemExit(f"Missing openEMS workflow under {workflow}")
    if not synth_script.is_file():
        raise SystemExit(f"Missing synthesize script {synth_script}")
    return workflow, synth_dir, synth_script


def _load_symmetric_octa_ihp(synth_script: Path) -> Callable[..., None]:
    """Load geometry-only code from synthesize_ihp_inductor_v3 (no gds2palace)."""
    lines = synth_script.read_text().splitlines()
    kept: list[str] = []
    skip_block = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("from gds2palace"):
            continue
        if stripped.startswith("import skrf") or stripped.startswith("from matplotlib"):
            continue
        if stripped.startswith("# RUN CONTROLS"):
            skip_block = True
            continue
        if stripped.startswith("# ==================================== INDUCTOR LAYOUT CODE"):
            skip_block = False
        if skip_block:
            continue
        if stripped.startswith("materials_list,") and "stackup_reader" in stripped:
            continue
        if line.startswith("# ==================================== SIMULATION FLOW"):
            break
        kept.append(line)
    namespace: dict[str, Any] = {"__builtins__": __builtins__}
    code = "\n".join(kept)
    exec(code, namespace)  # noqa: S102
    fn = namespace.get("symmetric_octa_IHP")
    if fn is None:
        raise RuntimeError(f"symmetric_octa_IHP not found in {synth_script}")
    return fn


def _check_openems() -> tuple[bool, bool, str | None]:
    """Return (python_ok, binary_ok, reason_if_missing)."""
    py_ok = False
    try:
        import openEMS  # noqa: F401

        py_ok = True
    except ImportError:
        pass
    bin_ok = shutil.which("openEMS") is not None
    if py_ok and bin_ok:
        return True, True, None
    reasons: list[str] = []
    if not py_ok:
        reasons.append("openEMS Python bindings not importable")
    if not bin_ok:
        reasons.append("openEMS binary not on PATH")
    return py_ok, bin_ok, "; ".join(reasons)


def _freq_grid(coarse: bool, fstop_hz: float) -> tuple[float, float, int]:
    fstart = 0.0
    numfreq = 51 if coarse else 401
    return fstart, fstop_hz, numfreq


def _prepare_workflow_modules(workflow: Path) -> None:
    modules = workflow / "modules"
    if str(modules) not in sys.path:
        sys.path.insert(0, str(modules))
    if str(workflow) not in sys.path:
        sys.path.insert(0, str(workflow))


def _ensure_gds(
    case: IndCase,
    *,
    workflow: Path,
    work_dir: Path,
    symmetric_octa: Callable[..., None] | None,
) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    gds_path = work_dir / case.gds_name
    if case.synthesize:
        if symmetric_octa is None:
            raise RuntimeError("symmetric_octa_IHP loader required for synthesized cases")
        symmetric_octa(
            N=case.synth_n,
            D=case.synth_d,
            w=case.synth_w,
            s=case.synth_s,
            includeCenterTap=False,
            LBE=False,
            forEM=True,
            filename=str(gds_path),
        )
        return gds_path
    src = workflow / case.gds_name
    if not src.is_file():
        raise FileNotFoundError(f"Canonical GDS not found: {src}")
    shutil.copy2(src, gds_path)
    return gds_path


def _maybe_add_subgnd(gds_path: Path, margin_um: float = 400.0) -> bool:
    """Add SUBGND (210/0) box if GDS has no layer 210 geometry."""
    import gdspy

    lib = gdspy.GdsLibrary(infile=str(gds_path))
    has_subgnd = False
    bbox: list[float] = [np.inf, np.inf, -np.inf, -np.inf]
    for cell in lib.cells.values():
        for poly in cell.polygons:
            layer = poly.layers[0]
            if layer == 210:
                has_subgnd = True
            if layer in (8, 126, 134, 201, 202):
                xs, ys = zip(*poly.polygons[0], strict=False)
                bbox[0] = min(bbox[0], min(xs))
                bbox[1] = min(bbox[1], min(ys))
                bbox[2] = max(bbox[2], max(xs))
                bbox[3] = max(bbox[3], max(ys))
    if has_subgnd or not np.isfinite(bbox[0]):
        return False
    cell = lib.top_level()[0] if lib.top_level() else lib.new_cell("SUBGND_FILL")
    m = margin_um
    rect = gdspy.Rectangle(
        (bbox[0] - m, bbox[1] - m),
        (bbox[2] + m, bbox[3] + m),
        layer=210,
        datatype=0,
    )
    cell.add(rect)
    lib.write_gds(str(gds_path))
    return True


def _run_openems_case(
    case: IndCase,
    *,
    workflow: Path,
    work_dir: Path,
    gds_path: Path,
    preview_only: bool,
    coarse: bool,
    fstop_hz: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    """Run mesh preview or full FDTD; return FREQ, Ldiff, Qdiff."""
    _prepare_workflow_modules(workflow)

    import modules.util_gds_reader as gds_reader  # type: ignore[import-untyped]
    import modules.util_meshlines as util_meshlines  # type: ignore[import-untyped]
    import modules.util_simulation_setup as simulation_setup  # type: ignore[import-untyped]
    import modules.util_stackup_reader as stackup_reader  # type: ignore[import-untyped]
    import modules.util_utilities as utilities  # type: ignore[import-untyped]
    from openEMS import openEMS  # type: ignore[import-untyped]

    xml_src = workflow / "SG13G2.xml"
    xml_path = work_dir / "SG13G2.xml"
    shutil.copy2(xml_src, xml_path)

    unit = 1e-6
    margin = 200.0
    fstart, fstop, numfreq = _freq_grid(coarse, fstop_hz)
    refined_cellsize = 2.0 if coarse else 1.0
    cells_per_wavelength = 10
    energy_limit = -50.0
    boundaries = ["PEC", "PEC", "PEC", "PEC", "PEC", "PEC"]
    merge_polygon_size = 1.0
    preprocess_gds = False

    materials_list, dielectrics_list, metals_list = stackup_reader.read_substrate(str(xml_path))

    simulation_ports = simulation_setup.all_simulation_ports()
    simulation_ports.add_port(
        simulation_setup.simulation_port(
            portnumber=1,
            voltage=1,
            port_Z0=50,
            source_layernum=201,
            from_layername=case.from_layer,
            to_layername=case.to_layer,
            direction="z",
        )
    )
    simulation_ports.add_port(
        simulation_setup.simulation_port(
            portnumber=2,
            voltage=1,
            port_Z0=50,
            source_layernum=202,
            from_layername=case.from_layer,
            to_layername=case.to_layer,
            direction="z",
        )
    )

    layernumbers = metals_list.getlayernumbers()
    layernumbers.extend(simulation_ports.portlayers)
    allpolygons = gds_reader.read_gds(
        str(gds_path),
        layernumbers,
        purposelist=[0],
        metals_list=metals_list,
        preprocess=preprocess_gds,
        merge_polygon_size=merge_polygon_size,
    )

    wavelength_air = 3e8 / fstop / unit
    max_cellsize = wavelength_air / (np.sqrt(materials_list.eps_max) * cells_per_wavelength)

    model_basename = f"sg13_ind_{case.key}"
    # Absolute paths required: openEMS.Run chdirs then compares Path.resolve().
    sim_path = (work_dir / f"{model_basename}_data").resolve()
    sim_path.mkdir(parents=True, exist_ok=True)

    run_log: dict[str, Any] = {
        "preview_only": preview_only,
        "coarse": coarse,
        "fstart_hz": fstart,
        "fstop_hz": fstop,
        "refined_cellsize_um": refined_cellsize,
        "cells_per_wavelength": cells_per_wavelength,
        "numfreq": numfreq,
        "sim_path": str(sim_path),
    }

    cwd0 = Path.cwd()
    try:
        for excite_ports in [[1], [2]]:
            os.chdir(cwd0)
            fdtd = openEMS(EndCriteria=np.exp(energy_limit / 10 * np.log(10)))
            fdtd.SetGaussExcite((fstart + fstop) / 2, (fstop - fstart) / 2)
            fdtd.SetBoundaryCond(boundaries)
            fdtd = simulation_setup.setupSimulation(
                excite_ports,
                simulation_ports,
                fdtd,
                materials_list,
                dielectrics_list,
                metals_list,
                allpolygons,
                max_cellsize,
                refined_cellsize,
                margin,
                unit,
                xy_mesh_function=util_meshlines.create_xy_mesh_from_polygons,
            )
            excitation_path = Path(
                utilities.get_excitation_path(str(sim_path), excite_ports)
            ).resolve()
            excitation_path.mkdir(parents=True, exist_ok=True)
            csx_file = excitation_path / (model_basename + ".xml")
            csx = fdtd.GetCSX()
            csx.Write2XML(str(csx_file))
            run_log[f"csx_port{excite_ports[0]}"] = str(csx_file)
            if not preview_only:
                print(f"  FDTD excitation {excite_ports} → {excitation_path}", flush=True)
                fdtd.Run(str(excitation_path), verbose=1)
    finally:
        os.chdir(cwd0)

    f = np.linspace(fstart, fstop, numfreq)
    if preview_only:
        return f, np.full(numfreq, np.nan), np.full(numfreq, np.nan), run_log

    with np.errstate(divide="ignore", invalid="ignore"):
        z11 = utilities.calculate_Zij_2port(1, 1, f, str(sim_path), simulation_ports)
        z21 = utilities.calculate_Zij_2port(2, 1, f, str(sim_path), simulation_ports)
        z12 = utilities.calculate_Zij_2port(1, 2, f, str(sim_path), simulation_ports)
        z22 = utilities.calculate_Zij_2port(2, 2, f, str(sim_path), simulation_ports)
        zdiff = z11 - z12 - z21 + z22
        omega = 2 * np.pi * f
        qdiff = np.where(zdiff.real != 0, zdiff.imag / zdiff.real, np.nan)
        ldiff = np.where(f > 0, zdiff.imag / omega, np.nan)
    idx10 = int(np.argmin(np.abs(f - 10e9)))
    run_log["L_at_10GHz_nH"] = float(ldiff[idx10] * 1e9) if np.isfinite(ldiff[idx10]) else None
    run_log["peak_Q"] = float(np.nanmax(qdiff)) if np.any(np.isfinite(qdiff)) else None
    s2p = sim_path / (model_basename + ".s2p")
    try:
        s11 = utilities.calculate_Sij(1, 1, f, str(sim_path), simulation_ports)
        s21 = utilities.calculate_Sij(2, 1, f, str(sim_path), simulation_ports)
        s12 = utilities.calculate_Sij(1, 2, f, str(sim_path), simulation_ports)
        s22 = utilities.calculate_Sij(2, 2, f, str(sim_path), simulation_ports)
        utilities.write_snp(np.array([[s11, s21], [s12, s22]]), f, str(s2p))
        run_log["s2p"] = str(s2p)
    except Exception as exc:
        run_log["s2p_error"] = str(exc)
    return f, ldiff, qdiff, run_log


def _jsonable(obj: Any) -> Any:
    """Convert numpy scalars/arrays into plain Python for JSON meta."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, Path):
        return str(obj)
    return obj


def _pimodel_arrays_from_s2p(s2p_path: Path) -> tuple[dict[str, np.ndarray], dict[str, float]]:
    """Extract pi-model frequency arrays + band-mean scalars from a Touchstone .s2p."""
    pimodel = pi_model_from_s2p(s2p_path)
    summary = pi_model_summary(pimodel)
    arrays = {
        "L_PI": pimodel["L"].astype(float),
        "R_SERIES": pimodel["R_SERIES"].astype(float),
        "C_PORT1": pimodel["C_PORT1"].astype(float),
        "C_PORT2": pimodel["C_PORT2"].astype(float),
        "G_PORT1": pimodel["G_PORT1"].astype(float),
        "G_PORT2": pimodel["G_PORT2"].astype(float),
    }
    return arrays, summary


def _merge_pimodel_into_lut(
    out_path: Path,
    s2p_path: Path | None,
) -> bool:
    """Attach pi-model arrays from .s2p into an existing committed .npz (no openEMS re-run)."""
    if s2p_path is None or not Path(s2p_path).is_file():
        return False
    arrays, meta = load_lut(out_path)
    pimodel = pi_model_from_s2p(Path(s2p_path))
    pi_summary = pi_model_summary(pimodel)
    freq = np.asarray(arrays["FREQ"], dtype=float)
    pi_freq = np.asarray(pimodel["FREQ"], dtype=float)
    pi_arrays: dict[str, np.ndarray] = {}
    key_map = {
        "L_PI": "L",
        "R_SERIES": "R_SERIES",
        "C_PORT1": "C_PORT1",
        "C_PORT2": "C_PORT2",
        "G_PORT1": "G_PORT1",
        "G_PORT2": "G_PORT2",
    }
    for dst, src in key_map.items():
        src_arr = np.asarray(pimodel[src], dtype=float)
        if len(pi_freq) == len(freq) and np.allclose(pi_freq, freq):
            pi_arrays[dst] = src_arr
        else:
            pi_arrays[dst] = np.interp(freq, pi_freq, src_arr)
    arrays.update(pi_arrays)
    meta["pimodel"] = _jsonable(pi_summary)
    meta["pimodel_s2p"] = str(s2p_path)
    axes = meta.get("axes") or {}
    axes.update({
        "L_PI": "2-port pi-model series inductance imag(Z12)/omega (H)",
        "R_SERIES": "2-port pi-model series resistance real(Z12) (Ohm)",
        "C_PORT1": "pi-model port-1 shunt capacitance (F)",
        "C_PORT2": "pi-model port-2 shunt capacitance (F)",
        "G_PORT1": "pi-model port-1 shunt conductance (S)",
        "G_PORT2": "pi-model port-2 shunt conductance (S)",
    })
    meta["axes"] = axes
    save_lut(out_path, arrays, meta)
    (out_path.parent / f"{out_path.stem}.meta.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n"
    )
    print(f"  pimodel merged into {out_path.name} from {Path(s2p_path).name}", flush=True)
    return True


def refresh_pimodel_luts(out_dir: Path) -> int:
    """Re-extract pi-model fields from on-disk .s2p paths referenced in LUT meta."""
    updated = 0
    for npz_path in sorted(out_dir.glob("sg13_ind_*.npz")):
        _, meta = load_lut(npz_path)
        s2p = None
        run_log = meta.get("run_log") or {}
        if isinstance(run_log, dict) and run_log.get("s2p"):
            s2p = Path(run_log["s2p"])
        if s2p is None and meta.get("pimodel_s2p"):
            s2p = Path(meta["pimodel_s2p"])
        if s2p is None and meta.get("from_s2p"):
            s2p = out_dir.parent / meta["from_s2p"] if not Path(meta["from_s2p"]).is_absolute() else Path(meta["from_s2p"])
        if _merge_pimodel_into_lut(npz_path, s2p):
            updated += 1
    return updated


def _write_case_outputs(
    case: IndCase,
    *,
    out_dir: Path,
    gds_path: Path,
    freq: np.ndarray,
    l_series: np.ndarray,
    q_series: np.ndarray,
    meta_extra: dict[str, Any],
    em_ok: bool,
    skip_reason: str | None,
) -> Path:
    em_completed = bool(em_ok)
    valid, invalid_reason = validate_ind_lut(
        freq,
        l_series,
        q_series,
        em_completed=em_completed,
    )
    meta: dict[str, Any] = {
        "format": "ihp-ind-em-lut-v1",
        "device": f"sg13_ind_{case.key}",
        "case": case.key,
        "pdk": "ihp-sg13g2",
        "solver": "openEMS",
        "nr_r": case.nr_r,
        "w": case.w_um,
        "s": case.s_um,
        "d": case.d_um,
        "gds": str(gds_path),
        "from_layer": case.from_layer,
        "to_layer": case.to_layer,
        "fstop_hz": case.fstop_hz,
        "axes": {
            "FREQ": "frequency (Hz)",
            "L": "differential series inductance Ldiff (H)",
            "Q": "differential Q factor Qdiff",
            "L_PI": "2-port pi-model series inductance imag(Z12)/omega (H)",
            "R_SERIES": "2-port pi-model series resistance real(Z12) (Ohm)",
            "C_PORT1": "pi-model port-1 shunt capacitance (F)",
            "C_PORT2": "pi-model port-2 shunt capacitance (F)",
            "G_PORT1": "pi-model port-1 shunt conductance (S)",
            "G_PORT2": "pi-model port-2 shunt conductance (S)",
            "SP_FREQ": "S-parameter verification frequency (Hz)",
            "S11_RE": "EM S11 real part",
            "S11_IM": "EM S11 imaginary part",
            "S21_RE": "EM S21 real part",
            "S21_IM": "EM S21 imaginary part",
            "S22_RE": "EM S22 real part",
            "S22_IM": "EM S22 imaginary part",
        },
        "em_completed": em_completed,
        "valid": valid,
        "invalid_reason": invalid_reason,
    }
    if skip_reason:
        meta["skip_reason"] = skip_reason
    meta.update(_jsonable(meta_extra))

    arrays: dict[str, np.ndarray] = {
        "FREQ": freq.astype(float),
        "L": l_series.astype(float),
        "Q": q_series.astype(float),
    }
    s2p_path = None
    run_log = meta_extra.get("run_log") if isinstance(meta_extra.get("run_log"), dict) else {}
    if run_log and run_log.get("s2p"):
        s2p_path = Path(run_log["s2p"])
    if s2p_path is not None and s2p_path.is_file():
        try:
            pi_arrays, pi_summary = _pimodel_arrays_from_s2p(s2p_path)
            arrays.update(pi_arrays)
            meta["pimodel"] = _jsonable(pi_summary)
            meta["pimodel_s2p"] = str(s2p_path)
        except Exception as exc:
            meta["pimodel_error"] = str(exc)
        try:
            arrays.update(sparam_arrays_from_s2p(s2p_path))
            meta["sparam_s2p"] = str(s2p_path)
            meta["sparam_nfreq"] = int(len(arrays["SP_FREQ"]))
        except Exception as exc:
            meta["sparam_error"] = str(exc)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"sg13_ind_{case.key}.npz"
    save_lut(out_path, arrays, meta)
    (out_dir / f"sg13_ind_{case.key}.meta.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n"
    )
    print(f"  wrote {out_path}", flush=True)
    return out_path


def run_case(
    case: IndCase,
    *,
    pdk_root: Path,
    out_dir: Path,
    preview_only: bool,
    coarse: bool,
    symmetric_octa: Callable[..., None] | None,
) -> int:
    workflow, _, synth_script = _pdk_paths(pdk_root)
    em_work = out_dir / "em_work" / case.key
    em_work.mkdir(parents=True, exist_ok=True)

    py_ok, bin_ok, skip_reason = _check_openems()
    can_mesh = py_ok
    can_solve = py_ok and bin_ok and not preview_only

    if not py_ok and symmetric_octa is None and case.synthesize:
        symmetric_octa = _load_symmetric_octa_ihp(synth_script)

    gds_path = _ensure_gds(
        case,
        workflow=workflow,
        work_dir=em_work,
        symmetric_octa=symmetric_octa,
    )
    subgnd_added = _maybe_add_subgnd(gds_path)
    print(f"== {case.key}: gds={gds_path.name} subgnd_added={subgnd_added}", flush=True)

    fstart, fstop, numfreq = _freq_grid(coarse, case.fstop_hz)
    freq = np.linspace(fstart, fstop, numfreq)
    l_series = np.full(numfreq, np.nan)
    q_series = np.full(numfreq, np.nan)
    run_log: dict[str, Any] = {}

    exit_code = 0
    effective_skip = skip_reason

    if can_mesh:
        try:
            if can_solve:
                freq, l_series, q_series, run_log = _run_openems_case(
                    case,
                    workflow=workflow,
                    work_dir=em_work,
                    gds_path=gds_path,
                    preview_only=False,
                    coarse=coarse,
                    fstop_hz=case.fstop_hz,
                )
            else:
                freq, l_series, q_series, run_log = _run_openems_case(
                    case,
                    workflow=workflow,
                    work_dir=em_work,
                    gds_path=gds_path,
                    preview_only=True,
                    coarse=coarse,
                    fstop_hz=case.fstop_hz,
                )
                if not bin_ok and not preview_only:
                    exit_code = 2
                    effective_skip = skip_reason or "openEMS binary missing"
        except Exception as exc:
            print(f"  [ERROR] openEMS run failed: {exc}", flush=True)
            exit_code = 2
            effective_skip = str(exc)
    else:
        exit_code = 2
        effective_skip = skip_reason or "openEMS Python bindings missing"

    _write_case_outputs(
        case,
        out_dir=out_dir,
        gds_path=gds_path,
        freq=freq,
        l_series=l_series,
        q_series=q_series,
        meta_extra={"subgnd_added": subgnd_added, "run_log": run_log},
        em_ok=can_solve and exit_code == 0 and np.any(np.isfinite(l_series)),
        skip_reason=effective_skip if exit_code != 0 or preview_only else None,
    )
    return exit_code


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parent / "out")
    p.add_argument(
        "--preview-only",
        action="store_true",
        help="Build mesh / CSX only; no FDTD solve (default False)",
    )
    p.add_argument(
        "--cases",
        nargs="+",
        default=["all"],
        help=f"Cases to run (default: all). Known: {', '.join(sorted(CASES))}",
    )
    p.add_argument(
        "--coarse",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Coarse mesh + fewer frequency points (default True)",
    )
    p.add_argument(
        "--pdk-root",
        type=Path,
        default=None,
        help="PDK root (default: PDK_ROOT env)",
    )
    p.add_argument(
        "--refresh-pimodel",
        action="store_true",
        help="Re-extract pi-model arrays from existing .s2p into committed .npz LUTs",
    )
    p.add_argument(
        "--refresh-sparams",
        action="store_true",
        help="Re-extract downsampled S-parameter arrays from existing .s2p into .npz LUTs",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.refresh_pimodel:
        out_dir = args.out_dir
        n = refresh_pimodel_luts(out_dir)
        print(f"Refreshed pi-model in {n} LUT file(s)", flush=True)
        return 0 if n > 0 else 1

    if args.refresh_sparams:
        out_dir = args.out_dir
        n = refresh_sparam_luts(out_dir)
        print(f"Refreshed S-parameters in {n} LUT file(s)", flush=True)
        return 0 if n > 0 else 1

    pdk_root = args.pdk_root or Path(os.environ.get("PDK_ROOT", ""))
    if not pdk_root or not pdk_root.is_dir():
        raise SystemExit("PDK_ROOT is not set or invalid. Source ihp-eda env.sh first.")

    workflow, _, synth_script = _pdk_paths(pdk_root)
    os.environ.setdefault(
        "OPENEMS_WORKFLOW",
        str(workflow),
    )
    if "PYTHONPATH" in os.environ:
        if str(workflow) not in os.environ["PYTHONPATH"]:
            os.environ["PYTHONPATH"] = f"{workflow}:{os.environ['PYTHONPATH']}"
    else:
        os.environ["PYTHONPATH"] = str(workflow)

    selected = args.cases
    if "all" in selected:
        keys = list(CASES.keys())
    else:
        keys = list(dict.fromkeys(selected))
        unknown = [k for k in keys if k not in CASES]
        if unknown:
            raise SystemExit(f"Unknown case(s): {unknown}. Known: {sorted(CASES)}")

    symmetric_octa: Callable[..., None] | None = None
    if any(CASES[k].synthesize for k in keys):
        symmetric_octa = _load_symmetric_octa_ihp(synth_script)

    print(
        f"Inductor EM: cases={keys} preview_only={args.preview_only} coarse={args.coarse}",
        flush=True,
    )
    py_ok, bin_ok, reason = _check_openems()
    print(f"  openEMS python={py_ok} binary={bin_ok} ({reason or 'ok'})", flush=True)

    worst = 0
    for key in keys:
        rc = run_case(
            CASES[key],
            pdk_root=pdk_root,
            out_dir=args.out_dir,
            preview_only=args.preview_only,
            coarse=args.coarse,
            symmetric_octa=symmetric_octa,
        )
        worst = max(worst, rc)

    print("\nDone.", flush=True)
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
