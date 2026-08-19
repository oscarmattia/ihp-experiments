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
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from layout.blocks.generators import Block, _connect_metal1_to
from layout.blocks.mos_array import build_mos_array
from layout.blocks.power_ring import add_power_ring
from layout.common import em
from layout.common.devices import build
from layout.common.drc import CONTEXT_RULES, run_drc
from layout.common.gds import stamp_net_labels
from layout.common.guard import RingSpec, add_guard_ring
from layout.common.layers import layer_map
from layout.common.lvs import run_lvs
from layout.common.netlist import write_block_cdl
from layout.common.parity import check_parity
from layout.common.pdk import new_layout, pya_module
from layout.common.pex import run_magic_pex, signal_resistors
from layout.common.render import render_gds
from layout.common.rules import grid, min_space, route_width
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

#: Gate contact geometry. Cnt.a makes Cont exactly 0.16 um; Cnt.d wants 0.07 um
#: of GatPoly around it and M1.c1 0.05 um of Metal1.
GATE_CONT_SIZE = 0.16
GATE_CONT_PITCH = 0.16 + 0.18  # Cnt.a + Cnt.b
GATE_CONT_CUTS = 4

#: Metal for the inter-row differential nets: top metal minus two, leaving
#: TopMetal1 for the ring's vertical runs so the two never contend for a layer.
ROUTE_METAL = "Metal5"

#: How far outside a device a via stack is placed, in um. A stack's landing pads
#: are wider than a device pin, so dropping one directly on a pin pushes contact
#: and via spacing rules against the device's own geometry.
VIA_OFFSET = 2.0

#: Chip-level rules the stage cannot satisfy alone: the coils' local back-end
#: markers are chip-area shapes with a 100 um minimum width and a spacing rule
#: between regions. Latch-up is enforced.
CHIP_LEVEL_ALLOWED = ("LBE.a", "LBE.c")


def _snap(value: float) -> float:
    g = grid()
    return round(round(value / g) * g, 6)


def _orient_trans(orientation: str):
    pya = pya_module()
    return {
        "R0": pya.DTrans.R0, "R90": pya.DTrans.R90, "R180": pya.DTrans.R180,
        "R270": pya.DTrans.R270, "M0": pya.DTrans.M0, "M45": pya.DTrans.M45,
        "M90": pya.DTrans.M90, "M135": pya.DTrans.M135,
    }[orientation]


def _device_bbox_at(spec: DeviceSpec, dx: float, dy: float, orientation: str = "R0"):
    """BBox a fully placed device would occupy, without drawing anything."""
    pya = pya_module()
    _, device_cell = build(spec)
    trans = pya.DTrans(_orient_trans(orientation), pya.DVector(_snap(dx), _snap(dy)))
    return trans * device_cell.dbbox()


def _place(layout, cell, spec: DeviceSpec, dx: float, dy: float, orientation: str = "R0",
           black_box: bool = False):
    """Place a device and return its terminals in stage coordinates."""
    from layout.common.route import metal_of

    pya = pya_module()
    device_layout, device_cell = build(spec)
    terminals = derive_terminals(spec, device_layout, device_cell)

    trans = pya.DTrans(_orient_trans(orientation), pya.DVector(_snap(dx), _snap(dy)))

    if not black_box:
        index = layout.add_cell(f"{spec.name}_{orientation}")
        layout.cell(index).copy_tree(device_cell)
        cell.insert(pya.DCellInstArray(index, trans))
        device_bbox = trans * layout.cell(index).dbbox()

    placed = {}
    pad_boxes: list = []
    for terminal in terminals:
        point = trans * pya.DPoint(*terminal.center)
        placed[terminal.name] = Terminal(
            name=terminal.name,
            layer=terminal.layer,
            center=(_snap(point.x), _snap(point.y)),
            width=terminal.width,
            orientation=terminal.orientation,
        )
        if black_box:
            metal = metal_of(terminal.layer)
            if metal is None:
                continue
            pad_w = _snap(terminal.width if terminal.width > 0 else route_width(metal))
            half = pad_w / 2
            cx, cy = placed[terminal.name].center
            _rect(layout, cell, metal, cx - half, cy - half, cx + half, cy + half)
            pad_boxes.append(pya.DBox(cx - half, cy - half, cx + half, cy + half))

    if black_box:
        # feed=same puts PLUS and MINUS at one x,y on Metal4 and Metal5; two pads
        # stacked there are correct and are not a short.
        if pad_boxes:
            bbox = pad_boxes[0]
            for box in pad_boxes[1:]:
                bbox += box
        else:
            bbox = pya.DBox(0, 0, 0, 0)
    else:
        bbox = device_bbox
    return placed, bbox


