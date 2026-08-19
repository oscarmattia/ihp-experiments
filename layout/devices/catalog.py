"""The device catalog: every device the front end actually instantiates.

Sizes are read from ``circuits/ctle56n/spice/params.inc`` rather than copied,
so a resize in ``size_ctle.py`` flows into layout without touching this file.
The catalog also carries a few ``char/mos`` sweep corners, so the MOS geometry
that the LUTs were characterized at is covered by DRC and LVS too.
"""

from __future__ import annotations

import math

from layout.common.sizing import metres, read_params
from layout.common.spec import DeviceSpec

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

#: Per-finger width snaps to this grid, so a total width that does not divide
#: onto it is silently rounded down. Choosing the total to land on the grid
#: keeps drawn width equal to requested width.
MOS_W_GRID = 5e-9

#: EM-characterized shunt-peaking coil. MEMORY.md records ~66 pH at 28 GHz for a
#: 1-turn TopMetal2 octagon at this geometry, which is what ``ind_shunt.inc``
#: was fitted from and what ctle_pdk.cir instantiates.
COIL = {"d": 40.0e-6, "w": 4.0e-6, "s": 2.1e-6, "nr_r": 1}


def plan_fingers(
    total_w: float, finger_w: float = TAIL_FINGER_W, max_ng: int = MOS_MAX_NG
) -> tuple[int, float]:
    """Pick a finger count and an achievable total width.

    Returns ``(ng, drawable_total_w)``. The total is adjusted onto the PCell's
    finger-width grid so the device the PCell draws is exactly the device asked
    for, rather than a silently rounded-down one.
    """
    ng = min(max_ng, max(1, int(math.ceil(total_w / finger_w))))
    per_finger = round(total_w / ng / MOS_W_GRID) * MOS_W_GRID
    return ng, per_finger * ng


def ctle_devices(params: dict[str, float] | None = None) -> list[DeviceSpec]:
    """Devices instantiated by the CTLE stage, at their sized geometry."""
    p = params or read_params()

    target_w = metres(p, "MOS_W")
    tail_l = metres(p, "MOS_L")
    ng, tail_w = plan_fingers(target_w)

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
        DeviceSpec(
            name="nmos_tail",
            kind="nmos_lv",
            params={"w": tail_w, "l": tail_l, "ng": ng},
            note=(
                f"CTLE tail/mirror device, W={tail_w * 1e6:.3f} um total in {ng} "
                f"fingers of {tail_w / ng * 1e6:.3f} um, L={tail_l * 1e6:.2f} um; "
                f"sizing asked for {target_w * 1e6:.3f} um "
                f"({(tail_w / target_w - 1) * 100:+.3f}% on the finger-width grid)"
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


def full_catalog(params: dict[str, float] | None = None) -> list[DeviceSpec]:
    return [*ctle_devices(params), *support_devices(), *char_mos_corners()]
