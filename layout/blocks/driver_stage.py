#!/usr/bin/env python3
"""The pad driver stage: symmetric placement, a power ring and EM-sized buses.

The cell is ``driver_dut`` — the same name as the subcircuit in
``circuits/ctle56n/spice/driver_pdk.cir``. ``layout/common/parity.py`` checks
that device for device on every run.

Two power grids, one per band. The core ring encloses the active devices; a
second ring encloses the pad band, so the clamp and the four ESD diodes tie to a
rail conductor a few um away instead of reaching down into the core ring. The two
rings are stitched together over the gap between them, vdd on the axis and vss
either side of it, all on TopMetal1 so a strap crosses the other net's TopMetal2
horizontals without shorting to them.

Floorplan, centred on one vertical axis with the two differential halves as exact
mirror images (top to bottom in the pad-driver orientation):

    ══════════════════════════════════════════════   pad ring top: vss then vdd
                clamp — vdd to vss on the axis
    ║ pad_p │ esd_p │ outp   outn │ esd_n │ pad_n ║  180 um pitch, ESD inboard
    ══════════════════════════════════════════════   pad ring bottom: vss, vdd
            vdd ── on the axis ──  vss ── ±40 ──     ring-to-ring, TopMetal1
    ══════════════════════════════════════════════   core ring top
    coil ════════╤═╧╤════════ coil      M135 / R270, pins inboard
         body   vdd  nlp       body
                 rd1   rd2                 rsil loads in the coil channel
                  Q1 ── Q2                npn13G2 pair, shared em on the axis
    ┌──── guard ring (NMOS only) ────┐
    │  diode │      tail             │    mirror off-axis, tail on axis
    └───────────────┬────────────────┘
mgate ──────────────┘                      Metal4, out of the left edge
    ══════════════════════════════════     core ring bottom; vss tap inside
              inp │ inn                      Metal4, out of the bottom edge

Each ESD column is two diodes stacked, ``diodevss`` below ``diodevdd``, so that
the two Metal1 rail pins — ``diodevss``'s VDD on its top edge and ``diodevdd``'s
VSS on its bottom edge — both come out into the gap between them and join the
Metal2 rail pins on the column's inboard edge. One vdd tap and one vss tap per
column then run down to the pad ring, on Metal2 and Metal3 at the same x since
those two layers have no spacing rule against each other.

Metal budget (no two structures share a layer at the same y crossing):

| Use | Metal |
| --- | --- |
| NMOS source/drain rails | Metal2 |
| em, outp, outn above the emitter row | Metal5 |
| Collector/load rise below ``safe_y`` | Metal2 on an outboard stub |
| inp, inn, mgate cell edges | Metal4 |
| outp/outn from collector column to bond pad | Metal5 end-to-end |
| vdd strap, nlp, both rings' horizontals | TopMetal2 |
| Both rings' verticals, vdd riser, ring-to-ring straps | TopMetal1 |
| ESD vdd taps, ESD pad bars | Metal2 |
| ESD vss taps, clamp rail taps | Metal3 |

Usage:
    python layout/blocks/driver_stage.py
"""

from __future__ import annotations

import argparse
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
from layout.blocks.mos_array import GAT_D_CLEARANCE, GATE_STRAP_W, build_mos_array
from layout.blocks.power_ring import (
    NET_SPACING_FACTOR,
    add_power_ring,
    corner_via_grid,
    ring_width,
)
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
LOAD_DX = 12.0
PWB_TAP_CLEARANCE = 2.0
HBT_DX = 8.0
BASE_VIA_DX = 2.2
PORT_METAL = "Metal4"
IN_METAL = PORT_METAL
PORT_REACH = 6.0
RING_CLEARANCE = 10.0
ARRAY_GAP = 12.0
ROUTE_METAL = "Metal5"
PAD_METAL = "TopMetal2"
CHIP_LEVEL_ALLOWED = ("LBE.a", "LBE.c")

#: Extra vertical clearance between the coil bodies and the active devices below
#: them, on top of the PWB.f distance the coil's pwell_block marker already
#: forces. PWB.f alone leaves the coil sitting on the HBT substrate ties.
COIL_ACTIVE_GAP = 10.0

#: Centre-to-centre bond-pad pitch, um. 70 um pads → 110 um channel, which is
#: what lets both ESD columns sit between the pads at the pads' own height
#: instead of outboard of them.
PAD_PITCH = 180.0
PAD_CHANNEL = PAD_PITCH - 70.0

#: Pad inner edge to the outer edge of that pad's ESD column, um. The column's
#: PAD bar and its Metal2-to-Metal5 stack live in this gap.
ESD_PAD_GAP = 9.0
#: Between the two stacked diodes of one column, um. Both Metal1 rail pins come
#: out into this gap and need room to turn inboard.
ESD_STACK_GAP = 8.0
#: Inboard of an ESD column, where its two rail taps run vertically.
ESD_TAP_DX = 2.5
#: Width of an ESD rail tap or pad bar, um. ESD conducts a 2 kV discharge rather
#: than a DC operating current, so this is not an ``em.py`` number and wider is
#: better; 4 um is what the corridor between a column and its own pins holds. 6 um
#: reports 32 Mn.b violations against the diodes' own Metal2.
ESD_BUS_W = 4.0

