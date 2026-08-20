#!/usr/bin/env python3
"""Build KLayout- and Magic-based post-layout DUT netlists from the sim view.

The sim view black-boxes kinds the extractor cannot model (``inductor``, and
``cmomi`` on the CTLE) so LVS compares only the extractable core; the wrapper
re-instantiates those devices from the schematic compact models. Flow A uses
KLayout LVS device lines; Flow B adds Magic interconnect capacitance on the
same device core.

Usage:
    python layout/blocks/run_postlayout.py
    python layout/blocks/run_postlayout.py --stage vga
    python layout/blocks/run_postlayout.py --stage driver --flow both
    python layout/blocks/run_postlayout.py --stage all
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_BLOCK_OUT = Path(__file__).resolve().parent / "out"
DEFAULT_OUT = _BLOCK_OUT / "postlayout"

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from layout.blocks.ctle_stage import (  # noqa: E402
    CELL as CTLE_CELL,
    PORT_NETS as CTLE_PORTS,
    build_ctle_stage,
)
from layout.blocks.driver_stage import (  # noqa: E402
    CELL as DRIVER_CELL,
    PORT_NETS as DRIVER_PORTS,
    build_driver_stage,
)
from layout.blocks.vga_stage import (  # noqa: E402
    CELL as VGA_CELL,
    PORT_NETS as VGA_PORTS,
    build_vga_stage,
)
from layout.common.lvs import run_lvs  # noqa: E402
from layout.common.pex import run_magic_pex  # noqa: E402
from layout.common.postlayout import (  # noqa: E402
    _split_element_tokens,
    extract_subckt_lines,
    magic_capacitor_lines,
    parse_subckt_ports,
    rewrite_extracted_lines,
    write_core,
)
from layout.common import simview  # noqa: E402

_PDK_LIBS_HBT_MOS_RES = """\
.lib '{PDK_MODELS}/cornerHBT.lib' hbt_typ
.lib '{PDK_MODELS}/cornerMOSlv.lib' mos_tt
.lib '{PDK_MODELS}/cornerRES.lib' res_typ
"""
_PDK_LIBS_CTLE = _PDK_LIBS_HBT_MOS_RES + ".lib '{PDK_MODELS}/cornerCAP.lib' cap_typ\n"
_PDK_LIBS_DRIVER = _PDK_LIBS_HBT_MOS_RES + ".lib '{PDK_MODELS}/cornerDIO.lib' dio_tt\n"

_BLACK_BOX_INDUCTOR = frozenset({"inductor"})
_BLACK_BOX_CTLE = frozenset({"inductor", "cap_cmomi"})


@dataclass(frozen=True)
class StageSpec:
    """One device-only RX stage the post-layout flow knows how to wrap."""

    name: str
    cell: str
    port_nets: list[str]
    build: Callable[..., object]
    pdk_lib_header: str
    black_box_models: frozenset[str]
    default_out: Path
    inductor_subckt: str = "ind_shunt"


STAGES: dict[str, StageSpec] = {
    "ctle": StageSpec(
        name="ctle",
        cell=CTLE_CELL,
        port_nets=list(CTLE_PORTS),
        build=build_ctle_stage,
        pdk_lib_header=_PDK_LIBS_CTLE,
        black_box_models=_BLACK_BOX_CTLE,
        default_out=_BLOCK_OUT / "postlayout",
    ),
    "vga": StageSpec(
        name="vga",
        cell=VGA_CELL,
        port_nets=list(VGA_PORTS),
        build=build_vga_stage,
        pdk_lib_header=_PDK_LIBS_HBT_MOS_RES,
        black_box_models=_BLACK_BOX_INDUCTOR,
        default_out=_BLOCK_OUT / "postlayout_vga",
    ),
    "driver": StageSpec(
        name="driver",
        cell=DRIVER_CELL,
        port_nets=list(DRIVER_PORTS),
        build=build_driver_stage,
        pdk_lib_header=_PDK_LIBS_DRIVER,
        black_box_models=_BLACK_BOX_INDUCTOR,
        default_out=_BLOCK_OUT / "postlayout_driver",
        inductor_subckt="ind_shunt_drv",
    ),
}


def _prepend_pdk_libs(wrapper_path: Path, header: str) -> None:
    """Match the schematic: corner libraries must load before the core devices."""
    text = wrapper_path.read_text()
    if "cornerHBT.lib" in text:
        return
    wrapper_path.write_text(header + "\n" + text)


def _inline_core(wrapper_path: Path, core_path: Path) -> None:
    """Paste core device lines into the DUT so ``save @q.xu1.xq1...`` resolves.

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


