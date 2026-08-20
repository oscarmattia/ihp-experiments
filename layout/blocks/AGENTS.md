# Agent notes — `layout/blocks/`

Matched device groups for the CTLE, and the four RX front-end stage layouts.

**Update this file** when a block's structure, a stage's gates, or a stage floorplan
changes.

## Layout

| Script | Role |
| --- | --- |
| `generators.py` | the five CTLE sub-blocks, each returning a `Block` |
| `mos_array.py` | strapped single-finger MOS arrays, for any wide device |
| `power_ring.py` | vdd/vss ring: TopMetal2 horizontal, TopMetal1 vertical |
| `draw.py` | shared placement and routing primitives (`snap`, `place`, `via_between`, `trunk_net`, …) |
| `stage_gates.py` | `run_stage_gates(...)` — parity, EM, render, DRC, LVS, PEX and JSON summaries |
| `gen_blocks.py` | build all CTLE sub-blocks and gate them on DRC, LVS and PEX |
| `term_stage.py` | `term_dut` — bond pads, ESD, 50 Ω termination (in progress) |
| `ctle_stage.py` | `ctle_dut` — full CTLE stage |
| `vga_stage.py` | `vga_dut` — current-steering VGA (in progress) |
| `driver_stage.py` | `driver_dut` — pad driver (in progress) |

## CTLE sub-blocks

These are the matched groups `ctle_stage.py` composes. Other stages reuse
`mos_array.py`, `power_ring.py` and `draw.py` directly rather than through
`generators.py`.

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

## Stage layouts

All four cells are **device-only** subcircuits gated by `run_stage_gates` against
the matching `circuits/ctle56n/spice/*_pdk.cir`. Floorplans are still being
iterated — record contracts and gate status here, not placement detail that the
next revision will move.

| Cell | Schematic | Ports | Size | Devices | parity | EM | DRC | LVS | PEX |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `term_dut` | `term_pdk.cir` | `inp inn vdd vss` | 314.2 × 291.1 um | 10 | ok | ok | **clean, no waivers** | match | 28 C |
| `ctle_dut` | `ctle_pdk.cir` | `outp outn inp inn vdd vss mgate` | (see summary) | 11 | ok | ok | clean apart from `LBE.a`/`LBE.c` | match | 95 C |
| `vga_dut` | `vga_pdk.cir` | `outp outn inp inn vicm steerp steern vdd vss mgate` | 586.7 × 221.0 um | 15 | ok | ok | 44 left (`CntB.a`, `CntB.a1`, `Gat.b`, `M2.b`, `M2.e`, `M3.b`, `M4.b`, `M5.a`) | **does not match** | 129 C |
| `driver_dut` | `driver_pdk.cir` | `outp outn inp inn vdd vss mgate` | 568.9 × 307.8 um | 13 | ok | ok | 22 left (`Gat.d`, `TM1.b`, `TM2.a`, `TM2.b`, `V2.b`, `V3.b`, `V4.b`) beyond the `LBE.a`/`LBE.c` coil waiver | match | 33 C |

**Contracts worth keeping straight:**

- **`term_dut` has four ports** because the bond pad, ESD diodes, 50 Ω shunt and
  the route to the CTLE are one net — nothing sits between them in the schematic or
  layout. The termination has **no coil**, so it takes **no** DRC waiver at all (the
  CTLE's `LBE.a`/`LBE.c` allowance exists only because of its shunt coils).
- **`vga_dut` and `driver_dut`** expose bias and steering on pins (`mgate`, `vicm`,
  `steerp`, `steern`); whoever instantiates the cell supplies those voltages or
  currents through the testbench `{DUT_BIAS}` tokens in `ngs.py`.
- **Pad capacitance** is parasitic metal, not a CDL device. Layout draws a `bondpad`
  PCell on the net; ngspice gets hand caps from the testbench.

Summaries land under `layout/blocks/out/<stage>/` as `{cell}_summary.json` plus
`parity.json`, `em.json`, and the tool run directories.

## Conventions

- **Mirror, do not translate.** A mirrored pair puts the same neighbour on each
  device's inner side, which is what a differential pair needs. Every pair
  reports 0.000 um of symmetry error.
- **Derive placement pitch from the device box.** `mirror_pitch()` exists because
  a hardcoded 10 um pitch put two load resistors 0.04 um apart.
- **Snap drawn geometry to the 5 nm database grid** (`rules.grid()` via
  `draw.snap`). That is not the same as MOS finger width: the `nmos` PCell floors
  its `w` parameter onto a **0.01 um** grid (`mos_array.MOS_W_GRID`,
  `catalog.MOS_W_GRID`, `size_ctle.MOS_W_GRID_UM`). Wide arrays use that grid;
  routes and boxes use 5 nm.
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

## CTLE stage (`ctle_dut`)

Cell name is **`ctle_dut`**, the same as the subcircuit in
`circuits/ctle56n/spice/ctle_pdk.cir`, because it is meant to be the same cell.
Pins are `outp outn inp inn vdd vss mgate`, in that order.

Symmetric about a single axis. Row order is VDD → L → RD → collector, per
`MEMORY.md`. Bottom to top: the three MOS arrays (bias diode to the left of the
centred tail pair), the guard ring, the centred degeneration network, the HBT pair,
the loads, then the coils.

The coils **face each other**, `M135` left and `R270` right, so each one's pins are
on the edge nearest the axis with **vdd on top** and `nlp` below it. vdd is then a
short strap between two adjacent pins, both bodies extend outward, and the channel
between them carries everything that crosses the coil row. Two rules follow from
the coil and are not tuning knobs:

- **Nothing with a substrate tie may sit inside a coil's footprint.** The
  `pwell_block` marker covers the whole 108 um cell and `PWB.f` wants 0.24 um to
  any p-tap, so the pin row sits a full half-height above the HBTs.
- **Leave a coil pin colinear with its feed.** The deck measures `w`, `s` and `d`
  from the winding inside `ind_drw`; a perpendicular stub at the pin had both coils
  extracting as `w=1.5 um, d=45 um` against a drawn 4 um and 40 um. The strap and
  both `nlp` runs leave at the feed's own width and y and turn only once clear.

The loads stay near the axis rather than under the feeds, so the horizontal run
lands on `nlp`, where the coil's port capacitance already sits and which stays out
of `C_L`; the drawn length is reported against `CL_INTERCONNECT` in `params.inc`.

`outp`/`outn` leave at the **top** edge and `inp`/`inn` at the **bottom**, both on
Metal4, which passes under both ring layers so the grid stays whole. The base taps
go sideways along the base's own bar: the HBT stacks all three terminals at one x
with 0.23 um between base Metal1 and emitter Metal2, so stacks dropped below each
short, and LVS reports the base merged into the emitter.

The ring is **squared about the axis**, 10 um clear of what it encloses, so it is
equidistant from both coils; the bias diode sets the left half-width and the right
side widens to match, which is what puts the diode inside. Both supplies tap it on
the axis — vss down from the source rail, vdd up from the coil strap crossing under
the inner run on TopMetal1. That connection is not optional and not checkable by
LVS: no device touches the ring, so a ring that merely shares a label passes.

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
