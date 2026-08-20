#!/usr/bin/env python3
"""The VGA stage: symmetric placement, a power ring and EM-sized buses.

The cell is ``vga_dut`` — the same name as the subcircuit in
``circuits/ctle56n/spice/vga_pdk.cir``. ``layout/common/parity.py`` checks that
device for device on every run.

Floorplan, centred on one vertical axis with mirror-image halves (CTLE-style single
MOS row):

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
    ║   │md pd1 ps1│tail1│tail2│ps2 pd2│  ║   one MOS row, CTLE order extended
    ║   │  mdiode  │      │      │      │  ║
mgate ─┘                │                    Metal4, left edge
vicm ───────────────────┘                    Metal4, right edge (own y)
steerp/steern ─────────────────────────    Metal3, right edge
    ╚═══════════════════╪══════════════════╝   vss taps the ring here
                  inp   │   inn                Metal4, out of the bottom edge

MOS row order (left to right, ~422 um of active width before the guard ring):

    mdiode | pd1 | ps1 | tail1 | tail2 | ps2 | pd2

``mdiode``, ``tail1`` and ``tail2`` share ``mgate`` and sit outside or on the
symmetric core; each tail sits next to the two steering devices it feeds
(``ps1``/``pd1`` on ``tx1``, ``ps2``/``pd2`` on ``tx2``), which keeps three of
the five two-row routing traps as local verticals inside each array's x span.
``tail2``, ``ps2`` and ``pd2`` mirror by **device span**, not bounding box — the
same correction documented in ``ctle_stage.py`` for the 7.9% ``inp``/``inn`` cap
mismatch. ``ARRAY_GAP`` stays at the CTLE value of 12 um; tightening it to
~5.97 um together with Metal4 via stacks regressed DRC and LVS and the good
intermediate was never saved, so the two-row floorplan was abandoned.

Usage:
    python layout/blocks/vga_stage.py
    python layout/blocks/vga_stage.py --probe mos
"""

from __future__ import annotations

import argparse
import json
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
COIL_PIN_GAP = 44.0
PWB_TAP_CLEARANCE = 2.0
LOAD_DX = 12.0
HBT_DX = 8.0
DUMMY_DX = 18.0
OUT_TRUNK_DX = 17.0
BASE_VIA_DX = 2.2
PORT_METAL = "Metal4"
CONTROL_METAL = "Metal3"
VICM_METAL = "Metal4"
PORT_REACH = 6.0
IN_TRUNK_METAL = "Metal4"
RING_CLEARANCE = 10.0
ARRAY_GAP = 12.0
STEER_PORT_DY = 12.0
ROUTE_METAL = "Metal5"
CHIP_LEVEL_ALLOWED = ("LBE.a", "LBE.c")


def _hbt_emitter_up(layout, cell, terminal: Terminal, metal: str,
                    outboard_sign: float) -> tuple[float, float]:
    """Raise the emitter on an outboard stub, then drop to ``em_y`` on ``metal``."""
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


