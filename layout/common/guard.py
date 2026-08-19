"""Guard rings built from foundry tap PCells.

A guard ring is what turns the latch-up rule from a context violation into a
satisfied one: ``LU.b`` wants a pSD-PWell tie within 20 um of any N+ active
inside the p-well, and an isolated device has none. Rings are assembled by
tiling the ``ptap1``/``ntap1`` PCells, so the tap geometry itself stays the
foundry's.
"""

from __future__ import annotations

from dataclasses import dataclass

from layout.common.devices import build
from layout.common.spec import DeviceSpec

#: Default tap size, matching the PCell minimum.
TAP_W = 0.78e-6
TAP_L = 0.78e-6

#: Gap from the enclosed geometry to the inside edge of the ring, in um.
DEFAULT_CLEARANCE = 1.5

#: Maximum distance from N+ active to a p-well tie that LU.b allows, in um.
#: A ring closer than this on all four sides satisfies the rule for anything it
#: encloses that is not itself wider than the limit.
LATCHUP_MAX_DISTANCE = 20.0


@dataclass(frozen=True)
class RingSpec:
    """A guard ring around a rectangular area."""

    kind: str = "ptap1"
    clearance: float = DEFAULT_CLEARANCE
    #: Tap pitch in um; taps are abutted with this spacing centre to centre.
    pitch: float = 1.4


def add_guard_ring(
    layout,
    cell,
    inner_box,
    ring: RingSpec | None = None,
) -> dict:
    """Tile tap PCells into a ring around ``inner_box``.

    ``inner_box`` is a ``DBox`` in ``cell`` coordinates. Returns a summary with
    the tap count and the ring's outer box.
    """
    from layout.common.pdk import pya_module

    pya = pya_module()
    ring = ring or RingSpec()

    tap_layout, tap_cell = build(
        DeviceSpec(name=f"{ring.kind}_unit", kind=ring.kind, params={"w": TAP_W, "l": TAP_L})
    )
    # Bring the tap into this layout once, then array it.
    tap_index = layout.add_cell(tap_cell.name)
    layout.cell(tap_index).copy_tree(tap_cell)
    tap_box = layout.cell(tap_index).dbbox()

    left = inner_box.left - ring.clearance - tap_box.width()
    right = inner_box.right + ring.clearance
    bottom = inner_box.bottom - ring.clearance - tap_box.height()
    top = inner_box.top + ring.clearance

    count = 0
    x = left
    while x <= right:
        for y in (bottom, top):
            cell.insert(
                pya.DCellInstArray(tap_index, pya.DTrans(pya.DVector(x - tap_box.left, y - tap_box.bottom)))
            )
            count += 1
        x += ring.pitch

    y = bottom + ring.pitch
    while y < top:
        for x_edge in (left, right):
            cell.insert(
                pya.DCellInstArray(
                    tap_index, pya.DTrans(pya.DVector(x_edge - tap_box.left, y - tap_box.bottom))
                )
            )
            count += 1
        y += ring.pitch

    outer = pya.DBox(left, bottom, right + tap_box.width(), top + tap_box.height())
    # Worst case for LU.b is the point inside the ring furthest from any tie.
    # Ties run along all four sides, so that point is the centre and its
    # distance is set by the *narrower* dimension: a long thin device is close
    # to the top and bottom ties everywhere along its length. Using the wider
    # dimension here previously reported 141 um for a cell that DRC passes.
    worst_distance = min(inner_box.width(), inner_box.height()) / 2.0 + ring.clearance
    return {
        "kind": ring.kind,
        "taps": count,
        "clearance_um": ring.clearance,
        "pitch_um": ring.pitch,
        "outer_box_um": [outer.left, outer.bottom, outer.right, outer.top],
        "worst_distance_to_tie_um": round(worst_distance, 3),
        "latchup_limit_um": LATCHUP_MAX_DISTANCE,
        "within_latchup_limit": worst_distance <= LATCHUP_MAX_DISTANCE,
    }
