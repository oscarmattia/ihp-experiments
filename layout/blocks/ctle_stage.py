#!/usr/bin/env python3
"""The CTLE stage: place the sub-blocks and route them with gdsfactory.

Floorplan follows the schematic in ``circuits/ctle56n/spice/ctle_pdk.cir`` and is
driven by the sized values in ``params.inc``, so a resize moves the layout rather
than invalidating it.

    vdd ── coil ── coil ── vdd          (top: TopMetal2 supply and peaking coils)
            │        │
           nlp1     nlp2
            │        │
           rppd     rppd               (loads, mirrored pair)
            │        │
          outp     outn
            │        │
           Q1 ──────  Q2               (HBT pair, mirrored, guard-ringed)
            │        │
           e1 ─ Rs‖Cs ─ e2             (degeneration)
            │        │
         tail1     tail2               (mirrored tails, guard-ringed)

Vertical order matters electrically: MEMORY.md records that the shunt-peaking
order must be VDD -> L -> RD -> collector, and that the coil's port capacitance
belongs on the internal nlp node rather than in the output load.

Usage:
    python layout/blocks/ctle_stage.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from layout.blocks.generators import Block, mirror_pitch
from layout.common.devices import build
from layout.common.drc import CONTEXT_RULES, run_drc
from layout.common.gds import layer_summary, stamp_net_labels, write_gds
from layout.common.guard import RingSpec, add_guard_ring
from layout.common.lvs import run_lvs
from layout.common.netlist import write_block_cdl
from layout.common.pdk import new_layout, pya_module
from layout.common.pex import run_magic_pex, signal_resistors
from layout.common.render import render_gds
from layout.common.sizing import read_params
from layout.common.spec import DeviceSpec, Terminal
from layout.common.wrap import derive_terminals
from layout.devices.catalog import ctle_devices

OUT_DIR = Path(__file__).resolve().parent / "out" / "ctle_stage"

#: Vertical gap between floorplan rows, in um. Generous on purpose: the routes
#: between rows carry the signal, and MEMORY.md notes C_L is a per-side budget,
#: so a shorter route is worth more than a smaller cell.
ROW_GAP = 6.0

#: Horizontal half-separation of the two differential columns, in um.
COLUMN_GAP = 8.0

#: The stage cannot satisfy LBE.a on its own: the local back-end marker the coil
#: PCell draws is a chip-area shape with a 100 um floor. Everything else,
#: including latch-up, is enforced.
CONTEXT_ALLOWED = ("LBE.a",)


def _place_device(layout, cell, spec: DeviceSpec, dx: float, dy: float, mirror: bool = False):
    """Place a device and return its terminals in stage coordinates."""
    pya = pya_module()
    device_layout, device_cell = build(spec)
    terminals = derive_terminals(spec, device_layout, device_cell)

    index = layout.add_cell(f"{spec.name}_{'m' if mirror else 'n'}")
    layout.cell(index).copy_tree(device_cell)
    trans = pya.DTrans(pya.DTrans.M90 if mirror else pya.DTrans.R0, pya.DVector(dx, dy))
    cell.insert(pya.DCellInstArray(index, trans))

    placed = {}
    for terminal in terminals:
        point = trans * pya.DPoint(*terminal.center)
        orientation = (180.0 - terminal.orientation) % 360.0 if mirror else terminal.orientation
        placed[terminal.name] = Terminal(
            name=terminal.name,
            layer=terminal.layer,
            center=(round(point.x, 6), round(point.y, 6)),
            width=terminal.width,
            orientation=orientation,
        )
    return placed, layout.cell(index).dbbox()


def build_ctle_stage(params: dict[str, float] | None = None) -> Block:
    """Place and route one CTLE stage."""
    p = params or read_params()
    devices = {spec.name: spec for spec in ctle_devices(p)}

    layout = new_layout()
    cell = layout.create_cell("ctle_stage")

    coil = devices["inductor_turn1_d40"]
    load = devices["rppd_load"]
    hbt = devices["npn13G2_pair_device"]
    rdeg = devices["rsil_degen"]
    cdeg = devices["cmomi_cs"]
    tail = devices["nmos_tail"]

    # Column positions: the two halves are mirrored about x = axis.
    tail_pitch = mirror_pitch(tail, 4.0)
    axis = tail_pitch / 2.0
    left_x = axis - COLUMN_GAP
    right_x = axis + COLUMN_GAP

    instances: list[tuple[DeviceSpec, dict[str, str]]] = []
    ports: dict[str, Terminal] = {}
    row_y = 0.0

    # --- tails (bottom row) ------------------------------------------------
    tail_a = tail.with_name("tail1")
    tail_b = tail.with_name("tail2")
    tail_a_t, tail_box = _place_device(layout, cell, tail_a, 0.0, row_y)
    tail_b_t, _ = _place_device(layout, cell, tail_b, tail_pitch, row_y, mirror=True)
    instances += [
        (tail_a, {"D": "e1", "G": "mgate", "S": "vss", "sub": "sub"}),
        (tail_b, {"D": "e2", "G": "mgate", "S": "vss", "sub": "sub"}),
    ]
    tail_top = row_y + tail_box.height()

    # --- degeneration ------------------------------------------------------
    row_y = tail_top + ROW_GAP
    rdeg_i = rdeg.with_name("rdeg")
    cdeg_i = cdeg.with_name("cdeg")
    rdeg_t, rdeg_box = _place_device(layout, cell, rdeg_i, axis - 6.0, row_y)
    cdeg_t, cdeg_box = _place_device(layout, cell, cdeg_i, axis + 2.0, row_y)
    instances += [
        (rdeg_i, {"PLUS": "e1", "MINUS": "e2", "sub": "sub"}),
        (cdeg_i, {"PLUS": "e1", "MINUS": "e2"}),
    ]
    degen_top = row_y + max(rdeg_box.height(), cdeg_box.height())

    # --- HBT pair ----------------------------------------------------------
    row_y = degen_top + ROW_GAP
    q1 = hbt.with_name("q1")
    q2 = hbt.with_name("q2")
    q1_t, q_box = _place_device(layout, cell, q1, left_x, row_y)
    q2_t, _ = _place_device(layout, cell, q2, right_x + q_box.width(), row_y, mirror=True)
    instances += [
        (q1, {"C": "outp", "B": "inp", "E": "e1", "sub": "sub"}),
        (q2, {"C": "outn", "B": "inn", "E": "e2", "sub": "sub"}),
    ]
    hbt_top = row_y + q_box.height()

    # --- loads -------------------------------------------------------------
    row_y = hbt_top + ROW_GAP
    rd1 = load.with_name("rd1")
    rd2 = load.with_name("rd2")
    rd1_t, rd_box = _place_device(layout, cell, rd1, left_x, row_y)
    rd2_t, _ = _place_device(layout, cell, rd2, right_x + rd_box.width(), row_y, mirror=True)
    instances += [
        (rd1, {"PLUS": "nlp1", "MINUS": "outp", "sub": "sub"}),
        (rd2, {"PLUS": "nlp2", "MINUS": "outn", "sub": "sub"}),
    ]
    load_top = row_y + rd_box.height()

    # --- coils (top row) ---------------------------------------------------
    # VDD -> L -> RD -> collector, per MEMORY.md: the coil sits between the
    # supply and the load resistor, not between the load and the output.
    row_y = load_top + ROW_GAP
    l1 = coil.with_name("l1")
    l2 = coil.with_name("l2")
    coil_probe_layout, coil_probe = build(coil)
    coil_w = coil_probe.dbbox().width()
    l1_t, _ = _place_device(layout, cell, l1, left_x - coil_w / 2.0, row_y)
    l2_t, _ = _place_device(layout, cell, l2, right_x + coil_w / 2.0, row_y)
    instances += [
        (l1, {"PLUS": "vdd", "MINUS": "nlp1", "sub": "sub"}),
        (l2, {"PLUS": "vdd", "MINUS": "nlp2", "sub": "sub"}),
    ]

    # --- ports at the stage boundary ---------------------------------------
    for name, terminal in (
        ("inp", q1_t["B"]),
        ("inn", q2_t["B"]),
        ("outp", q1_t["C"]),
        ("outn", q2_t["C"]),
        ("vdd_l1", l1_t["PLUS"]),
        ("vdd_l2", l2_t["PLUS"]),
        ("mgate", tail_a_t["G"]),
        ("vss", tail_a_t["S"]),
    ):
        ports[name] = terminal

    guard = add_guard_ring(layout, cell, cell.dbbox(), RingSpec(kind="ptap1", clearance=2.0))

    nets = {
        "e1": [tail_a_t["D"], q1_t["E"], rdeg_t["PLUS"], cdeg_t["PLUS"]],
        "e2": [tail_b_t["D"], q2_t["E"], rdeg_t["MINUS"], cdeg_t["MINUS"]],
        "outp": [q1_t["C"], rd1_t["MINUS"]],
        "outn": [q2_t["C"], rd2_t["MINUS"]],
        "nlp1": [rd1_t["PLUS"], l1_t["MINUS"]],
        "nlp2": [rd2_t["PLUS"], l2_t["MINUS"]],
    }
    stamp_net_labels(layout, cell, list(ports.values()))
    for net, terminals in nets.items():
        stamp_net_labels(layout, cell, terminals, {t.name: net for t in terminals})

    block = Block(
        name="ctle_stage",
        layout=layout,
        cell=cell,
        ports=ports,
        instances=instances,
        port_nets=["outp", "outn", "inp", "inn", "vdd", "mgate", "vss"],
        guard=guard,
        notes=[
            "row order is VDD -> L -> RD -> collector, which MEMORY.md records "
            "as the working shunt-peaking order",
            "the coil's port capacitance lands on nlp1/nlp2, not on the output, "
            "so it must not be counted in the C_L budget",
            "sized from circuits/ctle56n/spice/params.inc: RD "
            f"{p['RPPD_R']:.2f} ohm, RS {p['RS']:.2f} ohm, "
            f"CS {p['CS'] * 1e15:.1f} fF, coil ~{p.get('L_EM', 0) * 1e12:.1f} pH",
        ],
    )
    block.nets = nets  # type: ignore[attr-defined]
    return block


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--no-render", action="store_true")
    parser.add_argument("--no-pex", action="store_true")
    args = parser.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    block = build_ctle_stage()
    entry = block.summary()

    gds = block.write(args.out)
    entry["gds"] = str(gds)
    print(f"  placed  {entry['bbox_um']['width']:.1f} x {entry['bbox_um']['height']:.1f} um")

    cdl = write_block_cdl(
        "ctle_stage", block.port_nets, block.instances, args.out / "ctle_stage.cdl"
    )
    entry["cdl"] = str(cdl)

    if not args.no_render:
        png = render_gds(gds, args.out / "ctle_stage.png", width=1200)
        entry["png"] = str(png) if png else None

    drc = run_drc(
        gds=gds, run_dir=args.out / "drc_run", cell_name="ctle_stage", allow_context=True
    )
    remaining = {r: c for r, c in drc.context_by_rule.items() if r not in CONTEXT_ALLOWED}
    drc_ok = drc.real_total == 0 and not remaining and not drc.error
    entry["drc"] = drc.to_dict()
    entry["drc"]["ok"] = drc_ok
    entry["drc"]["allowed_context_rules"] = list(CONTEXT_ALLOWED)
    entry["drc"]["enforced_context_rules"] = sorted(set(CONTEXT_RULES) - set(CONTEXT_ALLOWED))
    print(
        f"  DRC     {'ok' if drc_ok else 'FAIL'}  "
        f"real={drc.real_total} context={drc.context_by_rule or '{}'}"
    )

    lvs = run_lvs(
        gds=gds,
        cdl=cdl,
        run_dir=args.out / "lvs_run",
        topcell="ctle_stage",
        disable_tap_extraction=True,
    )
    entry["lvs"] = lvs.to_dict()
    print(f"  LVS     {'ok' if lvs.clean else 'FAIL'}  {lvs.summary[:60]}")

    if not args.no_pex:
        pex = run_magic_pex(gds=gds, cell="ctle_stage", run_dir=args.out / "pex_run")
        entry["pex"] = pex.to_dict()
        if pex.ok:
            signal = signal_resistors(pex.resistor_elements)
            entry["pex"]["signal_resistance_ohm"] = round(sum(e["ohm"] for e in signal), 6)
            print(
                f"  PEX     ok  {pex.capacitors} C totalling "
                f"{pex.total_capacitance * 1e15:.2f} fF, {pex.resistors} R"
            )
        else:
            print(f"  PEX     FAIL {pex.error[:60]}")

    (args.out / "ctle_stage_summary.json").write_text(json.dumps(entry, indent=2) + "\n")
    return 0 if (drc_ok and lvs.clean) else 1


if __name__ == "__main__":
    raise SystemExit(main())
