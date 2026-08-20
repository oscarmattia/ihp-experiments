#!/usr/bin/env python3
"""The VGA stage: symmetric placement, a power ring and EM-sized buses.

The cell is ``vga_dut`` — the same name as the subcircuit in
``circuits/ctle56n/spice/vga_pdk.cir``. ``layout/common/parity.py`` checks that
device for device on every run.

Floorplan, centred on one vertical axis with mirror-image halves (driver template
minus the pad band, plus a separate steering row):

                          out        vdd                 top edge
    ╔════════════════════════════════════════════════╗     power ring
    ║  INDUCTOR P  ══╤═╧╤══     INDUCTOR N         ║     M135 / R270, pins inboard
    ║   body         vdd  nlp      body              ║
    ║                rd1  rd2                        ║     loads between the coils
    ║        Qd1   Q1 ── Q2   Qd2                    ║     dummies outboard
    ║  ┌──── guard ring B ────────────┐               ║
    ║  │  pd1 │ ps1 ┊ ps2 │ pd2      │               ║     steering row
    ║  └──────────────────────────────┘               ║
    ║  ┌──── guard ring A ────────────┐               ║
    ║  │ mdiode │ tail1 ┊ tail2       │               ║     MOS row
    ║  └──────────────────────────────┘               ║
    ╚════════════════════════════════════════════════╝
                          in                             bottom edge

Pins leave on fixed edges: ``outp``/``outn`` and ``vdd`` at the top,
``inp``/``inn`` at the bottom, and ``vicm``, ``steerp``, ``steern`` and
``mgate`` at the **left** edge. ``vss`` taps the ring from the source rail.

Metal budget (no two structures share a layer at the same y crossing):

| Use | Metal |
| --- | --- |
| Array source/drain rails, ``tx1``/``tx2`` between the rows | Metal2 |
| ``steerp``, ``steern`` horizontal buses | Metal3 |
| ``inp``, ``inn``, ``vicm``, ``mgate`` out to the cell edges | Metal4 |
| ``em``, ``ed1``, ``ed2``, ``outp``, ``outn`` above the emitter row | Metal5 |
| ``vdd`` strap, ``nlp``, ring horizontals | TopMetal2 |
| Ring verticals, ``vdd`` riser under ``vss`` | TopMetal1 |

Usage:
    python layout/blocks/vga_stage.py
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
from layout.blocks.power_ring import add_power_ring
from layout.blocks.stage_gates import run_stage_gates
from layout.common import em
from layout.common.devices import build
from layout.common.gds import stamp_net_labels
from layout.common.guard import RingSpec, add_guard_ring
from layout.common.layers import layer_map
from layout.common.pdk import new_layout, pya_module
from layout.common.rules import route_width, rule
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
RAIL_METAL = "Metal2"
CHIP_LEVEL_ALLOWED = ("LBE.a", "LBE.c")
COIL_ACTIVE_GAP = 10.0
OUT_FEED_DX = 8.0


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
    """Output column on ``col_x``; route metal starts above ``em_y``.

    Returns the y the column reaches, which is where a port feed picks it up.
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


def _ring_tap(layout, cell, guard: dict, left_x: float, rail_y: float, rail_w: float, *,
              outside_x: float | None = None) -> None:
    """Land one guard-ring tap on the shared vss source rail."""
    guard_box = guard["outer_box_um"]
    tap_x, tap_y = min(
        guard["tap_centres_um"],
        key=lambda c: (abs(c[1] - guard_box[1]) > 0.1, abs(c[0] - left_x)),
    )
    via_between(layout, cell, tap_x, tap_y, "Metal1", RAIL_METAL, columns=1, rows=1)
    drop_x = snap(outside_x if outside_x is not None else tap_x)
    if abs(drop_x - tap_x) > 1e-6:
        rect(layout, cell, RAIL_METAL, min(tap_x, drop_x), tap_y - rail_w / 2,
              max(tap_x, drop_x), tap_y + rail_w / 2)
    rect(layout, cell, RAIL_METAL,
          drop_x - rail_w / 2, min(tap_y, rail_y), drop_x + rail_w / 2, max(tap_y, rail_y))


