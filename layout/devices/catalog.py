"""The device catalog: every device the front end actually instantiates.

Sizes are read from the stage ``*_params.inc`` files rather than copied, so a
resize in the sizing scripts flows into layout without touching this file. The
catalog also carries a few ``char/mos`` sweep corners, so the MOS geometry that
the LUTs were characterized at is covered by DRC and LVS too.
"""

from __future__ import annotations

import math
from pathlib import Path

from layout.common.paths import repo_root
from layout.common.sizing import metres, read_params
from layout.common.spec import DeviceSpec

_SPICE = repo_root() / "circuits" / "ctle56n" / "spice"

#: Per-finger width for the fingered tail devices, in metres. The tail is
#: ~243 um wide in total, which cannot be drawn as one finger; MEMORY.md records
#: that the tail must stay saturated, and finger width does not change that, so
#: this is purely a drawing choice: wide enough to keep gate resistance low,
#: narrow enough that the cell stays roughly square.
TAIL_FINGER_W = 2.0e-6

#: The nmos PCell silently clamps ``ng`` here. Asking for more fingers leaves
#: the drawn geometry at 100 while the implied total width grows, so the device
#: ends up narrower than requested with no error: at ng=122 and w=242.988u the
#: PCell draws 100 fingers of 2.425u, i.e. 242.5u.
MOS_MAX_NG = 100

#: Per-finger width grid for the nmos PCell — same value as
#: ``layout/blocks/mos_array.MOS_W_GRID``.
MOS_W_GRID = 0.01e-6

#: EM-characterized shunt-peaking coil. MEMORY.md records ~66 pH at 28 GHz for a
#: 1-turn TopMetal2 octagon at this geometry, which is what ``ind_shunt.inc``
#: was fitted from and what ctle_pdk.cir instantiates.
COIL = {"d": 40.0e-6, "w": 4.0e-6, "s": 2.1e-6, "nr_r": 1}


def plan_fingers(
    total_w: float, finger_w: float = TAIL_FINGER_W, max_ng: int = MOS_MAX_NG
) -> tuple[int, float]:
    """Pick a finger count and an achievable total width.

    Kept for the finger-limit arithmetic it documents, but no longer used to
    build the tail: a multi-finger PCell instance draws no straps and extracts as
    a chain of transistors, so wide devices go through
    ``layout.blocks.mos_array`` instead.

    Returns ``(ng, drawable_total_w)``. The total is adjusted onto the PCell's
    finger-width grid so the device the PCell draws is exactly the device asked
    for, rather than a silently rounded-down one.
    """
    ng = min(max_ng, max(1, int(math.ceil(total_w / finger_w))))
    per_finger = math.floor(total_w / ng / MOS_W_GRID + 1e-12) * MOS_W_GRID
    return ng, per_finger * ng


def ctle_devices(params: dict[str, float] | None = None) -> list[DeviceSpec]:
    """Devices instantiated by the CTLE stage, at their sized geometry."""
    p = params or read_params()

    target_w = metres(p, "MOS_W")
    tail_l = metres(p, "MOS_L")
    from layout.blocks.mos_array import plan_units

    units, unit_w = plan_units(target_w)

    return [
        DeviceSpec(
            name="rppd_load",
            kind="rppd",
            params={"w": p["RPPD_W"], "l": p["RPPD_L"]},
            note=(
                f"CTLE shunt-peaked load, {p['RPPD_R']:.2f} ohm realized "
                f"(RD target {p['RD']:.0f} ohm)"
            ),
        ),
        DeviceSpec(
            name="rsil_degen",
            kind="rsil",
            params={"w": p["RSIL_W"], "l": p["RSIL_L"]},
            note=f"CTLE emitter degeneration, RS={p['RS']:.2f} ohm",
        ),
        DeviceSpec(
            name="cmomi_cs",
            kind="cmomi",
            params={
                "w": p["CMOMI_W"],
                "l": p["CMOMI_L"],
                "mmin": int(p["CMOMI_MMIN"]),
                "mmax": int(p["CMOMI_MMAX"]),
                # feed=same is electrically required at 28 GHz per MEMORY.md;
                # feed=double resonates.
                "feed": "same",
            },
            note=f"CTLE degeneration cap, CS={p['CS'] * 1e15:.1f} fF, feed=same",
        ),
        DeviceSpec(
            name="npn13G2_pair_device",
            kind="npn13G2",
            params={"Nx": int(p["Nx"])},
            note=f"CTLE differential pair HBT, Nx={int(p['Nx'])}",
        ),
        # The tail *unit*, not the whole tail. The foundry nmos PCell draws no
        # source/drain straps and caps a single finger near 10 um, so the sized
        # 243 um device cannot exist as one PCell instance: it is an array of
        # these units with drawn straps, built by layout/blocks/mos_array.py and
        # verified there. Keeping the unit here is what makes the catalog a list
        # of devices the deck can actually match one-to-one.
        DeviceSpec(
            name="nmos_tail_unit",
            kind="nmos_lv",
            params={"w": unit_w, "l": tail_l, "ng": 1},
            note=(
                f"CTLE tail unit, W={unit_w * 1e6:.3f} um x L={tail_l * 1e6:.2f} um; "
                f"{units} of these make the {units * unit_w * 1e6:.1f} um tail "
                f"(sizing asked for {target_w * 1e6:.3f} um)"
            ),
        ),
        DeviceSpec(
            name="inductor_turn1_d40",
            kind="inductor",
            params=dict(COIL),
            note=(
                "shunt-peaking coil at the EM-characterized geometry "
                f"(~{p.get('L_EM', 0) * 1e12:.1f} pH at 28 GHz per ind_shunt.inc)"
            ),
        ),
    ]


