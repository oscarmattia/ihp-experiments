# IHP SG13G2 — PDK notes for agents

Condensed, **verified** device/model knowledge for `ihp-sg13g2`, so an agent does not have to
re-read the PDK tree on every task. Read this before writing netlists or sizing scripts.

- Tools, install, and activation: [ENVIRONMENT.md](ENVIRONMENT.md)
- Simulator / flow pitfalls: [../MEMORY.md](../MEMORY.md)

**Provenance tags** used below: `[model]` = read from a PDK model file, `[sim]` = measured with
ngspice on this machine, `[EM]` = from our openEMS runs in `char/passive/`, `[est]` = estimate that
still needs verification.

## Where things live

`PDK_ROOT=$IHP_EDA_ROOT/IHP-Open-PDK`, `PDK=ihp-sg13g2`. The repo's `pdk/` path is **gitignored and
empty** — the installed tree above is the only PDK.

| What | Path (under `$PDK_ROOT/$PDK/libs.tech/`) |
| --- | --- |
| ngspice models | `ngspice/models/` |
| OSDI binaries | `ngspice/osdi/` |
| `.spiceinit` (loads OSDI) | `ngspice/.spiceinit` |
| Verilog-A sources | `verilog-a/<device>/<device>.va` |
| openEMS workflow | `openems/openems_ihp_sg13g2/workflow/` |
| Inductor synthesis | `palace/more_examples/inductor_synthesis_no_external_library/` |
| Magic extract deck (parasitic caps) | `magic/ihp-sg13g2-extract.tech` |
| Layer stack: thickness, `ER`, sheet R | `parasitics/itf/sg13g2_typ.itf` |
| Metal sheet resistance (LVS) | `klayout/tech/lvs/rule_decks/res_extraction.lvs` |
| KLayout LVS/DRC decks | `klayout/tech/` |

PDK checkout on this machine: branch `dev`, `970a7688`.

## Model inclusion and OSDI

Corner files are `.LIB` wrappers; include the **section**, not the raw model file:

```spice
.lib '$PDK_ROOT/ihp-sg13g2/libs.tech/ngspice/models/cornerHBT.lib'    hbt_typ
.lib '$PDK_ROOT/ihp-sg13g2/libs.tech/ngspice/models/cornerMOSlv.lib' mos_tt
.lib '$PDK_ROOT/ihp-sg13g2/libs.tech/ngspice/models/cornerRES.lib'   res_typ
.lib '$PDK_ROOT/ihp-sg13g2/libs.tech/ngspice/models/cornerCAP.lib'   cap_typ
.lib '$PDK_ROOT/ihp-sg13g2/libs.tech/ngspice/models/cornerDIO.lib'   dio_tt
```

Verified section names `[model]`:

- `cornerHBT.lib`: `hbt_typ`, `hbt_bcs`, `hbt_wcs`, `hbt_typ_stat`, `*_mismatch`
- `cornerMOSlv.lib` / `cornerMOShv.lib`: `mos_tt`, `mos_ss`, `mos_ff`, `mos_sf`, `mos_fs`, `mos_tt_stat`, `*_mismatch`
- `cornerRES.lib`: `res_typ`, `res_bcs`, `res_wcs`, `res_stat`, `*_mismatch`
- `cornerCAP.lib`: `cap_typ`, `cap_bcs`, `cap_wcs`, `cap_typ_stat`, `*_mismatch`
- `cornerDIO.lib`: `dio_tt`, `dio_ss`, `dio_ff`, `dio_tt_stat`
- `cornerMOSCAP.lib`: `moscap_tt`, `moscap_tt_stat`, `moscap_tt_mismatch`

**OSDI is mandatory** for MOS, resistors, and MoM caps. `~/.spiceinit` is symlinked to the PDK
`.spiceinit`, which loads `psp103`, `psp103_nqs`, `r3_cmc`, `mosvar`, `cap_cmomi`, `cap_cmomf`
`[model]`. If a sim runs with a different `HOME` or in a sandbox that hides `~/.spiceinit`, copy the
PDK `.spiceinit` into the run directory — `char/passive/ihp_cap_sweep.py` already does this
(`_spiceinit_src`). Symptom of a missing OSDI load: "unknown model type" on `rppd`/`rsil`/`cap_cmomi`.

The **HBT needs no OSDI** — `npn13G2` is built-in VBIC (`.model … npn level=9`) `[model]`.

