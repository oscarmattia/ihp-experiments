#!/usr/bin/env python3
"""The pad driver stage: symmetric placement, a power ring and EM-sized buses.

The cell is ``driver_dut`` — the same name as the subcircuit in
``circuits/ctle56n/spice/driver_pdk.cir``. ``layout/common/parity.py`` checks
that device for device on every run.

Floorplan, centred on one vertical axis with the two differential halves as exact
mirror images:

              pad_p   pad_n                 bond pads are outp/outn (TopMetal2)
    ╔═════════════╪═══════╪══════════════╗   ring: TopMetal2 horizontal,
    ║  esd esd    │ clamp │    esd esd   ║   TopMetal1 vertical, tapped on
    ║             └── vdd ──┘              ║   the axis
    ║   coil ══════╤═╧╤══════════ coil     ║   M135 / R270, pins inboard,
    ║   body     vdd  nlp        body      ║   vdd above nlp, bodies outboard
    ║              └─┐ └─┐                 ║   rsil loads, mirrored
    ║              rd1  rd2                ║
    ║               Q1 ── Q2               ║   HBT pair, shared emitter em
    ║   ┌───── guard ring (NMOS only) ──┐  ║
    ║   │  diode │      tail             │  ║   mirror off-axis, tail on axis
    ║   └───────────────┬────────────────┘ ║
mgate ─┘                │                    Metal4, out of the left edge
    ╚═══════════════════╪══════════════════╝   vss taps the ring here
                  inp   │   inn                Metal3, out of the bottom edge

There is no degeneration row: both emitters meet on ``em`` and the single tail
sinks the full ``ITAIL``. Outputs leave at the bond pads above the coil row.

Usage:
    python layout/blocks/driver_stage.py
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from layout.blocks.draw import (
    VIA_OFFSET,
    device_bbox_at,
    mirrored_pair_x,
    place,
    poly_contact,
    rect,
    snap,
    via_between,
    via_up,
)
from layout.blocks.generators import Block
from layout.blocks.mos_array import build_mos_array
from layout.blocks.power_ring import add_power_ring
from layout.blocks.stage_gates import run_stage_gates
from layout.common import em
from layout.common.devices import build
from layout.common.gds import stamp_net_labels
from layout.common.guard import RingSpec, add_guard_ring
from layout.common.layers import layer_map
from layout.common.pdk import new_layout, pya_module
from layout.common.rules import min_space, route_width, rule
from layout.common.sizing import metres, read_params
from layout.common.spec import DeviceSpec, Terminal
from layout.common.wrap import derive_terminals

OUT_DIR = Path(__file__).resolve().parent / "out" / "driver_stage"
PARAMS_INC = Path("circuits/ctle56n/spice/driver_params.inc")
SCHEMATIC = Path("circuits/ctle56n/spice/driver_pdk.cir")

CELL = "driver_dut"
PORT_NETS = ["outp", "outn", "inp", "inn", "vdd", "vss", "mgate"]

ROW_GAP = 8.0
COIL_PIN_GAP = 44.0
PWB_TAP_CLEARANCE = 4.0
LOAD_DX = 12.0
HBT_DX = 8.0
OUT_TRUNK_MARGIN = 8.0
BASE_VIA_DX = 2.2
PORT_METAL = "Metal4"
PORT_REACH = 6.0
IN_METAL = "Metal3"
RING_CLEARANCE = 10.0
ARRAY_GAP = 12.0
ROUTE_METAL = "Metal5"
PAD_METAL = "TopMetal2"
CHIP_LEVEL_ALLOWED = ("LBE.a", "LBE.c")

#: Half-separation from the axis to each bond-pad centre, in um. Pads sit
#: outboard of the coil bodies so a 70 um pad does not land on a spiral.
PAD_DX = 48.0

#: Vertical gap from the coil tops to the bond-pad bottoms, in um.
PAD_ROW_GAP = 18.0

#: Gap from a pad edge to the nearest ESD cell, in um.
ESD_OUTBOARD_GAP = 6.0

#: Metal for the pad-side output bus; Metal5 matches the collector trunks and
#: stays clear of inp/inn, which leave on Metal3 below the HBT row.
PAD_BUS_METAL = "Metal5"
_PAD_STACK = ("Metal5", "TopMetal1", "TopMetal2")

#: Bond-pad keepout read from the PDK deck (Pad.fR), not transcribed.
_PAD_KEEPOUT = rule("Pad_fR")
_MOS_W_GRID_M = 5e-9


def _drawn_mos_total_w(requested_w: float) -> float:
    """Total drawn width the PCell reports to LVS (floor per finger, not round).

    ``plan_units`` rounds each finger to the grid; the foundry PCell floors. CDL
    must carry the floored total or the deck rejects ``784.075u`` against ``783.68u``.
    """
    from layout.blocks.mos_array import plan_units

    units, _ = plan_units(requested_w)
    unit_w = math.floor(requested_w / units / _MOS_W_GRID_M) * _MOS_W_GRID_M
    return units * unit_w


def _via_chain(layout, cell, x: float, y: float, metals: tuple[str, ...]) -> None:
    for low, high in zip(metals, metals[1:]):
        via_between(layout, cell, x, y, low, high)


def _hbt_emitter_up(layout, cell, terminal: Terminal, metal: str,
                    outboard_sign: float) -> tuple[float, float]:
    """Raise the emitter on an outboard stub at the emitter row, then drop to ``em_y``.

    A downward diagonal stub from the stacked terminals crosses the base Metal1
    bar at the shared x and LVS merges base into emitter. Take the stub sideways
    only, then use a vertical leg on ``metal``.
    """
    from layout.common.route import metal_of

    tx, ty = terminal.center
    bottom = metal_of(terminal.layer)
    ex = snap(tx + outboard_sign * VIA_OFFSET)
    ey = snap(ty)
    stub_w = route_width(bottom) if bottom else route_width(metal)
    rect(layout, cell, bottom,
         min(tx, ex) - stub_w / 2, ey - stub_w / 2,
         max(tx, ex) + stub_w / 2, ey + stub_w / 2)
    if bottom and bottom != metal:
        via_between(layout, cell, ex, ey, bottom, metal, columns=1, rows=1)
    return (ex, ey)


def _edge_route(layout, cell, metal: str, x: float, y0: float, y1: float, width: float) -> None:
    rect(layout, cell, metal, x - width / 2, min(y0, y1), x + width / 2, max(y0, y1))


def _ring_tie(
    layout,
    cell,
    terminal: Terminal,
    ring_y: float,
    tap_x: float,
    pin_metal: str,
    route_metal: str,
    width: float,
) -> None:
    """Reach a ring side port: horizontal at the pin, vertical at ``tap_x`` only."""
    tx, ty = via_up(layout, cell, terminal, pin_metal)
    rect(layout, cell, pin_metal, min(tx, tap_x) - width / 2, ty - width / 2,
          max(tx, tap_x) + width / 2, ty + width / 2)
    if pin_metal != route_metal:
        via_between(layout, cell, tap_x, ty, pin_metal, route_metal)
    _edge_route(layout, cell, route_metal, tap_x, ty, ring_y, width)
    via_between(layout, cell, tap_x, ring_y, route_metal, "TopMetal1")


def _supply_ring_tie(
    layout,
    cell,
    terminal: Terminal,
    feed_y: float,
    ring_y: float,
    tap_x: float,
    pin_metal: str,
    route_metal: str,
    width: float,
    *,
    ring_port_x: float,
    inboard_sign: float,
) -> None:
    """Reach a ring side port; via stack sits on a stub outside the port pin."""
    tx, ty = via_up(layout, cell, terminal, pin_metal)
    vert_metal = route_metal
    if pin_metal != route_metal:
        via_between(layout, cell, tx, ty, pin_metal, route_metal)
    _edge_route(layout, cell, vert_metal, tx, ty, feed_y, width)
    stub_x = snap(ring_port_x + inboard_sign * (VIA_OFFSET + 3.0 * route_width(route_metal)))
    rect(layout, cell, route_metal, min(tx, stub_x) - width / 2, feed_y - width / 2,
          max(tx, stub_x) + width / 2, feed_y + width / 2)
    _edge_route(layout, cell, route_metal, stub_x, feed_y, ring_y, width)
    via_between(layout, cell, stub_x, feed_y, route_metal, "TopMetal1", columns=1, rows=1)
    tm1_w = route_width("TopMetal1")
    _edge_route(layout, cell, "TopMetal1", stub_x, feed_y, ring_y, tm1_w)
    rect(layout, cell, "TopMetal1", min(stub_x, ring_port_x) - tm1_w / 2, ring_y - tm1_w / 2,
          max(stub_x, ring_port_x) + tm1_w / 2, ring_y + tm1_w / 2)


def _collector_trunk_net(
    layout,
    cell,
    terminals: list[Terminal],
    trunk_x: float,
    em_y: float,
    metal: str,
    width: float,
) -> tuple[float, float]:
    """Vertical collector trunk outboard of ``em``, with joins above the emitter bus.

    ``trunk_net`` stubs at each terminal's y; on an HBT that y sits on the same
    Metal5 row as the shared emitter strap, so the collector width envelope crosses
    ``em_y`` and LVS merges ``outp`` into ``em``. A via stack's Metal5 landing pad
    does the same if it is dropped beside the collector land. Rise on the device
    pin metal, transition to ``metal`` only at ``safe_y`` above the emitter strap.
    """
    from layout.common.route import metal_of

    w = width
    trunk_x = snap(trunk_x)
    safe_y = snap(em_y + w + 2.0)
    rise_w = route_width("Metal2")
    top_y = safe_y
    for terminal in terminals:
        tx, ty = terminal.center
        tx, ty = snap(tx), snap(ty)
        bottom = metal_of(terminal.layer)
        stub_x = snap(tx - VIA_OFFSET if trunk_x < tx else tx + VIA_OFFSET)
        top_y = snap(max(top_y, ty))
        if bottom:
            bw = route_width(bottom)
            rect(layout, cell, bottom, min(tx, stub_x), ty - bw / 2,
                  max(tx, stub_x), ty + bw / 2)
            if bottom != "Metal2":
                via_between(layout, cell, stub_x, ty, bottom, "Metal2", columns=1, rows=1)
        if ty < safe_y - 1e-6:
            rect(layout, cell, "Metal2", stub_x - rise_w / 2, ty,
                  stub_x + rise_w / 2, safe_y)
        elif ty > safe_y + 1e-6:
            rect(layout, cell, "Metal2", stub_x - rise_w / 2, safe_y,
                  stub_x + rise_w / 2, ty)
        via_between(layout, cell, stub_x, safe_y, "Metal2", metal, columns=1, rows=1)
        rect(layout, cell, metal, min(stub_x, trunk_x), safe_y - w / 2,
              max(stub_x, trunk_x), safe_y + w / 2)
    rect(layout, cell, metal, trunk_x - w / 2, safe_y, trunk_x + w / 2, top_y)
    return (safe_y, top_y)


def build_driver_stage(params: dict[str, float] | None = None,
                       black_box: tuple[str, ...] = (),
                       *,
                       with_loads: bool = True,
                       with_coils: bool = True,
                       with_tapeout: bool = True,
                       with_pads: bool = True,
                       with_pad_feed: bool = True,
                       with_esd: bool = True) -> Block:
    """Place and wire one pad driver stage."""
    from layout.devices.catalog import COIL, driver_devices, esd_devices

    p = params or read_params(PARAMS_INC)
    catalog = {spec.name: spec for spec in driver_devices(p) + esd_devices()}
    bb_kinds = set(black_box)
    pya = pya_module()
    lm = layer_map()

    coil = DeviceSpec(
        name="inductor_turn1_d40",
        kind="inductor",
        params=dict(COIL),
        note="shunt-peaking coil at the EM-characterized geometry (driver_params.inc)",
    )
    hbt = catalog["npn13G2_driver"]
    esd_vdd_spec = catalog["esd_diodevdd_2kv"]
    esd_vss_spec = catalog["esd_diodevss_2kv"]
    clamp_spec = catalog["esd_nmoscl_2"]
    pad_spec = catalog["bondpad_70um"]

    load = DeviceSpec(
        name="rsil_load",
        kind="rsil",
        params={"w": p["RSIL_W"], "l": p["RSIL_L"]},
        note=f"Pad driver shunt load, RD={p['RD_ON_CHIP']:.0f} ohm",
    )

    i_tail = float(p["ITAIL"])
    i_mirror = i_tail / 2.0
    i_supply = i_tail + i_mirror
    mirror_w = metres(p, "MOS_W")
    mirror_l = metres(p, "MOS_L")
    tail_w = p["TAIL_W_m"]
    tail_w_drawn = _drawn_mos_total_w(tail_w)

    layout = new_layout()
    cell = layout.create_cell(CELL)
    instances: list[tuple[DeviceSpec, dict[str, str]]] = []
    em_segments: list[em.Segment] = []

    # --- NMOS row: mirror diode left, single tail centred on the axis --------
    arrays = {
        "mdiode": build_mos_array("mdiode", mirror_w, mirror_l, current_a=i_mirror),
        "tail": build_mos_array("tail", tail_w_drawn, mirror_l, current_a=i_tail),
    }
    mirror_box = arrays["mdiode"].cell.dbbox()
    tail_box = arrays["tail"].cell.dbbox()
    mirror_w_box = mirror_box.width()
    tail_w_box = tail_box.width()
    rail_w = arrays["tail"].rail_width_um
    device_span = tail_w_box - 2 * rail_w

    placement = {"mdiode": 0.0}
    placement["tail"] = snap(mirror_w_box + ARRAY_GAP)
    axis = snap(placement["tail"] + rail_w + device_span / 2.0)

    nmos_ports: dict[str, Terminal] = {}
    for name, array in arrays.items():
        dx = placement[name]
        index = layout.add_cell(f"{name}_cell")
        layout.cell(index).copy_tree(array.cell)
        trans = pya.DTrans(pya.DVector(dx, 0.0))
        cell.insert(pya.DCellInstArray(index, trans))
        for pin, terminal in array.ports.items():
            point = trans * pya.DPoint(*terminal.center)
            nmos_ports[f"{pin}_{name}"] = Terminal(
                name=f"{pin}_{name}",
                layer=terminal.layer,
                center=(snap(point.x), snap(point.y)),
                width=terminal.width,
                orientation=terminal.orientation,
            )
        em_segments += array.em_segments

    instances += [
        (arrays["mdiode"].total_spec.with_name("mdiode"),
         {"D": "mgate", "G": "mgate", "S": "vss", "sub": "vss"}),
        (DeviceSpec(
            name="tail",
            kind=arrays["tail"].total_spec.kind,
            params={**arrays["tail"].total_spec.params, "w": tail_w_drawn},
            note=arrays["tail"].total_spec.note,
        ), {"D": "em", "G": "mgate", "S": "vss", "sub": "vss"}),
    ]

    nmos_left = snap(min(placement.values()) - 1.0)
    nmos_right = snap(max(placement.values()) + tail_w_box + 1.0)

    vss_rail_w = snap(max(em.width_for_a("Metal2", i_supply), route_width("Metal2")))
    source_rail_top = snap(
        nmos_ports["S_tail"].center[1] + arrays["tail"].rail_width_um / 2
    )
    vss_rail_bottom = snap(source_rail_top - vss_rail_w)
    vss_rail_y = snap(source_rail_top - vss_rail_w / 2)
    rect(layout, cell, "Metal2", nmos_left, vss_rail_bottom, nmos_right, source_rail_top)
    em_segments.append(em.Segment("vss.rail", "Metal2", width_um=vss_rail_w,
                                  current_a=i_supply, note="shared source rail"))

    gate_y = nmos_ports["G_tail"].center[1]
    poly = lm["gatpoly_drw"]
    cell.shapes(layout.layer(poly[0], poly[1])).insert(
        pya.DBox(nmos_left, snap(gate_y - 0.3), nmos_right, snap(gate_y + 0.3))
    )

    diode_right = snap(placement["mdiode"] + mirror_box.right)
    tail1_left = snap(placement["tail"] + tail_box.left)
    channel_x = snap((diode_right + tail1_left) / 2.0)
    gate_tap = poly_contact(layout, cell, channel_x, gate_y)
    via_between(layout, cell, channel_x, gate_y, "Metal1", "Metal2", columns=1, rows=1)

    diode_rail = nmos_ports["D_mdiode"]
    link_x = snap(diode_right - arrays["mdiode"].rail_width_um / 2)
    link_w = route_width("Metal2")
    rect(layout, cell, "Metal2", link_x - link_w / 2, gate_y,
          link_x + link_w / 2, diode_rail.center[1])
    rect(layout, cell, "Metal2", min(link_x, channel_x), gate_y - link_w / 2,
          max(link_x, channel_x), gate_y + link_w / 2)

    nmos_box = pya.DBox(nmos_left, vss_rail_bottom, nmos_right, snap(tail_box.top))
    guard = add_guard_ring(layout, cell, nmos_box, RingSpec(kind="ptap1", clearance=2.0))
    guard_box = guard["outer_box_um"]
    tap_x, tap_y = min(
        guard["tap_centres_um"],
        key=lambda c: (abs(c[1] - guard_box[1]) > 0.1, abs(c[0] - nmos_left)),
    )
    via_between(layout, cell, tap_x, tap_y, "Metal1", "Metal2", columns=1, rows=1)
    rect(layout, cell, "Metal2",
          tap_x - vss_rail_w / 2, tap_y, tap_x + vss_rail_w / 2, vss_rail_y)

    nmos_top = snap(max(tail_box.top, guard_box[3]))

    # --- HBT pair, shared emitter ------------------------------------------------
    row_y = snap(nmos_top + ROW_GAP)
    q1 = hbt.with_name("q1")
    q2 = hbt.with_name("q2")
    q_left, q_right = mirrored_pair_x(hbt, axis, gap=2 * HBT_DX)
    q1_t, q_box = place(layout, cell, q1, q_left, row_y)
    q2_t, _ = place(layout, cell, q2, q_right, row_y, "M90")
    instances += [
        (q1, {"C": "outp", "B": "inp", "E": "em", "sub": "vss"}),
        (q2, {"C": "outn", "B": "inn", "E": "em", "sub": "vss"}),
    ]
    hbt_top = snap(q_box.top)

    # --- rsil loads near the axis ------------------------------------------------
    rd1_t: dict[str, Terminal] = {}
    rd2_t: dict[str, Terminal] = {}
    load_top = hbt_top
    upper = lower = None
    if with_loads:
        row_y = snap(hbt_top + ROW_GAP)
        rd1 = load.with_name("rd1")
        rd2 = load.with_name("rd2")
        load_terms = {t.name: t for t in derive_terminals(load, *build(load))}
        upper = max(load_terms.values(), key=lambda t: t.center[1])
        lower = min(load_terms.values(), key=lambda t: t.center[1])
        rd_left = snap(axis - LOAD_DX - upper.center[0])
        rd_right = snap(axis + LOAD_DX + upper.center[0])
        rd1_t, rd_box = place(layout, cell, rd1, rd_left, row_y)
        rd2_t, _ = place(layout, cell, rd2, rd_right, row_y, "M90")
        instances += [
            (rd1, {upper.name: "nlp1", lower.name: "outp", "sub": "vss"}),
            (rd2, {upper.name: "nlp2", lower.name: "outn", "sub": "vss"}),
        ]
        load_top = snap(rd_box.top)

    # --- coils, facing each other ------------------------------------------------
    l1_t: dict[str, Terminal] = {}
    l2_t: dict[str, Terminal] = {}
    l1_box = l2_box = pya.DBox(0, 0, 0, 0)
    coil_top = load_top
    channel = (snap(axis - COIL_PIN_GAP / 2), snap(axis + COIL_PIN_GAP / 2))
    strap_w = route_width("TopMetal2")
    vdd_y = snap(load_top + ROW_GAP)
    interconnect_um = 0.0
    coil_half_h = 0.0
    coil_row_y = snap(load_top + ROW_GAP)
    coil_bb = False
    rd_left = rd_right = snap(axis)
    if with_coils:
        _, coil_probe = build(coil)
        coil_probe_box = coil_probe.dbbox()
        coil_half_h = snap(coil_probe_box.width() / 2.0)
        row_y = snap(max(load_top + ROW_GAP, hbt_top + coil_half_h + PWB_TAP_CLEARANCE))
        l1 = coil.with_name("l1")
        l2 = coil.with_name("l2")
        l1_dx = snap(axis - COIL_PIN_GAP / 2)
        l2_dx = snap(axis + COIL_PIN_GAP / 2)
        coil_bb = l1.kind in bb_kinds
        coil_row_y = row_y
        l1_t, l1place_box = place(
            layout, cell, l1, l1_dx, row_y, "M135", black_box=coil_bb,
        )
        l2_t, l2place_box = place(
            layout, cell, l2, l2_dx, row_y, "R270", black_box=coil_bb,
        )
        if coil_bb:
            l1_box = device_bbox_at(l1, l1_dx, row_y, "M135")
            l2_box = device_bbox_at(l2, l2_dx, row_y, "R270")
        else:
            l1_box, l2_box = l1place_box, l2place_box
        instances += [
            (l1, {"PLUS": "vdd", "MINUS": "nlp1", "sub": "vss"}),
            (l2, {"PLUS": "vdd", "MINUS": "nlp2", "sub": "vss"}),
        ]
        channel = (snap(l1_box.right), snap(l2_box.left))
        coil_top = snap(max(l1_box.top, l2_box.top))

        strap_w = snap(l1_t["PLUS"].width)
        vdd_y = l1_t["PLUS"].center[1]
        rect(layout, cell, "TopMetal2",
              l1_t["PLUS"].center[0], vdd_y - strap_w / 2,
              l2_t["PLUS"].center[0], vdd_y + strap_w / 2)
        em_segments.append(em.Segment("vdd.strap", "TopMetal2", width_um=strap_w,
                                        current_a=i_supply, note="between the coil supply feeds"))

        if with_loads and upper is not None and lower is not None:
            nlp_w = snap(l1_t["MINUS"].width)
            for coil_pin, load_pin, name, turn_x in (
                (l1_t["MINUS"], rd1_t[upper.name], "nlp1", snap(axis - LOAD_DX)),
                (l2_t["MINUS"], rd2_t[upper.name], "nlp2", snap(axis + LOAD_DX)),
            ):
                feed_x, feed_y = coil_pin.center
                land_x, land_y = via_up(layout, cell, load_pin, "TopMetal2")
                rect(layout, cell, "TopMetal2", min(feed_x, turn_x), feed_y - nlp_w / 2,
                      max(feed_x, turn_x), feed_y + nlp_w / 2)
                rect(layout, cell, "TopMetal2", turn_x - nlp_w / 2, min(feed_y, land_y),
                      turn_x + nlp_w / 2, max(feed_y, land_y))
                if abs(turn_x - land_x) > 1e-6:
                    rect(layout, cell, "TopMetal2", min(turn_x, land_x), land_y - nlp_w / 2,
                          max(turn_x, land_x), land_y + nlp_w / 2)
                interconnect_um += abs(feed_x - turn_x) + abs(feed_y - land_y)
                em_segments.append(
                    em.Segment(name, "TopMetal2", width_um=nlp_w, current_a=i_tail,
                               note="coil feed continued inward, then down to the load")
                )

    sig_w = snap(max(em.width_for_a(ROUTE_METAL, i_tail), route_width(ROUTE_METAL)))

    # --- base taps (sideways, before em) -----------------------------------------
    in_trunk_x: dict[str, float] = {}
    for base, sign, name in ((q1_t["B"], -1.0, "inp"), (q2_t["B"], -1.0, "inn")):
        bx, by = base.center
        vx = snap(bx + sign * BASE_VIA_DX)
        stub_h = snap(base.width if base.width < 1.0 else route_width("Metal1"))
        rect(layout, cell, "Metal1", min(bx, vx), by - stub_h / 2, max(bx, vx), by + stub_h / 2)
        via_between(layout, cell, vx, by, "Metal1", IN_METAL, columns=1, rows=1)
        in_trunk_x[name] = vx

    # --- shared emitter on Metal5 (VGA gap routing) ------------------------------
    # A single horizontal bus at the emitter row must stay outboard of the inp/inn
    # trunks and leave a bridge between them for the centred tail.
    em_y = snap(q1_t["E"].center[1])
    q1x, _ = _hbt_emitter_up(layout, cell, q1_t["E"], ROUTE_METAL, outboard_sign=-1.0)
    q2x, _ = _hbt_emitter_up(layout, cell, q2_t["E"], ROUTE_METAL, outboard_sign=+1.0)
    corridor_left = snap(in_trunk_x["inp"] + sig_w / 2)
    corridor_right = snap(in_trunk_x["inn"] - sig_w / 2)
    gap_left = snap(in_trunk_x["inp"] - sig_w)
    gap_right = snap(in_trunk_x["inn"] + sig_w)
    rect(layout, cell, ROUTE_METAL, min(q1x, gap_left) - sig_w / 2, em_y - sig_w / 2,
          max(q1x, gap_left) + sig_w / 2, em_y + sig_w / 2)
    rect(layout, cell, ROUTE_METAL, min(gap_right, q2x) - sig_w / 2, em_y - sig_w / 2,
          max(gap_right, q2x) + sig_w / 2, em_y + sig_w / 2)
    rect(layout, cell, ROUTE_METAL, q1x - sig_w / 2, min(q1_t["E"].center[1], em_y),
          q1x + sig_w / 2, max(q1_t["E"].center[1], em_y))
    rect(layout, cell, ROUTE_METAL, q2x - sig_w / 2, min(q2_t["E"].center[1], em_y),
          q2x + sig_w / 2, max(q2_t["E"].center[1], em_y))
    rect(layout, cell, ROUTE_METAL, corridor_left - sig_w / 2, em_y - sig_w / 2,
          corridor_right + sig_w / 2, em_y + sig_w / 2)
    rect(layout, cell, ROUTE_METAL, min(q1x, corridor_left) - sig_w / 2, em_y - sig_w / 2,
          max(q1x, corridor_left) + sig_w / 2, em_y + sig_w / 2)
    rect(layout, cell, ROUTE_METAL, min(corridor_right, q2x) - sig_w / 2, em_y - sig_w / 2,
          max(corridor_right, q2x) + sig_w / 2, em_y + sig_w / 2)
    tail_x, tail_y = via_up(layout, cell, nmos_ports["D_tail"], ROUTE_METAL)
    rect(layout, cell, ROUTE_METAL, tail_x - sig_w / 2, min(tail_y, em_y),
          tail_x + sig_w / 2, max(tail_y, em_y))
    em_segments.append(em.Segment("em", ROUTE_METAL, width_um=sig_w, current_a=i_tail,
                                  note="shared emitter strap with inp/inn corridor gap"))

    em_left_extent = snap(min(q1x, gap_left))
    em_right_extent = snap(max(q2x, gap_right))
    out_trunk_p_x = snap(em_left_extent - sig_w - OUT_TRUNK_MARGIN)
    out_trunk_n_x = snap(em_right_extent + sig_w + OUT_TRUNK_MARGIN)

    pad_ports: dict[str, Terminal] = {}
    pad_terminals: dict[str, Terminal] = {}
    pad_cx_by_net = {"outp": snap(axis - PAD_DX), "outn": snap(axis + PAD_DX)}
    esd_terms: dict[str, tuple[dict[str, Terminal], dict[str, Terminal]]] = {}
    clamp_t = None
    pad_half = 35.0
    pad_bottom_y = snap(coil_top + PAD_ROW_GAP)
    pad_top_y = pad_bottom_y + 70.0
    esd_w = 0.0
    esd_h = 0.0
    esd_bus_w = snap(max(route_width("Metal2") * 3.0, 2.0))
    tm2_feed_w = route_width("TopMetal2")

    def _collector_trunk_targets(pad_net: str) -> list[Terminal]:
        targets = [q1_t["C"] if pad_net == "outp" else q2_t["C"]]
        if with_loads and lower is not None:
            targets.append(rd1_t[lower.name] if pad_net == "outp" else rd2_t[lower.name])
        return targets

    if with_tapeout:
        # --- rail clamp in the coil channel --------------------------------------------
        _, clamp_probe = build(clamp_spec)
        clamp_bbox = clamp_probe.dbbox()
        clamp_dx = snap(axis - clamp_bbox.width() / 2.0)
        clamp_dy = snap(coil_row_y - coil_half_h + clamp_bbox.height() / 2.0 + ROW_GAP)
        clamp_t, _ = place(layout, cell, clamp_spec.with_name("clamp"), clamp_dx, clamp_dy)
        instances.append((clamp_spec.with_name("clamp"), {"VDD": "vdd", "VSS": "vss"}))

        # --- bond pads and ESD above the coil row ------------------------------------
        if with_pads:
            _, pad_probe = build(pad_spec)
            pad_bbox = pad_probe.dbbox()
            pad_half = snap(pad_bbox.width() / 2.0)
            pad_bottom_y = snap(coil_top + PAD_ROW_GAP)
            pad_top_y = snap(pad_bottom_y + pad_bbox.height())
            stack_y = snap(pad_bottom_y - _PAD_KEEPOUT)
            bus_y_nom = snap(stack_y - sig_w)

            _, esd_vdd_probe = build(esd_vdd_spec)
            esd_bbox = esd_vdd_probe.dbbox()
            esd_w = esd_bbox.width()
            esd_h = snap(esd_bbox.height())
            esd_row_y = snap(pad_bottom_y - ESD_OUTBOARD_GAP - esd_h)

            if with_coils:
                pad_clear = snap(_PAD_KEEPOUT + tm2_feed_w)
                pad_cx_by_net = {
                    "outp": snap(l1_box.left - pad_clear - pad_half),
                    "outn": snap(l2_box.right + pad_clear + pad_half),
                }
            else:
                pad_cx_by_net = {
                    "outp": snap(axis - PAD_DX),
                    "outn": snap(axis + PAD_DX),
                }

            for side, sign, pad_net, esd_suffix in (
                ("p", -1.0, "outp", "p"),
                ("n", +1.0, "outn", "n"),
            ):
                pad_cx = pad_cx_by_net[pad_net]
                trunk_x = out_trunk_p_x if pad_net == "outp" else out_trunk_n_x
                inboard_x = snap(pad_cx - sign * pad_half)
                tm2_x = snap(inboard_x + sign * (_PAD_KEEPOUT + tm2_feed_w / 2))
                pad_anchor_y = snap(pad_bottom_y + pad_half)

                pad_t, pad_box = place(
                    layout, cell, pad_spec.with_name(f"pad_{side}"), pad_cx, pad_anchor_y,
                )
                pad_terminals[pad_net] = pad_t["PAD"]
                pad_ports[pad_net] = Terminal(
                    name=pad_net,
                    layer="topmetal2_drw",
                    center=(pad_cx, pad_top_y),
                    width=snap(pad_bbox.width()),
                    orientation=90.0,
                )

                evdd = esd_vdd_spec.with_name(f"esd_vdd_{esd_suffix}")
                evss = esd_vss_spec.with_name(f"esd_vss_{esd_suffix}")
                esd_row_y = snap(pad_box.bottom - ESD_OUTBOARD_GAP - esd_h)
                if with_esd:
                    if sign < 0:
                        evss_dx = snap(pad_box.left - ESD_OUTBOARD_GAP - esd_w)
                        evdd_dx = snap(evss_dx - ESD_OUTBOARD_GAP - esd_w)
                    else:
                        evdd_dx = snap(pad_box.right + ESD_OUTBOARD_GAP)
                        evss_dx = snap(evdd_dx + esd_w + ESD_OUTBOARD_GAP)
                    evdd_t, _ = place(layout, cell, evdd, evdd_dx, esd_row_y)
                    evss_t, _ = place(layout, cell, evss, evss_dx, esd_row_y)
                    instances += [
                        (evdd, {"VDD": "vdd", "PAD": pad_net, "VSS": "vss"}),
                        (evss, {"VDD": "vdd", "PAD": pad_net, "VSS": "vss"}),
                    ]
                    esd_terms[pad_net] = (evdd_t, evss_t)

                if not with_pad_feed or not with_esd:
                    continue

                evdd_t, evss_t = esd_terms[pad_net]
                _, trunk_top = _collector_trunk_net(
                    layout, cell, _collector_trunk_targets(pad_net),
                    trunk_x=trunk_x, em_y=em_y, metal=ROUTE_METAL, width=sig_w,
                )
                bus_y = snap(max(trunk_top + sig_w, bus_y_nom))

                _edge_route(layout, cell, ROUTE_METAL, trunk_x, trunk_top, bus_y, sig_w)
                rect(layout, cell, ROUTE_METAL,
                      min(trunk_x, inboard_x) - sig_w / 2, bus_y - sig_w / 2,
                      max(trunk_x, inboard_x) + sig_w / 2, bus_y + sig_w / 2)
                _edge_route(layout, cell, ROUTE_METAL, inboard_x, bus_y, stack_y, sig_w)
                _via_chain(layout, cell, inboard_x, stack_y, _PAD_STACK)
                _edge_route(layout, cell, "TopMetal2", tm2_x, stack_y, pad_bottom_y, tm2_feed_w)
                rect(layout, cell, "TopMetal2",
                      min(tm2_x, inboard_x) - tm2_feed_w / 2, pad_bottom_y - tm2_feed_w / 2,
                      max(tm2_x, inboard_x) + tm2_feed_w / 2, pad_bottom_y + tm2_feed_w / 2)

                esd_bus_y = snap(bus_y - sig_w - esd_bus_w)
                for esd_t in (evdd_t, evss_t):
                    px, py = via_up(layout, cell, esd_t["PAD"], ROUTE_METAL)
                    _edge_route(layout, cell, ROUTE_METAL, px, py, esd_bus_y, sig_w)
                    if abs(px - inboard_x) > 1e-6:
                        rect(layout, cell, ROUTE_METAL,
                              min(px, inboard_x) - sig_w / 2, esd_bus_y - sig_w / 2,
                              max(px, inboard_x) + sig_w / 2, esd_bus_y + sig_w / 2)
                if esd_bus_y < bus_y - 1e-6:
                    _edge_route(layout, cell, ROUTE_METAL, inboard_x, esd_bus_y, bus_y, sig_w)
        else:
            _, pad_probe = build(pad_spec)
            pad_bbox = pad_probe.dbbox()
            pad_half = snap(pad_bbox.width() / 2.0)
            pad_bottom_y = snap(coil_top + PAD_ROW_GAP)
            pad_top_y = snap(pad_bottom_y + pad_bbox.height())
            for pad_net, trunk_x in (("outp", out_trunk_p_x), ("outn", out_trunk_n_x)):
                _collector_trunk_net(
                    layout, cell, _collector_trunk_targets(pad_net),
                    trunk_x=trunk_x, em_y=em_y, metal=ROUTE_METAL, width=sig_w,
                )
    else:
        for pad_net, trunk_x in (("outp", out_trunk_p_x), ("outn", out_trunk_n_x)):
            _collector_trunk_net(
                layout, cell, _collector_trunk_targets(pad_net),
                trunk_x=trunk_x, em_y=em_y, metal=ROUTE_METAL, width=sig_w,
            )

    em_segments += [
        em.Segment(net, ROUTE_METAL, width_um=sig_w, current_a=i_tail, note="collector run")
        for net in ("outp", "outn")
    ]

    # --- power ring --------------------------------------------------------------
    devices_box = cell.dbbox()
    if with_coils and coil_bb:
        devices_box = devices_box + l1_box + l2_box
    if with_tapeout and with_pads:
        pad_envelope = pya.DBox(
            snap(min(pad_cx_by_net["outp"], pad_cx_by_net["outn"]) - pad_half - 2 * esd_w - ESD_OUTBOARD_GAP),
            snap(pad_bottom_y - ESD_OUTBOARD_GAP - esd_h) if esd_h else pad_bottom_y,
            snap(max(pad_cx_by_net["outp"], pad_cx_by_net["outn"]) + pad_half + 2 * esd_w + ESD_OUTBOARD_GAP),
            pad_top_y,
        )
        devices_box = devices_box + pad_envelope
    ring_half = snap(max(axis - devices_box.left, devices_box.right - axis))
    ring = add_power_ring(
        layout, cell,
        pya.DBox(snap(axis - ring_half), devices_box.bottom,
                 snap(axis + ring_half), devices_box.top),
        currents={"vss": i_supply, "vdd": i_supply},
        clearance=RING_CLEARANCE,
    )
    em_segments += ring.em_segments

    vss_ring_y = ring.ports["vss"][1].center[1]
    via_between(layout, cell, axis, vss_rail_y, "Metal2", "TopMetal2", columns=3, rows=3)
    rect(layout, cell, "TopMetal2", axis - strap_w / 2, vss_ring_y,
          axis + strap_w / 2, vss_rail_y)

    vdd_ring_y = ring.ports["vdd"][0].center[1]
    via_between(layout, cell, axis, vdd_y, "TopMetal1", "TopMetal2", columns=1, rows=1)
    rect(layout, cell, "TopMetal1", axis - strap_w / 2, vdd_y, axis + strap_w / 2, vdd_ring_y)
    via_between(layout, cell, axis, vdd_ring_y, "TopMetal1", "TopMetal2", columns=1, rows=1)

    em_segments += [
        em.Segment("vss.riser", "TopMetal2", width_um=strap_w, current_a=i_supply,
                   note="source rail down to the ring, on the axis"),
        em.Segment("vdd.riser", "TopMetal1", width_um=strap_w, current_a=i_supply,
                   note="coil strap up to the ring, crossing under the vss run"),
    ]

    ring_box = ring.outer_box
    m3_w = route_width("Metal3")
    m3_sep = min_space("Metal3")
    esd_vdd_feed_y = (
        snap(esd_row_y - esd_bus_w - ROW_GAP / 2.0)
        if with_tapeout and with_esd and esd_h
        else snap(coil_top + ROW_GAP)
    )
    esd_vss_feed_y = snap(esd_vdd_feed_y - max(m3_sep, 3.0))

    if clamp_t is not None:
        clamp_vdd_x, clamp_vdd_y = clamp_t["VDD"].center
        vdd_stub_x, vdd_stub_y = via_up(layout, cell, clamp_t["VDD"], "Metal3")
        _edge_route(layout, cell, "Metal3", clamp_vdd_x, vdd_stub_y, vdd_y, m3_w)
        if abs(vdd_stub_x - clamp_vdd_x) > 1e-6:
            rect(layout, cell, "Metal3", min(vdd_stub_x, clamp_vdd_x) - m3_w / 2, vdd_stub_y - m3_w / 2,
                  max(vdd_stub_x, clamp_vdd_x) + m3_w / 2, vdd_stub_y + m3_w / 2)
        if abs(clamp_vdd_x - axis) > 1e-6:
            rect(layout, cell, "Metal3", min(clamp_vdd_x, axis) - m3_w / 2, vdd_y - m3_w / 2,
                  max(clamp_vdd_x, axis) + m3_w / 2, vdd_y + m3_w / 2)
        _via_chain(layout, cell, axis, vdd_y, ("Metal3", "Metal4", "Metal5", "TopMetal1", "TopMetal2"))
        rect(layout, cell, "TopMetal2", min(axis, clamp_vdd_x) - strap_w / 2, vdd_y - strap_w / 2,
              max(axis, clamp_vdd_x) + strap_w / 2, vdd_y + strap_w / 2)

        clamp_vss_x, clamp_vss_y = clamp_t["VSS"].center
        vss_stub_x, vss_stub_y = via_up(layout, cell, clamp_t["VSS"], "Metal3")
        _edge_route(layout, cell, "Metal3", clamp_vss_x, vss_stub_y, vss_rail_y, m3_w)
        if abs(vss_stub_x - clamp_vss_x) > 1e-6:
            rect(layout, cell, "Metal3", min(vss_stub_x, clamp_vss_x) - m3_w / 2, vss_stub_y - m3_w / 2,
                  max(vss_stub_x, clamp_vss_x) + m3_w / 2, vss_stub_y + m3_w / 2)
        via_between(layout, cell, clamp_vss_x, vss_rail_y, "Metal3", "Metal2", columns=2, rows=2)
        rect(layout, cell, "Metal2", clamp_vss_x - vss_rail_w / 2, vss_rail_y - vss_rail_w / 2,
              nmos_right + vss_rail_w / 2, vss_rail_y + vss_rail_w / 2)

    for pad_net, (evdd_t, evss_t) in esd_terms.items():
        if pad_net == "outp":
            vdd_port = ring.ports["vdd"][2]
            vss_port = ring.ports["vss"][2]
            inboard_sign = 1.0
        else:
            vdd_port = ring.ports["vdd"][3]
            vss_port = ring.ports["vss"][3]
            inboard_sign = -1.0
        evdd_x, evdd_y = via_up(layout, cell, evdd_t["VDD"], "Metal2")
        evss_vdd_x, evss_vdd_y = via_up(layout, cell, evss_t["VDD"], "Metal2")
        vdd_link_y = snap(max(evdd_y, evss_vdd_y) + esd_bus_w)
        _edge_route(layout, cell, "Metal2", evdd_x, evdd_y, vdd_link_y, esd_bus_w)
        _edge_route(layout, cell, "Metal2", evss_vdd_x, evss_vdd_y, vdd_link_y, esd_bus_w)
        rect(layout, cell, "Metal2", min(evdd_x, evss_vdd_x) - esd_bus_w / 2, vdd_link_y - esd_bus_w / 2,
              max(evdd_x, evss_vdd_x) + esd_bus_w / 2, vdd_link_y + esd_bus_w / 2)
        _supply_ring_tie(
            layout, cell, evdd_t["VDD"], esd_vdd_feed_y, vdd_port.center[1], vdd_port.center[0],
            "Metal2", "Metal2", esd_bus_w,
            ring_port_x=vdd_port.center[0], inboard_sign=inboard_sign,
        )
        evdd_vss_x, evdd_vss_y = via_up(layout, cell, evdd_t["VSS"], "Metal3")
        evss_x, evss_y = via_up(layout, cell, evss_t["VSS"], "Metal3")
        vss_link_y = snap(min(evdd_vss_y, evss_y) - esd_bus_w)
        _edge_route(layout, cell, "Metal3", evdd_vss_x, evdd_vss_y, vss_link_y, esd_bus_w)
        _edge_route(layout, cell, "Metal3", evss_x, evss_y, vss_link_y, esd_bus_w)
        rect(layout, cell, "Metal3", min(evdd_vss_x, evss_x) - esd_bus_w / 2, vss_link_y - esd_bus_w / 2,
              max(evdd_vss_x, evss_x) + esd_bus_w / 2, vss_link_y + esd_bus_w / 2)
        _supply_ring_tie(
            layout, cell, evss_t["VSS"], esd_vss_feed_y, vss_port.center[1], vss_port.center[0],
            "Metal2", "Metal3", esd_bus_w,
            ring_port_x=vss_port.center[0], inboard_sign=inboard_sign,
        )

    # --- signal ports ------------------------------------------------------------
    port_bottom = snap(ring_box[1] - PORT_REACH)
    port_top = snap(ring_box[3] + PORT_REACH)
    signal_ports: dict[str, Terminal] = dict(pad_ports)
    if not signal_ports:
        for name, trunk_x in (("outp", out_trunk_p_x), ("outn", out_trunk_n_x)):
            signal_ports[name] = Terminal(
                name=name, layer=f"{PORT_METAL.lower()}_drw",
                center=(trunk_x, port_top), width=sig_w, orientation=90.0,
            )

    for name, x, y_from, y_to in (
        ("inp", in_trunk_x["inp"], q1_t["B"].center[1], port_bottom),
        ("inn", in_trunk_x["inn"], q2_t["B"].center[1], port_bottom),
    ):
        x = snap(x)
        rect(layout, cell, IN_METAL, x - sig_w / 2, min(y_from, y_to),
              x + sig_w / 2, max(y_from, y_to))
        signal_ports[name] = Terminal(
            name=name, layer=f"{IN_METAL.lower()}_drw",
            center=(x, y_to), width=sig_w, orientation=270.0,
        )
        em_segments.append(
            em.Segment(name, IN_METAL, width_um=sig_w, current_a=0.0,
                       note="signal trunk out of the cell edge")
        )

    port_left = snap(ring_box[0] - PORT_REACH)
    mgate_rise_y = snap(nmos_top + ROW_GAP / 2)
    rect(layout, cell, "Metal2", channel_x - link_w / 2, gate_y,
          channel_x + link_w / 2, mgate_rise_y)
    via_between(layout, cell, channel_x, mgate_rise_y, "Metal2", PORT_METAL)
    rect(layout, cell, PORT_METAL, min(channel_x, port_left) - sig_w / 2,
          mgate_rise_y - sig_w / 2, max(channel_x, port_left) + sig_w / 2,
          mgate_rise_y + sig_w / 2)
    rect(layout, cell, PORT_METAL, port_left - sig_w / 2, min(gate_y, mgate_rise_y),
          port_left + sig_w / 2, max(gate_y, mgate_rise_y))
    mgate_port = Terminal(
        name="mgate", layer=f"{PORT_METAL.lower()}_drw",
        center=(port_left, gate_y), width=sig_w, orientation=180.0,
    )
    em_segments.append(
        em.Segment("mgate", PORT_METAL, width_um=sig_w, current_a=0.0,
                   note="mirror gate bias out of the left cell edge")
    )

    ports = {
        "inp": signal_ports["inp"],
        "inn": signal_ports["inn"],
        "outp": signal_ports["outp"],
        "outn": signal_ports["outn"],
        "vdd": ring.ports["vdd"][0],
        "vss": ring.ports["vss"][0],
        "mgate": mgate_port,
    }

    label_pairs: list[tuple[Terminal, str]] = [
        (signal_ports["inp"], "inp"), (signal_ports["inn"], "inn"),
        (signal_ports["outp"], "outp"), (signal_ports["outn"], "outn"),
        (q1_t["C"], "outp"), (q2_t["C"], "outn"),
        (q1_t["E"], "em"), (q2_t["E"], "em"),
        (gate_tap, "mgate"), (mgate_port, "mgate"), (nmos_ports["S_tail"], "vss"),
        (ring.ports["vdd"][0], "vdd"), (ring.ports["vss"][0], "vss"),
    ]
    for pad_net, terminal in pad_terminals.items():
        label_pairs.append((terminal, pad_net))
    if with_loads and upper is not None:
        label_pairs += [
            (rd1_t[upper.name], "nlp1"), (rd2_t[upper.name], "nlp2"),
        ]
    if with_coils:
        label_pairs += [
            (l1_t["PLUS"], "vdd"), (l2_t["PLUS"], "vdd"),
        ]
    for terminal, net in label_pairs:
        stamp_net_labels(layout, cell, [terminal], {terminal.name: net})

    sym_pairs: dict[str, list[float]] = {
        "hbt": [q_left, q_right],
        "out_trunks": [out_trunk_p_x, out_trunk_n_x],
        "in_trunks": [in_trunk_x["inp"], in_trunk_x["inn"]],
    }
    if with_loads:
        sym_pairs["loads"] = [rd_left, rd_right]
    if with_coils:
        sym_pairs["coil_feeds"] = [l1_t["PLUS"].center[0], l2_t["PLUS"].center[0]]
    if with_tapeout:
        sym_pairs["pads"] = [pad_cx_by_net["outp"], pad_cx_by_net["outn"]]

    block = Block(
        name=CELL,
        layout=layout,
        cell=cell,
        ports=ports,
        instances=instances,
        port_nets=PORT_NETS,
        guard=guard,
        symmetry={
            "axis_x_um": axis,
            "pairs": sym_pairs,
        },
        notes=[
            f"cell name is {CELL}, shared with the schematic subcircuit",
            f"single tail centred on x={axis:.2f} um; the mirror diode sits to its "
            "left, outside the symmetric core because it carries no signal",
            "guard ring encloses the NMOS only and its taps are strapped to the "
            "vss rail, so the substrate really is vss",
            "coils rotated to face each other, so vdd is one straight TopMetal2 "
            "strap with the nlp feeds below it dropping onto the rsil loads",
            f"wire widths from the technology LEF at the operating point: "
            f"{i_tail * 1e3:.2f} mA tail, {i_supply * 1e3:.2f} mA supply",
            f"bond pads at outp/outn sit {PAD_ROW_GAP:.0f} um above the coil tops; "
            f"ESD diodes and the clamp sit in the row beneath the pads",
            f"the coil pin row sits {coil_half_h:.1f} um above the HBTs so the "
            "pwell-block markers clear their substrate ties",
            f"inputs leave at the bottom edge on {IN_METAL}; outputs are the bond "
            f"pads on {PAD_METAL} at the top of the cell",
            f"mgate leaves at the left edge on {PORT_METAL} at gate_y",
            f"ring squared about the axis at half-width {ring_half:.1f} um and "
            f"{RING_CLEARANCE:.0f} um clearance",
            f"drawn nlp interconnect {interconnect_um:.1f} um per side",
        ]
        + (
            [
                "black-boxed kinds (geometry omitted, terminal landing pads only): "
                + ", ".join(black_box),
            ]
            if black_box
            else []
        ),
    )
    block.em_segments = em_segments
    block.ring = ring
    return block


_C_LINE = __import__("re").compile(r"^C(\S+)\s+(\S+)\s+(\S+)\s+([0-9.eE+-]+)(\w*)", __import__("re").M)
_SI = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3}


def _cap_value(raw: str, suffix: str) -> float:
    scale = _SI.get(suffix[:1].lower(), 1.0) if suffix else 1.0
    return float(raw) * scale


def _pad_cap_breakdown(pex_spice: Path) -> dict[str, dict[str, float]]:
    """Half-capacitor attribution on outp/outn from the Magic PEX netlist."""
    if not pex_spice.is_file():
        return {}
    buckets = ("pad_metal", "esd", "coil", "core", "supply", "other")
    out: dict[str, dict[str, float]] = {net: {b: 0.0 for b in buckets} for net in ("outp", "outn")}

    def _bucket(other: str) -> str:
        name = other.lower()
        if "pad" in name or name.startswith("m7_"):
            return "pad_metal"
        if "esd" in name or name.startswith("d$") or "diode" in name:
            return "esd"
        if name.startswith("l$") or "nlp" in name or name.startswith("rd"):
            return "coil"
        if name in {"em", "inp", "inn", "mgate"} or name.startswith("m$") or name.startswith("q$"):
            return "core"
        if name in {"vdd", "vss"}:
            return "supply"
        return "other"

    for line in pex_spice.read_text().splitlines():
        match = _C_LINE.match(line.strip())
        if not match:
            continue
        a, b, val, suf = match.group(2), match.group(3), match.group(4), match.group(5)
        c = _cap_value(val, suf)
        for net in ("outp", "outn"):
            if a == net:
                out[net][_bucket(b)] += c / 2.0
            elif b == net:
                out[net][_bucket(a)] += c / 2.0
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--no-render", action="store_true")
    parser.add_argument("--no-pex", action="store_true")
    args = parser.parse_args(argv)

    params = read_params(PARAMS_INC)
    block = build_driver_stage(params=params)
    code, entry = run_stage_gates(
        block,
        args.out,
        schematic=SCHEMATIC,
        subckt=CELL,
        params=params,
        allowed_rules=CHIP_LEVEL_ALLOWED,
        no_render=args.no_render,
        no_pex=args.no_pex,
    )
    pex_path = args.out / "pex_run" / "driver_dut_pex.spice"
    pad_caps = _pad_cap_breakdown(pex_path)
    if pad_caps:
        entry.setdefault("pex", {})["pad_capacitance_f"] = pad_caps
        summary_path = args.out / "driver_dut_summary.json"
        if summary_path.is_file():
            import json
            summary = json.loads(summary_path.read_text())
            summary.setdefault("pex", {})["pad_capacitance_f"] = pad_caps
            summary_path.write_text(json.dumps(summary, indent=2) + "\n")
        for net in ("outp", "outn"):
            parts = pad_caps[net]
            total_ff = sum(parts.values()) * 1e15
            detail = ", ".join(f"{k}={v * 1e15:.1f} fF" for k, v in parts.items() if v > 0)
            print(f"  pad C   {net}  {total_ff:.1f} fF total  ({detail})")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
