# Physical layout flow — IHP SG13G2

How layout is generated and signed off in this repo. Device facts belong in
[PDK.md](PDK.md); tool versions and paths in [ENVIRONMENT.md](ENVIRONMENT.md);
pitfalls in [../MEMORY.md](../MEMORY.md).

## Shape of the flow

Devices are **foundry PCells** from the PDK's own `sg13g2_pycell_lib`.
gdsfactory is used for **composition and electrical routing only**.

That split is deliberate. The PCells are the authoritative geometry and are what
the LVS deck's device extraction is written against, so placing them directly
removes any chance of our geometry disagreeing with the foundry's. gdsfactory
supplies what PCells lack — typed ports, hierarchy and bundle routing — without
having an opinion about what a transistor looks like.

```
DeviceSpec ──► PCell placement ──► GDS ──► PDK DRC deck   ──► JSON verdict
    │                                └──►  PDK LVS deck   ──► JSON verdict
    │                                └──►  Magic extract  ──► parasitic netlist
    └──► CDL netlist ─────────────────────► (LVS reference)
```

A single `DeviceSpec` feeds the PCell parameters, the CDL element line and the
port names, so layout and netlist cannot drift apart silently.

## Install and verify

```bash
./scripts/install-ihp-layout.sh          # KLayout, Magic, netgen, gdsfactory
source ~/.local/share/ihp-eda/env.sh     # sources layout.env.sh automatically
./scripts/verify-ihp-layout.sh           # --quick skips the deck runs
```

`verify-ihp-layout.sh` is the gate. It checks the version triple, starts Magic
and netgen headless against the PDK tech files, runs the DRC and LVS decks on
the PDK's own testcases, and routes two PCell devices through gdsfactory and
puts the result through DRC.

### Tool versions come from the PDK

`$PDK_ROOT/versions.txt` is the single source of truth, and the PDK enforces it:
`run_drc.py` refuses to run when the KLayout binary is older than the pin, with
`Minimum required KLayout version is 0.30.5`. The installer reads the file
rather than hardcoding versions.

Two version facts worth knowing:

- **KLayout is built from source here.** `klayout.org` and `klayout.de` are
  blocked by this environment's egress policy, so the official `.deb` cannot be
  fetched and the installer falls back to a GitHub source build. Add
  `klayout.org` and `klayout.de` to the Cloud Agent network allowlist to get the
  fast path. The build passes `-nolibgit2`, dropping the GUI package manager
  that would otherwise need `git2.h`.
- **The klayout pip module is pinned below what gdsfactory asks for.**
  gdsfactory 9.44 depends on kfactory >= 2.5, which declares `klayout >= 0.30.8`,
  while the PDK pins 0.30.5. The PDK version wins: it is the combination IHP
  tested, and it keeps PCell generation and the DRC binary on the same KLayout.
  The routing spike in verification exercises kfactory end to end so this is
  validated rather than assumed.

## Signoff decks

| Check | Entry point | Wrapper |
| --- | --- | --- |
| DRC | `tech/drc/run_drc.py` | `layout/common/drc.py` |
| LVS | `tech/lvs/run_lvs.py` | `layout/common/lvs.py` |
| PEX | Magic `extract` / `ext2spice` | `layout/common/pex.py` |
| PEX cross-check | `klayout.pex` (R only) | `layout/common/pex.py` |

Both wrappers reduce the tools' output to JSON — a verdict, counts per rule and
a few examples — so an agent never has to scrape a log.

### Do not restrict DRC tables by hand

Passing `-rd tables=...` straight to the deck breaks it:

```
ERROR: In .../ihp-sg13g2.drc: '-': Argument needs to be a DRC layer
```

The connectivity section runs unconditionally and references layers that only
the full table set defines. The PDK's own CI pairs `--table=` with
`--no_feol --no_beol ...` for this reason. Go through `run_drc.py`, which manages
the combinations.

This error is worth naming precisely because it looks like a version problem and
is not: it appears on any KLayout version when the table list is restricted. The
upgrade is still required, but for a different reason — `run_drc.py` refuses to
run below the `versions.txt` pin.

### Judge LVS on the report, not the exit code

`run_lvs.py` exits 0 even when the compare fails. Reading the return code
reported four mismatched blocks and one mismatched device as clean. `lvs.py`
looks for `Netlists match` in the output instead.

### Magic needs a newer version than versions.txt pins

`ihp-sg13g2.tech` carries `requires magic-8.3.617` while `versions.txt` says
8.3.589. Below the tech file's floor its `version` and `cifinput` sections fail to
load and Magic cannot read a GDS at all. The installer takes the higher of the
two.

