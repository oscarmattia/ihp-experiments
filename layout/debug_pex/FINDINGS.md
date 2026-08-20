# Why Magic reported negative substrate capacitance

The CTLE stage's PEX came back with nine negative capacitors, worst -85 fF, on
`mgate`, `e1`, `e2` and on the MOS arrays' bus rails. A negative capacitance is not
a rounding problem to tolerate: ngspice accepts one without complaint and it
silently moves the AC result, so it had to be understood before anything was
simulated post-layout.

The two probes here are what settled it, and they are kept runnable because the
question recurs. Both write only to a scratch directory.

```bash
source ~/.local/share/ihp-eda/env.sh
python layout/debug_pex/probe_unit_sweep.py          # does it scale with device count?
python layout/debug_pex/probe_extract_settings.py    # is it how we asked?
python layout/debug_pex/probe_driver_pad_bw.py       # driver 91→35 GHz: pad model, not ESD
python layout/debug_pex/probe_standalone_pad.py      # pad 80 fF; +ESD column 102 fF; in-situ 144
```

## It is not the device count

First hypothesis: Magic subtracts device area from a node's substrate term because
the compact model will supply it through `AS`/`AD`/`PS`/`PD`, and with enough
parallel units the subtraction exceeds the node's own metal-to-substrate
capacitance. That predicts the substrate term falls with unit count and crosses
zero.

`probe_unit_sweep.py` says no. A bare strapped array is clean at every size, and a
bare metal plate with no devices under it is clean too:

| case | caps | total | drain node | negatives |
| --- | --- | --- | --- | --- |
| 1 unit | 6 | 4.85 fF | 2.11 fF | none |
| 5 units | 6 | 23.14 fF | 12.22 fF | none |
| 25 units | 6 | 120.60 fF | 65.72 fF | none |
| metal plate, no devices | 1 | 6.59 fF | - | none |

Monotonic and positive throughout.

## It is not our cthresh either

`_magic_script` in [../common/pex.py](../common/pex.py) sets `ext2spice cthresh 0`
to keep every coupling term, which made it a natural suspect: if the substrate
residual is what is left after subtracting coupling, keeping all of it should push
the residual down.

`probe_extract_settings.py` holds the geometry fixed and varies only the
extraction:

| setting | caps | total | negatives | worst |
| --- | --- | --- | --- | --- |
| `cthresh 0` | 98 | 135.07 fF | 9 | -85.23 fF |
| `cthresh 0.01` | 93 | 135.06 fF | 9 | -85.23 fF |
| `cthresh 1` | 34 | 120.97 fF | 9 | -85.23 fF |
| `hierarchy off` | 34 | 700.26 fF | **0** | - |
| `hierarchy off`, `cthresh 1` | 15 | 695.53 fF | **0** | - |

`cthresh` does not move the negatives at all. Byte-identical values at 0, 0.01
and 1.

## It is the hierarchy

Flat extraction has zero negative terms. The substrate terms flip to plausible
positives:

| net | hierarchical | flat |
| --- | --- | --- |
| `mgate` | -85.23 fF | +146.54 fF |
| `e1` | -41.07 fF | +65.97 fF |
| `e2` | -40.87 fF | +66.10 fF |

The negatives name the mechanism themselves. Inside the array cells the same value
repeats once per instance, on the drain and source bus rail nodes:

```
m2_n576_2380#  sub  -35.544 fF     (x3, one per array cell)
m2_n576_n1012# sub  -34.926 fF     (x3)
```

So Magic computes a rail's substrate capacitance in the parent, subtracts coupling
attributed to the 25 child unit cells, and the residual goes negative. The
top-level nets inherit it.

Only the stage was ever affected. [../devices/run_pex.py](../devices/run_pex.py)
flattens its GDS through `write_for_magic` first, so Magic never saw a hierarchy
there; the stage passed its own hierarchical GDS straight to `run_magic_pex`.

### Do not quote a ratio between the two totals