## Devices

### HBT (`sg13g2_hbt_mod.lib`)

| Item | Value `[model]` |
| --- | --- |
| Subckts | `npn13G2`, `npn13G2l`, `npn13G2v` (+ `_5t` thermal-node variants), `pnpMPA` |
| Ports | `c b e bn` (`_5t`: `c b e bn t`); `pnpMPA`: `c b e` |
| Params | `Nx` (emitter multiplicity), `Ny`, `le`, `we`, `dtemp`, `selft`, `sw_nqs` |
| Geometry | `npn13G2`: `le=0.96u`, `we=0.12u`; `npn13G2l`/`v`: `le=2.50u` |
| LVS extraction | reports **`Nx` as a geometry parameter** — do not fold it into `m`; the deck testcase `npn_adv.cdl` writes `Nx=2 m=1` |
| Internal instance | `Qnpn13G2` → probe as `@q.<path>.qnpn13g2[ic]` |
| VBIC soft limits | `vbe_max=1.6`, `vce_max=1.6`, `vbc_max=5.1` |
| **Emitter resistance** | `re = 7.13*(4/Nx)` Ω → **28.5 Ω at `Nx=1`** |

**The intrinsic emitter resistance self-degenerates the device and caps stage gain.** At `Nx=1` those
28.5 Ω sit inside the device in series with every emitter and cannot be shunted by any external element.
Consequence, confirmed two independent ways: the LUT reports `gm/Ic = 9.90 V⁻¹` against the ideal bipolar
`1/VT = 38.7 V⁻¹`, a 3.9x reduction that back-solves to 26.1 Ω `[sim]`. So a `Nx=1` stage is already
degenerated roughly 4:1 before any designed `Rs`, and its maximum gain is
`gm_i*RD/(1 + gm_i*re)`, not `gm*RD`. `re` scales as `1/Nx`, so gain per stage improves substantially with
a larger emitter at proportionally more current and input capacitance — a chain-wide sizing decision, not a
per-stage one.

`vce_max = 1.6 V` is the practical ceiling that sets the CML supply, not a "BVceo ≈ 1.6 V" figure —
see the correction in [../MEMORY.md](../MEMORY.md).

### MOS (`sg13g2_moslv_mod.lib`, `sg13g2_moshv_mod.lib`)

| Item | Value `[model]` |
| --- | --- |
| Subckts (LV) | `sg13_lv_nmos`, `sg13_lv_pmos`; ESD clamps `nmoscl_2`, `nmoscl_4` |
| Ports | `d g s b` (clamps: `VDD VSS`) |
| Params | `w=0.35u`, `l=0.34u`, `ng`, `m`, `as/ad/pd/ps`, `rfmode`, `pre_layout`, `wmin=0.15u` |
| Model | PSP103 via `psp103.osdi`; `rfmode=1` pulls NQS (`psp103_nqs.osdi`) |
| Current probe | **`ids`**, not `id`: `@n.<path>.nsg13_lv_nmos[ids]` |

`nmoscl_2` is a fixed-geometry rail clamp (`l=0.36u`, `w=168u`), only `m`/`trise`/`rfmode` vary.

### Resistors (`resistors_mod.lib`, R3_CMC via `r3_cmc.osdi`)

| Subckt | `rsh` typ `[model]` | Ports | Notes |
| --- | --- | --- | --- |
| `rsil` | 7.0 Ω/sq | `1 2 bn` | best 50 Ω device |
| `rppd` | 260 Ω/sq | `1 2 bn` | has `b` (bends), `ps` |
| `rhigh` | 1360 Ω/sq | `1 2 bn` | `l >= 0.96u` |

`w`/`l` are in **metres**. Tie `bn` to `0`. `ptap1`/`ntap1` are fixed ~262.8 Ω, not sheet-scaled.

**Do not size resistors from `rsh * L/W`.** Head/contact resistance dominates narrow devices and R
does not scale with the `L/W` ratio `[sim]`:

| Instance | `rsh*L/W` | ngspice, `res_typ`, 27 °C |
| --- | --- | --- |
| `rsil w=0.5u l=2.35u` | 32.9 Ω | **50.26 Ω** |
| `rsil w=2.0u l=9.4u` (same ratio) | 32.9 Ω | 37.24 Ω |
| `rppd w=5u l=0.9u` | 46.8 Ω | 60.77 Ω |

