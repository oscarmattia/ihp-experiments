"""Bridge from foundry PCells to gdsfactory components.

Foundry PCells draw geometry but expose no gdsfactory ports, so composition and
routing need a translation step. Rather than hardcode terminal coordinates per
device — which would rot the moment a PCell changes — ports are derived from
what the PCell itself draws:

* pin shapes on the ``*_pin`` layers mark the terminals,
* annotation text on ``text_drw`` (63/0) names them, when the PCell writes one
  (``npn13G2`` labels its pins ``C``/``B``/``E``, for instance).

Only when a PCell writes no terminal names does a per-kind rule assign them by
geometric order. That is safe for the affected devices because MOS source/drain
and the two terminals of a resistor or capacitor are permutable in the LVS
device classes, so which one is called ``D`` and which ``S`` cannot change the
compare result.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from layout.common.devices import DeviceKind, kind_of
from layout.common.layers import PIN_LAYERS, TEXT_DRW, layer_map
from layout.common.spec import DeviceSpec, Terminal

#: Distance in um within which a text label is taken to name a pin.
LABEL_MATCH_TOL = 0.05

#: Below this separation (um) two sibling pins are treated as sitting at the
#: same position along an axis, so they cannot orient each other along it.
_ORIENT_TIE_TOL = 0.01


@dataclass(frozen=True)
class PinShape:
    """One pin polygon found on a PCell's pin layer."""

    layer_name: str
    layer: int
    datatype: int
    left: float
    bottom: float
    right: float
    top: float

    @property
    def center(self) -> tuple[float, float]:
        return ((self.left + self.right) / 2.0, (self.bottom + self.top) / 2.0)

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.top - self.bottom

    @property
    def is_horizontal(self) -> bool:
        """True when the pin is wider than tall, i.e. faces up/down."""
        return self.width >= self.height


def pin_shapes(layout, cell) -> list[PinShape]:
    """Every pin polygon the PCell drew, in PIN_LAYERS preference order."""
    lm = layer_map()
    by_key: dict[tuple[int, int], list[PinShape]] = {}
    for layer_index in layout.layer_indexes():
        info = layout.get_info(layer_index)
        key = (info.layer, info.datatype)
        if key not in PIN_LAYERS:
            continue
        name = lm.name_for(*key) or f"{key[0]}/{key[1]}"
        found: list[PinShape] = []
        it = cell.begin_shapes_rec(layer_index)
        while not it.at_end():
            shape = it.shape()
            if not shape.is_text():
                box = shape.dbbox()
                # begin_shapes_rec yields shapes in the child's coordinate
                # system; apply the accumulated transform to reach cell space.
                box = it.dtrans() * box
                found.append(
                    PinShape(name, key[0], key[1], box.left, box.bottom, box.right, box.top)
                )
            it.next()
        if found:
            by_key[key] = found

    ordered: list[PinShape] = []
    for key in PIN_LAYERS:
        ordered.extend(by_key.get(key, []))
    return ordered


def annotation_labels(layout, cell) -> list[tuple[str, float, float]]:
    """Text annotations on ``text_drw``, as ``(string, x, y)`` in um."""
    from layout.common.pdk import pya_module

    pya = pya_module()
    out: list[tuple[str, float, float]] = []
    for layer_index in layout.layer_indexes():
        info = layout.get_info(layer_index)
        if (info.layer, info.datatype) != TEXT_DRW:
            continue
        it = cell.begin_shapes_rec(layer_index)
        while not it.at_end():
            shape = it.shape()
            if shape.is_text():
                text = shape.text
                # Text coordinates are in database units; lift them into um and
                # through the accumulated instance transform.
                point = it.dtrans() * pya.DPoint(text.x * layout.dbu, text.y * layout.dbu)
                out.append((text.string, point.x, point.y))
            it.next()
    return out


