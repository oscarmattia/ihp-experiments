"""Composition and electrical routing with gdsfactory.

Devices arrive as foundry PCells, which draw geometry but expose no ports, so
they are imported as gdsfactory components and annotated with the ports derived
in :mod:`layout.common.wrap`. From there gdsfactory's electrical routing does
the wiring.

Ports are placed on the *drawing* layer of the metal a pin sits on, not on the
pin layer itself. gdsfactory matches the route's cross-section layer against
the port layer, and routing happens on drawing layers.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from layout.common.devices import build
from layout.common.gds import write_gds
from layout.common.layers import ROUTING_METALS, layer_map
from layout.common.spec import DeviceSpec, Terminal
from layout.common.wrap import derive_terminals
from layout.common.xsection import activate_pdk, cross_section, gf_module

#: Pin or plate layer name -> drawing layer name for the same metal.
_PIN_TO_DRW: dict[str, str] = {}
for _metal in ROUTING_METALS:
    _PIN_TO_DRW[f"{_metal.lower()}_pin"] = f"{_metal.lower()}_drw"
    _PIN_TO_DRW[f"{_metal.lower()}_drw"] = f"{_metal.lower()}_drw"
_PIN_TO_DRW["gatpoly_pin"] = "gatpoly_drw"
_PIN_TO_DRW["activ_pin"] = "activ_drw"


def port_layer_for(pin_layer: str) -> tuple[int, int]:
    """Drawing layer a port on ``pin_layer`` should advertise."""
    lm = layer_map()
    return lm[_PIN_TO_DRW.get(pin_layer, pin_layer)]


def metal_of(pin_layer: str) -> str | None:
    """Routing metal a pin sits on, or None for poly/active pins."""
    for metal in ROUTING_METALS:
        if pin_layer.startswith(metal.lower()):
            return metal
    return None


def add_terminal_ports(component, terminals: list[Terminal]) -> None:
    """Attach electrical ports for ``terminals`` to a gdsfactory component."""
    for terminal in terminals:
        component.add_port(
            name=terminal.name,
            center=terminal.center,
            width=terminal.width,
            orientation=terminal.orientation,
            layer=port_layer_for(terminal.layer),
            port_type="electrical",
        )


def device_component(spec: DeviceSpec, gds_dir: Path | None = None):
    """Import a foundry PCell as a gdsfactory component with ports."""
    activate_pdk()
    gf = gf_module()

    layout, cell = build(spec)
    terminals = derive_terminals(spec, layout, cell)

    if gds_dir is not None:
        gds_dir = Path(gds_dir)
        gds_dir.mkdir(parents=True, exist_ok=True)
        gds_path = gds_dir / f"{spec.name}.gds"
        write_gds(layout, cell, gds_path, name=spec.name)
        component = gf.import_gds(gds_path)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            gds_path = Path(tmp) / f"{spec.name}.gds"
            write_gds(layout, cell, gds_path, name=spec.name)
            component = gf.import_gds(gds_path)

    add_terminal_ports(component, terminals)
    return component


def _check_same_metal(ports, metal: str) -> None:
    """Refuse to route a bundle whose ports are not on the routing metal.

    A metal change needs a via stack built to SG13G2 rules, which gdsfactory
    knows nothing about. Rather than let it invent a taper, layer transitions
    are made explicit by placing the PDK ``via_stack`` PCell, so every bundle
    stays on one metal.
    """
    target = layer_map()[f"{metal.lower()}_drw"]
    wrong = []
    for port in ports:
        layer_index = getattr(port, "layer", None)
        info = port.layer_info if hasattr(port, "layer_info") else None
        pair = (info.layer, info.datatype) if info is not None else layer_index
        if pair != target and info is not None:
            wrong.append(f"{port.name}@{info.layer}/{info.datatype}")
    if wrong:
        raise ValueError(
            f"cannot route on {metal} ({target[0]}/{target[1]}): "
            f"port(s) {', '.join(wrong)} are on another layer. "
            "Place a via_stack device to change metal, then route each side "
            "on its own metal."
        )


def route_electrical(
    component,
    ports1,
    ports2,
    metal: str = "Metal1",
    width: float | None = None,
    separation: float = 2.0,
    check_layers: bool = True,
    **kwargs,
):
    """Route a bundle between two port groups on ``metal``.

    Uses ``route_bundle_electrical``, which corners with wire bends rather than
    the curved bends used for photonics. ``auto_taper`` is off: tapering only
    applies between registered routing layers, and here a layer change means a
    via stack instead.
    """
    activate_pdk()
    gf = gf_module()
    if check_layers:
        _check_same_metal(list(ports1) + list(ports2), metal)
    kwargs.setdefault("auto_taper", False)
    return gf.routing.route_bundle_electrical(
        component,
        ports1,
        ports2,
        cross_section=cross_section(metal, width=width),
        separation=separation,
        **kwargs,
    )
