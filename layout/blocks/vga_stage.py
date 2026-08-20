#!/usr/bin/env python3
"""The VGA stage: symmetric placement, a power ring and EM-sized buses.

The cell is ``vga_dut`` — the same name as the subcircuit in
``circuits/ctle56n/spice/vga_pdk.cir``. ``layout/common/parity.py`` checks that
device for device on every run.

Floorplan, centred on one vertical axis with mirror-image halves:

                          out        vdd                 top edge
        ╔══════════════════╪══════════╪═══════════════════════╗  power ring
        ║  ┌────────────┐  │   vdd    │  ┌────────────┐        ║
        ║  │ INDUCTOR P │   ┌──┐  ┌──┐   │ INDUCTOR N │        ║
        ║  │   M135     │   │rd1│ │rd2│  │   R270     │        ║
        ║  └────────────┘   └──┘  └──┘   └────────────┘        ║
        ║        Qd1      Q1          Q2      Qd2               ║  HBT row
        ║ ctrl ── vicm / steerp / steern channel ────────────── ║
        ║  ┌──────────────────────────────────────────────┐    ║
        ║  │ mdiode │ pd1 │ tail1 │ ps1 ┊ ps2 │ tail2 │pd2│    ║  MOS row
        ║  └────────────────────── vss ─────────────────┘    ║  guard ring
        ╚══════════════════╪════════════════════════════════════╝
                           in                                      bottom edge

Routing discipline (``MEMORY.md``): every array's terminal leaves on **its own
vertical column at that array's x**, and changes layer before anything runs
horizontally. Gate nets run horizontally at gate height only. ``draw.trunk_net``
is unsafe here — its per-terminal stubs share y with other columns.

Metal assignment:

| Net class | Metal | Notes |
| --- | --- | --- |
| ``mgate`` gate bus | Metal2 | mdiode, tail1, tail2 poly straps |
| ``steern`` gate bus | Metal2 | pd1, pd2 — outboard ends of the row |
| ``steerp`` gate bus | Metal3 | ps1–ps2 short strap across the axis |
| ``tx*``, ``em``, ``ed*`` | Metal5 | inter-row currents; risers from drain/source rails |
| ``inp``, ``inn``, ``out*``, ``vicm``, ``mgate`` port | Metal4 | passes under the ring; control bundle on the left |
| ``nlp*``, ``vdd`` strap | TopMetal2 | vertical load columns; colinear coil feeds |

Usage:
    python layout/blocks/vga_stage.py
    python layout/blocks/vga_stage.py --probe mos
    python layout/blocks/vga_stage.py --probe hbt
"""

from __future__ import annotations

import argparse
import json
import re
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
from layout.common.rules import min_space, route_width
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
SIGNAL_DX = 10.0
HBT_DX = 8.0
DUMMY_DX = 18.0
BASE_VIA_DX = 2.2
PORT_METAL = "Metal4"
STEERP_METAL = "Metal3"
STEERN_METAL = "Metal2"
MGATE_BUS_METAL = "Metal3"
PORT_REACH = 6.0
IN_TRUNK_METAL = PORT_METAL
RING_CLEARANCE = 10.0
ROUTE_METAL = "Metal5"
CHIP_LEVEL_ALLOWED = ("LBE.a", "LBE.c")

# Minimum gap between adjacent MOS arrays: rule floor plus twice the wider rail
# overhang, with margin — tightening to the rule minimum alone traded spacing
# violations for thousands of contact ones (``MEMORY.md``).
ARRAY_GAP_MARGIN = 4.0


def _derive_array_gap(rail_w: float, steer_rail_w: float) -> float:
    rail = max(rail_w, steer_rail_w)
    return snap(max(12.0, 2 * rail + min_space("Metal2") + ARRAY_GAP_MARGIN))


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


def _poly_strap(layout, cell, left_x: float, right_x: float, gate_y: float) -> None:
    poly = layer_map()["gatpoly_drw"]
    pya = pya_module()
    cell.shapes(layout.layer(poly[0], poly[1])).insert(
        pya.DBox(snap(min(left_x, right_x)), snap(gate_y - 0.3),
                 snap(max(left_x, right_x)), snap(gate_y + 0.3))
    )


