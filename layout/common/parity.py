"""Parity between the simulation netlist and the layout netlist.

The cell that gets laid out and the cell that gets simulated have to be the same
cell. Keeping that true by review does not work: the layout CDL was missing the
bias diode for a while and nobody noticed, because LVS only compares the layout
against the CDL, never the CDL against the schematic.

This module closes that loop. It parses the SPICE subcircuit that the sizing flow
simulates and the CDL that the layout generator writes, reduces both to a set of
devices keyed by model and geometry, and reports the difference.

One substitution is permitted and is spelled out in :data:`MODEL_ALIASES`:
``ind_shunt`` is an EM-fitted lumped subcircuit standing in for the ``inductor``
PCell, because the PDK has no ngspice inductor model. Anything else that differs
is a divergence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

#: Simulation model name -> layout model name, for elements that cannot be the
#: same in both views. The value is the model the LVS deck extracts.
MODEL_ALIASES: dict[str, str] = {
    "ind_shunt": "inductor",
    "ind_shunt_drv": "inductor",
}

#: Where to recover geometry for an aliased model whose simulation form carries
#: none. A lumped EM model is a network of R, L and C with no width or diameter on
#: the element line, so without this the check could only compare that an inductor
#: exists — not that it is the coil the model was fitted to. The generator writes
#: the case it solved into the include's header, so that is the thing to read.
ALIAS_GEOMETRY_INCLUDE: dict[str, str] = {
    "ind_shunt": "ind_shunt.inc",
    "ind_shunt_drv": "ind_shunt_drv.inc",
}

_IND_HEADER = re.compile(
    r"nr_r=(?P<nr_r>[\d.]+)\s+D=(?P<d>[\d.]+)um\s+w=(?P<w>[\d.]+)um\s+s=(?P<s>[\d.]+)um",
    re.I,
)


def _alias_geometry(model: str, spice_file: Path) -> dict[str, float]:
    """Geometry for an aliased model, read from the include that generated it."""
    include = ALIAS_GEOMETRY_INCLUDE.get(model)
    if not include:
        return {}
    path = Path(spice_file).parent / include
    if not path.is_file():
        return {}
    match = _IND_HEADER.search(path.read_text())
    if not match:
        return {}
    return {
        "w": float(match.group("w")) * 1e-6,
        "s": float(match.group("s")) * 1e-6,
        "d": float(match.group("d")) * 1e-6,
    }

#: Geometry compared per model. Anything not listed is ignored, either because it
#: is not a size (rfmode, feed) or because the two views legitimately express it
#: differently (an EM model carries no w/l).
COMPARED_PARAMS: dict[str, tuple[str, ...]] = {
    "sg13_lv_nmos": ("w", "l"),
    "rppd": ("w", "l"),
    "rsil": ("w", "l"),
    "cap_cmomi": ("w", "l"),
    "cap_cmim": ("w", "l"),
    "npn13G2": ("Nx",),
    "inductor": ("w", "s", "d"),
}

#: Fractional tolerance when comparing geometry.
#:
#: This has to be tighter than the LVS deck's, or the check waves through
#: differences LVS will reject. The deck compares MOS w and l essentially exactly —
#: 242.988 um against a drawn 243.000 um fails, and those are only 4.9e-5 apart, so
#: a tolerance of 1e-4 misses it. What is left to absorb here is representation
#: rather than physics, 12.4252u against 1.24252e-05, which needs far less.
PARAM_TOL = 1e-6

_SI = {
    "t": 1e12, "g": 1e9, "meg": 1e6, "k": 1e3,
    "m": 1e-3, "u": 1e-6, "µ": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15, "a": 1e-18,
}
_NUM = re.compile(r"^([+-]?\d*\.?\d+(?:[eE][+-]?\d+)?)\s*([a-zA-Zµ]*)$")


def parse_si(text: str) -> float | None:
    match = _NUM.match(text.strip())
    if not match:
        return None
    value = float(match.group(1))
    suffix = match.group(2).lower()
    if not suffix:
        return value
    if suffix in _SI:
        return value * _SI[suffix]
    if suffix[0] in _SI:
        return value * _SI[suffix[0]]
    return value


@dataclass(frozen=True)
class Device:
    """One device instance, reduced to what parity cares about."""

    model: str
    params: tuple[tuple[str, float], ...]

    def key(self) -> str:
        if not self.params:
            return self.model
        inner = " ".join(f"{k}={v:.6g}" for k, v in self.params)
        return f"{self.model} {inner}"


@dataclass
class ParityResult:
    subckt: str
    spice_file: str
    cdl_file: str
    spice_ports: list[str] = field(default_factory=list)
    cdl_ports: list[str] = field(default_factory=list)
    only_in_spice: list[str] = field(default_factory=list)
    only_in_cdl: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    sources_in_spice: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ports_match(self) -> bool:
        return self.spice_ports == self.cdl_ports

    @property
    def devices_match(self) -> bool:
        return not self.only_in_spice and not self.only_in_cdl

    @property
    def ok(self) -> bool:
        return (
            self.ports_match
            and self.devices_match
            and not self.sources_in_spice
            and not self.error
        )

    def to_dict(self) -> dict:
        return {
            "subckt": self.subckt,
            "spice_file": self.spice_file,
            "cdl_file": self.cdl_file,
            "ok": self.ok,
            "ports_match": self.ports_match,
            "devices_match": self.devices_match,
            "spice_ports": self.spice_ports,
            "cdl_ports": self.cdl_ports,
            "matched_devices": sorted(self.matched),
            "only_in_spice": sorted(self.only_in_spice),
            "only_in_cdl": sorted(self.only_in_cdl),
            "independent_sources_in_spice": self.sources_in_spice,
            "model_aliases": MODEL_ALIASES,
            "notes": self.notes,
            "error": self.error,
        }

    def write(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        return path

    def summary(self) -> str:
        if self.ok:
            return f"{len(self.matched)} device(s) match, ports {' '.join(self.cdl_ports)}"
        bits = []
        if self.error:
            bits.append(self.error)
        if not self.ports_match:
            bits.append(
                f"ports differ: schematic [{' '.join(self.spice_ports)}] "
                f"vs layout [{' '.join(self.cdl_ports)}]"
            )
        if self.sources_in_spice:
            bits.append(
                "independent source(s) still inside the schematic subcircuit: "
                + ", ".join(self.sources_in_spice)
            )
        if self.only_in_spice:
            bits.append("only in schematic: " + "; ".join(sorted(self.only_in_spice)))
        if self.only_in_cdl:
            bits.append("only in layout: " + "; ".join(sorted(self.only_in_cdl)))
        return " | ".join(bits)


def _params_of(model: str, tokens: list[str]) -> tuple[tuple[str, float], ...]:
    wanted = COMPARED_PARAMS.get(model)
    if not wanted:
        return ()
    wanted_lower = {name.lower() for name in wanted}
    found: dict[str, float] = {}
    for token in tokens:
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        key = key.strip().lower()
        if key in wanted_lower:
            parsed = parse_si(value)
            if parsed is not None:
                found[key] = parsed
    return tuple(sorted(found.items()))


def _subckt_body(text: str, name: str) -> tuple[list[str], list[str]]:
    """Return ``(ports, element_lines)`` for a named subcircuit."""
    lines = text.splitlines()
    ports: list[str] = []
    body: list[str] = []
    inside = False
    for raw in lines:
        line = raw.strip()
        if not inside:
            match = re.match(rf"\.subckt\s+{re.escape(name)}\b(.*)", line, re.I)
            if match:
                ports = match.group(1).split()
                inside = True
            continue
        if re.match(r"\.ends\b", line, re.I):
            break
        if not line or line.startswith("*"):
            continue
        body.append(line)
    if not inside:
        raise ValueError(f"subcircuit {name!r} not found")
    return ports, body


def _devices_from_spice(
    body: list[str], spice_file: Path | None = None
) -> tuple[list[Device], list[str]]:
    """Devices and any independent sources found in a SPICE subcircuit body."""
    devices: list[Device] = []
    sources: list[str] = []
    for line in body:
        tokens = line.split()
        if not tokens:
            continue
        name = tokens[0]
        prefix = name[0].upper()
        if prefix in ("V", "I"):
            sources.append(name)
            continue
        if prefix == "X":
            # X<name> n1 n2 ... <model> [params]; the model is the last token that
            # is not a key=value assignment.
            model = None
            for token in reversed(tokens[1:]):
                if "=" not in token:
                    model = token
                    break
            if model is None:
                continue
            raw_model = model
            model = MODEL_ALIASES.get(model, model)
            params = _params_of(model, tokens)
            if not params and raw_model != model and spice_file is not None:
                geometry = _alias_geometry(raw_model, spice_file)
                wanted = COMPARED_PARAMS.get(model, ())
                params = tuple(
                    sorted((k, v) for k, v in geometry.items() if k in wanted)
                )
            devices.append(Device(model, params))
        elif prefix in ("R", "C", "L"):
            # Ideal element: not a PDK device, so it has no layout counterpart.
            # The ideal netlist is not the layout reference, so treat it as a
            # device named after its type so the difference is visible.
            devices.append(Device(f"ideal_{prefix.lower()}", ()))
    return devices, sources


def _devices_from_cdl(body: list[str]) -> list[Device]:
    devices: list[Device] = []
    for line in body:
        tokens = line.split()
        if not tokens:
            continue
        prefix = tokens[0][0].upper()
        if prefix in ("V", "I"):
            continue
        model = None
        for token in reversed(tokens[1:]):
            if "=" not in token:
                model = token
                break
        if model is None:
            continue
        model = MODEL_ALIASES.get(model, model)
        devices.append(Device(model, _params_of(model, tokens)))
    return devices


def _multiset_diff(a: list[Device], b: list[Device]) -> tuple[list[str], list[str], list[str]]:
    """Compare two device lists as multisets, with a tolerance on geometry."""
    remaining = list(b)
    matched: list[str] = []
    only_a: list[str] = []
    for device in a:
        hit = None
        for index, candidate in enumerate(remaining):
            if candidate.model != device.model:
                continue
            if len(candidate.params) != len(device.params):
                continue
            same = True
            for (ka, va), (kb, vb) in zip(device.params, candidate.params, strict=False):
                if ka != kb:
                    same = False
                    break
                scale = max(abs(va), abs(vb)) or 1.0
                if abs(va - vb) / scale > PARAM_TOL:
                    same = False
                    break
            if same:
                hit = index
                break
        if hit is None:
            only_a.append(device.key())
        else:
            matched.append(device.key())
            remaining.pop(hit)
    return matched, only_a, [d.key() for d in remaining]


def check_parity(
    spice_file: Path,
    cdl_file: Path,
    subckt: str,
    params: dict[str, float] | None = None,
    cdl_subckt: str | None = None,
) -> ParityResult:
    """Compare a simulation subcircuit against a layout CDL subcircuit.

    ``cdl_subckt`` defaults to ``subckt``: the layout cell should carry the same
    name as the schematic cell, since they are meant to be the same cell. It is
    only separate so a mismatch is reportable rather than a crash.
    """
    from layout.common.sizing import read_params

    spice_file = Path(spice_file)
    cdl_file = Path(cdl_file)
    result = ParityResult(subckt=subckt, spice_file=str(spice_file), cdl_file=str(cdl_file))
    if cdl_subckt and cdl_subckt != subckt:
        result.notes.append(
            f"layout cell is named {cdl_subckt!r} but the schematic cell is "
            f"{subckt!r}; they should share one name"
        )

    if not spice_file.is_file():
        result.error = f"missing schematic netlist {spice_file}"
        return result
    if not cdl_file.is_file():
        result.error = f"missing layout CDL {cdl_file}"
        return result

    values = params or read_params()
    text = spice_file.read_text()
    # Resolve {TOKEN} parameters so geometry can be compared numerically.
    for key, value in values.items():
        text = text.replace(f"{{{key}}}", repr(value))

    try:
        spice_ports, spice_body = _subckt_body(text, subckt)
        cdl_ports, cdl_body = _subckt_body(cdl_file.read_text(), cdl_subckt or subckt)
    except ValueError as exc:
        result.error = str(exc)
        return result

    result.spice_ports = spice_ports
    result.cdl_ports = cdl_ports

    spice_devices, sources = _devices_from_spice(spice_body, spice_file)
    result.sources_in_spice = sources
    cdl_devices = _devices_from_cdl(cdl_body)

    matched, only_spice, only_cdl = _multiset_diff(spice_devices, cdl_devices)
    result.matched = matched
    result.only_in_spice = only_spice
    result.only_in_cdl = only_cdl

    if MODEL_ALIASES:
        result.notes.append(
            "permitted substitution(s): "
            + ", ".join(f"{k} -> {v}" for k, v in MODEL_ALIASES.items())
            + "; geometry for these is read from "
            + ", ".join(ALIAS_GEOMETRY_INCLUDE.values())
            + " rather than from the element line, and is still compared"
        )
    return result
