"""Drawing primitives shared by every stage layout.

The comments inside record hard-won DRC/LVS findings — why a via stack goes on a
stub, why the poly contact is 0.16 um, why a vertical net is routed leg-first —
rather than casual notes.
"""

from __future__ import annotations

from layout.common.devices import build
from layout.common.layers import layer_map
from layout.common.pdk import pya_module
from layout.common.rules import grid, route_width
from layout.common.spec import DeviceSpec, Terminal
from layout.common.wrap import derive_terminals

#: Gate contact geometry. Cnt.a makes Cont exactly 0.16 um; Cnt.d wants 0.07 um
#: of GatPoly around it and M1.c1 0.05 um of Metal1.
GATE_CONT_SIZE = 0.16
GATE_CONT_PITCH = 0.16 + 0.18  # Cnt.a + Cnt.b
GATE_CONT_CUTS = 4

#: How far outside a device a via stack is placed, in um. A stack's landing pads
#: are wider than a device pin, so dropping one directly on a pin pushes contact
#: and via spacing rules against the device's own geometry.
VIA_OFFSET = 2.0


def snap(value: float) -> float:
    g = grid()
    return round(round(value / g) * g, 6)


def orient_trans(orientation: str):
    pya = pya_module()
    return {
        "R0": pya.DTrans.R0, "R90": pya.DTrans.R90, "R180": pya.DTrans.R180,
        "R270": pya.DTrans.R270, "M0": pya.DTrans.M0, "M45": pya.DTrans.M45,
        "M90": pya.DTrans.M90, "M135": pya.DTrans.M135,
    }[orientation]


def device_bbox_at(spec: DeviceSpec, dx: float, dy: float, orientation: str = "R0"):
    """BBox a fully placed device would occupy, without drawing anything."""
    pya = pya_module()
    _, device_cell = build(spec)
    trans = pya.DTrans(orient_trans(orientation), pya.DVector(snap(dx), snap(dy)))
    return trans * device_cell.dbbox()


def place(layout, cell, spec: DeviceSpec, dx: float, dy: float, orientation: str = "R0",
          black_box: bool = False):
    """Place a device and return its terminals in stage coordinates."""
    from layout.common.route import metal_of

    pya = pya_module()
    device_layout, device_cell = build(spec)
    terminals = derive_terminals(spec, device_layout, device_cell)

    trans = pya.DTrans(orient_trans(orientation), pya.DVector(snap(dx), snap(dy)))

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
            center=(snap(point.x), snap(point.y)),
            width=terminal.width,
            orientation=terminal.orientation,
        )
        if black_box:
            metal = metal_of(terminal.layer)
            if metal is None:
                continue
            pad_w = snap(terminal.width if terminal.width > 0 else route_width(metal))
            half = pad_w / 2
            cx, cy = placed[terminal.name].center
            rect(layout, cell, metal, cx - half, cy - half, cx + half, cy + half)
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


def mirrored_pair_x(spec: DeviceSpec, axis: float, gap: float) -> tuple[float, float]:
    """Translations for a device and its mirror, exactly symmetric about ``axis``."""
    _, cell = build(spec)
    box = cell.dbbox()
    inner = gap / 2.0
    return snap(axis - inner - box.right), snap(axis + inner + box.right)


def rect(layout, cell, metal: str, x0: float, y0: float, x1: float, y1: float) -> None:
    pya = pya_module()
    lm = layer_map()
    ld = lm[f"{metal.lower()}_drw"]
    cell.shapes(layout.layer(ld[0], ld[1])).insert(
        pya.DBox(snap(min(x0, x1)), snap(min(y0, y1)), snap(max(x0, x1)), snap(max(y0, y1)))
    )


def via_between(layout, cell, x: float, y: float, bottom: str, top: str,
                columns: int = 2, rows: int = 2) -> None:
    """Place one via stack between two metals at a point."""
    pya = pya_module()
    via = DeviceSpec(
        name=f"via_{bottom.lower()}_{top.lower()}",
        kind="via_stack",
        params={"b_layer": bottom, "t_layer": top, "columns": columns, "rows": rows},
    )
    _, via_cell = build(via)
    index = layout.add_cell(f"{via.name}_{snap(x)}_{snap(y)}_{columns}x{rows}")
    layout.cell(index).copy_tree(via_cell)
    box = via_cell.dbbox()
    cell.insert(
        pya.DCellInstArray(
            index, pya.DTrans(pya.DVector(snap(x - box.center().x), snap(y - box.center().y)))
        )
    )