def term_devices(params: dict[str, float] | None = None) -> list[DeviceSpec]:
    """Devices instantiated by the termination stage, at their sized geometry."""
    p = params or read_params(_SPICE / "term_params.inc")

    return [
        DeviceSpec(
            name="rsil_term",
            kind="rsil",
            params={"w": p["RSIL_W"], "l": p["RSIL_L"]},
            note=(
                f"Termination 50 ohm shunt per leg, RSRC_LEG={p['RSRC_LEG']:.0f} ohm "
                f"(term_params.inc RSIL_W/L)"
            ),
        ),
        DeviceSpec(
            name="rppd_vtt_top",
            kind="rppd",
            params={"w": p["VTT_RTOP_W"], "l": p["VTT_RTOP_L"]},
            note=(
                "VTT divider upper leg to VDD "
                f"(term_params.inc VTT_RTOP_W/L, w={p['VTT_RTOP_W'] * 1e6:.1f} um "
                f"l={p['VTT_RTOP_L'] * 1e6:.1f} um)"
            ),
        ),
        DeviceSpec(
            name="rppd_vtt_bot",
            kind="rppd",
            params={"w": p["VTT_RBOT_W"], "l": p["VTT_RBOT_L"]},
            note=(
                "VTT divider lower leg to VSS "
                f"(term_params.inc VTT_RBOT_W/L, w={p['VTT_RBOT_W'] * 1e6:.1f} um "
                f"l={p['VTT_RBOT_L'] * 1e6:.1f} um)"
            ),
        ),
        DeviceSpec(
            name="cmim_vtt_decap",
            kind="cmim",
            params={"w": p["VTT_CAP_W"], "l": p["VTT_CAP_L"]},
            note=(
                f"VTT rail MIM decap, CL={p['CL'] * 1e15:.2f} fF "
                f"(term_params.inc VTT_CAP_W/L)"
            ),
        ),
    ]


def vga_devices(params: dict[str, float] | None = None) -> list[DeviceSpec]:
    """Devices instantiated by the VGA stage, at their sized geometry."""
    p = params or read_params(_SPICE / "vga_params.inc")
    from layout.blocks.mos_array import plan_units

    tail_w = metres(p, "MOS_W")
    tail_l = metres(p, "MOS_L")
    steer_w = metres(p, "STEER_W")
    steer_l = metres(p, "STEER_L")
    tail_units, tail_unit_w = plan_units(tail_w)
    steer_units, steer_unit_w = plan_units(steer_w)

    return [
        # Mirror diode and both tail devices share MOS_W_m; rppd loads, the HBT
        # pair and the shunt coil match CTLE catalog entries and are omitted.
        DeviceSpec(
            name="nmos_vga_wide_unit",
            kind="nmos_lv",
            params={"w": tail_unit_w, "l": tail_l, "ng": 1},
            note=(
                f"VGA mirror and tail unit, W={tail_unit_w * 1e6:.3f} um x "
                f"L={tail_l * 1e6:.2f} um; {tail_units} of these make the "
                f"{tail_units * tail_unit_w * 1e6:.1f} um wide devices "
                f"(vga_params.inc MOS_W_m, sizing asked for {tail_w * 1e6:.3f} um)"
            ),
        ),
        DeviceSpec(
            name="nmos_vga_steer_unit",
            kind="nmos_lv",
            params={"w": steer_unit_w, "l": steer_l, "ng": 1},
            note=(
                f"VGA steering device unit, W={steer_unit_w * 1e6:.3f} um x "
                f"L={steer_l * 1e6:.2f} um; {steer_units} of these make the "
                f"{steer_units * steer_unit_w * 1e6:.1f} um steer pair "
                f"(vga_params.inc STEER_W_m, sizing asked for {steer_w * 1e6:.3f} um)"
            ),
        ),
    ]


