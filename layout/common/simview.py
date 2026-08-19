"""Post-layout simulation views: black-box devices and a schematic-compatible wrapper.

Two device kinds are removed from the Magic-extracted netlist and re-instantiated
from the schematic's compact models instead:

* ``inductor`` — the PDK has no ngspice inductor model; the authority is the
  EM-fitted ``ind_shunt`` lumped subcircuit.
* ``cmomi`` — Magic extracts finger-snapped geometry with an uncalibrated model,
  not the calibrated ``cap_cmomi`` compact model.

Because those devices are absent from the extracted core, any net they touched that
is not already a schematic port must be promoted to a port on the extracted
subcircuit. The wrapper subcircuit then reconnects the compact models on those
internal nodes while presenting the same seven pins as ``ctle_pdk.cir``, so
existing testbenches need no changes.
"""

from __future__ import annotations

from pathlib import Path

from layout.common.devices import kind_of, um
from layout.common.netlist import write_block_cdl
from layout.common.sizing import metres, read_params
from layout.common.spec import DeviceSpec

BLACK_BOX_KINDS: tuple[str, ...] = ("inductor", "cmomi")

#: The EM-fitted coil model is included by token, not by absolute path, exactly as
#: ctle_pdk.cir does it. The wrapper is always consumed through prepare_tb, which
#: substitutes it, and a committed netlist must not carry a path from the machine
#: that generated it.
_IND_SHUNT_INC = "{IND_SHUNT_INC}"


def is_black_boxed(spec: DeviceSpec) -> bool:
    """True when ``spec.kind`` is replaced by a compact model outside extraction."""
    return spec.kind in BLACK_BOX_KINDS


def split_instances(
    instances: list[tuple[DeviceSpec, dict[str, str]]],
) -> tuple[
    list[tuple[DeviceSpec, dict[str, str]]],
    list[tuple[DeviceSpec, dict[str, str]]],
]:
    """Return ``(kept, black_boxed)`` from a list of ``(DeviceSpec, nets)`` pairs."""
    kept: list[tuple[DeviceSpec, dict[str, str]]] = []
    black_boxed: list[tuple[DeviceSpec, dict[str, str]]] = []
    for spec, nets in instances:
        if is_black_boxed(spec):
            black_boxed.append((spec, nets))
        else:
            kept.append((spec, nets))
    return kept, black_boxed


def _skip_terminals(kind) -> set[str]:
    skip = set(kind.implicit_nets)
    if kind.bulk_node is not None:
        skip.add(kind.bulk_node)
    return skip


def promoted_nets(
    instances: list[tuple[DeviceSpec, dict[str, str]]],
    port_nets: list[str],
) -> list[str]:
    """Nets touched by a black-boxed device that are not already ports.

    Derived from the instances, never hardcoded. Bulk and implicit substrate
    terminals are skipped. Order is sorted alphabetically for stability.
    """
    ports = set(port_nets)
    found: set[str] = set()
    for spec, nets in instances:
        if not is_black_boxed(spec):
            continue
        kind = kind_of(spec)
        skip = _skip_terminals(kind)
        for terminal in kind.terminals:
            if terminal in skip:
                continue
            net = nets.get(terminal, terminal)
            if net not in ports:
                found.add(net)
    return sorted(found)


def sim_port_nets(
    port_nets: list[str],
    instances: list[tuple[DeviceSpec, dict[str, str]]],
) -> list[str]:
    """The schematic ports followed by the promoted nets."""
    return list(port_nets) + promoted_nets(instances, port_nets)


def write_reduced_cdl(
    cell: str,
    port_nets: list[str],
    instances: list[tuple[DeviceSpec, dict[str, str]]],
    path: Path,
) -> Path:
    """CDL of the kept devices only, with promoted nets added to the ports.

    Reuses :func:`layout.common.netlist.write_block_cdl` so this cannot drift from
    the tape-out CDL.
    """
    kept, _ = split_instances(instances)
    ports = sim_port_nets(port_nets, instances)
    return write_block_cdl(cell, ports, kept, path)


def _inductor_line(spec: DeviceSpec, nets: dict[str, str]) -> str:
    """``ind_shunt`` positional form matching ``ctle_pdk.cir``."""
    p = nets.get("PLUS", "PLUS")
    n = nets.get("MINUS", "MINUS")
    sub = nets.get("sub", "sub")
    return f"X{spec.name} {p} {n} {sub} ind_shunt"


def _cmomi_line(spec: DeviceSpec, nets: dict[str, str], params: dict[str, float]) -> str:
    """``cap_cmomi`` form matching ``ctle_pdk.cir`` — compact model is the authority."""
    plus = nets.get("PLUS", "PLUS")
    minus = nets.get("MINUS", "MINUS")
    w = um(metres(params, "CMOMI_W"))
    length = um(metres(params, "CMOMI_L"))
    mmin = int(params["CMOMI_MMIN"])
    mmax = int(params["CMOMI_MMAX"])
    # feed=same: feed=double self-resonates at 30–47 GHz (MEMORY.md); not for 28 GHz.
    return (
        f"X{spec.name} {plus} {minus} cap_cmomi w={w} l={length} "
        f"mmin={mmin} mmax={mmax} feed=same m=1"
    )


def black_box_lines(
    instances: list[tuple[DeviceSpec, dict[str, str]]],
    params: dict[str, float] | None = None,
) -> list[str]:
    """SPICE lines re-instantiating black-boxed devices from schematic parameters."""
    values = params or read_params()
    lines: list[str] = []
    for spec, nets in instances:
        if not is_black_boxed(spec):
            continue
        if spec.kind == "inductor":
            lines.append(_inductor_line(spec, nets))
        elif spec.kind == "cmomi":
            lines.append(_cmomi_line(spec, nets, values))
        else:
            raise KeyError(f"no black-box line for kind {spec.kind!r}")
    return lines


def write_wrapper(
    path: Path,
    cell: str,
    port_nets: list[str],
    instances: list[tuple[DeviceSpec, dict[str, str]]],
    core_netlist: Path | str,
    core_subckt: str,
    core_ports: list[str],
    params: dict[str, float] | None = None,
    extra_includes: tuple[str | Path, ...] = (),
) -> Path:
    """Write a wrapper subcircuit presenting the schematic's interface."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    _, black_boxed = split_instances(instances)
    lines = [
        f"* {cell} — post-layout DUT wrapper; do not edit by hand",
        f".include {core_netlist}",
    ]
    if any(spec.kind == "inductor" for spec, _ in black_boxed):
        lines.append(f".include {_IND_SHUNT_INC}")
    for include in extra_includes:
        lines.append(f".include {include}")
    lines.append(f".subckt {cell} {' '.join(port_nets)}")
    core_nodes = " ".join(core_ports)
    lines.append(f"Xcore {core_nodes} {core_subckt}")
    lines.extend(black_box_lines(instances, params=params))
    lines.extend([f".ends {cell}", ""])
    path.write_text("\n".join(lines))
    return path