def _collector_trunk_net(
    layout,
    cell,
    terminals: list[Terminal],
    trunk_x: float,
    em_y: float,
    metal: str,
    width: float,
) -> tuple[float, float]:
    """Vertical collector trunk outboard of ``em``, with joins above the emitter bus."""
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
        ox2 = snap(stub_x + route_width("Metal2"))
        ox3 = snap(stub_x + 2 * route_width("Metal2"))
        via_between(layout, cell, stub_x, safe_y, "Metal2", "Metal3", columns=1, rows=1)
        via_between(layout, cell, ox2, safe_y, "Metal3", "Metal4", columns=1, rows=1)
        via_between(layout, cell, ox3, safe_y, "Metal4", metal, columns=1, rows=1)
        rect(layout, cell, metal, min(ox3, trunk_x), safe_y - w / 2,
              max(ox3, trunk_x), safe_y + w / 2)
    rect(layout, cell, metal, trunk_x - w / 2, safe_y, trunk_x + w / 2, top_y)
    return (safe_y, top_y)


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

    arrays = {
        "mdiode": build_mos_array("mdiode", tail_w, tail_l, current_a=i_tail),
        "tail1": build_mos_array("tail1", tail_w, tail_l, current_a=i_tail),
        "tail2": build_mos_array("tail2", tail_w, tail_l, current_a=i_tail),
        "pd1": build_mos_array("pd1", steer_w, steer_l, current_a=i_tail),
        "ps1": build_mos_array("ps1", steer_w, steer_l, current_a=i_tail),
        "ps2": build_mos_array("ps2", steer_w, steer_l, current_a=i_tail),
        "pd2": build_mos_array("pd2", steer_w, steer_l, current_a=i_tail),
    }

    tail_box = arrays["tail1"].cell.dbbox()
    steer_box = arrays["ps1"].cell.dbbox()
    tail_array_w = tail_box.width()
    steer_array_w = steer_box.width()
    rail_w = arrays["tail1"].rail_width_um
    steer_rail_w = arrays["ps1"].rail_width_um
    tail_device_span = tail_array_w - 2 * rail_w
    steer_device_span = steer_array_w - 2 * steer_rail_w
    array_gap = ARRAY_GAP

    # Symmetric core is tail1 | gap | tail2 on the axis. Left group chains outward;
    # right group mirrors by device span, not bounding box (CTLE correction).
    axis = snap(2 * tail_array_w + 1.5 * array_gap)
    placement: dict[str, float] = {
        "tail1": snap(axis - array_gap / 2.0 - tail_array_w),
    }
    placement["tail2"] = snap(2 * axis - placement["tail1"] - tail_device_span)
    placement["ps1"] = snap(placement["tail1"] - array_gap - steer_array_w)
    placement["pd1"] = snap(placement["ps1"] - array_gap - steer_array_w)
    placement["mdiode"] = snap(placement["pd1"] - array_gap - tail_array_w)
    ps1_active_left = snap(placement["ps1"] + steer_rail_w)
    pd1_active_left = snap(placement["pd1"] + steer_rail_w)
    placement["ps2"] = snap(2 * axis - ps1_active_left - steer_device_span - steer_rail_w)
    placement["pd2"] = snap(2 * axis - pd1_active_left - steer_device_span - steer_rail_w)

    mos_row_w = snap(
        placement["pd2"] + steer_array_w - placement["mdiode"]
    )

    row_y = 0.0
    nmos_ports: dict[str, Terminal] = {}
    for name, array in arrays.items():
        nmos_ports.update(_place_array(layout, cell, array, placement[name], row_y, name))
        em_segments += array.em_segments

    nmos_left = snap(placement["mdiode"] - 1.0)
    nmos_right = snap(placement["pd2"] + steer_array_w + 1.0)
    link_w = route_width("Metal2")

    vss_rail_w = snap(max(em.width_for_a("Metal2", i_supply), route_width("Metal2")))
    source_rail_top = snap(nmos_ports["S_tail1"].center[1] + rail_w / 2)
    vss_rail_bottom = snap(source_rail_top - vss_rail_w)
    vss_rail_y = snap(source_rail_top - vss_rail_w / 2)
    # Only the mirror and tail arrays tie to vss. A bar across the full row would
    # short ps1/ps2 sources onto em and pd1/pd2 onto ed1/ed2.
    mdiode_vss_left = snap(placement["mdiode"] - 1.0)
    mdiode_vss_right = snap(placement["mdiode"] + tail_array_w + 1.0)
    tail_vss_left = snap(placement["tail1"] - 1.0)
    tail_vss_right = snap(placement["tail2"] + tail_array_w + 1.0)
    rect(layout, cell, "Metal2", mdiode_vss_left, vss_rail_bottom,
          mdiode_vss_right, source_rail_top)
    rect(layout, cell, "Metal2", tail_vss_left, vss_rail_bottom,
          tail_vss_right, source_rail_top)
    vss_trunk_x = snap(placement["mdiode"] - array_gap / 2)
    vss_below_y = snap(vss_rail_bottom - vss_rail_w)
    rect(layout, cell, "Metal2", vss_trunk_x - link_w / 2, vss_below_y,
          mdiode_vss_left + link_w / 2, vss_rail_y)
    rect(layout, cell, "Metal2", vss_trunk_x - link_w / 2, vss_below_y,
          vss_trunk_x + link_w / 2, vss_rail_y)
    rect(layout, cell, "Metal2", vss_trunk_x - link_w / 2, vss_below_y,
          tail_vss_right + 1.0, vss_below_y + link_w)
    rect(layout, cell, "Metal2", tail_vss_right - link_w / 2, vss_below_y,
          tail_vss_right + link_w / 2, vss_rail_y)
    em_segments.append(em.Segment("vss.rail", "Metal2", width_um=vss_rail_w,
                                  current_a=i_supply, note="mirror and tail source rails only"))

    gate_y = nmos_ports["G_tail1"].center[1]
    poly = lm["gatpoly_drw"]

    def _mgate_poly_strap(left_x: float, right_x: float) -> None:
        cell.shapes(layout.layer(poly[0], poly[1])).insert(
            pya.DBox(snap(left_x), snap(gate_y - 0.3),
                      snap(right_x), snap(gate_y + 0.3))
        )

    for prefix, array_w, device_rail_w, device_span in (
        ("mdiode", tail_array_w, rail_w, tail_device_span),
        ("tail1", tail_array_w, rail_w, tail_device_span),
        ("tail2", tail_array_w, rail_w, tail_device_span),
    ):
        left = snap(placement[prefix] + device_rail_w)
        right = snap(placement[prefix] + device_rail_w + device_span)
        _mgate_poly_strap(left, right)

    diode_right = snap(placement["mdiode"] + tail_box.right)
    pd1_left = snap(placement["pd1"] + steer_box.left)
    channel_x = snap((diode_right + pd1_left) / 2.0)
    gate_tap = poly_contact(layout, cell, channel_x, gate_y)
    via_between(layout, cell, channel_x, gate_y, "Metal1", "Metal2", columns=1, rows=1)

    diode_rail = nmos_ports["D_mdiode"]
    link_x = snap(diode_right - rail_w / 2)
    rect(layout, cell, "Metal2", link_x - link_w / 2, gate_y,
          link_x + link_w / 2, diode_rail.center[1])
    rect(layout, cell, "Metal2", min(link_x, channel_x), gate_y - link_w / 2,
          max(link_x, channel_x), gate_y + link_w / 2)

    mgate_top_y = snap(tail_box.top + 6.0)
    mgate_bus_xs: list[float] = [channel_x]
    tail1_bus_x = snap(placement["ps1"] + steer_array_w + array_gap / 2)
    tail2_bus_x = snap(placement["tail2"] + tail_device_span + rail_w + array_gap / 2)
    mgate_bus_xs.extend([tail1_bus_x, tail2_bus_x])

    def _mgate_rise(bus_x: float, strap_left: float, strap_right: float) -> None:
        cell.shapes(layout.layer(poly[0], poly[1])).insert(
            pya.DBox(snap(min(strap_left, bus_x)), snap(gate_y - 0.3),
                      snap(max(strap_right, bus_x)), snap(gate_y + 0.3))
        )
        poly_contact(layout, cell, bus_x, gate_y)
        rect(layout, cell, "Metal2", bus_x - link_w / 2, gate_y,
              bus_x + link_w / 2, mgate_top_y)

    _mgate_rise(channel_x, placement["mdiode"] + rail_w,
                placement["mdiode"] + rail_w + tail_device_span)
    _mgate_rise(tail1_bus_x, placement["tail1"] + rail_w,
                placement["tail1"] + rail_w + tail_device_span)
    _mgate_rise(tail2_bus_x, placement["tail2"] + rail_w,
                placement["tail2"] + rail_w + tail_device_span)
    rect(layout, cell, "Metal2", min(mgate_bus_xs) - link_w / 2, mgate_top_y - link_w / 2,
          max(mgate_bus_xs) + link_w / 2, mgate_top_y + link_w / 2)

    instances += [
        (arrays["mdiode"].total_spec.with_name("mdiode"),
         {"D": "mgate", "G": "mgate", "S": "vss", "sub": "vss"}),
        (arrays["tail1"].total_spec.with_name("tail1"),
         {"D": "tx1", "G": "mgate", "S": "vss", "sub": "vss"}),
        (arrays["tail2"].total_spec.with_name("tail2"),
         {"D": "tx2", "G": "mgate", "S": "vss", "sub": "vss"}),
        (arrays["ps1"].total_spec.with_name("ps1"),
         {"D": "tx1", "G": "steerp", "S": "em", "sub": "vss"}),
        (arrays["ps2"].total_spec.with_name("ps2"),
         {"D": "tx2", "G": "steerp", "S": "em", "sub": "vss"}),
        (arrays["pd1"].total_spec.with_name("pd1"),
         {"D": "tx1", "G": "steern", "S": "ed1", "sub": "vss"}),
        (arrays["pd2"].total_spec.with_name("pd2"),
         {"D": "tx2", "G": "steern", "S": "ed2", "sub": "vss"}),
    ]

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

    sig_w = snap(max(em.width_for_a(ROUTE_METAL, i_tail), route_width(ROUTE_METAL)))
    ctrl_w = snap(max(route_width(CONTROL_METAL), sig_w * 0.5))
    vicm_w = snap(max(route_width(VICM_METAL), ctrl_w))

    tx1_trunk_x = snap(placement["mdiode"] - array_gap / 2)
    tx2_trunk_x = snap(placement["pd2"] + steer_array_w + array_gap / 2)
    drain_y = snap(nmos_ports["D_tail1"].center[1])
    route_safe_y = snap(drain_y + sig_w + 2.0)
    m5_rise_y = snap(tail_box.top + 2.0)
    src_hop_metal = "Metal4"

    def _source_rise(source: Terminal, gap_x: float) -> None:
        sx, sy = source.center
        w2 = route_width("Metal2")
        rect(layout, cell, "Metal2", min(sx, gap_x) - w2 / 2, sy - w2 / 2,
              max(sx, gap_x) + w2 / 2, sy + w2 / 2)
        rect(layout, cell, "Metal2", gap_x - w2 / 2, sy, gap_x + w2 / 2, route_safe_y)

    def _metal5_vertical(x: float, y0: float, y1: float) -> None:
        rect(layout, cell, ROUTE_METAL, x - sig_w / 2, min(y0, y1),
              x + sig_w / 2, max(y0, y1))

    def _metal5_join(x0: float, x1: float, y: float) -> None:
        rect(layout, cell, ROUTE_METAL, min(x0, x1) - sig_w / 2, y - sig_w / 2,
              max(x0, x1) + sig_w / 2, y + sig_w / 2)

    def _source_to_bus(source: Terminal, gap_x: float, bus_y: float) -> None:
        hop_w = route_width(src_hop_metal)
        _source_rise(source, gap_x)
        via_between(layout, cell, gap_x, route_safe_y, "Metal2", src_hop_metal, columns=1, rows=1)
        rect(layout, cell, src_hop_metal, gap_x - hop_w / 2, route_safe_y,
              gap_x + hop_w / 2, m5_rise_y)
        via_between(layout, cell, gap_x, m5_rise_y, src_hop_metal, ROUTE_METAL, columns=1, rows=2)
        _metal5_vertical(gap_x, m5_rise_y, bus_y)

    gap_ps1_em = snap(placement["pd1"] + steer_array_w + array_gap / 4)
    gap_ps2_em = snap(placement["ps2"] + steer_array_w + array_gap / 4)
    gap_pd1_ed = snap(placement["mdiode"] + tail_array_w + array_gap * 0.75)
    gap_pd2_ed = snap(placement["pd2"] + steer_rail_w - array_gap / 4)

    trunk_net(
        layout, cell,
        [nmos_ports["D_tail1"], nmos_ports["D_ps1"], nmos_ports["D_pd1"]],
        trunk_x=tx1_trunk_x, metal=ROUTE_METAL, width=sig_w,
    )
    em_segments.append(
        em.Segment("tx1", ROUTE_METAL, width_um=sig_w, current_a=i_tail,
                   note="steering drains and tail1 on an outboard Metal5 trunk")
    )
    trunk_net(
        layout, cell,
        [nmos_ports["D_tail2"], nmos_ports["D_ps2"], nmos_ports["D_pd2"]],
        trunk_x=tx2_trunk_x, metal=ROUTE_METAL, width=sig_w,
    )
    em_segments.append(
        em.Segment("tx2", ROUTE_METAL, width_um=sig_w, current_a=i_tail,
                   note="steering drains and tail2 on an outboard Metal5 trunk")
    )

    if probe == "mos":
        em_stub_y = snap(nmos_top + ROW_GAP)
        _source_to_bus(nmos_ports["S_ps1"], gap_ps1_em, em_stub_y)
        _source_to_bus(nmos_ports["S_ps2"], gap_ps2_em, em_stub_y)
        _metal5_join(gap_ps1_em, gap_ps2_em, em_stub_y)

        ed_left = snap(placement["mdiode"] - array_gap)
        ed_right = snap(placement["pd2"] + steer_array_w + array_gap)
        _source_to_bus(nmos_ports["S_pd1"], gap_pd1_ed, em_stub_y)
        _source_to_bus(nmos_ports["S_pd2"], gap_pd2_ed, em_stub_y)
        _metal5_join(gap_pd1_ed, ed_left, em_stub_y)
        _metal5_join(gap_pd2_ed, ed_right, em_stub_y)

        steer_port_y = snap(gate_y + STEER_PORT_DY)
        steern_port_y = snap(gate_y - STEER_PORT_DY)
        port_right = snap(nmos_right + PORT_REACH)

        def _probe_steer_port(net: str, devices: tuple[str, ...], port_y: float) -> None:
            xs = []
            for name in devices:
                gx, gy = nmos_ports[f"G_{name}"].center
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
        mgate_rise_y = snap(mgate_top_y + ROW_GAP / 2)
        rect(layout, cell, "Metal2", channel_x - link_w / 2, gate_y,
              channel_x + link_w / 2, mgate_rise_y)
        via_between(layout, cell, channel_x, mgate_rise_y, "Metal2", PORT_METAL)
        rect(layout, cell, PORT_METAL, min(channel_x, port_left) - sig_w / 2,
              mgate_rise_y - sig_w / 2, max(channel_x, port_left) + sig_w / 2,
              mgate_rise_y + sig_w / 2)
        mgate_port = Terminal(
            name="mgate", layer=f"{PORT_METAL.lower()}_drw",
            center=(port_left, gate_y), width=sig_w, orientation=180.0,
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
        stamp_net_labels(layout, cell, [nmos_ports["S_tail1"]], {"S_tail1": "vss"})
        return Block(
            name=CELL, layout=layout, cell=cell, ports=ports, instances=instances,
            port_nets=["mgate", "vss", "steerp", "steern"],
            guard=guard,
            symmetry={"axis_x_um": axis, "pairs": {}},
            notes=[
                "MOS-row bisection probe: seven arrays, tx trunks, no HBT/loads/coils",
                f"row order mdiode|pd1|ps1|tail1|tail2|ps2|pd2, {mos_row_w:.1f} um bbox width",
            ],
        )

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

    em_y = snap(q1_t["E"].center[1])
    em_tap_left = snap(placement["ps1"] + steer_rail_w)
    em_tap_right = snap(placement["ps2"] + steer_device_span + steer_rail_w)
    em_left_x = snap(min(q1_t["E"].center[0], em_tap_left) - sig_w - 2.0)
    em_right_x = snap(max(q2_t["E"].center[0], em_tap_right) + sig_w + 2.0)
    rect(layout, cell, ROUTE_METAL, em_left_x - sig_w / 2, em_y - sig_w / 2,
          em_right_x + sig_w / 2, em_y + sig_w / 2)

    def _route_to_trunk(source: Terminal, gap_x: float, trunk_x: float) -> None:
        _source_to_bus(source, gap_x, em_y)
        if abs(gap_x - trunk_x) > 1e-6:
            _metal5_join(gap_x, trunk_x, em_y)

    q1x, _ = _hbt_emitter_up(layout, cell, q1_t["E"], ROUTE_METAL, outboard_sign=-1.0)
    q2x, _ = _hbt_emitter_up(layout, cell, q2_t["E"], ROUTE_METAL, outboard_sign=+1.0)
    rect(layout, cell, ROUTE_METAL, min(q1x, em_left_x) - sig_w / 2, em_y - sig_w / 2,
          max(q1x, em_left_x) + sig_w / 2, em_y + sig_w / 2)
    rect(layout, cell, ROUTE_METAL, min(em_right_x, q2x) - sig_w / 2, em_y - sig_w / 2,
          max(em_right_x, q2x) + sig_w / 2, em_y + sig_w / 2)
    rect(layout, cell, ROUTE_METAL, q1x - sig_w / 2, min(q1_t["E"].center[1], em_y),
          q1x + sig_w / 2, max(q1_t["E"].center[1], em_y))
    rect(layout, cell, ROUTE_METAL, q2x - sig_w / 2, min(q2_t["E"].center[1], em_y),
          q2x + sig_w / 2, max(q2_t["E"].center[1], em_y))
    _route_to_trunk(nmos_ports["S_ps1"], gap_ps1_em, em_left_x)
    _route_to_trunk(nmos_ports["S_ps2"], gap_ps2_em, em_right_x)
    em_segments.append(em.Segment("em", ROUTE_METAL, width_um=sig_w, current_a=i_tail,
                                  note="shared signal emitter strap"))

    ed_bus_left = snap(placement["mdiode"] - array_gap)
    ed_bus_right = snap(placement["pd2"] + steer_array_w + array_gap)

    def _dummy_emitter(dummy: Terminal, source: Terminal, gap_x: float, bus_x: float) -> None:
        dy = dummy.center[1]
        _source_rise(source, gap_x)
        via_between(layout, cell, gap_x, route_safe_y, "Metal2", src_hop_metal, columns=1, rows=1)
        rect(layout, cell, src_hop_metal, gap_x - route_width(src_hop_metal) / 2, route_safe_y,
              gap_x + route_width(src_hop_metal) / 2, m5_rise_y)
        via_between(layout, cell, gap_x, m5_rise_y, src_hop_metal, ROUTE_METAL, columns=1, rows=2)
        _metal5_vertical(gap_x, m5_rise_y, dy)
        if abs(gap_x - bus_x) > 1e-6:
            _metal5_join(gap_x, bus_x, dy)
        dx, _ = dummy.center
        via_up(layout, cell, dummy, ROUTE_METAL)
        rect(layout, cell, ROUTE_METAL, min(dx, bus_x) - sig_w / 2, dy - sig_w / 2,
              max(dx, bus_x) + sig_w / 2, dy + sig_w / 2)

    _dummy_emitter(qd1_t["E"], nmos_ports["S_pd1"], gap_pd1_ed, ed_bus_left)
    em_segments.append(
        em.Segment("ed1", ROUTE_METAL, width_um=sig_w, current_a=i_tail,
                   note="dummy emitter to its steering source")
    )
    _dummy_emitter(qd2_t["E"], nmos_ports["S_pd2"], gap_pd2_ed, ed_bus_right)
    em_segments.append(
        em.Segment("ed2", ROUTE_METAL, width_um=sig_w, current_a=i_tail,
                   note="dummy emitter to its steering source")
    )

    out_trunk_top: dict[str, float] = {}
    for net, trunk_x, targets in (
        ("outp", axis - OUT_TRUNK_DX, [q1_t["C"], qd1_t["C"], rd1_t[lower.name]]),
        ("outn", axis + OUT_TRUNK_DX, [q2_t["C"], qd2_t["C"], rd2_t[lower.name]]),
    ):
        _, top = _collector_trunk_net(
            layout, cell, targets, trunk_x=trunk_x, em_y=em_y,
            metal=ROUTE_METAL, width=sig_w,
        )
        out_trunk_top[net] = top
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

    vicm_bus_y = snap(hbt_top + ROW_GAP + STEER_PORT_DY)
    vicm_pts: list[tuple[float, float]] = []
    vicm_terminals: list[Terminal] = []
    for base in (qd1_t["B"], qd2_t["B"]):
        bx, by = base.center
        poly_contact(layout, cell, bx, by)
        sign = -1.0 if bx < axis else +1.0
        vx = snap(bx + sign * BASE_VIA_DX)
        stub_h = snap(base.width if base.width < 1.0 else route_width("Metal1"))
        rect(layout, cell, "Metal1", min(bx, vx), by - stub_h / 2, max(bx, vx), by + stub_h / 2)
        via_between(layout, cell, vx, by, "Metal1", VICM_METAL, columns=1, rows=1)
        rect(layout, cell, VICM_METAL, vx - vicm_w / 2, min(by, vicm_bus_y),
              vx + vicm_w / 2, max(by, vicm_bus_y))
        vicm_pts.append((vx, by))
        vicm_terminals.append(base)
    rect(layout, cell, VICM_METAL,
          min(p[0] for p in vicm_pts) - vicm_w / 2, vicm_bus_y - vicm_w / 2,
          max(p[0] for p in vicm_pts) + vicm_w / 2, vicm_bus_y + vicm_w / 2)

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
        ("outp", axis - OUT_TRUNK_DX, out_trunk_top["outp"], port_top, 90.0),
        ("outn", axis + OUT_TRUNK_DX, out_trunk_top["outn"], port_top, 90.0),
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

    steer_port_y = snap(gate_y + STEER_PORT_DY)
    steern_port_y = snap(gate_y - STEER_PORT_DY)

    def _steer_gate_port(net: str, devices: tuple[str, ...], port_y: float) -> None:
        xs = []
        for name in devices:
            gx, gy = nmos_ports[f"G_{name}"].center
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
    rect(layout, cell, VICM_METAL, vicm_mid - vicm_w / 2, vicm_bus_y - vicm_w / 2,
          port_right + vicm_w / 2, vicm_bus_y + vicm_w / 2)
    em_segments.append(
        em.Segment("vicm", VICM_METAL, width_um=vicm_w, current_a=0.0,
                   note="dummy base common mode out of the right cell edge on Metal4")
    )

    mgate_rise_y = snap(mgate_top_y + ROW_GAP / 2)
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

    control_ports = {
        "vicm": Terminal(
            name="vicm", layer=f"{VICM_METAL.lower()}_drw",
            center=(port_right, vicm_bus_y), width=vicm_w, orientation=0.0,
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
        (nmos_ports["S_ps1"], "em"), (nmos_ports["S_ps2"], "em"),
        (qd1_t["E"], "ed1"), (qd2_t["E"], "ed2"),
        (rd1_t[upper.name], "nlp1"), (rd2_t[upper.name], "nlp2"),
        (l1_t["PLUS"], "vdd"), (l2_t["PLUS"], "vdd"),
        (gate_tap, "mgate"), (mgate_port, "mgate"), (nmos_ports["S_tail1"], "vss"),
        (nmos_ports["S_tail2"], "vss"), (nmos_ports["S_mdiode"], "vss"),
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
                "tails": [placement["tail1"], placement["tail2"]],
                "steer_signal": [placement["ps1"], placement["ps2"]],
                "steer_dummy": [placement["pd1"], placement["pd2"]],
                "hbt_signal": [sig_left, sig_right],
                "hbt_dummy": [dum_left, dum_right],
                "loads": [rd_left, rd_right],
                "coil_feeds": [l1_t["PLUS"].center[0], l2_t["PLUS"].center[0]],
                "out_trunks": [snap(axis - OUT_TRUNK_DX), snap(axis + OUT_TRUNK_DX)],
                "in_trunks": [in_trunk_x["inp"], in_trunk_x["inn"]],
            },
        },
        notes=[
            f"cell name is {CELL}, shared with the schematic subcircuit",
            "seven MOS arrays in one CTLE-style row, left to right: "
            f"mdiode|pd1|ps1|tail1|tail2|ps2|pd2 ({mos_row_w:.1f} um bbox width, "
            f"~{3 * tail_device_span + 4 * steer_device_span:.0f} um active span)",
            "two-row floorplan abandoned: horizontals at one row's drain y crossed "
            "another array's x; tightening ARRAY_GAP to ~5.97 um with Metal4 via "
            "stacks regressed DRC (Act.b/M2.b) and LVS (merged tails) with no "
            "saved intermediate",
            "tail2, ps2 and pd2 mirror by device span not bounding box; vicm on "
            f"Metal4 at y={vicm_bus_y:.1f} um, separate from steerp/steern on Metal3",
            "coils rotated to face each other, so vdd is one straight TopMetal2 "
            "strap with the nlp feeds below it dropping onto the loads",
            f"wire widths from the technology LEF at the operating point: "
            f"{i_tail * 1e3:.2f} mA per tail, {i_supply * 1e3:.2f} mA supply",
            f"the coil pin row sits {coil_half_h:.1f} um above the HBTs so the "
            "pwell-block markers clear their substrate ties",
            "outputs leave at the top edge on Metal4 and inputs at the bottom on "
            "Metal4; vicm on Metal4, steerp and steern on Metal3 at the right edge, "
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