def _mirrored_pair_x(spec: DeviceSpec, axis: float, gap: float) -> tuple[float, float]:
    """Translations for a device and its mirror, exactly symmetric about ``axis``."""
    _, cell = build(spec)
    box = cell.dbbox()
    inner = gap / 2.0
    return _snap(axis - inner - box.right), _snap(axis + inner + box.right)


def _rect(layout, cell, metal: str, x0: float, y0: float, x1: float, y1: float) -> None:
    pya = pya_module()
    lm = layer_map()
    ld = lm[f"{metal.lower()}_drw"]
    cell.shapes(layout.layer(ld[0], ld[1])).insert(
        pya.DBox(_snap(min(x0, x1)), _snap(min(y0, y1)), _snap(max(x0, x1)), _snap(max(y0, y1)))
    )


def _via_between(layout, cell, x: float, y: float, bottom: str, top: str,
                 columns: int = 2, rows: int = 2) -> None:
    """Place one via stack between two metals at a point."""
    pya = pya_module()
    via = DeviceSpec(
        name=f"via_{bottom.lower()}_{top.lower()}",
        kind="via_stack",
        params={"b_layer": bottom, "t_layer": top, "columns": columns, "rows": rows},
    )
    _, via_cell = build(via)
    index = layout.add_cell(f"{via.name}_{_snap(x)}_{_snap(y)}_{columns}x{rows}")
    layout.cell(index).copy_tree(via_cell)
    box = via_cell.dbbox()
    cell.insert(
        pya.DCellInstArray(
            index, pya.DTrans(pya.DVector(_snap(x - box.center().x), _snap(y - box.center().y)))
        )
    )


def _via_up(layout, cell, terminal: Terminal, metal: str) -> tuple[float, float]:
    """Via stack from a terminal's metal up to ``metal``, on a stub outside it."""
    from layout.common.route import metal_of

    bottom = metal_of(terminal.layer)
    x, y = terminal.center
    if bottom is None:
        return (x, y)

    angle = terminal.orientation % 360.0
    dx = {0.0: VIA_OFFSET, 180.0: -VIA_OFFSET}.get(angle, 0.0)
    dy = {90.0: VIA_OFFSET, 270.0: -VIA_OFFSET}.get(angle, 0.0)
    vx, vy = _snap(x + dx), _snap(y + dy)

    stub_w = route_width(bottom)
    _rect(layout, cell, bottom,
          min(x, vx) - stub_w / 2, min(y, vy) - stub_w / 2,
          max(x, vx) + stub_w / 2, max(y, vy) + stub_w / 2)

    if bottom != metal:
        _via_between(layout, cell, vx, vy, bottom, metal)
    return (vx, vy)


def _poly_contact(layout, cell, x: float, y: float, cuts: int = GATE_CONT_CUTS) -> Terminal:
    """Contact a GatPoly strap up to Metal1 and return the Metal1 terminal.

    The nmos PCell leaves its gate as bare poly with no contact at all, so a gate
    net cannot leave the device without this. Cont is a fixed 0.16 um in this
    PDK (Cnt.a is both a minimum and a maximum), so a wider connection means more
    cuts rather than a bigger one.
    """
    pya = pya_module()
    lm = layer_map()
    span = (cuts - 1) * GATE_CONT_PITCH
    x0 = _snap(x - span / 2)

    cont = lm["cont_drw"]
    cont_layer = layout.layer(cont[0], cont[1])
    for i in range(cuts):
        cx = _snap(x0 + i * GATE_CONT_PITCH)
        cell.shapes(cont_layer).insert(
            pya.DBox(
                _snap(cx - GATE_CONT_SIZE / 2), _snap(y - GATE_CONT_SIZE / 2),
                _snap(cx + GATE_CONT_SIZE / 2), _snap(y + GATE_CONT_SIZE / 2),
            )
        )

    # Metal1 pad enclosing every cut, and a poly pad doing the same.
    pad_w = _snap(span + GATE_CONT_SIZE + 2 * 0.09)
    pad_h = _snap(GATE_CONT_SIZE + 2 * 0.09)
    _rect(layout, cell, "Metal1", x - pad_w / 2, y - pad_h / 2, x + pad_w / 2, y + pad_h / 2)
    poly = lm["gatpoly_drw"]
    cell.shapes(layout.layer(poly[0], poly[1])).insert(
        pya.DBox(_snap(x - pad_w / 2), _snap(y - pad_h / 2),
                 _snap(x + pad_w / 2), _snap(y + pad_h / 2))
    )
    return Terminal("gate_contact", "metal1_drw", (_snap(x), _snap(y)), pad_h, 90.0)


