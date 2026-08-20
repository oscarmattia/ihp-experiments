#!/usr/bin/env python3
"""The CTLE stage: symmetric placement, a power ring and EM-sized buses.

The cell is ``ctle_dut`` — the same name as the subcircuit in
``circuits/ctle56n/spice/ctle_pdk.cir``, because it is meant to be the same cell.
``layout/common/parity.py`` checks that device for device on every run.

Floorplan, centred on one vertical axis with the two differential halves as exact
mirror images:

                 outp     outn                    Metal4, out of the top edge
    ╔═════════════╪════════╪══════════════╗   ring: TopMetal2 horizontal,
    ║             └── vdd ──┘              ║   TopMetal1 vertical, tapped on
    ║   coil ══════╤═╧╤══════════ coil     ║   the axis
    ║   body     vdd  nlp        body      ║   M135 / R270, pins inboard,
    ║              └─┐ └─┐                 ║   vdd above nlp, bodies outboard
    ║              rppd rppd               ║   loads, mirrored
    ║               Q1 ── Q2               ║   HBT pair, mirrored
    ║               e1 ─ Rs‖Cs ─ e2        ║   degeneration, centred
    ║   ┌───── guard ring (NMOS only) ──┐  ║
    ║   │  diode │ tail1 │ tail2        │  ║   strapped 243 um arrays
    ║   └───────────────┬────────────────┘ ║
    ╚═══════════════════╪══════════════════╝   vss taps the ring here
                  inp   │   inn                Metal4, out of the bottom edge

Vertical order is not cosmetic: MEMORY.md records that shunt peaking must be
wired VDD -> L -> RD -> collector, and that the coil's port capacitance lands on
the internal nlp node rather than in the output load, so it stays out of C_L.

Usage:
    python layout/blocks/ctle_stage.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from layout.blocks.draw import (
    device_bbox_at,
    mirrored_pair_x,
    place,
    poly_contact,
    rect,
    snap,
    trunk_net,
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
from layout.common.rules import route_width
from layout.common.sizing import metres, read_params
from layout.common.spec import DeviceSpec, Terminal
from layout.common.wrap import derive_terminals

OUT_DIR = Path(__file__).resolve().parent / "out" / "ctle_stage"

#: Cell name, shared with the schematic subcircuit.
CELL = "ctle_dut"

#: Pin order, matching ctle_pdk.cir exactly.
PORT_NETS = ["outp", "outn", "inp", "inn", "vdd", "vss", "mgate"]

#: Vertical gap between floorplan rows, in um.
ROW_GAP = 8.0

#: Gap between the two coils' pin columns, in um.
#:
#: The coils face each other: each one's pins are on the edge nearest the axis and
#: its 108 um body extends outward, so vdd is a short strap between two adjacent
#: pins instead of a run across the cell. The gap has to leave a channel wide
#: enough for everything that crosses the coil row vertically — the nlp drops, the
#: output trunks and the vdd riser — since the coil bodies block everything else.
COIL_PIN_GAP = 44.0

#: Clearance from the highest p-tap to the bottom of a coil's body, in um.
#:
#: The coil's pwell-block marker covers its whole 108 um cell and PWB.f wants
#: 0.24 um between that marker and any p-tap, so a coil whose body reaches down
#: over the HBT row puts both substrate ties in violation. Facing the coils inward
#: means their bodies extend down as far as they extend up, so the pin row has to
#: sit a full half-height above the HBTs.
PWB_TAP_CLEARANCE = 2.0

#: Half-separations from the axis, in um: loads, HBT pair, output trunks. All of
#: them stay inside the channel between the coil bodies, so nothing has to cross a
#: spiral.
LOAD_DX = 12.0
HBT_DX = 8.0
OUT_TRUNK_DX = 17.0

#: Sideways offset of the base via from the base pin, in um.
#:
#: The HBT stacks collector, emitter and base at one x, with only 0.23 um between
#: the base's Metal1 bar and the emitter's Metal2 block. Via stacks dropped below
#: each of them land 1.13 um apart and short, which LVS reported as the base merged
#: into the emitter. The base therefore leaves sideways along its own bar and the
#: emitter keeps the downward offset.
BASE_VIA_DX = 2.2

#: Metal the signals leave the cell on, at both edges.
#:
#: Metal4 passes under the ring's TopMetal2 and TopMetal1 runs and under the coils,
#: so a signal can reach an edge without breaking the grid. The degeneration
#: capacitor and its Metal4 leg are the only other Metal4 in the stage and both sit
#: within a few um of the axis, well inside the trunk positions.
PORT_METAL = "Metal4"

#: How far a signal port sits outside the ring, in um.
PORT_REACH = 6.0

#: Metal the inputs run on from the base tap down to the bottom edge. Same as
#: PORT_METAL, so no via is needed on the way out.
IN_METAL = PORT_METAL

#: Drop from the coil pin row to the horizontal part of the nlp runs, in um. Below
#: the pin row, so the nlp jogs and the vdd strap never share a y on TopMetal2.
NLP_ROW_DROP = 8.0

#: Clearance from the enclosed geometry to the power ring, in um. The plan asks for
#: at least 10; the ring box is also squared about the axis, so the ring ends up
#: further than this from the coils and equidistant from both.
RING_CLEARANCE = 10.0

#: Gap between adjacent MOS arrays, in um. Wide enough that the two arrays' bus
#: rails, which overhang their array by the rail width, leave a clear channel
#: between them for the mgate contact and the diode tie.
ARRAY_GAP = 12.0

#: Metal for the inter-row differential nets: top metal minus two, leaving
#: TopMetal1 for the ring's vertical runs so the two never contend for a layer.
ROUTE_METAL = "Metal5"

#: Chip-level rules the stage cannot satisfy alone: the coils' local back-end
#: markers are chip-area shapes with a 100 um minimum width and a spacing rule
#: between regions. Latch-up is enforced.
CHIP_LEVEL_ALLOWED = ("LBE.a", "LBE.c")


def build_ctle_stage(params: dict[str, float] | None = None,
                     black_box: tuple[str, ...] = ()) -> Block:
    """Place and wire one CTLE stage."""
    from layout.devices.catalog import ctle_devices

    p = params or read_params()
    devices = {spec.name: spec for spec in ctle_devices(p)}
    bb_kinds = set(black_box)
    pya = pya_module()
    lm = layer_map()

    coil = devices["inductor_turn1_d40"]
    load = devices["rppd_load"]
    hbt = devices["npn13G2_pair_device"]
    rdeg = devices["rsil_degen"]
    cdeg = devices["cmomi_cs"]

    # Operating-point currents drive every wire width from here on.
    i_tail = float(p["ITAIL"])
    i_supply = 3.0 * i_tail  # two tails plus the mirror reference leg
    tail_w = metres(p, "MOS_W")
    tail_l = metres(p, "MOS_L")

    layout = new_layout()
    cell = layout.create_cell(CELL)
    instances: list[tuple[DeviceSpec, dict[str, str]]] = []
    em_segments: list[em.Segment] = []

    # --- NMOS row: bias diode, then the two tails, tails centred on the axis --
    arrays = {
        "mdiode": build_mos_array("mdiode", tail_w, tail_l, current_a=i_tail),
        "tail1": build_mos_array("tail1", tail_w, tail_l, current_a=i_tail),
        "tail2": build_mos_array("tail2", tail_w, tail_l, current_a=i_tail),
    }
    array_box = arrays["tail1"].cell.dbbox()
    array_w = array_box.width()
    array_gap = ARRAY_GAP

    # The two tails straddle the axis with array_gap between them. The bias diode
    # goes to the left of tail1, outside the symmetric core, because it carries no
    # signal and mirroring it would break the differential match. The axis is
    # chosen so the leftmost array starts at x=0.
    axis = snap(2 * array_w + 1.5 * array_gap)
    placement = {
        "tail1": snap(axis - array_gap / 2.0 - array_w),
    }
    # Arrays are placed by bounding box, but the tails must mirror by device area:
    # build_mos_array extends the box by rail_width on each side for the Metal2 bus
    # overhang, so a box-symmetric tail2 put inp over tail1's empty rail band and
    # inn over tail2's active devices — 7.9% inp/inn node capacitance mismatch in
    # post-layout extraction despite identical trunk geometry.
    rail_w = arrays["tail1"].rail_width_um
    device_span = array_w - 2 * rail_w
    placement["tail2"] = snap(2 * axis - placement["tail1"] - device_span)
    placement["mdiode"] = snap(placement["tail1"] - array_gap - array_w)

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

    # One device per array in the netlist: the deck merges the drawn parallel
    # units into a single device of the total width, which is how the schematic
    # describes it too.
    instances += [
        (arrays["mdiode"].total_spec.with_name("mdiode"),
         {"D": "mgate", "G": "mgate", "S": "vss", "sub": "vss"}),
        (arrays["tail1"].total_spec.with_name("tail1"),
         {"D": "e1", "G": "mgate", "S": "vss", "sub": "vss"}),
        (arrays["tail2"].total_spec.with_name("tail2"),
         {"D": "e2", "G": "mgate", "S": "vss", "sub": "vss"}),
    ]

    # Source rails and gate straps tie across all three arrays.
    nmos_left = snap(min(placement.values()) - 1.0)
    nmos_right = snap(max(placement.values()) + array_box.width() + 1.0)

    # The shared vss rail carries all three devices' current, so it is much wider
    # than one array's own source rail. It has to grow *downward* from that rail:
    # centred on it, the extra width reached up into the drain via columns, which
    # start at the bottom of the active area, and shorted e1 and e2 to vss. LVS
    # caught it as one merged net with a single 729 um transistor.
    vss_rail_w = snap(max(em.width_for_a("Metal2", i_supply), route_width("Metal2")))
    source_rail_top = snap(
        nmos_ports["S_tail1"].center[1] + arrays["tail1"].rail_width_um / 2
    )
    vss_rail_bottom = snap(source_rail_top - vss_rail_w)
    vss_rail_y = snap(source_rail_top - vss_rail_w / 2)
    rect(layout, cell, "Metal2", nmos_left, vss_rail_bottom, nmos_right, source_rail_top)
    em_segments.append(em.Segment("vss.rail", "Metal2", width_um=vss_rail_w,
                                  current_a=i_supply, note="shared source rail"))

    gate_y = nmos_ports["G_tail1"].center[1]
    poly = lm["gatpoly_drw"]
    cell.shapes(layout.layer(poly[0], poly[1])).insert(
        pya.DBox(nmos_left, snap(gate_y - 0.3), nmos_right, snap(gate_y + 0.3))
    )

    # mgate leaves the poly strap in the clear channel between the diode array and
    # tail1. Nothing else is there: each array's bus rails overhang it by the rail
    # width, and the channel is wider than twice that. Routing this over the array
    # instead put Metal2 0.19 um from the drain stubs.
    diode_right = snap(placement["mdiode"] + array_box.right)
    tail1_left = snap(placement["tail1"] + array_box.left)
    channel_x = snap((diode_right + tail1_left) / 2.0)
    gate_tap = poly_contact(layout, cell, channel_x, gate_y)
    via_between(layout, cell, channel_x, gate_y, "Metal1", "Metal2", columns=1, rows=1)

    # Diode connection: the diode's drain rail comes back to that same gate tap,
    # which is what makes it diode connected and what sets mgate. The rail
    # overhangs its array into the channel, so the link is a short vertical run
    # hugging the rail's inner edge, clear of tail1's drain rail (net e1) on the
    # other side of the channel.
    diode_rail = nmos_ports["D_mdiode"]
    link_x = snap(diode_right - arrays["mdiode"].rail_width_um / 2)
    link_w = route_width("Metal2")
    rect(layout, cell, "Metal2", link_x - link_w / 2, gate_y,
          link_x + link_w / 2, diode_rail.center[1])
    rect(layout, cell, "Metal2", min(link_x, channel_x), gate_y - link_w / 2,
          max(link_x, channel_x), gate_y + link_w / 2)

    nmos_box = pya.DBox(nmos_left, vss_rail_bottom, nmos_right,
                        snap(array_box.top))
    # Guard ring around the NMOS only. Its taps are strapped to the vss rail, so
    # the substrate really is vss and the netlist's bulk connection is true.
    guard = add_guard_ring(layout, cell, nmos_box, RingSpec(kind="ptap1", clearance=2.0))
    guard_box = guard["outer_box_um"]
    # The taps sit at a pitch, so the ring's Metal1 is not continuous and the taps
    # are one net only through the substrate. One via is therefore enough to make
    # the substrate be vss, and it has to land inside a tap's own Metal1: a strap
    # drawn along the ring edge instead left a 0.1 um sliver against the tap pads
    # and reported Metal1 and contact spacing violations all along the row.
    tap_x, tap_y = min(
        guard["tap_centres_um"],
        key=lambda c: (abs(c[1] - guard_box[1]) > 0.1, abs(c[0] - nmos_left)),
    )
    via_between(layout, cell, tap_x, tap_y, "Metal1", "Metal2", columns=1, rows=1)
    rect(layout, cell, "Metal2",
          tap_x - vss_rail_w / 2, tap_y, tap_x + vss_rail_w / 2, vss_rail_y)

    nmos_top = snap(max(array_box.top, guard_box[3]))

    # --- degeneration, centred on the axis ---------------------------------
    # The resistor is rotated R270 so PLUS comes out on its left and MINUS on its
    # right, matching e1 on the left half and e2 on the right. The capacitor keeps
    # feed=same (MEMORY.md: feed=double self-resonates in a 28 GHz path), so both
    # of its terminals sit at one point, PLUS on Metal4 and MINUS on Metal5; p and
    # n are separated by which metal they leave on rather than by orientation.
    #
    # The capacitor is rotated R90 to bring that feed point onto its bottom edge.
    # Left unrotated the feed is on its left edge and the legs have to cross the
    # finger field to reach it, which is where the Metal4 and Metal5 spacing
    # violations were coming from.
    row_y = snap(nmos_top + ROW_GAP)
    rdeg_i = rdeg.with_name("rdeg")
    cdeg_i = cdeg.with_name("cdeg")
    _, rdeg_probe = build(rdeg_i)
    rdeg_span = rdeg_probe.dbbox().height()
    rdeg_t, rdeg_box = place(layout, cell, rdeg_i, snap(axis - rdeg_span / 2.0), row_y, "R270")

    _, cdeg_probe = build(cdeg_i)
    cdeg_w = cdeg_probe.dbbox().height()  # R90 swaps the cell's extents
    cdeg_dx = snap(axis + cdeg_w / 2.0)
    cdeg_dy = snap(rdeg_box.top + ROW_GAP)
    cdeg_bb = cdeg_i.kind in bb_kinds
    cdeg_t, cdegplace_box = place(
        layout, cell, cdeg_i, cdeg_dx, cdeg_dy, "R90", black_box=cdeg_bb,
    )
    cdeg_box = (
        device_bbox_at(cdeg_i, cdeg_dx, cdeg_dy, "R90") if cdeg_bb else cdegplace_box
    )

    # Legs run out of the cap's bottom edge, down into the clear band above the
    # resistor, then sideways to each resistor terminal. Metal4 for p and Metal5
    # for n, so the two paths may share x and y freely.
    leg_y = snap((rdeg_box.top + cdeg_box.bottom) / 2.0)
    for terminal, target, leg_metal in (
        (rdeg_t["PLUS"], cdeg_t["PLUS"], "Metal4"),
        (rdeg_t["MINUS"], cdeg_t["MINUS"], "Metal5"),
    ):
        w = route_width(leg_metal)
        rx, ry = terminal.center
        fx, fy = target.center
        via_between(layout, cell, rx, ry, "Metal1", leg_metal)
        rect(layout, cell, leg_metal, rx - w / 2, ry, rx + w / 2, leg_y)
        rect(layout, cell, leg_metal, min(rx, fx), leg_y - w / 2, max(rx, fx), leg_y + w / 2)
        rect(layout, cell, leg_metal, fx - w / 2, leg_y, fx + w / 2, fy)

    instances += [
        (rdeg_i, {"PLUS": "e1", "MINUS": "e2", "sub": "vss"}),
        (cdeg_i, {"PLUS": "e1", "MINUS": "e2"}),
    ]
    degen_top = snap(max(rdeg_box.top, cdeg_box.top))

    # --- HBT pair ----------------------------------------------------------
    row_y = snap(degen_top + ROW_GAP)
    q1 = hbt.with_name("q1")
    q2 = hbt.with_name("q2")
    q_left, q_right = mirrored_pair_x(hbt, axis, gap=2 * HBT_DX)
    q1_t, q_box = place(layout, cell, q1, q_left, row_y)
    q2_t, _ = place(layout, cell, q2, q_right, row_y, "M90")
    instances += [
        (q1, {"C": "outp", "B": "inp", "E": "e1", "sub": "vss"}),
        (q2, {"C": "outn", "B": "inn", "E": "e2", "sub": "vss"}),
    ]
    hbt_top = snap(q_box.top)

    # --- loads, kept near the axis above their transistors -----------------
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

    # --- coils, facing each other with vdd on top --------------------------
    # M135 and R270 put each coil's two pins on the edge nearest the axis, stacked
    # vertically with PLUS above MINUS, and send the 108 um body outward. So vdd is
    # a short strap between two adjacent pins and each nlp leaves directly below
    # its own vdd, which is what keeps the coil row's routing simple.
    #
    # The price is that the body now extends as far down as it does up, and its
    # pwell-block marker covers all of it while PWB.f wants 0.24 um to any p-tap.
    # The pin row therefore sits a full half-height above the HBTs, whose substrate
    # ties are the highest ones in the cell. Everything between them and the coils
    # is either p-tap free or inside the channel between the two bodies.
    _, coil_probe = build(coil)
    coil_half_h = snap(coil_probe.dbbox().width() / 2.0)
    row_y = snap(max(load_top + ROW_GAP, hbt_top + coil_half_h + PWB_TAP_CLEARANCE))
    l1 = coil.with_name("l1")
    l2 = coil.with_name("l2")
    l1_dx = snap(axis - COIL_PIN_GAP / 2)
    l2_dx = snap(axis + COIL_PIN_GAP / 2)
    coil_bb = l1.kind in bb_kinds
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
    # The clear channel between the two bodies. Every vertical crossing of the coil
    # row has to stay inside it.
    channel = (snap(l1_box.right), snap(l2_box.left))

    # --- vdd strap across the top of the coil pins -------------------------
    # Drawn at the feed's own width and y, so it is a straight continuation of both
    # feeds. The deck derives the coil's w, s and d from the winding geometry inside
    # the ind_drw marker, and the marker covers the whole coil cell: a strap of a
    # different width meets the feed inside it and is measured as part of the
    # winding. A 3 um strap on a 4 um feed had the coils extracting as w=1.5 um,
    # d=45 um against the drawn 4 um and 40 um.
    strap_w = snap(l1_t["PLUS"].width)
    vdd_y = l1_t["PLUS"].center[1]
    rect(layout, cell, "TopMetal2",
          l1_t["PLUS"].center[0], vdd_y - strap_w / 2,
          l2_t["PLUS"].center[0], vdd_y + strap_w / 2)
    em_segments.append(em.Segment("vdd.strap", "TopMetal2", width_um=strap_w,
                                  current_a=i_supply, note="between the coil supply feeds"))

    # --- nlp nets: feed continued inward, then down to the load ------------
    # The horizontal part leaves the pin at the feed's own width and y, for the same
    # reason the vdd strap does: it is a straight continuation of the feed, so the
    # winding measurement inside ind_drw is unchanged. Turning down at the pin
    # instead adds a stub perpendicular to the feed *inside* the marker, and the
    # deck then measured both coils as w=1.5 um, d=45 um against the drawn 4 um and
    # 40 um. Extracted geometry, not just connectivity, depends on how a coil is
    # approached.
    #
    # The turn happens at the load's own x, which is well clear of both markers, so
    # the two runs end 2 * LOAD_DX apart on different x. Drawn as two long
    # horizontal runs at one shared y they read as a single broken conductor even
    # though the deck saw two nets.
    nlp_w = snap(l1_t["MINUS"].width)
    interconnect_um = 0.0
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

    # --- differential nets on Metal5, trunks placed symmetrically ----------
    sig_w = snap(max(em.width_for_a(ROUTE_METAL, i_tail), route_width(ROUTE_METAL)))
    trunk_out = snap(array_box.width() * 0.5)
    trunk_net(layout, cell, [q1_t["E"], nmos_ports["D_tail1"], rdeg_t["PLUS"]],
               trunk_x=axis - trunk_out, metal=ROUTE_METAL, width=sig_w)
    trunk_net(layout, cell, [q2_t["E"], nmos_ports["D_tail2"], rdeg_t["MINUS"]],
               trunk_x=axis + trunk_out, metal=ROUTE_METAL, width=sig_w)
    # The output trunks run outboard of the loads rather than through them: the
    # load's own via stack up to nlp includes Metal5, so a trunk sharing that x
    # would short the output to nlp.
    out_trunk_top = {
        "outp": trunk_net(layout, cell, [q1_t["C"], rd1_t[lower.name]],
                           trunk_x=axis - OUT_TRUNK_DX, metal=ROUTE_METAL, width=sig_w)[1],
        "outn": trunk_net(layout, cell, [q2_t["C"], rd2_t[lower.name]],
                           trunk_x=axis + OUT_TRUNK_DX, metal=ROUTE_METAL, width=sig_w)[1],
    }
    em_segments += [
        em.Segment(net, ROUTE_METAL, width_um=sig_w, current_a=i_tail, note="emitter/collector run")
        for net in ("e1", "e2", "outp", "outn")
    ]

    # --- base taps, offset sideways along the base bar ---------------------
    # Inboard on each device, so the two inputs come down either side of the axis
    # and stay symmetric.
    in_trunk_x: dict[str, float] = {}
    for base, sign, name in ((q1_t["B"], +1.0, "inp"), (q2_t["B"], -1.0, "inn")):
        bx, by = base.center
        vx = snap(bx + sign * BASE_VIA_DX)
        stub_h = snap(base.width if base.width < 1.0 else route_width("Metal1"))
        rect(layout, cell, "Metal1", min(bx, vx), by - stub_h / 2, max(bx, vx), by + stub_h / 2)
        via_between(layout, cell, vx, by, "Metal1", IN_METAL, columns=1, rows=1)
        in_trunk_x[name] = vx

    # --- power ring ---------------------------------------------------------
    # Squared about the axis so the ring is the same distance from each coil. The
    # bias diode hangs off the left of the NMOS row, so it is the left side that
    # sets the half-width and the right side that widens to match.
    devices_box = cell.dbbox()
    if coil_bb:
        # Ring clearance is measured from the coils too; black-boxing omits their
        # drawn geometry but the floorplan footprint must stay put.
        devices_box = devices_box + l1_box + l2_box
    if cdeg_bb:
        devices_box = devices_box + cdeg_box
    ring_half = snap(max(axis - devices_box.left, devices_box.right - axis))
    ring = add_power_ring(
        layout, cell,
        pya.DBox(snap(axis - ring_half), devices_box.bottom,
                 snap(axis + ring_half), devices_box.top),
        currents={"vss": i_supply, "vdd": i_supply},
        clearance=RING_CLEARANCE,
    )
    em_segments += ring.em_segments

    # --- supply connections, both on the symmetry axis ---------------------
    # vss goes down from the source rail to the inner ring run, vdd up from the
    # coil strap to the outer one. Being on the axis makes each one symmetric with
    # respect to the two halves it feeds.
    #
    # vdd has to cross the inner vss run to reach the outer vdd run, so it makes
    # that crossing on TopMetal1, which the ring uses only for its vertical sides
    # out at the cell edge. Without a physical connection the ring is a floating
    # conductor that happens to share a label, and LVS cannot see that: no device
    # touches the ring, so it never appears in the compare.
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

    # --- signal ports, outside the ring ------------------------------------
    # The stage is meant to cascade, so the outputs leave at the top edge towards
    # the next stage and the inputs at the bottom towards the previous one. Both
    # cross under the ring's TopMetal2 runs rather than breaking the grid: the
    # outputs on Metal5, which also carries them past the coils, and the inputs on
    # Metal3, which nothing else in the stage uses.
    ring_box = ring.outer_box
    port_top = snap(ring_box[3] + PORT_REACH)
    port_bottom = snap(ring_box[1] - PORT_REACH)
    signal_ports: dict[str, Terminal] = {}
    for name, x, y_from, y_to, orientation in (
        ("outp", axis - OUT_TRUNK_DX, out_trunk_top["outp"], port_top, 90.0),
        ("outn", axis + OUT_TRUNK_DX, out_trunk_top["outn"], port_top, 90.0),
        ("inp", in_trunk_x["inp"], q1_t["B"].center[1], port_bottom, 270.0),
        ("inn", in_trunk_x["inn"], q2_t["B"].center[1], port_bottom, 270.0),
    ):
        x = snap(x)
        # The outputs arrive on the Metal5 collector trunk and change to Metal4 at
        # the top of it; the inputs are already on Metal4 from the base tap.
        if name.startswith("out"):
            via_between(layout, cell, x, y_from, PORT_METAL, ROUTE_METAL)
        rect(layout, cell, PORT_METAL, x - sig_w / 2, min(y_from, y_to),
              x + sig_w / 2, max(y_from, y_to))
        signal_ports[name] = Terminal(
            name=name, layer=f"{PORT_METAL.lower()}_drw",
            center=(x, y_to), width=sig_w, orientation=orientation,
        )
        em_segments.append(
            em.Segment(name, PORT_METAL, width_um=sig_w, current_a=0.0,
                       note="signal trunk out of the cell edge")
        )

    ports = {
        "inp": signal_ports["inp"],
        "inn": signal_ports["inn"],
        "outp": signal_ports["outp"],
        "outn": signal_ports["outn"],
        "vdd": ring.ports["vdd"][0],
        "vss": ring.ports["vss"][0],
        "mgate": gate_tap,
    }

    for terminal, net in (
        (signal_ports["inp"], "inp"), (signal_ports["inn"], "inn"),
        (signal_ports["outp"], "outp"), (signal_ports["outn"], "outn"),
        (q1_t["E"], "e1"), (q2_t["E"], "e2"),
        (rd1_t[upper.name], "nlp1"), (rd2_t[upper.name], "nlp2"),
        (l1_t["PLUS"], "vdd"), (l2_t["PLUS"], "vdd"),
        (gate_tap, "mgate"), (nmos_ports["S_tail1"], "vss"),
        (ring.ports["vdd"][0], "vdd"), (ring.ports["vss"][0], "vss"),
    ):
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
                "tails": [placement["tail1"], placement["tail2"]],
                "hbt": [q_left, q_right],
                "loads": [rd_left, rd_right],
                "coil_feeds": [l1_t["PLUS"].center[0], l2_t["PLUS"].center[0]],
                "out_trunks": [snap(axis - OUT_TRUNK_DX), snap(axis + OUT_TRUNK_DX)],
                "in_trunks": [in_trunk_x["inp"], in_trunk_x["inn"]],
            },
        },
        notes=[
            f"cell name is {CELL}, shared with the schematic subcircuit",
            f"tails mirrored about x={axis:.2f} um; the bias diode sits to their "
            "left, outside the symmetric core because it carries no signal",
            "guard ring encloses the NMOS only and its taps are strapped to the "
            "vss rail, so the substrate really is vss",
            "coils rotated to face each other, so vdd is one straight TopMetal2 "
            "strap with the nlp feeds below it dropping onto the loads",
            f"wire widths from the technology LEF at the operating point: "
            f"{i_tail * 1e3:.2f} mA per tail, {i_supply * 1e3:.2f} mA supply",
            "coils face each other with vdd on top, so the supply is a short strap "
            "between two adjacent pins and each nlp leaves directly below its own "
            f"vdd; their bodies extend outward and the channel between them is "
            f"{channel[1] - channel[0]:.1f} um wide",
            f"the coil pin row sits {coil_half_h:.1f} um above the HBTs so the "
            "pwell-block markers clear their substrate ties",
            f"outputs leave at the top edge on {ROUTE_METAL} and inputs at the "
            f"bottom on {IN_METAL}, both passing under the ring rather than "
            "breaking it",
            f"ring squared about the axis at half-width {ring_half:.1f} um and "
            f"{RING_CLEARANCE:.0f} um clearance, so it is equidistant from both "
            "coils; the bias diode sets the left side and the right widens to match",
            f"drawn nlp interconnect {interconnect_um:.1f} um per side against the "
            f"{float(p.get('CL_INTERCONNECT', 0.0)) * 1e15:.2f} fF budget in params.inc",
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--no-render", action="store_true")
    parser.add_argument("--no-pex", action="store_true")
    args = parser.parse_args(argv)

    # No --black-box flag here on purpose. This entry point writes the tape-out
    # artifacts and gates them on parity against the full schematic and LVS against
    # the full CDL, none of which a deliberately reduced view can satisfy. Exposing
    # it as a flag would let a simulation view overwrite the tape-out GDS under the
    # same cell name and then be judged against the wrong netlist. The
    # `black_box` argument to build_ctle_stage() is the API; layout/blocks/
    # run_postlayout.py is the caller that gates it correctly.
    block = build_ctle_stage()
    code, _entry = run_stage_gates(
        block,
        args.out,
        schematic=Path("circuits/ctle56n/spice/ctle_pdk.cir"),
        subckt=CELL,
        allowed_rules=CHIP_LEVEL_ALLOWED,
        no_render=args.no_render,
        no_pex=args.no_pex,
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