The flat total is about five times the hierarchical one on the stage, which is easy
to report as "hierarchy lost 80% of the capacitance". That is not a meaningful
statement: the hierarchical total has negative terms inside it, so the ratio
measures how negative those were rather than how much capacitance went missing. On
the minimal case below the hierarchical total is negative outright, and a ratio to a
negative number means nothing at all.

Two separate things also have to be kept apart. A hierarchical netlist states a
subcell's parasitics **once** in its `.subckt` and then instantiates it N times, so
a textual sum over capacitor lines under-reports by roughly the instance count even
when the extraction is perfect. `probe_hierarchy_total.py` reports both a textual
sum and one expanded by instance count, so that effect cannot be mistaken for the
real one.

## The minimal case: one NMOS, N instances

`probe_hierarchy_total.py` puts a single labelled NMOS in a subcell, instantiates it
N times in a top cell, and extracts three ways: the subcell alone, the top cell
hierarchically, and the top cell flat.

| N | sub alone | N x sub | top flat | top hier (expanded) | hier negative terms |
| --- | --- | --- | --- | --- | --- |
| 1 | 2.607 fF | 2.607 fF | **2.607 fF** | 1.352 fF | 2 |
| 2 | 2.607 fF | 5.215 fF | **5.657 fF** | -2.068 fF | 4 |
| 4 | 2.607 fF | 10.430 fF | **11.757 fF** | -3.694 fF | 8 |
| 8 | 2.607 fF | 20.859 fF | **23.957 fF** | -6.945 fF | 16 |

Flat extraction is verifiably right:

- At N=1 it equals the subcell extracted on its own, to the last digit.
- Above that it is `N x sub + (N-1) x 0.442 fF`. The residual per adjacent pair is
  0.442, 0.4423 and 0.4426 fF at N=2, 4 and 8 — a constant neighbour coupling, which
  is what a row of identical cells at a fixed pitch should produce.

Hierarchical extraction is wrong at **every** N, including N=1, where it reports
1.352 fF against the correct 2.607 fF and already emits two negative terms. The
negative count scales as 2N.

So the conclusion is not "hierarchy loses capacitance in proportion". It is that
hierarchical extraction with this PDK's extract deck does not produce a capacitance
at all once a cell has subcells, and flat extraction agrees with the sum of the
parts plus a physically sensible coupling term.

## Consequences, all now in the code

1. **Extraction is flat.** `_magic_script` appends `ext2spice hierarchy off`, with
   these numbers recorded in its docstring.
2. **Nothing is clamped.** There is no unexplained residual left, so no negative
   value is fudged to zero anywhere.
3. **The pipeline detects it.** `PexResult.physical` is false when any capacitor is
   negative, so this class of error is a gate rather than something a reader is
   expected to notice in a JSON summary.
4. **Flat output has no `.subckt` line at all**, just a bare deck, so anything
   needing an includable subcircuit supplies the header and port list itself.

## A second problem, found on the way

Flat extraction fixes the capacitance but exposes how Magic handles a strapped
array:

```
X2  vss mgate e1 vss sg13_lv_nmos ad=3.3048p   pd=20.12u    as=3.3048p   ps=20.12u
X3  vss mgate e1 vss sg13_lv_nmos ad=0.25869n  pd=2.26392m  as=53.31775p ps=0.50858m
X4  vss mgate e1 vss sg13_lv_nmos ad=0 pd=0 as=0 ps=0
...  (X4 through X26 all zero)
```

The 25 units share drain and source nets, so Magic dumps the merged node's whole
diffusion onto one arbitrary instance and gives the other 24 zero. In aggregate
that would be about right, except the PDK model file states:

```
* if as = 0, calculate value, else take it
```

`as=0` means "estimate it from w and l", not "no junction here". So the result is
24 estimated junctions on top of one measurement of the whole array.

The KLayout LVS deck gets this right, because it merges the array into the single
device it really is:

```
M$9 e2 mgate vss vss sg13_lv_nmos L=1u W=243u AS=82.62p AD=82.62p PS=503u PD=503u
```

Hence the split both post-layout flows use: **devices from the LVS extraction,
capacitance from Magic.** Restricting Magic's capacitors to nets both views share
keeps 493 fF and discards 3 fF (0.6%), all of it on resistor body nodes whose
parasitics the compact model already covers.