def driver_devices(params: dict[str, float] | None = None) -> list[DeviceSpec]:
    """Devices instantiated by the pad driver stage, at their sized geometry."""
    p = params or read_params(_SPICE / "driver_params.inc")
    from layout.blocks.mos_array import plan_units

    mirror_w = metres(p, "MOS_W")
    mirror_l = metres(p, "MOS_L")
    tail_w = p["TAIL_W_m"]
    mirror_units, mirror_unit_w = plan_units(mirror_w)
    tail_units, tail_unit_w = plan_units(tail_w)

    return [
        # rsil loads match rsil_term; coils, ESD and the clamp are in esd_devices.
        DeviceSpec(
            name="npn13G2_driver",
            kind="npn13G2",
            params={"Nx": int(p["Nx"])},
            note=f"Pad driver differential pair HBT, Nx={int(p['Nx'])} (driver_params.inc)",
        ),
        DeviceSpec(
            name="nmos_driver_mirror_unit",
            kind="nmos_lv",
            params={"w": mirror_unit_w, "l": mirror_l, "ng": 1},
            note=(
                f"Pad driver mirror diode unit, W={mirror_unit_w * 1e6:.3f} um x "
                f"L={mirror_l * 1e6:.2f} um; {mirror_units} of these make the "
                f"{mirror_units * mirror_unit_w * 1e6:.1f} um mirror "
                f"(driver_params.inc MOS_W_m, sizing asked for {mirror_w * 1e6:.3f} um)"
            ),
        ),
        DeviceSpec(
            name="nmos_driver_tail_unit",
            kind="nmos_lv",
            params={"w": tail_unit_w, "l": mirror_l, "ng": 1},
            note=(
                f"Pad driver tail unit, W={tail_unit_w * 1e6:.3f} um x "
                f"L={mirror_l * 1e6:.2f} um; {tail_units} of these make the "
                f"{tail_units * tail_unit_w * 1e6:.1f} um tail "
                f"(driver_params.inc TAIL_W_m, sizing asked for {tail_w * 1e6:.3f} um)"
            ),
        ),
    ]


def esd_devices() -> list[DeviceSpec]:
    """ESD and bond-pad devices used by the termination and driver stages."""
    pad_diameter = 70.0e-6
    return [
        DeviceSpec(
            name="esd_diodevdd_2kv",
            kind="esd_diodevdd",
            params={"model": "diodevdd_2kv", "m": 1},
            note="VDD-side ESD diode for termination and driver pads (term_pdk.cir)",
        ),
        DeviceSpec(
            name="esd_diodevss_2kv",
            kind="esd_diodevss",
            params={"model": "diodevss_2kv", "m": 1},
            note="VSS-side ESD diode for termination and driver pads (term_pdk.cir)",
        ),
        DeviceSpec(
            name="esd_nmoscl_2",
            kind="esd_nmoscl",
            params={"model": "nmoscl_2", "m": 1},
            note="VDD/VSS rail clamp shared by termination and driver (term_pdk.cir)",
        ),
        DeviceSpec(
            name="bondpad_70um",
            kind="bondpad",
            params={"diameter": pad_diameter},
            note=(
                "stacked TM1+TM2 bond pad at 70 um diameter "
                f"({pad_diameter * 1e6:.0f} um per size_term.py and size_driver.py)"
            ),
        ),
    ]


def support_devices() -> list[DeviceSpec]:
    """Guard-ring taps and a via stack, needed once blocks are assembled."""
    return [
        DeviceSpec(name="ntap1_guard", kind="ntap1", params={"w": 0.78e-6, "l": 0.78e-6},
                   note="n-tap for guard rings"),
        DeviceSpec(name="ptap1_guard", kind="ptap1", params={"w": 0.78e-6, "l": 0.78e-6},
                   note="p-tap for guard rings"),
        DeviceSpec(name="cmim_decap", kind="cmim", params={"w": 10e-6, "l": 10e-6},
                   note="MIM decap of the kind the termination stage uses"),
    ]


def char_mos_corners() -> list[DeviceSpec]:
    """MOS geometries the char/mos LUT sweeps were taken at.

    ``char/mos/ihp_sweep.py`` characterizes W = 1 um, ng = 1 across a set of
    channel lengths, so those are the devices the sizing scripts trust.
    """
    lengths_um = (0.13, 0.18, 0.25, 0.5)
    out = []
    for length in lengths_um:
        tag = f"{length:g}".replace(".", "p")
        out.append(
            DeviceSpec(
                name=f"nmos_char_l{tag}",
                kind="nmos_lv",
                params={"w": 1.0e-6, "l": length * 1e-6, "ng": 1},
                note=f"char/mos LV sweep corner L={length} um, W=1 um, ng=1",
            )
        )
    return out


def _device_key(spec: DeviceSpec) -> tuple:
    return (spec.kind, tuple(sorted(spec.params.items())))


def full_catalog(params: dict[str, float] | None = None) -> list[DeviceSpec]:
    # One entry per distinct (kind, params): the gates check drawable geometry,
    # not how many schematic instances share it. Earlier groups win on collision
    # so CTLE keeps rppd_load, npn13G2_pair_device and inductor_turn1_d40 when
    # a later stage sizes the same PCell the same way.
    groups = [
        ctle_devices(params),
        term_devices(),
        vga_devices(),
        driver_devices(),
        esd_devices(),
        support_devices(),
        char_mos_corners(),
    ]
    seen: set[tuple] = set()
    out: list[DeviceSpec] = []
    for group in groups:
        for spec in group:
            key = _device_key(spec)
            if key in seen:
                continue
            seen.add(key)
            out.append(spec)
    return out
