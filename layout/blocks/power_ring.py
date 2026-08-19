"""A vdd/vss power ring on the top two metals.

The ring is the block's power grid: two nets running side by side around the
cell, horizontal on the top metal and vertical on the metal below it, stitched
together at the corners.

Splitting the directions across two layers is what makes the ring a grid rather
than four disconnected bars: a horizontal run and a vertical run of the same net
can cross the other net's run without a short, so both nets reach all four sides.

Widths come from the electromigration limit for the current the ring carries, but
on these layers the PDK's minimum width is the binding constraint by an order of
magnitude — 8.7 mA needs 0.54 um of TopMetal2 against a 2.0 um minimum — so the
ring is drawn at the minimum-width-derived routing width and the headroom is
reported rather than trimmed away.

Corner stitching uses the ``via_stack`` PCell, which draws **one** TopVia2 cut per
instance because TopVia2 is a single large-area via. Cut count therefore comes
from placing several instances along the corner, not from the row/column
parameters, which only multiply the thin-metal vias lower in a stack.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from layout.common import em
from layout.common.devices import build
from layout.common.layers import layer_map
from layout.common.pdk import pya_module
from layout.common.rules import grid, min_space, route_width
from layout.common.spec import DeviceSpec, Terminal

#: Horizontal runs go on this metal, vertical runs one below it.
H_METAL = "TopMetal2"
V_METAL = "TopMetal1"

#: Via layer joining them, from layers.VIA_STACK.
CORNER_VIA = "TopVia2"

#: Gap from the enclosed geometry to the innermost ring conductor, in um.
DEFAULT_CLEARANCE = 4.0

#: Spacing multiple between the two nets' conductors. The rule minimum is the
#: floor; a little more keeps the coupling between supply rails down.
NET_SPACING_FACTOR = 1.5


def _snap(value: float) -> float:
    g = grid()
    return round(round(value / g) * g, 6)


@dataclass
class PowerRing:
    """Geometry and ports of a generated ring."""

    outer_box: tuple[float, float, float, float]
    #: Net name -> its conductor width in um.
    widths: dict[str, float] = field(default_factory=dict)
    #: Net name -> ports on each metal, for tapping into the ring.
    ports: dict[str, list[Terminal]] = field(default_factory=dict)
    corner_vias: int = 0
    em_segments: list[em.Segment] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "outer_box_um": list(self.outer_box),
            "widths_um": self.widths,
            "h_metal": H_METAL,
            "v_metal": V_METAL,
            "corner_via": CORNER_VIA,
            "corner_vias_per_corner": self.corner_vias,
            "ports": {
                net: [t.to_dict() for t in terms] for net, terms in self.ports.items()
            },
            "notes": self.notes,
        }


def corner_via_grid(net_current_a: float) -> tuple[int, int]:
    """Rows and columns of corner via instances needed for ``net_current_a``.

    One instance is one TopVia2 cut, so the cut count is the instance count, and
    the instances have to fit inside the square where the horizontal and vertical
    runs overlap — hence a square-ish grid rather than a line.
    """
    import math

    cuts = em.cuts_for_a(CORNER_VIA, net_current_a)
    side = int(math.ceil(math.sqrt(cuts)))
    return side, side


def ring_width(net_current_a: float) -> float:
    """Conductor width for a ring net, in um.

    Three constraints, largest wins: the electromigration limit, the PDK's
    minimum routing width, and enough room in the corner square for the via cuts
    the current needs. On these layers the third usually decides, because the top
    metals carry 15-16 mA/um and the corner via is a 2 um cell.
    """
    em_width = max(
        em.width_for_a(H_METAL, net_current_a),
        em.width_for_a(V_METAL, net_current_a),
    )
    rows, cols = corner_via_grid(net_current_a)
    _, via_cell = build(
        DeviceSpec(
            name="_ring_via_probe",
            kind="via_stack",
            params={"b_layer": V_METAL, "t_layer": H_METAL, "columns": 1, "rows": 1},
        )
    )
    via_box = via_cell.dbbox()
    pitch = via_box.width() + min_space(H_METAL)
    corner_width = cols * via_box.width() + (cols - 1) * min_space(H_METAL)
    del pitch
    return _snap(max(em_width, route_width(H_METAL), route_width(V_METAL), corner_width))


def add_power_ring(
    layout,
    cell,
    inner_box,
    currents: dict[str, float],
    clearance: float = DEFAULT_CLEARANCE,
    order: tuple[str, ...] = ("vss", "vdd"),
) -> PowerRing:
    """Draw a ring per net around ``inner_box``; innermost net first in ``order``."""
    pya = pya_module()
    lm = layer_map()

    h_ld = lm[f"{H_METAL.lower()}_drw"]
    v_ld = lm[f"{V_METAL.lower()}_drw"]
    h_layer = layout.layer(h_ld[0], h_ld[1])
    v_layer = layout.layer(v_ld[0], v_ld[1])

    spacing = _snap(max(min_space(H_METAL), min_space(V_METAL)) * NET_SPACING_FACTOR)

    total_current = max(currents.values()) if currents else 0.0
    via_rows, via_cols = corner_via_grid(total_current)
    corner_instances = via_rows * via_cols

    via = DeviceSpec(
        name="ring_corner_via",
        kind="via_stack",
        params={"b_layer": V_METAL, "t_layer": H_METAL, "columns": 1, "rows": 1},
    )
    _, via_cell = build(via)
    via_index = layout.add_cell("ring_corner_via")
    layout.cell(via_index).copy_tree(via_cell)
    via_box = via_cell.dbbox()
    via_pitch = _snap(via_box.width() + min_space(H_METAL))

    result = PowerRing(outer_box=(0.0, 0.0, 0.0, 0.0), corner_vias=corner_instances)
    offset = clearance

    for net in order:
        width = ring_width(currents.get(net, total_current))
        result.widths[net] = width

        left = _snap(inner_box.left - offset - width)
        right = _snap(inner_box.right + offset)
        bottom = _snap(inner_box.bottom - offset - width)
        top = _snap(inner_box.top + offset)

        # Horizontal runs on the top metal, spanning the full ring width so the
        # corners overlap the vertical runs.
        for y in (bottom, top):
            cell.shapes(h_layer).insert(
                pya.DBox(left, y, _snap(right + width), _snap(y + width))
            )
        # Vertical runs one metal below, spanning the full ring height.
        for x in (left, right):
            cell.shapes(v_layer).insert(
                pya.DBox(x, bottom, _snap(x + width), _snap(top + width))
            )

        # Stitch each corner inside the square where the two runs overlap. Vias
        # placed outside it push a landing pad past the conductor edge, which
        # either shorts toward the neighbouring net's run or reports a spacing
        # violation against it; the conductor width is set to fit this grid.
        for x in (left, right):
            for y in (bottom, top):
                for row in range(via_rows):
                    for col in range(via_cols):
                        ox = _snap(
                            x + width / 2 - via_box.center().x
                            + (col - (via_cols - 1) / 2) * via_pitch
                        )
                        oy = _snap(
                            y + width / 2 - via_box.center().y
                            + (row - (via_rows - 1) / 2) * via_pitch
                        )
                        cell.insert(
                            pya.DCellInstArray(via_index, pya.DTrans(pya.DVector(ox, oy)))
                        )

        # Ports: one per side, so a tap can reach the ring from any direction.
        mid_x = _snap((left + right + width) / 2)
        mid_y = _snap((bottom + top + width) / 2)
        result.ports[net] = [
            Terminal(f"{net}_top", f"{H_METAL.lower()}_drw", (mid_x, _snap(top + width / 2)), width, 90.0),
            Terminal(f"{net}_bottom", f"{H_METAL.lower()}_drw", (mid_x, _snap(bottom + width / 2)), width, 270.0),
            Terminal(f"{net}_left", f"{V_METAL.lower()}_drw", (_snap(left + width / 2), mid_y), width, 180.0),
            Terminal(f"{net}_right", f"{V_METAL.lower()}_drw", (_snap(right + width / 2), mid_y), width, 0.0),
        ]

        current = currents.get(net, total_current)
        result.em_segments += [
            em.Segment(net=f"ring.{net}.horizontal", layer=H_METAL, width_um=width,
                       current_a=current, note=f"{net} top/bottom run"),
            em.Segment(net=f"ring.{net}.vertical", layer=V_METAL, width_um=width,
                       current_a=current, note=f"{net} left/right run"),
            em.Segment(net=f"ring.{net}.corner", layer=CORNER_VIA, cuts=corner_instances,
                       current_a=current, note=f"{net} corner stitch"),
        ]

        result.outer_box = (left, bottom, _snap(right + width), _snap(top + width))
        offset = _snap(offset + width + spacing)

    result.notes.append(
        f"{H_METAL} horizontal, {V_METAL} vertical, {CORNER_VIA} stitched "
        f"{corner_instances} cut(s) per corner"
    )
    result.notes.append(
        "ring width is set by the PDK minimum width, not by electromigration: "
        f"{total_current * 1e3:.2f} mA needs only "
        f"{em.width_for_a(H_METAL, total_current, derate=1.0):.3f} um of {H_METAL}"
    )
    return result
