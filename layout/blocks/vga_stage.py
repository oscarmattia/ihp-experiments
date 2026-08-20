#!/usr/bin/env python3
"""The VGA stage: symmetric placement, a power ring and EM-sized buses.

The cell is ``vga_dut`` — the same name as the subcircuit in
``circuits/ctle56n/spice/vga_pdk.cir``. ``layout/common/parity.py`` checks that
device for device on every run.

Floorplan, centred on one vertical axis with mirror-image halves:

                 outp     outn                    Metal4, out of the top edge
    ╔═════════════╪════════╪══════════════╗   ring: TopMetal2 horizontal,
    ║             └── vdd ──┘              ║   TopMetal1 vertical, tapped on
    ║   coil ══════╤═╧╤══════════ coil     ║   the axis
    ║   body     vdd  nlp        body      ║   M135 / R270, pins inboard
    ║              └─┐ └─┐                 ║
    ║              rppd rppd               ║   loads, mirrored
    ║            Qd1 Q1 ── Q2 Qd2          ║   dummy outboard, signal inboard
    ║               └── em ──┘             ║   shared emitter strap, no Rs row
    ║   ┌───── guard ring (NMOS only) ──┐  ║
    ║   │ pd1 ps1 │      │ ps2 pd2     │  ║   upper MOS row: steering
    ║   │  tail1  │      │  tail2      │  ║   lower MOS row: mirror + tails
    ║   │ mdiode  │      │             │  ║
mgate ─┘                │                    Metal4, left edge
vicm ───────────────────┘                    Metal3, right edge
steerp/steern ─────────────────────────    Metal3, right edge
    ╚═══════════════════╪══════════════════╝   vss taps the ring here
                  inp   │   inn                Metal4, out of the bottom edge

Seven wide NMOS devices split across two rows so the cell stays narrow enough
for the coils to face each other without stretching every signal run.

Usage:
    python layout/blocks/vga_stage.py
"""

from __future__ import annotations

import argparse
import json
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
from layout.common.rules import min_space, route_width, rule
from layout.common.sizing import metres, read_params
from layout.common.spec import DeviceSpec, Terminal
from layout.common.wrap import derive_terminals

OUT_DIR = Path(__file__).resolve().parent / "out" / "vga_stage"
PARAMS = Path("circuits/ctle56n/spice/vga_params.inc")
SCHEMATIC = Path("circuits/ctle56n/spice/vga_pdk.cir")

CELL = "vga_dut"
PORT_NETS = [
    "outp", "outn", "inp", "inn", "vicm", "steerp", "steern", "vdd", "vss", "mgate",
]

ROW_GAP = 8.0
MOS_ROW_GAP = 12.0
COIL_PIN_GAP = 44.0
PWB_TAP_CLEARANCE = 2.0
LOAD_DX = 12.0
HBT_DX = 8.0
DUMMY_DX = 18.0
OUT_TRUNK_DX = 17.0
BASE_VIA_DX = 2.2
PORT_METAL = "Metal4"
CONTROL_METAL = "Metal3"
PORT_REACH = 6.0
IN_TRUNK_METAL = "Metal4"
RING_CLEARANCE = 10.0
ARRAY_GAP = 12.0
ROUTE_METAL = "Metal5"
CHIP_LEVEL_ALLOWED = ("LBE.a", "LBE.c")


def _place_array(layout, cell, array, dx: float, dy: float, prefix: str) -> dict[str, Terminal]:
    """Insert one MOS array and return its ports in stage coordinates."""
    pya = pya_module()
    index = layout.add_cell(f"{prefix}_cell")
    layout.cell(index).copy_tree(array.cell)
    trans = pya.DTrans(pya.DVector(dx, dy))
    cell.insert(pya.DCellInstArray(index, trans))
    ports: dict[str, Terminal] = {}
    for pin, terminal in array.ports.items():
        point = trans * pya.DPoint(*terminal.center)
        ports[f"{pin}_{prefix}"] = Terminal(
            name=f"{pin}_{prefix}",
            layer=terminal.layer,
            center=(snap(point.x), snap(point.y)),
            width=terminal.width,
            orientation=terminal.orientation,
        )
    return ports