def _trunk_net(layout, cell, terminals: list[Terminal], trunk_x: float, metal: str,
               width: float | None = None) -> tuple[float, float]:
    """Vertical trunk on ``metal`` plus a horizontal stub per terminal.

    Returns the trunk's ``(bottom, top)``, so a caller can carry the net onward
    from where it ends rather than guessing.
    """
    w = width if width is not None else route_width(metal)
    trunk_x = _snap(trunk_x)
    points = [_via_up(layout, cell, t, metal) for t in terminals]
    ys = [py for _, py in points]
    _rect(layout, cell, metal, trunk_x - w / 2, min(ys), trunk_x + w / 2, max(ys))
    for x, y in points:
        _rect(layout, cell, metal, min(x, trunk_x), y - w / 2, max(x, trunk_x), y + w / 2)
    return (min(ys), max(ys))


def _vertical_net(layout, cell, terminals: list[Terminal], metal: str,
                  width: float | None = None) -> None:
    """Join terminals that share an x with a single vertical run."""
    w = width if width is not None else route_width(metal)
    xs = {t.center[0] for t in terminals}
    if len(xs) != 1:
        raise ValueError(f"vertical net needs one x, got {sorted(xs)}")
    x = xs.pop()
    points = [_via_up(layout, cell, t, metal) for t in terminals]
    ys = [py for _, py in points]
    _rect(layout, cell, metal, x - w / 2, min(ys), x + w / 2, max(ys))


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
    axis = _snap(2 * array_w + 1.5 * array_gap)
    placement = {
        "tail1": _snap(axis - array_gap / 2.0 - array_w),
    }
    # Arrays are placed by bounding box, but the tails must mirror by device area:
    # build_mos_array extends the box by rail_width on each side for the Metal2 bus
    # overhang, so a box-symmetric tail2 put inp over tail1's empty rail band and
    # inn over tail2's active devices — 7.9% inp/inn node capacitance mismatch in
    # post-layout extraction despite identical trunk geometry.
    rail_w = arrays["tail1"].rail_width_um
    device_span = array_w - 2 * rail_w
    placement["tail2"] = _snap(2 * axis - placement["tail1"] - device_span)
    placement["mdiode"] = _snap(placement["tail1"] - array_gap - array_w)

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
                center=(_snap(point.x), _snap(point.y)),
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
    nmos_left = _snap(min(placement.values()) - 1.0)
    nmos_right = _snap(max(placement.values()) + array_box.width() + 1.0)

    # The shared vss rail carries all three devices' current, so it is much wider
    # than one array's own source rail. It has to grow *downward* from that rail:
    # centred on it, the extra width reached up into the drain via columns, which
    # start at the bottom of the active area, and shorted e1 and e2 to vss. LVS
    # caught it as one merged net with a single 729 um transistor.
    vss_rail_w = _snap(max(em.width_for_a("Metal2", i_supply), route_width("Metal2")))
    source_rail_top = _snap(
        nmos_ports["S_tail1"].center[1] + arrays["tail1"].rail_width_um / 2
    )
    vss_rail_bottom = _snap(source_rail_top - vss_rail_w)
    vss_rail_y = _snap(source_rail_top - vss_rail_w / 2)
    _rect(layout, cell, "Metal2", nmos_left, vss_rail_bottom, nmos_right, source_rail_top)
    em_segments.append(em.Segment("vss.rail", "Metal2", width_um=vss_rail_w,
                                  current_a=i_supply, note="shared source rail"))

    gate_y = nmos_ports["G_tail1"].center[1]
    poly = lm["gatpoly_drw"]
    cell.shapes(layout.layer(poly[0], poly[1])).insert(
        pya.DBox(nmos_left, _snap(gate_y - 0.3), nmos_right, _snap(gate_y + 0.3))
    )

    # mgate leaves the poly strap in the clear channel between the diode array and
    # tail1. Nothing else is there: each array's bus rails overhang it by the rail
    # width, and the channel is wider than twice that. Routing this over the array
    # instead put Metal2 0.19 um from the drain stubs.
    diode_right = _snap(placement["mdiode"] + array_box.right)
    tail1_left = _snap(placement["tail1"] + array_box.left)
    channel_x = _snap((diode_right + tail1_left) / 2.0)
    gate_tap = _poly_contact(layout, cell, channel_x, gate_y)
    _via_between(layout, cell, channel_x, gate_y, "Metal1", "Metal2", columns=1, rows=1)

    # Diode connection: the diode's drain rail comes back to that same gate tap,
    # which is what makes it diode connected and what sets mgate. The rail
    # overhangs its array into the channel, so the link is a short vertical run
    # hugging the rail's inner edge, clear of tail1's drain rail (net e1) on the
    # other side of the channel.
    diode_rail = nmos_ports["D_mdiode"]
    link_x = _snap(diode_right - arrays["mdiode"].rail_width_um / 2)
    link_w = route_width("Metal2")
    _rect(layout, cell, "Metal2", link_x - link_w / 2, gate_y,
          link_x + link_w / 2, diode_rail.center[1])
    _rect(layout, cell, "Metal2", min(link_x, channel_x), gate_y - link_w / 2,
          max(link_x, channel_x), gate_y + link_w / 2)

    nmos_box = pya.DBox(nmos_left, vss_rail_bottom, nmos_right,
                        _snap(array_box.top))
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
    _via_between(layout, cell, tap_x, tap_y, "Metal1", "Metal2", columns=1, rows=1)
    _rect(layout, cell, "Metal2",
          tap_x - vss_rail_w / 2, tap_y, tap_x + vss_rail_w / 2, vss_rail_y)

    nmos_top = _snap(max(array_box.top, guard_box[3]))

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
    row_y = _snap(nmos_top + ROW_GAP)
    rdeg_i = rdeg.with_name("rdeg")
    cdeg_i = cdeg.with_name("cdeg")
    _, rdeg_probe = build(rdeg_i)
    rdeg_span = rdeg_probe.dbbox().height()
    rdeg_t, rdeg_box = _place(layout, cell, rdeg_i, _snap(axis - rdeg_span / 2.0), row_y, "R270")

    _, cdeg_probe = build(cdeg_i)
    cdeg_w = cdeg_probe.dbbox().height()  # R90 swaps the cell's extents
    cdeg_dx = _snap(axis + cdeg_w / 2.0)
    cdeg_dy = _snap(rdeg_box.top + ROW_GAP)
    cdeg_bb = cdeg_i.kind in bb_kinds
    cdeg_t, cdeg_place_box = _place(
        layout, cell, cdeg_i, cdeg_dx, cdeg_dy, "R90", black_box=cdeg_bb,
    )
    cdeg_box = (
        _device_bbox_at(cdeg_i, cdeg_dx, cdeg_dy, "R90") if cdeg_bb else cdeg_place_box
    )

    # Legs run out of the cap's bottom edge, down into the clear band above the
    # resistor, then sideways to each resistor terminal. Metal4 for p and Metal5
    # for n, so the two paths may share x and y freely.
    leg_y = _snap((rdeg_box.top + cdeg_box.bottom) / 2.0)
    for terminal, target, leg_metal in (
        (rdeg_t["PLUS"], cdeg_t["PLUS"], "Metal4"),
        (rdeg_t["MINUS"], cdeg_t["MINUS"], "Metal5"),
    ):
        w = route_width(leg_metal)
        rx, ry = terminal.center
        fx, fy = target.center
        _via_between(layout, cell, rx, ry, "Metal1", leg_metal)
        _rect(layout, cell, leg_metal, rx - w / 2, ry, rx + w / 2, leg_y)
        _rect(layout, cell, leg_metal, min(rx, fx), leg_y - w / 2, max(rx, fx), leg_y + w / 2)
        _rect(layout, cell, leg_metal, fx - w / 2, leg_y, fx + w / 2, fy)

    instances += [
        (rdeg_i, {"PLUS": "e1", "MINUS": "e2", "sub": "vss"}),
        (cdeg_i, {"PLUS": "e1", "MINUS": "e2"}),
    ]
    degen_top = _snap(max(rdeg_box.top, cdeg_box.top))

    # --- HBT pair ----------------------------------------------------------
    row_y = _snap(degen_top + ROW_GAP)
    q1 = hbt.with_name("q1")
    q2 = hbt.with_name("q2")
    q_left, q_right = _mirrored_pair_x(hbt, axis, gap=2 * HBT_DX)
    q1_t, q_box = _place(layout, cell, q1, q_left, row_y)
    q2_t, _ = _place(layout, cell, q2, q_right, row_y, "M90")
    instances += [
        (q1, {"C": "outp", "B": "inp", "E": "e1", "sub": "vss"}),
        (q2, {"C": "outn", "B": "inn", "E": "e2", "sub": "vss"}),
    ]
    hbt_top = _snap(q_box.top)

    # --- loads, kept near the axis above their transistors -----------------
    row_y = _snap(hbt_top + ROW_GAP)
    rd1 = load.with_name("rd1")
    rd2 = load.with_name("rd2")
    load_terms = {t.name: t for t in derive_terminals(load, *build(load))}
    upper = max(load_terms.values(), key=lambda t: t.center[1])
    lower = min(load_terms.values(), key=lambda t: t.center[1])
    rd_left = _snap(axis - LOAD_DX - upper.center[0])
    rd_right = _snap(axis + LOAD_DX + upper.center[0])
    rd1_t, rd_box = _place(layout, cell, rd1, rd_left, row_y)
    rd2_t, _ = _place(layout, cell, rd2, rd_right, row_y, "M90")
    instances += [
        (rd1, {upper.name: "nlp1", lower.name: "outp", "sub": "vss"}),
        (rd2, {upper.name: "nlp2", lower.name: "outn", "sub": "vss"}),
    ]
    load_top = _snap(rd_box.top)

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
    coil_half_h = _snap(coil_probe.dbbox().width() / 2.0)
    row_y = _snap(max(load_top + ROW_GAP, hbt_top + coil_half_h + PWB_TAP_CLEARANCE))
    l1 = coil.with_name("l1")
    l2 = coil.with_name("l2")
    l1_dx = _snap(axis - COIL_PIN_GAP / 2)
    l2_dx = _snap(axis + COIL_PIN_GAP / 2)
    coil_bb = l1.kind in bb_kinds
    l1_t, l1_place_box = _place(
        layout, cell, l1, l1_dx, row_y, "M135", black_box=coil_bb,
    )
    l2_t, l2_place_box = _place(
        layout, cell, l2, l2_dx, row_y, "R270", black_box=coil_bb,
    )
    if coil_bb:
        l1_box = _device_bbox_at(l1, l1_dx, row_y, "M135")
        l2_box = _device_bbox_at(l2, l2_dx, row_y, "R270")
    else:
        l1_box, l2_box = l1_place_box, l2_place_box
    instances += [
        (l1, {"PLUS": "vdd", "MINUS": "nlp1", "sub": "vss"}),
        (l2, {"PLUS": "vdd", "MINUS": "nlp2", "sub": "vss"}),
    ]
    # The clear channel between the two bodies. Every vertical crossing of the coil
    # row has to stay inside it.
    channel = (_snap(l1_box.right), _snap(l2_box.left))

    # --- vdd strap across the top of the coil pins -------------------------
    # Drawn at the feed's own width and y, so it is a straight continuation of both
    # feeds. The deck derives the coil's w, s and d from the winding geometry inside
    # the ind_drw marker, and the marker covers the whole coil cell: a strap of a
    # different width meets the feed inside it and is measured as part of the
    # winding. A 3 um strap on a 4 um feed had the coils extracting as w=1.5 um,
    # d=45 um against the drawn 4 um and 40 um.
    strap_w = _snap(l1_t["PLUS"].width)
    vdd_y = l1_t["PLUS"].center[1]
    _rect(layout, cell, "TopMetal2",
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
    nlp_w = _snap(l1_t["MINUS"].width)
    interconnect_um = 0.0
    for coil_pin, load_pin, name, turn_x in (
        (l1_t["MINUS"], rd1_t[upper.name], "nlp1", _snap(axis - LOAD_DX)),
        (l2_t["MINUS"], rd2_t[upper.name], "nlp2", _snap(axis + LOAD_DX)),
    ):
        feed_x, feed_y = coil_pin.center
        land_x, land_y = _via_up(layout, cell, load_pin, "TopMetal2")
        _rect(layout, cell, "TopMetal2", min(feed_x, turn_x), feed_y - nlp_w / 2,
              max(feed_x, turn_x), feed_y + nlp_w / 2)
        _rect(layout, cell, "TopMetal2", turn_x - nlp_w / 2, min(feed_y, land_y),
              turn_x + nlp_w / 2, max(feed_y, land_y))
        if abs(turn_x - land_x) > 1e-6:
            _rect(layout, cell, "TopMetal2", min(turn_x, land_x), land_y - nlp_w / 2,
                  max(turn_x, land_x), land_y + nlp_w / 2)
        interconnect_um += abs(feed_x - turn_x) + abs(feed_y - land_y)
        em_segments.append(
            em.Segment(name, "TopMetal2", width_um=nlp_w, current_a=i_tail,
                       note="coil feed continued inward, then down to the load")
        )

    # --- differential nets on Metal5, trunks placed symmetrically ----------
    sig_w = _snap(max(em.width_for_a(ROUTE_METAL, i_tail), route_width(ROUTE_METAL)))
    trunk_out = _snap(array_box.width() * 0.5)
    _trunk_net(layout, cell, [q1_t["E"], nmos_ports["D_tail1"], rdeg_t["PLUS"]],
               trunk_x=axis - trunk_out, metal=ROUTE_METAL, width=sig_w)
    _trunk_net(layout, cell, [q2_t["E"], nmos_ports["D_tail2"], rdeg_t["MINUS"]],
               trunk_x=axis + trunk_out, metal=ROUTE_METAL, width=sig_w)
    # The output trunks run outboard of the loads rather than through them: the
    # load's own via stack up to nlp includes Metal5, so a trunk sharing that x
    # would short the output to nlp.
    out_trunk_top = {
        "outp": _trunk_net(layout, cell, [q1_t["C"], rd1_t[lower.name]],
                           trunk_x=axis - OUT_TRUNK_DX, metal=ROUTE_METAL, width=sig_w)[1],
        "outn": _trunk_net(layout, cell, [q2_t["C"], rd2_t[lower.name]],
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
        vx = _snap(bx + sign * BASE_VIA_DX)
        stub_h = _snap(base.width if base.width < 1.0 else route_width("Metal1"))
        _rect(layout, cell, "Metal1", min(bx, vx), by - stub_h / 2, max(bx, vx), by + stub_h / 2)
        _via_between(layout, cell, vx, by, "Metal1", IN_METAL, columns=1, rows=1)
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
    ring_half = _snap(max(axis - devices_box.left, devices_box.right - axis))
    ring = add_power_ring(
        layout, cell,
        pya.DBox(_snap(axis - ring_half), devices_box.bottom,
                 _snap(axis + ring_half), devices_box.top),
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
    _via_between(layout, cell, axis, vss_rail_y, "Metal2", "TopMetal2", columns=3, rows=3)
    _rect(layout, cell, "TopMetal2", axis - strap_w / 2, vss_ring_y,
          axis + strap_w / 2, vss_rail_y)

    vdd_ring_y = ring.ports["vdd"][0].center[1]
    _via_between(layout, cell, axis, vdd_y, "TopMetal1", "TopMetal2", columns=1, rows=1)
    _rect(layout, cell, "TopMetal1", axis - strap_w / 2, vdd_y, axis + strap_w / 2, vdd_ring_y)
    _via_between(layout, cell, axis, vdd_ring_y, "TopMetal1", "TopMetal2", columns=1, rows=1)

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
    port_top = _snap(ring_box[3] + PORT_REACH)
    port_bottom = _snap(ring_box[1] - PORT_REACH)
    signal_ports: dict[str, Terminal] = {}
    for name, x, y_from, y_to, orientation in (
        ("outp", axis - OUT_TRUNK_DX, out_trunk_top["outp"], port_top, 90.0),
        ("outn", axis + OUT_TRUNK_DX, out_trunk_top["outn"], port_top, 90.0),
        ("inp", in_trunk_x["inp"], q1_t["B"].center[1], port_bottom, 270.0),
        ("inn", in_trunk_x["inn"], q2_t["B"].center[1], port_bottom, 270.0),
    ):
        x = _snap(x)
        # The outputs arrive on the Metal5 collector trunk and change to Metal4 at
        # the top of it; the inputs are already on Metal4 from the base tap.
        if name.startswith("out"):
            _via_between(layout, cell, x, y_from, PORT_METAL, ROUTE_METAL)
        _rect(layout, cell, PORT_METAL, x - sig_w / 2, min(y_from, y_to),
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
                "out_trunks": [_snap(axis - OUT_TRUNK_DX), _snap(axis + OUT_TRUNK_DX)],
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

    args.out.mkdir(parents=True, exist_ok=True)
    # No --black-box flag here on purpose. This entry point writes the tape-out
    # artifacts and gates them on parity against the full schematic and LVS against
    # the full CDL, none of which a deliberately reduced view can satisfy. Exposing
    # it as a flag would let a simulation view overwrite the tape-out GDS under the
    # same cell name and then be judged against the wrong netlist. The
    # `black_box` argument to build_ctle_stage() is the API; layout/blocks/
    # run_postlayout.py is the caller that gates it correctly.
    block = build_ctle_stage()
    entry = block.summary()

    gds = block.write(args.out)
    entry["gds"] = str(gds)
    print(f"  placed  {entry['bbox_um']['width']:.1f} x {entry['bbox_um']['height']:.1f} um, "
          f"{len(block.instances)} device(s), axis x={block.symmetry['axis_x_um']}")

    cdl = write_block_cdl(CELL, block.port_nets, block.instances, args.out / f"{CELL}.cdl")
    entry["cdl"] = str(cdl)

    # Parity against the schematic, before any geometry check: if the two
    # netlists disagree there is no point asking whether the layout matches one.
    parity = check_parity(
        Path("circuits/ctle56n/spice/ctle_pdk.cir"), cdl, subckt="ctle_dut"
    )
    parity.write(args.out / "parity.json")
    entry["parity"] = parity.to_dict()
    print(f"  parity  {'ok' if parity.ok else 'FAIL'}  {parity.summary()[:110]}")

    em_report = em.check_segments(block.em_segments)
    (args.out / "em.json").write_text(json.dumps(em_report, indent=2) + "\n")
    entry["em"] = em_report
    worst = max(
        (s for s in em_report["segments"] if s.get("checkable")),
        key=lambda s: s.get("utilisation") or 0.0,
        default=None,
    )
    detail = (
        f"worst {worst['net']} on {worst['layer']} at "
        f"{(worst['utilisation'] or 0) * 100:.0f}% of limit"
        if worst else "no checkable segments"
    )
    print(f"  EM      {'ok' if em_report['ok'] else 'FAIL ' + str(em_report['failures'])}  {detail}")

    if not args.no_render:
        png = render_gds(gds, args.out / f"{CELL}.png", width=1400)
        entry["png"] = str(png) if png else None

    allowed = set(CHIP_LEVEL_ALLOWED)
    drc = run_drc(gds=gds, run_dir=args.out / "drc_run", cell_name=CELL, allow_context=True)
    remaining = {r: c for r, c in drc.by_rule.items() if r not in allowed}
    drc_ok = not remaining and not drc.error
    entry["drc"] = drc.to_dict()
    entry["drc"]["ok"] = drc_ok
    entry["drc"]["allowed_chip_level_rules"] = sorted(allowed)
    entry["drc"]["enforced_context_rules"] = sorted(set(CONTEXT_RULES) - allowed)
    entry["drc"]["remaining_violations"] = remaining
    chip = {r: c for r, c in drc.by_rule.items() if r in allowed}
    print(f"  DRC     {'ok' if drc_ok else 'FAIL'}  chip-level={chip} remaining={remaining}")

    lvs = run_lvs(gds=gds, cdl=cdl, run_dir=args.out / "lvs_run", topcell=CELL,
                  disable_tap_extraction=True)
    entry["lvs"] = lvs.to_dict()
    print(f"  LVS     {'ok' if lvs.clean else 'FAIL'}  {lvs.summary[:70]}")

    if not args.no_pex:
        pex = run_magic_pex(gds=gds, cell=CELL, run_dir=args.out / "pex_run")
        entry["pex"] = pex.to_dict()
        if pex.ok:
            signal = signal_resistors(pex.resistor_elements)
            entry["pex"]["signal_resistance_ohm"] = round(sum(e["ohm"] for e in signal), 6)
            print(f"  PEX     ok  {pex.capacitors} C totalling "
                  f"{pex.total_capacitance * 1e15:.2f} fF, {pex.resistors} R")
        else:
            print(f"  PEX     FAIL {pex.error[:60]}")

    (args.out / f"{CELL}_summary.json").write_text(json.dumps(entry, indent=2) + "\n")
    return 0 if (drc_ok and lvs.clean and parity.ok and em_report["ok"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
