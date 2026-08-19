#!/usr/bin/env python3
"""The CTLE stage: symmetric placement about one axis, with a supply strap.

Floorplan follows ``circuits/ctle56n/spice/ctle_pdk.cir`` and is driven by the
sized values in ``params.inc``, so a resize moves the layout rather than
invalidating it.

Every row is centred on a single vertical axis and the two differential halves
are exact mirror images of each other about it. The coils are rotated so they
face each other across that axis: their supply feeds then land at the same
height a few microns apart, and one straight TopMetal2 strap ties them to vdd.
Their other feeds sit below the strap, directly above the load resistors, so
each nlp net is a short vertical run that never crosses the supply.

    ══════════ vdd strap (TopMetal2) ══════════
      coil ◄──┤                 ├──► coil        M135 / R270, bodies outboard
              nlp1            nlp2
              rppd            rppd               loads, mirrored
              outp            outn
               Q1 ─────────────Q2                HBT pair, mirrored, guard-ringed
               e1 ─── Rs‖Cs ─── e2               degeneration, rotated 90 deg
             tail1            tail2              strapped 243 um arrays

The vertical order is not cosmetic: MEMORY.md records that shunt peaking must be
wired VDD -> L -> RD -> collector, and that the coil's port capacitance lands on
the internal nlp node rather than in the output load, so it must stay out of the
C_L budget.

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

from layout.blocks.generators import Block, _connect_metal1_to
from layout.blocks.mos_array import build_mos_array
from layout.common.devices import build
from layout.common.drc import CONTEXT_RULES, run_drc
from layout.common.gds import stamp_net_labels
from layout.common.guard import RingSpec, add_guard_ring
from layout.common.layers import layer_map
from layout.common.lvs import run_lvs
from layout.common.netlist import write_block_cdl
from layout.common.pdk import new_layout, pya_module
from layout.common.pex import run_magic_pex, signal_resistors
from layout.common.render import render_gds
from layout.common.sizing import metres, read_params
from layout.common.spec import DeviceSpec, Terminal
from layout.common.wrap import derive_terminals
from layout.common.xsection import ROUTE_WIDTHS
from layout.devices.catalog import ctle_devices

OUT_DIR = Path(__file__).resolve().parent / "out" / "ctle_stage"

#: Manufacturing grid in um.
GRID = 0.005

#: Vertical gap between floorplan rows, in um.
ROW_GAP = 8.0

#: Half-separation of the coil feeds from the symmetry axis, in um. This also
#: sets the span of the vdd strap and the x of the load resistors underneath.
FEED_DX = 8.0

#: Metal used for the inter-row differential nets. Devices bring their terminals
#: out on Metal1 and Metal2 (the cap on Metal4/5, the coils on TopMetal2), so
#: routing a level above lets a net cross a device without touching its pins.
ROUTE_METAL = "Metal3"

#: Chip-level rules the stage cannot satisfy alone: the coils' local back-end
#: markers are chip-area shapes with a 100 um minimum width and a spacing rule
#: between regions. Latch-up is enforced.
CHIP_LEVEL_ALLOWED = ("LBE.a", "LBE.c")


def _snap(value: float) -> float:
    return round(round(value / GRID) * GRID, 6)


def _place(
    layout,
    cell,
    spec: DeviceSpec,
    dx: float,
    dy: float,
    orientation: str = "R0",
):
    """Place a device and return its terminals in stage coordinates."""
    pya = pya_module()
    device_layout, device_cell = build(spec)
    terminals = derive_terminals(spec, device_layout, device_cell)

    rot = {
        "R0": pya.DTrans.R0,
        "R90": pya.DTrans.R90,
        "R180": pya.DTrans.R180,
        "R270": pya.DTrans.R270,
        "M0": pya.DTrans.M0,
        "M45": pya.DTrans.M45,
        "M90": pya.DTrans.M90,
        "M135": pya.DTrans.M135,
    }[orientation]

    index = layout.add_cell(f"{spec.name}_{orientation}")
    layout.cell(index).copy_tree(device_cell)
    trans = pya.DTrans(rot, pya.DVector(_snap(dx), _snap(dy)))
    cell.insert(pya.DCellInstArray(index, trans))

    placed = {}
    for terminal in terminals:
        point = trans * pya.DPoint(*terminal.center)
        placed[terminal.name] = Terminal(
            name=terminal.name,
            layer=terminal.layer,
            center=(_snap(point.x), _snap(point.y)),
            width=terminal.width,
            # Orientation is only used to pick a routing direction; the exact
            # value after an arbitrary transform is not needed here because
            # every run in this stage is explicitly vertical or horizontal.
            orientation=terminal.orientation,
        )
    return placed, (trans * layout.cell(index).dbbox())


def _mirrored_pair_x(spec: DeviceSpec, axis: float, gap: float) -> tuple[float, float]:
    """Translations for a device and its mirror, exactly symmetric about ``axis``.

    A mirrored instance reflects about its own origin, so its extent depends on
    the device's bounding box. Solving for the translation rather than guessing
    a pitch is what keeps the halves symmetric when the device is resized.
    """
    _, cell = build(spec)
    box = cell.dbbox()
    inner = gap / 2.0
    left = axis - inner - box.right
    right = axis + inner + box.right
    return _snap(left), _snap(right)


def _rect(layout, cell, metal: str, x0: float, y0: float, x1: float, y1: float) -> None:
    pya = pya_module()
    lm = layer_map()
    ld = lm[f"{metal.lower()}_drw"]
    cell.shapes(layout.layer(ld[0], ld[1])).insert(
        pya.DBox(_snap(min(x0, x1)), _snap(min(y0, y1)), _snap(max(x0, x1)), _snap(max(y0, y1)))
    )


#: How far outside a device a via stack is placed, in um.
VIA_OFFSET = 2.0


def _via_up(layout, cell, terminal: Terminal, metal: str) -> tuple[float, float]:
    """Via stack from a terminal's metal up to ``metal``; returns its position.

    The stack is placed on a short stub *outside* the device rather than on the
    pin itself. A via stack's landing pads are wider than a device pin, so
    dropping one directly on a pin pushes Metal1, Metal2, contact-bar and via
    spacing rules against the device's own geometry — twelve CntB.h1, eight V1.b
    and eight PWB.f violations before this offset was introduced.
    """
    from layout.common.route import metal_of

    pya = pya_module()
    bottom = metal_of(terminal.layer)
    x, y = terminal.center
    if bottom is None:
        return (x, y)

    # Step outward along the terminal's facing direction.
    angle = terminal.orientation % 360.0
    dx = {0.0: VIA_OFFSET, 180.0: -VIA_OFFSET}.get(angle, 0.0)
    dy = {90.0: VIA_OFFSET, 270.0: -VIA_OFFSET}.get(angle, 0.0)
    vx, vy = _snap(x + dx), _snap(y + dy)

    # Stub on the terminal's own metal from the pin out to the via.
    stub_w = ROUTE_WIDTHS.get(bottom, 0.3)
    _rect(
        layout, cell, bottom,
        min(x, vx) - stub_w / 2, min(y, vy) - stub_w / 2,
        max(x, vx) + stub_w / 2, max(y, vy) + stub_w / 2,
    )

    if bottom == metal:
        return (vx, vy)

    via = DeviceSpec(
        name=f"via_{bottom.lower()}_{metal.lower()}",
        kind="via_stack",
        params={"b_layer": bottom, "t_layer": metal, "columns": 2, "rows": 2},
    )
    _, via_cell = build(via)
    index = layout.add_cell(f"{via.name}_{vx}_{vy}")
    layout.cell(index).copy_tree(via_cell)
    box = via_cell.dbbox()
    cell.insert(
        pya.DCellInstArray(
            index,
            pya.DTrans(pya.DVector(_snap(vx - box.center().x), _snap(vy - box.center().y))),
        )
    )
    return (vx, vy)


def _vertical_net(layout, cell, terminals: list[Terminal], metal: str) -> None:
    """Join terminals that share an x with a single vertical run."""
    width = ROUTE_WIDTHS.get(metal, 0.6)
    xs = {t.center[0] for t in terminals}
    if len(xs) != 1:
        raise ValueError(f"vertical net needs one x, got {sorted(xs)}")
    x = xs.pop()
    points = [_via_up(layout, cell, t, metal) for t in terminals]
    ys = [py for _, py in points]
    _rect(layout, cell, metal, x - width / 2, min(ys), x + width / 2, max(ys))


def _trunk_net(layout, cell, terminals: list[Terminal], trunk_x: float, metal: str) -> None:
    """Vertical trunk on ``metal`` plus a horizontal stub per terminal."""
    width = ROUTE_WIDTHS.get(metal, 0.6)
    trunk_x = _snap(trunk_x)
    points = [_via_up(layout, cell, t, metal) for t in terminals]
    ys = [py for _, py in points]
    _rect(layout, cell, metal, trunk_x - width / 2, min(ys), trunk_x + width / 2, max(ys))
    for x, y in points:
        _rect(layout, cell, metal, min(x, trunk_x), y - width / 2, max(x, trunk_x), y + width / 2)


def build_ctle_stage(params: dict[str, float] | None = None) -> Block:
    """Place and wire one CTLE stage."""
    p = params or read_params()
    devices = {spec.name: spec for spec in ctle_devices(p)}
    pya = pya_module()
    lm = layer_map()

    coil = devices["inductor_turn1_d40"]
    load = devices["rppd_load"]
    hbt = devices["npn13G2_pair_device"]
    rdeg = devices["rsil_degen"]
    cdeg = devices["cmomi_cs"]

    layout = new_layout()
    cell = layout.create_cell("ctle_stage")
    instances: list[tuple[DeviceSpec, dict[str, str]]] = []

    # --- symmetry axis, set by the widest row (the tail arrays) -------------
    tail_w = metres(p, "MOS_W")
    tail_l = metres(p, "MOS_L")
    array_a = build_mos_array("tail1", tail_w, tail_l)
    array_b = build_mos_array("tail2", tail_w, tail_l)
    array_box = array_a.cell.dbbox()
    tail_gap = 12.0
    axis = _snap(array_box.width() + tail_gap / 2.0)

    # --- tails (bottom row), mirrored about the axis ------------------------
    tail_ports: dict[str, Terminal] = {}
    tail_left = _snap(axis - tail_gap / 2.0 - array_box.width())
    tail_right = _snap(axis + tail_gap / 2.0)
    for tag, array, dx, drain_net in (
        ("A", array_a, tail_left, "e1"),
        ("B", array_b, tail_right, "e2"),
    ):
        index = layout.add_cell(f"{array.name}_cell")
        layout.cell(index).copy_tree(array.cell)
        trans = pya.DTrans(pya.DVector(dx, 0.0))
        cell.insert(pya.DCellInstArray(index, trans))
        for name, terminal in array.ports.items():
            point = trans * pya.DPoint(*terminal.center)
            tail_ports[f"{name}_{tag}"] = Terminal(
                name=f"{name}_{tag}",
                layer=terminal.layer,
                center=(_snap(point.x), _snap(point.y)),
                width=terminal.width,
                orientation=terminal.orientation,
            )
        instances += [
            (
                array.unit.with_name(f"{array.name}_u{i}"),
                {"D": drain_net, "G": "mgate", "S": "vss", "sub": "sub"},
            )
            for i in range(array.units)
        ]

    # Tie the arrays' source rails and gate straps across both halves.
    rail_left = _snap(tail_left - 1.0)
    rail_right = _snap(tail_right + array_box.width() + 1.0)
    source_y = tail_ports["S_A"].center[1]
    _rect(layout, cell, "Metal2", rail_left, source_y - 0.5, rail_right, source_y + 0.5)
    gate_y = tail_ports["G_A"].center[1]
    poly = lm["gatpoly_drw"]
    cell.shapes(layout.layer(poly[0], poly[1])).insert(
        pya.DBox(rail_left, _snap(gate_y - 0.3), rail_right, _snap(gate_y + 0.3))
    )
    tail_top = array_box.top

    # --- degeneration, rotated so its terminals separate left/right --------
    row_y = _snap(tail_top + ROW_GAP)
    rdeg_i = rdeg.with_name("rdeg")
    cdeg_i = cdeg.with_name("cdeg")
    _, rdeg_probe = build(rdeg_i)
    rdeg_t, rdeg_box = _place(
        layout, cell, rdeg_i, axis - rdeg_probe.dbbox().height() / 2.0, row_y, orientation="R90"
    )
    cdeg_t, cdeg_box = _place(layout, cell, cdeg_i, axis + 14.0, row_y + 6.0)
    _connect_metal1_to(
        layout, cell, rdeg_t["PLUS"], cdeg_t["PLUS"], "Metal4", avoid=rdeg_t["MINUS"]
    )
    _connect_metal1_to(
        layout, cell, rdeg_t["MINUS"], cdeg_t["MINUS"], "Metal5", avoid=rdeg_t["PLUS"]
    )
    instances += [
        (rdeg_i, {"PLUS": "e1", "MINUS": "e2", "sub": "sub"}),
        (cdeg_i, {"PLUS": "e1", "MINUS": "e2"}),
    ]
    degen_top = _snap(max(rdeg_box.top, cdeg_box.top))

    # --- HBT pair, mirrored ------------------------------------------------
    row_y = _snap(degen_top + ROW_GAP)
    q1 = hbt.with_name("q1")
    q2 = hbt.with_name("q2")
    q_left, q_right = _mirrored_pair_x(hbt, axis, gap=2 * FEED_DX)
    q1_t, q_box = _place(layout, cell, q1, q_left, row_y)
    q2_t, _ = _place(layout, cell, q2, q_right, row_y, orientation="M90")
    instances += [
        (q1, {"C": "outp", "B": "inp", "E": "e1", "sub": "sub"}),
        (q2, {"C": "outn", "B": "inn", "E": "e2", "sub": "sub"}),
    ]
    hbt_top = _snap(q_box.top)

    # --- loads, placed so each PLUS sits under its coil feed ---------------
    row_y = _snap(hbt_top + ROW_GAP)
    rd1 = load.with_name("rd1")
    rd2 = load.with_name("rd2")
    _, load_probe = build(load)
    load_terms = {t.name: t for t in derive_terminals(load, *build(load))}
    # The upper terminal faces the coil and the lower one the collector; a
    # resistor is symmetric, so the nets follow the geometry rather than forcing
    # the geometry to follow the schematic's terminal order.
    upper = max(load_terms.values(), key=lambda t: t.center[1])
    lower = min(load_terms.values(), key=lambda t: t.center[1])
    rd_left = _snap(axis - FEED_DX - upper.center[0])
    rd_right = _snap(axis + FEED_DX + upper.center[0])
    rd1_t, rd_box = _place(layout, cell, rd1, rd_left, row_y)
    rd2_t, _ = _place(layout, cell, rd2, rd_right, row_y, orientation="M90")
    instances += [
        (rd1, {upper.name: "nlp1", lower.name: "outp", "sub": "sub"}),
        (rd2, {upper.name: "nlp2", lower.name: "outn", "sub": "sub"}),
    ]
    load_top = _snap(rd_box.top)

    # --- coils, rotated to face each other across the axis -----------------
    # M135 keeps the body to the left and R270 to the right, and both put the
    # supply feed above the nlp feed, so the strap never crosses an nlp run.
    row_y = _snap(load_top + ROW_GAP + 3.05)
    l1 = coil.with_name("l1")
    l2 = coil.with_name("l2")
    l1_t, l1_box = _place(layout, cell, l1, axis - FEED_DX, row_y, orientation="M135")
    l2_t, l2_box = _place(layout, cell, l2, axis + FEED_DX, row_y, orientation="R270")
    instances += [
        (l1, {"PLUS": "vdd", "MINUS": "nlp1", "sub": "sub"}),
        (l2, {"PLUS": "vdd", "MINUS": "nlp2", "sub": "sub"}),
    ]

    # --- supply strap ------------------------------------------------------
    # One straight TopMetal2 run between the two supply feeds. This is the whole
    # point of facing the coils inward: vdd becomes a strap rather than two
    # routes that have to find their way around the coil bodies.
    tm2_w = ROUTE_WIDTHS["TopMetal2"]
    vdd_y = l1_t["PLUS"].center[1]
    _rect(
        layout, cell, "TopMetal2",
        l1_t["PLUS"].center[0] - tm2_w / 2, vdd_y - tm2_w / 2,
        l2_t["PLUS"].center[0] + tm2_w / 2, vdd_y + tm2_w / 2,
    )

    # --- nlp nets: purely vertical, coil feed straight down to the load ----
    _vertical_net(layout, cell, [l1_t["MINUS"], rd1_t[upper.name]], "TopMetal2")
    _vertical_net(layout, cell, [l2_t["MINUS"], rd2_t[upper.name]], "TopMetal2")

    # --- differential nets on Metal3, trunks placed symmetrically ----------
    trunk_out = _snap(array_box.width() * 0.5)
    _trunk_net(
        layout, cell,
        [q1_t["E"], tail_ports["D_A"], rdeg_t["PLUS"]],
        trunk_x=axis - trunk_out, metal=ROUTE_METAL,
    )
    _trunk_net(
        layout, cell,
        [q2_t["E"], tail_ports["D_B"], rdeg_t["MINUS"]],
        trunk_x=axis + trunk_out, metal=ROUTE_METAL,
    )
    _vertical_net(layout, cell, [q1_t["C"], rd1_t[lower.name]], ROUTE_METAL) if (
        q1_t["C"].center[0] == rd1_t[lower.name].center[0]
    ) else _trunk_net(
        layout, cell, [q1_t["C"], rd1_t[lower.name]],
        trunk_x=q1_t["C"].center[0], metal=ROUTE_METAL,
    )
    _trunk_net(
        layout, cell, [q2_t["C"], rd2_t[lower.name]],
        trunk_x=q2_t["C"].center[0], metal=ROUTE_METAL,
    )

    ports = {
        "inp": q1_t["B"],
        "inn": q2_t["B"],
        "outp": q1_t["C"],
        "outn": q2_t["C"],
        "vdd": l1_t["PLUS"],
        "mgate": tail_ports["G_A"],
        "vss": tail_ports["S_A"],
    }

    # Ring the active rows only, stopping below the coils. A substrate ring
    # around an inductor is wrong on its own terms — the coil sits over blocked
    # p-well and wants no ties near it — and wrapping them tripped the p-well
    # block spacing and contact-bar rules against the coil markers.
    active_box = pya.DBox(
        cell.dbbox().left, tail_ports["S_A"].center[1] - 2.0,
        cell.dbbox().right, load_top + 2.0,
    )
    guard = add_guard_ring(layout, cell, active_box, RingSpec(kind="ptap1", clearance=4.0, pitch=2.0))

    for terminal, net in (
        (q1_t["B"], "inp"), (q2_t["B"], "inn"),
        (q1_t["C"], "outp"), (q2_t["C"], "outn"),
        (q1_t["E"], "e1"), (q2_t["E"], "e2"),
        (rd1_t[upper.name], "nlp1"), (rd2_t[upper.name], "nlp2"),
        (l1_t["PLUS"], "vdd"), (l2_t["PLUS"], "vdd"),
        (tail_ports["G_A"], "mgate"), (tail_ports["S_A"], "vss"),
    ):
        stamp_net_labels(layout, cell, [terminal], {terminal.name: net})

    symmetry = {
        "axis_x_um": axis,
        "pairs": {
            "tails": [tail_left, tail_right],
            "hbt": [q_left, q_right],
            "loads": [rd_left, rd_right],
            "coil_feeds": [l1_t["PLUS"].center[0], l2_t["PLUS"].center[0]],
        },
        "coil_feed_dy_um": round(l1_t["PLUS"].center[1] - l1_t["MINUS"].center[1], 4),
    }
    _ = (l1_box, l2_box, load_probe)

    return Block(
        name="ctle_stage",
        layout=layout,
        cell=cell,
        ports=ports,
        instances=instances,
        port_nets=["outp", "outn", "inp", "inn", "vdd", "mgate", "vss"],
        guard=guard,
        symmetry=symmetry,
        notes=[
            f"every row is centred on x={axis:.2f} um and the two halves are "
            "mirror images about it",
            "the coils are rotated to face each other (M135 and R270), which "
            "puts both supply feeds at the same height so vdd is one straight "
            "TopMetal2 strap, with the nlp feeds below it running straight down "
            "to the loads",
            "row order is VDD -> L -> RD -> collector, the working shunt-peaking "
            "order recorded in MEMORY.md",
            "the coils' port capacitance lands on nlp1/nlp2, not on the outputs, "
            "so it must stay out of the C_L budget",
            f"each tail is {array_a.units} strapped single-finger units totalling "
            f"{array_a.total_w * 1e6:.1f} um",
            f"sized from params.inc: RD {p['RPPD_R']:.2f} ohm, RS {p['RS']:.2f} ohm, "
            f"CS {p['CS'] * 1e15:.1f} fF, coil ~{p.get('L_EM', 0) * 1e12:.1f} pH",
        ],
    )


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
    print(f"  placed  {entry['bbox_um']['width']:.1f} x {entry['bbox_um']['height']:.1f} um, "
          f"{len(block.instances)} device instance(s), axis x={block.symmetry['axis_x_um']}")

    cdl = write_block_cdl(
        "ctle_stage", block.port_nets, block.instances, args.out / "ctle_stage.cdl"
    )
    entry["cdl"] = str(cdl)

    if not args.no_render:
        png = render_gds(gds, args.out / "ctle_stage.png", width=1400)
        entry["png"] = str(png) if png else None

    allowed = set(CHIP_LEVEL_ALLOWED)
    drc = run_drc(
        gds=gds, run_dir=args.out / "drc_run", cell_name="ctle_stage", allow_context=True
    )
    remaining = {r: c for r, c in drc.by_rule.items() if r not in allowed}
    drc_ok = not remaining and not drc.error
    entry["drc"] = drc.to_dict()
    entry["drc"]["ok"] = drc_ok
    entry["drc"]["allowed_chip_level_rules"] = sorted(allowed)
    entry["drc"]["enforced_context_rules"] = sorted(set(CONTEXT_RULES) - allowed)
    entry["drc"]["remaining_violations"] = remaining
    chip = {r: c for r, c in drc.by_rule.items() if r in allowed}
    print(f"  DRC     {'ok' if drc_ok else 'FAIL'}  chip-level={chip} remaining={remaining}")

    lvs = run_lvs(
        gds=gds,
        cdl=cdl,
        run_dir=args.out / "lvs_run",
        topcell="ctle_stage",
        disable_tap_extraction=True,
    )
    entry["lvs"] = lvs.to_dict()
    print(f"  LVS     {'ok' if lvs.clean else 'FAIL'}  {lvs.summary[:70]}")

    if not args.no_pex:
        pex = run_magic_pex(gds=gds, cell="ctle_stage", run_dir=args.out / "pex_run")
        entry["pex"] = pex.to_dict()
        if pex.ok:
            signal = signal_resistors(pex.resistor_elements)
            entry["pex"]["signal_resistance_ohm"] = round(sum(e["ohm"] for e in signal), 6)
            print(f"  PEX     ok  {pex.capacitors} C totalling "
                  f"{pex.total_capacitance * 1e15:.2f} fF, {pex.resistors} R")
        else:
            print(f"  PEX     FAIL {pex.error[:60]}")

    (args.out / "ctle_stage_summary.json").write_text(json.dumps(entry, indent=2) + "\n")
    return 0 if (drc_ok and lvs.clean) else 1


if __name__ == "__main__":
    raise SystemExit(main())