def _orientation_for(pin: PinShape, bbox, siblings: list[PinShape] | None = None) -> float:
    """Outward direction in degrees for a pin.

    A pin is contacted along its long side, so routing leaves across its short
    side: a wide flat pin exits vertically, a tall thin one horizontally.

    Which way along that axis is decided relative to the other pins on the same
    layer, not to the cell outline. On a multi-finger MOS the merged drain and
    source pins both sit well inside the cell and can land on the same side of
    its centre, so a cell-relative test points them the same way and the two
    routes collide. Their mutual midpoint always separates them correctly.
    """
    cx, cy = pin.center
    center = bbox.center()
    ref_x, ref_y = center.x, center.y

    group = [p for p in (siblings or []) if p.layer_name == pin.layer_name]
    if len(group) >= 2:
        group_x = sum(p.center[0] for p in group) / len(group)
        group_y = sum(p.center[1] for p in group) / len(group)
        # Siblings only disambiguate along the axis that actually separates
        # them. An inductor's two feed pins sit side by side at the same height,
        # so their midpoint says nothing about which way is out and the cell
        # outline has to decide.
        axis = abs(cy - group_y) if pin.is_horizontal else abs(cx - group_x)
        if axis > _ORIENT_TIE_TOL:
            ref_x, ref_y = group_x, group_y

    if pin.is_horizontal:
        return 90.0 if cy >= ref_y else 270.0
    return 0.0 if cx >= ref_x else 180.0


def _port_width(pin: PinShape) -> float:
    """Port width is the pin extent across its facing direction."""
    return pin.width if pin.is_horizontal else pin.height


#: Terminal naming for PCells that do not label their own pins. Values are the
#: names to assign to pins in PIN_LAYERS order after sorting by position.
#: ``sort`` picks the axis used to order pins on the same layer.
TERMINAL_RULES: dict[str, dict[str, object]] = {
    # Two metal1 pins (drain/source, permutable) plus one gatpoly pin.
    "nmos_lv": {"names": ("D", "S", "G"), "sort": "x"},
    "pmos_lv": {"names": ("D", "S", "G"), "sort": "x"},
    "rppd": {"names": ("PLUS", "MINUS"), "sort": "y"},
    "rsil": {"names": ("PLUS", "MINUS"), "sort": "y"},
    "rhigh": {"names": ("PLUS", "MINUS"), "sort": "y"},
    # Taps draw a single contact; the well/substrate side is implicit.
    "ntap1": {"names": ("PLUS",), "sort": "y"},
    "ptap1": {"names": ("PLUS",), "sort": "y"},
    "inductor": {"names": ("PLUS", "MINUS"), "sort": "x"},
    "cmomi": {"names": ("PLUS", "MINUS"), "sort": "y"},
    # ESD PCells label their pins on text_drw but the labels do not always sit
    # on the pin centres within LABEL_MATCH_TOL, so names are assigned by layer
    # and position instead. CDL order is VDD PAD VSS (esd_ptap.cdl).
    "esd_diodevdd": {"names": ("VSS", "PAD", "VDD"), "sort": "x"},
    "esd_diodevss": {"names": ("VDD", "PAD", "VSS"), "sort": "x"},
    "esd_nmoscl": {"names": ("VSS", "VDD"), "sort": "y"},
}

#: Devices whose terminals are plates rather than pin shapes. ``cmim`` draws no
#: pin layer at all: the bottom plate is Metal5 and the top plate TopMetal1.
#: The plates are stacked, so each terminal is given a different edge to leave
#: from; putting both on the same edge would place the two ports on top of each
#: other and invite a short at the first route.
PLATE_TERMINALS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "cmim": (
        ("MINUS", "metal5_drw", "left"),
        ("PLUS", "topmetal1_drw", "top"),
    ),
    "bondpad": (("PAD", "topmetal2_drw", "top"),),
}

_EDGE_ORIENTATION = {"left": 180.0, "right": 0.0, "bottom": 270.0, "top": 90.0}

#: Terminals whose exit direction cannot be inferred from geometry because the
#: pin sits alone in the middle of the device. The HBT emitter is the only such
#: case: its Metal2 pin is centred under the collector and base pins, and in a
#: CML pair it always routes downward to the tail.
TERMINAL_ORIENTATION_OVERRIDES: dict[str, dict[str, float]] = {
    "npn13G2": {"E": 270.0},
}