def _rise_to_bus(
    layout,
    cell,
    terminal: Terminal,
    col_x: float,
    bus_y: float,
    bus_metal: str,
    width: float,
) -> None:
    """Own vertical column at ``col_x``; layer change at ``bus_y`` before any horizontal."""
    from layout.common.route import metal_of

    tx, ty = snap(terminal.center[0]), snap(terminal.center[1])
    col_x = snap(col_x)
    bus_y = snap(bus_y)
    bottom = metal_of(terminal.layer)
    bw = route_width(bottom) if bottom else width
    if bottom and abs(tx - col_x) > 1e-6:
        rect(layout, cell, bottom, min(tx, col_x) - bw / 2, ty - bw / 2,
              max(tx, col_x) + bw / 2, ty + bw / 2)
    if bottom:
        rect(layout, cell, bottom, col_x - bw / 2, min(ty, bus_y),
              col_x + bw / 2, max(ty, bus_y))
    if bottom and bottom != bus_metal:
        via_between(layout, cell, col_x, bus_y, bottom, bus_metal, columns=1, rows=2)
    elif not bottom:
        rect(layout, cell, bus_metal, col_x - width / 2, min(ty, bus_y),
              col_x + width / 2, max(ty, bus_y))


def _bus_at_y(layout, cell, xs: list[float], y: float, metal: str, width: float) -> None:
    y = snap(y)
    rect(layout, cell, metal, min(xs) - width / 2, y - width / 2,
          max(xs) + width / 2, y + width / 2)


def _gate_port_path(
    layout,
    cell,
    gate_x: float,
    gate_y: float,
    bus_y: float,
    port_x: float,
    gate_metal: str,
    trunk_metal: str,
    width: float,
) -> None:
    """One gate column: vertical on ``gate_metal``, trunk horizontal on ``trunk_metal`` at ``bus_y``.

    Horizontals stop at this gate's x so they never cross another column's vertical on the
    same layer (``MEMORY.md``).
    """
    gate_x = snap(gate_x)
    bus_y = snap(bus_y)
    poly_contact(layout, cell, gate_x, gate_y)
    via_between(layout, cell, gate_x, gate_y, "Metal1", gate_metal, columns=1, rows=1)
    rect(layout, cell, gate_metal, gate_x - width / 2, min(gate_y, bus_y),
          gate_x + width / 2, max(gate_y, bus_y))
    if gate_metal != trunk_metal:
        via_between(layout, cell, gate_x, bus_y, gate_metal, trunk_metal, columns=1, rows=1)
    tw = route_width(trunk_metal) if trunk_metal != gate_metal else width
    rect(layout, cell, trunk_metal, min(gate_x, port_x) - tw / 2, bus_y - tw / 2,
          max(gate_x, port_x) + tw / 2, bus_y + tw / 2)


def _mgate_port_path(
    layout,
    cell,
    channel_x: float,
    gate_y: float,
    bus_y: float,
    port_x: float,
    bus_metal: str,
    port_metal: str,
    width: float,
) -> None:
    """mgate/Iref out the left edge — rise on the gate bus metal, cross on ``port_metal``."""
    channel_x = snap(channel_x)
    bus_y = snap(bus_y)
    rect(layout, cell, bus_metal, channel_x - width / 2, min(gate_y, bus_y),
          channel_x + width / 2, max(gate_y, bus_y))
    via_between(layout, cell, channel_x, bus_y, bus_metal, port_metal, columns=1, rows=1)
    rect(layout, cell, port_metal, min(channel_x, port_x) - width / 2, bus_y - width / 2,
          max(channel_x, port_x) + width / 2, bus_y + width / 2)
    rect(layout, cell, port_metal, port_x - width / 2, min(gate_y, bus_y),
          port_x + width / 2, max(gate_y, bus_y))


