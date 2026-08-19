"""Block generators: matched device groups for the CTLE stage.

Each generator places foundry PCells and returns a :class:`Block` carrying the
GDS, the ports, and the CDL instances needed to run LVS against it. Blocks are
where the context rules from Stage 1 become real gates, because a block is where
guard rings and a shared substrate tie exist.

Symmetry matters more than area here. The CTLE is a differential stage, so the
pair, the loads and the two tails are laid out so that the two halves see the
same environment; ``symmetry_error`` on each block reports how well that held.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from layout.common.devices import build, kind_of
from layout.common.gds import layer_summary, stamp_net_labels, write_gds
from layout.common.guard import RingSpec, add_guard_ring
from layout.common.layers import layer_map
from layout.common.xsection import ROUTE_WIDTHS
from layout.common.pdk import new_layout, pya_module
from layout.common.spec import DeviceSpec, Terminal
from layout.common.wrap import derive_terminals


@dataclass
class Block:
    """A generated block and everything needed to verify it."""

    name: str
    layout: Any
    cell: Any
    #: Terminal name -> placed Terminal, in block coordinates.
    ports: dict[str, Terminal] = field(default_factory=dict)
    #: (DeviceSpec, terminal->net) pairs for the CDL.
    instances: list[tuple[DeviceSpec, dict[str, str]]] = field(default_factory=list)
    #: Net names that appear at the block boundary.
    port_nets: list[str] = field(default_factory=list)
    guard: dict | None = None
    notes: list[str] = field(default_factory=list)
    symmetry: dict | None = None

    def write(self, gds_dir: Path) -> Path:
        return write_gds(
            self.layout, self.cell, Path(gds_dir) / f"{self.name}.gds", name=self.name
        )

    def summary(self) -> dict:
        bbox = self.cell.dbbox()
        return {
            "name": self.name,
            "bbox_um": {
                "width": round(bbox.width(), 4),
                "height": round(bbox.height(), 4),
                "area": round(bbox.width() * bbox.height(), 4),
            },
            "ports": {n: t.to_dict() for n, t in self.ports.items()},
            "port_nets": self.port_nets,
            "devices": [
                {"name": s.name, "kind": s.kind, "nets": nets} for s, nets in self.instances
            ],
            "guard_ring": self.guard,
            "symmetry": self.symmetry,
            "notes": self.notes,
            "layers": layer_summary(self.layout, self.cell),
        }


def _place(block_layout, block_cell, spec: DeviceSpec, dx: float, dy: float, mirror: bool = False):
    """Place a device PCell into a block; returns its transformed terminals."""
    pya = pya_module()
    device_layout, device_cell = build(spec)
    terminals = derive_terminals(spec, device_layout, device_cell)

    index = block_layout.add_cell(f"{spec.name}_{'m' if mirror else 'n'}")
    block_layout.cell(index).copy_tree(device_cell)

    trans = pya.DTrans(pya.DTrans.M90 if mirror else pya.DTrans.R0, pya.DVector(dx, dy))
    block_cell.insert(pya.DCellInstArray(index, trans))

    placed = []
    for terminal in terminals:
        point = trans * pya.DPoint(*terminal.center)
        orientation = terminal.orientation
        if mirror:
            # M90 mirrors about the y axis: horizontal directions flip.
            orientation = (180.0 - orientation) % 360.0
        placed.append(
            Terminal(
                name=terminal.name,
                layer=terminal.layer,
                center=(round(point.x, 6), round(point.y, 6)),
                width=terminal.width,
                orientation=orientation,
            )
        )
    return placed


def mirror_pitch(spec: DeviceSpec, gap: float) -> float:
    """Translation for a mirrored copy that leaves ``gap`` um between the cells.

    A mirrored placement reflects the cell about its own origin, so the copy
    extends from ``pitch - right`` to ``pitch - left``. Deriving the pitch from
    the device's own bounding box is what keeps a block correct when the device
    is resized: a hardcoded pitch put the two load resistors 0.04 um apart and
    tripped the Metal1 spacing rule.
    """
    _, cell = build(spec)
    box = cell.dbbox()
    return 2 * box.right + gap


def translate_pitch(spec: DeviceSpec, gap: float) -> float:
    """Translation for an unmirrored copy that leaves ``gap`` um between cells."""
    _, cell = build(spec)
    return cell.dbbox().width() + gap


def _symmetry_error(left: list[Terminal], right: list[Terminal], axis_x: float) -> dict:
    """How closely two half-circuits mirror about ``axis_x``."""
    errors = []
    for a, b in zip(left, right, strict=False):
        mirrored_x = 2 * axis_x - a.center[0]
        errors.append(
            {
                "terminal": a.name,
                "dx_um": round(abs(b.center[0] - mirrored_x), 6),
                "dy_um": round(abs(b.center[1] - a.center[1]), 6),
            }
        )
    worst = max((max(e["dx_um"], e["dy_um"]) for e in errors), default=0.0)
    return {"axis_x_um": round(axis_x, 6), "worst_um": worst, "per_terminal": errors}


# --- blocks ----------------------------------------------------------------


def hbt_differential_pair(spec: DeviceSpec, gap: float = 5.0) -> Block:
    """Two HBTs mirrored about a vertical axis, sharing a guard ring.

    Mirroring rather than translating is what makes the pair matched: both
    devices see the same neighbour on their inner side. A common-centroid
    arrangement would do better against a linear gradient, but it needs four
    devices and the sized design uses Nx=1 per side.
    """
    layout = new_layout()
    cell = layout.create_cell("hbt_diff_pair")
    spacing = mirror_pitch(spec, gap)

    left_spec = spec.with_name(f"{spec.name}_a")
    right_spec = spec.with_name(f"{spec.name}_b")
    left = _place(layout, cell, left_spec, 0.0, 0.0)
    right = _place(layout, cell, right_spec, spacing, 0.0, mirror=True)

    ports: dict[str, Terminal] = {}
    for terminal in left:
        ports[f"{terminal.name}_A"] = terminal
    for terminal in right:
        ports[f"{terminal.name}_B"] = terminal

    axis_x = spacing / 2.0
    block = Block(
        name="hbt_diff_pair",
        layout=layout,
        cell=cell,
        ports=ports,
        instances=[
            (left_spec, {"C": "outp", "B": "inp", "E": "e1", "sub": "sub"}),
            (right_spec, {"C": "outn", "B": "inn", "E": "e2", "sub": "sub"}),
        ],
        port_nets=["outp", "inp", "e1", "outn", "inn", "e2"],
        symmetry=_symmetry_error(left, right, axis_x),
        notes=[
            "mirrored about x={:.2f} um so both devices see the same inner "
            "neighbour".format(axis_x)
        ],
    )
    block.guard = add_guard_ring(layout, cell, cell.dbbox(), RingSpec(kind="ptap1"))
    stamp_net_labels(layout, cell, list(ports.values()))
    return block


def resistor_load_pair(spec: DeviceSpec, gap: float = 1.0) -> Block:
    """The two shunt-peaked load resistors, mirrored."""
    layout = new_layout()
    cell = layout.create_cell("rppd_load_pair")
    spacing = mirror_pitch(spec, gap)

    a = spec.with_name(f"{spec.name}_a")
    b = spec.with_name(f"{spec.name}_b")
    left = _place(layout, cell, a, 0.0, 0.0)
    right = _place(layout, cell, b, spacing, 0.0, mirror=True)

    ports = {f"{t.name}_A": t for t in left}
    ports.update({f"{t.name}_B": t for t in right})

    block = Block(
        name="rppd_load_pair",
        layout=layout,
        cell=cell,
        ports=ports,
        instances=[
            (a, {"PLUS": "nlp1", "MINUS": "outp", "sub": "sub"}),
            (b, {"PLUS": "nlp2", "MINUS": "outn", "sub": "sub"}),
        ],
        port_nets=["nlp1", "outp", "nlp2", "outn"],
        symmetry=_symmetry_error(left, right, spacing / 2.0),
    )
    stamp_net_labels(layout, cell, list(ports.values()))
    return block


def degeneration_network(res_spec: DeviceSpec, cap_spec: DeviceSpec, gap: float = 4.0) -> Block:
    """The emitter degeneration resistor and capacitor, side by side.

    They sit in one block because they are electrically in parallel between the
    two emitter nodes, so keeping them adjacent keeps that loop small.
    """
    layout = new_layout()
    cell = layout.create_cell("degeneration_network")

    res = _place(layout, cell, res_spec, 0.0, 0.0)
    res_box = cell.dbbox()
    cap = _place(layout, cell, cap_spec, res_box.right + gap, 0.0)

    ports = {f"R_{t.name}": t for t in res}
    ports.update({f"C_{t.name}": t for t in cap})

    # Connect the resistor to the capacitor. The metal-finger cap brings its two
    # terminals out stacked on Metal4 and Metal5 at the same point (feed=same),
    # while the resistor's are Metal1, so each side needs a via stack up from
    # Metal1 and then a run on the cap's own metal. Without these the block holds
    # two isolated two-terminal devices and LVS correctly refuses to match a
    # netlist that puts them in parallel.
    by_name = {t.name: t for t in res}
    cap_by_name = {t.name: t for t in cap}
    _connect_metal1_to(
        layout, cell, by_name["PLUS"], cap_by_name["PLUS"], "Metal4", avoid=by_name["MINUS"]
    )
    _connect_metal1_to(
        layout, cell, by_name["MINUS"], cap_by_name["MINUS"], "Metal5", avoid=by_name["PLUS"]
    )

    block = Block(
        name="degeneration_network",
        layout=layout,
        cell=cell,
        ports=ports,
        instances=[
            (res_spec, {"PLUS": "e1", "MINUS": "e2", "sub": "sub"}),
            (cap_spec, {"PLUS": "e1", "MINUS": "e2"}),
        ],
        port_nets=["e1", "e2"],
        notes=[
            "Rs and Cs are in parallel between the emitter nodes",
            "the cap's terminals are stacked on Metal4 and Metal5 (feed=same), "
            "so each side is joined with a via_stack up from the resistor's "
            "Metal1 and a run on the cap's metal",
        ],
    )
    stamp_net_labels(layout, cell, list(ports.values()))
    return block


def _connect_metal1_to(
    layout,
    cell,
    m1_terminal: Terminal,
    target: Terminal,
    metal: str,
    avoid: Terminal | None = None,
) -> None:
    """Join a Metal1 terminal to a terminal on a higher metal.

    A via stack is placed at the Metal1 terminal and an L-shaped run on the
    target metal carries the connection across. gdsfactory's router is not used
    here because this is a deliberate layer change, which it refuses by design.
    """
    pya = pya_module()
    lm = layer_map()

    via = DeviceSpec(
        name=f"via_m1_{metal.lower()}",
        kind="via_stack",
        params={"b_layer": "Metal1", "t_layer": metal, "columns": 2, "rows": 2},
    )
    via_layout, via_cell = build(via)
    index = layout.add_cell(f"{via.name}_{m1_terminal.name}")
    layout.cell(index).copy_tree(via_cell)
    via_box = via_cell.dbbox()

    x0, y0 = m1_terminal.center
    cell.insert(
        pya.DCellInstArray(
            index,
            pya.DTrans(pya.DVector(x0 - via_box.center().x, y0 - via_box.center().y)),
        )
    )

    ld = lm[f"{metal.lower()}_drw"]
    layer_index = layout.layer(ld[0], ld[1])
    x1, y1 = target.center
    # Each metal has its own minimum width, and the thick top metals are far
    # wider than the thin ones: drawing every run at 0.6 um put eight TM2.a
    # width violations on the TopMetal2 connections, whose minimum is 2 um.
    width = ROUTE_WIDTHS.get(metal, 0.6)

    # Which leg to run first depends on where the device's other terminal is.
    # Both orders are wrong in one case and right in the other: a resistor drawn
    # upright has its two terminals in the same column, so a vertical-first run
    # passes straight over the sibling's via pad, while the same resistor rotated
    # has them in the same row and a horizontal-first run does. Either way the
    # two nets short through an intermediate metal of the stack, so step away
    # from the sibling first.
    vertical_first = True
    if avoid is not None:
        ax, ay = avoid.center
        # Sibling in the same column -> leave sideways; same row -> leave upward.
        vertical_first = abs(ax - x0) > abs(ay - y0)

    legs = (
        (x0, min(y0, y1), x0, max(y0, y1), min(x0, x1), y1, max(x0, x1), y1)
        if vertical_first
        else (min(x0, x1), y0, max(x0, x1), y0, x1, min(y0, y1), x1, max(y0, y1))
    )
    ax0, ay0, ax1, ay1, bx0, by0, bx1, by1 = legs
    cell.shapes(layer_index).insert(
        pya.DBox(min(ax0, ax1) - width / 2, min(ay0, ay1) - width / 2,
                 max(ax0, ax1) + width / 2, max(ay0, ay1) + width / 2)
    )
    cell.shapes(layer_index).insert(
        pya.DBox(min(bx0, bx1) - width / 2, min(by0, by1) - width / 2,
                 max(bx0, bx1) + width / 2, max(by0, by1) + width / 2)
    )


def tail_pair(total_w: float, length: float, gap: float = 6.0) -> Block:
    """The two tail devices as strapped arrays, guard-ringed.

    The topology uses two tails, one per emitter node, rather than one shared
    tail; ``ctle_pdk.cir`` instantiates Xtail1 and Xtail2.

    Each tail is an array rather than a single PCell instance because the foundry
    ``nmos`` PCell provides no source/drain strapping and silently caps a single
    finger at about 10 um of width — see :mod:`layout.blocks.mos_array`, which
    also carries the measurements behind that. The array is what makes the
    sized 243 um tail extract as one 243 um device.
    """
    from layout.blocks.mos_array import build_mos_array

    pya = pya_module()
    layout = new_layout()
    cell = layout.create_cell("nmos_tail_pair")

    array_a = build_mos_array("tail1", total_w, length)
    array_b = build_mos_array("tail2", total_w, length)
    array_box = array_a.cell.dbbox()  # type: ignore[attr-defined]
    pitch = array_box.width() + gap

    ports: dict[str, Terminal] = {}
    instances: list[tuple[DeviceSpec, dict[str, str]]] = []
    for tag, array, dx, drain_net in (
        ("A", array_a, 0.0, "e1"),
        ("B", array_b, pitch, "e2"),
    ):
        index = layout.add_cell(f"{array.name}_cell")
        layout.cell(index).copy_tree(array.cell)
        trans = pya.DTrans(pya.DVector(dx, 0.0))
        cell.insert(pya.DCellInstArray(index, trans))
        for name, terminal in array.ports.items():
            point = trans * pya.DPoint(*terminal.center)
            placed = Terminal(
                name=f"{name}_{tag}",
                layer=terminal.layer,
                center=(round(point.x, 6), round(point.y, 6)),
                width=terminal.width,
                orientation=terminal.orientation,
            )
            ports[placed.name] = placed
        nets = {"D": drain_net, "G": "mgate", "S": "vss", "sub": "sub"}
        instances += [
            (array.unit.with_name(f"{array.name}_u{i}"), dict(nets))
            for i in range(array.units)
        ]

    # Tie the two arrays' source rails and gate straps together. Both arrays are
    # identical and sit at the same y, so a single spanning shape on each layer
    # does it; without this the block has two isolated vss nets and two isolated
    # gates, and LVS rightly refuses to match a netlist that shares them.
    lm = layer_map()
    span_left = min(t.center[0] for t in ports.values()) - 1.0
    span_right = max(t.center[0] for t in ports.values()) + 1.0

    source_y = ports["S_A"].center[1]
    m2 = lm["metal2_drw"]
    cell.shapes(layout.layer(m2[0], m2[1])).insert(
        pya.DBox(span_left, source_y - 0.5, span_right, source_y + 0.5)
    )

    gate_y = ports["G_A"].center[1]
    poly = lm["gatpoly_drw"]
    cell.shapes(layout.layer(poly[0], poly[1])).insert(
        pya.DBox(span_left, gate_y - 0.3, span_right, gate_y + 0.3)
    )

    block = Block(
        name="nmos_tail_pair",
        layout=layout,
        cell=cell,
        ports=ports,
        instances=instances,
        port_nets=["e1", "e2", "mgate", "vss"],
        notes=[
            f"each tail is {array_a.units} single-finger units of "
            f"{array_a.unit.params['w'] * 1e6:.2f} um strapped on Metal2 with "
            f"{array_a.vias} vias, totalling {array_a.total_w * 1e6:.1f} um",
            "the nmos PCell provides no finger strapping and caps a single "
            "finger near 10 um, so a wide device must be built as an array",
        ],
    )
    # The tails are the reason LU.b appears at device level: a lone NMOS has no
    # p-well tie. A ring around both satisfies it for the block.
    block.guard = add_guard_ring(layout, cell, cell.dbbox(), RingSpec(kind="ptap1"))
    stamp_net_labels(
        layout,
        cell,
        list(ports.values()),
        {
            "D_A": "e1", "S_A": "vss", "G_A": "mgate",
            "D_B": "e2", "S_B": "vss", "G_B": "mgate",
        },
    )
    return block


def shunt_coil(spec: DeviceSpec) -> Block:
    """The shunt-peaking inductor on its own.

    Kept separate because its parasitics come from openEMS and the fitted
    ind_shunt model, not from Magic, and because its keep-out markers are larger
    than the coil itself.
    """
    layout = new_layout()
    cell = layout.create_cell("shunt_coil")
    terminals = _place(layout, cell, spec, 0.0, 0.0)
    ports = {t.name: t for t in terminals}

    block = Block(
        name="shunt_coil",
        layout=layout,
        cell=cell,
        ports=ports,
        instances=[(spec, {"PLUS": "vdd", "MINUS": "nlp1", "sub": "sub"})],
        port_nets=["vdd", "nlp1"],
        notes=[
            "L and Q come from openEMS plus the fitted ind_shunt model; Magic "
            "contributes substrate capacitance only",
            "the nofill/no-RCX keep-out markers extend well beyond the coil "
            "metal, which is why the cell is much larger than the coil",
        ],
    )
    stamp_net_labels(layout, cell, list(ports.values()))
    return block