def via_up(layout, cell, terminal: Terminal, metal: str) -> tuple[float, float]:
    """Via stack from a terminal's metal up to ``metal``, on a stub outside it."""
    from layout.common.route import metal_of

    bottom = metal_of(terminal.layer)
    x, y = terminal.center
    if bottom is None:
        return (x, y)

    angle = terminal.orientation % 360.0
    dx = {0.0: VIA_OFFSET, 180.0: -VIA_OFFSET}.get(angle, 0.0)
    dy = {90.0: VIA_OFFSET, 270.0: -VIA_OFFSET}.get(angle, 0.0)
    vx, vy = snap(x + dx), snap(y + dy)

    stub_w = route_width(bottom)
    rect(layout, cell, bottom,
         min(x, vx) - stub_w / 2, min(y, vy) - stub_w / 2,
         max(x, vx) + stub_w / 2, max(y, vy) + stub_w / 2)

    if bottom != metal:
        via_between(layout, cell, vx, vy, bottom, metal)
    return (vx, vy)


def poly_contact(layout, cell, x: float, y: float, cuts: int = GATE_CONT_CUTS) -> Terminal:
    """Contact a GatPoly strap up to Metal1 and return the Metal1 terminal.

    The nmos PCell leaves its gate as bare poly with no contact at all, so a gate
    net cannot leave the device without this. Cont is a fixed 0.16 um in this
    PDK (Cnt.a is both a minimum and a maximum), so a wider connection means more
    cuts rather than a bigger one.
    """
    pya = pya_module()
    lm = layer_map()
    span = (cuts - 1) * GATE_CONT_PITCH
    x0 = snap(x - span / 2)

    cont = lm["cont_drw"]
    cont_layer = layout.layer(cont[0], cont[1])
    for i in range(cuts):
        cx = snap(x0 + i * GATE_CONT_PITCH)
        cell.shapes(cont_layer).insert(
            pya.DBox(
                snap(cx - GATE_CONT_SIZE / 2), snap(y - GATE_CONT_SIZE / 2),
                snap(cx + GATE_CONT_SIZE / 2), snap(y + GATE_CONT_SIZE / 2),
            )
        )

    # Metal1 pad enclosing every cut, and a poly pad doing the same.
    pad_w = snap(span + GATE_CONT_SIZE + 2 * 0.09)
    pad_h = snap(GATE_CONT_SIZE + 2 * 0.09)
    rect(layout, cell, "Metal1", x - pad_w / 2, y - pad_h / 2, x + pad_w / 2, y + pad_h / 2)
    poly = lm["gatpoly_drw"]
    cell.shapes(layout.layer(poly[0], poly[1])).insert(
        pya.DBox(snap(x - pad_w / 2), snap(y - pad_h / 2),
                 snap(x + pad_w / 2), snap(y + pad_h / 2))
    )
    return Terminal("gate_contact", "metal1_drw", (snap(x), snap(y)), pad_h, 90.0)


def trunk_net(layout, cell, terminals: list[Terminal], trunk_x: float, metal: str,
              width: float | None = None) -> tuple[float, float]:
    """Vertical trunk on ``metal`` plus a horizontal stub per terminal.

    Returns the trunk's ``(bottom, top)``, so a caller can carry the net onward
    from where it ends rather than guessing.
    """
    w = width if width is not None else route_width(metal)
    trunk_x = snap(trunk_x)
    points = [via_up(layout, cell, t, metal) for t in terminals]
    ys = [py for _, py in points]
    rect(layout, cell, metal, trunk_x - w / 2, min(ys), trunk_x + w / 2, max(ys))
    for x, y in points:
        rect(layout, cell, metal, min(x, trunk_x), y - w / 2, max(x, trunk_x), y + w / 2)
    return (min(ys), max(ys))


def vertical_net(layout, cell, terminals: list[Terminal], metal: str,
                 width: float | None = None) -> None:
    """Join terminals that share an x with a single vertical run."""
    w = width if width is not None else route_width(metal)
    xs = {t.center[0] for t in terminals}
    if len(xs) != 1:
        raise ValueError(f"vertical net needs one x, got {sorted(xs)}")
    x = xs.pop()
    points = [via_up(layout, cell, t, metal) for t in terminals]
    ys = [py for _, py in points]
    rect(layout, cell, metal, x - w / 2, min(ys), x + w / 2, max(ys))