def _is_black_box_line(line: str, black_box_models: frozenset[str]) -> bool:
    tokens = line.split()
    body = tokens[1:]
    while body and "=" in body[-1]:
        body.pop()
    if not body:
        return False
    model = body[-1]
    return model.lower() in black_box_models


def klayout_core_devices(
    extracted: Path, cell: str, black_box_models: frozenset[str]
) -> list[str]:
    """Device lines from the LVS deck, minus black-boxed kinds, normalised."""
    raw = extract_subckt_lines(extracted, cell)
    kept = [line for line in raw if not _is_black_box_line(line, black_box_models)]
    return rewrite_extracted_lines(kept, cell)


CL_MARKER = "* postlayout-cl-model:"


def _declare_cl_model(netlist: Path, model: str) -> None:
    """Record which testbench load a netlist expects, for the simulator to read.

    Whether the lumped CL should include the interconnect term depends on whether
    this netlist carries the interconnect itself, which only the flow that built it
    knows. Leaving that to a CLI default gets it wrong silently: applying Miller-only
    to a netlist with no parasitics under-loads the output by 15 fF and reported
    0.6 dB more peaking than the design has.

    On the pad driver the same marker also means "drop the testbench pad cap":
    Magic already extracted the bond-pad metal, so ``PAD_C`` would double-count it.
    """
    text = netlist.read_text()
    netlist.write_text(f"{CL_MARKER} {model}\n{text}")


