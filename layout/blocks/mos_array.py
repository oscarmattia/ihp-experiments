"""Strapped MOS arrays.

The foundry ``nmos`` PCell does not connect anything for you. Measured against
the LVS deck:

* ``ng > 1`` draws the fingers but no source/drain straps, so a 4-finger device
  extracts as four transistors in series, not one wide device;
* ``m > 1`` changes nothing in the layout at all — it is a netlist-only
  parameter, so a CDL claiming ``m=4`` cannot match one drawn device;
* a single finger is silently capped: above about 10 um of width the PCell
  reverts to minimum width without raising an error.

So a wide device has to be built as an array of single-finger units with the
straps drawn explicitly. Drain and source stripes both span the full device
height at different x, so a rail on the same metal would short them; the rails
go on Metal2 with a ``via_stack`` per connection. Gates are strapped in poly,
which is free here because the tail gate is a DC bias node.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from layout.common.devices import build
from layout.common.layers import layer_map
from layout.common.pdk import new_layout, pya_module
from layout.common.spec import DeviceSpec, Terminal

#: Largest width the nmos PCell will actually draw as one finger, in metres.
#: Above this it reverts to minimum width silently, so the array unit must stay
#: at or below it.
MAX_FINGER_W = 10.0e-6

#: Metal2 rail width in um, and its clearance from the device rows.
RAIL_WIDTH = 1.0
RAIL_CLEARANCE = 2.0

#: Poly gate strap width in um.
GATE_STRAP_W = 0.6

#: Rule Gat.d: minimum GatPoly space to Activ, in um. The strap has to clear
#: this or every unit reports a violation.
GAT_D_CLEARANCE = 0.08


@dataclass
class MosArray:
    """A strapped array of single-finger MOS units."""

    name: str
    layout: object
    cell: object
    unit: DeviceSpec
    units: int
    total_w: float
    ports: dict[str, Terminal]
    vias: int

    def summary(self) -> dict:
        bbox = self.cell.dbbox()  # type: ignore[attr-defined]
        return {
            "name": self.name,
            "units": self.units,
            "unit_w_um": round(self.unit.params["w"] * 1e6, 4),
            "total_w_um": round(self.total_w * 1e6, 4),
            "l_um": round(self.unit.params["l"] * 1e6, 4),
            "vias": self.vias,
            "bbox_um": {
                "width": round(bbox.width(), 4),
                "height": round(bbox.height(), 4),
            },
            "ports": {n: t.to_dict() for n, t in self.ports.items()},
        }


def plan_units(total_w: float, max_unit_w: float = MAX_FINGER_W) -> tuple[int, float]:
    """Split a total width into equal single-finger units.

    Returns ``(units, unit_width)`` with the unit width snapped to the PCell's
    5 nm width grid so the drawn total matches the requested total.
    """
    units = max(1, int(math.ceil(total_w / max_unit_w)))
    unit_w = round(total_w / units / 5e-9) * 5e-9
    return units, unit_w


def build_mos_array(
    name: str,
    total_w: float,
    length: float,
    kind: str = "nmos_lv",
    max_unit_w: float = MAX_FINGER_W,
    pitch_gap: float = 0.6,
) -> MosArray:
    """Build a strapped MOS array of the requested total width."""
    pya = pya_module()
    lm = layer_map()

    units, unit_w = plan_units(total_w, max_unit_w)
    unit = DeviceSpec(
        name=f"{name}_unit", kind=kind, params={"w": unit_w, "l": length, "ng": 1, "m": 1}
    )

    unit_layout, unit_cell = build(unit)
    unit_box = unit_cell.dbbox()
    pitch = unit_box.width() + pitch_gap

    layout = new_layout()
    cell = layout.create_cell(name)
    index = layout.add_cell(f"{name}_unitcell")
    layout.cell(index).copy_tree(unit_cell)

    # Unit terminal geometry, read from the PCell rather than assumed.
    from layout.common.wrap import derive_terminals

    unit_terminals = {t.name: t for t in derive_terminals(unit, unit_layout, unit_cell)}
    drain_x = unit_terminals["D"].center[0]
    source_x = unit_terminals["S"].center[0]
    gate = unit_terminals["G"]

    via = DeviceSpec(
        name=f"{name}_via",
        kind="via_stack",
        params={"b_layer": "Metal1", "t_layer": "Metal2", "columns": 1, "rows": 2},
    )
    via_layout, via_cell = build(via)
    via_index = layout.add_cell(f"{name}_via")
    layout.cell(via_index).copy_tree(via_cell)
    via_box = via_cell.dbbox()

    drain_rail_y = unit_box.top + RAIL_CLEARANCE
    source_rail_y = unit_box.bottom - RAIL_CLEARANCE - RAIL_WIDTH

    m2 = lm["metal2_drw"]
    m2_layer = layout.layer(m2[0], m2[1])
    poly = lm["gatpoly_drw"]
    poly_layer = layout.layer(poly[0], poly[1])

    via_count = 0
    for i in range(units):
        dx = i * pitch
        cell.insert(pya.DCellInstArray(index, pya.DTrans(pya.DVector(dx, 0.0))))

        # Drain via at the top of the drain stripe, source via at the bottom of
        # the source stripe: the stubs run in opposite directions so neither
        # crosses the other terminal's stripe.
        for x, y in ((dx + drain_x, unit_box.top), (dx + source_x, unit_box.bottom)):
            cell.insert(
                pya.DCellInstArray(
                    via_index,
                    pya.DTrans(
                        pya.DVector(
                            x - via_box.center().x,
                            y - via_box.center().y - (0.0 if y > 0 else 0.0),
                        )
                    ),
                )
            )
            via_count += 1

        # Metal2 stubs from each via up/down to its rail.
        cell.shapes(m2_layer).insert(
            pya.DBox(
                dx + drain_x - RAIL_WIDTH / 2,
                unit_box.top - via_box.height() / 2,
                dx + drain_x + RAIL_WIDTH / 2,
                drain_rail_y + RAIL_WIDTH,
            )
        )
        cell.shapes(m2_layer).insert(
            pya.DBox(
                dx + source_x - RAIL_WIDTH / 2,
                source_rail_y,
                dx + source_x + RAIL_WIDTH / 2,
                unit_box.bottom + via_box.height() / 2,
            )
        )

    span_left = -RAIL_WIDTH
    span_right = (units - 1) * pitch + unit_box.width() + RAIL_WIDTH

    # Rails.
    cell.shapes(m2_layer).insert(
        pya.DBox(span_left, drain_rail_y, span_right, drain_rail_y + RAIL_WIDTH)
    )
    cell.shapes(m2_layer).insert(
        pya.DBox(span_left, source_rail_y, span_right, source_rail_y + RAIL_WIDTH)
    )

    # Gate strap in poly. It has to sit in the band where the gate poly extends
    # past the active area: poly drawn over active is a transistor, and a strap
    # that overlapped active merged all 25 units into one 241 um device with its
    # gate, drain and source shorted together.
    activ = lm["activ_drw"]
    activ_index = layout.layer(activ[0], activ[1])
    activ_box = None
    it = unit_cell.begin_shapes_rec(unit_layout.layer(activ[0], activ[1]))
    while not it.at_end():
        shape = it.shape()
        if not shape.is_text():
            box = it.dtrans() * shape.dbbox()
            activ_box = box if activ_box is None else activ_box + box
        it.next()
    if activ_box is None:
        raise RuntimeError(f"{name}: no active area found in the unit device")

    gate_poly = None
    it = unit_cell.begin_shapes_rec(unit_layout.layer(poly[0], poly[1]))
    while not it.at_end():
        shape = it.shape()
        if not shape.is_text():
            box = it.dtrans() * shape.dbbox()
            gate_poly = box if gate_poly is None else gate_poly + box
        it.next()
    if gate_poly is None:
        raise RuntimeError(f"{name}: no gate poly found in the unit device")

    # Gat.d requires 0.07 um between poly and active, so the strap cannot sit
    # flush against the active edge; it goes just above, still inside the band
    # where the gate poly overhangs, so the two merge.
    if activ_box.top + GAT_D_CLEARANCE >= gate_poly.top:
        raise RuntimeError(
            f"{name}: the gate poly only overhangs active by "
            f"{gate_poly.top - activ_box.top:.3f} um, which leaves no room for a "
            f"strap clearing Gat.d ({GAT_D_CLEARANCE} um)"
        )
    strap_bottom = activ_box.top + GAT_D_CLEARANCE
    strap_top = strap_bottom + GATE_STRAP_W
    cell.shapes(poly_layer).insert(pya.DBox(span_left, strap_bottom, span_right, strap_top))
    _ = activ_index

    ports = {
        "D": Terminal(
            name="D",
            layer="metal2_drw",
            center=(round((span_left + span_right) / 2, 6), round(drain_rail_y + RAIL_WIDTH / 2, 6)),
            width=RAIL_WIDTH,
            orientation=90.0,
        ),
        "S": Terminal(
            name="S",
            layer="metal2_drw",
            center=(round((span_left + span_right) / 2, 6), round(source_rail_y + RAIL_WIDTH / 2, 6)),
            width=RAIL_WIDTH,
            orientation=270.0,
        ),
        "G": Terminal(
            name="G",
            layer="gatpoly_drw",
            center=(round(span_left + GATE_STRAP_W, 6), round((strap_bottom + strap_top) / 2, 6)),
            width=GATE_STRAP_W,
            orientation=180.0,
        ),
    }
    _ = gate

    return MosArray(
        name=name,
        layout=layout,
        cell=cell,
        unit=unit,
        units=units,
        total_w=unit_w * units,
        ports=ports,
        vias=via_count,
    )
