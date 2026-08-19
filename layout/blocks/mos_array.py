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
straps drawn explicitly.

Each unit's source and drain metal carries a **full-length single-column via
stack** rather than a couple of cuts at one end. That distributes the contact
along the stripe instead of funnelling current through one point, and at 0.21 um
the column is narrow enough to sit on the 0.16 um Metal1 stripe without reaching
the gate poly. From there a short stub reaches the shared Metal2 rail, whose
width comes from the electromigration limit for the current the rail carries.

Drain and source stripes both span the same y at different x, so the two rails
run above and below the array and each column stubs only to its own rail.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache

from layout.common import em
from layout.common.devices import build
from layout.common.layers import layer_map
from layout.common.pdk import new_layout, pya_module
from layout.common.rules import grid, min_space, route_width
from layout.common.spec import DeviceSpec, Terminal

#: Largest width the nmos PCell will actually draw as one finger, in metres.
#: Above this it reverts to minimum width silently, so the array unit must stay
#: at or below it.
MAX_FINGER_W = 10.0e-6

#: Clearance from the device rows to the Metal2 rails, in um.
RAIL_CLEARANCE = 2.0

#: Poly gate strap width in um.
GATE_STRAP_W = 0.6

#: Rule Gat.d: minimum GatPoly space to Activ, in um. The strap has to clear
#: this or every unit reports a violation.
GAT_D_CLEARANCE = 0.08

#: Metal the source/drain columns rise to, and the metal the rails run on.
BUS_METAL = "Metal2"

#: Via layer between Metal1 and the bus metal, for the EM cut count.
BUS_VIA = "Via1"


def _snap(value: float) -> float:
    g = grid()
    return round(round(value / g) * g, 6)


@lru_cache(maxsize=1)
def _via_column_geometry() -> tuple[float, float]:
    """Return ``(height_at_two_rows, row_pitch)`` for a 1-column via stack.

    Measured from the PCell rather than assumed, so a PDK change in via pitch or
    enclosure moves the column length with it.
    """
    heights = {}
    for rows in (2, 3):
        _, cell = build(
            DeviceSpec(
                name=f"_probe_via_{rows}",
                kind="via_stack",
                params={
                    "b_layer": "Metal1",
                    "t_layer": BUS_METAL,
                    "columns": 1,
                    "rows": rows,
                },
            )
        )
        heights[rows] = cell.dbbox().height()
    return heights[2], heights[3] - heights[2]


def via_rows_for_length(length_um: float) -> int:
    """How many via rows fit along a stripe of ``length_um``."""
    base, pitch = _via_column_geometry()
    if length_um < base or pitch <= 0:
        return 2
    return int(2 + math.floor((length_um - base) / pitch))