def build_vga_stage(params: dict[str, float] | None = None,
                    black_box: tuple[str, ...] = (),
                    probe: str | None = None) -> Block:
    """Place and wire one VGA stage."""
    from layout.devices.catalog import COIL

    p = params or read_params(PARAMS)
    bb_kinds = set(black_box)
    pya = pya_module()
    lm = layer_map()

    coil = DeviceSpec(
        name="inductor_turn1_d40",
        kind="inductor",
        params=dict(COIL),
        note="shunt-peaking coil at the EM-characterized geometry",
    )
    load = DeviceSpec(
        name="rppd_load",
        kind="rppd",
        params={"w": p["RPPD_W"], "l": p["RPPD_L"]},
        note=f"VGA shunt-peaked load, RD target {p['RD']:.1f} ohm",
    )
    hbt = DeviceSpec(
        name="npn13G2_pair_device",
        kind="npn13G2",
        params={"Nx": int(p["Nx"])},
        note=f"VGA HBT pair, Nx={int(p['Nx'])}",
    )

    i_tail = float(p["ITAIL"])
    i_supply = 3.0 * i_tail
    tail_w = metres(p, "MOS_W")
    tail_l = metres(p, "MOS_L")
    steer_w = metres(p, "STEER_W")
    steer_l = metres(p, "STEER_L")

    layout = new_layout()
    cell = layout.create_cell(CELL)
    instances: list[tuple[DeviceSpec, dict[str, str]]] = []
    em_segments: list[em.Segment] = []

    arrays_lower = {
        "mdiode": build_mos_array("mdiode", tail_w, tail_l, current_a=i_tail),
        "tail1": build_mos_array("tail1", tail_w, tail_l, current_a=i_tail),
        "tail2": build_mos_array("tail2", tail_w, tail_l, current_a=i_tail),
    }
    arrays_upper = {
        "pd1": build_mos_array("pd1", steer_w, steer_l, current_a=i_tail),
        "ps1": build_mos_array("ps1", steer_w, steer_l, current_a=i_tail),
        "ps2": build_mos_array("ps2", steer_w, steer_l, current_a=i_tail),
        "pd2": build_mos_array("pd2", steer_w, steer_l, current_a=i_tail),
    }

    tail_box = arrays_lower["tail1"].cell.dbbox()
    steer_box = arrays_upper["ps1"].cell.dbbox()
    tail_array_w = tail_box.width()
    steer_array_w = steer_box.width()
    rail_w = arrays_lower["tail1"].rail_width_um
    steer_rail_w = arrays_upper["ps1"].rail_width_um
    device_span = tail_array_w - 2 * rail_w
    steer_span = steer_array_w - 2 * steer_rail_w
    array_gap = snap(max(
        ARRAY_GAP,
        2 * max(rail_w, steer_rail_w) + min_space("Metal2"),
        2 * max(rail_w, steer_rail_w) + rule("Act_b"),
    ))

    # Lower row: tails straddle the axis, mirror diode to their left. Upper row:
    # four steering arrays in two mirrored pairs above the tails. A single row of
    # all seven would be ~422 um wide before anything else; two rows keep the MOS
    # footprint near 210 um and leave room for facing coils.
    axis = snap(2 * tail_array_w + 1.5 * array_gap)
    placement_lower = {
        "tail1": snap(axis - array_gap / 2.0 - tail_array_w),
    }
    placement_lower["tail2"] = snap(2 * axis - placement_lower["tail1"] - device_span)
    placement_lower["mdiode"] = snap(placement_lower["tail1"] - array_gap - tail_array_w)

    pair_w = snap(2 * steer_array_w + array_gap)
    tail1_active_left = snap(placement_lower["tail1"] + rail_w)
    tail2_active_left = snap(placement_lower["tail2"] + rail_w)
    pair_left = snap(tail1_active_left + (device_span - pair_w) / 2.0)
    pair_right = snap(tail2_active_left + (device_span - pair_w) / 2.0)
    placement_upper = {
        "pd1": pair_left,
        "ps1": snap(pair_left + steer_array_w + array_gap),
        "ps2": pair_right,
        "pd2": snap(pair_right + steer_array_w + array_gap),
    }

    lower_y = 0.0
    lower_ports: dict[str, Terminal] = {}
    for name, array in arrays_lower.items():
        lower_ports.update(_place_array(layout, cell, array, placement_lower[name], lower_y, name))
        em_segments += array.em_segments

    lower_top = snap(tail_box.top)
    upper_y = snap(lower_top + MOS_ROW_GAP)
    upper_ports: dict[str, Terminal] = {}
    for name, array in arrays_upper.items():
        upper_ports.update(_place_array(layout, cell, array, placement_upper[name], upper_y, name))
        em_segments += array.em_segments

    nmos_left = snap(min(placement_lower["mdiode"], placement_upper["pd1"]) - 1.0)
    nmos_right = snap(
        max(placement_lower["tail2"] + tail_array_w,
            placement_upper["pd2"] + steer_array_w) + 1.0
    )

    vss_rail_w = snap(max(em.width_for_a("Metal2", i_supply), route_width("Metal2")))
    source_rail_top = snap(lower_ports["S_tail1"].center[1] + rail_w / 2)
    vss_rail_bottom = snap(source_rail_top - vss_rail_w)
    vss_rail_y = snap(source_rail_top - vss_rail_w / 2)
    vss_left = snap(min(placement_lower["mdiode"], placement_lower["tail1"]) - 1.0)
    vss_right = snap(max(placement_lower["tail2"] + tail_array_w,
                         placement_lower["tail1"] + tail_array_w) + 1.0)
    rect(layout, cell, "Metal2", vss_left, vss_rail_bottom, vss_right, source_rail_top)
    em_segments.append(em.Segment("vss.rail", "Metal2", width_um=vss_rail_w,
                                  current_a=i_supply, note="shared source rail"))

    gate_y_lower = lower_ports["G_tail1"].center[1]
    gate_y_upper = upper_ports["G_ps1"].center[1]
    link_w = route_width("Metal2")
    poly = lm["gatpoly_drw"]
    cell.shapes(layout.layer(poly[0], poly[1])).insert(
        pya.DBox(nmos_left, snap(gate_y_lower - 0.3), nmos_right, snap(gate_y_lower + 0.3))
    )

    def _steer_poly_strap(prefix: str) -> None:
        left = snap(placement_upper[prefix] + steer_rail_w)
        right = snap(placement_upper[prefix] + steer_span + steer_rail_w)
        cell.shapes(layout.layer(poly[0], poly[1])).insert(
            pya.DBox(left, snap(gate_y_upper - 0.3), right, snap(gate_y_upper + 0.3))
        )

    for prefix in ("pd1", "pd2", "ps1", "ps2"):
        _steer_poly_strap(prefix)

    diode_right = snap(placement_lower["mdiode"] + tail_box.right)
    tail1_left = snap(placement_lower["tail1"] + tail_box.left)
    channel_x = snap((diode_right + tail1_left) / 2.0)
    gate_tap = poly_contact(layout, cell, channel_x, gate_y_lower)
    via_between(layout, cell, channel_x, gate_y_lower, "Metal1", "Metal2", columns=1, rows=1)

    diode_rail = lower_ports["D_mdiode"]
    link_x = snap(diode_right - rail_w / 2)
    rect(layout, cell, "Metal2", link_x - link_w / 2, gate_y_lower,
          link_x + link_w / 2, diode_rail.center[1])
    rect(layout, cell, "Metal2", min(link_x, channel_x), gate_y_lower - link_w / 2,
          max(link_x, channel_x), gate_y_lower + link_w / 2)

    instances += [
        (arrays_lower["mdiode"].total_spec.with_name("mdiode"),
         {"D": "mgate", "G": "mgate", "S": "vss", "sub": "vss"}),
        (arrays_lower["tail1"].total_spec.with_name("tail1"),
         {"D": "tx1", "G": "mgate", "S": "vss", "sub": "vss"}),
        (arrays_lower["tail2"].total_spec.with_name("tail2"),
         {"D": "tx2", "G": "mgate", "S": "vss", "sub": "vss"}),
        (arrays_upper["ps1"].total_spec.with_name("ps1"),
         {"D": "tx1", "G": "steerp", "S": "em", "sub": "vss"}),
        (arrays_upper["ps2"].total_spec.with_name("ps2"),
         {"D": "tx2", "G": "steerp", "S": "em", "sub": "vss"}),
        (arrays_upper["pd1"].total_spec.with_name("pd1"),
         {"D": "tx1", "G": "steern", "S": "ed1", "sub": "vss"}),
        (arrays_upper["pd2"].total_spec.with_name("pd2"),
         {"D": "tx2", "G": "steern", "S": "ed2", "sub": "vss"}),
    ]

    nmos_box = pya.DBox(
        nmos_left, vss_rail_bottom, nmos_right,
        snap(max(tail_box.top, steer_box.top) + upper_y),
    )
    lower_nmos_box = pya.DBox(
        nmos_left, vss_rail_bottom, nmos_right, snap(tail_box.top + lower_y),
    )
    upper_nmos_box = pya.DBox(nmos_left, upper_y, nmos_right, snap(steer_box.top + upper_y))
    guard_lower = add_guard_ring(layout, cell, lower_nmos_box, RingSpec(kind="ptap1", clearance=2.0))
    guard_upper = add_guard_ring(layout, cell, upper_nmos_box, RingSpec(kind="ptap1", clearance=2.0))
    guard = guard_lower

    lower_guard_box = guard_lower["outer_box_um"]
    tap_x, tap_y = min(
        guard_lower["tap_centres_um"],
        key=lambda c: (abs(c[1] - lower_guard_box[1]) > 0.1, abs(c[0] - nmos_left)),
    )
    via_between(layout, cell, tap_x, tap_y, "Metal1", "Metal2", columns=1, rows=1)
    rect(layout, cell, "Metal2",
          tap_x - vss_rail_w / 2, tap_y, tap_x + vss_rail_w / 2, vss_rail_y)

    nmos_top = snap(max(
        nmos_box.top,
        guard_lower["outer_box_um"][3],
        guard_upper["outer_box_um"][3],
    ))

    sig_w = snap(max(em.width_for_a(ROUTE_METAL, i_tail), route_width(ROUTE_METAL)))
    ctrl_w = snap(max(route_width(CONTROL_METAL), sig_w * 0.5))

    # tx1/tx2 trunks sit outboard of the steering pairs so a vertical at the tail
    # drain x does not cross an array body (tail2 at x≈185 falls inside ps2).
    tx1_trunk_x = snap(placement_lower["mdiode"] - array_gap / 2)
    tx2_trunk_x = snap(placement_lower["tail2"] + tail_array_w + array_gap / 2)

    trunk_net(
        layout, cell,
        [lower_ports["D_tail1"], upper_ports["D_ps1"], upper_ports["D_pd1"]],
        trunk_x=tx1_trunk_x, metal=ROUTE_METAL, width=sig_w,
    )
    em_segments.append(
        em.Segment("tx1", ROUTE_METAL, width_um=sig_w, current_a=i_tail,
                   note="steering drains and tail1 on an outboard Metal5 trunk")
    )
    trunk_net(
        layout, cell,
        [lower_ports["D_tail2"], upper_ports["D_ps2"], upper_ports["D_pd2"]],
        trunk_x=tx2_trunk_x, metal=ROUTE_METAL, width=sig_w,
    )
    em_segments.append(
        em.Segment("tx2", ROUTE_METAL, width_um=sig_w, current_a=i_tail,
                   note="steering drains and tail2 on an outboard Metal5 trunk")
    )

    if probe == "mos":
        em_stub_y = snap(upper_y + steer_box.top + ROW_GAP)
        em_left = snap(placement_upper["ps1"] + steer_rail_w)
        em_right = snap(placement_upper["ps2"] + steer_span + steer_rail_w)

        def _mos_em_source(source: Terminal, tap_x: float) -> None:
            sx, sy = source.center
            w2 = route_width("Metal2")
            rect(layout, cell, "Metal2", min(sx, tap_x) - w2 / 2, sy - w2 / 2,
                  max(sx, tap_x) + w2 / 2, sy + w2 / 2)
            via_between(layout, cell, tap_x, sy, "Metal2", ROUTE_METAL, columns=1, rows=2)
            rect(layout, cell, ROUTE_METAL, min(tap_x, em_left) - sig_w / 2, min(sy, em_stub_y),
                  max(tap_x, em_left) + sig_w / 2, max(sy, em_stub_y))

        _mos_em_source(upper_ports["S_ps1"], em_left)
        _mos_em_source(upper_ports["S_ps2"], em_right)
        rect(layout, cell, ROUTE_METAL, em_left - sig_w / 2, em_stub_y - sig_w / 2,
              em_right + sig_w / 2, em_stub_y + sig_w / 2)

        ed_left = snap(placement_upper["pd1"] - array_gap)
        ed_right = snap(placement_upper["pd2"] + steer_array_w + array_gap)
        for source, bus_x, net in (
            (upper_ports["S_pd1"], ed_left, "ed1"),
            (upper_ports["S_pd2"], ed_right, "ed2"),
        ):
            sx, sy = source.center
            w2 = route_width("Metal2")
            rect(layout, cell, "Metal2", min(sx, bus_x) - w2 / 2, sy - w2 / 2,
                  max(sx, bus_x) + w2 / 2, sy + w2 / 2)
            via_between(layout, cell, bus_x, sy, "Metal2", ROUTE_METAL, columns=1, rows=2)
            rect(layout, cell, ROUTE_METAL, bus_x - sig_w / 2, sy - sig_w / 2,
                  bus_x + sig_w / 2, sy + sig_w / 2)

        steer_port_y = snap(gate_y_upper + MOS_ROW_GAP)
        steern_port_y = snap(gate_y_upper - MOS_ROW_GAP)
        port_right = snap(nmos_right + PORT_REACH)

        def _probe_steer_port(net: str, devices: tuple[str, ...], port_y: float) -> None:
            xs = []
            for name in devices:
                gx, gy = upper_ports[f"G_{name}"].center
                poly_contact(layout, cell, gx, gy)
                via_between(layout, cell, gx, gy, "Metal1", CONTROL_METAL, columns=1, rows=1)
                rect(layout, cell, CONTROL_METAL, gx - ctrl_w / 2, min(gy, port_y),
                      gx + ctrl_w / 2, max(gy, port_y))
                xs.append(gx)
            rect(layout, cell, CONTROL_METAL, min(xs) - ctrl_w / 2, port_y - ctrl_w / 2,
                  port_right + ctrl_w / 2, port_y + ctrl_w / 2)

        _probe_steer_port("steerp", ("ps1", "ps2"), steer_port_y)
        _probe_steer_port("steern", ("pd1", "pd2"), steern_port_y)

        port_left = snap(nmos_left - PORT_REACH)
        mgate_rise_y = snap(lower_y + tail_box.height() / 2)
        rect(layout, cell, "Metal2", channel_x - link_w / 2, gate_y_lower,
              channel_x + link_w / 2, mgate_rise_y)
        via_between(layout, cell, channel_x, mgate_rise_y, "Metal2", PORT_METAL)
        rect(layout, cell, PORT_METAL, min(channel_x, port_left) - sig_w / 2,
              mgate_rise_y - sig_w / 2, max(channel_x, port_left) + sig_w / 2,
              mgate_rise_y + sig_w / 2)
        mgate_port = Terminal(
            name="mgate", layer=f"{PORT_METAL.lower()}_drw",
            center=(port_left, gate_y_lower), width=sig_w, orientation=180.0,
        )
        vss_port = Terminal(
            name="vss", layer="topmetal2_drw",
            center=(axis, vss_rail_y), width=vss_rail_w, orientation=270.0,
        )
        ports = {
            "mgate": mgate_port,
            "vss": vss_port,
            "steerp": Terminal(name="steerp", layer=f"{CONTROL_METAL.lower()}_drw",
                               center=(port_right, steer_port_y), width=ctrl_w, orientation=0.0),
            "steern": Terminal(name="steern", layer=f"{CONTROL_METAL.lower()}_drw",
                               center=(port_right, steern_port_y), width=ctrl_w, orientation=0.0),
        }
        stamp_net_labels(layout, cell, [gate_tap, mgate_port], {"gate_contact": "mgate", "mgate": "mgate"})
        stamp_net_labels(layout, cell, [lower_ports["S_tail1"]], {"S_tail1": "vss"})
        return Block(
            name=CELL, layout=layout, cell=cell, ports=ports, instances=instances,
            port_nets=["mgate", "vss", "steerp", "steern"],
            guard=guard,
            symmetry={"axis_x_um": axis, "pairs": {}},
            notes=["MOS-row bisection probe: seven arrays, tx trunks, no HBT/loads/coils"],
        )

    if probe in ("mos+hbt", "mos+hbt+load"):
        pass  # fall through until the matching early return below

    # --- HBT row: signal inboard, dummy outboard ----------------------------
    row_y = snap(nmos_top + ROW_GAP)
    q1 = hbt.with_name("q1")
    q2 = hbt.with_name("q2")
    qd1 = hbt.with_name("qd1")
    qd2 = hbt.with_name("qd2")

    sig_left, sig_right = mirrored_pair_x(hbt, axis, gap=2 * HBT_DX)
    dum_left, dum_right = mirrored_pair_x(hbt, axis, gap=2 * DUMMY_DX)

    q1_t, q_box = place(layout, cell, q1, sig_left, row_y)
    q2_t, _ = place(layout, cell, q2, sig_right, row_y, "M90")
    qd1_t, _ = place(layout, cell, qd1, dum_left, row_y)
    qd2_t, _ = place(layout, cell, qd2, dum_right, row_y, "M90")
    instances += [
        (q1, {"C": "outp", "B": "inp", "E": "em", "sub": "vss"}),
        (q2, {"C": "outn", "B": "inn", "E": "em", "sub": "vss"}),
        (qd1, {"C": "outp", "B": "vicm", "E": "ed1", "sub": "vss"}),
        (qd2, {"C": "outn", "B": "vicm", "E": "ed2", "sub": "vss"}),
    ]
    hbt_top = snap(q_box.top)

    # --- loads --------------------------------------------------------------
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

    # --- coils --------------------------------------------------------------
    _, coil_probe = build(coil)
    coil_half_h = snap(coil_probe.dbbox().width() / 2.0)
    row_y = snap(max(load_top + ROW_GAP, hbt_top + coil_half_h + PWB_TAP_CLEARANCE))
    l1 = coil.with_name("l1")
    l2 = coil.with_name("l2")
    l1_dx = snap(axis - COIL_PIN_GAP / 2)
    l2_dx = snap(axis + COIL_PIN_GAP / 2)
    coil_bb = l1.kind in bb_kinds
    l1_t, l1place_box = place(layout, cell, l1, l1_dx, row_y, "M135", black_box=coil_bb)
    l2_t, l2place_box = place(layout, cell, l2, l2_dx, row_y, "R270", black_box=coil_bb)
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

    strap_w = snap(l1_t["PLUS"].width)
    vdd_y = l1_t["PLUS"].center[1]
    rect(layout, cell, "TopMetal2",
          l1_t["PLUS"].center[0], vdd_y - strap_w / 2,
          l2_t["PLUS"].center[0], vdd_y + strap_w / 2)
    em_segments.append(em.Segment("vdd.strap", "TopMetal2", width_um=strap_w,
                                  current_a=i_supply, note="between the coil supply feeds"))

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

    # em: outboard vertical trunks joined by one continuous Metal5 strap at the
    # emitter row. Signal HBT emitters reach those trunks horizontally so a local
    # Metal5 column at the device x does not cross the Metal1 base stub (inp/inn).
    em_y = snap(q1_t["E"].center[1])
    em_tap_left = snap(placement_upper["ps1"] + steer_rail_w)
    em_tap_right = snap(placement_upper["ps2"] + steer_span + steer_rail_w)
    em_left_x = snap(min(q1_t["E"].center[0], em_tap_left) - sig_w - 2.0)
    em_right_x = snap(max(q2_t["E"].center[0], em_tap_right) + sig_w + 2.0)
    rect(layout, cell, ROUTE_METAL, em_left_x - sig_w / 2, em_y - sig_w / 2,
          em_right_x + sig_w / 2, em_y + sig_w / 2)
    for trunk_x in (em_left_x, em_right_x):
        rect(layout, cell, ROUTE_METAL, trunk_x - sig_w / 2, em_y - sig_w / 2,
              trunk_x + sig_w / 2, em_y + sig_w / 2)

    def _route_to_trunk(terminal: Terminal, trunk_x: float, top_metal: str) -> None:
        tx, ty = terminal.center
        w2 = route_width("Metal2")
        rect(layout, cell, "Metal2", min(tx, trunk_x) - w2 / 2, ty - w2 / 2,
              max(tx, trunk_x) + w2 / 2, ty + w2 / 2)
        via_between(layout, cell, trunk_x, ty, "Metal2", top_metal, columns=1, rows=2)
        rect(layout, cell, top_metal, trunk_x - sig_w / 2, min(ty, em_y),
              trunk_x + sig_w / 2, max(ty, em_y))

    def _emitter_to_em(emitter: Terminal, trunk_x: float) -> None:
        ex, ey = emitter.center
        vx, _ = via_up(layout, cell, emitter, ROUTE_METAL)
        rect(layout, cell, ROUTE_METAL, min(vx, trunk_x) - sig_w / 2, ey - sig_w / 2,
              max(vx, trunk_x) + sig_w / 2, ey + sig_w / 2)
        rect(layout, cell, ROUTE_METAL, trunk_x - sig_w / 2, min(ey, em_y),
              trunk_x + sig_w / 2, max(ey, em_y))

    _emitter_to_em(q1_t["E"], em_left_x)
    _emitter_to_em(q2_t["E"], em_right_x)
    _route_to_trunk(upper_ports["S_ps1"], em_left_x, ROUTE_METAL)
    _route_to_trunk(upper_ports["S_ps2"], em_right_x, ROUTE_METAL)
    em_segments.append(em.Segment("em", ROUTE_METAL, width_um=sig_w, current_a=i_tail,
                                  note="shared signal emitter strap"))

    out_trunk_left = snap(em_left_x - sig_w - 8.0)
    out_trunk_right = snap(em_right_x + sig_w + 8.0)

    def _dummy_emitter(dummy: Terminal, source: Terminal, bus_x: float) -> None:
        dx, dy = dummy.center
        sx, sy = source.center
        via_up(layout, cell, dummy, ROUTE_METAL)
        via_up(layout, cell, source, ROUTE_METAL)
        rect(layout, cell, ROUTE_METAL, min(dx, bus_x) - sig_w / 2, dy - sig_w / 2,
              max(dx, bus_x) + sig_w / 2, dy + sig_w / 2)
        rect(layout, cell, ROUTE_METAL, min(sx, bus_x) - sig_w / 2, sy - sig_w / 2,
              max(sx, bus_x) + sig_w / 2, sy + sig_w / 2)
        rect(layout, cell, ROUTE_METAL, bus_x - sig_w / 2, min(dy, sy),
              bus_x + sig_w / 2, max(dy, sy))

    ed_bus_left = snap(placement_upper["pd1"] - array_gap)
    ed_bus_right = snap(placement_upper["pd2"] + steer_array_w + array_gap)
    _dummy_emitter(qd1_t["E"], upper_ports["S_pd1"], ed_bus_left)
    em_segments.append(
        em.Segment("ed1", ROUTE_METAL, width_um=sig_w, current_a=i_tail,
                   note="dummy emitter to its steering source")
    )
    _dummy_emitter(qd2_t["E"], upper_ports["S_pd2"], ed_bus_right)
    em_segments.append(
        em.Segment("ed2", ROUTE_METAL, width_um=sig_w, current_a=i_tail,
                   note="dummy emitter to its steering source")
    )

    out_trunk_top = {
        "outp": trunk_net(
            layout, cell, [q1_t["C"], qd1_t["C"], rd1_t[lower.name]],
            trunk_x=axis - out_trunk_left, metal=ROUTE_METAL, width=sig_w,
        )[1],
        "outn": trunk_net(
            layout, cell, [q2_t["C"], qd2_t["C"], rd2_t[lower.name]],
            trunk_x=axis + out_trunk_right, metal=ROUTE_METAL, width=sig_w,
        )[1],
    }
    em_segments += [
        em.Segment(net, ROUTE_METAL, width_um=sig_w, current_a=i_tail, note="collector run")
        for net in ("outp", "outn")
    ]

    in_trunk_x: dict[str, float] = {}
    for base, sign, name in ((q1_t["B"], +1.0, "inp"), (q2_t["B"], -1.0, "inn")):
        bx, by = base.center
        vx = snap(bx + sign * BASE_VIA_DX)
        stub_h = snap(base.width if base.width < 1.0 else route_width("Metal1"))
        rect(layout, cell, "Metal1", min(bx, vx), by - stub_h / 2, max(bx, vx), by + stub_h / 2)
        via_between(layout, cell, vx, by, "Metal1", IN_TRUNK_METAL, columns=1, rows=1)
        in_trunk_x[name] = vx

    # vicm: dummy bases on a Metal3 bus that stays clear of the steering gates.
    vicm_bus_y = snap(gate_y_upper + 2 * MOS_ROW_GAP)
    vicm_pts: list[tuple[float, float]] = []
    vicm_terminals: list[Terminal] = []
    for base in (qd1_t["B"], qd2_t["B"]):
        bx, by = base.center
        poly_contact(layout, cell, bx, by)
        sign = -1.0 if bx < axis else +1.0
        vx = snap(bx + sign * BASE_VIA_DX)
        stub_h = snap(base.width if base.width < 1.0 else route_width("Metal1"))
        rect(layout, cell, "Metal1", min(bx, vx), by - stub_h / 2, max(bx, vx), by + stub_h / 2)
        via_between(layout, cell, vx, by, "Metal1", CONTROL_METAL, columns=1, rows=1)
        rect(layout, cell, CONTROL_METAL, vx - ctrl_w / 2, min(by, vicm_bus_y),
              vx + ctrl_w / 2, max(by, vicm_bus_y))
        vicm_pts.append((vx, by))
        vicm_terminals.append(base)
    rect(layout, cell, CONTROL_METAL,
          min(p[0] for p in vicm_pts) - ctrl_w / 2, vicm_bus_y - ctrl_w / 2,
          max(p[0] for p in vicm_pts) + ctrl_w / 2, vicm_bus_y + ctrl_w / 2)

    devices_box = cell.dbbox()
    if coil_bb:
        devices_box = devices_box + l1_box + l2_box
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

    upper_guard_box = guard_upper["outer_box_um"]
    utap_x, utap_y = min(
        guard_upper["tap_centres_um"],
        key=lambda c: (abs(c[1] - upper_guard_box[1]) > 0.1, -abs(c[0] - nmos_right)),
    )
    via_between(layout, cell, utap_x, utap_y, "Metal1", "Metal2", columns=1, rows=1)
    via_between(layout, cell, utap_x, utap_y, "Metal2", "TopMetal2", columns=2, rows=2)
    rect(layout, cell, "TopMetal2", min(utap_x, axis) - strap_w / 2, min(utap_y, vss_ring_y),
          max(utap_x, axis) + strap_w / 2, max(utap_y, vss_ring_y))

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
    port_top = snap(ring_box[3] + PORT_REACH)
    port_bottom = snap(ring_box[1] - PORT_REACH)
    port_left = snap(ring_box[0] - PORT_REACH)
    port_right = snap(ring_box[2] + PORT_REACH)

    signal_ports: dict[str, Terminal] = {}
    for name, x, y_from, y_to, orientation in (
        ("outp", axis - out_trunk_left, out_trunk_top["outp"], port_top, 90.0),
        ("outn", axis + out_trunk_right, out_trunk_top["outn"], port_top, 90.0),
        ("inp", in_trunk_x["inp"], q1_t["B"].center[1], port_bottom, 270.0),
        ("inn", in_trunk_x["inn"], q2_t["B"].center[1], port_bottom, 270.0),
    ):
        x = snap(x)
        if name.startswith("out"):
            via_between(layout, cell, x, y_from, PORT_METAL, ROUTE_METAL)
            rect(layout, cell, PORT_METAL, x - sig_w / 2, min(y_from, y_to),
                  x + sig_w / 2, max(y_from, y_to))
        else:
            rect(layout, cell, IN_TRUNK_METAL, x - sig_w / 2, min(y_from, y_to),
                  x + sig_w / 2, max(y_from, y_to))
        signal_ports[name] = Terminal(
            name=name, layer=f"{PORT_METAL.lower()}_drw",
            center=(x, y_to), width=sig_w, orientation=orientation,
        )
        em_segments.append(
            em.Segment(name, PORT_METAL, width_um=sig_w, current_a=0.0,
                       note="signal trunk out of the cell edge")
        )

    # Control ports on Metal4 at distinct y levels; inp/inn stay on Metal3 so a
    # horizontal steering bus never crosses an input trunk on the same layer.
    steer_rise_y = snap(upper_y + steer_box.height() / 2)
    steer_port_y = snap(gate_y_upper + MOS_ROW_GAP)
    steern_port_y = snap(gate_y_upper - MOS_ROW_GAP)

    def _steer_gate_port(net: str, devices: tuple[str, ...], port_y: float) -> None:
        xs = []
        for name in devices:
            gx, gy = upper_ports[f"G_{name}"].center
            poly_contact(layout, cell, gx, gy)
            via_between(layout, cell, gx, gy, "Metal1", CONTROL_METAL, columns=1, rows=1)
            rect(layout, cell, CONTROL_METAL, gx - ctrl_w / 2, min(gy, port_y),
                  gx + ctrl_w / 2, max(gy, port_y))
            xs.append(gx)
        rect(layout, cell, CONTROL_METAL, min(xs) - ctrl_w / 2, port_y - ctrl_w / 2,
              port_right + ctrl_w / 2, port_y + ctrl_w / 2)
        em_segments.append(
            em.Segment(net, CONTROL_METAL, width_um=ctrl_w, current_a=0.0,
                       note="steering gate out of the right cell edge")
        )

    _steer_gate_port("steerp", ("ps1", "ps2"), steer_port_y)
    _steer_gate_port("steern", ("pd1", "pd2"), steern_port_y)

    vicm_mid = snap(max(p[0] for p in vicm_pts) + 2.0)
    rect(layout, cell, CONTROL_METAL, vicm_mid - ctrl_w / 2, vicm_bus_y - ctrl_w / 2,
          port_right + ctrl_w / 2, vicm_bus_y + ctrl_w / 2)
    em_segments.append(
        em.Segment("vicm", CONTROL_METAL, width_um=ctrl_w, current_a=0.0,
                   note="dummy base common mode out of the right cell edge")
    )

    mgate_rise_y = snap(gate_y_upper + 3 * MOS_ROW_GAP)
    via_between(layout, cell, channel_x, gate_y_lower, "Metal2", PORT_METAL)
    rect(layout, cell, PORT_METAL, channel_x - sig_w / 2, gate_y_lower,
          channel_x + sig_w / 2, mgate_rise_y)
    rect(layout, cell, PORT_METAL, min(channel_x, port_left) - sig_w / 2,
          mgate_rise_y - sig_w / 2, max(channel_x, port_left) + sig_w / 2,
          mgate_rise_y + sig_w / 2)
    rect(layout, cell, PORT_METAL, port_left - sig_w / 2, min(gate_y_lower, mgate_rise_y),
          port_left + sig_w / 2, max(gate_y_lower, mgate_rise_y))
    mgate_port = Terminal(
        name="mgate", layer=f"{PORT_METAL.lower()}_drw",
        center=(port_left, gate_y_lower), width=sig_w, orientation=180.0,
    )
    em_segments.append(
        em.Segment("mgate", PORT_METAL, width_um=sig_w, current_a=0.0,
                   note="mirror gate bias out of the left cell edge")
    )

    control_ports = {
        "vicm": Terminal(
            name="vicm", layer=f"{CONTROL_METAL.lower()}_drw",
            center=(port_right, vicm_bus_y), width=ctrl_w, orientation=0.0,
        ),
        "steerp": Terminal(
            name="steerp", layer=f"{CONTROL_METAL.lower()}_drw",
            center=(port_right, steer_port_y), width=ctrl_w, orientation=0.0,
        ),
        "steern": Terminal(
            name="steern", layer=f"{CONTROL_METAL.lower()}_drw",
            center=(port_right, steern_port_y), width=ctrl_w, orientation=0.0,
        ),
    }

    ports = {
        **signal_ports,
        **control_ports,
        "vdd": ring.ports["vdd"][0],
        "vss": ring.ports["vss"][0],
        "mgate": mgate_port,
    }

    for terminal, net in (
        (signal_ports["inp"], "inp"), (signal_ports["inn"], "inn"),
        (signal_ports["outp"], "outp"), (signal_ports["outn"], "outn"),
        (control_ports["vicm"], "vicm"), (control_ports["steerp"], "steerp"),
        (control_ports["steern"], "steern"),
        (q1_t["E"], "em"), (q2_t["E"], "em"),
        (upper_ports["S_ps1"], "em"), (upper_ports["S_ps2"], "em"),
        (qd1_t["E"], "ed1"), (qd2_t["E"], "ed2"),
        (rd1_t[upper.name], "nlp1"), (rd2_t[upper.name], "nlp2"),
        (l1_t["PLUS"], "vdd"), (l2_t["PLUS"], "vdd"),
        (gate_tap, "mgate"), (mgate_port, "mgate"), (lower_ports["S_tail1"], "vss"),
        (lower_ports["S_tail2"], "vss"), (lower_ports["S_mdiode"], "vss"),
        (ring.ports["vdd"][0], "vdd"), (ring.ports["vss"][0], "vss"),
    ):
        stamp_net_labels(layout, cell, [terminal], {terminal.name: net})
    for base in vicm_terminals:
        stamp_net_labels(layout, cell, [base], {base.name: "vicm"})

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
                "tails": [placement_lower["tail1"], placement_lower["tail2"]],
                "steer_signal": [placement_upper["ps1"], placement_upper["ps2"]],
                "steer_dummy": [placement_upper["pd1"], placement_upper["pd2"]],
                "hbt_signal": [sig_left, sig_right],
                "hbt_dummy": [dum_left, dum_right],
                "loads": [rd_left, rd_right],
                "coil_feeds": [l1_t["PLUS"].center[0], l2_t["PLUS"].center[0]],
                "out_trunks": [snap(axis - out_trunk_left), snap(axis + out_trunk_right)],
                "in_trunks": [in_trunk_x["inp"], in_trunk_x["inn"]],
            },
        },
        notes=[
            f"cell name is {CELL}, shared with the schematic subcircuit",
            "seven MOS arrays in two rows: mirror and tails below, four steering "
            f"devices above; single-row width would be ~422 um, two rows ~210 um",
            "guard ring encloses each MOS row separately: the lower ring taps the "
            "source rail on the left, the upper ring reaches the power-ring vss "
            "run on TopMetal2 so no Metal2 strap crosses the mgate channel",
            "coils rotated to face each other, so vdd is one straight TopMetal2 "
            "strap with the nlp feeds below it dropping onto the loads",
            f"wire widths from the technology LEF at the operating point: "
            f"{i_tail * 1e3:.2f} mA per tail, {i_supply * 1e3:.2f} mA supply",
            f"the coil pin row sits {coil_half_h:.1f} um above the HBTs so the "
            "pwell-block markers clear their substrate ties",
            "outputs leave at the top edge on Metal4 and inputs at the bottom on "
            "Metal4; vicm, steerp and steern leave on Metal3 at the right edge, "
            "mgate on Metal4 at the left",
            f"ring squared about the axis at half-width {ring_half:.1f} um and "
            f"{RING_CLEARANCE:.0f} um clearance",
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


def _analyze_extracted_mos(path: Path) -> dict:
    """Summarise MOS count and tx/em nets from an extracted netlist."""
    import re

    text = path.read_text()
    devs = re.findall(
        r"^M\$\d+ (\S+) (\S+) (\S+) (\S+) sg13_lv_nmos.*W=([\d.]+)u",
        text,
        re.MULTILINE,
    )
    widths = [float(w) for *_, w in devs]
    drain_nets = sorted({d for d, *_ in devs})
    return {
        "mos_count": len(devs),
        "widths_um": widths,
        "drain_nets": drain_nets,
        "has_em_split": bool(re.search(r"\bem\$\d", text)),
        "has_vicm": " vicm " in text.split(".SUBCKT", 1)[0] or " vicm " in text,
    }


def run_bisect_probe(out_dir: Path, stage: str) -> int:
    """Build a partial cell and summarise extracted MOS connectivity."""
    from layout.common.lvs import run_lvs
    from layout.common.netlist import write_block_cdl

    out_dir.mkdir(parents=True, exist_ok=True)
    block = build_vga_stage(probe=stage)
    gds = block.write(out_dir)
    cdl = write_block_cdl(CELL, block.port_nets, block.instances, out_dir / f"probe_{stage}.cdl")
    lvs = run_lvs(gds=gds, cdl=cdl, run_dir=out_dir / f"lvs_probe_{stage}",
                  topcell=CELL, disable_tap_extraction=True)
    summary = _analyze_extracted_mos(Path(lvs.extracted_netlist))
    print(f"  bisect {stage}: MOS={summary['mos_count']} drains={summary['drain_nets']} "
          f"em_split={summary['has_em_split']} lvs_clean={lvs.clean}")
    (out_dir / f"bisect_{stage}.json").write_text(json.dumps(summary, indent=2) + "\n")
    return 0 if summary["mos_count"] == 7 and "tx1" in summary["drain_nets"] and "tx2" in summary["drain_nets"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--no-render", action="store_true")
    parser.add_argument("--no-pex", action="store_true")
    parser.add_argument("--probe", choices=["mos"], default=None,
                        help="build partial cell for LVS bisection")
    args = parser.parse_args(argv)

    if args.probe:
        return run_bisect_probe(args.out, args.probe)

    block = build_vga_stage()
    code, _entry = run_stage_gates(
        block,
        args.out,
        schematic=SCHEMATIC,
        subckt=CELL,
        params=read_params(PARAMS),
        allowed_rules=CHIP_LEVEL_ALLOWED,
        no_render=args.no_render,
        no_pex=args.no_pex,
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
