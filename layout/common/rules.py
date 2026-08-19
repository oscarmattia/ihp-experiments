"""DRC rule values, read from the PDK rather than transcribed.

``rule_decks/sg13g2_tech_default.json`` is the table the DRC deck itself loads,
so reading it keeps our geometry constants and signoff in agreement. Every
numeric limit the layout code needs — minimum widths, spacings, the latch-up
distance, the poly-to-active clearance — comes from here.

Rule names follow the design-rule manual, with one wrinkle worth knowing:
Metal2 through Metal5 share a single generic set named ``Mn_*`` rather than
having per-layer entries, and the deck reports violations against them as
``M3.a`` and so on.
"""

from __future__ import annotations

import json
from functools import lru_cache

from layout.common.paths import pdk_paths


@lru_cache(maxsize=1)
def drc_rules() -> dict[str, float | str]:
    """The PDK's DRC rule values."""
    path = pdk_paths().klayout_tech / "drc" / "rule_decks" / "sg13g2_tech_default.json"
    data = json.loads(path.read_text())
    rules = data.get("drc_rules")
    if not isinstance(rules, dict):
        raise RuntimeError(f"{path} has no drc_rules table; PDK format may have changed")
    return rules


def rule(name: str) -> float:
    """One numeric rule value."""
    value = drc_rules().get(name)
    if not isinstance(value, (int, float)):
        raise KeyError(f"DRC rule {name!r} is missing or non-numeric ({value!r})")
    return float(value)


#: Rule name giving the minimum width of each routing metal. Metal2..Metal5 all
#: map to the generic Mn set.
_MIN_WIDTH_RULE = {
    "Metal1": "M1_a",
    "Metal2": "Mn_a",
    "Metal3": "Mn_a",
    "Metal4": "Mn_a",
    "Metal5": "Mn_a",
    "TopMetal1": "TM1_a",
    "TopMetal2": "TM2_a",
}

_MIN_SPACE_RULE = {
    "Metal1": "M1_b",
    "Metal2": "Mn_b",
    "Metal3": "Mn_b",
    "Metal4": "Mn_b",
    "Metal5": "Mn_b",
    "TopMetal1": "TM1_b",
    "TopMetal2": "TM2_b",
}


def min_width(metal: str) -> float:
    return rule(_MIN_WIDTH_RULE[metal])


def min_space(metal: str) -> float:
    return rule(_MIN_SPACE_RULE[metal])


def grid() -> float:
    return rule("grid")


#: Multiple of the minimum width used for routing. Drawing exactly at the limit
#: leaves no room for the grid snapping that a route's endpoints go through, so
#: routes are widened slightly.
WIDTH_MARGIN = 1.5


def route_width(metal: str) -> float:
    """Width to draw a route on ``metal``, snapped to the grid."""
    g = grid()
    wanted = min_width(metal) * WIDTH_MARGIN
    return round(round(wanted / g) * g, 6)


def route_widths() -> dict[str, float]:
    return {metal: route_width(metal) for metal in _MIN_WIDTH_RULE}
