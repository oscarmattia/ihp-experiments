"""SG13G2 layer map, parsed from the PDK's own DRC layer definitions.

``rule_decks/layers_def.drc`` is what the DRC deck itself uses, so parsing it
keeps our layer numbers in lockstep with signoff instead of duplicating a
289-entry table that would silently rot when the PDK moves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from layout.common.paths import pdk_paths

# name = get_polygons(<layer>, <datatype>)   /   name = labels(<layer>, <datatype>)
_POLY_RE = re.compile(r"^(\w+)\s*=\s*get_polygons\(\s*(\d+)\s*,\s*(\d+)\s*\)", re.M)
_TEXT_RE = re.compile(r"^(\w+)\s*=\s*labels\(\s*(\d+)\s*,\s*(\d+)\s*\)", re.M)

#: Text layer that PCells use for device and terminal annotations.
TEXT_DRW = (63, 0)

#: Pin layers a PCell may use to mark its terminals, in the order we prefer
#: them when a device exposes more than one. Metal pins come first because
#: routing attaches to metal.
PIN_LAYERS: tuple[tuple[int, int], ...] = (
    (8, 2),    # metal1_pin
    (10, 2),   # metal2_pin
    (30, 2),   # metal3_pin
    (50, 2),   # metal4_pin
    (67, 2),   # metal5_pin
    (126, 2),  # topmetal1_pin
    (134, 2),  # topmetal2_pin
    (5, 2),    # gatpoly_pin
    (1, 2),    # activ_pin
)

#: Routing metals, bottom to top, with the drawing layer and the LVS text
#: layer that carries net names for that metal.
ROUTING_METALS: dict[str, dict[str, object]] = {
    "Metal1": {"drw": (8, 0), "pin": (8, 2), "text": (8, 25), "width": 0.16},
    "Metal2": {"drw": (10, 0), "pin": (10, 2), "text": (10, 25), "width": 0.20},
    "Metal3": {"drw": (30, 0), "pin": (30, 2), "text": (30, 25), "width": 0.20},
    "Metal4": {"drw": (50, 0), "pin": (50, 2), "text": (50, 25), "width": 0.20},
    "Metal5": {"drw": (67, 0), "pin": (67, 2), "text": (67, 25), "width": 0.20},
    "TopMetal1": {"drw": (126, 0), "pin": (126, 2), "text": (126, 25), "width": 1.50},
    "TopMetal2": {"drw": (134, 0), "pin": (134, 2), "text": (134, 25), "width": 2.00},
}

#: Via layers between consecutive routing metals.
VIA_STACK: tuple[tuple[str, tuple[int, int], str], ...] = (
    ("Metal1", (19, 0), "Metal2"),    # Via1
    ("Metal2", (29, 0), "Metal3"),    # Via2
    ("Metal3", (49, 0), "Metal4"),    # Via3
    ("Metal4", (66, 0), "Metal5"),    # Via4
    ("Metal5", (125, 0), "TopMetal1"),  # TopVia1
    ("TopMetal1", (133, 0), "TopMetal2"),  # TopVia2
)


@dataclass(frozen=True)
class LayerMap:
    """Layer/datatype pairs keyed by the PDK's own layer names."""

    polygons: dict[str, tuple[int, int]]
    texts: dict[str, tuple[int, int]]

    def __getitem__(self, name: str) -> tuple[int, int]:
        if name in self.polygons:
            return self.polygons[name]
        if name in self.texts:
            return self.texts[name]
        raise KeyError(f"Unknown SG13G2 layer: {name}")

    def get(self, name: str, default=None):
        try:
            return self[name]
        except KeyError:
            return default

    def name_for(self, layer: int, datatype: int) -> str | None:
        """Reverse lookup, for turning shapes back into readable names."""
        target = (layer, datatype)
        for name, ld in self.polygons.items():
            if ld == target:
                return name
        for name, ld in self.texts.items():
            if ld == target:
                return name
        return None

    def text_layer_for_metal(self, metal: str) -> tuple[int, int]:
        """LVS text layer for a routing metal (net names live here)."""
        spec = ROUTING_METALS.get(metal)
        if spec is None:
            raise KeyError(f"{metal} is not a routing metal")
        return spec["text"]  # type: ignore[return-value]


@lru_cache(maxsize=1)
def layer_map() -> LayerMap:
    """Parse the PDK DRC layer definitions into a LayerMap."""
    text = pdk_paths().drc_layers_def.read_text()
    polygons = {m.group(1): (int(m.group(2)), int(m.group(3))) for m in _POLY_RE.finditer(text)}
    texts = {m.group(1): (int(m.group(2)), int(m.group(3))) for m in _TEXT_RE.finditer(text)}
    if not polygons:
        raise RuntimeError(
            f"No layers parsed from {pdk_paths().drc_layers_def}; PDK format may have changed"
        )
    return LayerMap(polygons=polygons, texts=texts)