#: The pad band's own ring: clearance from the band to its innermost conductor.
PAD_RING_CLEARANCE = 6.0
#: Between the core ring's outer conductor and the pad ring's outer conductor, um.
RING_TO_RING_GAP = 10.0
#: Where the vss ring-to-ring straps sit either side of the axis; vdd is on it.
VSS_STRAP_DX = 40.0
#: Where the clamp's vss tap turns vertical, off the axis. Its own cell is 37 um
#: wide, so a tap on the axis would run up the clamp's own Metal3.
CLAMP_VSS_DX = 24.0

PAD_FEED_METAL = "Metal5"
#: How far inboard of its output column the pad feed comes down, um. The column
#: itself cannot be extended past the load: the load's nlp pin sits directly above
#: its outp pin, and the via stack that takes nlp up to TopMetal2 passes through
#: Metal5 on the way, so a Metal5 riser at the column's x shorts the load out.
#: Inboard puts the riser in the channel between the two coil bodies.
OUT_FEED_DX = 8.0
#: Minimum length a metal leaving a bond pad has to run before it stops or turns
#: (``Pad.fR``). It is an exit length, not a keepout: the bondpad PCell is a
#: filled Metal3-to-TopMetal2 stack, so a feed leaves on whichever of those
#: layers suits it and simply has to reach this far past the pad edge.
PAD_EXIT_LENGTH = rule("Pad_fR")


def _edge_route(layout, cell, metal: str, x: float, y0: float, y1: float, width: float) -> None:
    rect(layout, cell, metal, x - width / 2, min(y0, y1), x + width / 2, max(y0, y1))


def _rail_tap(
    layout,
    cell,
    terminal: Terminal,
    ring_y: float,
    tap_x: float,
    width: float,
    *,
    pin_metal: str = "Metal2",
    tap_metal: str = "Metal2",
) -> None:
    """Take a device rail pin to a ring conductor: horizontal at the pin, then vertical.

    ``tap_metal`` stays below TopMetal1 the whole way, so the run crosses the
    other net's ring conductor rather than having to dodge it, and only the via
    stack at ``ring_y`` reaches the top metals.
    """
    tx, ty = via_up(layout, cell, terminal, pin_metal)
    rect(layout, cell, pin_metal, min(tx, tap_x) - width / 2, ty - width / 2,
         max(tx, tap_x) + width / 2, ty + width / 2)
    if pin_metal != tap_metal:
        via_between(layout, cell, tap_x, ty, pin_metal, tap_metal, columns=1, rows=1)
    _edge_route(layout, cell, tap_metal, tap_x, ty, ring_y, width)
    via_between(layout, cell, tap_x, ring_y, tap_metal, "TopMetal2", columns=2, rows=2)


def _place_at(layout, cell, spec: DeviceSpec, orientation: str = "R0", *,
              left: float | None = None, right: float | None = None,
              bottom: float | None = None):
    """Place a device by an edge of its placed bounding box rather than by ``dx``."""
    box = device_bbox_at(spec, 0.0, 0.0, orientation)
    dx = snap(left - box.left) if left is not None else snap(right - box.right)
    return place(layout, cell, spec, dx, snap(bottom - box.bottom), orientation)


def _top_via_field(layout, cell, x: float, y: float, current_a: float) -> int:
    """Stitch TopMetal1 to TopMetal2 with enough TopVia2 cuts for ``current_a``.

    ``via_stack`` draws one TopVia2 cut per instance — its row and column
    parameters only multiply the thin vias lower in a stack — so the cut count is
    the instance count, the same reason ``power_ring`` tiles its corners.
    """
    rows, cols = corner_via_grid(current_a)
    _, via_cell = build(
        DeviceSpec(name="_ring_via_probe", kind="via_stack",
                   params={"b_layer": "TopMetal1", "t_layer": "TopMetal2",
                           "columns": 1, "rows": 1})
    )
    pitch = snap(via_cell.dbbox().width() + min_space("TopMetal2"))
    for row in range(rows):
        for col in range(cols):
            via_between(
                layout, cell,
                snap(x + (col - (cols - 1) / 2) * pitch),
                snap(y + (row - (rows - 1) / 2) * pitch),
                "TopMetal1", "TopMetal2", columns=1, rows=1,
            )
    return rows * cols


def _hbt_emitter_up(layout, cell, terminal: Terminal, metal: str,
                    outboard_sign: float) -> tuple[float, float]:
    """Raise the emitter on an outboard stub; a downward stub crosses the base bar."""
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


def _pin_placement_dx(spec: DeviceSpec, pin_name: str, target_x: float,
                      orientation: str = "R0") -> float:
    """Device ``dx`` that lands ``pin_name`` on ``target_x`` (R0/M90 probe at origin)."""
    pya = pya_module()
    layout_probe, cell_probe = build(spec)
    terms = {t.name: t for t in derive_terminals(spec, layout_probe, cell_probe)}
    pin = terms[pin_name]
    trans = pya.DTrans(
        {"R0": pya.DTrans.R0, "M90": pya.DTrans.M90}[orientation],
        pya.DVector(0.0, 0.0),
    )
    px = (trans * pya.DPoint(*pin.center)).x
    return snap(target_x - px)


