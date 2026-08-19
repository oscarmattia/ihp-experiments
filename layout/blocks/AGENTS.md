# Agent notes — `layout/blocks/`

Matched device groups for the CTLE stage, and the stage itself.

**Update this file** when a block's structure, its gates, or the stage floorplan
changes.

## Layout

| Script | Role |
| --- | --- |
| `generators.py` | the five sub-blocks, each returning a `Block` |
| `mos_array.py` | strapped single-finger MOS arrays, for any wide device |
| `gen_blocks.py` | build all blocks and gate them on DRC, LVS and PEX |
| `ctle_stage.py` | the full stage: symmetric placement plus interconnect |

## Blocks

| Block | Contents | Status |
| --- | --- | --- |
| `hbt_diff_pair` | two `npn13G2` mirrored, p-tap ring | DRC+LVS clean |
| `rppd_load_pair` | the two shunt-peaked loads, mirrored | DRC+LVS clean |
| `degeneration_network` | `rsil` ‖ `cmomi`, joined with via stacks | DRC+LVS clean |
| `nmos_tail_pair` | two strapped 243 um arrays, p-tap ring | DRC+LVS clean |
| `shunt_coil` | the EM-characterized coil | DRC+LVS clean |

Blocks run DRC with **context rules enforced**, because a block has guard rings
and a shared substrate tie. The tail pair, which trips `LU.b` as a bare device,
comes back with zero violations of any kind once ringed. Only the coil keeps an
allowance, for `LBE.a` alone.

## Conventions

- **Mirror, do not translate.** A mirrored pair puts the same neighbour on each
  device's inner side, which is what a differential pair needs. Every pair
  reports 0.000 um of symmetry error.
- **Derive placement pitch from the device box.** `mirror_pitch()` exists because
  a hardcoded 10 um pitch put two load resistors 0.04 um apart.
- **Snap everything to the 5 nm grid.** Distributing guard-ring taps evenly
  produced offgrid violations on five layers at once.
- **Wide MOS devices go through `mos_array.py`.** The PCell provides no finger
  strapping; see `../devices/AGENTS.md`.
- **LVS with `disable_tap_extraction=True` whenever a block has a guard ring.**
  Ring taps are layout-only and the deck would otherwise extract hundreds of tap
  devices no netlist declares.
- **Route vertical-leg-first.** When a device brings both terminals out at the
  same y, a horizontal-first run passes over the other terminal's via pad and
  shorts the two nets through an intermediate metal of the stack.
- **Place via stacks on a stub outside the device.** A stack's landing pads are
  wider than a pin; dropping one on a pin pushes contact and via spacing rules
  against the device's own geometry.

## CTLE stage

Symmetric about a single axis, with every row centred on it and the two halves
exact mirror images. The coils are rotated to face each other (`M135` on the left,
`R270` on the right), which puts both supply feeds at the same height so **vdd is
one straight TopMetal2 strap**, with the nlp feeds below it running straight down
to the loads. Row order is VDD → L → RD → collector, per `MEMORY.md`.

The guard ring covers the active rows only and stops below the coils: a substrate
ring around an inductor is wrong on its own terms, and wrapping the coils tripped
p-well block and contact-bar rules against their markers.

### Current state and what remains

Placement, the supply strap and the nlp nets are done, and every net in the
extracted netlist is electrically distinct — the earlier shorts are gone.

Two things are still open, both narrow:

1. **DRC**: `CntB.h1` and `PWB.f` violations remain, from the guard-ring taps
   interacting with neighbouring wells rather than from the signal routing.
   Neither responds to ring clearance, so the tap arrangement itself needs work.
2. **LVS**: the compare does not match yet. `inp`/`inn` are not routed out to the
   boundary, so the base nets have no path to a port.

The differential nets are hand-drawn trunks with stubs. That works but every
change risks a new interaction, and each of the shorts fixed so far was found by
the deck rather than by inspection. The better structure is
`gf.routing.route_bundle_electrical`, already proven against this PDK in
`../spike_routing.py`: import the placement, add Metal3 ports at the via-stack
landings, and hand it the device boxes as obstacles so collisions are checked
rather than hoped for.
