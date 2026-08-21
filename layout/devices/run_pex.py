#!/usr/bin/env python3
"""Extract parasitics for the device catalog and simulate the result.

Three things happen here:

1. Magic extracts R and C for every device using the PDK extraction tech.
2. ``klayout.pex`` independently computes the resistance of a routed wire, as a
   cross-check on Magic with a different algorithm and a different
   sheet-resistance source.
3. ngspice simulates the extracted resistor and capacitors against their
   schematic counterparts, so the output is "layout cost you X%" rather than a
   parasitic count.

Usage:
    python layout/devices/run_pex.py
    python layout/devices/run_pex.py --only rppd_load
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from layout.common.devices import kind_of
from layout.common.gds import write_for_magic
from layout.common.pex import klayout_wire_resistance, run_magic_pex, signal_resistors
from layout.common.postlayout import (
    compare,
    measure_two_terminal_capacitance,
    measure_two_terminal_resistance,
    prepare_pex_include,
)
from layout.common.spec import read_specs

OUT_DIR = Path(__file__).resolve().parent / "out"
SPIKE_GDS = Path(__file__).resolve().parents[1] / "out" / "spike" / "rppd_route_spike.gds"

#: Devices whose electrical behaviour we compare schematic vs post-layout, and
#: the quantity to compare. Two-terminal passives are the meaningful cases: a
#: MOS or HBT needs a bias-dependent comparison, which belongs with the block
#: and stage simulations that already exist in circuits/ctle56n.
COMPARE: dict[str, dict[str, str]] = {
    "rppd": {"quantity": "resistance", "lib": "cornerRES.lib", "corner": "res_typ"},
    "rsil": {"quantity": "resistance", "lib": "cornerRES.lib", "corner": "res_typ"},
    "cmim": {"quantity": "capacitance", "lib": "cornerCAP.lib", "corner": "cap_typ"},
    "cmomi": {"quantity": "capacitance", "lib": "cornerCAP.lib", "corner": "cap_typ"},
}

#: Where Magic's extraction is not the authority, and what is instead. Recorded
#: alongside the numbers so nobody reads a large post-layout delta as a real
#: layout cost.
PEX_NOTES: dict[str, str] = {
    "cmomi": (
        "Magic does not recognise the metal-finger cap as a device: it extracts "
        "the interdigitated fingers geometrically (thousands of coupling caps) "
        "with an uncalibrated area+fringe model, so the post-layout figure is "
        "not comparable to the calibrated cap_cmomi compact model. Trust the "
        "compact model; use this only to see that the geometry is connected as "
        "intended."
    ),
    "inductor": (
        "Coil behaviour comes from openEMS plus the fitted ind_shunt model, not "
        "from Magic — see MEMORY.md. Magic contributes substrate capacitance "
        "only, which is a useful independent check on the EM port capacitance."
    ),
}


def schematic_dut_line(spec, kind) -> str:
    """A schematic-only instantiation of the device between nets p and n."""
    params = " ".join(f"{k}={v}" for k, v in kind.to_cdl(spec.params).items())
    if kind.bulk_node is not None:
        return f"X1 p n 0 {kind.spice_model} {params}"
    return f"X1 p n {kind.spice_model} {params}"


def compare_device(spec, kind, pex_netlist: Path, run_dir: Path) -> dict | None:
    """Simulate schematic and extracted views and compare them."""
    plan = COMPARE.get(spec.kind)
    if plan is None:
        return None

    include = prepare_pex_include(pex_netlist, run_dir)
    body = f".include {include.path}"
    schem_line = schematic_dut_line(spec, kind)

    if include.subckt:
        post_dut = include.dut_line()
        force, sense = "p", "n"
    else:
        # Top-level extraction: drive the circuit's own port nets directly
        # instead of instantiating a subcircuit that does not exist.
        post_dut = ""
        force, sense = include.ports

    if plan["quantity"] == "resistance":
        schem = measure_two_terminal_resistance(
            run_dir, f"{spec.name}_schem_r", "", plan["lib"], plan["corner"], schem_line
        )
        post = measure_two_terminal_resistance(
            run_dir, f"{spec.name}_pex_r", body, plan["lib"], plan["corner"], post_dut,
            force=force, sense=sense,
        )
        unit = "ohm"
    else:
        schem = measure_two_terminal_capacitance(
            run_dir, f"{spec.name}_schem_c", "", plan["lib"], plan["corner"], schem_line
        )
        post = measure_two_terminal_capacitance(
            run_dir, f"{spec.name}_pex_c", body, plan["lib"], plan["corner"], post_dut,
            force=force, sense=sense,
        )
        unit = "F"

    if not (schem.ok and post.ok):
        return {
            "quantity": plan["quantity"],
            "unit": unit,
            "ok": False,
            "error": schem.error or post.error,
        }
    out = {"quantity": plan["quantity"], "unit": unit, "ok": True}
    out.update(compare(schem.value, post.value))  # type: ignore[arg-type]
    return out


def cross_check_wire(run_dir: Path) -> dict | None:
    """Compare Magic and klayout.pex on the same routed Metal1 wire.

    Two independent paths to the same number: Magic's extract deck against
    KLayout's square counting scaled by the ITF sheet resistance. Agreement is
    evidence that neither is badly misconfigured; the wire is a plain rectilinear
    route, which is the case both tools should get right.
    """
    if not SPIKE_GDS.is_file():
        return None

    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(SPIKE_GDS))
    top = layout.top_cell()
    layer_index = layout.layer(8, 0)
    region = kdb.Region(top.begin_shapes_rec(layer_index)).merged()
    if region.is_empty():
        return None
    polygon = max(region.each(), key=lambda p: p.area())
    box = polygon.bbox()
    ports = [
        (box.left * layout.dbu, box.bottom * layout.dbu),
        (box.right * layout.dbu, box.top * layout.dbu),
    ]

    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    out: dict = {}
    try:
        out["klayout_pex"] = klayout_wire_resistance(SPIKE_GDS, "Metal1", ports)
    except Exception as exc:  # noqa: BLE001
        out["klayout_pex"] = {"error": f"{type(exc).__name__}: {exc}"}

    # Outside run_dir: run_magic_pex clears its run directory first.
    magic_gds, magic_cell = write_for_magic(
        SPIKE_GDS, run_dir.parent / f"{SPIKE_GDS.stem}_flat.gds", cell=None
    )
    magic = run_magic_pex(gds=magic_gds, cell=magic_cell, run_dir=run_dir)
    if magic.ok:
        # Only the signal interconnect counts: the cell's other resistors are the
        # devices' well paths, which run to kilo-ohms and would swamp the route.
        signal = signal_resistors(magic.resistor_elements)
        route_r = sum(e["ohm"] for e in signal)
        out["magic"] = {
            "resistors": magic.resistors,
            "signal_resistors": signal,
            "route_resistance_ohm": round(route_r, 6),
            "bulk_resistors": [
                e for e in magic.resistor_elements if e not in signal
            ][:6],
            "netlist": magic.netlist,
        }
    else:
        out["magic"] = {"error": magic.error}

    kl = out.get("klayout_pex", {})
    mg = out.get("magic", {})
    if "series_resistance_ohm" in kl and "route_resistance_ohm" in mg:
        a = kl["series_resistance_ohm"]
        b = mg["route_resistance_ohm"]
        worst = max(abs(a), abs(b)) or 1.0
        out["agreement"] = {
            "klayout_ohm": a,
            "magic_ohm": b,
            "abs_diff_ohm": round(abs(a - b), 6),
            "rel_diff": round(abs(a - b) / worst, 6),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--no-sim", action="store_true", help="extract but do not simulate")
    args = parser.parse_args(argv)

    manifest_path = args.out / "manifest.json"
    if not manifest_path.is_file():
        parser.error(f"{manifest_path} missing; run gen_devices.py first")
    manifest = json.loads(manifest_path.read_text())
    specs = {s.name: s for s in read_specs(args.out / "specs.json")}

    devices = manifest["devices"]
    if args.only:
        wanted = set(args.only)
        devices = [d for d in devices if d["name"] in wanted]

    results = {}
    failed = []
    for device in devices:
        name = device["name"]
        spec = specs[name]
        kind = kind_of(spec)
        pex = run_magic_pex(
            gds=args.out / device["gds"],
            cell=name,
            run_dir=args.out / "pex_run" / name,
        )
        entry = pex.to_dict()
        if spec.kind in PEX_NOTES:
            entry["authority_note"] = PEX_NOTES[spec.kind]

        if pex.ok and not args.no_sim:
            comparison = compare_device(
                spec, kind, Path(pex.netlist), args.out / "pex_run" / name / "sim"
            )
            if comparison:
                entry["comparison"] = comparison

        (args.out / "pex").mkdir(parents=True, exist_ok=True)
        (args.out / "pex" / f"{name}_pex.json").write_text(json.dumps(entry, indent=2) + "\n")
        results[name] = entry

        if not pex.ok:
            print(f"  {name:<26} PEX FAIL  {pex.error[:70]}")
            failed.append(name)
            continue

        note = f"{pex.resistors} R, {pex.capacitors} C, Ctot {pex.total_capacitance * 1e15:.2f} fF"
        if not pex.resistance_extracted:
            note += " (C only)"
        comparison = entry.get("comparison")
        if comparison and comparison.get("ok"):
            caveat = "  [see authority_note]" if spec.kind in PEX_NOTES else ""
            note += (
                f"  |  {comparison['quantity']}: "
                f"{comparison['schematic']:.4g} -> {comparison['post_layout']:.4g} "
                f"{comparison['unit']} ({comparison['rel_change_pct']:+.2f}%){caveat}"
            )
        elif comparison:
            note += f"  |  sim failed: {str(comparison.get('error'))[:40]}"
        print(f"  {name:<26} PEX ok    {note}")

    cross = cross_check_wire(args.out / "pex_run" / "_wire_cross_check")
    summary = {
        "ok": not failed,
        "total_devices": len(results),
        "failed": failed,
        "klayout_pex_wire_cross_check": cross,
        "by_device": results,
    }
    (args.out / "pex_summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    if cross and "agreement" in cross:
        a = cross["agreement"]
        print(
            f"\nrouted Metal1 wire, two extractors: klayout.pex "
            f"{a['klayout_ohm']:.3f} ohm vs magic {a['magic_ohm']:.3f} ohm "
            f"({a['rel_diff'] * 100:.1f}% apart)"
        )
    elif cross:
        print(f"\nwire cross-check incomplete: {cross}")
    print(f"\n{len(results) - len(failed)}/{len(results)} device(s) extracted")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