### Numeric limits come from the PDK

`layout/common/rules.py` reads `rule_decks/sg13g2_tech_default.json`, the same
table the deck loads. Nothing in the layout code should carry a transcribed
limit: a hand-written width table had TopMetal1 at 1.50 um against the 1.64 um
minimum, and the deck only caught it once a route used that metal.

### Electromigration, and the one gap in it

`layout/common/em.py` reads `DCCURRENTDENSITY AVERAGE` from
`libs.ref/sg13g2_stdcell/lef/sg13g2_tech.lef`. That is the **only**
machine-readable source of current limits in this PDK — not the DRC decks, not the
ITF, not the Magic tech files — which has two consequences.

There is **no signoff check** for electromigration here. The limits are LEF
metadata for tools that consume it, so anything drawn in this repo has to be
checked by us. `em.check_segments` does that and the CTLE stage reports it as a
gate alongside DRC and LVS, writing `em.json` next to the layout.

And `Cont` has a resistance but no current limit, so MOS source/drain contact
electromigration cannot be checked from open PDK data. `em.UNCHECKABLE` records
that, and `max_current_a` raises for such a layer rather than returning a number,
so silence cannot pass for a verified limit. The nearest analogue is the resistor
PCells' `ikspec` of 0.11–0.30 mA per contact, which is not stated for MOS
contacts; if it applied it would bind on a wide tail.

| Layer | Limit | 8.67 mA supply needs | Drawn |
| --- | --- | --- | --- |
| Metal2 | 2 mA/um | 4.32 um | 8.64 um (2x derate) |
| Metal5 | 2 mA/um | 1.44 um per tail | 2.88 um |
| TopMetal1 | 15 mA/um | 0.58 um | 6.0 um (min width and corner vias) |
| TopMetal2 | 16 mA/um | 0.54 um | 6.0 um |
| Via1 | 0.4 mA/cut | 8 cuts per tail | 24 per unit terminal |
| TopVia2 | 10 mA/cut | 1 cut | 4 per ring corner |

Sizing uses a 2x derate (`em.DEFAULT_DERATE`) because the current is an
operating-point estimate rather than a worst-case corner; the check itself only
insists on 1x. On the thin metals EM decides the width — the array's previous
hand-picked 1.0 um rail was 45% under what 2.9 mA needs — while on the top metals
the PDK minimum width wins by an order of magnitude, so the ring is
minimum-width limited and runs at 9% of its EM limit.

An EM width is a computed number, so snap it: an unsnapped rail reports
`metal2_drw_Offgrid`.

### Netlist parity

LVS compares layout against the CDL and never compares the CDL against the
schematic, so a device missing from both is invisible. That is how the bias diode
stayed absent from the CTLE layout for several revisions.

`layout/common/parity.py` closes the loop: it resolves the schematic's `{TOKEN}`
parameters from `params.inc`, reduces both netlists to devices keyed by model and
geometry, and reports what only one side has. The CTLE stage runs it **before**
DRC, since there is no point asking whether the layout matches a netlist that does
not match the schematic.

One substitution is permitted, in `MODEL_ALIASES`: `ind_shunt`, an EM-fitted
lumped subcircuit, stands in for the `inductor` PCell, because the PDK has no
ngspice inductor model. Geometry is still compared — `size_ind.py` writes the case
it solved into `ind_shunt.inc`'s header, so `w`, `s` and `d` are read from there.

Parity also constrains the **schematic**. The deck compares MOS `W` and `L` with
essentially no tolerance (`rule_decks/custom_devices.lvs` relaxes only inductors,
5% on `w`/`s`/`d`, and diodes, 1% on `A`/`P`), and a wide MOS is drawn as an array
of 5 nm-snapped single-finger units, so a width that does not land on that grid is
not drawable: 242.988 um against a drawn 243.000 um fails. `size_ctle.py` therefore
emits the drawable width, and parity fails if the two rules ever drift apart.

What the deck accepts for a 25-unit 243 um array, measured: one `w=243u ng=1 m=1`,
one `w=9.72u m=25`, and 25 explicit 9.72 um devices all match, so no
layout-specific restructuring is needed and both netlists carry one device.

Because the cell that is laid out can only contain devices, the CTLE's bias
current source lives in the testbench and `mgate` is a pin; `vss` is a pin too,
rather than the global node `0`, so the port list matches the layout. The VGA,
driver and termination netlists still hold their own sources and need the same
treatment before they can be laid out.

### Context rules

Some rules constrain a cell's *surroundings* and cannot be satisfied by an
isolated device:

| Rule | Why an isolated cell fails it |
| --- | --- |
| `LU.b` / `LU.a` | latch-up wants a substrate tie within 20 um; a lone device has none |
| `LBE.a` | local-back-end minimum width is 100 um, a chip-area marker |
| density | a bare device cannot meet metal density |
| antenna | needs the full connected net |

`layout/common/drc.py` lists these in `CONTEXT_RULES` and reports them
separately rather than dropping them, so `clean` means "no real violations" and
`strictly_clean` is the unfiltered verdict. The classification is evidence
based: running the same deck on the PDK's own device layouts under
`tech/lvs/testing/testcases/unit` trips `LU.b` 35 times on `sg13_lv_nmos`.
Pass `allow_context=False` at block level, where guard rings exist.

## Devices

`layout/common/devices.py` is the registry. Each entry ties a logical device to
its PCell, its CDL element form (taken from the PDK LVS testcases), its terminal
names, and what LVS extraction actually reports back.

Parameters are given in SI base units and translated there, so callers never
hand-format PCell unit strings.

### PCell traps encoded in the registry

- **`library_by_name` needs the technology name.**
  `pya.Library.library_by_name("SG13_dev")` returns `None`; the second argument
  is required.
- **Resistors and capacitors ignore `w`/`l` unless `Calculate` is changed.**
  `rppd` defaults to solving for `l` from a target `R`, `cmim` to solving for
  `w&l` from a target `C`. Passing a geometry without setting `Calculate`
  silently yields the default device.
- **`nmos` clamps `ng` at 100 and snaps finger width to 5 nm.** Asking for 122
  fingers of a 242.988 um device quietly draws 100 fingers of 2.425 um, i.e.
  242.5 um. `catalog.plan_fingers()` caps the count and puts the total on the
  finger grid.
- **The LVS deck reports MOS width per finger**, and reports taps as area and
  perimeter rather than `w`/`l`. `DeviceKind.to_extracted` states what to expect
  per device; capacitors are checked on area, which is what sets `C`.

## Ports

Ports are derived from what the PCells already draw, not hardcoded:

1. pin shapes on the `*_pin` layers mark the terminals,
2. annotation text on `text_drw` (63/0) names them where the PCell writes one —
   `npn13G2` labels its pins `C`, `B` and `E`,
3. otherwise a per-kind rule in `wrap.TERMINAL_RULES` names them by geometric
   order.

Step 3 is safe for the devices that need it because MOS source/drain and the two
terminals of a resistor or capacitor are permutable in the LVS device classes.

Ports advertise the **drawing** layer of their metal, not the pin layer, because
gdsfactory matches a route's cross-section layer against the port layer.

### Net labels

Net names go on the `*_text` layer of the metal a pin sits on (Metal1 is 8/25,
Metal2 10/25). They are not strictly required — the PDK's own testcase layouts
carry none, and the deck matches on connectivity with
`--ignore_top_ports_mismatch` — but they make the compare able to check the port
mapping, and they matter once several devices share a cell.

There is no `gatpoly_text` layer, so a MOS gate net can only be named where the
gate reaches metal. `gen_devices.py` records such terminals under
`unlabelled_terminals` in the manifest.

## Routing

```python
from layout.common.route import device_component, route_electrical
c = device_component(spec)
route_electrical(top, [a.ports["MINUS"]], [b.ports["PLUS"]], metal="Metal1")
```

**Every bundle stays on one metal.** A metal change needs a via stack built to
SG13G2 rules, which gdsfactory knows nothing about, so `route_electrical`
refuses a bundle whose ports are on another layer and points at the `via_stack`
PCell instead of letting gdsfactory invent a taper. `auto_taper` is off for the
same reason.

`layout/common/xsection.py` registers a minimal gdsfactory PDK: cross-section
factories per metal (`metal_routing` aliases TopMetal2, the thick RF metal), the
`wire_corner`/`straight`/`taper` primitives that electrical routing resolves by
name, and a `LayerMap` of the routing stack.

Two gdsfactory details that cost time:

- **`activate_pdk()` must run before any other gdsfactory call.** gdsfactory
  resolves its default PDK from `$PDK`, which `env.sh` sets to `ihp-sg13g2` for
  the SPICE flow. That is not an importable Python module, so `get_active_pdk()`
  raises until a PDK is activated explicitly.
- **Cross-sections must be registered as factories**, not instances, or
  kfactory raises `'CrossSection' object is not callable`.