@dataclass
class MosArray:
    """A strapped array of single-finger MOS units."""

    name: str
    layout: object
    cell: object
    unit: DeviceSpec
    units: int
    total_w: float
    length: float
    ports: dict[str, Terminal]
    via_rows: int
    rail_width_um: float
    current_a: float
    em_segments: list[em.Segment] = field(default_factory=list)

    @property
    def total_spec(self) -> DeviceSpec:
        """The array as a single device, which is how the netlists describe it.

        The LVS deck merges the drawn parallel units into one device of the total
        width, so both the schematic and the layout CDL carry one element. That
        was measured: a CDL with one ``w=243u`` device matches this layout, and so
        does one with ``m=25``, and so do 25 explicit devices.
        """
        return DeviceSpec(
            name=self.name,
            kind=self.unit.kind,
            params={"w": self.total_w, "l": self.length, "ng": 1, "m": 1},
            note=(
                f"{self.units} single-finger units of {self.unit.params['w'] * 1e6:.3f} um "
                f"strapped on {BUS_METAL}"
            ),
        )

    def summary(self) -> dict:
        bbox = self.cell.dbbox()  # type: ignore[attr-defined]
        return {
            "name": self.name,
            "units": self.units,
            "unit_w_um": round(self.unit.params["w"] * 1e6, 4),
            "total_w_um": round(self.total_w * 1e6, 4),
            "l_um": round(self.length * 1e6, 4),
            "via_rows_per_terminal": self.via_rows,
            "via_cuts_per_terminal": self.via_rows,
            "rail_width_um": self.rail_width_um,
            "current_a": self.current_a,
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
    ``size_ctle.snap_drawable_mos_w`` mirrors this on the schematic side.
    """
    units = max(1, int(math.ceil(total_w / max_unit_w)))
    unit_w = round(total_w / units / 5e-9) * 5e-9
    return units, unit_w


def build_mos_array(
    name: str,
    total_w: float,
    length: float,
    current_a: float,
    kind: str = "nmos_lv",
    max_unit_w: float = MAX_FINGER_W,
    pitch_gap: float = 0.6,
) -> MosArray:
    """Build a strapped MOS array carrying ``current_a`` of drain current."""
    pya = pya_module()
    lm = layer_map()

    units, unit_w = plan_units(total_w, max_unit_w)
    unit = DeviceSpec(
        name=f"{name}_unit", kind=kind, params={"w": unit_w, "l": length, "ng": 1, "m": 1}
    )

    unit_layout, unit_cell = build(unit)
    unit_box = unit_cell.dbbox()
    pitch = _snap(unit_box.width() + pitch_gap)

    layout = new_layout()
    cell = layout.create_cell(name)
    index = layout.add_cell(f"{name}_unitcell")
    layout.cell(index).copy_tree(unit_cell)

    from layout.common.wrap import derive_terminals

    unit_terminals = {t.name: t for t in derive_terminals(unit, unit_layout, unit_cell)}
    drain_x = unit_terminals["D"].center[0]
    source_x = unit_terminals["S"].center[0]

    # Active extent sets how long the via column can be and where the gate strap
    # may sit; both are read from the unit rather than assumed.
    activ_box = _layer_bbox(unit_layout, unit_cell, "activ_drw")
    gate_poly = _layer_bbox(unit_layout, unit_cell, "gatpoly_drw")
    if activ_box is None or gate_poly is None:
        raise RuntimeError(f"{name}: unit device has no active area or gate poly")

    via_rows = via_rows_for_length(activ_box.height())
    via = DeviceSpec(
        name=f"{name}_via",
        kind="via_stack",
        params={"b_layer": "Metal1", "t_layer": BUS_METAL, "columns": 1, "rows": via_rows},
    )
    via_layout, via_cell = build(via)
    via_index = layout.add_cell(f"{name}_viacol")
    layout.cell(via_index).copy_tree(via_cell)
    via_box = via_cell.dbbox()

    rail_width = _snap(max(em.width_for_a(BUS_METAL, current_a), route_width(BUS_METAL)))
    drain_rail_y = _snap(unit_box.top + RAIL_CLEARANCE)
    source_rail_y = _snap(unit_box.bottom - RAIL_CLEARANCE - rail_width)

    # The stub from a via column to its rail sits between the drain and source
    # columns, so it cannot be wider than their spacing allows.
    stub_max = (source_x - drain_x) - min_space(BUS_METAL) - via_box.width()
    stub_width = _snap(max(min(route_width(BUS_METAL), stub_max), via_box.width()))

    m2 = lm[f"{BUS_METAL.lower()}_drw"]
    m2_layer = layout.layer(m2[0], m2[1])
    poly = lm["gatpoly_drw"]
    poly_layer = layout.layer(poly[0], poly[1])

    # Centre the column on the active area so it never overhangs into the
    # gate-poly overhang band above and below.
    column_y = _snap(activ_box.center().y - via_box.center().y)

    for i in range(units):
        dx = _snap(i * pitch)
        cell.insert(pya.DCellInstArray(index, pya.DTrans(pya.DVector(dx, 0.0))))

        for x, rail_y, up in (
            (drain_x, drain_rail_y, True),
            (source_x, source_rail_y, False),
        ):
            cell.insert(
                pya.DCellInstArray(
                    via_index,
                    pya.DTrans(pya.DVector(_snap(dx + x - via_box.center().x), column_y)),
                )
            )
            # Stub from the end of the column to its rail.
            col_top = _snap(column_y + via_box.top)
            col_bottom = _snap(column_y + via_box.bottom)
            y0, y1 = (col_top, rail_y + rail_width) if up else (rail_y, col_bottom)
            cell.shapes(m2_layer).insert(
                pya.DBox(
                    _snap(dx + x - stub_width / 2), min(y0, y1),
                    _snap(dx + x + stub_width / 2), max(y0, y1),
                )
            )

    span_left = _snap(-rail_width)
    span_right = _snap((units - 1) * pitch + unit_box.width() + rail_width)

    for rail_y in (drain_rail_y, source_rail_y):
        cell.shapes(m2_layer).insert(
            pya.DBox(span_left, rail_y, span_right, _snap(rail_y + rail_width))
        )

    # Gate strap in poly, in the band where the gate overhangs the active area.
    # Poly drawn over active is a transistor: a strap flush against the active
    # edge merged every unit into one device with its terminals shorted.
    if activ_box.top + GAT_D_CLEARANCE >= gate_poly.top:
        raise RuntimeError(
            f"{name}: the gate poly overhangs active by only "
            f"{gate_poly.top - activ_box.top:.3f} um, too little for a strap "
            f"clearing Gat.d ({GAT_D_CLEARANCE} um)"
        )
    strap_bottom = _snap(activ_box.top + GAT_D_CLEARANCE)
    strap_top = _snap(strap_bottom + GATE_STRAP_W)
    cell.shapes(poly_layer).insert(pya.DBox(span_left, strap_bottom, span_right, strap_top))

    ports = {
        "D": Terminal(
            name="D",
            layer=f"{BUS_METAL.lower()}_drw",
            center=(_snap((span_left + span_right) / 2), _snap(drain_rail_y + rail_width / 2)),
            width=rail_width,
            orientation=90.0,
        ),
        "S": Terminal(
            name="S",
            layer=f"{BUS_METAL.lower()}_drw",
            center=(_snap((span_left + span_right) / 2), _snap(source_rail_y + rail_width / 2)),
            width=rail_width,
            orientation=270.0,
        ),
        "G": Terminal(
            name="G",
            layer="gatpoly_drw",
            center=(_snap(span_left + GATE_STRAP_W), _snap((strap_bottom + strap_top) / 2)),
            width=GATE_STRAP_W,
            orientation=180.0,
        ),
    }

    per_unit = current_a / units if units else current_a
    segments = [
        em.Segment(
            net=f"{name}.D_rail", layer=BUS_METAL, width_um=rail_width,
            current_a=current_a, note="drain rail carries the whole device current",
        ),
        em.Segment(
            net=f"{name}.S_rail", layer=BUS_METAL, width_um=rail_width,
            current_a=current_a, note="source rail carries the whole device current",
        ),
        em.Segment(
            net=f"{name}.unit_stub", layer=BUS_METAL, width_um=stub_width,
            current_a=per_unit, note="one unit's share from its via column to the rail",
        ),
        em.Segment(
            net=f"{name}.unit_via", layer=BUS_VIA, cuts=via_rows,
            current_a=per_unit, note="full-length via column on one unit's terminal",
        ),
    ]

    return MosArray(
        name=name,
        layout=layout,
        cell=cell,
        unit=unit,
        units=units,
        total_w=unit_w * units,
        length=length,
        ports=ports,
        via_rows=via_rows,
        rail_width_um=rail_width,
        current_a=current_a,
        em_segments=segments,
    )


def _layer_bbox(layout, cell, layer_name: str):
    """Bounding box of one named layer inside a cell, or None."""
    lm = layer_map()
    ld = lm[layer_name]
    box = None
    it = cell.begin_shapes_rec(layout.layer(ld[0], ld[1]))
    while not it.at_end():
        shape = it.shape()
        if not shape.is_text():
            here = it.dtrans() * shape.dbbox()
            box = here if box is None else box + here
        it.next()
    return box