def _tx_vertical(
    layout,
    cell,
    tail_drain: Terminal,
    steer_sources: list[Terminal],
    *,
    gap_top: float,
    gap_bottom: float,
    width: float,
) -> None:
    """Bridge the row gap on Metal2; array rails already exist at top and bottom."""
    trunk_x = snap(tail_drain.center[0])
    rect(layout, cell, RAIL_METAL, trunk_x - width / 2, gap_bottom,
          trunk_x + width / 2, gap_top)
    for src in steer_sources:
        sx, sy = src.center
        rect(layout, cell, RAIL_METAL, min(sx, trunk_x), sy - width / 2,
              max(sx, trunk_x), sy + width / 2)


def _steer_drain_rise(
    layout,
    cell,
    drain: Terminal,
    bus_y: float,
    bus_metal: str,
    bus_w: float,
    bus_x: float,
) -> None:
    """Vertical-leg-first rise from a steering drain rail up to a horizontal bus."""
    dx, dy = drain.center
    dx, dy = snap(dx), snap(dy)
    w2 = route_width(RAIL_METAL)
    hop_y = snap(dy + ROW_GAP / 2.0)
    rect(layout, cell, RAIL_METAL, dx - w2 / 2, dy, dx + w2 / 2, hop_y)
    via_between(layout, cell, dx, hop_y, RAIL_METAL, bus_metal, columns=1, rows=1)
    rect(layout, cell, bus_metal, min(dx, bus_x) - bus_w / 2, bus_y - bus_w / 2,
          max(dx, bus_x) + bus_w / 2, bus_y + bus_w / 2)


