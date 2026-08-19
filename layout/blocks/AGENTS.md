# Agent notes — `layout/blocks/`

Matched device groups for the CTLE stage, and the stage itself.

**Update this file** when a block's structure, its gates, or the stage floorplan
changes.

## Layout

| Script | Role |
| --- | --- |
| `generators.py` | the five sub-blocks, each returning a `Block` |
| `mos_array.py` | strapped single-finger MOS arrays, for any wide device |
| `power_ring.py` | vdd/vss ring: TopMetal2 horizontal, TopMetal1 vertical |
| `gen_blocks.py` | build all blocks and gate them on DRC, LVS and PEX |
| `ctle_stage.py` | the full stage: symmetric placement, power grid, interconnect |

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
- **Wire widths come from `common/em.py`.** Every drawn power and bus conductor is
  sized from the technology LEF at the operating-point current, at a 2x derate, and
  the block carries `em_segments` so the gate can check it. Snap the result: an EM
  width is a computed number and lands off-grid.
- **A shared supply rail grows away from the devices.** Centred on an array's own
  source rail, the extra width reaches into the drain via columns and shorts the
  drains to the rail.
- **One device per array in the CDL.** The extractor merges drawn parallel units
  into a single device of the total width, so `MosArray.total_spec` is the netlist
  form — and it is the same form the schematic uses.

## CTLE stage

Cell name is **`ctle_dut`**, the same as the subcircuit in
`circuits/ctle56n/spice/ctle_pdk.cir`, because it is meant to be the same cell.
Pins are `outp outn inp inn vdd vss mgate`, in that order.

Symmetric about a single axis. Row order is VDD → L → RD → collector, per
`MEMORY.md`. Bottom to top: the three MOS arrays (bias diode to the left of the
centred tail pair), the guard ring, the centred degeneration network, the HBT pair,
the loads, then the coils.

The floorplan is dictated by the coil, not chosen. Its cell is 108 um square and
its `pwell_block` marker covers all of it, so:

- Coils go **R0 and its mirror**, pins on the bottom edge and body extending up
  into empty area. Rotating them to face each other put the markers over the HBT
  row and `PWB.f` fired on both devices' substrate ties.
- The feeds sit ~62 um from the axis, which is what two coils need to clear each
  other, while the loads stay near the axis. That puts the long horizontal run on
  `nlp`, where the coil's port capacitance already sits and which stays out of
  `C_L`; the drawn length is reported against `CL_INTERCONNECT` in `params.inc`.
- `nlp` crosses on **TopMetal1**, because the vdd strap has to span between the two
  supply feeds and the loads sit inside that span.

The guard ring encloses the **NMOS arrays only**, and one via lands inside a tap's
own Metal1 to make the substrate be `vss` — which is what lets the netlist say the
MOS bulk is `vss`. A substrate ring around an inductor is wrong on its own terms.

Degeneration is centred: the resistor is `R270` so p faces left and n right, and
the capacitor is `R90` so its `feed=same` terminals come out of its bottom edge.
`feed=same` stays (MEMORY.md: `feed=double` self-resonates at 30–47 GHz), so p and
n are separated by metal — Metal4 to the resistor's left terminal, Metal5 to its
right — rather than by orientation.

### Gates

Five, all reported as JSON next to the layout:

| Gate | Result | File |
| --- | --- | --- |
| parity | 11 devices match, ports match | `parity.json` |
| EM | every conductor within its LEF limit | `em.json` |
| DRC | clean apart from `LBE.a`/`LBE.c` | `drc_run/` |
| LVS | netlists match | `lvs_run/` |
| PEX | 95 C totalling 141.6 fF | `pex_run/` |

Parity runs **first**: there is no point asking whether the layout matches a
netlist that does not match the schematic. `LBE.a` and `LBE.c` are chip-area
back-end markers on the coils and are the only rules an isolated stage is allowed
to trip; every other context rule, latch-up included, is enforced.

Magic extracts capacitance only here, because `extresist` segfaults on DC-shorted
ports and a coil is one continuous piece of TopMetal2.