def _tm2_column(
    layout,
    cell,
    col_x: float,
    y_bottom: float,
    y_top: float,
    width: float,
    terminals: list[Terminal],
) -> None:
    """One TopMetal2 column; each terminal joins with a vertical leg at its own y first."""
    col_x = snap(col_x)
    y0, y1 = snap(min(y_bottom, y_top)), snap(max(y_bottom, y_top))
    for terminal in terminals:
        tx, ty = terminal.center
        land_x, land_y = via_up(layout, cell, terminal, "TopMetal2")
        if abs(land_x - col_x) > 1e-6:
            rect(layout, cell, "TopMetal2", min(land_x, col_x), land_y - width / 2,
                  max(land_x, col_x), land_y + width / 2)
        rect(layout, cell, "TopMetal2", col_x - width / 2, min(land_y, y1),
              col_x + width / 2, max(land_y, y1))
    rect(layout, cell, "TopMetal2", col_x - width / 2, y0, col_x + width / 2, y1)


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
    array_gap = _derive_array_gap(rail_w, steer_rail_w)

    # Symmetric core is ps1 | gap | ps2 on the axis. Order left to right:
    # mdiode | pd1 | tail1 | ps1 ┊ ps2 | tail2 | pd2
    axis = snap(2 * steer_array_w + 1.5 * array_gap)
    placement: dict[str, float] = {
        "ps1": snap(axis - array_gap / 2.0 - steer_array_w),
    }
    ps1_active_left = snap(placement["ps1"] + steer_rail_w)
    placement["ps2"] = snap(2 * axis - ps1_active_left - steer_device_span - steer_rail_w)
    placement["tail1"] = snap(placement["ps1"] - array_gap - tail_array_w)
    placement["tail2"] = snap(2 * axis - placement["tail1"] - tail_device_span)
    placement["pd1"] = snap(placement["tail1"] - array_gap - steer_array_w)
    pd1_active_left = snap(placement["pd1"] + steer_rail_w)
    placement["pd2"] = snap(2 * axis - pd1_active_left - steer_device_span - steer_rail_w)
    placement["mdiode"] = snap(placement["pd1"] - array_gap - tail_array_w)

    mos_row_w = snap(placement["pd2"] + steer_array_w - placement["mdiode"])

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

    # mgate poly on mdiode, tail1, tail2 only — steern and steerp use separate metals.
    for prefix, pl_x, span, dev_rail in (
        ("mdiode", placement["mdiode"], tail_device_span, rail_w),
        ("tail1", placement["tail1"], tail_device_span, rail_w),
        ("tail2", placement["tail2"], tail_device_span, rail_w),
    ):
        left = snap(pl_x + dev_rail)
        right = snap(pl_x + dev_rail + span)
        _poly_strap(layout, cell, left, right, gate_y)

    diode_right = snap(placement["mdiode"] + tail_box.right)
    tail1_left = snap(placement["tail1"] + tail_box.left)
    channel_x = snap((diode_right + tail1_left) / 2.0)
    mgate_cols = [channel_x]
    for prefix, pl_x, span, dev_rail in (
        ("tail1", placement["tail1"], tail_device_span, rail_w),
        ("tail2", placement["tail2"], tail_device_span, rail_w),
    ):
        left = snap(pl_x + dev_rail)
        right = snap(pl_x + dev_rail + span)
        _poly_strap(layout, cell, left, right, gate_y)
        mgate_cols.append(snap((left + right) / 2.0))

    gate_tap = poly_contact(layout, cell, channel_x, gate_y)
    via_between(layout, cell, channel_x, gate_y, "Metal1", "Metal2", columns=1, rows=1)

    diode_rail = nmos_ports["D_mdiode"]
    link_x = snap(diode_right - rail_w / 2)
    rect(layout, cell, "Metal2", link_x - link_w / 2, gate_y,
          link_x + link_w / 2, diode_rail.center[1])
    rect(layout, cell, "Metal2", min(link_x, channel_x), gate_y - link_w / 2,
          max(link_x, channel_x), gate_y + link_w / 2)

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

    # mgate bus on Metal3 above the source rail — staggered below the ctrl bundle
    # so trunk horizontals never share y with another net's vertical on one metal.
    mgate_link_y = snap(nmos_top + 2.0)
    for bus_x in mgate_cols:
        if abs(bus_x - channel_x) > 1e-6:
            poly_contact(layout, cell, bus_x, gate_y)
            via_between(layout, cell, bus_x, gate_y, "Metal1", MGATE_BUS_METAL, columns=1, rows=1)
        rect(layout, cell, MGATE_BUS_METAL, bus_x - link_w / 2, gate_y,
              bus_x + link_w / 2, mgate_link_y)
    via_between(layout, cell, channel_x, gate_y, "Metal2", MGATE_BUS_METAL, columns=1, rows=1)
    rect(layout, cell, MGATE_BUS_METAL, channel_x - link_w / 2, gate_y,
          channel_x + link_w / 2, mgate_link_y)
    _bus_at_y(layout, cell, mgate_cols, mgate_link_y, MGATE_BUS_METAL, link_w)

    ctrl_steern_pd2_y = snap(nmos_top + 5.0)
    ctrl_steern_pd1_y = snap(nmos_top + 8.0)
    ctrl_steerp_y = snap(nmos_top + 11.0)
    ctrl_vicm_y = snap(nmos_top + 14.0)

    sig_w = snap(max(em.width_for_a(ROUTE_METAL, i_tail), route_width(ROUTE_METAL)))
    ctrl_w = snap(max(route_width(STEERP_METAL), sig_w * 0.5))
    gate_w = snap(max(route_width(STEERN_METAL), ctrl_w))

    drain_y = snap(nmos_ports["D_tail1"].center[1])
    tx_bus_y = snap(drain_y + sig_w + 2.0)
    source_y = snap(nmos_ports["S_ps1"].center[1])

    # --- tx1 / tx2: adjacent triples, horizontal only at tx_bus_y on Metal5 ----
    tx1_cols = [
        nmos_ports["D_pd1"].center[0],
        nmos_ports["D_tail1"].center[0],
        nmos_ports["D_ps1"].center[0],
    ]
    tx2_cols = [
        nmos_ports["D_ps2"].center[0],
        nmos_ports["D_tail2"].center[0],
        nmos_ports["D_pd2"].center[0],
    ]
    for terminal, col_x in (
        (nmos_ports["D_pd1"], tx1_cols[0]),
        (nmos_ports["D_tail1"], tx1_cols[1]),
        (nmos_ports["D_ps1"], tx1_cols[2]),
    ):
        _rise_to_bus(layout, cell, terminal, col_x, tx_bus_y, ROUTE_METAL, sig_w)
    _bus_at_y(layout, cell, tx1_cols, tx_bus_y, ROUTE_METAL, sig_w)
    em_segments.append(em.Segment("tx1", ROUTE_METAL, width_um=sig_w, current_a=i_tail,
                                  note="tail1 drain and its two consumers"))

    for terminal, col_x in (
        (nmos_ports["D_ps2"], tx2_cols[0]),
        (nmos_ports["D_tail2"], tx2_cols[1]),
        (nmos_ports["D_pd2"], tx2_cols[2]),
    ):
        _rise_to_bus(layout, cell, terminal, col_x, tx_bus_y, ROUTE_METAL, sig_w)
    _bus_at_y(layout, cell, tx2_cols, tx_bus_y, ROUTE_METAL, sig_w)
    em_segments.append(em.Segment("tx2", ROUTE_METAL, width_um=sig_w, current_a=i_tail,
                                  note="tail2 drain and its two consumers"))

    # Gate straps at gate_y only — separate metals per net class.
    ps1_gx, ps2_gx = nmos_ports["G_ps1"].center[0], nmos_ports["G_ps2"].center[0]
    pd1_gx, pd2_gx = nmos_ports["G_pd1"].center[0], nmos_ports["G_pd2"].center[0]
    poly_contact(layout, cell, ps1_gx, gate_y)
    poly_contact(layout, cell, ps2_gx, gate_y)
    via_between(layout, cell, ps1_gx, gate_y, "Metal1", STEERP_METAL, columns=1, rows=1)
    via_between(layout, cell, ps2_gx, gate_y, "Metal1", STEERP_METAL, columns=1, rows=1)
    _bus_at_y(layout, cell, [ps1_gx, ps2_gx], gate_y, STEERP_METAL, ctrl_w)

    poly_contact(layout, cell, pd1_gx, gate_y)
    poly_contact(layout, cell, pd2_gx, gate_y)
    via_between(layout, cell, pd1_gx, gate_y, "Metal1", STEERN_METAL, columns=1, rows=1)
    via_between(layout, cell, pd2_gx, gate_y, "Metal1", STEERN_METAL, columns=1, rows=1)
    # steern drops from the control channel at each gate — no Metal2 bar
    # between pd1 and pd2 at gate_y; it would cross the vss trunk and tails.

    port_left = snap(nmos_left - PORT_REACH)

    if probe == "tx":
        block = Block(
            name=CELL, layout=layout, cell=cell,
            ports={"vss": Terminal("vss", "topmetal2_drw", (axis, vss_rail_y), vss_rail_w, 270.0)},
            instances=instances, port_nets=["vss"], guard=guard,
            symmetry={"axis_x_um": axis, "pairs": {}},
            notes=["tx-only bisection: tx1/tx2 Metal5 buses and gate straps, no ctrl ports"],
        )
        block.em_segments = em_segments
        return block

    if probe == "arrays":
        block = Block(
            name=CELL, layout=layout, cell=cell,
            ports={"vss": Terminal("vss", "topmetal2_drw", (axis, vss_rail_y), vss_rail_w, 270.0)},
            instances=instances, port_nets=["vss"], guard=guard,
            symmetry={"axis_x_um": axis, "pairs": {}},
            notes=["arrays-only bisection: placement and vss, no signal routing"],
        )
        block.em_segments = em_segments
        return block

    if probe == "mos":
        # em/ed rises land in the HBT row in the full cell; omit here so the
        # probe stops after tx buses and gate straps.
        # _mgate_port_path(layout, cell, channel_x, gate_y, mgate_link_y, port_left,
        #                  MGATE_BUS_METAL, PORT_METAL, sig_w)
        # _gate_port_path(layout, cell, ps1_gx, gate_y, ctrl_steerp_y, port_left,
        #                 STEERP_METAL, STEERP_METAL, ctrl_w)
        # _gate_port_path(layout, cell, pd1_gx, gate_y, ctrl_steern_pd1_y, port_left,
        #                 STEERN_METAL, PORT_METAL, gate_w)
        # _gate_port_path(layout, cell, pd2_gx, gate_y, ctrl_steern_pd2_y, port_left,
        #                 STEERN_METAL, PORT_METAL, gate_w)
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
            "steerp": Terminal(name="steerp", layer=f"{STEERP_METAL.lower()}_drw",
                               center=(port_left, ctrl_steerp_y), width=ctrl_w, orientation=180.0),
            "steern": Terminal(name="steern", layer=f"{PORT_METAL.lower()}_drw",
                               center=(port_left, ctrl_steern_pd1_y), width=gate_w, orientation=180.0),
        }
        # stamp_net_labels(layout, cell, [gate_tap, mgate_port], {"gate_contact": "mgate", "mgate": "mgate"})
        stamp_net_labels(layout, cell, [nmos_ports["S_tail1"]], {"S_tail1": "vss"})
        return Block(
            name=CELL, layout=layout, cell=cell, ports=ports, instances=instances,
            port_nets=["mgate", "vss", "steerp", "steern"],
            guard=guard,
            symmetry={"axis_x_um": axis, "pairs": {}},
            notes=[
                "MOS-row bisection: seven arrays, tx1/tx2 column buses, no HBT/loads/coils",
                f"row mdiode|pd1|tail1|ps1|ps2|tail2|pd2, {mos_row_w:.1f} um bbox width",
            ],
        )

    # --- HBT row: Qd1 | Q1 ┊ Q2 | Qd2 ---------------------------------------
    row_y = snap(nmos_top + ROW_GAP)
    q1 = hbt.with_name("q1")
    q2 = hbt.with_name("q2")
    qd1 = hbt.with_name("qd1")
    qd2 = hbt.with_name("qd2")

    out_col_l = snap(axis - SIGNAL_DX)
    out_col_r = snap(axis + SIGNAL_DX)

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
    em_y = snap(q1_t["E"].center[1])

    # em: ps1/ps2 sources and HBT emitters — horizontal only at em_y.
    em_cols = [
        nmos_ports["S_ps1"].center[0],
        q1_t["E"].center[0],
        q2_t["E"].center[0],
        nmos_ports["S_ps2"].center[0],
    ]
    for terminal, col_x in (
        (nmos_ports["S_ps1"], em_cols[0]),
        (q1_t["E"], em_cols[1]),
        (q2_t["E"], em_cols[2]),
        (nmos_ports["S_ps2"], em_cols[3]),
    ):
        _rise_to_bus(layout, cell, terminal, col_x, em_y, ROUTE_METAL, sig_w)
    _bus_at_y(layout, cell, em_cols, em_y, ROUTE_METAL, sig_w)
    em_segments.append(em.Segment("em", ROUTE_METAL, width_um=sig_w, current_a=i_tail,
                                  note="shared signal emitter strap at HBT row"))

    ed1_col = nmos_ports["S_pd1"].center[0]
    ed2_col = nmos_ports["S_pd2"].center[0]
    ed_y = snap(qd1_t["E"].center[1])
    _rise_to_bus(layout, cell, nmos_ports["S_pd1"], ed1_col, ed_y, ROUTE_METAL, sig_w)
    _rise_to_bus(layout, cell, qd1_t["E"], ed1_col, ed_y, ROUTE_METAL, sig_w)
    _rise_to_bus(layout, cell, nmos_ports["S_pd2"], ed2_col, ed_y, ROUTE_METAL, sig_w)
    _rise_to_bus(layout, cell, qd2_t["E"], ed2_col, ed_y, ROUTE_METAL, sig_w)
    em_segments += [
        em.Segment("ed1", ROUTE_METAL, width_um=sig_w, current_a=i_tail,
                   note="dummy steering column at pd1 x"),
        em.Segment("ed2", ROUTE_METAL, width_um=sig_w, current_a=i_tail,
                   note="dummy steering column at pd2 x"),
    ]

    if probe == "hbt":
        _gate_port_path(layout, cell, ps1_gx, gate_y, ctrl_steerp_y, port_left,
                        STEERP_METAL, STEERP_METAL, ctrl_w)
        _gate_port_path(layout, cell, pd1_gx, gate_y, ctrl_steern_pd1_y, port_left,
                        STEERN_METAL, PORT_METAL, gate_w)
        block = Block(
            name=CELL, layout=layout, cell=cell,
            ports={"vss": Terminal("vss", "topmetal2_drw", (axis, vss_rail_y), vss_rail_w, 270.0)},
            instances=instances, port_nets=["vss"], guard=guard,
            symmetry={"axis_x_um": axis, "pairs": {}},
            notes=["HBT bisection: em, ed1, ed2 as three distinct nets"],
        )
        block.em_segments = em_segments
        return block

    # --- loads: vertical columns outp/outn ------------------------------------
    row_y = snap(hbt_top + ROW_GAP)
    rd1 = load.with_name("rd1")
    rd2 = load.with_name("rd2")
    load_terms = {t.name: t for t in derive_terminals(load, *build(load))}
    upper = max(load_terms.values(), key=lambda t: t.center[1])
    lower = min(load_terms.values(), key=lambda t: t.center[1])

    rd1_dx = snap(out_col_l - lower.center[0])
    rd2_dx = snap(out_col_r - lower.center[0])
    rd1_t, rd_box = place(layout, cell, rd1, rd1_dx, row_y)
    rd2_t, _ = place(layout, cell, rd2, rd2_dx, row_y, "M90")
    nlp_col_l = snap(rd1_t[upper.name].center[0])
    nlp_col_r = snap(rd2_t[upper.name].center[0])
    instances += [
        (rd1, {upper.name: "nlp1", lower.name: "outp", "sub": "vss"}),
        (rd2, {upper.name: "nlp2", lower.name: "outn", "sub": "vss"}),
    ]
    load_top = snap(rd_box.top)

    # Vertical TopMetal2 columns: collectors -> rd lower -> rd upper (no horizontal run).
    nlp_w = snap(route_width("TopMetal2"))
    _tm2_column(layout, cell, out_col_l, q1_t["C"].center[1], rd1_t[upper.name].center[1],
                nlp_w, [q1_t["C"], qd1_t["C"], rd1_t[lower.name], rd1_t[upper.name]])
    _tm2_column(layout, cell, out_col_r, q2_t["C"].center[1], rd2_t[upper.name].center[1],
                nlp_w, [q2_t["C"], qd2_t["C"], rd2_t[lower.name], rd2_t[upper.name]])
    em_segments += [
        em.Segment("outp", "TopMetal2", width_um=nlp_w, current_a=i_tail,
                   note="vertical load column in the coil channel"),
        em.Segment("outn", "TopMetal2", width_um=nlp_w, current_a=i_tail,
                   note="vertical load column in the coil channel"),
    ]

    # --- coils facing each other ----------------------------------------------
    _, coil_probe = build(coil)
    coil_half_h = snap(coil_probe.dbbox().width() / 2.0)
    row_y = snap(max(load_top + ROW_GAP, hbt_top + coil_half_h + PWB_TAP_CLEARANCE))
    l1 = coil.with_name("l1")
    l2 = coil.with_name("l2")

    l1_layout, l1_dev = build(l1)
    l2_layout, l2_dev = build(l2)
    l1_terms = {t.name: t for t in derive_terminals(l1, l1_layout, l1_dev)}
    l2_terms = {t.name: t for t in derive_terminals(l2, l2_layout, l2_dev)}
    l1_minus_local = l1_terms["MINUS"].center[0]
    l2_minus_local = l2_terms["MINUS"].center[0]

    l1_dx = snap(nlp_col_l - l1_minus_local)
    l2_dx = snap(nlp_col_r - l2_minus_local)
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

    strap_w = snap(l1_t["PLUS"].width)
    vdd_y = l1_t["PLUS"].center[1]
    rect(layout, cell, "TopMetal2",
          l1_t["PLUS"].center[0], vdd_y - strap_w / 2,
          l2_t["PLUS"].center[0], vdd_y + strap_w / 2)
    em_segments.append(em.Segment("vdd.strap", "TopMetal2", width_um=strap_w,
                                  current_a=i_supply, note="colinear coil supply feeds"))

    # Continue each load column through the coil MINUS pin — vertical only.
    for coil_pin, rd_pin, col_x, name in (
        (l1_t["MINUS"], rd1_t[upper.name], nlp_col_l, "nlp1"),
        (l2_t["MINUS"], rd2_t[upper.name], nlp_col_r, "nlp2"),
    ):
        feed_y = coil_pin.center[1]
        land_y = rd_pin.center[1]
        rect(layout, cell, "TopMetal2", col_x - nlp_w / 2, min(feed_y, land_y),
              col_x + nlp_w / 2, max(feed_y, land_y))
        em_segments.append(
            em.Segment(name, "TopMetal2", width_um=nlp_w, current_a=i_tail,
                       note="coil MINUS colinear with load column")
        )

    # --- control channel from the left edge -----------------------------------
    vicm_w = snap(max(route_width(PORT_METAL), ctrl_w))
    vicm_terminals: list[Terminal] = []
    vicm_xs: list[float] = []
    for base in (qd1_t["B"], qd2_t["B"]):
        bx, by = base.center
        poly_contact(layout, cell, bx, by)
        sign = -1.0 if bx < axis else +1.0
        vx = snap(bx + sign * BASE_VIA_DX)
        stub_h = snap(base.width if base.width < 1.0 else route_width("Metal1"))
        rect(layout, cell, "Metal1", min(bx, vx), by - stub_h / 2, max(bx, vx), by + stub_h / 2)
        via_between(layout, cell, vx, by, "Metal1", PORT_METAL, columns=1, rows=1)
        rect(layout, cell, PORT_METAL, vx - vicm_w / 2, min(by, ctrl_vicm_y),
              vx + vicm_w / 2, max(by, ctrl_vicm_y))
        vicm_xs.append(vx)
        vicm_terminals.append(base)
    rect(layout, cell, PORT_METAL,
          min(vicm_xs) - vicm_w / 2, ctrl_vicm_y - vicm_w / 2,
          max(vicm_xs) + vicm_w / 2, ctrl_vicm_y + vicm_w / 2)
    rect(layout, cell, PORT_METAL, port_left - vicm_w / 2, ctrl_vicm_y - vicm_w / 2,
          min(vicm_xs) - vicm_w / 2, ctrl_vicm_y + vicm_w / 2)

    _gate_port_path(layout, cell, ps1_gx, gate_y, ctrl_steerp_y, port_left,
                    STEERP_METAL, STEERP_METAL, ctrl_w)
    _gate_port_path(layout, cell, pd1_gx, gate_y, ctrl_steern_pd1_y, port_left,
                    STEERN_METAL, PORT_METAL, gate_w)
    _gate_port_path(layout, cell, pd2_gx, gate_y, ctrl_steern_pd2_y, port_left,
                    STEERN_METAL, PORT_METAL, gate_w)

    em_segments += [
        em.Segment("steerp", STEERP_METAL, width_um=ctrl_w, current_a=0.0,
                   note="steering gate bus on Metal3 at gate height"),
        em.Segment("steern", STEERN_METAL, width_um=gate_w, current_a=0.0,
                   note="dummy steering gate bus on Metal2 at gate height"),
        em.Segment("vicm", PORT_METAL, width_um=vicm_w, current_a=0.0,
                   note="dummy base common mode from left edge"),
    ]

    # --- power ring -----------------------------------------------------------
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

    # Signal trunks in the coil channel — vertical columns on Metal4.
    out_top_y = snap(vdd_y - ROW_GAP)
    for name, col_x, orientation in (
        ("outp", out_col_l, 90.0),
        ("outn", out_col_r, 90.0),
    ):
        via_between(layout, cell, col_x, out_top_y, PORT_METAL, "TopMetal2", columns=1, rows=2)
        rect(layout, cell, PORT_METAL, col_x - sig_w / 2, out_top_y, col_x + sig_w / 2, port_top)

    in_trunk_x: dict[str, float] = {}
    for base, sign, name in ((q1_t["B"], +1.0, "inp"), (q2_t["B"], -1.0, "inn")):
        bx, by = base.center
        vx = snap(bx + sign * BASE_VIA_DX)
        stub_h = snap(base.width if base.width < 1.0 else route_width("Metal1"))
        rect(layout, cell, "Metal1", min(bx, vx), by - stub_h / 2, max(bx, vx), by + stub_h / 2)
        via_between(layout, cell, vx, by, "Metal1", IN_TRUNK_METAL, columns=1, rows=1)
        in_trunk_x[name] = vx
        rect(layout, cell, IN_TRUNK_METAL, vx - sig_w / 2, min(by, port_bottom),
              vx + sig_w / 2, max(by, port_bottom))

    signal_ports = {
        "outp": Terminal(name="outp", layer=f"{PORT_METAL.lower()}_drw",
                         center=(out_col_l, port_top), width=sig_w, orientation=90.0),
        "outn": Terminal(name="outn", layer=f"{PORT_METAL.lower()}_drw",
                         center=(out_col_r, port_top), width=sig_w, orientation=90.0),
        "inp": Terminal(name="inp", layer=f"{PORT_METAL.lower()}_drw",
                        center=(in_trunk_x["inp"], port_bottom), width=sig_w, orientation=270.0),
        "inn": Terminal(name="inn", layer=f"{PORT_METAL.lower()}_drw",
                        center=(in_trunk_x["inn"], port_bottom), width=sig_w, orientation=270.0),
    }
    for name in ("outp", "outn", "inp", "inn"):
        em_segments.append(
            em.Segment(name, PORT_METAL, width_um=sig_w, current_a=0.0,
                       note="signal trunk in the coil channel")
        )

    _mgate_port_path(layout, cell, channel_x, gate_y, mgate_link_y, port_left,
                     MGATE_BUS_METAL, PORT_METAL, sig_w)
    mgate_port = Terminal(
        name="mgate", layer=f"{PORT_METAL.lower()}_drw",
        center=(port_left, gate_y), width=sig_w, orientation=180.0,
    )
    em_segments.append(
        em.Segment("mgate", PORT_METAL, width_um=sig_w, current_a=0.0,
                   note="mirror gate bias out of the left cell edge")
    )

    control_ports = {
        "vicm": Terminal(name="vicm", layer=f"{PORT_METAL.lower()}_drw",
                         center=(port_left, ctrl_vicm_y), width=vicm_w, orientation=180.0),
        "steerp": Terminal(name="steerp", layer=f"{STEERP_METAL.lower()}_drw",
                           center=(port_left, ctrl_steerp_y), width=ctrl_w, orientation=180.0),
        "steern": Terminal(name="steern", layer=f"{PORT_METAL.lower()}_drw",
                           center=(port_left, ctrl_steern_pd1_y), width=gate_w, orientation=180.0),
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
                "loads": [rd1_dx, rd2_dx],
                "out_columns": [out_col_l, out_col_r],
                "in_trunks": [in_trunk_x["inp"], in_trunk_x["inn"]],
            },
        },
        notes=[
            f"cell name is {CELL}, shared with the schematic subcircuit",
            "MOS row mdiode|pd1|tail1|ps1|ps2|tail2|pd2 — tail between steering "
            f"devices so tx1/tx2 rise in adjacent columns ({mos_row_w:.1f} um row)",
            f"array gap {array_gap:.1f} um from rail width and M2 spacing plus margin",
            "load columns are coil->rd->collector vertically in the coil channel; "
            "no inward nlp jog",
            "inp/inn and outp/outn trunks stay in the coil channel on Metal4",
            "ctrl bundle (vicm, steerp, steern) and mgate leave the left edge",
            f"coil pin row {coil_half_h:.1f} um above HBTs for pwell_block clearance",
            f"ring half-width {ring_half:.1f} um squared about x={axis:.1f} um",
        ],
    )
    block.em_segments = em_segments
    block.ring = ring
    return block


