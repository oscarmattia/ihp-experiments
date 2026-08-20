#!/usr/bin/env python3
"""Does the PDK LVS deck compare a hierarchical CDL against a hierarchical GDS?

Builds a throwaway top cell from two ``one_r`` sub-cells in series, writes the
CDL with :func:`layout.common.netlist.chain_subckt`, and runs the PDK deck. A
flat control netlist for the same layout must match; a single guard-ring block
proves ``X`` calls compare when the sub-cell survives extraction.

Nothing is written into the repo; GDS, CDL and LVS scratch files go to a temp
directory.

Usage:
    source ~/.local/share/ihp-eda/env.sh
    export QT_QPA_PLATFORM=offscreen
    python layout/debug_pex/probe_hier_lvs.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from layout.blocks.draw import snap
from layout.blocks.generators import _place, hbt_differential_pair
from layout.common.gds import stamp_net_labels, write_gds
from layout.common.netlist import BlockDef, chain_subckt, write_chain_cdl
from layout.common.paths import pdk_paths
from layout.common.pdk import new_layout, pya_module
from layout.common.sizing import read_params
from layout.common.spec import Terminal
from layout.devices.catalog import ctle_devices

SUB_CELL = "one_r"
TOP_CELL = "hier_pair_top"
GUARD_TOP = "hier_guard_top"


def _one_r_block(layout, spec) -> tuple[list[str], list, dict[str, Terminal]]:
    cell = layout.create_cell(SUB_CELL)
    placed = _place(layout, cell, spec.with_name("r1"), 0.0, 0.0)
    by_name = {t.name: t for t in placed}
    ports = {"n1": by_name["PLUS"], "n2": by_name["MINUS"]}
    stamp_net_labels(layout, cell, list(ports.values()), {"n1": "n1", "n2": "n2"})
    instances = [(spec.with_name("r1"), {"PLUS": "n1", "MINUS": "n2", "sub": "sub"})]
    return ["n1", "n2"], instances, ports


def build_pair_top(out: Path) -> tuple[Path, dict[str, BlockDef], list[tuple[str, str, dict[str, str]]], Path]:
    """Two ``one_r`` instances in series; ``mid`` abuts at a shared terminal."""
    params = read_params()
    spec = {s.name: s for s in ctle_devices(params)}["rppd_load"]

    layout = new_layout()
    port_nets, instances, ports = _one_r_block(layout, spec)
    pitch_y = snap(ports["n2"].center[1] - ports["n1"].center[1])

    pya = pya_module()
    sub_index = layout.cell_by_name(SUB_CELL)
    top = layout.create_cell(TOP_CELL)
    top.insert(pya.DCellInstArray(sub_index, pya.DTrans()))
    top.insert(pya.DCellInstArray(sub_index, pya.DTrans(pya.DVector(0.0, pitch_y))))

    stamp_net_labels(
        layout,
        top,
        [
            Terminal("a", ports["n1"].layer, ports["n1"].center, ports["n1"].width, ports["n1"].orientation),
            Terminal(
                "b",
                ports["n2"].layer,
                (ports["n2"].center[0], snap(ports["n2"].center[1] + pitch_y)),
                ports["n2"].width,
                ports["n2"].orientation,
            ),
            Terminal("mid", ports["n2"].layer, ports["n2"].center, ports["n2"].width, ports["n2"].orientation),
        ],
    )

    gds = write_gds(layout, top, out / f"{TOP_CELL}.gds", name=TOP_CELL)
    blocks = {SUB_CELL: (port_nets, instances)}
    chain_instances = [
        ("Xa", SUB_CELL, {"n1": "a", "n2": "mid"}),
        ("Xb", SUB_CELL, {"n1": "mid", "n2": "b"}),
    ]
    flat_cdl = out / f"{TOP_CELL}_flat.cdl"
    flat_cdl.write_text(
        "\n".join(
            [
                f"* flat control for {TOP_CELL}",
                f".SUBCKT {TOP_CELL} a b mid",
                "R1 a mid sub rppd m=1 l=1.4u w=5u",
                "R2 mid b sub rppd m=1 l=1.4u w=5u",
                f".ENDS {TOP_CELL}",
                "",
            ]
        )
    )
    return gds, blocks, chain_instances, flat_cdl


def build_guard_top(out: Path) -> tuple[Path, dict[str, BlockDef], list[tuple[str, str, dict[str, str]]]]:
    params = read_params()
    devices = {s.name: s for s in ctle_devices(params)}
    sub = hbt_differential_pair(devices["npn13G2_pair_device"])

    layout = new_layout()
    index = layout.add_cell(sub.name)
    layout.cell(index).copy_tree(sub.cell)
    top = layout.create_cell(GUARD_TOP)
    top.insert(pya_module().DCellInstArray(index, pya_module().DTrans()))

    labels = [
        Terminal(net, sub.ports[port].layer, sub.ports[port].center,
                 sub.ports[port].width, sub.ports[port].orientation)
        for port, net in (
            ("C_A", "outp"), ("B_A", "inp"), ("E_A", "e1"),
            ("C_B", "outn"), ("B_B", "inn"), ("E_B", "e2"),
        )
    ]
    stamp_net_labels(layout, top, labels)

    gds = write_gds(layout, top, out / f"{GUARD_TOP}.gds", name=GUARD_TOP)
    blocks = {sub.name: (sub.port_nets, sub.instances)}
    instances = [
        (
            "Xpair",
            sub.name,
            {
                "outp": "outp", "inp": "inp", "e1": "e1",
                "outn": "outn", "inn": "inn", "e2": "e2",
            },
        ),
    ]
    return gds, blocks, instances


def _extracted_text(extracted: str) -> str:
    return Path(extracted).read_text() if extracted else ""


def _has_subckt(text: str, name: str) -> bool:
    lower = text.lower()
    return f".subckt {name.lower()}" in lower


def _has_x_calls(text: str) -> bool:
    return any(
        line.strip().startswith("X") and not line.strip().startswith("*")
        for line in text.splitlines()
    )


def _run_lvs(
    label: str,
    gds: Path,
    cdl: Path,
    topcell: str | None,
    out: Path,
    *,
    disable_taps: bool = False,
    no_series_res: bool = False,
) -> dict:
    run_dir = out / f"lvs_{label}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)

    cmd = [
        os.environ.get("IHP_PYTHON", "python3"),
        str(pdk_paths().lvs_runner),
        f"--layout={gds}",
        f"--netlist={cdl}",
        f"--run_dir={run_dir}",
        "--run_mode=deep",
        "--implicit_nets=sub,sub!",
        "--ignore_top_ports_mismatch",
    ]
    if topcell:
        cmd.append(f"--topcell={topcell}")
    if disable_taps:
        cmd.append("--disable_tap_extraction")
    if no_series_res:
        cmd.append("--no_series_res")

    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    combined = f"{completed.stdout}\n{completed.stderr}"
    (run_dir / "lvs_run.log").write_text(f"$ {' '.join(cmd)}\n\n{combined}")

    clean = "Netlists match" in combined and "Netlists don't match" not in combined
    summary = next(
        (line.strip() for line in combined.splitlines()
         if any(marker in line for marker in ("Congratulations", "Netlists don't match", "ERROR :"))),
        "no verdict",
    )
    extracted = next(run_dir.rglob("*_extracted.cir"), None)
    extracted_path = str(extracted) if extracted else ""
    text = _extracted_text(extracted_path)

    return {
        "label": label,
        "topcell": topcell,
        "disable_tap_extraction": disable_taps,
        "no_series_res": no_series_res,
        "clean": clean,
        "summary": summary,
        "extracted": extracted_path,
        "extracted_has_subckt": _has_subckt(text, SUB_CELL) or _has_subckt(text, "hbt_diff_pair"),
        "extracted_has_x_calls": _has_x_calls(text),
        "extracted_flattened": bool(text) and not _has_x_calls(text),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("/tmp/debug_pex_hier_lvs"))
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    gds, blocks, instances, flat_cdl = build_pair_top(args.out)
    top_ports = ["a", "b", "mid"]
    hier_cdl = write_chain_cdl(
        TOP_CELL, top_ports, blocks, instances, args.out / f"{TOP_CELL}.cdl"
    )

    print(f"Wrote {gds.name}, {hier_cdl.name}, and flat control\n")
    cases = [
        _run_lvs(
            "pair_hier_cdl", gds, hier_cdl, TOP_CELL, args.out, no_series_res=True
        ),
        _run_lvs(
            "pair_flat_control", gds, flat_cdl, TOP_CELL, args.out, no_series_res=True
        ),
        _run_lvs("pair_no_topcell", gds, hier_cdl, None, args.out, no_series_res=True),
    ]

    bad_blocks = dict(blocks)
    bad_blocks["wrong_name"] = bad_blocks.pop(SUB_CELL)
    bad_instances = [
        (inst, "wrong_name" if sub == SUB_CELL else sub, mapping)
        for inst, sub, mapping in instances
    ]
    bad_cdl = args.out / f"{TOP_CELL}_bad_subckt.cdl"
    bad_cdl.write_text(chain_subckt(TOP_CELL, top_ports, bad_blocks, bad_instances))
    cases.append(
        _run_lvs("pair_bad_subckt", gds, bad_cdl, TOP_CELL, args.out, no_series_res=True)
    )

    gds_g, blocks_g, instances_g = build_guard_top(args.out)
    guard_ports = ["outp", "inp", "e1", "outn", "inn", "e2"]
    cdl_g = write_chain_cdl(
        GUARD_TOP, guard_ports, blocks_g, instances_g, args.out / f"{GUARD_TOP}.cdl"
    )
    cases.extend([
        _run_lvs("guard_taps_on", gds_g, cdl_g, GUARD_TOP, args.out),
        _run_lvs("guard_taps_off", gds_g, cdl_g, GUARD_TOP, args.out, disable_taps=True),
    ])

    report = args.out / "probe_hier_lvs.json"
    report.write_text(json.dumps(cases, indent=2) + "\n")

    print(f"{'label':<22}{'clean':>6}  summary")
    print("-" * 72)
    for case in cases:
        print(f"{case['label']:<22}{str(case['clean']):>6}  {case['summary'][:52]}")

    flat = next(c for c in cases if c["label"] == "pair_flat_control")
    guard = next(c for c in cases if c["label"] == "guard_taps_off")
    print("\nFindings:")
    print(f"  flat control (same layout, expanded CDL): {flat['clean']}")
    print(f"  hierarchical CDL on one-device subcells: {cases[0]['clean']} (layout flattens)")
    print(f"  guard-ring block hierarchical X call: {guard['clean']}")
    print(f"  topcell required: {cases[2]['clean'] is False}")
    print(f"  subckt name must match GDS cell: {cases[3]['clean'] is False}")
    print(f"  disable_tap_extraction on sub-block with ring: required ({cases[4]['clean']} vs {guard['clean']})")
    print(f"\nFull JSON: {report}")
    return 0 if flat["clean"] and guard["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
