"""Parasitic extraction: Magic for R and C, klayout.pex as an R cross-check.

Two backends, because neither is sufficient alone:

* **Magic** extracts both resistance and capacitance using the PDK's own
  ``ihp-sg13g2-extract.tech``, and writes an ngspice-ready netlist. This is the
  one that feeds post-layout simulation.
* **klayout.pex** is resistance only. Its ``RExtractor`` works on a single
  polygon with ports rather than a whole layout, which makes it a good
  independent check on a specific routed wire: same geometry, different
  algorithm, different sheet-resistance source.

Sheet resistances come from ``docs/PDK.md``, which records them from
``parasitics/itf/sg13g2_typ.itf`` and ``res_extraction.lvs``. Note Metal1 is
0.110 ohm/sq while Metal2-Metal5 are 0.088; using one value for all thin metals
is a real error.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from layout.common.layers import ROUTING_METALS, layer_map
from layout.common.paths import pdk_paths

#: Sheet resistance in ohm/square per routing metal, from docs/PDK.md.
SHEET_RESISTANCE: dict[str, float] = {
    "Metal1": 0.110,
    "Metal2": 0.088,
    "Metal3": 0.088,
    "Metal4": 0.088,
    "Metal5": 0.088,
    "TopMetal1": 0.018,
    "TopMetal2": 0.011,
}

#: Via resistance in ohm per cut, from the ITF via definitions.
VIA_RESISTANCE: dict[str, float] = {
    "Via1": 2.2,
    "Via2": 2.2,
    "Via3": 2.2,
    "Via4": 2.2,
    "TopVia1": 2.2,
    "TopVia2": 1.1,
}

_R_LINE = re.compile(r"^R(\S+)\s+(\S+)\s+(\S+)\s+([0-9.eE+-]+)", re.M)

#: Magic names extracted nodes by the layer they sit on: ``w_...`` for well and
#: ``a_...`` for area/metal nodes. Resistors touching a well or substrate node
#: are the bulk path, not signal interconnect, and they dominate the total by
#: orders of magnitude — a single rppd contributes a few kilo-ohms of well while
#: its terminals contribute a couple of ohms of metal.
_BULK_PREFIXES = ("w_", "sub", "well", "nwell", "pwell")
_BULK_NODES = {"0", "gnd", "sub!"}


def is_bulk_node(node: str) -> bool:
    name = node.lower()
    return name in _BULK_NODES or name.startswith(_BULK_PREFIXES)


def signal_resistors(elements: list[dict]) -> list[dict]:
    """Resistors on signal nets, i.e. excluding the well/substrate path."""
    return [e for e in elements if not (is_bulk_node(e["a"]) or is_bulk_node(e["b"]))]
_C_LINE = re.compile(r"^C(\S+)\s+(\S+)\s+(\S+)\s+([0-9.eE+-]+)(\w*)", re.M)

_SI = {"m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15, "a": 1e-18, "k": 1e3}


def _si(value: str, suffix: str = "") -> float:
    scale = _SI.get(suffix[:1].lower(), 1.0) if suffix else 1.0
    return float(value) * scale


@dataclass
class PexResult:
    """Outcome of a Magic extraction."""

    cell: str
    gds: str
    netlist: str = ""
    resistors: int = 0
    capacitors: int = 0
    total_resistance: float = 0.0
    total_capacitance: float = 0.0
    per_net_capacitance: dict[str, float] = field(default_factory=dict)
    resistor_elements: list[dict] = field(default_factory=list)
    resistance_extracted: bool = True
    #: Capacitors the extractor reported with a negative value, if any.
    negative_capacitors: list[dict] = field(default_factory=list)
    note: str = ""
    log: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.netlist) and not self.error

    @property
    def physical(self) -> bool:
        """Whether the result can be simulated as it stands.

        A negative capacitance is not a small error to tolerate: ngspice accepts it
        without complaint and it silently moves the AC result, so it has to be a
        gate rather than something a reader is expected to spot. Hierarchical Magic
        extraction produced nine of them on the CTLE stage, worst -85 fF; see
        `_magic_script` for why extraction is flattened.
        """
        return self.ok and not self.negative_capacitors

    def to_dict(self) -> dict:
        return {
            "cell": self.cell,
            "gds": self.gds,
            "netlist": self.netlist,
            "ok": self.ok,
            "resistors": self.resistors,
            "capacitors": self.capacitors,
            # The sum is reported for completeness but is rarely the interesting
            # number: a device cell's resistor list mixes a few ohms of signal
            # interconnect with kilo-ohms of well/substrate path, so read
            # resistor_elements rather than the total.
            "total_resistance_ohm": self.total_resistance,
            "resistor_elements": self.resistor_elements,
            "resistance_extracted": self.resistance_extracted,
            "physical": self.physical,
            "negative_capacitors": self.negative_capacitors,
            "note": self.note,
            "total_capacitance_f": self.total_capacitance,
            "per_net_capacitance_f": self.per_net_capacitance,
            "log": self.log,
            "error": self.error,
        }

    def write(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        return path


def _magic_script(
    gds: Path, cell: str, out_spice: Path, resistance: bool, flat: bool = True
) -> str:
    """Tcl for a headless Magic extraction.

    ``ext2spice scale off`` is already set by the PDK magicrc; the thresholds are
    set to 0 here so no parasitic is discarded, which is the point of PEX.

    Extraction is flattened because a hierarchical one produces **negative**
    substrate capacitance. Measured on the CTLE stage: hierarchical gives 98
    capacitors totalling 135.07 fF with nine negative terms, worst -85.2 fF, while
    flat gives 26 totalling 212.71 fF with none. The negatives name the mechanism —
    the same value repeats once per array instance on the bus rail nodes, so Magic
    computes a rail's substrate capacitance in the parent and subtracts coupling
    attributed to the child unit cells until the residual goes negative. So the
    hierarchical numbers were not merely mislabelled, they were 58 fF short.

    It is not the ``cthresh`` setting: 0, 0.01 and 1 all give identical negatives.
    """
    lines = [
        "crashbackups stop",
        "drc off",
        f"gds read {gds}",
        f"load {cell} -dereference",
        "select top cell",
        "extract path .",
    ]
    if resistance:
        # Resistance extraction is a separate pass in Magic: extract writes the
        # .ext files, extresist then solves the resistor networks.
        lines += [
            "extract do resistance",
            "extract all",
            "ext2sim labels on",
            "ext2sim",
            "extresist tolerance 10",
            "extresist all",
        ]
    else:
        lines += ["extract all"]

    lines += [
        "ext2spice lvs",
        "ext2spice cthresh 0",
        "ext2spice rthresh 0",
        "ext2spice subcircuit on",
    ]
    if flat:
        # Flattening the netlist also drops the .subckt wrapper, so a caller that
        # needs an includable subcircuit has to supply the header and the port
        # list itself. layout/common/simview.py does that.
        lines.append("ext2spice hierarchy off")
    if resistance:
        lines.append("ext2spice extresist on")
    lines += [
        f"ext2spice -o {out_spice}",
        "quit -noprompt",
    ]
    return "\n".join(lines) + "\n"


def run_magic_pex(
    gds: Path,
    cell: str,
    run_dir: Path,
    resistance: bool = True,
    timeout: int = 1800,
    retry_without_resistance: bool = True,
    flat: bool = True,
) -> PexResult:
    """Extract R and C with Magic using the PDK extraction tech.

    Magic's ``extresist`` pass segfaults when two ports of a cell are DC-shorted.
    That is not a malformed layout: an inductor is one continuous piece of
    TopMetal2, so its terminals genuinely are shorted at DC, and a substrate tap
    ties its contact to the well. When the resistance pass dies,
    ``retry_without_resistance`` falls back to capacitance-only extraction and
    records that it did, rather than reporting the cell as unextractable.
    """
    result = _run_magic_pex_once(gds, cell, run_dir, resistance, timeout, flat)
    if result.ok or not (resistance and retry_without_resistance):
        return result

    fallback = _run_magic_pex_once(
        gds, cell, Path(run_dir).with_name(Path(run_dir).name + "_nores"), False, timeout, flat
    )
    if fallback.ok:
        fallback.resistance_extracted = False
        fallback.note = (
            "resistance extraction crashed (Magic extresist segfaults on "
            "DC-shorted ports); capacitance-only extraction used"
        )
        return fallback
    return result


def _run_magic_pex_once(
    gds: Path,
    cell: str,
    run_dir: Path,
    resistance: bool,
    timeout: int,
    flat: bool = True,
) -> PexResult:
    gds = Path(gds).resolve()
    run_dir = Path(run_dir).resolve()
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    result = PexResult(cell=cell, gds=str(gds))
    magic = shutil.which("magic")
    if magic is None:
        result.error = "magic not found on PATH; run scripts/install-ihp-layout.sh"
        return result

    rcfile = pdk_paths().magic_rcfile
    if not rcfile.is_file():
        result.error = f"PDK magicrc missing at {rcfile}"
        return result

    out_spice = run_dir / f"{cell}_pex.spice"
    script = run_dir / "extract.tcl"
    script.write_text(_magic_script(gds, cell, out_spice, resistance, flat))

    env = dict(os.environ)
    env.setdefault("PDK_ROOT", str(pdk_paths().root))
    try:
        completed = subprocess.run(
            [magic, "-dnull", "-noconsole", "-rcfile", str(rcfile), str(script)],
            cwd=run_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        result.error = f"magic extraction timed out after {timeout}s"
        return result

    log = run_dir / "magic_pex.log"
    log.write_text(
        f"$ magic -dnull -noconsole -rcfile {rcfile} {script}\n\n"
        f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}\n"
    )
    result.log = str(log)

    if not out_spice.is_file():
        result.error = (
            f"magic produced no netlist (exit {completed.returncode}); see {log}"
        )
        return result

    result.netlist = str(out_spice)
    text = out_spice.read_text()
    _summarize_netlist(text, result)
    return result


def _summarize_netlist(text: str, result: PexResult) -> None:
    total_r = 0.0
    count_r = 0
    elements: list[dict] = []
    for match in _R_LINE.finditer(text):
        try:
            value = float(match.group(4))
        except ValueError:
            continue
        total_r += value
        count_r += 1
        elements.append(
            {
                "name": f"R{match.group(1)}",
                "a": match.group(2),
                "b": match.group(3),
                "ohm": value,
            }
        )
    # Ties are broken on the name so the order does not depend on where Magic
    # happened to emit each element; these summaries are committed and diffed.
    result.resistor_elements = sorted(elements, key=lambda e: (-e["ohm"], e["name"]))[:20]
    total_c = 0.0
    count_c = 0
    per_net: dict[str, float] = {}
    for match in _C_LINE.finditer(text):
        try:
            value = _si(match.group(4), match.group(5))
        except ValueError:
            continue
        total_c += value
        count_c += 1
        if value < 0:
            result.negative_capacitors.append(
                {
                    "net_a": match.group(2),
                    "net_b": match.group(3),
                    "farads": float(f"{value:.6g}"),
                }
            )
        for net in (match.group(2), match.group(3)):
            per_net[net] = per_net.get(net, 0.0) + value
    result.resistors = count_r
    result.capacitors = count_c
    result.total_resistance = round(total_r, 6)
    # Summing floats in file order leaves the last couple of digits unstable between
    # otherwise identical runs, and these summaries are committed, so that jitter
    # showed up as a diff every time. Six significant figures is far more precision
    # than an extraction of an aF-scale coupling supports.
    result.total_capacitance = float(f"{total_c:.6g}")
    result.per_net_capacitance = {
        net: float(f"{value:.6g}")
        for net, value in sorted(per_net.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
    }


# --- klayout.pex cross-check ----------------------------------------------


def klayout_wire_resistance(
    gds: Path,
    metal: str,
    port_points: list[tuple[float, float]],
    cell: str | None = None,
) -> dict:
    """Resistance of the largest ``metal`` polygon between two port points.

    An independent check on Magic: same geometry, a different algorithm
    (square counting on a convex decomposition) and a different sheet-resistance
    source (docs/PDK.md rather than the Magic extract deck).
    """
    import klayout.db as kdb  # noqa: PLC0415
    import klayout.pex as kpex  # noqa: PLC0415

    if metal not in SHEET_RESISTANCE:
        raise KeyError(f"no sheet resistance recorded for {metal}")

    layout = kdb.Layout()
    layout.read(str(gds))
    top = layout.cell(cell) if cell else layout.top_cell()
    if top is None:
        raise RuntimeError(f"no cell {cell!r} in {gds}")

    ld = layer_map()[f"{metal.lower()}_drw"]
    layer_index = layout.layer(ld[0], ld[1])
    region = kdb.Region(top.begin_shapes_rec(layer_index)).merged()
    if region.is_empty():
        raise RuntimeError(f"no {metal} geometry in {gds}")

    polygon = max(region.each(), key=lambda p: p.area())

    # The extractor takes the database unit and returns resistances normalized
    # to a sheet resistance of 1 ohm/square, i.e. a square count. Scaling by the
    # metal's sheet resistance afterwards is what makes this an independent
    # check: the geometry decomposition is KLayout's, while the ohms-per-square
    # comes from the ITF stack rather than from Magic's extract deck.
    extractor = kpex.RExtractor.square_counting_extractor(layout.dbu)
    vertex_ports = [kdb.Point(int(x / layout.dbu), int(y / layout.dbu)) for x, y in port_points]
    network = extractor.extract(polygon, vertex_ports, [])

    elements = list(network.each_element())
    squares = sum(element.resistance() for element in elements)
    sheet = SHEET_RESISTANCE[metal]
    return {
        "metal": metal,
        "sheet_resistance_ohm_per_sq": sheet,
        "polygon_area_um2": round(polygon.area() * layout.dbu * layout.dbu, 6),
        "elements": len(elements),
        "squares": round(squares, 6),
        "series_resistance_ohm": round(squares * sheet, 6),
        "network": network.to_s(True)[:2000],
    }