def _rel(path: Path) -> str:
    """Repo-relative where possible: this summary is a committed artifact."""
    try:
        return str(Path(path).resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def core_port_list(extracted: Path, cell: str, instances: list, port_nets: list[str]) -> list[str]:
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
    extracted_ports = parse_subckt_ports(extracted, cell)
    # Black-box promotion covers nets a removed device used to touch. Internal
    # nets that stay in the core (VGA ``em``/``ed1``/``ed2``, driver ``em``)
    # still appear on the extracted ``.SUBCKT`` line whenever the layout
    # labelled them, and Magic capacitors on those nets are dropped unless
    # they are core ports. Append them; do not take the extracted list as the
    # *only* source — ``vdd`` is absent from it when both coils are boxed.
    extra = [net for net in extracted_ports if net not in ports]
    return ports + extra


def build_klayout_flow(
    spec: StageSpec,
    extracted: Path,
    out_dir: Path,
    instances: list,
    port_nets: list[str],
) -> tuple[Path, dict]:
    """Flow A: KLayout devices only, wrapped for schematic interface."""
    core_ports = core_port_list(extracted, spec.cell, instances, port_nets)
    devices = klayout_core_devices(extracted, spec.cell, spec.black_box_models)
    core_subckt = f"{spec.cell}_core"
    core_path = write_core(
        out_dir / f"{core_subckt}.cir",
        core_subckt,
        core_ports,
        devices,
    )
    wrapper_path = out_dir / "postlayout_klayout.cir"
    simview.write_wrapper(
        wrapper_path,
        spec.cell,
        port_nets,
        instances,
        core_netlist=str(core_path.resolve()),
        core_subckt=core_subckt,
        core_ports=core_ports,
        inductor_subckt=spec.inductor_subckt,
    )
    _prepend_pdk_libs(wrapper_path, spec.pdk_lib_header)
    _inline_core(wrapper_path, core_path)
    _declare_cl_model(wrapper_path, "full")
    summary = {
        "netlist": _rel(wrapper_path),
        "core_netlist": _rel(core_path),
        "device_count": len(devices),
        "parasitic_count": 0,
        "capacitance_kept_fF": 0.0,
        "capacitance_dropped_fF": 0.0,
        "gates": {},
    }
    return wrapper_path, summary


def build_magic_flow(
    spec: StageSpec,
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

    core_ports = core_port_list(extracted, spec.cell, instances, port_nets)
    devices = klayout_core_devices(extracted, spec.cell, spec.black_box_models)
    known_nets = set(core_ports)
    for line in devices:
        try:
            _, nodes, _, _ = _split_element_tokens(line)
        except ValueError:
            continue
        known_nets.update(nodes)
    cap_lines, kept_f, dropped_f = magic_capacitor_lines(pex_netlist, known_nets)

    core_subckt = f"{spec.cell}_core"
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
        spec.cell,
        port_nets,
        instances,
        core_netlist=str(core_path.resolve()),
        core_subckt=core_subckt,
        core_ports=core_ports,
        inductor_subckt=spec.inductor_subckt,
    )
    _prepend_pdk_libs(wrapper_path, spec.pdk_lib_header)
    _inline_core(wrapper_path, core_path)
    _declare_cl_model(wrapper_path, "miller")
    summary.update(
        {
            "netlist": _rel(wrapper_path),
            "core_netlist": _rel(core_path),
            "device_count": len(devices),
            "parasitic_count": len(cap_lines),
            "capacitance_kept_fF": round(kept_f * 1e15, 4),
            "capacitance_dropped_fF": round(dropped_f * 1e15, 4),
        }
    )
    return wrapper_path, summary


def run_stage(spec: StageSpec, out_dir: Path, flow: str) -> int:
    """Build one stage's post-layout netlists. Returns 0 on gate success."""
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"== post-layout {spec.name} ({spec.cell}) → {out_dir}")

    block = spec.build(black_box=simview.BLACK_BOX_KINDS)
    gds_path = block.write(out_dir / "simview_gds")
    reduced_cdl = simview.write_reduced_cdl(
        spec.cell,
        block.port_nets,
        block.instances,
        out_dir / "simview_reduced.cdl",
    )

    lvs = run_lvs(
        gds=gds_path,
        cdl=reduced_cdl,
        run_dir=out_dir / "lvs_run",
        topcell=spec.cell,
        disable_tap_extraction=True,
    )
    lvs.write(out_dir / "lvs_result.json")

    gates_ok = lvs.clean
    summary: dict = {
        "cell": spec.cell,
        "stage": spec.name,
        "simview_gds": _rel(gds_path),
        "simview_reduced_cdl": _rel(reduced_cdl),
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
    # A one-flow rebuild must not wipe the other flow's committed summary.
    flows: dict = {}
    summary_path = out_dir / "postlayout_summary.json"
    if summary_path.is_file():
        try:
            previous = json.loads(summary_path.read_text()).get("flows") or {}
            if isinstance(previous, dict):
                flows.update(previous)
        except json.JSONDecodeError:
            pass

    if flow in ("klayout", "both"):
        wrapper, flow_summary = build_klayout_flow(
            spec, extracted, out_dir, block.instances, block.port_nets
        )
        flow_summary["gates"] = {"lvs_match": True}
        flows["klayout"] = flow_summary
        print(
            f"Flow A (klayout): {wrapper.name} — "
            f"{flow_summary['device_count']} devices, 0 parasitics"
        )

    pex_physical = False
    pex_netlist: Path | None = None
    if flow in ("magic", "both"):
        # Straight to Magic, NOT through write_for_magic. Pre-flattening the GDS
        # merges nets: the same layout that LVS-matches the reduced CDL extracts
        # with e1, e2 and vss collapsed into one node, all 75 array devices reading
        # d=e2 s=e2, and the parasitics asymmetric (nlp1 and nlp2 both coupling to
        # e2 with identical values). Passed directly, the same GDS gives three
        # distinct arrays and symmetric couplings. run_magic_pex already flattens
        # where it matters, in the netlist, via ext2spice hierarchy off.
        pex = run_magic_pex(
            gds=gds_path,
            cell=spec.cell,
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

    if flow in ("magic", "both") and pex_netlist is not None:
        wrapper, flow_summary = build_magic_flow(
            spec,
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

    if flow in ("magic", "both") and not pex_physical:
        gates_ok = False

    return 0 if gates_ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build post-layout DUT netlists for CTLE, VGA, and/or pad driver"
    )
    parser.add_argument(
        "--stage",
        choices=("ctle", "vga", "driver", "all"),
        default="ctle",
        help="Which stage to wrap (default: ctle, matching the original entry point)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory (default: layout/blocks/out/postlayout[_vga|_driver])",
    )
    parser.add_argument(
        "--flow",
        choices=("klayout", "magic", "both"),
        default="both",
        help="Which netlist flow(s) to build",
    )
    args = parser.parse_args()

    names = list(STAGES) if args.stage == "all" else [args.stage]
    if args.out is not None and len(names) > 1:
        parser.error("--out cannot be combined with --stage all")

    rc = 0
    for name in names:
        spec = STAGES[name]
        out_dir = args.out.resolve() if args.out is not None else spec.default_out
        stage_rc = run_stage(spec, out_dir, args.flow)
        if stage_rc != 0:
            rc = stage_rc
    return rc


if __name__ == "__main__":
    sys.exit(main())