def _analyze_extracted(path: Path) -> dict:
    """Summarise device and net counts from an extracted netlist."""
    text = path.read_text()
    mos = re.findall(
        r"^M\$\d+ (\S+) (\S+) (\S+) (\S+) sg13_lv_nmos.*W=([\d.]+)u",
        text,
        re.MULTILINE,
    )
    hbts = re.findall(r"^Q\$\d+", text, re.MULTILINE)
    loads = re.findall(r"^R\$\d+", text, re.MULTILINE)
    coils = re.findall(r"^L\$\d+", text, re.MULTILINE)
    drain_nets = sorted({d for d, *_ in mos})
    return {
        "mos_count": len(mos),
        "mos_widths_um": [float(w) for *_, w in mos],
        "hbt_count": len(hbts),
        "load_count": len(loads),
        "coil_count": len(coils),
        "drain_nets": drain_nets,
        "has_em": bool(re.search(r"\bem\b", text)),
        "has_ed1": bool(re.search(r"\bed1\b", text)),
        "has_ed2": bool(re.search(r"\bed2\b", text)),
        "has_tx1": "tx1" in drain_nets or bool(re.search(r"\btx1\b", text)),
        "has_tx2": "tx2" in drain_nets or bool(re.search(r"\btx2\b", text)),
    }