Use `char/passive/out/sg13_{rsil,rppd,rhigh}.npz` or a quick `op` sim instead.
**50 Ω termination: `rsil w=0.5u l=2.35u` → 50.3 Ω** `[sim]`.

### Capacitors

| Device | File | Ports | Density | Notes |
| --- | --- | --- | --- | --- |
| `cap_cmim` | `capacitors_mod.lib` | `PLUS MINUS` | `cap_carea = 1.5e-15` F/µm² `[model]` | MIM, `w`/`l` default 7u, series `R1=55m`; densest |
| `cap_rfcmim` | `capacitors_mod.lib` | `PLUS MINUS bn` | polynomial fit | `w`,`l` 7–75u, `wfeed` 1–30u |
| **`cap_cmomi`** | `cap_cmomi.lib` + OSDI | `PLUS MINUS` | 0.82 / 1.09 / **1.36** fF/µm² for N=3/4/5 metal layers `[model]` | **the metal-only interdigitated finger cap** |
| `cap_cmomf` | `cap_cmomf.lib` + OSDI | `PLUS MINUS` | `(mmin==1 ? 0.372 : 0.305) + (N-1)*0.305` fF/µm² `[model]` | fringe MoM, **low-frequency only**, not silicon-validated |
| `sg13_moscap_n/p` | `sg13g2_moscap_mod.lib` | `G SUB` / `G NW` | ~2.7 fF/µm² at 0 V `[sim]` | PSP103 MOSCAP |
| `cparasitic` | `capacitors_mod.lib` | — | scaled by `cap_cpara` | extraction wrapper, not a drawn device |

`cap_cmomi` details that matter:

```spice
.subckt cap_cmomi PLUS MINUS w=5e-6 l=5e-6 mmin=1 mmax=5 feed=double subblock=0 mm_ok=1
```

- Unit cell 0.84 × 0.89 µm; `active_x = floor(l/0.84)`, `active_y = floor(w/0.89) - 1` (one row is
  consumed by the feed), so the realized density is below the nominal figure `[model]`.
- The wrapper ties the Verilog-A `SUB` node to **global `0`** — substrate shunt lands on ground.
- `mm_ok` is a NO-OP; there is **no corner or mismatch model**, so it returns the same nominal C in
  `cap_typ`/`bcs`/`wcs` and in Monte Carlo. Treat corner results as nominal.
- RF branches are lumped fits valid to ~50 GHz. With the default `feed=double`, **large devices
  (l ≈ 60 µm, or ~30 × 30) self-resonate in-band, ~30–47 GHz**. `feed=same`/`none` stay capacitive
  well past 50 GHz. For 28 GHz signal-path work, prefer `feed=same` or keep the device small.

Measured `[sim]`, `cap_typ`, `w=l=12u`, effective C from `imag(Y11)/ω`:

| Config | C @ 1 GHz | C @ 28 GHz |
| --- | --- | --- |
| `mmin=1 mmax=5 feed=double` | 170.8 fF | 176.4 fF |
| `mmin=1 mmax=5 feed=same` | 187.3 fF | 188.2 fF |
| `mmin=2 mmax=5 feed=same` | 151.4 fF | 152.1 fF |

So ~150 fF of metal-only finger cap ≈ **12 × 12 µm on M2–M5**, and 1.19 fF/µm² is the realized M1–M5
density at that size (below the 1.36 nominal, as expected from the feed row).

### Inductors — none in ngspice

There is **no ngspice inductor model or subcircuit anywhere** in `ngspice/models/` `[model]`
(grep-verified). `inductor` (Magic `inductor2`) and `inductor3` exist only as layout PCells / LVS
devices with `w`, `s`, `d`, `nr_r`, `m` (`wmin=2u`, `smin=2.1u`, `dmin=25.35u`; `inductor3` needs an
even turn count). Magic's own comment: "there is no direct extraction of inductors in magic".

To use an inductor in a simulation you must go through EM and fit a lumped model. Our openEMS results
`[EM]` (`char/passive/out/ind_summary.csv`):

| Case | Geometry | L @ 10 GHz | Q peak | Trust |
| --- | --- | --- | --- | --- |
| `l2n0` | PDK `L_2n0_twoport.gds`, 2 turns, w=s=3u, d=114u | 2.022 nH | 21.0 @ 9.6 GHz | production |
| `turn1` | synthesized 1-turn octagon, w=s=3u, d=120u | 0.215 nH | 23.2 @ 30 GHz | experimental, plausible |
| `turn2` | synthesized 2-turn octagon, w=s=3u, d=150u | **−12.24 nH** | 0.0 | **broken — do not use** |

