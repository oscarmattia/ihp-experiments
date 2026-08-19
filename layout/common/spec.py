"""The single source of truth for a device instance.

A :class:`DeviceSpec` is written once and then drives three views that must
agree for LVS to pass:

1. the foundry PCell parameters that build the layout,
2. the CDL element line that the PDK LVS deck compares against,
3. the port names that gdsfactory routes to.

Keeping them derived from one object is what stops layout and netlist from
drifting apart silently.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Terminal:
    """One device terminal as it appears in layout and netlist."""

    name: str
    #: PDK layer name the port sits on, e.g. ``metal1_pin``.
    layer: str
    #: Where the port sits, in um, relative to the cell origin.
    center: tuple[float, float]
    #: Port width in um (the pin extent across the routing direction).
    width: float
    #: Outward direction in degrees; gdsfactory routes away from the device.
    orientation: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DeviceSpec:
    """A named, sized device instance."""

    #: Cell name used for GDS, DRC/LVS runs and artifact filenames.
    name: str
    #: Key into ``layout.common.devices.DEVICE_KINDS``.
    kind: str
    #: Engineering parameters in SI base units (metres, farads, ohms).
    params: dict[str, Any] = field(default_factory=dict)
    #: Free-form note recorded in the manifest, e.g. where the size came from.
    note: str = ""

    def with_name(self, name: str) -> DeviceSpec:
        return DeviceSpec(name=name, kind=self.kind, params=dict(self.params), note=self.note)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "params": self.params, "note": self.note}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> DeviceSpec:
        return DeviceSpec(
            name=data["name"],
            kind=data["kind"],
            params=dict(data.get("params", {})),
            note=data.get("note", ""),
        )


def write_specs(specs: list[DeviceSpec], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([s.to_dict() for s in specs], indent=2) + "\n")


def read_specs(path: Path) -> list[DeviceSpec]:
    return [DeviceSpec.from_dict(d) for d in json.loads(Path(path).read_text())]