def run_bisect_probe(out_dir: Path, stage: str) -> int:
    """Build a partial cell and summarise extracted connectivity."""
    from layout.common.lvs import run_lvs
    from layout.common.netlist import write_block_cdl

    out_dir.mkdir(parents=True, exist_ok=True)
    block = build_vga_stage(probe=stage)
    gds = block.write(out_dir)
    cdl = write_block_cdl(CELL, block.port_nets, block.instances, out_dir / f"probe_{stage}.cdl")
    lvs = run_lvs(gds=gds, cdl=cdl, run_dir=out_dir / f"lvs_probe_{stage}",
                  topcell=CELL, disable_tap_extraction=True)
    summary = _analyze_extracted(Path(lvs.extracted_netlist))
    summary["lvs_clean"] = lvs.clean
    summary["lvs_summary"] = lvs.summary
    print(f"  bisect {stage}: MOS={summary['mos_count']} drains={summary['drain_nets']} "
          f"HBT={summary['hbt_count']} em={summary['has_em']} ed1={summary['has_ed1']} "
          f"lvs={lvs.summary!r}")
    (out_dir / f"bisect_{stage}.json").write_text(json.dumps(summary, indent=2) + "\n")
    if stage == "mos":
        ok = summary["mos_count"] == 7 and summary["has_tx1"] and summary["has_tx2"]
    elif stage == "arrays":
        ok = summary["mos_count"] == 7 and summary["drain_nets"] != ["mgate|vss"]
    elif stage == "tx":
        ok = (summary["mos_count"] == 7 and summary["has_tx1"] and summary["has_tx2"]
              and "mgate" not in summary["drain_nets"])
    elif stage == "hbt":
        ok = summary["hbt_count"] == 4 and summary["has_em"] and summary["has_ed1"] and summary["has_ed2"]
    else:
        ok = lvs.clean
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--no-render", action="store_true")
    parser.add_argument("--no-pex", action="store_true")
    parser.add_argument("--probe", choices=["arrays", "tx", "mos", "hbt"], default=None,
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
