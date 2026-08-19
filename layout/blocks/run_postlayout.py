#!/usr/bin/env python3
"""Build KLayout- and Magic-based post-layout CTLE DUT netlists from the sim view.

The sim view black-boxes ``inductor`` and ``cmomi`` so LVS compares only the
extractable core; the wrapper re-instantiates those devices from the schematic
compact models. Flow A uses KLayout LVS device lines; Flow B adds Magic
interconnect capacitance on the same device core.

Usage:
    python layout/blocks/run_postlayout.py
    python layout/blocks/run_postlayout.py --flow both --out layout/blocks/out/postlayout
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from layout.blocks.ctle_stage import CELL, PORT_NETS, build_ctle_stage
from layout.common.lvs import run_lvs
from layout.common.pex import run_magic_pex
from layout.common.postlayout import (
    extract_subckt_lines,
    magic_capacitor_lines,
    normalise_element,
    parse_subckt_ports,
    rename_schematic_instances,
    write_core,
)
from layout.common import simview

DEFAULT_OUT = Path(__file__).resolve().parent / "out" / "postlayout"

#: LVS writes these model names for black-boxed kinds; they are not in the core.
_BLACK_BOX_MODELS = frozenset({"inductor", "cap_cmomi"})

_PDK_LIB_HEADER = """\
.lib '{PDK_MODELS}/cornerHBT.lib' hbt_typ
.lib '{PDK_MODELS}/cornerMOSlv.lib' mos_tt
.lib '{PDK_MODELS}/cornerRES.lib' res_typ
.lib '{PDK_MODELS}/cornerCAP.lib' cap_typ
"""


def _prepend_pdk_libs(wrapper_path: Path) -> None:
    """Match ``ctle_pdk.cir``: corner libraries must load before the core devices."""
    text = wrapper_path.read_text()
    if "cornerHBT.lib" in text:
        return
    wrapper_path.write_text(_PDK_LIB_HEADER + "\n" + text)


def _inline_core(wrapper_path: Path, core_path: Path) -> None:
    """Paste core device lines into ``ctle_dut`` so ``save @q.xu1.xq1...`` resolves.

    The includable ``*_core.cir`` stays on disk for inspection; only the wrapper
    fed to ngspice is flattened one level.
    """
    core_devices = [
        line
        for line in core_path.read_text().splitlines()
        if line.strip()
        and not line.strip().startswith(("*", ".subckt", ".ends"))
    ]
    out: list[str] = []
    for line in wrapper_path.read_text().splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(".include") and core_path.name in stripped:
            continue
        if stripped.startswith("Xcore "):
            out.extend(core_devices)
            continue
        out.append(line)
    wrapper_path.write_text("\n".join(out) + "\n")


def _is_black_box_line(line: str) -> bool:
    tokens = line.split()
    params: list[str] = []
    body = tokens[1:]
    while body and "=" in body[-1]:
        body.pop()
    if not body:
        return False
    model = body[-1]
    return model.lower() in _BLACK_BOX_MODELS


def klayout_core_devices(extracted: Path) -> list[str]:
    """Device lines from the LVS deck, minus black-boxed kinds, normalised."""
    raw = extract_subckt_lines(extracted, CELL)
    kept = [line for line in raw if not _is_black_box_line(line)]
    return [rename_schematic_instances(normalise_element(line)) for line in kept]


def core_port_list(extracted: Path, instances: list, port_nets: list[str]) -> list[str]:
    """The core's interface: the schematic pins plus the promoted nets.

    Deliberately NOT the extracted netlist's own `.SUBCKT` line. That line lists
    only nets a *device* touches, and in the reduced view nothing touches `vdd` —
    both coils are black-boxed — so `vdd` is absent from it. Building the core from
    that list silently discarded 137 fF of supply capacitance, 134.78 fF of which is
    the power ring's coupling to `vss`, and left the wrapper connecting `ind_shunt`
    to a `vdd` that was not the layout's `vdd` at all.

    The extracted list is still checked against this one, so a labelled net that
    should have been promoted shows up as an error rather than as lost parasitics.
    """
    ports = simview.sim_port_nets(port_nets, instances)
    extracted_ports = set(parse_subckt_ports(extracted, CELL))
    missing = sorted(extracted_ports - set(ports))
    if missing:
        raise ValueError(
            f"the extracted core exposes {missing}, which the wrapper interface does "
            "not carry; promote them or they will be left floating"
        )
    return ports


def build_klayout_flow(
    extracted: Path,
    out_dir: Path,
    instances: list,
    port_nets: list[str],
) -> tuple[Path, dict]:
    """Flow A: KLayout devices only, wrapped for schematic interface."""
    core_ports = core_port_list(extracted, instances, port_nets)
    devices = klayout_core_devices(extracted)
    core_subckt = f"{CELL}_core"
    core_path = write_core(
        out_dir / f"{core_subckt}.cir",
        core_subckt,
        core_ports,
        devices,
    )
    wrapper_path = out_dir / "postlayout_klayout.cir"
    simview.write_wrapper(
        wrapper_path,
        CELL,
        port_nets,
        instances,
        core_netlist=str(core_path.resolve()),
        core_subckt=core_subckt,
        core_ports=core_ports,
    )
    _prepend_pdk_libs(wrapper_path)
    _inline_core(wrapper_path, core_path)
    summary = {
        "netlist": str(wrapper_path),
        "core_netlist": str(core_path),
        "device_count": len(devices),
        "parasitic_count": 0,
        "capacitance_kept_fF": 0.0,
        "capacitance_dropped_fF": 0.0,
        "gates": {},
    }
    return wrapper_path, summary


def build_magic_flow(
    extracted: Path,
    pex_netlist: Path,
    out_dir: Path,
    instances: list,
    port_nets: list[str],
    pex_physical: bool,
) -> tuple[Path | None, dict]:
    """Flow B: KLayout devices plus Magic interconnect capacitors."""
    summary: dict = {
        "netlist": None,
        "core_netlist": None,
        "device_count": 0,
        "parasitic_count": 0,
        "capacitance_kept_fF": 0.0,
        "capacitance_dropped_fF": 0.0,
        "gates": {"pex_physical": pex_physical},
    }
    if not pex_physical:
        return None, summary

    core_ports = core_port_list(extracted, instances, port_nets)
    known_nets = set(core_ports)
    devices = klayout_core_devices(extracted)
    cap_lines, kept_f, dropped_f = magic_capacitor_lines(pex_netlist, known_nets)

    core_subckt = f"{CELL}_core"
    core_path = write_core(
        out_dir / f"{core_subckt}_magic.cir",
        core_subckt,
        core_ports,
        devices,
        parasitic_lines=tuple(cap_lines),
    )
    wrapper_path = out_dir / "postlayout_magic.cir"
    simview.write_wrapper(
        wrapper_path,
        CELL,
        port_nets,
        instances,
        core_netlist=str(core_path.resolve()),
        core_subckt=core_subckt,
        core_ports=core_ports,
    )
    _prepend_pdk_libs(wrapper_path)
    _inline_core(wrapper_path, core_path)
    summary.update(
        {
            "netlist": str(wrapper_path),
            "core_netlist": str(core_path),
            "device_count": len(devices),
            "parasitic_count": len(cap_lines),
            "capacitance_kept_fF": round(kept_f * 1e15, 4),
            "capacitance_dropped_fF": round(dropped_f * 1e15, 4),
        }
    )
    return wrapper_path, summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Build post-layout CTLE DUT netlists")
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Output directory (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--flow",
        choices=("klayout", "magic", "both"),
        default="both",
        help="Which netlist flow(s) to build",
    )
    args = parser.parse_args()

    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    block = build_ctle_stage(black_box=simview.BLACK_BOX_KINDS)
    gds_path = block.write(out_dir / "simview_gds")
    reduced_cdl = simview.write_reduced_cdl(
        CELL,
        block.port_nets,
        block.instances,
        out_dir / "simview_reduced.cdl",
    )

    lvs = run_lvs(
        gds=gds_path,
        cdl=reduced_cdl,
        run_dir=out_dir / "lvs_run",
        topcell=CELL,
        disable_tap_extraction=True,
    )
    lvs.write(out_dir / "lvs_result.json")

    gates_ok = lvs.clean
    summary: dict = {
        "cell": CELL,
        "simview_gds": str(gds_path),
        "simview_reduced_cdl": str(reduced_cdl),
        "flows": {},
        "gates": {
            "lvs_match": lvs.clean,
            "lvs_summary": lvs.summary,
        },
    }

    if not lvs.clean:
        print(f"LVS gate FAILED: {lvs.summary}", file=sys.stderr)
        summary_path = out_dir / "postlayout_summary.json"
        summary_path.write_text(json.dumps(summary, indent=2) + "\n")
        return 1

    extracted = Path(lvs.extracted_netlist)
    flows: dict = {}

    if args.flow in ("klayout", "both"):
        wrapper, flow_summary = build_klayout_flow(
            extracted, out_dir, block.instances, block.port_nets
        )
        flow_summary["gates"] = {"lvs_match": True}
        flows["klayout"] = flow_summary
        print(
            f"Flow A (klayout): {wrapper.name} — "
            f"{flow_summary['device_count']} devices, 0 parasitics"
        )

    pex_physical = False
    pex_netlist: Path | None = None
    if args.flow in ("magic", "both"):
        # Straight to Magic, NOT through write_for_magic. Pre-flattening the GDS
        # merges nets: the same layout that LVS-matches the reduced CDL extracts
        # with e1, e2 and vss collapsed into one node, all 75 array devices reading
        # d=e2 s=e2, and the parasitics asymmetric (nlp1 and nlp2 both coupling to
        # e2 with identical values). Passed directly, the same GDS gives three
        # distinct arrays and symmetric couplings. run_magic_pex already flattens
        # where it matters, in the netlist, via ext2spice hierarchy off.
        pex = run_magic_pex(
            gds=gds_path,
            cell=CELL,
            run_dir=out_dir / "pex_run",
            resistance=False,
        )
        pex.write(out_dir / "pex_result.json")
        pex_physical = pex.physical
        pex_netlist = Path(pex.netlist) if pex.netlist else None
        summary["gates"]["pex_physical"] = pex_physical
        if not pex_physical:
            neg = len(pex.negative_capacitors)
            print(
                f"PEX gate FAILED: physical={pex_physical} "
                f"(negative_caps={neg}); refusing Magic netlist",
                file=sys.stderr,
            )

    if args.flow in ("magic", "both") and pex_netlist is not None:
        wrapper, flow_summary = build_magic_flow(
            extracted,
            pex_netlist,
            out_dir,
            block.instances,
            block.port_nets,
            pex_physical,
        )
        flow_summary["gates"]["lvs_match"] = True
        flows["magic"] = flow_summary
        if wrapper is not None:
            print(
                f"Flow B (magic): {wrapper.name} — "
                f"{flow_summary['device_count']} devices, "
                f"{flow_summary['parasitic_count']} caps, "
                f"kept {flow_summary['capacitance_kept_fF']:.2f} fF, "
                f"dropped {flow_summary['capacitance_dropped_fF']:.2f} fF"
            )

    summary["flows"] = flows
    summary_path = out_dir / "postlayout_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")

    if args.flow in ("magic", "both") and not pex_physical:
        gates_ok = False

    return 0 if gates_ok else 1


if __name__ == "__main__":
    sys.exit(main())
