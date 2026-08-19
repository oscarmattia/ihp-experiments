"""Typed wrappers over the foundry PCells.

Each :class:`DeviceKind` ties together the three views of a device that have to
stay consistent: the PCell that draws it, the CDL element the LVS deck expects,
and the terminal names used for ports and nets. Engineering parameters are
given in SI base units (metres, farads, ohms) and translated here, so callers
never hand-format PCell unit strings.

The CDL element forms below are taken from the PDK's own LVS testcases under
``tech/lvs/testing/testcases/unit``, which is what ``sg13g2.lvs`` is validated
against.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from layout.common.pdk import instantiate
from layout.common.spec import DeviceSpec


def um(value: float) -> str:
    """Format a length in metres as a PCell/CDL micron string."""
    return f"{value * 1e6:.6g}u"


def _require(params: dict[str, Any], *names: str) -> tuple:
    missing = [n for n in names if n not in params]
    if missing:
        raise KeyError(f"missing required parameter(s): {', '.join(missing)}")
    return tuple(params[n] for n in names)


@dataclass(frozen=True)
class DeviceKind:
    """How one logical device maps onto PCell, netlist and ports."""

    key: str
    #: PCell name in the SG13_dev library.
    pcell: str
    #: Model name as the LVS deck's device extraction reports it.
    spice_model: str
    #: SPICE/CDL element prefix (M, R, C, Q, L, X).
    cdl_prefix: str
    #: Terminal names in CDL order.
    terminals: tuple[str, ...]
    #: Engineering params -> PCell parameter dict.
    to_pcell: Callable[[dict[str, Any]], dict[str, Any]]
    #: Engineering params -> CDL parameter dict (ordered).
    to_cdl: Callable[[dict[str, Any]], dict[str, str]]
    #: Engineering params -> values expected back from LVS extraction, in SI
    #: units. This is not the same as ``to_cdl``: the deck reports MOS width
    #: per finger, and reports taps as area and perimeter rather than w/l. The
    #: special key ``area`` is satisfied by the product of extracted w and l.
    to_extracted: Callable[[dict[str, Any]], dict[str, float]] | None = None
    #: Bulk/substrate node emitted between the signal terminals and the model
    #: name, when the CDL form has one. ``None`` means no bulk node.
    bulk_node: str | None = None
    #: Terminals that carry the bulk connection rather than a routed signal.
    implicit_nets: tuple[str, ...] = field(default_factory=tuple)


# --- parameter translators -------------------------------------------------


def _mos_pcell(p: dict[str, Any]) -> dict[str, Any]:
    w, length = _require(p, "w", "l")
    ng = int(p.get("ng", 1))
    return {
        "w": um(w),
        "l": um(length),
        "ng": str(ng),
        "m": str(int(p.get("m", 1))),
    }


def _mos_cdl(p: dict[str, Any]) -> dict[str, str]:
    w, length = _require(p, "w", "l")
    return {
        "w": um(w),
        "l": um(length),
        "ng": str(int(p.get("ng", 1))),
        "m": str(int(p.get("m", 1))),
    }


def _res_pcell(p: dict[str, Any]) -> dict[str, Any]:
    """Resistor PCell parameters.

    ``Calculate`` must be flipped to ``R``: the PCells default to solving for
    ``l`` from a target resistance, and in that mode an explicit ``l`` is
    ignored, silently producing the default device.
    """
    w, length = _require(p, "w", "l")
    out: dict[str, Any] = {
        "Calculate": "R",
        "w": um(w),
        "l": um(length),
        "m": str(int(p.get("m", 1))),
    }
    if "b" in p:
        out["b"] = str(int(p["b"]))
    if "ps" in p:
        out["ps"] = um(p["ps"])
    return out


def _res_cdl(p: dict[str, Any]) -> dict[str, str]:
    w, length = _require(p, "w", "l")
    out = {"m": str(int(p.get("m", 1))), "l": um(length), "w": um(w)}
    if "b" in p:
        out["b"] = str(int(p["b"]))
    if "ps" in p:
        out["ps"] = um(p["ps"])
    return out


def _cmim_pcell(p: dict[str, Any]) -> dict[str, Any]:
    """MIM cap PCell parameters; ``Calculate=C`` honours an explicit w/l."""
    w, length = _require(p, "w", "l")
    return {
        "Calculate": "C",
        "w": um(w),
        "l": um(length),
        "m": str(int(p.get("m", 1))),
    }


def _cap_cdl(p: dict[str, Any]) -> dict[str, str]:
    w, length = _require(p, "w", "l")
    out = {"w": um(w), "l": um(length)}
    if int(p.get("m", 1)) != 1:
        out["m"] = str(int(p["m"]))
    return out


def _cmomi_pcell(p: dict[str, Any]) -> dict[str, Any]:
    """Metal-finger cap. ``feed`` matters electrically, not just physically.

    MEMORY.md records that ``feed=same`` is required at 28 GHz because
    ``feed=double`` resonates, so it is the default here.
    """
    w, length = _require(p, "w", "l")
    return {
        "w": um(w),
        "l": um(length),
        "mmin": int(p.get("mmin", 1)),
        "mmax": int(p.get("mmax", 5)),
        "feed": str(p.get("feed", "same")),
    }


def _cmomi_cdl(p: dict[str, Any]) -> dict[str, str]:
    w, length = _require(p, "w", "l")
    return {"w": um(w), "l": um(length)}


def _npn_pcell(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "Nx": int(p.get("Nx", 1)),
        "Ny": int(p.get("Ny", 1)),
        "le": um(p.get("le", 0.9e-6)),
        "we": um(p.get("we", 0.07e-6)),
        "m": str(int(p.get("m", 1))),
    }


def _npn_cdl(p: dict[str, Any]) -> dict[str, str]:
    # The LVS testcase writes le/we, not Nx; Nx becomes multiple emitter
    # stripes in layout and is reported through the device count.
    return {
        "le": um(p.get("le", 0.9e-6)),
        "we": um(p.get("we", 0.07e-6)),
        "m": str(int(p.get("m", 1)) * int(p.get("Nx", 1))),
    }


def _ind_pcell(p: dict[str, Any]) -> dict[str, Any]:
    d, w = _require(p, "d", "w")
    return {
        "w": um(w),
        "s": um(p.get("s", 2.1e-6)),
        "d": um(d),
        "nr_r": int(p.get("nr_r", 1)),
    }


def _ind_cdl(p: dict[str, Any]) -> dict[str, str]:
    d, w = _require(p, "d", "w")
    return {
        "w": um(w),
        "s": um(p.get("s", 2.1e-6)),
        "d": um(d),
        "nr_r": str(int(p.get("nr_r", 1))),
    }


def _tap_pcell(p: dict[str, Any]) -> dict[str, Any]:
    w = p.get("w", 0.78e-6)
    length = p.get("l", 0.78e-6)
    return {"Calculate": "R,A", "w": um(w), "l": um(length), "m": str(int(p.get("m", 1)))}


def _tap_cdl(p: dict[str, Any]) -> dict[str, str]:
    return {"w": um(p.get("w", 0.78e-6)), "l": um(p.get("l", 0.78e-6))}


def _via_pcell(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "b_layer": str(p.get("b_layer", "Metal1")),
        "t_layer": str(p.get("t_layer", "Metal2")),
        "vn_columns": int(p.get("columns", 2)),
        "vn_rows": int(p.get("rows", 2)),
    }


def _no_cdl(p: dict[str, Any]) -> dict[str, str]:
    return {}


# --- expected extraction values --------------------------------------------


def _mos_extracted(p: dict[str, Any]) -> dict[str, float]:
    """The deck reports MOS width per finger, not total."""
    w, length = _require(p, "w", "l")
    ng = max(1, int(p.get("ng", 1)))
    return {"w": w / ng, "l": length}


def _res_extracted(p: dict[str, Any]) -> dict[str, float]:
    w, length = _require(p, "w", "l")
    return {"w": w, "l": length, "m": float(int(p.get("m", 1)))}


def _cap_extracted(p: dict[str, Any]) -> dict[str, float]:
    """Capacitors are checked on area, which is what sets C.

    The metal-finger cap snaps w and l onto its finger pitch, so neither
    matches the request exactly while the area — and therefore the capacitance
    — does.
    """
    w, length = _require(p, "w", "l")
    return {"area": w * length}


def _tap_extracted(p: dict[str, Any]) -> dict[str, float]:
    """Taps come back as area and perimeter rather than w/l."""
    w = p.get("w", 0.78e-6)
    length = p.get("l", 0.78e-6)
    return {"a": w * length}


def _ind_extracted(p: dict[str, Any]) -> dict[str, float]:
    d, w = _require(p, "d", "w")
    return {"w": w, "s": p.get("s", 2.1e-6), "d": d, "nr_r": float(int(p.get("nr_r", 1)))}


def _npn_extracted(p: dict[str, Any]) -> dict[str, float]:
    return {"le": p.get("le", 0.9e-6), "we": p.get("we", 0.07e-6)}


# --- registry --------------------------------------------------------------

DEVICE_KINDS: dict[str, DeviceKind] = {
    "nmos_lv": DeviceKind(
        key="nmos_lv",
        pcell="nmos",
        spice_model="sg13_lv_nmos",
        cdl_prefix="M",
        terminals=("D", "G", "S", "sub"),
        to_pcell=_mos_pcell,
        to_cdl=_mos_cdl,
        to_extracted=_mos_extracted,
        implicit_nets=("sub",),
    ),
    "pmos_lv": DeviceKind(
        key="pmos_lv",
        pcell="pmos",
        spice_model="sg13_lv_pmos",
        cdl_prefix="M",
        terminals=("D", "G", "S", "sub"),
        to_pcell=_mos_pcell,
        to_cdl=_mos_cdl,
        to_extracted=_mos_extracted,
        implicit_nets=("sub",),
    ),
    "rppd": DeviceKind(
        key="rppd",
        pcell="rppd",
        spice_model="rppd",
        cdl_prefix="R",
        terminals=("PLUS", "MINUS"),
        to_pcell=_res_pcell,
        to_cdl=_res_cdl,
        to_extracted=_res_extracted,
        bulk_node="sub",
    ),
    "rsil": DeviceKind(
        key="rsil",
        pcell="rsil",
        spice_model="rsil",
        cdl_prefix="R",
        terminals=("PLUS", "MINUS"),
        to_pcell=_res_pcell,
        to_cdl=_res_cdl,
        to_extracted=_res_extracted,
        bulk_node="sub",
    ),
    "rhigh": DeviceKind(
        key="rhigh",
        pcell="rhigh",
        spice_model="rhigh",
        cdl_prefix="R",
        terminals=("PLUS", "MINUS"),
        to_pcell=_res_pcell,
        to_cdl=_res_cdl,
        to_extracted=_res_extracted,
        bulk_node="sub",
    ),
    "cmim": DeviceKind(
        key="cmim",
        pcell="cmim",
        spice_model="cap_cmim",
        cdl_prefix="C",
        terminals=("PLUS", "MINUS"),
        to_pcell=_cmim_pcell,
        to_cdl=_cap_cdl,
        to_extracted=_cap_extracted,
    ),
    "cmomi": DeviceKind(
        key="cmomi",
        pcell="cmomi",
        spice_model="cap_cmomi",
        cdl_prefix="C",
        terminals=("PLUS", "MINUS"),
        to_pcell=_cmomi_pcell,
        to_cdl=_cmomi_cdl,
        to_extracted=_cap_extracted,
    ),
    "npn13G2": DeviceKind(
        key="npn13G2",
        pcell="npn13G2",
        spice_model="npn13G2",
        cdl_prefix="Q",
        terminals=("C", "B", "E", "sub"),
        to_pcell=_npn_pcell,
        to_cdl=_npn_cdl,
        to_extracted=_npn_extracted,
        implicit_nets=("sub",),
    ),
    "inductor": DeviceKind(
        key="inductor",
        pcell="inductor2",
        spice_model="inductor",
        cdl_prefix="L",
        terminals=("PLUS", "MINUS"),
        to_pcell=_ind_pcell,
        to_cdl=_ind_cdl,
        to_extracted=_ind_extracted,
        bulk_node="sub",
    ),
    # Taps have one drawn contact; the second CDL node is the well or substrate
    # they tie to, which is implicit in layout. The PDK testcase form is
    # "R1 net1 WELL1 ntap1 w=.. l=..".
    "ntap1": DeviceKind(
        key="ntap1",
        pcell="ntap1",
        spice_model="ntap1",
        cdl_prefix="R",
        terminals=("PLUS", "WELL"),
        to_pcell=_tap_pcell,
        to_cdl=_tap_cdl,
        to_extracted=_tap_extracted,
        implicit_nets=("WELL",),
    ),
    "ptap1": DeviceKind(
        key="ptap1",
        pcell="ptap1",
        spice_model="ptap1",
        cdl_prefix="R",
        terminals=("PLUS", "WELL"),
        to_pcell=_tap_pcell,
        to_cdl=_tap_cdl,
        to_extracted=_tap_extracted,
        implicit_nets=("WELL",),
    ),
    "via_stack": DeviceKind(
        key="via_stack",
        pcell="via_stack",
        spice_model="",
        cdl_prefix="X",
        terminals=("BOTTOM", "TOP"),
        to_pcell=_via_pcell,
        to_cdl=_no_cdl,
    ),
}


def kind_of(spec: DeviceSpec) -> DeviceKind:
    try:
        return DEVICE_KINDS[spec.kind]
    except KeyError as exc:
        raise KeyError(
            f"Unknown device kind {spec.kind!r}; known: {sorted(DEVICE_KINDS)}"
        ) from exc


def build(spec: DeviceSpec, layout=None):
    """Place ``spec`` as a foundry PCell; returns ``(layout, cell)``."""
    kind = kind_of(spec)
    return instantiate(kind.pcell, kind.to_pcell(spec.params), layout=layout)