`turn2` is recorded with `em_completed: True` but returns negative L and zero Q: the port
de-embedding is wrong for that case. Scale from `turn1` for small coils (tens of pH) and re-run EM for
the actual geometry before trusting a number.

### Diodes and ESD

| Subckt | File | Ports | Notes `[model]` |
| --- | --- | --- | --- |
| `diodevdd_2kv` / `_4kv` | `sg13g2_esd.lib` | `VDD PAD VSS` | PAD→VDD diode + VSS→VDD `dsub`; fixed `DEV_A=35`, `DEV_P=58.08` (2 kV); only `m` scales |
| `diodevss_2kv` / `_4kv` | `sg13g2_esd.lib` | `VDD PAD VSS` | VSS→PAD diode + `dsub` |
| `dantenna` / `dpantenna` | `diodes.lib` | `1 2` | `l=w=780n`; area/perimeter/corner triplet; reverse-bias model only |
| `schottky_nbl1` | `sg13g2_dschottky_nbl1_mod.lib` | `A C S` | scalable `l`, `w`, `Nx`, `Ny` |
| `isolbox` | `diodes.lib` | `isosub NWell bn` | `l=w=3u` |
| `nmoscl_2` / `_4` | `sg13g2_moslv_mod.lib` | `VDD VSS` | rail clamp |

`idiodevdd_*` / `idiodevss_*` and `esd_ptap` exist only in LVS/qucs-s, **not** in ngspice.

**Layout PCell `esd`** (`sg13g2_pycell_lib`): one cell, `model` parameter selects the
device (`diodevdd_2kv`, `diodevss_2kv`, `diodevdd_4kv`, `diodevss_4kv`, `nmoscl_2`,
`nmoscl_4`). Terminal order matches the ngspice subckts above. In this repo
`layout/common/devices.py` registers `esd_diodevdd`, `esd_diodevss` and
`esd_nmoscl` kinds that all instantiate this PCell; CDL uses the `D` prefix with
only `m` as a parameter.

Junction caps: `diodevdd_mod cj0=8.716e-16`, `diodevss_mod cj0=9.42e-16` (per unit area, `area=35`)
`[model]`. Measured pad load of one `diodevdd_2kv` + one `diodevss_2kv` pair at PAD = 1.4 V,
VDD = 1.65 V: **50.9 fF, with 1.15 pA of DC leakage** `[sim]`. That is the dominant capacitance on a
high-speed input, and it is the reason a 50 Ω shunt termination is what makes such a pad usable at
28 GHz.

### Bond pad

**ngspice:** `sg13g2_bondpad.lib` is an **empty** `.subckt bondpad PAD` with
`size=80u shape=0 padtype=0` and no electrical content `[model]`. Pad capacitance must
be hand-modelled from the layer-to-substrate values below or from PEX of a drawn pad.

**Layout PCell `bondpad`** (`bondpad_code.py`): metal stack only — no LVS device class,
no ngspice model. Parameters include `diameter` (default 80 µm), `shape`
(`octagon`/`square`/`circle`), `stack`, `topMetal`/`bottomMetal`, `padPin` (`PAD`).
This repo draws **70 µm** octagonal TM1+TM2 pads on the signal nets (`catalog.esd_devices`).
Parasitic pad C belongs to whatever instantiates the stage (testbench `{PAD_C}` in
`ngs.py`), not to the device-only subcircuit.

**Magic C-only PEX of the same drawn pad** `[sim]` (see `layout/debug_pex/FINDINGS.md`):

| Case | Value |
| --- | --- |
| Hand TM1-area formula (`pad_capacitance_f`) | 27.68 fF |
| Isolated `bondpad_70um` | 80.45 fF |
| Pad + ESD column tied (M2 bar + M5) | 101.56 fF `pad`–`vss` |
| Driver in-situ `outp`–`vss` | **143.56 fF** |

The stacked-plate area formula underestimates the drawn octagon (Magic 80 fF vs 28 fF
hand). In-situ neighbours add the rest. Termination sizing still uses the hand formula;
the pad driver schematic uses the Magic in-situ metal (143.56 fF) plus ESD junction C
in the compact models.

### Layer-to-substrate area capacitance (Magic extract deck)

