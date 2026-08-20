#!/usr/bin/env python3
"""The termination stage: bond pads, ESD, 50 ohm shunt to on-chip vtt.

The cell is ``term_dut`` — the same name as the subcircuit in
``circuits/ctle56n/spice/term_pdk.cir``. ``layout/common/parity.py`` checks that
device for device on every run.

Floorplan, centred on one vertical axis with the two differential halves as exact
mirror images. Bottom to top:

                 inp_top  inn_top                 Metal4, out of the top edge
    ╔═════════════╪════════╪══════════════╗   ring: TopMetal2 horizontal,
    ║   rt_p ╲      │      ╱ rt_n         ║   TopMetal1 vertical, tapped on
    ║             └── vtt ──┘              ║   the axis; control channel above
    ╠═════════════╪════════╪══════════════╣   the pad band
    ║ vdd│inp│inn│vss                      ║   four verticals in the 60 um channel
    ║  esd│ bondpad  bondpad │esd          ║   ESD inboard of each 70 um pad
    ║  inp_pad      clamp       inn_pad      ║   clamp on the axis at the outer edge
    ╚═════════════════╪══════════════════════╝   Metal4 out of the bottom edge
          │ rppd_top                                off-axis left, no signal
          │ rppd_bot ‖ cmim

Pad pitch is derived from ``Pad_fR`` keepout, ESD column width and the four
channel verticals — not guessed. The vtt divider and decap sit left of the
symmetric core, the way the CTLE bias diode sits outside the tail pair.

Usage:
    python layout/blocks/term_stage.py
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
    VIA_OFFSET,
    mirrored_pair_x,
    place,
    rect,
    snap,
    trunk_net,
    via_between,
    via_up,
)
from layout.blocks.generators import Block
from layout.blocks.power_ring import PowerRing, add_power_ring
from layout.blocks.stage_gates import run_stage_gates
from layout.common import em
from layout.common.devices import build
from layout.common.gds import stamp_net_labels
from layout.common.guard import RingSpec, add_guard_ring
from layout.common.pdk import new_layout, pya_module
from layout.common.rules import route_width, min_space, rule
from layout.common.sizing import read_params
from layout.common.spec import DeviceSpec, Terminal

OUT_DIR = Path(__file__).resolve().parent / "out" / "term_stage"
PARAMS_INC = Path("circuits/ctle56n/spice/term_params.inc")

CELL = "term_dut"
PORT_NETS = ["inp", "inn", "vdd", "vss"]

ROW_GAP = 8.0
TARGET_PAD_PITCH = 130.0
MAX_PAD_PITCH = 200.0
PORT_METAL = "Metal4"
#: Bond-pad feeder on Metal5, like the driver stage: TopMetal1/TopMetal2 also
#: carry the power ring, so stacking them from the signal trunk shorts inp to vdd.
PAD_FEED_METAL = "Metal5"
#: vtt and the divider loads — Metal4 carries the differential trunks in the pad
#: gap; routing vtt on the same layer crosses those trunks when the rotated rsil
#: MINUS leg runs horizontally toward the axis (MEMORY.md vertical-leg-first).
VTT_METAL = "Metal5"
PORT_REACH = 6.0
RING_CLEARANCE = 10.0
CHIP_LEVEL_ALLOWED: tuple[str, ...] = ()

_PAD_STACK = ("Metal5", "TopMetal1", "TopMetal2")
_PAD_KEEPOUT = rule("Pad_fR")

#: Set by ``out/term_stage/_bisect_lvs.py`` only; ``None`` means build the full cell.
_BISECT_STAGE: str | None = None

_BISECT_ORDER = ("pads_esd", "plus_divider", "plus_clamp", "plus_ring", "full")


def _derive_pad_pitch_um(
    pad_half: float,
    esd_w: float,
    *,
    keepout: float | None = None,
    supply_w: float | None = None,
    sig_w: float | None = None,
) -> tuple[float, str]:
    """Smallest snapped pitch in [TARGET, MAX] that fits the channel verticals."""
    ko = keepout if keepout is not None else rule("Pad_fR")
    vdd_w = supply_w if supply_w is not None else snap(max(route_width("Metal2") * 3.0, 2.0))
    sig = sig_w if sig_w is not None else route_width(PORT_METAL)
    vss_w = vdd_w
    sp_m2 = min_space("Metal2")
    sp_m4 = min_space("Metal4")
    rail_budget = vdd_w + sp_m2 + sig + sp_m4 + sig + sp_m4 + vss_w

    def middle_um(pitch: float) -> float:
        left_ko = -pitch / 2.0 + pad_half + ko
        right_ko = pitch / 2.0 - pad_half - ko
        return (right_ko - left_ko) - 2.0 * esd_w

    pitch = TARGET_PAD_PITCH
    while pitch <= MAX_PAD_PITCH + 1e-9:
        mid = middle_um(pitch)
        if mid + 1e-9 >= rail_budget:
            chosen = snap(pitch)
            return chosen, (
                f"pitch {chosen:.0f} um: 70 um pads leave {chosen - 2 * pad_half:.0f} um channel; "
                f"Pad_fR {ko:.1f} um keepout shrinks usable span to "
                f"{chosen - 2 * pad_half - 2 * ko:.0f} um; two ESD columns at {esd_w:.2f} um take "
                f"{2 * esd_w:.2f} um; {mid:.2f} um remain for four verticals needing "
                f"{rail_budget:.2f} um ({vdd_w:.2f}+{sp_m2:.2f}+{sig:.2f}+{sp_m4:.2f}+"
                f"{sig:.2f}+{sp_m4:.2f}+{vss_w:.2f})"
            )
        pitch = snap(pitch + 5.0)
    chosen = snap(MAX_PAD_PITCH)
    mid = middle_um(chosen)
    return chosen, (
        f"pitch {chosen:.0f} um (MAX): middle {mid:.2f} um vs rail budget {rail_budget:.2f} um"
    )


def _channel_rail_x(
    channel_left: float,
    channel_right: float,
    supply_w: float,
    sig_w: float,
) -> tuple[float, float, float, float]:
    """``vdd | inp | inn | vss`` centres left-to-right inside the channel window."""
    sp_m2 = min_space("Metal2")
    sp_m4 = min_space("Metal4")
    x = channel_left
    vdd_x = snap(x + supply_w / 2.0)
    x += supply_w + sp_m2
    inp_x = snap(x + sig_w / 2.0)
    x += sig_w + sp_m4
    inn_x = snap(x + sig_w / 2.0)
    x += sig_w + sp_m4
    vss_x = snap(x + supply_w / 2.0)
    if vss_x + supply_w / 2.0 > channel_right + 1e-6:
        raise RuntimeError(
            f"channel {channel_right - channel_left:.2f} um too narrow for "
            f"four rails ending at vss_x={vss_x:.2f}"
        )
    return vdd_x, inp_x, inn_x, vss_x


def _bisect_at_least(stage: str) -> bool:
    current = _BISECT_STAGE or "full"
    return _BISECT_ORDER.index(current) >= _BISECT_ORDER.index(stage)


def _via_chain(layout, cell, x: float, y: float, metals: tuple[str, ...]) -> None:
    for low, high in zip(metals, metals[1:]):
        via_between(layout, cell, x, y, low, high)


def _edge_route(layout, cell, metal: str, x: float, y0: float, y1: float, width: float) -> None:
    rect(layout, cell, metal, x - width / 2, min(y0, y1), x + width / 2, max(y0, y1))


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
) -> None:
    """Reach a ring port; change layer at the pin before the vertical leg."""
    tx, ty = via_up(layout, cell, terminal, pin_metal)
    vert_metal = route_metal
    if pin_metal != route_metal:
        via_between(layout, cell, tx, ty, pin_metal, route_metal)
    _edge_route(layout, cell, vert_metal, tx, ty, feed_y, width)
    rect(layout, cell, route_metal, min(tx, tap_x) - width / 2, feed_y - width / 2,
          max(tx, tap_x) + width / 2, feed_y + width / 2)
    _edge_route(layout, cell, route_metal, tap_x, feed_y, ring_y, width)
    via_between(layout, cell, tap_x, ring_y, route_metal, "TopMetal1")


def build_term_stage(params: dict[str, float] | None = None) -> Block:
    """Place and wire one termination stage."""
    from layout.devices.catalog import esd_devices, term_devices

    p = params or read_params(PARAMS_INC)
    term = {spec.name: spec for spec in term_devices(p)}
    esd = {spec.name: spec for spec in esd_devices()}

    rsil = term["rsil_term"]
    rtop = term["rppd_vtt_top"]
    rbot = term["rppd_vtt_bot"]
    cdec = term["cmim_vtt_decap"]
    pad = esd["bondpad_70um"]
    esd_vdd = esd["esd_diodevdd_2kv"]
    esd_vss = esd["esd_diodevss_2kv"]
    clamp_spec = esd["esd_nmoscl_2"]

    i_supply = 5.0e-6
    # ESD straps are sized for a 2 kV discharge, not the LEF average-current rating.
    esd_bus_w = snap(max(route_width("Metal2") * 3.0, 2.0))
    pad_feed_w = snap(route_width(PAD_FEED_METAL))
    tm2_feed_w = snap(route_width("TopMetal2"))
    sig_w = snap(route_width(PORT_METAL))
    vtt_w = snap(route_width(VTT_METAL))
    m3_w = snap(route_width("Metal3"))

    layout = new_layout()
    cell = layout.create_cell(CELL)
    instances: list[tuple[DeviceSpec, dict[str, str]]] = []
    em_segments: list[em.Segment] = []

    _, pad_probe = build(pad)
    pad_half = snap(pad_probe.dbbox().width() / 2.0)
    _, esd_probe = build(esd_vdd)
    esd_h = esd_probe.dbbox().height()
    esd_w_box = esd_probe.dbbox().width()
    pad_pitch, pitch_note = _derive_pad_pitch_um(pad_half, esd_w_box, keepout=_PAD_KEEPOUT,
                                                  supply_w=esd_bus_w, sig_w=sig_w)
    pad_gap = snap(pad_pitch - 2.0 * pad_half)
    axis = snap(pad_half + pad_gap / 2.0 + pad_half)

    _, clamp_probe = build(clamp_spec)
    clamp_nominal_h = clamp_probe.dbbox().height()
    band_floor_y = snap(ROW_GAP)
    clamp_t: dict[str, Terminal] = {}
    clamp_box = None
    if _bisect_at_least("plus_clamp"):
        clamp = clamp_spec.with_name("clamp")
        clamp_bbox = clamp_probe.dbbox()
        clamp_dx = snap(axis - clamp_bbox.width() / 2.0)
        clamp_dy = snap(band_floor_y + clamp_bbox.height() / 2.0)
        clamp_t, clamp_box = place(layout, cell, clamp, clamp_dx, clamp_dy)
        instances.append((clamp, {"VDD": "vdd", "VSS": "vss"}))
        band_floor_y = snap(clamp_box.top + ROW_GAP)
    else:
        band_floor_y = snap(band_floor_y + clamp_nominal_h + ROW_GAP)

    pad_p = pad.with_name("pad_p")
    pad_n = pad.with_name("pad_n")
    pad_left, pad_right = mirrored_pair_x(pad, axis, gap=pad_gap)
    pad_cy = snap(band_floor_y + pad_half)
    pad_p_t, pad_p_box = place(layout, cell, pad_p, pad_left, pad_cy)
    pad_n_t, pad_n_box = place(layout, cell, pad_n, pad_right, pad_cy, "M90")
    pad_top = snap(pad_p_box.top)
    pad_cx_p = pad_p_t["PAD"].center[0]
    pad_cx_n = pad_n_t["PAD"].center[0]

    esd_row_y = snap(pad_p_box.bottom)
    esd_p_dx = snap(pad_p_box.right + _PAD_KEEPOUT)
    esd_n_dx = snap(pad_n_box.left - _PAD_KEEPOUT - esd_w_box)
    channel_left = snap(esd_p_dx + esd_w_box)
    channel_right = snap(esd_n_dx)
    vdd_x, trunk_inp_x, trunk_inn_x, vss_x = _channel_rail_x(
        channel_left, channel_right, esd_bus_w, sig_w,
    )
    bus_top = snap(esd_row_y + 2.0 * esd_h + ROW_GAP)

    esd_terms: dict[str, tuple[dict[str, Terminal], dict[str, Terminal]]] = {}
    esd_top = esd_row_y
    m3_sep = snap(m3_w + min_space("Metal3"))

    for side, pad_net, pad_cx, trunk_x, pad_box, esd_dx in (
        ("p", "inp", pad_cx_p, trunk_inp_x, pad_p_box, esd_p_dx),
        ("n", "inn", pad_cx_n, trunk_inn_x, pad_n_box, esd_n_dx),
    ):
        inboard_x = snap(pad_box.right if side == "p" else pad_box.left)
        pad_feed_x = snap(
            pad_box.right + _PAD_KEEPOUT if side == "p" else pad_box.left - _PAD_KEEPOUT
        )
        evdd = esd_vdd.with_name(f"esd_vdd_{side}")
        evss = esd_vss.with_name(f"esd_vss_{side}")
        evdd_t, evdd_box = place(layout, cell, evdd, esd_dx, esd_row_y)
        evss_t, evss_box = place(
            layout, cell, evss, esd_dx, snap(esd_row_y + esd_h + ROW_GAP / 2.0),
        )
        instances += [
            (evdd, {"VDD": "vdd", "PAD": pad_net, "VSS": "vss"}),
            (evss, {"VDD": "vdd", "PAD": pad_net, "VSS": "vss"}),
        ]
        esd_terms[pad_net] = (evdd_t, evss_t)
        esd_top = max(esd_top, snap(evdd_box.top), snap(evss_box.top))

        pad_stack_y = snap(pad_box.bottom + _PAD_KEEPOUT)
        _via_chain(layout, cell, pad_feed_x, pad_stack_y, _PAD_STACK)
        _edge_route(layout, cell, "TopMetal2", pad_feed_x, pad_stack_y, pad_box.bottom, tm2_feed_w)
        rect(layout, cell, "TopMetal2", min(inboard_x, pad_feed_x) - tm2_feed_w / 2, pad_box.bottom,
              max(inboard_x, pad_feed_x) + tm2_feed_w / 2, pad_box.bottom + tm2_feed_w)
        esd_pad_feed_y = snap(esd_row_y + esd_h + ROW_GAP / 4.0)
        _edge_route(layout, cell, PAD_FEED_METAL, pad_feed_x, esd_pad_feed_y, bus_top, pad_feed_w)
        rect(layout, cell, PAD_FEED_METAL, min(pad_feed_x, trunk_x) - pad_feed_w / 2, bus_top - pad_feed_w / 2,
              max(pad_feed_x, trunk_x) + pad_feed_w / 2, bus_top + pad_feed_w / 2)
        via_between(layout, cell, trunk_x, bus_top, PAD_FEED_METAL, PORT_METAL, columns=2, rows=2)
        for esd_t in (evdd_t, evss_t):
            px, py = via_up(layout, cell, esd_t["PAD"], PAD_FEED_METAL)
            _edge_route(layout, cell, PAD_FEED_METAL, px, py, esd_pad_feed_y, pad_feed_w)
            if abs(px - pad_feed_x) > 1e-6:
                rect(layout, cell, PAD_FEED_METAL, min(px, pad_feed_x) - pad_feed_w / 2, esd_pad_feed_y - pad_feed_w / 2,
                      max(px, pad_feed_x) + pad_feed_w / 2, esd_pad_feed_y + pad_feed_w / 2)

    row_y = snap(esd_top + ROW_GAP)
    esd_vdd_feed_y = snap(esd_top + ROW_GAP / 2.0)
    esd_vss_feed_y = snap(esd_vdd_feed_y - max(m3_sep, 3.0))
    band_top_y = snap(esd_top + ROW_GAP)
    channel_bottom_y = snap(ROW_GAP)
    if clamp_box is not None:
        channel_bottom_y = snap(clamp_box.bottom)
    _edge_route(layout, cell, "Metal2", vdd_x, channel_bottom_y, esd_vdd_feed_y, esd_bus_w)
    _edge_route(layout, cell, "Metal2", vdd_x, esd_vdd_feed_y, band_top_y, esd_bus_w)
    _edge_route(layout, cell, "Metal3", vss_x, esd_vss_feed_y, band_top_y, esd_bus_w)

    if clamp_t:
        clamp_vdd_x, clamp_vdd_y = clamp_t["VDD"].center
        vdd_stub_x, vdd_stub_y = via_up(layout, cell, clamp_t["VDD"], "Metal2")
        _edge_route(layout, cell, "Metal2", clamp_vdd_x, vdd_stub_y, esd_vdd_feed_y, esd_bus_w)
        if abs(vdd_stub_x - vdd_x) > 1e-6:
            rect(layout, cell, "Metal2", min(vdd_stub_x, vdd_x) - esd_bus_w / 2, esd_vdd_feed_y - esd_bus_w / 2,
                  max(vdd_stub_x, vdd_x) + esd_bus_w / 2, esd_vdd_feed_y + esd_bus_w / 2)
        clamp_vss_x, clamp_vss_y = clamp_t["VSS"].center
        vss_stub_x, vss_stub_y = via_up(layout, cell, clamp_t["VSS"], "Metal3")
        _edge_route(layout, cell, "Metal3", clamp_vss_x, vss_stub_y, esd_vss_feed_y, m3_w)
        if abs(vss_stub_x - vss_x) > 1e-6:
            rect(layout, cell, "Metal3", min(vss_stub_x, vss_x) - m3_w / 2, esd_vss_feed_y - m3_w / 2,
                  max(vss_stub_x, vss_x) + m3_w / 2, esd_vss_feed_y + m3_w / 2)

    for _pad_net, (evdd_t, evss_t) in esd_terms.items():
        for terminal in (evdd_t["VDD"], evss_t["VDD"]):
            px, py = via_up(layout, cell, terminal, "Metal2")
            _edge_route(layout, cell, "Metal2", px, py, esd_vdd_feed_y, esd_bus_w)
            if abs(px - vdd_x) > 1e-6:
                rect(layout, cell, "Metal2", min(px, vdd_x) - esd_bus_w / 2, esd_vdd_feed_y - esd_bus_w / 2,
                      max(px, vdd_x) + esd_bus_w / 2, esd_vdd_feed_y + esd_bus_w / 2)
        for terminal in (evdd_t["VSS"], evss_t["VSS"]):
            px, py = via_up(layout, cell, terminal, "Metal2")
            via_between(layout, cell, px, py, "Metal2", "Metal3", columns=1, rows=1)
            _edge_route(layout, cell, "Metal3", px, py, esd_vss_feed_y, esd_bus_w)
            if abs(px - vss_x) > 1e-6:
                rect(layout, cell, "Metal3", min(px, vss_x) - esd_bus_w / 2, esd_vss_feed_y - esd_bus_w / 2,
                      max(px, vss_x) + esd_bus_w / 2, esd_vss_feed_y + esd_bus_w / 2)

    rt_p_t: dict[str, Terminal] = {}
    rt_n_t: dict[str, Terminal] = {}
    guard: dict | None = None
    guard_tap: tuple[float, float] | None = None
    side_x = snap(axis - pad_pitch / 2.0 - ROW_GAP - 40.0)
    vss_bus_x = snap(side_x - 2.0)
    vdd_bus_x = snap(side_x - 2.0 - max(m3_sep, 3.0))
    vdd_strap_y = row_y
    vss_strap_y = row_y

    if _bisect_at_least("plus_divider"):
        _, rt_probe = build(rsil)
        rt_span = rt_probe.dbbox().height()
        rt_p = rsil.with_name("rt_p")
        rt_n = rsil.with_name("rt_n")
        rt_p_t, rt_box = place(
            layout, cell, rt_p, snap(trunk_inp_x - rt_span / 2.0), row_y, "R270",
        )
        rt_n_t, _ = place(
            layout, cell, rt_n, snap(trunk_inn_x + rt_span / 2.0), row_y, "M90",
        )
        instances += [
            (rt_p, {"PLUS": "inp", "MINUS": "vtt", "sub": "vss"}),
            (rt_n, {"PLUS": "inn", "MINUS": "vtt", "sub": "vss"}),
        ]

    if _bisect_at_least("plus_divider"):
        rtop_i = rtop.with_name("rtt_top")
        rbot_i = rbot.with_name("rtt_bot")
        cdec_i = cdec.with_name("vtt_decap")
        divider_y = snap(row_y + ROW_GAP)
        if clamp_box is not None:
            divider_y = snap(max(divider_y, clamp_box.top + ROW_GAP))
        rtop_t, rtop_box = place(layout, cell, rtop_i, snap(side_x + 34.0), divider_y)
        rbot_t, rbot_box = place(
            layout, cell, rbot_i, snap(side_x + 34.0), snap(rtop_box.top + ROW_GAP),
        )
        cdec_t, cdec_box = place(
            layout, cell, cdec_i, snap(side_x + 16.0), snap(rbot_box.top + ROW_GAP),
        )
        instances += [
            (rtop_i, {"PLUS": "vdd", "MINUS": "vtt", "sub": "vss"}),
            (rbot_i, {"PLUS": "vtt", "MINUS": "vss", "sub": "vss"}),
            (cdec_i, {"PLUS": "vtt", "MINUS": "vss"}),
        ]
        vdd_strap_y = snap(rtop_t["PLUS"].center[1])
        vss_strap_y = snap(rbot_t["MINUS"].center[1])
    else:
        _, rtop_probe = build(rtop)
        _, rbot_probe = build(rbot)
        _, cdec_probe = build(cdec)
        rtop_t = {"PLUS": Terminal("PLUS", "metal2_drw", (side_x, row_y), 0.5, 0.0)}
        rbot_t = {"MINUS": Terminal("MINUS", "metal3_drw", (side_x, row_y), 0.5, 0.0)}
        cdec_t = {"PLUS": Terminal("PLUS", "metal2_drw", (side_x, row_y), 0.5, 0.0),
                  "MINUS": Terminal("MINUS", "metal3_drw", (side_x, row_y), 0.5, 0.0)}

    if _bisect_at_least("plus_clamp") and clamp_t and clamp_box is not None:
        guard = add_guard_ring(layout, cell, clamp_box, RingSpec(kind="ptap1", clearance=2.0))
        guard_box = guard["outer_box_um"]
        vss_bus_x = snap(guard_box[0] - 2.0)
        vdd_bus_x = snap(guard_box[0] - 2.0 - max(m3_sep, 3.0))
        tap_x, tap_y = min(
            guard["tap_centres_um"],
            key=lambda c: (abs(c[1] - guard_box[1]) > 0.1, abs(c[0] - guard_box[0])),
        )
        guard_tap = (tap_x, tap_y)
    else:
        vss_bus_x = snap(side_x - 2.0)
        vdd_bus_x = snap(side_x - 2.0 - max(m3_sep, 3.0))

    def _join_vtt(terminal: Terminal, trunk_top_y: float, stub_dx: float) -> float:
        """Reach the axis vtt rail on ``VTT_METAL`` without crossing the signal trunk.

        ``derive_terminals`` does not rotate pin orientation with the instance, so
        ``via_up`` on a rotated ``rsil`` lands on the trunk and shorts Metal4 to
        Metal5 through one Metal1 stack.
        """
        from layout.common.route import metal_of

        tx, ty = terminal.center
        bottom = metal_of(terminal.layer)
        vx = snap(tx + stub_dx)
        vy = snap(ty)
        stub_w = snap(route_width(bottom) if bottom else vtt_w)
        rect(layout, cell, bottom, min(tx, vx) - stub_w / 2, vy - stub_w / 2,
              max(tx, vx) + stub_w / 2, vy + stub_w / 2)
        if bottom and bottom != VTT_METAL:
            via_between(layout, cell, vx, vy, bottom, VTT_METAL)
        y_run = snap(max(vy, trunk_top_y + ROW_GAP))
        rect(layout, cell, VTT_METAL, vx - vtt_w / 2, min(vy, y_run),
              vx + vtt_w / 2, max(vy, y_run))
        rect(layout, cell, VTT_METAL, min(vx, vtt_x), y_run - vtt_w / 2,
              max(vx, vtt_x), y_run + vtt_w / 2)
        return y_run

    vdd_strap_y = snap(rtop_t["PLUS"].center[1])
    vss_strap_y = snap(rbot_t["MINUS"].center[1])
    vtt_x = snap(trunk_inn_x + ROW_GAP)
    vtt_stub_dx = -snap(VIA_OFFSET)
    m2_w = snap(route_width("Metal2"))
    vtt_ys: list[float] = []

    if _bisect_at_least("plus_divider"):
        # Do not strap the clamp on the left bus: its VDD and VSS pins share one Metal3
        # island inside the PCell, so any bus that touches both would short the rails.
        for terminal, bus_x, strap_y, bottom, bus_w in (
            (rtop_t["PLUS"], vdd_bus_x, vdd_strap_y, "Metal2", m2_w),
            (rbot_t["MINUS"], vss_bus_x, vss_strap_y, "Metal3", m3_w),
        ):
            lx, ly = via_up(layout, cell, terminal, bottom)
            rect(layout, cell, bottom, lx - bus_w / 2, min(ly, strap_y), lx + bus_w / 2, max(ly, strap_y))
            rect(layout, cell, bottom, min(lx, bus_x), strap_y - bus_w / 2,
                  max(lx, bus_x), strap_y + bus_w / 2)
        rect(layout, cell, "Metal2", min(vdd_x, vdd_bus_x) - m2_w / 2, band_top_y - m2_w / 2,
              max(vdd_x, vdd_bus_x) + m2_w / 2, band_top_y + m2_w / 2)
        rect(layout, cell, "Metal3", min(vss_x, vss_bus_x) - m3_w / 2, band_top_y - m3_w / 2,
              max(vss_x, vss_bus_x) + m3_w / 2, band_top_y + m3_w / 2)
        # ``cmim`` MINUS: ctle-style Metal5 leg from ``rbot_t['MINUS']`` to the cap plate,
        # then the same Metal5 run into the vss column. A strap from the cap edge alone does
        # not merge mim_btm with the divider ``vss`` net in hierarchical LVS.
        m5_w = snap(route_width("Metal5"))
        leg_y = vss_strap_y
        rx, ry = rbot_t["MINUS"].center
        fx, fy = cdec_t["MINUS"].center
        via_between(layout, cell, rx, ry, "Metal1", "Metal5")
        rect(layout, cell, "Metal5", rx - m5_w / 2, min(ry, leg_y), rx + m5_w / 2, max(ry, leg_y))
        rect(layout, cell, "Metal5", min(rx, fx), leg_y - m5_w / 2, max(rx, fx), leg_y + m5_w / 2)
        rect(layout, cell, "Metal5", fx - m5_w / 2, min(fy, leg_y), fx + m5_w / 2, max(fy, leg_y))
        rect(layout, cell, "Metal5", min(rx, vss_bus_x), leg_y - m5_w / 2,
              max(rx, vss_bus_x), leg_y + m5_w / 2)
        via_between(layout, cell, vss_bus_x, leg_y, "Metal5", "Metal4", columns=2, rows=2)
        via_between(layout, cell, vss_bus_x, leg_y, "Metal4", "Metal3", columns=2, rows=2)
        # ``cmim`` PLUS: TopMetal1 leg from ``rbot_t['PLUS']`` to the cap top plate, then
        # ``_join_vtt`` on the divider ``PLUS`` pin carries ``vtt`` on Metal5.
        plus_leg_y = snap((rbot_box.bottom + cdec_box.top) / 2.0)
        px, py = rbot_t["PLUS"].center
        tx, ty = cdec_t["PLUS"].center
        tm1_w = snap(route_width("TopMetal1"))
        via_between(layout, cell, px, py, "Metal1", "TopMetal1")
        rect(layout, cell, "TopMetal1", px - tm1_w / 2, min(py, plus_leg_y), px + tm1_w / 2, max(py, plus_leg_y))
        rect(layout, cell, "TopMetal1", min(px, tx), plus_leg_y - tm1_w / 2, max(px, tx), plus_leg_y + tm1_w / 2)
        rect(layout, cell, "TopMetal1", tx - tm1_w / 2, min(ty, plus_leg_y), tx + tm1_w / 2, max(ty, plus_leg_y))
        rbot_feed_y = _join_vtt(rbot_t["PLUS"], row_y, vtt_stub_dx)
        vtt_ys += [
            _join_vtt(rtop_t["MINUS"], row_y, vtt_stub_dx),
            rbot_feed_y,
        ]

    if guard_tap is not None:
        tap_x, tap_y = guard_tap
        via_between(layout, cell, tap_x, tap_y, "Metal1", "Metal2", columns=1, rows=1)
        rect(layout, cell, "Metal2", min(tap_x, vss_bus_x) - m2_w / 2, tap_y - m2_w / 2,
              max(tap_x, vss_bus_x) + m2_w / 2, tap_y + m2_w / 2)
        via_between(layout, cell, vss_bus_x, tap_y, "Metal2", "Metal3", columns=1, rows=1)
        _edge_route(layout, cell, "Metal3", vss_bus_x, tap_y, vss_strap_y, m3_w)

    trunk_tops: dict[str, float] = {}
    for pad_net, trunk_x in (("inp", trunk_inp_x), ("inn", trunk_inn_x)):
        trunk_targets: list[Terminal] = []
        if _bisect_at_least("plus_divider"):
            rt_term = rt_p_t["PLUS"] if pad_net == "inp" else rt_n_t["PLUS"]
            trunk_targets.append(rt_term)
        if trunk_targets:
            trunk_bottom, trunk_top_y = trunk_net(
                layout, cell, trunk_targets,
                trunk_x=trunk_x, metal=PORT_METAL, width=sig_w,
            )
        else:
            trunk_bottom, trunk_top_y = bus_top, bus_top
        rect(layout, cell, PORT_METAL, trunk_x - sig_w / 2, min(trunk_bottom, bus_top),
              trunk_x + sig_w / 2, max(trunk_bottom, bus_top))
        trunk_tops[pad_net] = trunk_top_y
        em_segments.append(
            em.Segment(pad_net, PORT_METAL, width_um=sig_w, current_a=0.0,
                       note="differential pad/ESD/resistor trunk")
        )
        if _bisect_at_least("plus_divider"):
            vtt_ys.append(_join_vtt(
                rt_p_t["MINUS"] if pad_net == "inp" else rt_n_t["MINUS"],
                trunk_top_y,
                vtt_stub_dx if pad_net == "inp" else snap(VIA_OFFSET),
            ))
            rt_plus = rt_p_t["PLUS"] if pad_net == "inp" else rt_n_t["PLUS"]
            px, py = via_up(layout, cell, rt_plus, PAD_FEED_METAL)
            _edge_route(layout, cell, PAD_FEED_METAL, px, py, bus_top, pad_feed_w)
            if abs(px - trunk_x) > 1e-6:
                rect(layout, cell, PAD_FEED_METAL, min(px, trunk_x) - pad_feed_w / 2, bus_top - pad_feed_w / 2,
                      max(px, trunk_x) + pad_feed_w / 2, bus_top + pad_feed_w / 2)

    devices_box = cell.dbbox()
    ring_half = snap(max(axis - devices_box.left, devices_box.right - axis))
    ring: PowerRing | None = None
    ring_box = (
        devices_box.left - RING_CLEARANCE,
        devices_box.bottom - RING_CLEARANCE,
        devices_box.right + RING_CLEARANCE,
        devices_box.top + RING_CLEARANCE,
    )

    if _bisect_at_least("plus_ring"):
        ring = add_power_ring(
            layout, cell,
            pya_module().DBox(snap(axis - ring_half), devices_box.bottom,
                              snap(axis + ring_half), devices_box.top),
            currents={"vss": i_supply, "vdd": i_supply},
            clearance=RING_CLEARANCE,
        )
        em_segments += ring.em_segments
        ring_box = ring.outer_box
        vdd_ring_y = ring.ports["vdd"][0].center[1]
        vss_ring_y = ring.ports["vss"][1].center[1]

        strap_w = snap(route_width("TopMetal2"))
        if _bisect_at_least("plus_divider"):
            via_between(layout, cell, vdd_bus_x, vdd_strap_y, "Metal2", "TopMetal1", columns=1, rows=1)
            rect(layout, cell, "TopMetal1", min(vdd_bus_x, axis) - strap_w / 2, vdd_strap_y - strap_w / 2,
                  max(vdd_bus_x, axis) + strap_w / 2, vdd_strap_y + strap_w / 2)
            _edge_route(layout, cell, "Metal3", vss_bus_x, vss_strap_y, vss_ring_y, m3_w)
            rect(layout, cell, "Metal3", min(vss_bus_x, axis) - m3_w / 2, vss_ring_y - m3_w / 2,
                  max(vss_bus_x, axis) + m3_w / 2, vss_ring_y + m3_w / 2)
        via_between(layout, cell, axis, vss_ring_y, "Metal3", "TopMetal2", columns=3, rows=3)
        via_between(layout, cell, axis, vdd_strap_y, "Metal2", "TopMetal1", columns=1, rows=1)
        rect(layout, cell, "TopMetal1", axis - strap_w / 2, vdd_strap_y,
              axis + strap_w / 2, vdd_ring_y)
        via_between(layout, cell, axis, vdd_ring_y, "TopMetal1", "TopMetal2", columns=1, rows=1)
        em_segments += [
            em.Segment("vss.riser", "TopMetal2", width_um=strap_w, current_a=i_supply,
                       note="divider vss bus down to the ring bottom at the axis"),
            em.Segment("vdd.riser", "TopMetal1", width_um=strap_w, current_a=i_supply,
                       note="divider vdd bus up to the ring top, crossing under the vss run"),
        ]

        for _pad_net, (evdd_t, evss_t) in esd_terms.items():
            for terminal in (evdd_t["VDD"], evss_t["VDD"], evdd_t["VSS"], evss_t["VSS"]):
                em_segments.append(
                    em.Segment(f"{_pad_net}.{terminal.name.lower()}", "Metal2", width_um=esd_bus_w,
                               current_a=0.0,
                               note="ESD rail tie; width is for 2 kV discharge, not LEF DC rating")
                )

    if vtt_ys:
        rect(layout, cell, VTT_METAL, vtt_x - vtt_w / 2, min(vtt_ys), vtt_x + vtt_w / 2, max(vtt_ys))

    port_top = snap(ring_box[3] + PORT_REACH)
    port_bottom = snap(ring_box[1] - PORT_REACH)
    signal_ports: dict[str, Terminal] = {}
    for name, trunk_x in (("inp", trunk_inp_x), ("inn", trunk_inn_x)):
        top_y = trunk_tops[name]
        rect(layout, cell, PORT_METAL, trunk_x - sig_w / 2, top_y, trunk_x + sig_w / 2, port_top)
        signal_ports[f"{name}_top"] = Terminal(
            name=f"{name}_top", layer=f"{PORT_METAL.lower()}_drw",
            center=(trunk_x, port_top), width=sig_w, orientation=90.0,
        )
        em_segments.append(
            em.Segment(f"{name}_top", PORT_METAL, width_um=sig_w, current_a=0.0,
                       note="same net as the bond pad, out of the top edge")
        )
        rect(layout, cell, PAD_FEED_METAL, trunk_x - pad_feed_w / 2, port_bottom,
              trunk_x + pad_feed_w / 2, bus_top)
        via_between(layout, cell, trunk_x, port_bottom, PAD_FEED_METAL, PORT_METAL)
        signal_ports[f"{name}_pad"] = Terminal(
            name=f"{name}_pad", layer=f"{PORT_METAL.lower()}_drw",
            center=(trunk_x, port_bottom), width=sig_w, orientation=270.0,
        )
        em_segments.append(
            em.Segment(f"{name}_pad", PORT_METAL, width_um=sig_w, current_a=0.0,
                       note="bond-pad side of the differential net")
        )

    vdd_port = ring.ports["vdd"][0] if ring else Terminal(
        "vdd", "topmetal2_drw", (axis, port_top), sig_w, 90.0,
    )
    vss_port = ring.ports["vss"][0] if ring else Terminal(
        "vss", "topmetal2_drw", (axis, port_bottom), sig_w, 270.0,
    )
    ports = {
        "inp_pad": signal_ports["inp_pad"],
        "inp_top": signal_ports["inp_top"],
        "inn_pad": signal_ports["inn_pad"],
        "inn_top": signal_ports["inn_top"],
        "inp": signal_ports["inp_pad"],
        "inn": signal_ports["inn_pad"],
        "vdd": vdd_port,
        "vss": vss_port,
    }

    labels: list[tuple[Terminal, str]] = [
        (signal_ports["inp_pad"], "inp"), (signal_ports["inp_top"], "inp"),
        (signal_ports["inn_pad"], "inn"), (signal_ports["inn_top"], "inn"),
        (pad_p_t["PAD"], "inp"), (pad_n_t["PAD"], "inn"),
        (vdd_port, "vdd"), (vss_port, "vss"),
    ]
    if _bisect_at_least("plus_divider"):
        labels += [
            (rt_p_t["PLUS"], "inp"), (rt_p_t["MINUS"], "vtt"),
            (rt_n_t["PLUS"], "inn"), (rt_n_t["MINUS"], "vtt"),
        ]
    for pad_net, (evdd_t, evss_t) in esd_terms.items():
        labels += [
            (evdd_t["PAD"], pad_net), (evss_t["PAD"], pad_net),
            (evdd_t["VDD"], "vdd"), (evdd_t["VSS"], "vss"),
            (evss_t["VDD"], "vdd"), (evss_t["VSS"], "vss"),
        ]
    for terminal, net in labels:
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
                "pads": [pad_left, pad_right],
                "signal_trunks": [trunk_inp_x, trunk_inn_x],
            },
        },
        notes=[
            f"cell name is {CELL}, shared with the schematic subcircuit",
            pitch_note,
            f"pad band pitch {pad_pitch:.0f} um (gap {pad_gap:.0f} um); ESD inboard; clamp on axis at outer edge",
            "vtt divider and decap sit left of the axis; channel verticals vdd|inp|inn|vss",
            f"ESD straps drawn at {esd_bus_w:.2f} um for 2 kV discharge, not LEF average current",
            "clamp guard ring ties substrate on the divider vss bus only",
            f"ring squared about x={axis:.2f} um at half-width {ring_half:.1f} um",
        ],
    )
    block.em_segments = em_segments
    block.ring = ring
    return block


_C_LINE = re.compile(r"^C(\S+)\s+(\S+)\s+(\S+)\s+([0-9.eE+-]+)(\w*)(?:\s+\$.*)?$")
_CAP_SI = {"m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15, "a": 1e-18}


def _cap_value(raw: str, suffix: str) -> float:
    scale = _CAP_SI.get(suffix[:1].lower(), 1.0) if suffix else 1.0
    return float(raw) * scale


def _pad_cap_breakdown(pex_spice: Path) -> dict[str, dict[str, float]]:
    """Half-capacitor attribution on inp/inn from explicit Magic PEX ``C`` lines."""
    if not pex_spice.is_file():
        return {}
    buckets = ("pad_metal", "esd", "shunt_50r", "vtt_divider", "diff_cpl", "substrate", "other")
    out: dict[str, dict[str, float]] = {net: {b: 0.0 for b in buckets} for net in ("inp", "inn")}

    def _bucket(other: str, net: str) -> str:
        name = other.lower()
        if "floating" in name:
            return "substrate"
        if name in {"vdd", "vss"} and net in {"inp", "inn"}:
            return "esd"
        if name.startswith("m7_") or "pad" in name:
            return "pad_metal"
        if name.startswith("rt_") or name.startswith("rtt_") or "rsil" in name:
            return "shunt_50r"
        if name == "vtt":
            return "vtt_divider"
        if name in {"inp", "inn"}:
            return "diff_cpl"
        if "ptap" in name or "/sub" in name or name.startswith("w_"):
            return "substrate"
        return "other"

    for line in pex_spice.read_text().splitlines():
        if "**FLOATING" in line:
            continue
        match = _C_LINE.match(line.strip())
        if not match:
            continue
        a, b = match.group(2), match.group(3)
        c = _cap_value(match.group(4), match.group(5))
        for net in ("inp", "inn"):
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

    block = build_term_stage(read_params(PARAMS_INC))
    code, entry = run_stage_gates(
        block,
        args.out,
        schematic=Path("circuits/ctle56n/spice/term_pdk.cir"),
        subckt=CELL,
        params=read_params(PARAMS_INC),
        allowed_rules=CHIP_LEVEL_ALLOWED,
        no_render=args.no_render,
        no_pex=args.no_pex,
    )
    pex_path = args.out / "pex_run" / f"{CELL}_pex.spice"
    pad_caps = _pad_cap_breakdown(pex_path)
    if pad_caps:
        entry.setdefault("pex", {})["pad_capacitance_f"] = pad_caps
        per_net = entry.get("pex", {}).get("per_net_capacitance_f", {})
        summary_path = args.out / f"{CELL}_summary.json"
        if summary_path.is_file():
            summary = json.loads(summary_path.read_text())
            summary.setdefault("pex", {})["pad_capacitance_f"] = pad_caps
            summary_path.write_text(json.dumps(summary, indent=2) + "\n")
        pad_c_model = read_params(PARAMS_INC).get("PAD_C", 0.0)
        for net in ("inp", "inn"):
            parts = pad_caps[net]
            interconnect_ff = sum(parts.values()) * 1e15
            physical_ff = float(per_net.get(net, 0.0)) * 1e15
            detail = ", ".join(f"{k}={v * 1e15:.1f} fF" for k, v in parts.items() if v > 0)
            print(
                f"  pad C   {net}  {physical_ff:.1f} fF physical (Magic per-net); "
                f"{interconnect_ff:.1f} fF explicit C lines ({detail}); "
                f"hand pad model {pad_c_model * 1e15:.1f} fF; "
                f"MEMORY ESD pair ~50.9 fF at 1.4 V"
            )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
