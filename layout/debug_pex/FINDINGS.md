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

Flat extraction has zero negative terms, and five times the total capacitance, so
hierarchical extraction was not merely mislabelling capacitance -- it was losing
most of it. The substrate terms flip to plausible positives:

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