def _column_rise(
    layout,
    cell,
    terminals: list[Terminal],
    col_x: float,
    metal: str,
    width: float,
    *,
    em_y: float,
) -> float:
    """Output column on ``col_x``; Metal5 starts above ``em_y`` so it never crosses the emitter.

    Returns the y the column reaches, which is where a pad feed has to pick it up.
    """
    from layout.common.route import metal_of

    w = width
    col_x = snap(col_x)
    safe_y = snap(em_y + w + 2.0)
    rise_w = route_width("Metal2")
    top_y = safe_y
    for terminal in terminals:
        tx, ty = terminal.center
        tx, ty = snap(tx), snap(ty)
        bottom = metal_of(terminal.layer)
        stub_x = snap(tx - VIA_OFFSET if col_x < tx else tx + VIA_OFFSET)
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
        rect(layout, cell, metal, min(stub_x, col_x), safe_y - w / 2,
              max(stub_x, col_x), safe_y + w / 2)
    rect(layout, cell, metal, col_x - w / 2, safe_y, col_x + w / 2, top_y)
    return top_y


def build_driver_stage(params: dict[str, float] | None = None,
                       black_box: tuple[str, ...] = (),
                       *,
                       with_loads: bool = True,
                       with_coils: bool = True,
                       with_tapeout: bool = True,
                       with_pads: bool = True,
                       with_pad_feed: bool = True,
                       with_esd: bool = True,
                       _probe: dict[str, bool] | None = None) -> Block:
    """Place and wire one pad driver stage."""
    from layout.devices.catalog import COIL, driver_devices, esd_devices

    probe = _probe or {}
    with_clamp = probe.get("with_clamp", True)
    with_pad_ring = probe.get("with_pad_ring", True)
    with_esd_taps = probe.get("with_esd_taps", True)
    with_clamp_routes = probe.get("with_clamp_routes", True)

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

    layout = new_layout()
    cell = layout.create_cell(CELL)
    instances: list[tuple[DeviceSpec, dict[str, str]]] = []
    em_segments: list[em.Segment] = []

    # --- NMOS row --------------------------------------------------------------
    arrays = {
        "mdiode": build_mos_array("mdiode", mirror_w, mirror_l, current_a=i_mirror),
        "tail": build_mos_array("tail", tail_w, mirror_l, current_a=i_tail),
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
        (arrays["tail"].total_spec.with_name("tail"),
         {"D": "em", "G": "mgate", "S": "vss", "sub": "vss"}),
    ]

    # From the placed boxes, not from the placement offsets. An array's cell box
    # starts left of its own origin — the gate strap inside it overhangs the
    # source rail by 4 um — so a ring placed off the offsets sat on that strap and
    # reported Gat.d against the tap activ either side of it.
    array_boxes = {name: array.cell.dbbox() for name, array in arrays.items()}
    nmos_left = snap(min(placement[n] + b.left for n, b in array_boxes.items()) - 1.0)
    nmos_right = snap(max(placement[n] + b.right for n, b in array_boxes.items()) + 1.0)

    vss_rail_w = snap(max(em.width_for_a("Metal2", i_supply), route_width("Metal2")))
    source_rail_top = snap(nmos_ports["S_tail"].center[1] + arrays["tail"].rail_width_um / 2)
    vss_rail_bottom = snap(source_rail_top - vss_rail_w)
    vss_rail_y = snap(source_rail_top - vss_rail_w / 2)
    rect(layout, cell, "Metal2", nmos_left, vss_rail_bottom, nmos_right, source_rail_top)
    em_segments.append(em.Segment("vss.rail", "Metal2", width_um=vss_rail_w,
                                  current_a=i_supply, note="shared source rail"))

    gate_y = nmos_ports["G_tail"].center[1]
    from layout.blocks.mos_array import _layer_bbox
    activ_tops = []
    for array in arrays.values():
        ab = _layer_bbox(array.layout, array.cell, "activ_drw")
        if ab is not None:
            activ_tops.append(ab.top)
    strap_bottom = snap(max(activ_tops) + GAT_D_CLEARANCE)
    strap_top = snap(strap_bottom + GATE_STRAP_W)
    poly = lm["gatpoly_drw"]
    cell.shapes(layout.layer(poly[0], poly[1])).insert(
        pya.DBox(nmos_left, strap_bottom, nmos_right, strap_top)
    )

    diode_right = snap(placement["mdiode"] + mirror_box.right)
    tail1_left = snap(placement["tail"] + tail_box.left)
    channel_x = snap((diode_right + tail1_left) / 2.0)
    gate_tap = poly_contact(layout, cell, channel_x, gate_y)
    via_between(layout, cell, channel_x, gate_y, "Metal1", "Metal2", columns=1, rows=1)

    link_w = route_width("Metal2")
    link_x = snap(diode_right - arrays["mdiode"].rail_width_um / 2)
    diode_rail = nmos_ports["D_mdiode"]
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

    col_p_x = snap(axis - COIL_PIN_GAP / 2.0)
    col_n_x = snap(axis + COIL_PIN_GAP / 2.0)

    # --- HBT pair — collectors on the load columns -----------------------------
    row_y = snap(nmos_top + ROW_GAP)
    q1 = hbt.with_name("q1")
    q2 = hbt.with_name("q2")
    q_left = _pin_placement_dx(q1, "C", col_p_x)
    q_right = _pin_placement_dx(q2, "C", col_n_x, "M90")
    q1_t, q_box = place(layout, cell, q1, q_left, row_y)
    q2_t, q2_box = place(layout, cell, q2, q_right, row_y, "M90")
    instances += [
        (q1, {"C": "outp", "B": "inp", "E": "em", "sub": "vss"}),
        (q2, {"C": "outn", "B": "inn", "E": "em", "sub": "vss"}),
    ]
    hbt_top = snap(q_box.top)

    # --- rsil loads in the coil channel ----------------------------------------
    rd1_t: dict[str, Terminal] = {}
    rd2_t: dict[str, Terminal] = {}
    load_top = hbt_top
    upper = lower = None
    if with_loads:
        row_y = snap(hbt_top + ROW_GAP)
        load_terms = {t.name: t for t in derive_terminals(load, *build(load))}
        upper = max(load_terms.values(), key=lambda t: t.center[1])
        lower = min(load_terms.values(), key=lambda t: t.center[1])
        rd1 = load.with_name("rd1")
        rd2 = load.with_name("rd2")
        rd_left = _pin_placement_dx(rd1, lower.name, col_p_x)
        rd_right = _pin_placement_dx(rd2, lower.name, col_n_x, "M90")
        rd1_t, rd_box = place(layout, cell, rd1, rd_left, row_y)
        rd2_t, _ = place(layout, cell, rd2, rd_right, row_y, "M90")
        instances += [
            (rd1, {upper.name: "nlp1", lower.name: "outp", "sub": "vss"}),
            (rd2, {upper.name: "nlp2", lower.name: "outn", "sub": "vss"}),
        ]
        load_top = snap(rd_box.top)

    # --- coils facing each other -----------------------------------------------
    l1_t: dict[str, Terminal] = {}
    l2_t: dict[str, Terminal] = {}
    l1_box = l2_box = pya.DBox(0, 0, 0, 0)
    coil_top = load_top
    coil_half_h = 0.0
    coil_bb = False
    tm2_riser_w = route_width("TopMetal2")
    strap_w = tm2_riser_w
    vdd_y = snap(load_top + ROW_GAP)
    interconnect_um = 0.0
    if with_coils:
        _, coil_probe = build(coil)
        coil_half_h = snap(coil_probe.dbbox().width() / 2.0)
        ptap_y_hi = snap(max(y for _, y in guard["tap_centres_um"]))
        for term in (q1_t, q2_t):
            if "sub" in term:
                ptap_y_hi = snap(max(ptap_y_hi, term["sub"].center[1]))
        # PWB.f is the only rule the coil's pwell_block marker imposes on the
        # p-taps below it, and satisfying it exactly leaves the coil body sitting
        # on the HBT substrate ties. COIL_ACTIVE_GAP lifts the whole coil row off
        # the active devices by a margin the deck does not ask for.
        row_y = snap(max(
            load_top + ROW_GAP,
            ptap_y_hi + coil_half_h + PWB_TAP_CLEARANCE + rule("PWB_f"),
        ) + COIL_ACTIVE_GAP)
        l1 = coil.with_name("l1")
        l2 = coil.with_name("l2")
        coil_bb = l1.kind in bb_kinds
        l1_dx = snap(axis - COIL_PIN_GAP / 2.0)
        l2_dx = snap(axis + COIL_PIN_GAP / 2.0)
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
        coil_top = snap(max(l1_box.top, l2_box.top))

        strap_w = snap(l1_t["PLUS"].width)
        vdd_y = l1_t["PLUS"].center[1]
        rect(layout, cell, "TopMetal2",
              l1_t["PLUS"].center[0], vdd_y - strap_w / 2,
              l2_t["PLUS"].center[0], vdd_y + strap_w / 2)
        em_segments.append(em.Segment("vdd.strap", "TopMetal2", width_um=strap_w,
                                        current_a=i_supply, note="between the coil supply feeds"))

        if with_loads and upper is not None:
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

    # --- base taps sideways; em bus on the axis --------------------------------
    in_trunk_x: dict[str, float] = {}
    for base, sign, name in ((q1_t["B"], +1.0, "inp"), (q2_t["B"], -1.0, "inn")):
        bx, by = base.center
        vx = snap(bx + sign * BASE_VIA_DX)
        stub_h = snap(base.width if base.width < 1.0 else route_width("Metal1"))
        rect(layout, cell, "Metal1", min(bx, vx), by - stub_h / 2, max(bx, vx), by + stub_h / 2)
        via_between(layout, cell, vx, by, "Metal1", IN_METAL, columns=1, rows=1)
        in_trunk_x[name] = vx

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
    em_segments.append(
        em.Segment("em", ROUTE_METAL, width_um=sig_w, current_a=i_tail,
                   note="shared emitter strap with inp/inn corridor gap")
    )

    # Where each output column ends, so the pad feed carries on from the metal
    # that is actually there rather than from an assumed height.
    out_col_top: dict[str, float] = {}
    if with_loads and lower is not None:
        for col_x, q_t, rd_t, net in (
            (col_p_x, q1_t, rd1_t, "outp"),
            (col_n_x, q2_t, rd2_t, "outn"),
        ):
            out_col_top[net] = _column_rise(
                layout, cell, [q_t["C"], rd_t[lower.name]], col_x,
                ROUTE_METAL, sig_w, em_y=em_y,
            )
            em_segments.append(
                em.Segment(net, ROUTE_METAL, width_um=sig_w, current_a=i_tail,
                           note="collector straight into the load")
            )

    # --- power ring around the core (pad band goes above the ring top) ---------
    devices_box = cell.dbbox()
    if with_coils and coil_bb:
        devices_box = devices_box + l1_box + l2_box
    ring_half = snap(max(axis - devices_box.left, devices_box.right - axis))
    core_ring_top = snap(coil_top + RING_CLEARANCE)
    ring = add_power_ring(
        layout, cell,
        pya.DBox(snap(axis - ring_half), devices_box.bottom,
                 snap(axis + ring_half), core_ring_top),
        currents={"vss": i_supply, "vdd": i_supply},
        clearance=RING_CLEARANCE,
    )
    em_segments += ring.em_segments
    ring_box = ring.outer_box

    # The vss riser goes down to the ring's bottom run, next to the source rail;
    # anything reaching the pad band leaves from the top run instead.
    vss_ring_y = ring.ports["vss"][1].center[1]
    vss_ring_top_y = ring.ports["vss"][0].center[1]
    via_between(layout, cell, axis, vss_rail_y, "Metal2", "TopMetal2", columns=3, rows=3)
    _edge_route(layout, cell, "TopMetal2", axis, vss_ring_y, vss_rail_y, tm2_riser_w)
    rect(layout, cell, "TopMetal2", axis - tm2_riser_w / 2, min(vss_rail_y, vss_ring_y),
          axis + tm2_riser_w / 2, max(vss_rail_y, vss_ring_y))

    vdd_ring_y = ring.ports["vdd"][0].center[1]
    via_between(layout, cell, axis, vdd_y, "TopMetal1", "TopMetal2", columns=1, rows=1)
    rect(layout, cell, "TopMetal1", axis - strap_w / 2, vdd_y, axis + strap_w / 2, vdd_ring_y)
    via_between(layout, cell, axis, vdd_ring_y, "TopMetal1", "TopMetal2", columns=1, rows=1)

    em_segments += [
        em.Segment("vss.riser", "TopMetal2", width_um=tm2_riser_w, current_a=i_supply,
                   note="source rail to the ring on the axis"),
        em.Segment("vdd.riser", "TopMetal1", width_um=strap_w, current_a=i_supply,
                   note="coil strap up to the ring, crossing under vss on TM1"),
    ]

    # --- pad band: pads, ESD between them, clamp, and the band's own ring ------
    pad_ports: dict[str, Terminal] = {}
    pad_terminals: dict[str, Terminal] = {}
    esd_col_x: dict[str, float] = {}
    clamp_t: dict[str, Terminal] | None = None
    pad_ring = None
    pad_top_y = snap(ring.outer_box[3])
    band_top_y = pad_top_y
    pad_pitch_um = PAD_PITCH
    pad_cx_p = snap(axis - PAD_PITCH / 2.0)
    pad_cx_n = snap(axis + PAD_PITCH / 2.0)
    ring_top_y = snap(ring.outer_box[3])
    if with_tapeout and with_pads:
        _, pad_probe = build(pad_spec)
        pad_size = snap(pad_probe.dbbox().width())
        pad_half = snap(pad_size / 2.0)

        _, esd_probe = build(esd_vdd_spec)
        esd_w = snap(esd_probe.dbbox().width())
        esd_h = snap(esd_probe.dbbox().height())
        esd_col_h = snap(2.0 * esd_h + ESD_STACK_GAP)

        # The pad ring goes below the band, between it and the core ring, so the
        # band's floor is set by how much room that ring needs. ring_width and the
        # net spacing are the same functions add_power_ring will use.
        pad_ring_w = ring_width(i_supply)
        ring_net_space = snap(
            max(min_space("TopMetal2"), min_space("TopMetal1")) * NET_SPACING_FACTOR
        )
        pad_ring_depth = snap(PAD_RING_CLEARANCE + 2.0 * pad_ring_w + ring_net_space)
        band_y0 = snap(ring_top_y + RING_TO_RING_GAP + pad_ring_depth)

        # Pads and ESD columns share a centre line: the pad feed then leaves the
        # pad, crosses the column in the gap between its two diodes, and turns
        # down the output column without a single jog.
        pad_row_y = snap(band_y0 + esd_col_h / 2.0)
        pad_left, pad_right = mirrored_pair_x(pad_spec, axis, gap=PAD_CHANNEL)
        pad_p_t, pad_p_box = place(layout, cell, pad_spec.with_name("pad_p"), pad_left, pad_row_y)
        pad_n_t, pad_n_box = place(
            layout, cell, pad_spec.with_name("pad_n"), pad_right, pad_row_y, "M90",
        )
        pad_top_y = snap(max(pad_p_box.top, pad_n_box.top))
        pad_cx_p = pad_p_t["PAD"].center[0]
        pad_cx_n = pad_n_t["PAD"].center[0]
        pad_pitch_um = snap(abs(pad_cx_n - pad_cx_p))
        band_top_y = snap(max(pad_top_y, band_y0 + esd_col_h))

        # Place the whole band first: the ring has to be around it before anything
        # can be routed to a ring conductor.
        sides: list[dict] = []
        for side, pad_net, pad_box, inb, orient, col_x in (
            ("p", "outp", pad_p_box, +1.0, "R0", col_p_x),
            ("n", "outn", pad_n_box, -1.0, "M90", col_n_x),
        ):
            pad_t = pad_p_t if side == "p" else pad_n_t
            pad_cx = pad_cx_p if side == "p" else pad_cx_n
            pad_terminals[pad_net] = pad_t["PAD"]
            pad_ports[pad_net] = Terminal(
                name=pad_net,
                layer="topmetal2_drw",
                center=(pad_cx, pad_top_y),
                width=pad_size,
                orientation=90.0,
            )
            if not with_esd:
                continue

            pad_inner = snap(pad_box.right if inb > 0 else pad_box.left)
            col_outer = snap(pad_inner + inb * ESD_PAD_GAP)
            col_inner = snap(col_outer + inb * esd_w)

            # diodevss below diodevdd, so both cells' Metal1 rail pins come out
            # into the gap between them and both Metal2 ones face inboard.
            evss = esd_vss_spec.with_name(f"esd_vss_{side}")
            evdd = esd_vdd_spec.with_name(f"esd_vdd_{side}")
            edge = {"left": col_outer} if inb > 0 else {"right": col_outer}
            evss_t, _ = _place_at(layout, cell, evss, orient, bottom=band_y0, **edge)
            evdd_t, _ = _place_at(
                layout, cell, evdd, orient,
                bottom=snap(band_y0 + esd_h + ESD_STACK_GAP), **edge,
            )
            instances += [
                (evdd, {"VDD": "vdd", "PAD": pad_net, "VSS": "vss"}),
                (evss, {"VDD": "vdd", "PAD": pad_net, "VSS": "vss"}),
            ]
            esd_col_x[pad_net] = snap((col_outer + col_inner) / 2.0)
            sides.append({
                "net": pad_net, "inb": inb, "col_x": col_x, "pad_inner": pad_inner,
                "col_inner": col_inner, "evdd": evdd_t, "evss": evss_t,
            })

        if clamp_spec and with_clamp:
            clamp = clamp_spec.with_name("clamp")
            clamp_dx = _pin_placement_dx(clamp, "VDD", axis)
            clamp_box0 = device_bbox_at(clamp, 0.0, 0.0)
            clamp_t, clamp_box = place(
                layout, cell, clamp, clamp_dx,
                snap(band_top_y + ROW_GAP - clamp_box0.bottom),
            )
            instances.append((clamp, {"VDD": "vdd", "VSS": "vss"}))
            band_top_y = snap(clamp_box.top)

        if with_pad_ring:
            pad_ring = add_power_ring(
                layout, cell,
                pya.DBox(snap(axis - PAD_PITCH / 2.0 - pad_half), band_y0,
                         snap(axis + PAD_PITCH / 2.0 + pad_half), band_top_y),
                currents={"vss": i_supply, "vdd": i_supply},
                clearance=PAD_RING_CLEARANCE,
            )
            em_segments += [
                em.Segment(f"pad_{s.net}", s.layer, width_um=s.width_um, cuts=s.cuts,
                           current_a=s.current_a, note=f"pad band {s.note}")
                for s in pad_ring.em_segments
            ]
            pad_rail_y = {
                ("vdd", "lo"): pad_ring.ports["vdd"][1].center[1],
                ("vss", "lo"): pad_ring.ports["vss"][1].center[1],
                ("vdd", "hi"): pad_ring.ports["vdd"][0].center[1],
                ("vss", "hi"): pad_ring.ports["vss"][0].center[1],
            }

            # Stitch the two rings. Every strap is TopMetal1, so it runs under the
            # other net's TopMetal2 horizontal instead of having to route around
            # it, and only the via field at each end reaches TopMetal2.
            ring_strap_w = pad_ring.widths["vdd"]
            for net, strap_x, y_lo in (
                ("vdd", axis, vdd_ring_y),
                ("vss", snap(axis - VSS_STRAP_DX), vss_ring_top_y),
                ("vss", snap(axis + VSS_STRAP_DX), vss_ring_top_y),
            ):
                # Run the strap the full width of both conductors it lands on, not
                # just centre to centre: a via field is placed about the centre and
                # its outer row of TopMetal1 pads would otherwise sit outside the
                # strap as islands, 1.1 um from it, which is TM1.b.
                y_hi = pad_rail_y[(net, "lo")]
                _edge_route(layout, cell, "TopMetal1", strap_x,
                            snap(y_lo - ring_strap_w / 2.0),
                            snap(y_hi + ring_strap_w / 2.0), ring_strap_w)
                cuts = _top_via_field(layout, cell, strap_x, y_lo, i_supply)
                _top_via_field(layout, cell, strap_x, y_hi, i_supply)
                em_segments += [
                    em.Segment(f"{net}.ring_strap", "TopMetal1", width_um=ring_strap_w,
                               current_a=i_supply,
                               note="core ring to pad ring, under the other net's TM2"),
                    em.Segment(f"{net}.ring_strap.via", "TopVia2", cuts=cuts,
                               current_a=i_supply, note="ring-to-ring stitch"),
                ]

        for s in sides:
            inb, col_x, evdd_t, evss_t = s["inb"], s["col_x"], s["evdd"], s["evss"]
            pad_net, pad_inner, col_inner = s["net"], s["pad_inner"], s["col_inner"]

            if with_pad_feed:
                # PAD: a Metal2 bar in the pad gap collects both diodes' PAD pins
                # and meets the Metal5 pad feed on one stack, at the pads' centre y.
                bar_x = snap(pad_inner + inb * ESD_PAD_GAP / 2.0)
                pad_pin_ys = [evss_t["PAD"].center[1], evdd_t["PAD"].center[1]]
                rect(layout, cell, "Metal2", bar_x - ESD_BUS_W / 2, min(pad_pin_ys),
                     bar_x + ESD_BUS_W / 2, max(pad_pin_ys))
                for pin in (evss_t["PAD"], evdd_t["PAD"]):
                    px, py = pin.center
                    rect(layout, cell, "Metal2", min(px, bar_x) - ESD_BUS_W / 2,
                         py - ESD_BUS_W / 2, max(px, bar_x) + ESD_BUS_W / 2,
                         py + ESD_BUS_W / 2)
                via_between(layout, cell, bar_x, pad_row_y, "Metal2", PAD_FEED_METAL)

                # The bondpad PCell is a filled Metal3-to-TopMetal2 stack, so the
                # feed just leaves on Metal5. Pad.fR is an exit length, and this
                # run is far longer than the 7 um it asks for.
                feed_from = snap(pad_inner - inb * PAD_EXIT_LENGTH / 2.0)
                feed_x = snap(col_x + inb * OUT_FEED_DX)
                rect(layout, cell, PAD_FEED_METAL, min(feed_from, feed_x),
                     pad_row_y - sig_w / 2, max(feed_from, feed_x), pad_row_y + sig_w / 2)
                col_top = out_col_top.get(pad_net, pad_row_y)
                _edge_route(layout, cell, PAD_FEED_METAL, feed_x, col_top, pad_row_y, sig_w)
                rect(layout, cell, PAD_FEED_METAL, min(col_x, feed_x) - sig_w / 2,
                     col_top - sig_w / 2, max(col_x, feed_x) + sig_w / 2,
                     col_top + sig_w / 2)
                em_segments.append(
                    em.Segment(f"{pad_net}.pad_feed", PAD_FEED_METAL, width_um=sig_w,
                               current_a=i_tail,
                               note="output column continued up to the bond pad")
                )

            if not (with_esd_taps and pad_ring is not None):
                continue

            # One tap per rail, both at the same x: vdd stays on Metal2 and vss on
            # Metal3 the whole way, and those two layers have no spacing rule
            # against each other, so neither has to dodge the other.
            tap_x = snap(col_inner + inb * (ESD_TAP_DX + ESD_BUS_W / 2.0))
            for net, tap_metal, m2_pin, m1_pin in (
                ("vdd", "Metal2", evdd_t["VDD"], evss_t["VDD"]),
                ("vss", "Metal3", evss_t["VSS"], evdd_t["VSS"]),
            ):
                # The Metal2 rail pin sits on the column's inboard edge; the Metal1
                # one leaves through the gap between the two diodes. vss changes to
                # Metal3 at the pin, not at the tap, or its Metal2 would reach
                # across the vdd tap running past it.
                px, py = m2_pin.center
                hop_x = px if tap_metal == "Metal2" else snap(px + inb * VIA_OFFSET / 2.0)
                rect(layout, cell, "Metal2", min(px, hop_x) - ESD_BUS_W / 2,
                     py - ESD_BUS_W / 2, max(px, hop_x) + ESD_BUS_W / 2, py + ESD_BUS_W / 2)
                if tap_metal != "Metal2":
                    via_between(layout, cell, hop_x, py, "Metal2", tap_metal,
                                columns=1, rows=1)
                rect(layout, cell, tap_metal, min(hop_x, tap_x) - ESD_BUS_W / 2,
                     py - ESD_BUS_W / 2, max(hop_x, tap_x) + ESD_BUS_W / 2,
                     py + ESD_BUS_W / 2)
                gx, gy = via_up(layout, cell, m1_pin, tap_metal)
                rect(layout, cell, tap_metal, min(gx, tap_x) - ESD_BUS_W / 2,
                     gy - ESD_BUS_W / 2, max(gx, tap_x) + ESD_BUS_W / 2, gy + ESD_BUS_W / 2)
                ring_y = pad_rail_y[(net, "lo")]
                _edge_route(layout, cell, tap_metal, tap_x, max(py, gy), ring_y, ESD_BUS_W)
                via_between(layout, cell, tap_x, ring_y, tap_metal, "TopMetal2")
                em_segments.append(
                    em.Segment(f"{pad_net}.{net}", tap_metal, width_um=ESD_BUS_W,
                               current_a=0.0,
                               note="ESD rail tap to the pad ring; width is for a "
                                    "2 kV discharge, not a LEF DC rating")
                )

        if clamp_t and with_clamp_routes and pad_ring is not None:
            # The clamp sits directly under the pad ring's top runs, which is what
            # the band's own ring buys: both taps are a few um of Metal3 instead of
            # a run all the way down into the core ring.
            _rail_tap(layout, cell, clamp_t["VDD"], pad_rail_y[("vdd", "hi")], axis,
                      ESD_BUS_W, pin_metal="Metal3", tap_metal="Metal3")
            _rail_tap(layout, cell, clamp_t["VSS"], pad_rail_y[("vss", "hi")],
                      snap(axis + CLAMP_VSS_DX), ESD_BUS_W,
                      pin_metal="Metal3", tap_metal="Metal3")

    # --- signal ports -----------------------------------------------------------
    band_outer_top = snap(pad_ring.outer_box[3]) if pad_ring is not None else band_top_y
    port_bottom = snap(ring_box[1] - PORT_REACH)
    port_top = snap(max(ring_box[3], band_outer_top) + PORT_REACH)
    signal_ports: dict[str, Terminal] = dict(pad_ports)
    if not signal_ports:
        for name, col_x in (("outp", col_p_x), ("outn", col_n_x)):
            signal_ports[name] = Terminal(
                name=name, layer=f"{ROUTE_METAL.lower()}_drw",
                center=(col_x, port_top), width=sig_w, orientation=90.0,
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

    # vdd and vss leave from the outermost ring the cell has, so a stage that
    # abuts this one meets them at the top edge next to outp/outn.
    supply_ring = pad_ring if pad_ring is not None else ring
    ports = {
        "inp": signal_ports["inp"],
        "inn": signal_ports["inn"],
        "outp": signal_ports["outp"],
        "outn": signal_ports["outn"],
        "vdd": supply_ring.ports["vdd"][0],
        "vss": supply_ring.ports["vss"][0],
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
    if pad_ring is not None:
        label_pairs += [
            (pad_ring.ports["vdd"][0], "vdd"), (pad_ring.ports["vss"][0], "vss"),
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
            "pairs": {
                "hbt": [q_left, q_right],
                "in_trunks": [in_trunk_x["inp"], in_trunk_x["inn"]],
                "columns": [col_p_x, col_n_x],
                "pads": [pad_cx_p, pad_cx_n],
                "esd_columns": [esd_col_x.get("outp", col_p_x), esd_col_x.get("outn", col_n_x)],
            },
        },
        notes=[
            f"cell name is {CELL}, shared with the schematic subcircuit",
            f"bond-pad pitch {pad_pitch_um:.2f} um (target {PAD_PITCH:.0f} um); "
            f"{PAD_CHANNEL:.0f} um channel holds both ESD columns at the pads' own y",
            f"single tail centred on x={axis:.2f} um; mirror diode off-axis left",
            "output columns are coil → load → collector with no collector trunk",
            "outp/outn run one straight Metal5 column from the collector to the pad",
            f"coil pin row {coil_half_h:.1f} um half-height above highest p-tap "
            f"(PWB_f={rule('PWB_f'):.2f} um) plus {COIL_ACTIVE_GAP:.0f} um clearance",
            f"drawn nlp interconnect {interconnect_um:.1f} um per side",
            "two power grids: core ring plus a pad-band ring, stitched with "
            "TopMetal1 straps (vdd on the axis, vss at ±"
            f"{VSS_STRAP_DX:.0f} um)"
            if pad_ring is not None else "single power ring; pad band has no ring",
        ],
    )
    block.em_segments = em_segments
    block.ring = ring
    block.pad_ring = pad_ring
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

    def _bucket(other: str, net: str) -> str:
        name = other.lower()
        if name.startswith("m2_n") or "floating" in name:
            return "other"
        if name in {"vdd", "vss"} and net in {"outp", "outn"}:
            return "esd"
        if name.startswith("m7_") or name.startswith("pad"):
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
        if "**FLOATING" in line:
            continue
        match = _C_LINE.match(line.strip())
        if not match:
            continue
        a, b = match.group(2), match.group(3)
        c = _cap_value(match.group(4), match.group(5))
        for net in ("outp", "outn"):
            if a == net:
                out[net][_bucket(b, net)] += c / 2.0
            elif b == net:
                out[net][_bucket(a, net)] += c / 2.0
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
            physical_ff = float(entry.get("pex", {}).get("per_net_capacitance_f", {}).get(net, 0.0)) * 1e15
            interconnect_ff = sum(parts.values()) * 1e15
            pad_c_model = float(params.get("PAD_C", 0.0)) * 1e15
            detail = ", ".join(f"{k}={v * 1e15:.1f} fF" for k, v in parts.items() if v > 0)
            print(
                f"  pad C   {net}  {physical_ff:.1f} fF physical (Magic per-net); "
                f"{interconnect_ff:.1f} fF explicit C lines ({detail}); "
                f"hand pad model {pad_c_model:.1f} fF; MEMORY ESD pair ~50.9 fF at 1.4 V"
            )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
