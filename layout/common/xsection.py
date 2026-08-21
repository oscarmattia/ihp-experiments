"""gdsfactory cross-sections and PDK registration for the SG13G2 metal stack.

Layer numbers come from :mod:`layout.common.layers`, which parses the PDK's own
DRC layer definitions, so there is no second copy of the stack to keep in sync.

Only routing is defined here. Devices are foundry PCells, so gdsfactory needs
no device cells — just enough technology to build wires and vias between ports.
"""

from __future__ import annotations

from functools import lru_cache, partial

from layout.common.layers import ROUTING_METALS
from layout.common.rules import route_widths

#: Width to draw a route on each metal, derived from the PDK's own DRC rule
#: values rather than transcribed. A hand-written table had TopMetal1 at 1.50 um
#: against a 1.64 um minimum, which the deck caught only once a route used it.
ROUTE_WIDTHS: dict[str, float] = route_widths()


@lru_cache(maxsize=1)
def gf_module():
    import gdsfactory as gf  # noqa: PLC0415

    return gf


@lru_cache(maxsize=1)
def layer_enum():
    """A gdsfactory ``LayerMap`` for the routing stack.

    gdsfactory needs a real ``LayerMap`` — several of its calls assert
    ``pdk.layers is not None`` — and ``LayerMap`` is an ``aenum`` enum whose
    members must exist at class-definition time, so the routing layers are
    written out here. ``layers.validate_routing_layers()`` checks these values
    against the PDK's own definitions so the duplication cannot drift silently.
    Only routing layers are listed: devices come from PCells, so gdsfactory
    never needs the other ~280.
    """
    from gdsfactory.technology import LayerMap as GfLayerMap  # noqa: PLC0415

    class SG13G2RoutingLayers(GfLayerMap):
        METAL1 = (8, 0)
        METAL1_PIN = (8, 2)
        METAL1_TEXT = (8, 25)
        METAL2 = (10, 0)
        METAL2_PIN = (10, 2)
        METAL2_TEXT = (10, 25)
        METAL3 = (30, 0)
        METAL3_PIN = (30, 2)
        METAL3_TEXT = (30, 25)
        METAL4 = (50, 0)
        METAL4_PIN = (50, 2)
        METAL4_TEXT = (50, 25)
        METAL5 = (67, 0)
        METAL5_PIN = (67, 2)
        METAL5_TEXT = (67, 25)
        TOPMETAL1 = (126, 0)
        TOPMETAL1_PIN = (126, 2)
        TOPMETAL1_TEXT = (126, 25)
        TOPMETAL2 = (134, 0)
        TOPMETAL2_PIN = (134, 2)
        TOPMETAL2_TEXT = (134, 25)
        VIA1 = (19, 0)
        VIA2 = (29, 0)
        VIA3 = (49, 0)
        VIA4 = (66, 0)
        TOPVIA1 = (125, 0)
        TOPVIA2 = (133, 0)
        # Poly and active appear as port layers on MOS gates and taps, so
        # gdsfactory has to be able to name them even though we never route on
        # them.
        GATPOLY = (5, 0)
        ACTIV = (1, 0)

    return SG13G2RoutingLayers


def cross_section(metal: str, width: float | None = None):
    """An electrical cross-section on ``metal``."""
    if metal not in ROUTING_METALS:
        raise KeyError(f"{metal} is not a routing metal; known: {sorted(ROUTING_METALS)}")
    gf = gf_module()
    return gf.cross_section.cross_section(
        width=width if width is not None else ROUTE_WIDTHS[metal],
        layer=ROUTING_METALS[metal]["drw"],  # type: ignore[arg-type]
        port_names=gf.cross_section.port_names_electrical,
        port_types=gf.cross_section.port_types_electrical,
        radius=None,
    )


def cross_sections() -> dict[str, object]:
    """Cross-section factories for every routing metal, plus a default alias.

    The PDK registry wants callables, not CrossSection instances; kfactory
    invokes them and a bare instance raises "'CrossSection' object is not
    callable".
    """
    out: dict[str, object] = {}
    for metal in ROUTING_METALS:
        out[metal.lower()] = partial(cross_section, metal)
    # gdsfactory's electrical routing helpers default to a cross-section named
    # "metal_routing"; point it at TopMetal2, the thick RF metal used for the
    # differential signal pairs.
    out["metal_routing"] = partial(cross_section, "TopMetal2")
    return out


@lru_cache(maxsize=1)
def activate_pdk():
    """Register an SG13G2 routing PDK with gdsfactory and activate it.

    Must run before any other gdsfactory call. gdsfactory resolves its default
    PDK from the ``PDK`` environment variable, which ``env.sh`` sets to
    ``ihp-sg13g2`` for the SPICE flow; that is not an importable Python module,
    so ``get_active_pdk()`` raises until something activates a PDK explicitly.
    """
    gf = gf_module()
    # route_bundle_electrical corners with "wire_corner" and fills with
    # "straight", both resolved by name through the active PDK, so they have to
    # be registered. Both are layer-agnostic and take their geometry from the
    # cross-section, so the generic implementations are correct here.
    pdk = gf.Pdk(
        name="sg13g2_routing",
        layers=layer_enum(),
        cross_sections=cross_sections(),  # type: ignore[arg-type]
        cells={
            "wire_corner": gf.components.wire_corner,
            "straight": gf.components.straight,
            "taper": gf.components.taper,
        },
    )
    pdk.activate()
    return pdk
