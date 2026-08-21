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


# --- Post-layout netlist assembly --------------------------------------------

_SUBCKT_END_RE = re.compile(r"^\s*\.ends\b", re.I)
_ELEMENT_LINE_RE = re.compile(r"^[A-Za-z\*]", re.I)
_FLOATING_COMMENT_RE = re.compile(r"\s+\$\s*\*\*FLOATING\s*$", re.I)
_C_LINE = re.compile(r"^C(\S+)\s+(\S+)\s+(\S+)\s+([0-9.eE+-]+)(\w*)", re.I)
_LIB_SUBCKT_RE = re.compile(r"^\s*\.subckt\s+(\S+)(.*)$", re.I | re.M)
_LIB_PARAM_RE = re.compile(r"^\s*\.param\s+(.+)$", re.I | re.M)
_PARAM_NAME_RE = re.compile(r"(\w+)=", re.I)

_SI = {"m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15, "a": 1e-18, "k": 1e3}

#: Magic substrate / well nodes are allowed on capacitors even when not core ports.
_SUBSTRATE_NODES = frozenset({"0", "gnd", "sub!", "sub"})


def _si(value: str, suffix: str = "") -> float:
    scale = _SI.get(suffix[:1].lower(), 1.0) if suffix else 1.0
    return float(value) * scale


def _is_substrate_node(node: str) -> bool:
    name = node.lower()
    return name in _SUBSTRATE_NODES or name.startswith("w_")


def _model_lib_files() -> list[Path]:
    root = pdk_models()
    if not root.is_dir():
        return []
    return sorted(root.glob("*.lib"))


_model_param_cache: dict[str, frozenset[str]] = {}


def model_parameters(model: str) -> frozenset[str]:
    """Accepted parameter names for ``model``, parsed from the PDK ``.subckt`` line.

    The deck lists defaults on the subcircuit declaration and on ``.param``
    continuations; anything else extraction emits (``A``/``P`` on ``cap_cmomi``,
    for instance) must be dropped before ngspice sees the netlist.
    """
    key = model.lower()
    if key in _model_param_cache:
        return _model_param_cache[key]

    found: set[str] = set()
    for lib_path in _model_lib_files():
        text = lib_path.read_text(encoding="utf-8", errors="replace")
        for match in _LIB_SUBCKT_RE.finditer(text):
            if match.group(1).lower() != key:
                continue
            tail = match.group(2)
            for param_match in _PARAM_NAME_RE.finditer(tail):
                found.add(param_match.group(1).lower())
            start = match.end()
            end = text.find("\n.subckt", start)
            if end < 0:
                end = text.find("\n.ends", start)
            if end < 0:
                end = len(text)
            block = text[start:end]
            for line in block.splitlines():
                stripped = line.strip()
                if stripped.startswith("+"):
                    for param_match in _PARAM_NAME_RE.finditer(stripped):
                        found.add(param_match.group(1).lower())
                param_line = _LIB_PARAM_RE.match(stripped)
                if param_line:
                    for param_match in _PARAM_NAME_RE.finditer(param_line.group(1)):
                        found.add(param_match.group(1).lower())
            break
        if found:
            break

    result = frozenset(found)
    _model_param_cache[key] = result
    return result


def _join_element_lines(raw_lines: list[str]) -> list[str]:
    """Merge SPICE continuation lines (``+``) into single element strings."""
    joined: list[str] = []
    current = ""
    for line in raw_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(("*", ".")):
            continue
        if stripped.startswith("+"):
            current += " " + stripped[1:].strip()
        else:
            if current:
                joined.append(current)
            current = stripped
    if current:
        joined.append(current)
    return joined


def _split_element_tokens(line: str) -> tuple[str, list[str], str, list[str]]:
    """Return ``(element_name, nodes, model, param_tokens)``."""
    tokens = line.split()
    if len(tokens) < 2:
        raise ValueError(f"not a device line: {line!r}")
    name = tokens[0]
    body = tokens[1:]
    params: list[str] = []
    while body and "=" in body[-1]:
        params.insert(0, body.pop())
    if not body:
        raise ValueError(f"no model name in line: {line!r}")
    model = body.pop()
    return name, body, model, params


def _as_subckt_instance(name: str) -> str:
    """LVS writes ``R$``/``Q$``/``M$``/``D$`` lines; PDK models are ``.subckt`` wrappers.

    ``D`` is required for the ESD diodes and the ``nmoscl_2`` clamp: their compact
    models are subcircuits (``diodevdd_2kv``, ``diodevss_2kv``, ``nmoscl_2``), and
    leaving the LVS ``D$`` prefix makes ngspice treat them as primitive diodes.
    """
    prefix = name[0].upper()
    if prefix == "X":
        return name
    if prefix in {"R", "Q", "M", "C", "D"}:
        return "X" + name[1:]
    return name


def normalise_element(line: str) -> str:
    """Drop parameters the model does not accept; force ``pre_layout=0`` on MOS.

    Layout extraction supplies ``AS/AD/PS/PD``; ``pre_layout=1`` (the model
    default) would add a layout allowance on top of those measured values.
    """
    joined = " ".join(line.split())
    _, nodes, model, param_tokens = _split_element_tokens(joined)
    accepted = model_parameters(model)
    kept: dict[str, str] = {}
    for token in param_tokens:
        key, _, value = token.partition("=")
        if key.lower() in accepted:
            kept[key.lower()] = value

    if "pre_layout" in accepted:
        # Core carries extracted junction geometry; do not stack layout allowance.
        kept["pre_layout"] = "0"

    param_part = " ".join(f"{key}={value}" for key, value in kept.items())
    elem_name = _as_subckt_instance(joined.split()[0])
    parts = [elem_name, *nodes, model]
    if param_part:
        parts.append(param_part)
    return " ".join(parts)


def _rename_ctle(nodes: list[str], model_l: str) -> str | None:
    if model_l == "npn13g2":
        node_set = set(nodes)
        if {"outp", "inp", "e1"}.issubset(node_set):
            return "XQ1"
        if {"outn", "inn", "e2"}.issubset(node_set):
            return "XQ2"
    elif model_l == "sg13_lv_nmos":
        if len(nodes) >= 2 and nodes[0] == "mgate" and nodes[1] == "mgate":
            return "Xmdiode"
        if nodes and nodes[0] == "e1":
            return "Xtail1"
        if nodes and nodes[0] == "e2":
            return "Xtail2"
    return None


def _rename_vga(nodes: list[str], model_l: str) -> str | None:
    if model_l == "npn13g2":
        node_set = set(nodes)
        if {"outp", "inp", "em"}.issubset(node_set):
            return "XQ1"
        if {"outn", "inn", "em"}.issubset(node_set):
            return "XQ2"
        if {"outp", "vicm", "ed1"}.issubset(node_set):
            return "XQd1"
        if {"outn", "vicm", "ed2"}.issubset(node_set):
            return "XQd2"
    elif model_l == "sg13_lv_nmos":
        if len(nodes) >= 2 and nodes[0] == "mgate" and nodes[1] == "mgate":
            return "Xmdiode"
        if len(nodes) >= 3 and nodes[0] == "tx1" and nodes[1] == "mgate":
            return "Xtail1"
        if len(nodes) >= 3 and nodes[0] == "tx2" and nodes[1] == "mgate":
            return "Xtail2"
        if len(nodes) >= 3 and nodes[0] == "em" and nodes[1] == "steerp" and nodes[2] == "tx1":
            return "Xps1"
        if len(nodes) >= 3 and nodes[0] == "em" and nodes[1] == "steerp" and nodes[2] == "tx2":
            return "Xps2"
        if len(nodes) >= 3 and nodes[0] == "ed1" and nodes[1] == "steern" and nodes[2] == "tx1":
            return "Xpd1"
        if len(nodes) >= 3 and nodes[0] == "ed2" and nodes[1] == "steern" and nodes[2] == "tx2":
            return "Xpd2"
    return None


def _rename_driver(nodes: list[str], model_l: str) -> str | None:
    node_set = set(nodes)
    if model_l == "npn13g2":
        if {"outp", "inp", "em"}.issubset(node_set):
            return "XQ1"
        if {"outn", "inn", "em"}.issubset(node_set):
            return "XQ2"
    elif model_l == "sg13_lv_nmos":
        if len(nodes) >= 2 and nodes[0] == "mgate" and nodes[1] == "mgate":
            return "Xmdiode"
        if len(nodes) >= 2 and nodes[0] == "em" and nodes[1] == "mgate":
            return "Xtail"
    elif model_l == "diodevdd_2kv":
        if "outp" in node_set:
            return "Xesd_vdd_p"
        if "outn" in node_set:
            return "Xesd_vdd_n"
    elif model_l == "diodevss_2kv":
        if "outp" in node_set:
            return "Xesd_vss_p"
        if "outn" in node_set:
            return "Xesd_vss_n"
    elif model_l == "nmoscl_2":
        return "Xclamp"
    return None


_RENAME_BY_CELL = {
    "ctle_dut": _rename_ctle,
    "vga_dut": _rename_vga,
    "driver_dut": _rename_driver,
}


def _clean_node(name: str) -> str:
    """KLayout escapes generated net names as ``\\$10``."""
    return name.replace("\\", "")


def _vga_tx_aliases(lines: list[str]) -> dict[str, str]:
    """Map LVS-generated tail nets onto the schematic's ``tx1``/``tx2``.

    Those nets are unlabeled in the GDS, so LVS invents ``$10``/``$11``. The
    dummy-steer devices identify them: ``ed1/steern/<net>`` is ``tx1``.
    """
    aliases: dict[str, str] = {}
    for line in lines:
        try:
            _, nodes, model, _ = _split_element_tokens(line)
        except ValueError:
            continue
        if model.lower() != "sg13_lv_nmos" or len(nodes) < 3:
            continue
        drain, gate, source = (_clean_node(n) for n in nodes[:3])
        if drain == "ed1" and gate == "steern":
            aliases[source] = "tx1"
        elif drain == "ed2" and gate == "steern":
            aliases[source] = "tx2"
    return {src: dst for src, dst in aliases.items() if src not in {"tx1", "tx2"}}


# LVS writes ESD terminals in deck order (BJT3 C-B-E, clamp diode N-P),
# which is not the ngspice ``.subckt`` pin order.
_ESD_LVS_TO_SPICE = {
    "diodevdd_2kv": (1, 2, 0),  # C B E = VSS VDD PAD → VDD PAD VSS
    "diodevss_2kv": (0, 2, 1),  # C B E = VDD VSS PAD → VDD PAD VSS
    "nmoscl_2": (1, 0),  # extracted VSS VDD → VDD VSS
}


def _remap_esd_nodes(nodes: list[str], model_l: str) -> list[str]:
    order = _ESD_LVS_TO_SPICE.get(model_l)
    if order is None or len(nodes) < len(order):
        return nodes
    return [nodes[i] for i in order] + nodes[len(order):]


def _apply_node_aliases(line: str, aliases: dict[str, str]) -> str:
    joined = " ".join(line.split())
    name, nodes, model, params = _split_element_tokens(joined)
    nodes = [aliases.get(_clean_node(n), _clean_node(n)) for n in nodes]
    nodes = _remap_esd_nodes(nodes, model.lower())
    return " ".join([name, *nodes, model, *params])


def rewrite_extracted_lines(lines: list[str], cell: str = "ctle_dut") -> list[str]:
    """Normalise LVS lines and rename instances (and VGA ``tx*`` nets) for probes."""
    aliases = _vga_tx_aliases(lines) if cell == "vga_dut" else {}
    rewritten: list[str] = []
    for line in lines:
        line = _apply_node_aliases(line, aliases)
        line = rename_schematic_instances(normalise_element(line), cell)
        rewritten.append(line)
    return rewritten


def rename_schematic_instances(line: str, cell: str = "ctle_dut") -> str:
    """Map LVS instance names to the schematic's ``XQ1``/``Xtail1``/… for ``save`` probes."""
    _, nodes, model, _param_tokens = _split_element_tokens(line)
    model_l = model.lower()
    rename = _RENAME_BY_CELL.get(cell)
    new_name = rename(nodes, model_l) if rename is not None else None
    if new_name is None:
        return line
    tokens = line.split()
    tokens[0] = new_name
    return " ".join(tokens)


def parse_subckt_ports(netlist: Path, subckt: str) -> list[str]:
    """Read the port list from ``.subckt <subckt> ...`` in a netlist file."""
    needle = subckt.lower()
    lines = Path(netlist).read_text().splitlines()
    for index, line in enumerate(lines):
        match = _SUBCKT_RE.match(line)
        if not match or match.group(1).lower() != needle:
            continue
        ports = [tok for tok in match.group(2).split() if tok and "=" not in tok]
        nxt = index + 1
        while nxt < len(lines) and lines[nxt].lstrip().startswith("+"):
            ports.extend(
                tok for tok in lines[nxt].lstrip()[1:].split() if tok and "=" not in tok
            )
            nxt += 1
        if not ports:
            raise ValueError(f"empty port list for .subckt {subckt!r} in {netlist}")
        return ports
    raise ValueError(f"no .subckt {subckt!r} in {netlist}")


def extract_subckt_lines(netlist: Path, subckt: str) -> list[str]:
    """Raw element lines inside ``.subckt <subckt>`` (continuations merged)."""
    text = Path(netlist).read_text()
    needle = subckt.lower()
    in_body = False
    raw: list[str] = []
    for line in text.splitlines():
        match = _SUBCKT_RE.match(line)
        if match:
            in_body = match.group(1).lower() == needle
            continue
        if not in_body:
            continue
        if _SUBCKT_END_RE.match(line):
            break
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            continue
        # A wrapped ``.SUBCKT`` port list is a ``+`` line before any element.
        if not raw and stripped.startswith("+"):
            continue
        raw.append(line)
    return _join_element_lines(raw)


def magic_capacitor_lines(
    pex_netlist: Path,
    known_nets: set[str],
) -> tuple[list[str], float, float]:
    """``C*`` lines from a flat Magic deck, restricted to ``known_nets``.

    Returns ``(lines, kept_farads, dropped_farads)``. Resistor body nodes such
    as ``rd1_R0_0/a_0_0#`` are not in the core and their parasitics are dropped
    (~3 fF on the CTLE stage); the compact resistor model already covers them.
    """
    text = Path(pex_netlist).read_text()
    kept: list[tuple[tuple[str, str], float, str]] = []
    kept_f = 0.0
    dropped_f = 0.0

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("C"):
            continue
        match = _C_LINE.match(stripped)
        if not match:
            continue
        net_a, net_b = match.group(2), match.group(3)
        value_f = _si(match.group(4), match.group(5))

        def allowed(net: str) -> bool:
            return net in known_nets or _is_substrate_node(net)

        if allowed(net_a) and allowed(net_b):
            # Canonicalise the whole line, not just its sort position. Magic varies
            # both the order of capacitor lines and the order of the two terminals
            # within a line between runs, so `C0 mgate inp` and `C0 inp mgate` came
            # out of identical geometry. A capacitor is symmetric, so sorting its
            # terminals loses nothing and makes the artifact reproducible.
            pair = tuple(sorted((net_a, net_b)))
            kept.append((pair, value_f, f"{match.group(4)}{match.group(5)}"))
            kept_f += value_f
        else:
            dropped_f += value_f

    # Magic emits capacitors in a different order from one run to the next, so two
    # identical runs produced netlists that differed only in line order. These
    # netlists are committed artifacts, so they are renumbered in a sorted order:
    # by net pair, then by value.
    kept.sort(key=lambda item: (item[0], item[1]))
    kept_lines = [
        f"C{index} {pair[0]} {pair[1]} {value_text}"
        for index, (pair, _, value_text) in enumerate(kept)
    ]
    return kept_lines, kept_f, dropped_f


def write_core(
    path: Path,
    subckt: str,
    ports: list[str],
    device_lines: list[str],
    parasitic_lines: tuple[str, ...] = (),
) -> Path:
    """Write an includable extracted core: ``.subckt`` / devices / ``.ends``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"* {subckt} — extracted core; do not edit by hand",
        f".subckt {subckt} {' '.join(ports)}",
        *device_lines,
        *parasitic_lines,
        f".ends {subckt}",
        "",
    ]
    path.write_text("\n".join(lines))
    return path
