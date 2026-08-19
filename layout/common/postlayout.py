"""Post-layout simulation: does the extracted netlist behave like the schematic?

The point of PEX is not the parasitic count, it is the answer to "how much did
layout cost me". These helpers simulate a device twice in ngspice — once as the
schematic element, once as the Magic-extracted subcircuit — and report the
difference in the quantity that matters for that device.

ngspice conventions follow ``circuits/ctle56n/python/ctlelib``: the PDK model
libraries are included by corner, and per MEMORY.md device currents are only
available when listed in ``save`` before the analysis.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from layout.common.paths import pdk_paths


def pdk_models() -> Path:
    return pdk_paths().root / pdk_paths().pdk / "libs.tech" / "ngspice" / "models"


@dataclass
class SimResult:
    ok: bool
    value: float | None
    log: str
    error: str = ""


@dataclass
class PexInclude:
    """An extracted netlist prepared for inclusion in a testbench."""

    path: Path
    #: Subcircuit name when Magic wrapped the cell, else None.
    subckt: str | None
    #: Port nets, either the subcircuit's or the top-level circuit's own nets.
    ports: tuple[str, str]

    def dut_line(self, p: str = "p", n: str = "n") -> str:
        """Instantiation line, or empty when the netlist is already top level."""
        if self.subckt:
            return f"X1 {p} {n} {self.subckt}"
        return ""


_SUBCKT_RE = re.compile(r"^\s*\.subckt\s+(\S+)((?:\s+\S+)*)", re.M | re.I)


def prepare_pex_include(
    netlist: Path, run_dir: Path, ports: tuple[str, str] = ("PLUS", "MINUS")
) -> PexInclude:
    """Make an extracted netlist safe to ``.include`` and describe its shape.

    Magic emits either a ``.subckt`` — when it recognises the cell's labels as
    ports — or a bare top-level circuit. A plate capacitor lands in the second
    form because its terminals are on Metal5 and TopMetal1 rather than on a
    layer Magic maps to port labels, so both have to be handled.

    The trailing ``.end`` is also stripped: left in place it terminates the
    including netlist's parsing.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    text = Path(netlist).read_text()

    match = _SUBCKT_RE.search(text)
    subckt = match.group(1) if match else None
    if match:
        named = [tok for tok in match.group(2).split() if tok]
        if len(named) >= 2:
            ports = (named[0], named[1])

    cleaned = "\n".join(
        line for line in text.splitlines() if line.strip().lower() not in (".end",)
    )
    out = run_dir / f"{Path(netlist).stem}.inc"
    out.write_text(cleaned + "\n")
    return PexInclude(path=out, subckt=subckt, ports=ports)


_MEAS_RE = re.compile(r"^\s*(\w+)\s*=\s*([0-9.eE+-]+)", re.M)


def run_ngspice(netlist: str, run_dir: Path, name: str, timeout: int = 600) -> tuple[bool, str]:
    """Run a netlist in batch ngspice; returns ``(ok, output)``."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"{name}.cir"
    path.write_text(netlist)

    ngspice = shutil.which("ngspice")
    if ngspice is None:
        return False, "ngspice not found on PATH"

    try:
        completed = subprocess.run(
            [ngspice, "-b", str(path)],
            cwd=run_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=dict(os.environ),
        )
    except subprocess.TimeoutExpired:
        return False, f"ngspice timed out after {timeout}s"

    output = f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}\n"
    (run_dir / f"{name}.log").write_text(output)
    return completed.returncode == 0, output


def _extract_print(output: str, node: str) -> float | None:
    """Pull a value out of ngspice's `print` output for a node expression."""
    for line in output.splitlines():
        if node in line and "=" in line:
            match = re.search(r"=\s*([0-9.eE+-]+)", line)
            if match:
                try:
                    return float(match.group(1))
                except ValueError:
                    continue
    return None


def measure_two_terminal_resistance(
    run_dir: Path,
    name: str,
    body: str,
    corner_lib: str,
    corner: str,
    dut_line: str,
    force: str = "p",
    sense: str = "n",
    bias: float = 0.1,
) -> SimResult:
    """DC resistance between two terminals of a DUT, from a 100 mV bias.

    ``body`` holds any extra netlist text (an ``.include`` of the extracted
    subcircuit, for instance) and ``dut_line`` instantiates the device between
    nets ``p`` and ``n``.
    """
    models = pdk_models()
    netlist = f"""* {name} — two-terminal resistance
.lib '{models}/{corner_lib}' {corner}
{body}

Vforce {force} 0 dc {bias}
{dut_line}
Vsense {sense} 0 dc 0

.control
save all
op
let rmeas = {bias} / abs(i(Vsense))
print rmeas
quit
.endc
.end
"""
    ok, output = run_ngspice(netlist, run_dir, name)
    value = _extract_print(output, "rmeas")
    if not ok or value is None:
        return SimResult(False, value, output, error=f"{name}: could not measure resistance")
    return SimResult(True, value, output)


def measure_two_terminal_capacitance(
    run_dir: Path,
    name: str,
    body: str,
    corner_lib: str,
    corner: str,
    dut_line: str,
    force: str = "p",
    sense: str = "n",
    freq: float = 1e9,
) -> SimResult:
    """Capacitance between two terminals, from the AC current at ``freq``."""
    models = pdk_models()
    netlist = f"""* {name} — two-terminal capacitance at {freq:g} Hz
.lib '{models}/{corner_lib}' {corner}
{body}

Vforce {force} 0 dc 0 ac 1
{dut_line}
Vsense {sense} 0 dc 0

.control
save all
ac lin 1 {freq:g} {freq:g}
let cmeas = abs(i(Vsense)) / (2 * pi * {freq:g})
print cmeas
quit
.endc
.end
"""
    ok, output = run_ngspice(netlist, run_dir, name)
    value = _extract_print(output, "cmeas")
    if not ok or value is None:
        return SimResult(False, value, output, error=f"{name}: could not measure capacitance")
    return SimResult(True, value, output)


def compare(schematic: float, extracted: float) -> dict:
    """Relative change from schematic to post-layout."""
    delta = extracted - schematic
    rel = delta / schematic if schematic else float("inf")
    return {
        "schematic": schematic,
        "post_layout": extracted,
        "delta": delta,
        "rel_change": rel,
        "rel_change_pct": rel * 100.0,
    }
