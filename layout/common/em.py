"""Electromigration limits, read from the PDK's technology LEF.

``libs.ref/sg13g2_stdcell/lef/sg13g2_tech.lef`` is the only machine-readable
source of current limits in this PDK: the DRC decks, the ITF and the Magic tech
files carry none. That has two consequences worth stating plainly.

First, there is **no signoff check** for electromigration here. The limits exist
as LEF metadata for tools that consume it, so anything drawn in this repo has to
be checked by us — see :func:`check_segments`.

Second, ``Cont`` (the poly/active contact) has resistance but **no**
``DCCURRENTDENSITY`` entry, so the current a MOS source/drain contact can carry
is not derivable from open PDK data. :data:`UNCHECKABLE` records that rather than
letting an unstated assumption pass for a verified one.

Routing-layer values are given in mA per um of wire width; cut layers in mA per
cut.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from layout.common.paths import pdk_paths

#: Layers whose current limit the PDK does not state, with the reason.
UNCHECKABLE: dict[str, str] = {
    "Cont": (
        "the technology LEF gives Cont a resistance but no DCCURRENTDENSITY, so "
        "MOS source/drain contact electromigration cannot be checked from open "
        "PDK data. The resistor PCells' ikspec (0.11-0.30 mA per contact) is the "
        "closest analogue but is not stated for MOS contacts."
    ),
}

#: Safety factor applied to the LEF limit when sizing. 1.0 draws exactly at the
#: limit; sizing at 2x leaves room for the current being an operating-point
#: estimate rather than a worst-case corner.
DEFAULT_DERATE = 2.0

_LAYER_RE = re.compile(r"^\s*LAYER\s+(\S+)\s*$", re.M)
_END_RE = re.compile(r"^\s*END\s+(\S+)\s*$", re.M)
_TYPE_RE = re.compile(r"^\s*TYPE\s+(\S+)\s*;", re.M)
_DC_RE = re.compile(r"^\s*DCCURRENTDENSITY\s+AVERAGE\s+([0-9.eE+-]+)\s*;", re.M)
_THICK_RE = re.compile(r"^\s*THICKNESS\s+([0-9.eE+-]+)\s*;", re.M)
_RES_RE = re.compile(r"^\s*RESISTANCE\s+(?:RPERSQ\s+)?([0-9.eE+-]+)\s*;", re.M)


@dataclass(frozen=True)
class LayerEm:
    """Current-carrying data for one layer."""

    name: str
    #: ROUTING or CUT.
    kind: str
    #: mA/um for routing layers, mA/cut for cut layers. None when unstated.
    dc_current_density: float | None
    thickness_um: float | None = None
    resistance: float | None = None

    @property
    def per_width(self) -> bool:
        return self.kind.upper() == "ROUTING"


def tech_lef() -> object:
    paths = pdk_paths()
    return paths.root / paths.pdk / "libs.ref" / "sg13g2_stdcell" / "lef" / "sg13g2_tech.lef"


@lru_cache(maxsize=1)
def layers() -> dict[str, LayerEm]:
    """Parse every LAYER block in the technology LEF."""
    path = tech_lef()
    if not path.is_file():  # type: ignore[attr-defined]
        raise FileNotFoundError(f"technology LEF not found at {path}")
    text = path.read_text()  # type: ignore[attr-defined]

    out: dict[str, LayerEm] = {}
    for match in _LAYER_RE.finditer(text):
        name = match.group(1)
        end = _END_RE.search(text, match.end())
        block = text[match.end() : end.start() if end else len(text)]
        kind_m = _TYPE_RE.search(block)
        dc_m = _DC_RE.search(block)
        th_m = _THICK_RE.search(block)
        res_m = _RES_RE.search(block)
        out[name] = LayerEm(
            name=name,
            kind=kind_m.group(1) if kind_m else "",
            dc_current_density=float(dc_m.group(1)) if dc_m else None,
            thickness_um=float(th_m.group(1)) if th_m else None,
            resistance=float(res_m.group(1)) if res_m else None,
        )
    if not out:
        raise RuntimeError(f"no LAYER blocks parsed from {path}; LEF format may have changed")
    return out


def limit(layer: str) -> LayerEm:
    try:
        return layers()[layer]
    except KeyError as exc:
        raise KeyError(
            f"{layer!r} is not in the technology LEF; known: {sorted(layers())}"
        ) from exc


def max_current_a(layer: str, width_um: float = 1.0, cuts: int = 1) -> float:
    """Current a layer can carry, in amps.

    For a routing layer this scales with ``width_um``; for a cut layer with
    ``cuts``. Raises when the PDK states no limit, so an unchecked layer cannot
    be mistaken for an unlimited one.
    """
    info = limit(layer)
    if info.dc_current_density is None:
        reason = UNCHECKABLE.get(layer, "the technology LEF states no DCCURRENTDENSITY")
        raise ValueError(f"no EM limit available for {layer}: {reason}")
    scale = width_um if info.per_width else cuts
    return info.dc_current_density * 1e-3 * scale


def width_for_a(layer: str, current_a: float, derate: float = DEFAULT_DERATE) -> float:
    """Minimum width in um for a routing layer to carry ``current_a``."""
    info = limit(layer)
    if not info.per_width:
        raise ValueError(f"{layer} is a cut layer; use cuts_for_a()")
    if info.dc_current_density is None:
        raise ValueError(f"no EM limit available for {layer}")
    return current_a * derate / (info.dc_current_density * 1e-3)


def cuts_for_a(layer: str, current_a: float, derate: float = DEFAULT_DERATE) -> int:
    """Minimum number of cuts for a via layer to carry ``current_a``."""
    import math

    info = limit(layer)
    if info.per_width:
        raise ValueError(f"{layer} is a routing layer; use width_for_a()")
    if info.dc_current_density is None:
        raise ValueError(f"no EM limit available for {layer}")
    return max(1, math.ceil(current_a * derate / (info.dc_current_density * 1e-3)))


@dataclass
class Segment:
    """A drawn conductor with the current it has to carry."""

    net: str
    layer: str
    #: um for routing layers.
    width_um: float = 0.0
    #: number of cuts for via layers.
    cuts: int = 0
    current_a: float = 0.0
    note: str = ""

    def evaluate(self, derate: float = 1.0) -> dict:
        """Compare the drawn geometry against the LEF limit.

        ``derate`` here is the margin the *check* insists on, separate from the
        margin used when sizing: geometry is sized at
        :data:`DEFAULT_DERATE` but only has to clear 1.0x to be legal.
        """
        info = limit(self.layer)
        entry: dict = {
            "net": self.net,
            "layer": self.layer,
            "kind": info.kind,
            "current_a": self.current_a,
            "note": self.note,
        }
        if info.dc_current_density is None:
            entry.update(
                {
                    "ok": None,
                    "checkable": False,
                    "reason": UNCHECKABLE.get(
                        self.layer, "no DCCURRENTDENSITY in the technology LEF"
                    ),
                }
            )
            return entry

        if info.per_width:
            capacity = max_current_a(self.layer, width_um=self.width_um)
            entry["width_um"] = self.width_um
            entry["min_width_um"] = round(width_for_a(self.layer, self.current_a, derate=1.0), 4)
        else:
            capacity = max_current_a(self.layer, cuts=self.cuts)
            entry["cuts"] = self.cuts
            entry["min_cuts"] = cuts_for_a(self.layer, self.current_a, derate=1.0)

        entry["capacity_a"] = capacity
        entry["utilisation"] = round(self.current_a / capacity, 4) if capacity else None
        entry["checkable"] = True
        entry["ok"] = capacity >= self.current_a * derate
        return entry


def check_segments(segments: list[Segment], derate: float = 1.0) -> dict:
    """Evaluate a list of segments; returns a JSON-ready summary."""
    rows = [segment.evaluate(derate=derate) for segment in segments]
    failures = [r for r in rows if r.get("checkable") and not r.get("ok")]
    unchecked = [r for r in rows if not r.get("checkable")]
    return {
        "ok": not failures,
        "source": str(tech_lef()),
        "derate_required": derate,
        "derate_used_for_sizing": DEFAULT_DERATE,
        "segments": rows,
        "failures": [f"{r['net']} on {r['layer']}" for r in failures],
        "unchecked_layers": sorted({r["layer"] for r in unchecked}),
        "uncheckable_reasons": {
            layer: UNCHECKABLE[layer] for layer in {r["layer"] for r in unchecked}
            if layer in UNCHECKABLE
        },
    }