def derive_terminals(spec: DeviceSpec, layout, cell) -> list[Terminal]:
    """Work out named, positioned terminals for a placed device."""
    kind = kind_of(spec)
    bbox = cell.dbbox()
    pins = pin_shapes(layout, cell)
    labels = annotation_labels(layout, cell)

    if not pins and spec.kind in PLATE_TERMINALS:
        return _plate_terminals(spec, layout, cell)

    if not pins:
        raise RuntimeError(
            f"{spec.name}: PCell {kind.pcell} drew no pin shapes and has no plate rule"
        )

    named = _name_by_labels(pins, labels)
    if named is None:
        named = _name_by_rule(spec, kind, pins)

    overrides = TERMINAL_ORIENTATION_OVERRIDES.get(spec.kind, {})
    terminals = []
    for name, pin in named:
        orientation = overrides.get(name)
        if orientation is None:
            orientation = _orientation_for(pin, bbox, siblings=pins)
        terminals.append(
            Terminal(
                name=name,
                layer=pin.layer_name,
                center=pin.center,
                width=round(_port_width(pin), 6),
                orientation=orientation,
            )
        )
    return terminals


def _name_by_labels(
    pins: list[PinShape], labels: list[tuple[str, float, float]]
) -> list[tuple[str, PinShape]] | None:
    """Name pins from coincident annotation text, or return None.

    Only accepted when every pin gets a distinct name, so a device that labels
    some but not all of its pins falls back to the geometric rule rather than
    producing a half-named set.
    """
    matched: list[tuple[str, PinShape]] = []
    used: set[str] = set()
    for pin in pins:
        cx, cy = pin.center
        best: tuple[float, str] | None = None
        for text, lx, ly in labels:
            if text in used:
                continue
            distance = math.hypot(lx - cx, ly - cy)
            if distance <= LABEL_MATCH_TOL and (best is None or distance < best[0]):
                best = (distance, text)
        if best is None:
            return None
        used.add(best[1])
        matched.append((best[1], pin))
    if len({name for name, _ in matched}) != len(matched):
        return None
    return matched


def _name_by_rule(
    spec: DeviceSpec, kind: DeviceKind, pins: list[PinShape]
) -> list[tuple[str, PinShape]]:
    rule = TERMINAL_RULES.get(spec.kind)
    if rule is None:
        raise RuntimeError(
            f"{spec.name}: PCell {kind.pcell} does not label its pins and "
            f"no TERMINAL_RULES entry exists for kind {spec.kind!r}"
        )
    names: tuple[str, ...] = rule["names"]  # type: ignore[assignment]
    axis: str = rule["sort"]  # type: ignore[assignment]

    # Group by layer, preserving the PIN_LAYERS preference order, then sort
    # within each layer so naming is deterministic across runs.
    groups: dict[str, list[PinShape]] = {}
    for pin in pins:
        groups.setdefault(pin.layer_name, []).append(pin)
    ordered: list[PinShape] = []
    for layer_name in dict.fromkeys(p.layer_name for p in pins):
        group = groups[layer_name]
        group.sort(key=lambda p: p.center[0] if axis == "x" else p.center[1])
        ordered.extend(group)

    if len(ordered) < len(names):
        raise RuntimeError(
            f"{spec.name}: expected at least {len(names)} pins for {spec.kind}, "
            f"found {len(ordered)} ({[p.layer_name for p in ordered]})"
        )
    return list(zip(names, ordered[: len(names)], strict=True))


def _plate_terminals(spec: DeviceSpec, layout, cell) -> list[Terminal]:
    """Terminals for plate capacitors, taken from the plate layers."""
    lm = layer_map()
    out: list[Terminal] = []
    for name, layer_name, edge in PLATE_TERMINALS[spec.kind]:
        ld = lm[layer_name]
        layer_index = layout.layer(ld[0], ld[1])
        region_box = None
        it = cell.begin_shapes_rec(layer_index)
        while not it.at_end():
            shape = it.shape()
            if not shape.is_text():
                box = it.dtrans() * shape.dbbox()
                region_box = box if region_box is None else region_box + box
            it.next()
        if region_box is None:
            raise RuntimeError(f"{spec.name}: no {layer_name} plate found")

        mid = region_box.center()
        if edge == "left":
            center, width = (region_box.left, mid.y), region_box.height()
        elif edge == "right":
            center, width = (region_box.right, mid.y), region_box.height()
        elif edge == "bottom":
            center, width = (mid.x, region_box.bottom), region_box.width()
        else:
            center, width = (mid.x, region_box.top), region_box.width()

        out.append(
            Terminal(
                name=name,
                layer=layer_name,
                center=(round(center[0], 6), round(center[1], 6)),
                width=round(width, 6),
                orientation=_EDGE_ORIENTATION[edge],
            )
        )
    return out