## Two further traps, found while wiring the flows up

**`write_for_magic` merges nets on a block.** The CTLE simulation view, which
LVS-matches its own CDL, extracted with `e1`, `e2` and `vss` collapsed into one
node: all 75 array devices read `d=e2 s=e2`, and `e1` and `vss` were absent from the
deck entirely. Passing the same GDS straight to `run_magic_pex` gives the correct
three distinct arrays. Flatten in the netlist, not in the layout.

The tell was asymmetry. `nlp1` and `nlp2` both coupled to `e2` with byte-identical
values, and so did `outp` and `outn` — which a differential layout cannot do. A
correct extraction gives matched pairs instead (`mgate-e1` 11.406 fF against
`mgate-e2` 11.383 fF). Worth checking symmetry before believing any parasitic set
from a symmetric layout.

**Extracted ports are not the interface.** The LVS netlist lists only nets a
*device* touches. In the reduced view nothing touches `vdd`, because both coils are
black-boxed, so `vdd` is missing from its `.SUBCKT` line. Building the post-layout
core from that list discarded 137 fF of supply capacitance — 134.78 fF of it the
power ring's coupling to `vss` — and left the wrapper connecting `ind_shunt` to a
`vdd` that was not the layout's `vdd`. The core has to be built from the intended
interface, with the extracted list checked against it so a net that should have been
promoted surfaces as an error rather than as missing parasitics.

## A total is not a load: where the 493 fF actually goes

Quoting the deck total against `CL_INTERCONNECT` compares unrelated quantities, and
overstates the problem by more than an order of magnitude.
`probe_signal_net_caps.py` splits it up.

| | fF | share |
| --- | --- | --- |
| between the supply nets and substrate only | 134.78 | 27.1% |
| touching a signal net (`outp`/`outn`/`inp`/`inn`) | 55.24 | 11.1% |
| other internal nets (`e1`, `e2`, `mgate`, `nlp`) | 306.42 | 61.7% |

The largest single term is `mgate`-`vss` at 146.54 fF -- the gate strap across 75
array devices -- followed by `vdd`-`vss` at 134.78 fF, the power ring. Neither loads
a signal path. `e1` and `e2` carry ~61 fF each, which does matter, but as
degeneration-node loading rather than output loading.

Per signal node:

| net | extracted | vs the 6.80 fF `CL_INTERCONNECT` allowance |
| --- | --- | --- |
| `outp` | 15.264 fF | 2.2x |
| `outn` | 15.190 fF | 2.2x |
| `inp` | 11.938 fF | - |
| `inn` | 12.884 fF | - |

So the routing allowance is out by 2.2x on the outputs, not by the 70x a deck total
would imply. The testbench still applies `CL` post-layout, so these add to it.

## Standalone wire versus the same wire in situ

The interesting comparison is the drawn trunk against an isolated wire of the same
layer, width and length, extracted the same way:

| net | metal | w | length | standalone | fF/um | sizing at 0.17 fF/um |
| --- | --- | --- | --- | --- | --- | --- |
| `outp`/`outn` | Metal4 | 2.88 um | 135.60 um | 11.550 fF | 0.0852 | 23.05 fF |
| `inp`/`inn` | Metal4 | 2.88 um | 97.22 um | 8.328 fF | 0.0857 | 16.53 fF |

Two effects pull in opposite directions, and separating them is the point:

- **Metal4 is half as capacitive per um as the sizing assumes**: 0.085 fF/um
  measured against the 0.17 fF/um in `size_ctle.ROUTING_CAP_FF_PER_UM`. That figure
  is a mid-range value for this stack; a high metal sits far enough from the
  substrate to come in well under it.
- **The drawn length is 3.4x the budget**: 135.6 um against the 40 um behind
  `CL_INTERCONNECT`. That is the real cost, and it follows directly from bringing
  `outp`/`outn` out to the top edge past a 108 um coil and a power ring.

Length wins, so the net result is over budget either way.

In situ against standalone gives what the surroundings add:

| net | standalone | in situ | ratio |
| --- | --- | --- | --- |
| `outp` | 11.550 fF | 15.264 fF | 1.3x |
| `outn` | 11.550 fF | 15.190 fF | 1.3x |
| `inp` | 8.328 fF | 11.938 fF | 1.4x |
| `inn` | 8.328 fF | 12.884 fF | 1.5x |

Neighbour coupling adds 30-50% over an isolated wire.

One asymmetry worth chasing: `outp` and `outn` match to 0.5%, but `inp` and `inn`
differ by 7.9% (11.94 against 12.88 fF) despite identical trunk geometry and
identical standalone values. The difference is environmental, and the input trunks
run within a few um of the degeneration capacitor, so the suspect is the cap's
internal finger structure not being mirror-symmetric about the axis.

## Resistance extraction: what works, what does not (deferred, not fixed)

Recorded rather than repaired, by decision. Three measurements on the CTLE
simulation view.

**The `extresist` segfault is gone.** On the tape-out view it crashed, because the
coil is one continuous piece of TopMetal2 and its two ports are a DC short. With the
coil black-boxed, `extract do resistance` / `extresist all` runs to completion on
every cell and writes `.res.ext` files, and `run_magic_pex(resistance=True)` returns
`ok=True` with `resistance_extracted=True`. So the guard-ring taps, the other
DC-shorted structure named in the fallback comment, are not enough to trigger it.

**But no resistance reaches the netlist.** `ext2spice extresist on` emits **zero**
`R` lines, both flat and hierarchical, even with `rthresh 0`. The `.res.ext` files
exist, so the extraction pass ran; it is the netlist writing that drops it. That
looks like an invocation detail in how `extresist` results are handed to
`ext2spice`, not a fundamental limit, so it is probably fixable.

**Enabling the resistance pass perturbs the capacitance by ~11%.** Flat, same
geometry, same 39 capacitors: 496.43 fF with `resistance=False` against 549.37 fF
with `resistance=True`. `extract do resistance` re-partitions nodes at resistive
elements, which redistributes the capacitance. So a capacitance-only flow should
leave resistance off rather than extracting both and ignoring one, which is what
`run_postlayout.py` does.

Consequence for the flows: Magic supplies capacitance only. Resistance has to come
from `klayout.pex`, which already has the hard part solved — `klayout_wire_resistance`
in [../common/pex.py](../common/pex.py) needs explicit port points, and a generated
layout knows every terminal coordinate. That work is open.

## Method notes worth reusing

- **Compare merged polygons, not bounding boxes.** An early connectivity check
  using bounding boxes made a floating power ring look connected, because a coil's
  octagon bbox swallows everything inside it.
- **Take the geometry from git when bisecting.** A concurrent regeneration
  otherwise changes the thing under test half way through the experiment.
- **Watch the parser as well as the tool.** A throwaway regex anchored at end of
  line silently skipped the eight capacitors Magic annotates `$ **FLOATING`, which
  undercounted the flat total as 212 fF instead of 700 fF and nearly inverted the
  conclusion about how much capacitance hierarchy was losing.
- **Never quote a deck total as a load.** It is dominated by whatever has the most
  area, which on a cell with a power ring is the supply, and on this one is the bias
  gate strap. Sum the terms touching the node you care about.
- **Bound a geometry search by shape, not by a tolerance derived from the shape.**
  Locating trunks with `abs(centre.x - trunk_x) < max(width, 0.5)` matched the
  degeneration capacitor's 37 um Metal5 plate for four different nets at once,
  because the tolerance grew with the polygon. Requiring a narrow width first fixed
  it.

## Driver post-layout BW is the pad model, not missing ESD

`probe_driver_pad_bw.py` against the committed Magic wrapper. The schematic
already has both pieces of the load: a 27.68 fF hand `PAD_C` (TM1 area to
substrate only — `sg13g2_bondpad.lib` is empty) and the `diodevdd_2kv` +
`diodevss_2kv` compact pair at **50.9 fF**. Magic zeros `PAD_C` and keeps those
same ESD subcircuits, then adds extracted metal C.

The 819 fF Magic total is the usual deck-total trap. Per-node:

| Node | Extracted C | Role |
| --- | --- | --- |
| `mgate` | 282 fF | MOS gate strap — not `C_L` |
| `em` | 242 fF | tail/emitter — not `C_L` |
| `outp` / `outn` | 147 fF each | **this is the load** |
| `inp` / `inn` | 21 fF each | input wiring |
| `nlp*` | 7.5 fF each | coil port, already out of `C_L` |

Of the 147 fF on `outp`, **143.56 fF is `outp`–`vss`**. The collector-to-pad
feed and Miller terms are 2.56 fF (`nlp1`–`outp`) + 0.68 fF (`em`–`outp`) +
0.05 fF (`inp`–`outp`). Wiring is not the story.

Schematic AC, ESD compact models left in, only `PAD_C` swept `[sim]`:

| `PAD_C` | DC | 28 GHz | f_−3dB |
| --- | --- | --- | --- |
| 0 (ESD only) | −0.83 dB | +0.46 dB | **127.69 GHz** |
| 27.68 fF (hand, committed schematic) | −0.83 dB | +0.24 dB | **90.69 GHz** |
| 143.56 fF (Magic `outp`–`vss`) | −0.83 dB | −1.59 dB | **35.60 GHz** |
| Magic extracted DUT | −0.83 dB | −1.67 dB | **34.88 GHz** |

`PAD_C=0` gets *wider*, so the schematic is not missing the ESD junctions.
Putting the Magic pad-to-vss term on the schematic lands 0.7 GHz from the
extracted DUT; the leftover is the 3 fF of feed coupling. Effective `C_L`
goes 78.6 → 194.5 fF/side (2.47×) and BW falls 90.7 → 35.6 GHz (2.55×),
which is the RC ratio, not \(1/\sqrt{C}\).

The hand 27.68 fF is `70 × 70 × 5.649 aF/µm²` — TM1 plate to substrate, no
fringe, no stack. Magic's 144 fF is **not** that pad sitting in free space.
`probe_standalone_pad.py` extracts the same `bondpad_70um` PCell alone at
**80.45 fF** to substrate. Tying the driver's ESD column onto that pad
adds **21 fF** of `pad`–`vss` (101.6 fF). A TM2 `vss` ring at 6 um adds
4 fF. The remaining ~42 fF of the in-situ 144 fF is still the rest of the
pad band (dual-net ring, Metal5 feed, neighbour pad), not the pad stack
and not proximity to unconnected ESD. Do not retune the cell to the hand
number. The isolated PCell is still 3× the hand 27.7 fF.

## Standalone `bondpad_70um` is 80 fF, not 144 fF

Same Magic C-only flow as the driver wrapper. Catalog pad: 70 um octagon,
`bottomMetal=3`, `topMetal=TM2`, `stack=t`.

| Case | Pad-node | `pad`–`vss` |
| --- | --- | --- |
| `bondpad_70um` alone | 80.45 fF to sub | — |
| same + ESD column placed, no bar | 80.45 fF | 80.45 fF (vss ≡ sub; no extra) |
| same + ESD column tied (M2 bar + M5) | 114.32 fF | **101.56 fF** |
| ESD column alone (two diodes + PAD bar) | 33.00 fF | 19.31 fF |
| same pad + TM2 `vss` ring, 6 um gap | 81.67 fF | 3.98 fF |
| TM1 70×70 square (hand geometry) | 38.15 fF | — |
| Metal3 70×70 square | 67.55 fF | — |
| Driver in-situ `outp`–`vss` | 146.86 fF | **143.56 fF** |

Proximity does nothing: an unconnected ESD column leaves pad-node at 80.45 fF
and adds no `pad`–`esd_pad` term. Shorting the column onto the pad (the
driver's Metal2 PAD bar) adds the column's own metal — 19 fF of `pad`–`vss`
on the column alone, 21 fF once it sits next to the pad. That is **extracted
metal**, on top of the 50.9 fF ESD *junction* C the compact models already
supply in both schematic and post-layout.

144 − 102 = **42 fF** still unaccounted. That is not the pad and not the ESD
column.