From `magic/ihp-sg13g2-extract.tech`, typ variant `[model]`.

> **UNITS ARE aF/µm², NOT fF/µm².** The deck says so explicitly: *"Units are aF/um^2 for area caps
> and aF/um for perimeter and sidewall caps."* Getting this wrong is a **1000x** error in a load or
> pad capacitance budget, and it has already happened once in this repo.
>
> Built-in cross-check: the MIM row reads `1500`, and the MIM density is independently
> `cap_carea = 1.5e-15` F/µm² = 1.5 fF/µm² = **1500 aF/µm²**. If your reading of the table does not
> reproduce that, your units are off.

| Layer | To substrate | | Layer | To substrate |
| --- | --- | --- | --- | --- |
| `metal1` | 35.015 aF/µm² | | `metal5` | 7.136 aF/µm² |
| `metal2` | 18.180 aF/µm² | | `metal6` (TopMetal1) | 5.649 aF/µm² |
| `metal3` | 11.994 aF/µm² | | `metal7` (TopMetal2) | 3.233 aF/µm² |
| `metal4` | 8.948 aF/µm² | | MIM (on `metal5`) | 1500 aF/µm² |

Two further traps when budgeting with these:

- **Stacked plates shield each other.** For a pad or strap drawn on TopMetal1 + TopMetal2, the
  capacitance to substrate is set by the **bottom** plate (TM1, 5.649 aF/µm²); TM2 sees TM1, not the
  substrate. Summing both rows double counts.
- **Area capacitance alone badly underestimates a narrow signal wire.** A 0.14 µm-wide route is
  fringe- and coupling-dominated, not plate-dominated: plate area gives well under 0.1 fF for a 40 µm
  run, whereas a realistic on-chip wire is order 0.15-0.2 fF/µm. Use a per-length figure for routing
  estimates and reserve these area values for genuinely plate-like structures (pads, wide straps,
  coil metal over substrate).

### Metal resistors and the metal stack

`res_metal1` … `res_topmetal2` are layout/LVS only — no ngspice subckt. But the **sheet resistances are
documented and authoritative**, and two independent decks agree `[model]`:

| Layer | `RPSQ` (Ω/sq) | Thickness (µm) |
| --- | --- | --- |
| `Metal1` | 0.110 | 0.420 |
| `Metal2`…`Metal5` | 0.088 | 0.490 |
| `TopMetal1` | 0.018 | 2.0 |
| `TopMetal2` | 0.011 | 3.0 |

Sources: `parasitics/itf/sg13g2_typ.itf` (also every dielectric thickness and `ER`, mostly 4.1) and
`klayout/tech/lvs/rule_decks/res_extraction.lvs`. Use these for the DC resistance of any hand-built
metal structure (inductor, strap, pad feed) rather than inferring one from an EM material property.

The ITF dielectrics carry **no loss term** (`ER` only, no conductivity), and the openEMS stackup likewise
models oxide as `Epsilon=4.1` with no `Kappa`, with loss coming only from metal conductivity and the
2-5 S/m silicon substrate. So **"dielectric loss" is zero by construction** in these flows — do not fit a
resistor to it. Any residual shunt conductance in an EM fit is substrate coupling, which should be modelled
by its real mechanism (coupled inductors with parallel resistors, representing induced substrate current
loops) if and when it matters, not as a lumped port resistor.

## Netlist recipes

```spice
* 50 ohm termination
Xrt padp vtt 0 rsil w=0.5e-6 l=2.35e-6 m=1          $ 50.3 ohm [sim]

* ~150 fF metal-only finger cap, safe at 28 GHz
Xcs e1 e2 cap_cmomi w=12e-6 l=12e-6 mmin=2 mmax=5 feed=same

* primary ESD, ~51 fF per pad
Xesd_hi vdd padp 0 diodevdd_2kv
Xesd_lo vdd padp 0 diodevss_2kv
Xclamp  vdd 0 nmoscl_2

* HBT, ports c b e bn
XQ1 outp inp e1 s1 npn13G2 Nx=1
```

## Units and traps

- `w`, `l`, `le`, `we` are **metres** in ngspice. `{W}u` token forms silently misbehave.
- Resistor `bn` and cap substrate nodes must be tied (usually `0`).
- Shunt peaking order is `VDD → L → RD → collector`.
- Anything measured above is at `TEMP = TNOM = 27 °C` unless stated.