The routing `LayerMap` is the one hand-written layer table in the flow, because
gdsfactory needs its members at class-definition time. Everything else parses
`rule_decks/layers_def.drc`. `layers.validate_routing_layers()` compares the two
and verification fails on drift.

## Power grid and metal budget

The CTLE stage assigns metals so that no two structures contend for one layer:

| Use | Metal | Why |
| --- | --- | --- |
| Device source/drain buses | Metal2 | reachable from the PCell's Metal1 in one via |
| Differential nets (e1, e2, outp, outn) | Metal5 | top minus two, clear of both ring layers |
| nlp (coil to load) | TopMetal1 | has to cross under the vdd strap |
| vdd strap, vss riser, ring horizontals | TopMetal2 | thick RF metal |
| Ring verticals | TopMetal1 | crossing the other net's horizontals without a short |

`layout/blocks/power_ring.py` draws the ring: two nets side by side, horizontal on
TopMetal2, vertical on TopMetal1, stitched at the corners with TopVia2. Splitting
the directions across two layers is what makes it a grid rather than four bars —
a horizontal run and a vertical run of the same net can cross the other net's run.

Two constraints that decide the ring's geometry:

- **Top vias are single large-area cuts.** `via_stack` draws one TopVia2 cut per
  instance; its row and column parameters only multiply thin-metal vias lower in a
  stack. So cut count is instance count, and the instances have to fit inside the
  square where the two runs overlap — which is what sets the conductor width.
- **Vias must stay inside that overlap square.** Spread along or across the run
  they push a landing pad past the conductor edge and into the spacing to the
  neighbouring net.

The substrate is tied to `vss` by landing **one** via inside a guard-ring tap's own
Metal1. The taps sit at a pitch, so the ring is not one continuous piece of Metal1
and is a single net only through the substrate; `add_guard_ring` returns
`tap_centres_um` for this. A strap drawn along the ring edge instead leaves a
0.1 um sliver against the tap pads and reports Metal1 and contact spacing all along
the row.

### The coil dictates the floorplan

The `inductor` cell is 108 um square and its `pwell_block` marker covers all of it,
while `PWB.f` wants 0.24 um between that marker and any p-tap. So **nothing with a
substrate tie can sit inside a coil's footprint**, and coils must be oriented with
their 108 um body extending into empty area — R0 and its mirror keep the pins on
the bottom edge and the body above. Rotating them to face each other put the
markers over the HBT row and both devices' substrate ties reported the violation.

It also sets the width: two coils side by side need their feeds about 62 um from
the symmetry axis before their bodies clear each other. The CTLE keeps its loads
near the axis instead of under the feeds, so the long horizontal run lands on
`nlp` — the node between coil and load — where the coil's port capacitance already
sits and which stays out of `C_L`. The drawn length is reported in the stage
summary against the `CL_INTERCONNECT` budget in `params.inc`.

### Wide devices

`layout/blocks/mos_array.py` builds a wide MOS as an array of single-finger units,
because the `nmos` PCell provides no strapping (`ng > 1` extracts as series
devices), ignores `m` in layout, and silently caps one finger near 10 um.

Each unit's source and drain carries a **full-length single-column via stack**
rather than a few cuts at one end: it distributes the contact along the stripe, and
at 0.21 um the column sits on the 0.16 um Metal1 stripe without reaching the gate
poly. Row count is derived by measuring the PCell's own via pitch, so it tracks a
resize.

Two things to get right when strapping:

- **The gate needs a real contact.** The PCell leaves it as bare poly, so a gate
  net cannot leave the device at all. Cont is exactly 0.16 um (`Cnt.a` is a maximum
  too), with 0.07 um of poly (`Cnt.d`) and 0.05 um of Metal1 (`M1.c1`) around it.
  Metal drawn near the strap without a Cont looks connected and is not.
- **A shared supply rail grows away from the devices.** It is much wider than one
  array's own rail, and centred on that rail the extra width reaches into the drain
  via columns, shorting the drains to the source rail.

## Running it

```bash
source ~/.local/share/ihp-eda/env.sh

python layout/devices/gen_devices.py     # GDS, CDL, ports, manifest, PNGs
python layout/devices/run_drc.py         # DRC every device
python layout/devices/run_lvs.py         # LVS every device
./layout/run_all.sh                      # everything, against committed goldens
```

Outputs land under `layout/devices/out/`: `gds/`, `cdl/`, `png/`,
`manifest.json`, `specs.json`, and per-device `drc/` and `lvs/` JSON plus
`drc_summary.json` / `lvs_summary.json`. Tool scratch directories
(`drc_run/`, `lvs_run/`, `pex_run/`) are gitignored.
