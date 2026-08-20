#!/usr/bin/env python3
"""Batch-render IHP SG13G2 passive device layouts to PNG screenshots.

Copies or synthesizes GDS into ``out/layouts/gds/`` and writes PNGs to
``out/layouts/``. Uses KLayout batch mode (``klayout -b``) when available;
falls back to matplotlib + gdspy polygon plots.
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
from typing import Any, Callable

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

PNG_W, PNG_H = 1200, 900


@dataclass(frozen=True)
class LayoutSpec:
    stem: str
    title: str
    # kind: copy from PDK | synthesize MIM char | synthesize inductor
    kind: str
    pdk_rel: str | None = None
    synth_key: str | None = None


def _pdk_gds_root(pdk_root: Path) -> Path:
    return pdk_root / "ihp-sg13g2/libs.tech/klayout/tech/lvs/testing/testcases"


def _openems_workflow(pdk_root: Path) -> Path:
    return pdk_root / "ihp-sg13g2/libs.tech/openems/openems_ihp_sg13g2/workflow"


def _palace_workflow(pdk_root: Path) -> Path:
    return pdk_root / "ihp-sg13g2/libs.tech/palace/workflow"


def _synth_script(pdk_root: Path) -> Path:
    return (
        pdk_root
        / "ihp-sg13g2/libs.tech/palace/more_examples/inductor_synthesis_no_external_library"
        / "synthesize_ihp_inductor_v3.py"
    )


def layout_catalog(pdk_root: Path) -> list[LayoutSpec]:
    cap = _pdk_gds_root(pdk_root) / "unit/cap_devices/layout"
    ind = _pdk_gds_root(pdk_root) / "unit/ind_devices/layout"
    openems = _openems_workflow(pdk_root)
    palace = _palace_workflow(pdk_root)

    specs: list[LayoutSpec] = [
        LayoutSpec("cap_cmim", "MIM capacitor (PDK testcase)", "pdk", f"{cap}/cap_cmim.gds"),
        LayoutSpec("cap_cmomi", "Interdigitated MoM capacitor", "pdk", f"{cap}/cap_cmomi.gds"),
        LayoutSpec("cap_cmomf", "Finger MoM capacitor (cmomf)", "pdk", f"{cap}/cap_cmomf.gds"),
        LayoutSpec("rfcmim", "RF MIM capacitor", "pdk", f"{cap}/rfcmim.gds"),
        LayoutSpec("sg13_moscap_n", "NMOS MOSCAP", "pdk", f"{cap}/sg13_moscap_n.gds"),
        LayoutSpec("sg13_moscap_p", "PMOS MOSCAP", "pdk", f"{cap}/sg13_moscap_p.gds"),
        LayoutSpec("char_cap_cmim_7x7", "Educational MIM 7×7 µm", "synth_mim", synth_key="7x7"),
        LayoutSpec("char_cap_cmim_20x20", "Educational MIM 20×20 µm", "synth_mim", synth_key="20x20"),
        LayoutSpec("L_2n0_twoport", "Canonical 2 nH two-port inductor", "pdk", f"{openems}/L_2n0_twoport.gds"),
        LayoutSpec("inductor_1turn", "PDK 1-turn inductor", "pdk", f"{ind}/inductor.gds"),
        LayoutSpec("inductor_2turn_ct", "PDK 2-turn center-tap inductor", "pdk", f"{ind}/inductor3.gds"),
        LayoutSpec("ind_turn1_em", "Synthesized 1-turn octagon (EM)", "synth_ind", synth_key="turn1"),
        LayoutSpec("ind_turn2_em", "Synthesized 2-turn octagon (EM)", "synth_ind", synth_key="turn2"),
        LayoutSpec(
            "inductor_500pH",
            "500 pH inductor with ports (Palace workflow)",
            "pdk",
            f"{palace}/inductor_500pH_with_ports.gds",
        ),
    ]
    return specs


def _load_symmetric_octa_ihp(synth_script: Path) -> Callable[..., None]:
    """Geometry-only loader from synthesize_ihp_inductor_v3 (matches ihp_ind_em.py)."""
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
    exec("\n".join(kept), namespace)  # noqa: S102
    fn = namespace.get("symmetric_octa_IHP")
    if fn is None:
        raise RuntimeError(f"symmetric_octa_IHP not found in {synth_script}")
    return fn


def _synth_mim_gds(out_path: Path, size_um: float) -> None:
    """Draw a simple stacked MIM rectangle matching char LUT geometry."""
    import gdspy

    lib = gdspy.GdsLibrary(unit=1e-6, precision=1e-9)
    cell = lib.new_cell(f"char_cap_cmim_{int(size_um)}x{int(size_um)}")
    half = size_um / 2.0
    # Layers observed in PDK cap_cmim.gds (TopMetal stack + MIM markers).
    bottom = gdspy.Rectangle((-half, -half), (half, half), layer=126, datatype=0)
    mim_dielectric = gdspy.Rectangle(
        (-half + 0.5, -half + 0.5),
        (half - 0.5, half - 0.5),
        layer=67,
        datatype=0,
    )
    top = gdspy.Rectangle((-half + 1.0, -half + 1.0), (half - 1.0, half - 1.0), layer=129, datatype=0)
    label = gdspy.Rectangle((-half, -half - 2.0), (half, -half - 1.0), layer=36, datatype=0)
    cell.add(bottom)
    cell.add(mim_dielectric)
    cell.add(top)
    cell.add(label)
    lib.write_gds(str(out_path))


def _synth_inductor_gds(
    out_path: Path,
    *,
    symmetric_octa: Callable[..., None],
    turn_key: str,
) -> None:
    params = {
        "turn1": dict(N=1, D=120.0, w=3.0, s=3.0),
        "turn2": dict(N=2, D=150.0, w=3.0, s=3.0),
    }
    if turn_key not in params:
        raise ValueError(f"unknown inductor synth key: {turn_key}")
    p = params[turn_key]
    symmetric_octa(
        N=p["N"],
        D=p["D"],
        w=p["w"],
        s=p["s"],
        includeCenterTap=False,
        LBE=False,
        forEM=True,
        filename=str(out_path),
    )


def _resolve_gds(
    spec: LayoutSpec,
    *,
    pdk_root: Path,
    gds_dir: Path,
    symmetric_octa: Callable[..., None] | None,
) -> Path | None:
    out_gds = gds_dir / f"{spec.stem}.gds"
    gds_dir.mkdir(parents=True, exist_ok=True)

    if spec.kind == "pdk":
        if not spec.pdk_rel:
            return None
        src = Path(spec.pdk_rel)
        if not src.is_file():
            print(f"  [skip] missing PDK GDS: {src}", flush=True)
            return None
        shutil.copy2(src, out_gds)
        return out_gds

    if spec.kind == "synth_mim":
        size = 7.0 if spec.synth_key == "7x7" else 20.0
        _synth_mim_gds(out_gds, size)
        return out_gds

    if spec.kind == "synth_ind":
        if symmetric_octa is None:
            print(f"  [skip] inductor synth unavailable for {spec.stem}", flush=True)
            return None
        _synth_inductor_gds(out_gds, symmetric_octa=symmetric_octa, turn_key=spec.synth_key or "")
        return out_gds

    return None


def _klayout_bin() -> str | None:
    return shutil.which("klayout")


def _render_klayout_batch(
  *,
    jobs: list[tuple[Path, Path, str]],
    layer_props: Path | None,
    width: int,
    height: int,
) -> dict[Path, bool]:
    """Render PNGs via one klayout -b invocation. Returns {png_path: ok}."""
    if not jobs:
        return {}

    klayout = _klayout_bin()
    if not klayout:
        return {png: False for _, png, _ in jobs}

    lyp = str(layer_props) if layer_props and layer_props.is_file() else ""

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tf:
        script_path = Path(tf.name)
        tf.write("import pya\n\n")
        tf.write(f"WIDTH = {width}\n")
        tf.write(f"HEIGHT = {height}\n")
        tf.write(f'LYP = r"""{lyp}"""\n\n')
        tf.write("def render_one(gds_path, png_path, title):\n")
        tf.write("    ly = pya.Layout()\n")
        tf.write("    ly.read(gds_path)\n")
        tf.write("    top = ly.top_cell() or ly.cell(0)\n")
        tf.write("    lv = pya.LayoutView()\n")
        tf.write("    if LYP:\n")
        tf.write("        lv.load_layer_props(LYP)\n")
        tf.write("    lv.show_layout(ly, False)\n")
        tf.write("    lv.select_cell(top.cell_index(), 0)\n")
        tf.write("    lv.max_hier()\n")
        tf.write("    lv.zoom_fit()\n")
        tf.write("    lv.save_image(png_path, WIDTH, HEIGHT)\n")
        tf.write('    print(f"klayout: {png_path}")\n\n')
        tf.write("JOBS = [\n")
        for gds, png, title in jobs:
            tf.write(f'    (r"""{gds}""", r"""{png}""", r"""{title}"""),\n')
        tf.write("]\n\n")
        tf.write("for gds, png, title in JOBS:\n")
        tf.write("    render_one(gds, png, title)\n")

    try:
        proc = subprocess.run(
            [klayout, "-b", "-r", str(script_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            print(f"  [warn] klayout batch failed ({proc.returncode}):\n{proc.stderr}", flush=True)
    finally:
        script_path.unlink(missing_ok=True)

    return {png: png.is_file() and png.stat().st_size > 0 for _, png, _ in jobs}


def _layer_color(layer: int) -> tuple[float, float, float]:
    palette = {
        8: (0.2, 0.2, 0.8),
        36: (0.5, 0.5, 0.5),
        67: (0.9, 0.7, 0.2),
        126: (0.85, 0.45, 0.1),
        129: (0.1, 0.65, 0.35),
        134: (0.6, 0.2, 0.7),
        201: (0.9, 0.1, 0.1),
        202: (0.1, 0.1, 0.9),
        210: (0.3, 0.3, 0.3),
    }
    if layer in palette:
        return palette[layer]
    hue = (layer * 37) % 360
    return (
        0.4 + 0.4 * ((hue // 60) % 2),
        0.3 + 0.4 * ((hue // 120) % 2),
        0.3 + 0.4 * ((hue // 180) % 2),
    )


def _render_matplotlib(gds_path: Path, png_path: Path, title: str, *, width: int, height: int) -> bool:
    """Fallback: polygon fill plot via gdspy + matplotlib."""
    import gdspy
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import PatchCollection
    from matplotlib.patches import Polygon as MplPolygon

    lib = gdspy.GdsLibrary(infile=str(gds_path))
    top_cells = lib.top_level()
    if not top_cells:
        return False

    patches: list[MplPolygon] = []
    colors: list[tuple[float, float, float]] = []
    bbox = [float("inf"), float("inf"), float("-inf"), float("-inf")]

    for cell in top_cells:
        for poly in cell.polygons:
            layer = int(poly.layers[0])
            for pts in poly.polygons:
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                bbox[0] = min(bbox[0], min(xs))
                bbox[1] = min(bbox[1], min(ys))
                bbox[2] = max(bbox[2], max(xs))
                bbox[3] = max(bbox[3], max(ys))
                patches.append(MplPolygon(list(zip(xs, ys, strict=False)), closed=True))
                colors.append(_layer_color(layer))

    if not patches:
        return False

    dpi = 100
    fig_w, fig_h = width / dpi, height / dpi
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=dpi)
    collection = PatchCollection(patches, facecolor=colors, edgecolor="black", linewidth=0.2, alpha=0.85)
    ax.add_collection(collection)
    pad_x = max((bbox[2] - bbox[0]) * 0.05, 1.0)
    pad_y = max((bbox[3] - bbox[1]) * 0.05, 1.0)
    ax.set_xlim(bbox[0] - pad_x, bbox[2] + pad_x)
    ax.set_ylim(bbox[1] - pad_y, bbox[3] + pad_y)
    ax.set_aspect("equal")
    ax.set_title(title, fontsize=11)
    ax.set_xlabel("µm")
    ax.set_ylabel("µm")
    fig.tight_layout()
    png_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png_path, dpi=dpi)
    plt.close(fig)
    return png_path.is_file() and png_path.stat().st_size > 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "out" / "layouts",
        help="Output root (PNG here, GDS in gds/ subdir)",
    )
    p.add_argument("--pdk-root", type=Path, default=None, help="PDK root (default: PDK_ROOT env)")
    p.add_argument("--width", type=int, default=PNG_W)
    p.add_argument("--height", type=int, default=PNG_H)
    p.add_argument("--force", action="store_true", help="Re-render even if PNG exists")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    pdk_root = args.pdk_root or Path(os.environ.get("PDK_ROOT", ""))
    if not pdk_root.is_dir():
        raise SystemExit("PDK_ROOT is not set or invalid. Source ihp-eda env.sh first.")

    out_dir: Path = args.out_dir
    gds_dir = out_dir / "gds"
    out_dir.mkdir(parents=True, exist_ok=True)

    layer_props = pdk_root / "ihp-sg13g2/libs.tech/klayout/tech/sg13g2.lyp"

    symmetric_octa: Callable[..., None] | None = None
    synth_script = _synth_script(pdk_root)
    if synth_script.is_file():
        try:
            symmetric_octa = _load_symmetric_octa_ihp(synth_script)
        except Exception as exc:
            print(f"  [warn] could not load inductor synth: {exc}", flush=True)

    specs = layout_catalog(pdk_root)
    manifest: list[dict[str, Any]] = []
    render_jobs: list[tuple[Path, Path, str]] = []

    print(f"Layout render → {out_dir}", flush=True)
    for spec in specs:
        gds_path = _resolve_gds(spec, pdk_root=pdk_root, gds_dir=gds_dir, symmetric_octa=symmetric_octa)
        if gds_path is None:
            manifest.append({"stem": spec.stem, "status": "skipped", "reason": "no GDS"})
            continue

        png_path = out_dir / f"{spec.stem}.png"
        if png_path.is_file() and not args.force and png_path.stat().st_size > 0:
            print(f"  exists {png_path.name}", flush=True)
            manifest.append(
                {
                    "stem": spec.stem,
                    "title": spec.title,
                    "gds": str(gds_path),
                    "png": str(png_path),
                    "png_bytes": png_path.stat().st_size,
                    "status": "cached",
                }
            )
            continue

        render_jobs.append((gds_path.resolve(), png_path.resolve(), spec.title))

    klayout_ok = _render_klayout_batch(
        jobs=render_jobs,
        layer_props=layer_props,
        width=args.width,
        height=args.height,
    )

    for gds_path, png_path, title in render_jobs:
        stem = png_path.stem
        ok = klayout_ok.get(png_path, False)
        if not ok:
            print(f"  matplotlib fallback: {stem}", flush=True)
            ok = _render_matplotlib(
                gds_path,
                png_path,
                title,
                width=args.width,
                height=args.height,
            )
        status = "ok" if ok else "failed"
        entry: dict[str, Any] = {
            "stem": stem,
            "title": title,
            "gds": str(gds_path),
            "png": str(png_path),
            "status": status,
        }
        if ok:
            entry["png_bytes"] = png_path.stat().st_size
            print(f"  wrote {png_path.name} ({entry['png_bytes']} bytes)", flush=True)
        else:
            print(f"  [fail] {stem}", flush=True)
        manifest.append(entry)

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Manifest: {manifest_path}", flush=True)

    failed = sum(1 for m in manifest if m.get("status") == "failed")
  # Include cached + ok as success
    produced = [m for m in manifest if m.get("png_bytes", 0) > 0 or m.get("status") in ("ok", "cached")]
    print(f"Done: {len(produced)} PNG(s), {failed} failed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