def build_vga_stage(params: dict[str, float] | None = None,
                    black_box: tuple[str, ...] = ()) -> Block:
    """Place and wire one VGA stage."""
    from layout.blocks.mos_array import _layer_bbox
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

    axis = snap(2 * tail_array_w + 1.5 * array_gap)
    placement: dict[str, float] = {
        "tail1": snap(axis - array_gap / 2.0 - tail_array_w),
    }
    placement["tail2"] = snap(2 * axis - placement["tail1"] - tail_device_span)
    placement["mdiode"] = snap(placement["tail1"] - array_gap - tail_array_w)
    placement["ps1"] = snap(placement["tail1"])
    placement["pd1"] = snap(placement["ps1"] - array_gap - steer_array_w)
    ps1_active_left = snap(placement["ps1"] + steer_rail_w)
    pd1_active_left = snap(placement["pd1"] + steer_rail_w)
    placement["ps2"] = snap(2 * axis - ps1_active_left - steer_device_span - steer_rail_w)
    placement["pd2"] = snap(2 * axis - pd1_active_left - steer_device_span - steer_rail_w)

    mos_row_y = 0.0
    steer_row_y = snap(tail_box.top + ROW_GAP)

    nmos_ports: dict[str, Terminal] = {}
    steer_ports: dict[str, Terminal] = {}
    for name in ("mdiode", "tail1", "tail2"):
        nmos_ports.update(_place_array(layout, cell, arrays[name], placement[name], mos_row_y, name))
        em_segments += arrays[name].em_segments
    for name in ("pd1", "ps1", "ps2", "pd2"):
        steer_ports.update(_place_array(layout, cell, arrays[name], placement[name], steer_row_y, name))
        em_segments += arrays[name].em_segments

    mos_names = ("mdiode", "tail1", "tail2")
    steer_names = ("pd1", "ps1", "ps2", "pd2")
    mos_boxes = {n: arrays[n].cell.dbbox() for n in mos_names}
    steer_boxes = {n: arrays[n].cell.dbbox() for n in steer_names}
    mos_left = snap(min(placement[n] + b.left for n, b in mos_boxes.items()) - 1.0)
    mos_right = snap(max(placement[n] + b.right for n, b in mos_boxes.items()) + 1.0)
    steer_left = snap(min(placement[n] + b.left for n, b in steer_boxes.items()) - 1.0)
    steer_right = snap(max(placement[n] + b.right for n, b in steer_boxes.items()) + 1.0)

    link_w = route_width(RAIL_METAL)
    vss_rail_w = snap(max(em.width_for_a(RAIL_METAL, i_supply), route_width(RAIL_METAL)))
    source_rail_top = snap(nmos_ports["S_tail1"].center[1] + rail_w / 2)
    vss_rail_bottom = snap(source_rail_top - vss_rail_w)
    vss_rail_y = snap(source_rail_top - vss_rail_w / 2)

    mdiode_vss_left = snap(placement["mdiode"] + mos_boxes["mdiode"].left - 1.0)
    mdiode_vss_right = snap(placement["mdiode"] + mos_boxes["mdiode"].right + 1.0)
    tail_vss_left = snap(placement["tail1"] + mos_boxes["tail1"].left - 1.0)
    tail_vss_right = snap(placement["tail2"] + mos_boxes["tail2"].right + 1.0)
    rect(layout, cell, RAIL_METAL, mdiode_vss_left, vss_rail_bottom,
          mdiode_vss_right, source_rail_top)
    rect(layout, cell, RAIL_METAL, tail_vss_left, vss_rail_bottom,
          tail_vss_right, source_rail_top)
    em_segments.append(em.Segment("vss.rail", RAIL_METAL, width_um=vss_rail_w,
                                  current_a=i_supply, note="mirror and tail source rails only"))

    gate_y = nmos_ports["G_tail1"].center[1]
    poly = lm["gatpoly_drw"]
    activ_tops = []
    for name in mos_names:
        ab = _layer_bbox(arrays[name].layout, arrays[name].cell, "activ_drw")
        if ab is not None:
            activ_tops.append(ab.top + mos_row_y)
    strap_bottom = snap(max(activ_tops) + GAT_D_CLEARANCE)
    strap_top = snap(strap_bottom + GATE_STRAP_W)
    cell.shapes(layout.layer(poly[0], poly[1])).insert(
        pya.DBox(mos_left, strap_bottom, mos_right, strap_top)
    )

    diode_right = snap(placement["mdiode"] + mos_boxes["mdiode"].right)
    tail1_left = snap(placement["tail1"] + mos_boxes["tail1"].left)
    channel_x = snap((diode_right + tail1_left) / 2.0)
    gate_tap = poly_contact(layout, cell, channel_x, gate_y)
    via_between(layout, cell, channel_x, gate_y, "Metal1", RAIL_METAL, columns=1, rows=1)

    link_x = snap(diode_right - rail_w / 2)
    diode_rail = nmos_ports["D_mdiode"]
    rect(layout, cell, RAIL_METAL, link_x - link_w / 2, gate_y,
          link_x + link_w / 2, diode_rail.center[1])
    rect(layout, cell, RAIL_METAL, min(link_x, channel_x), gate_y - link_w / 2,
          max(link_x, channel_x), gate_y + link_w / 2)

    instances += [
        (arrays["mdiode"].total_spec.with_name("mdiode"),
         {"D": "mgate", "G": "mgate", "S": "vss", "sub": "vss"}),
        (arrays["tail1"].total_spec.with_name("tail1"),
         {"D": "tx1", "G": "mgate", "S": "vss", "sub": "vss"}),
        (arrays["tail2"].total_spec.with_name("tail2"),
         {"D": "tx2", "G": "mgate", "S": "vss", "sub": "vss"}),
        (arrays["ps1"].total_spec.with_name("ps1"),
         {"D": "em", "G": "steerp", "S": "tx1", "sub": "vss"}),
        (arrays["ps2"].total_spec.with_name("ps2"),
         {"D": "em", "G": "steerp", "S": "tx2", "sub": "vss"}),
        (arrays["pd1"].total_spec.with_name("pd1"),
         {"D": "ed1", "G": "steern", "S": "tx1", "sub": "vss"}),
        (arrays["pd2"].total_spec.with_name("pd2"),
         {"D": "ed2", "G": "steern", "S": "tx2", "sub": "vss"}),
    ]

    mos_box = pya.DBox(mos_left, vss_rail_bottom, mos_right, snap(mos_row_y + tail_box.top))
    guard_mos = add_guard_ring(layout, cell, mos_box, RingSpec(kind="ptap1", clearance=2.0))
    _ring_tap(layout, cell, guard_mos, mos_left, vss_rail_y, vss_rail_w)

    steer_top = snap(steer_row_y + steer_box.top)
    steer_box_guard = pya.DBox(steer_left, steer_row_y, steer_right, steer_top)
    guard_steer = add_guard_ring(layout, cell, steer_box_guard, RingSpec(kind="ptap1", clearance=2.0))
    _ring_tap(layout, cell, guard_steer, steer_left, vss_rail_y, vss_rail_w,
              outside_x=snap(mos_left - 3.0))

    mos_top = snap(max(mos_row_y + tail_box.top, guard_mos["outer_box_um"][3]))
    steer_guard_top = snap(guard_steer["outer_box_um"][3])

    tx_w = snap(max(em.width_for_a(RAIL_METAL, i_tail), route_width(RAIL_METAL)))
    tail_drain_y = snap(nmos_ports["D_tail1"].center[1])
    steer_src_y = snap(steer_ports["S_ps1"].center[1])
    tx_gap_top = snap(tail_drain_y - rail_w / 2)
    tx_gap_bottom = snap(steer_src_y + steer_rail_w / 2)
    _tx_vertical(
        layout, cell, nmos_ports["D_tail1"],
        [steer_ports["S_ps1"], steer_ports["S_pd1"]],
        gap_top=tx_gap_top, gap_bottom=tx_gap_bottom, width=tx_w,
    )
    _tx_vertical(
        layout, cell, nmos_ports["D_tail2"],
        [steer_ports["S_ps2"], steer_ports["S_pd2"]],
        gap_top=tx_gap_top, gap_bottom=tx_gap_bottom, width=tx_w,
    )
    em_segments += [
        em.Segment("tx1", RAIL_METAL, width_um=tx_w, current_a=i_tail,
                   note="tail1 drain to ps1/pd1 sources, vertical between rows"),
        em.Segment("tx2", RAIL_METAL, width_um=tx_w, current_a=i_tail,
                   note="tail2 drain to ps2/pd2 sources, vertical between rows"),
    ]

    sig_w = snap(max(em.width_for_a(ROUTE_METAL, i_tail), route_width(ROUTE_METAL)))
    ctrl_w = snap(max(route_width(CONTROL_METAL), sig_w * 0.5))
    vicm_w = snap(max(route_width(VICM_METAL), ctrl_w))

    col_p_x = snap(axis - COIL_PIN_GAP / 2.0)
    col_n_x = snap(axis + COIL_PIN_GAP / 2.0)
    out_col_p = snap(col_p_x + OUT_FEED_DX)
    out_col_n = snap(col_n_x - OUT_FEED_DX)

    row_y = snap(steer_guard_top + ROW_GAP)
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

    row_y = snap(hbt_top + ROW_GAP)
    rd1 = load.with_name("rd1")
    rd2 = load.with_name("rd2")
    load_terms = {t.name: t for t in derive_terminals(load, *build(load))}
    upper = max(load_terms.values(), key=lambda t: t.center[1])
    lower = min(load_terms.values(), key=lambda t: t.center[1])
    rd_left = _pin_placement_dx(rd1, lower.name, col_p_x)
    rd_right = _pin_placement_dx(rd2, lower.name, col_n_x, "M90")
    rd1_t, rd_box = place(layout, cell, rd1, rd_left, row_y)
    rd2_t, _ = place(layout, cell, rd2, rd_right, row_y, "M90")
    instances += [
        (rd1, {upper.name: "nlp1", lower.name: "outp", "sub": "vss"}),
        (rd2, {upper.name: "nlp2", lower.name: "outn", "sub": "vss"}),
    ]
    load_top = snap(rd_box.top)

    _, coil_probe = build(coil)
    coil_half_h = snap(coil_probe.dbbox().width() / 2.0)
    ptap_y_hi = snap(max(
        y for _, y in guard_mos["tap_centres_um"] + guard_steer["tap_centres_um"]
    ))
    for term in (q1_t, q2_t, qd1_t, qd2_t):
        if "sub" in term:
            ptap_y_hi = snap(max(ptap_y_hi, term["sub"].center[1]))
    row_y = snap(max(
        load_top + ROW_GAP,
        ptap_y_hi + coil_half_h + PWB_TAP_CLEARANCE + rule("PWB_f"),
    ) + COIL_ACTIVE_GAP)
    l1 = coil.with_name("l1")
    l2 = coil.with_name("l2")
    l1_dx = snap(axis - COIL_PIN_GAP / 2.0)
    l2_dx = snap(axis + COIL_PIN_GAP / 2.0)
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
    coil_top = snap(max(l1_box.top, l2_box.top))

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
    q1x, _ = _hbt_emitter_up(layout, cell, q1_t["E"], ROUTE_METAL, outboard_sign=-1.0)
    q2x, _ = _hbt_emitter_up(layout, cell, q2_t["E"], ROUTE_METAL, outboard_sign=+1.0)
    qd1x, _ = _hbt_emitter_up(layout, cell, qd1_t["E"], ROUTE_METAL, outboard_sign=-1.0)
    qd2x, _ = _hbt_emitter_up(layout, cell, qd2_t["E"], ROUTE_METAL, outboard_sign=+1.0)

    em_left = snap(min(q1x, steer_ports["D_ps1"].center[0]) - sig_w)
    em_right = snap(max(q2x, steer_ports["D_ps2"].center[0]) + sig_w)
    rect(layout, cell, ROUTE_METAL, em_left - sig_w / 2, em_y - sig_w / 2,
          em_right + sig_w / 2, em_y + sig_w / 2)
    rect(layout, cell, ROUTE_METAL, q1x - sig_w / 2, min(q1_t["E"].center[1], em_y),
          q1x + sig_w / 2, max(q1_t["E"].center[1], em_y))
    rect(layout, cell, ROUTE_METAL, q2x - sig_w / 2, min(q2_t["E"].center[1], em_y),
          q2x + sig_w / 2, max(q2_t["E"].center[1], em_y))
    rect(layout, cell, ROUTE_METAL, min(q1x, em_left) - sig_w / 2, em_y - sig_w / 2,
          max(q1x, em_left) + sig_w / 2, em_y + sig_w / 2)
    rect(layout, cell, ROUTE_METAL, min(em_right, q2x) - sig_w / 2, em_y - sig_w / 2,
          max(em_right, q2x) + sig_w / 2, em_y + sig_w / 2)

    em_mid = snap((em_left + em_right) / 2.0)
    _steer_drain_rise(layout, cell, steer_ports["D_ps1"], em_y, ROUTE_METAL, sig_w, em_mid)
    _steer_drain_rise(layout, cell, steer_ports["D_ps2"], em_y, ROUTE_METAL, sig_w, em_mid)
    em_segments.append(
        em.Segment("em", ROUTE_METAL, width_um=sig_w, current_a=i_tail,
                   note="shared signal emitter strap")
    )

    ed_left = snap(min(qd1x, steer_ports["D_pd1"].center[0]) - sig_w)
    ed_right = snap(max(qd2x, steer_ports["D_pd2"].center[0]) + sig_w)
    _steer_drain_rise(layout, cell, steer_ports["D_pd1"], em_y, ROUTE_METAL, sig_w, qd1x)
    _steer_drain_rise(layout, cell, steer_ports["D_pd2"], em_y, ROUTE_METAL, sig_w, qd2x)
    rect(layout, cell, ROUTE_METAL, qd1x - sig_w / 2, min(qd1_t["E"].center[1], em_y),
          qd1x + sig_w / 2, max(qd1_t["E"].center[1], em_y))
    rect(layout, cell, ROUTE_METAL, qd2x - sig_w / 2, min(qd2_t["E"].center[1], em_y),
          qd2x + sig_w / 2, max(qd2_t["E"].center[1], em_y))
    rect(layout, cell, ROUTE_METAL, min(qd1x, ed_left) - sig_w / 2, em_y - sig_w / 2,
          max(qd1x, ed_left) + sig_w / 2, em_y + sig_w / 2)
    rect(layout, cell, ROUTE_METAL, min(ed_right, qd2x) - sig_w / 2, em_y - sig_w / 2,
          max(ed_right, qd2x) + sig_w / 2, em_y + sig_w / 2)
    em_segments += [
        em.Segment("ed1", ROUTE_METAL, width_um=sig_w, current_a=i_tail,
                   note="dummy emitter to pd1 drain"),
        em.Segment("ed2", ROUTE_METAL, width_um=sig_w, current_a=i_tail,
                   note="dummy emitter to pd2 drain"),
    ]

    out_col_top: dict[str, float] = {}
    for col_x, q_t, qd_t, rd_t, net in (
        (out_col_p, q1_t, qd1_t, rd1_t, "outp"),
        (out_col_n, q2_t, qd2_t, rd2_t, "outn"),
    ):
        out_col_top[net] = _column_rise(
            layout, cell, [q_t["C"], qd_t["C"], rd_t[lower.name]], col_x,
            ROUTE_METAL, sig_w, em_y=em_y,
        )
        em_segments.append(
            em.Segment(net, ROUTE_METAL, width_um=sig_w, current_a=i_tail,
                       note="collector straight into the load")
        )

    in_trunk_x: dict[str, float] = {}
    for base, sign, name in ((q1_t["B"], +1.0, "inp"), (q2_t["B"], -1.0, "inn")):
        bx, by = base.center
        vx = snap(bx + sign * BASE_VIA_DX)
        stub_h = snap(base.width if base.width < 1.0 else route_width("Metal1"))
        rect(layout, cell, "Metal1", min(bx, vx), by - stub_h / 2, max(bx, vx), by + stub_h / 2)
        via_between(layout, cell, vx, by, "Metal1", IN_TRUNK_METAL, columns=1, rows=1)
        in_trunk_x[name] = vx

    vicm_bus_y = snap(hbt_top + ROW_GAP / 2.0)
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

    tm2_riser_w = route_width("TopMetal2")
    vss_ring_y = ring.ports["vss"][1].center[1]
    via_between(layout, cell, axis, vss_rail_y, RAIL_METAL, "TopMetal2", columns=3, rows=3)
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

    port_top = snap(ring_box[3] + PORT_REACH)
    port_bottom = snap(ring_box[1] - PORT_REACH)
    port_left = snap(ring_box[0] - PORT_REACH)

    signal_ports: dict[str, Terminal] = {}
    for name, col_x, col_top, y_to, orientation in (
        ("outp", out_col_p, out_col_top["outp"], port_top, 90.0),
        ("outn", out_col_n, out_col_top["outn"], port_top, 90.0),
        ("inp", in_trunk_x["inp"], q1_t["B"].center[1], port_bottom, 270.0),
        ("inn", in_trunk_x["inn"], q2_t["B"].center[1], port_bottom, 270.0),
    ):
        col_x = snap(col_x)
        if name.startswith("out"):
            via_between(layout, cell, col_x, col_top, PORT_METAL, ROUTE_METAL)
            rect(layout, cell, PORT_METAL, col_x - sig_w / 2, min(col_top, y_to),
                  col_x + sig_w / 2, max(col_top, y_to))
        else:
            rect(layout, cell, IN_TRUNK_METAL, col_x - sig_w / 2, min(col_top, y_to),
                  col_x + sig_w / 2, max(col_top, y_to))
        signal_ports[name] = Terminal(
            name=name, layer=f"{PORT_METAL.lower()}_drw",
            center=(col_x, y_to), width=sig_w, orientation=orientation,
        )
        em_segments.append(
            em.Segment(name, PORT_METAL, width_um=sig_w, current_a=0.0,
                       note="signal trunk out of the cell edge")
        )

    steer_port_y = snap(steer_ports["G_ps1"].center[1] + STEER_PORT_DY / 2.0)
    steern_port_y = snap(steer_ports["G_pd1"].center[1] - STEER_PORT_DY / 2.0)

    def _steer_gate_port(net: str, devices: tuple[str, ...], port_y: float) -> None:
        xs = []
        for dev in devices:
            gx, gy = steer_ports[f"G_{dev}"].center
            poly_contact(layout, cell, gx, gy)
            via_between(layout, cell, gx, gy, "Metal1", CONTROL_METAL, columns=1, rows=1)
            rect(layout, cell, CONTROL_METAL, gx - ctrl_w / 2, min(gy, port_y),
                  gx + ctrl_w / 2, max(gy, port_y))
            xs.append(gx)
        rect(layout, cell, CONTROL_METAL, port_left - ctrl_w / 2, port_y - ctrl_w / 2,
              max(xs) + ctrl_w / 2, port_y + ctrl_w / 2)
        em_segments.append(
            em.Segment(net, CONTROL_METAL, width_um=ctrl_w, current_a=0.0,
                       note="steering gate out of the left cell edge")
        )

    _steer_gate_port("steerp", ("ps1", "ps2"), steer_port_y)
    _steer_gate_port("steern", ("pd1", "pd2"), steern_port_y)

    rect(layout, cell, VICM_METAL, port_left - vicm_w / 2, vicm_bus_y - vicm_w / 2,
          min(p[0] for p in vicm_pts) - vicm_w / 2, vicm_bus_y + vicm_w / 2)
    em_segments.append(
        em.Segment("vicm", VICM_METAL, width_um=vicm_w, current_a=0.0,
                   note="dummy base common mode out of the left cell edge on Metal4")
    )

    mgate_rise_y = snap(steer_guard_top + ROW_GAP / 2)
    rect(layout, cell, RAIL_METAL, channel_x - link_w / 2, gate_y,
          channel_x + link_w / 2, mgate_rise_y)
    via_between(layout, cell, channel_x, mgate_rise_y, RAIL_METAL, PORT_METAL)
    rect(layout, cell, PORT_METAL, port_left - sig_w / 2, mgate_rise_y - sig_w / 2,
          channel_x + sig_w / 2, mgate_rise_y + sig_w / 2)
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
            center=(port_left, vicm_bus_y), width=vicm_w, orientation=180.0,
        ),
        "steerp": Terminal(
            name="steerp", layer=f"{CONTROL_METAL.lower()}_drw",
            center=(port_left, steer_port_y), width=ctrl_w, orientation=180.0,
        ),
        "steern": Terminal(
            name="steern", layer=f"{CONTROL_METAL.lower()}_drw",
            center=(port_left, steern_port_y), width=ctrl_w, orientation=180.0,
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
        guard=guard_mos,
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
                "out_columns": [out_col_p, out_col_n],
                "in_trunks": [in_trunk_x["inp"], in_trunk_x["inn"]],
            },
        },
        notes=[
            f"cell name is {CELL}, shared with the schematic subcircuit",
            "two MOS rows: mdiode|tail1|tail2 below, pd1|ps1|ps2|pd2 above; "
            f"tx1/tx2 are vertical Metal2 between the rows",
            "tail2, ps2 and pd2 mirror by device span not bounding box",
            f"coil pin row {coil_half_h:.1f} um half-height above highest p-tap "
            f"(PWB_f={rule('PWB_f'):.2f} um) plus {COIL_ACTIVE_GAP:.0f} um clearance",
            f"drawn nlp interconnect {interconnect_um:.1f} um per side",
            "vicm/steerp/steern/mgate leave at the left edge; "
            f"steerp/steern on Metal3, vicm/mgate on Metal4",
            f"output columns jog {OUT_FEED_DX:.0f} um inboard of the load x "
            "so Metal5 does not short the nlp via stack",
            f"wire widths from the technology LEF at the operating point: "
            f"{i_tail * 1e3:.2f} mA per tail, {i_supply * 1e3:.2f} mA supply",
            f"ring squared about the axis at half-width {ring_half:.1f} um and "
            f"{RING_CLEARANCE:.0f} um clearance",
        ],
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
