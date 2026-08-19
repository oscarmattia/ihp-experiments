"""CDL generation from the same DeviceSpec that builds the layout.

Element forms follow the PDK's LVS testcases under
``tech/lvs/testing/testcases/unit``, which is what ``sg13g2.lvs`` is validated
against:

    M<name> D G S sub sg13_lv_nmos w=5u l=1u ng=1 m=1
    R<name> n1 n2 sub rppd m=1 l=1.4u w=5u
    C<name> plus minus cap_cmim w=10u l=10u
    Q<name> C B E sub! npn13G2 le=900n we=70n m=1
    L<name> n1 n2 sub inductor w=4u s=2.1u d=40u nr_r=1

Resistors, capacitors and inductors put the model name after an optional bulk
node; MOS and BJT carry the bulk as a regular terminal.
"""

from __future__ import annotations

from pathlib import Path

from layout.common.devices import kind_of
from layout.common.spec import DeviceSpec

#: Net name used for the substrate/bulk connection.
BULK_NET = "sub"


def element_line(spec: DeviceSpec, nets: dict[str, str] | None = None, instance: str = "1") -> str:
    """One CDL element line for ``spec``."""
    kind = kind_of(spec)
    nets = nets or {}

    signal_terminals = [t for t in kind.terminals if t not in kind.implicit_nets]
    node_names = [nets.get(t, t) for t in signal_terminals]
    for terminal in kind.implicit_nets:
        node_names.append(nets.get(terminal, BULK_NET))

    fields = [f"{kind.cdl_prefix}{instance}", *node_names]
    if kind.bulk_node is not None:
        fields.append(nets.get(kind.bulk_node, BULK_NET))
    fields.append(kind.spice_model)
    for key, value in kind.to_cdl(spec.params).items():
        fields.append(f"{key}={value}")
    return " ".join(fields)


def device_subckt(spec: DeviceSpec, nets: dict[str, str] | None = None) -> str:
    """A one-device ``.SUBCKT`` wrapper, matching the PDK testcase style."""
    kind = kind_of(spec)
    nets = nets or {}
    ports = [nets.get(t, t) for t in kind.terminals if t not in kind.implicit_nets]
    lines = [
        f"* {spec.name} — generated from DeviceSpec; do not edit by hand",
        f".SUBCKT {spec.name} {' '.join(ports)}",
        element_line(spec, nets=nets),
        f".ENDS {spec.name}",
        "",
    ]
    return "\n".join(lines)


def write_cdl(spec: DeviceSpec, path: Path, nets: dict[str, str] | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(device_subckt(spec, nets=nets))
    return path


def block_subckt(
    name: str,
    ports: list[str],
    instances: list[tuple[DeviceSpec, dict[str, str]]],
) -> str:
    """A multi-device ``.SUBCKT`` for a block.

    ``instances`` pairs each device with its terminal-to-net mapping. Instance
    names are made unique per element prefix so two resistors become R1 and R2
    rather than colliding.
    """
    counters: dict[str, int] = {}
    lines = [
        f"* {name} — generated from DeviceSpecs; do not edit by hand",
        f".SUBCKT {name} {' '.join(ports)}",
    ]
    for spec, nets in instances:
        prefix = kind_of(spec).cdl_prefix
        counters[prefix] = counters.get(prefix, 0) + 1
        lines.append(element_line(spec, nets=nets, instance=str(counters[prefix])))
    lines.extend([f".ENDS {name}", ""])
    return "\n".join(lines)


def write_block_cdl(
    name: str,
    ports: list[str],
    instances: list[tuple[DeviceSpec, dict[str, str]]],
    path: Path,
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(block_subckt(name, ports, instances))
    return path
