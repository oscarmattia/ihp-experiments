# circuits/ctle56n/ — 56 Gb/s NRZ RX front end

Despite the directory name, this experiment now covers the **whole receiver front end**:
50 Ω/ESD termination → CML CTLE → current-steering VGA, plus a combined chain. The stages share one
sizing/simulation/measurement codebase, which is why they live together.

## Agent workflow

- **Coordinator:** the run's assigned coordinator model (Grok or Claude Opus 5).
- **Implementers:** Composer 2.5 sub-agents.
- **Update this AGENTS.md before any PR** that touches this experiment.
- Read [`../../MEMORY.md`](../../MEMORY.md) and [`../../docs/PDK.md`](../../docs/PDK.md) first. They carry
  device facts and corrections that were expensive to learn here (aF vs fF, resistor contact R, the LUT
  `gm` already containing `re`, why variable degeneration is not a gain control at 28 GHz).

## Stages

| Stage | Subckt | Netlist | Sizing | Runner |
| --- | --- | --- | --- | --- |
| Termination + ESD | `term_dut` | `spice/term_pdk.cir` | `python/size_term.py` → `term_params.inc` | `python/stage_term.py` |
| CTLE | `ctle_dut` | `spice/ctle_ideal.cir`, `spice/ctle_pdk.cir` | `python/size_ctle.py` → `params.inc` | `python/run_sims.py` |
| VGA | `vga_dut` | `spice/vga_ideal.cir`, `spice/vga_pdk.cir` | `python/size_vga.py` → `vga_params.inc` | `python/stage_vga.py` |
| Pad driver | `driver_dut` | `spice/driver_pdk.cir` | `python/size_driver.py` → `driver_params.inc` | `python/stage_driver.py` |
| Chain | `chain_dut` | `spice/chain_pdk.cir` | reuses all three | `python/stage_chain.py` |

The `tb_*.cir` templates serve every stage. `prepare_tb` substitutes `{DUT_PORTS}`,
`{DUT_BIAS}` and `{DUT_NODESET}` from `ctlelib/ngs.py` — one set per stage
(`TERM_DUT_*`, `CTLE_DUT_*`, `VGA_DUT_*`, `DRIVER_DUT_*`, `CHAIN_DUT_*`).

| Subckt | Cell ports | Testbench notes |
| --- | --- | --- |
| `term_dut` | `inp inn vdd vss` | `{DUT_BIAS}` adds hand pad caps; four ports because pad, ESD, 50 Ω and the CTLE route are one net |
| `ctle_dut` | `outp outn inp inn vdd vss mgate` | `{DUT_BIAS}` is `Iref`; templates wire port `0` to global ground |
| `vga_dut` | `outp outn inp inn vicm steerp steern vdd vss mgate` | `{DUT_BIAS}` is `Iref`, `Vicm`, `Vsp`, `Vsn`; signal emitters share one node (no drawable `Rs`) |
| `driver_dut` | `outp outn inp inn vdd vss mgate` | `{DUT_BIAS}` is half tail `Iref` plus pad caps on `outp`/`outn` |
| `chain_dut` | `outp outn inp inn vdd` | cascaded stages; sources live in `chain_pdk.cir` |

**Device-only cells:** ideal sources and pad capacitance belong to whoever instantiates
the cell — the standalone testbench or `chain_pdk.cir` when cascaded. `vss` is a pin
rather than the global `0` in the subcircuit so the port list matches layout;
testbench templates still wire the ground pin to `0`. `layout/common/parity.py` fails
the layout build if schematic and layout CDL ever diverge.

`vga_dut` and `driver_dut` are LVS-clean (see `layout/blocks/AGENTS.md`); `term_dut` is clean
but not yet in `layout/run_all.sh`. Floorplans are still being iterated.

That restructure is also what makes post-layout simulation cheap. An extracted stage presents the same
pins as its schematic, so the existing runners take the netlist **by path** and write `out/<name>/`
alongside the schematic passes, comparable metric for metric. Nothing here imports from `layout/`;
the netlists come from `layout/blocks/run_postlayout.py --stage {ctle,vga,driver}`.

| Stage | Runner | Pass names |
| --- | --- | --- |
| CTLE | `python/stage_postlayout.py --dut <path> --pass-name <name>` | `postlayout_klayout`, `postlayout_magic` |
| VGA | `python/stage_vga.py --dut <path> --pass-name <name>` | `postlayout_vga_klayout`, `postlayout_vga_magic` |
| Driver | `python/stage_driver.py --dut <path> --pass-name <name>` | `postlayout_driver_klayout`, `postlayout_driver_magic` |

`run.sh --with-postlayout <path>` is the CTLE-only opt-in hook. Each netlist declares
`* postlayout-cl-model: full|miller` so the testbench does not double-count interconnect
(or, on the driver, the hand `PAD_C`).

Post-layout numbers live in `vga_report.md` / `driver_report.md` (KLayout
matches schematic; Magic is the layout cost). VGA Magic drops 388 fF on
unlabeled `tx1`/`tx2` — see MEMORY.md. The driver's pad load is Magic in-situ
metal **143.56 fF** + ESD junction **50.9 fF** ≈ **194 fF/side**; shunt L uses
Butterworth **m = 0.414** → EM case `turn1` in `ind_shunt_drv.inc`. CTLE/VGA
stay Bessel **m = 0.32** in shared `ind_shunt.inc`. Layout GDS coil is still
`turn1_d40`. Both stages have DC+AC+PRBS/SBR. Artifacts:
`out/postlayout_{vga,driver}_{klayout,magic}/`.

## Targets

| Item | Value |
| --- | --- |
| Rate | 56 Gb/s NRZ, Nyquist **28 GHz** |
| CTLE DC gain | **−6 dB to 0 dB** (aim 0 dB) |
| CTLE peaking | **3–10 dB at 28 GHz** |
| Load | R+L shunt peaking: CTLE/VGA Bessel **m = L/(RD² C_L) = 0.32**; pad driver Butterworth **m = 0.414** at Magic pad metal **C_L ≈ 194 fF/side** |
| Fan-out | CTLE sees **FO1** (one VGA input unit); VGA drives **FO2** (2× its own input) |
| CMRR | **> 6 dB** at low frequency |
| PSRR | **> 20 dB** at low frequency (clipped to 120 dB when `vod ≈ 0`) |
| Termination | 50 Ω per leg (100 Ω differential) to an on-chip **~1.4 V** common mode |
| VGA range | **≥ 10 dB measured at 28 GHz** (never quote it at DC — see below) |
| Analyses | DC OP, AC sweep **1 MHz–300 GHz** (diff/CM/PSRR), Zin/S11, PRBS9 transient + eye, SBR taps |
| AC plot display | Frequency axis **100 MHz–200 GHz** (`AC_PLOT_FMIN_HZ` / `AC_PLOT_FMAX_HZ` in `ctlelib/metrics.py`); metrics still use full sweep |

## Topology notes that are easy to get wrong

- **Two per-emitter tail current sources**, not one tail on a midpoint tap. With a single midpoint tail
  the degeneration resistor carries `Ic` and drops ~132 mV, so the tail device sees far less `VDS` than
  the sizing assumes, and the drop grows as peaking is dialled up. With two sources the degeneration
  carries no DC, the tail gets `VBASE − VBE`, and gain stops perturbing the operating point.
- **`Rs` in parallel with `Cs`** directly between `e1` and `e2`.
- **Shunt-peaking order is `VDD → L → RD → collector`.** The coil's port capacitance therefore lands on
  the internal node, where it resonates with `L` (raising effective inductance) rather than loading the
  output — so it must **not** be counted in `C_L`.
- **`RD` is not the gain knob.** It is pinned by the Bessel condition and by a floor derived from the
  smallest buildable coil (PCell `dmin = 25.35 µm` → ~39 pH → `RD ≥ √(L_min/(m·C_L))` ≈ 70 Ω at
  `C_L = 25 fF`). Trim gain with `Rs`. `C_L` never forces `RD` down, since it does not enter DC gain.
- **The VGA controls gain by current steering**, not by variable degeneration: a fixed signal pair plus a
  dummy pair whose bases sit at the input common mode, both feeding the same loads, with the tail current
  steered between them through a **shared tail node**. Separate mirrors per branch cap the range at ~1 dB.
  The signal pair's emitters share one node — the old 0.01 Ω `Rs` placeholder is below anything `rsil`
  can build. The dummy pair's emitters stay **separate**; shorting them is a real circuit change (cost ~0.09 dB
  gain and broke ideal-versus-PDK eye-width agreement).
- **Termination has four ports** (`inp inn vdd vss`) because the bond pad, ESD, 50 Ω shunt and the route
  to the CTLE are one net — no device sits between them. Pad capacitance is a drawn-metal parasitic, modelled
  in the testbench rather than as a subcircuit device.
- **The chain is DC-coupled**, so each stage's `VBASE` must equal the previous stage's output common mode.
  Sizing stages independently against a hardcoded `VBASE` leaves the VGA's dummy pair referenced to the
  wrong level and skews the steering.
- `Nx = 1` for both the CTLE and VGA — the FO1 relationship depends on them matching. Note the HBT's
  intrinsic `re = 7.13·(4/Nx) Ω` (28.5 Ω at `Nx=1`) self-degenerates the device ~4:1 and caps stage gain.
- **The tail width has to be drawable.** `size_ctle.snap_drawable_mos_w` rounds it onto the
  **0.01 um MOS PCell grid** (`MOS_W_GRID_UM`), because a wide MOS is laid out as single-finger units
  and the LVS deck compares MOS `W` and `L` with essentially no tolerance — 242.988 µm against a
  drawn 243.000 µm is a mismatch. The snap moved the width by 0.005%, which moved `pdk_RD_realized_ohm`
  by 0.2 mΩ and nothing else. `python/check_mos_w_grid.py` pins the sizer, `mos_array` and catalog
  grids together.

## LUT inputs

| LUT | Path | Use |
| --- | --- | --- |
| HBT | `char/bjt/out/sg13_npn13G2.npz` | FT, CBE, CBC, GM, IC vs VBE/VCE; bias at max FT |
| MOS | `char/mos/out/lv_core_n.npz` | Tail and steering device sizing |
| R load | `char/passive/out/sg13_rppd.npz` | `rppd` loads and the vtt divider |
| R deg / term | `char/passive/out/sg13_rsil.npz` | Emitter degeneration and the 50 Ω termination |
| C deg | `char/passive/out/sg13_cap_cmomi.npz` | Metal-only finger cap, `feed=same`, `mmin=2` |
| C decap | `char/passive/out/sg13_cap_cmim.npz` | vtt decoupling |
| L shunt | `char/passive/out/sg13_ind_turn1_d*.npz`, `sg13_ind_turn1.npz` | EM pi-model → `spice/ind_shunt.inc` (CTLE/VGA); driver → `spice/ind_shunt_drv.inc` |

**Size every PDK resistor by ngspice `op` measurement**, never from `rsh·L/W` or a LUT lookup with a
scale factor. Head/contact resistance dominates and does not scale with `L/W`.

## Passives

- **Ideal pass:** ideal R, L, C in `spice/ctle_ideal.cir` / `spice/vga_ideal.cir`.
- **PDK pass:** `rppd` loads, `rsil` degeneration and termination, `cap_cmomi` metal-finger degeneration
  cap, `cap_cmim` decap, `diodevdd_2kv`/`diodevss_2kv` ESD with an `nmoscl_2` clamp, and the EM-fitted
  `ind_shunt` subcircuit. The PDK has **no ngspice inductor model at all**, so coils are openEMS-extracted
  and lumped-fitted by `python/size_ind.py`; the port branch is capacitance-only (the stack models oxide
  as lossless).
- `spice/ind_shunt.inc`, `spice/ind_shunt_drv.inc`, `spice/params.inc`, `spice/term_params.inc`,
  `spice/vga_params.inc` and `spice/driver_params.inc` are **generated** artifacts and are committed.

## Measurement definitions

- **SBR**: 1 UI isolated pulse after 32 UI of settling, 100 mVpp,diff, ~4.5 ps edges. Sample 3
  pre-cursors + cursor + 10 post-cursors every UI, and **drop taps with |h| < 0.5% of |cursor|**.
  Normalized ISI = `Σ h_k / h_0` over kept taps, `k ≠ 0`, signed; `Σ|h_k|/|h_0|` also reported.
  Cursor = max `|vod_ac|` within 3 UI of pulse start, baseline-subtracted.
- **Eye height**: fold onto 1 UI, **roll the phase axis so the eye centre sits at 0**, then take the
  vertical opening at the 0 V threshold over a ±2% UI window at the best phase. The centring is not
  optional — without it the measurement clips whenever the eye lands near the fold seam.
- **Eye width**: threshold crossings either side of the centred eye. Must be `< 1 UI`; a reported 2 UI
  means the folding window is being measured instead of the opening.
- **Peak-to-peak swing** is reported separately and is not the eye height (height is ~44% of pp on the
  CTLE, consistent with its post-cursor ISI).
- **Known limitation, verified independently**: sweeping an artificial time offset through a full UI leaves
  eye *height* invariant everywhere (spread <= 0.17%), and leaves *width* invariant for the termination,
  VGA and driver passes (0-0.5%). But CTLE width still varies ~15% with phase (ideal 0.51-0.61 UI, pdk
  0.62-0.73 UI), because that eye has the most ISI and so the most ambiguous plateau/alias structure. Treat
  CTLE eye width as indicative only. The acceptance-critical measurement — driver pad eye margin — is
  invariant to 0.3%. Do not "fix" this with acceptance windows on the result: a previous attempt hardcoded
  a `[0.63, 0.73] UI` band and discarded candidates above 0.85 UI, which made the metric unable to report a
  closing eye at all.
- **VGA gain range is quoted at 28 GHz.** DC-referenced range badly overstates in-band range for a
  degenerated stage, and small-signal range can overstate large-signal range where the steered pair runs
  at low current.
- **Return loss is referenced to 100 Ω differential.** Against a 50 Ω reference, adding shunt capacitance
  makes S11 appear to improve, which is impossible.

## ngspice `save` requirement

Device currents are **not** available to `print`/`wrdata` unless listed in `save` before the analysis;
`.options savecurrents` and `save all` are insufficient. HBT: `@q.xu1.xq1.qnpn13g2[ic]`. MOS LV PSP uses
**`ids`**, not `id`. Each stage supplies its own DC save/print lines through `prepare_tb`, because the
internal instance paths differ per stage.

## Outputs

Per-pass directories `out/{term,ideal,pdk,vga_ideal,vga_pdk,driver,chain}/` each carry `metrics.csv`, `op.txt`,
`ac_diff.png`/`.csv`, `cmrr.png`, `psrr.png`, transient and eye PNG/CSV, and `sbr.png`/`sbr.csv`/
`sbr_taps.csv`. Termination and chain additionally carry `zin.png`/`.csv`. Chain also writes
`out/chain/chain_perstg/` with per-stage AC (`ac_diff_*.png/.csv`), PRBS (`tran_diff_*.png`), and
SBR (`sbr_*.png`) overlays at each tap (term→CTLE in→VGA in→drv in→pad out). `out/summary.csv` aggregates
every pass. Auto-generated markdown reports (`*_report.md`) are regenerated by `python/generate_reports.py`
from committed `out/` artifacts (no ngspice re-run). Replot AC/S11 PNGs from CSV with
`python/replot_ac_plots.py` (also no ngspice). `out/**/work/` is gitignored ngspice scratch.

## Commands

```bash
source ~/.local/share/ihp-eda/env.sh
./circuits/ctle56n/run.sh                 # term -> CTLE -> VGA -> summary
./circuits/ctle56n/run.sh --with-chain    # also the combined chain
./circuits/ctle56n/run.sh --no-tran       # skip PRBS/SBR everywhere
./circuits/ctle56n/run.sh --help
```

Individual stages:

```bash
$IHP_EDA_ROOT/venv/bin/python circuits/ctle56n/python/stage_term.py
$IHP_EDA_ROOT/venv/bin/python circuits/ctle56n/python/size_ctle.py     # -> params.inc + ind_shunt.inc
$IHP_EDA_ROOT/venv/bin/python circuits/ctle56n/python/size_driver.py  # -> driver_params.inc + ind_shunt_drv.inc
$IHP_EDA_ROOT/venv/bin/python circuits/ctle56n/python/run_sims.py      # CTLE ideal + pdk
$IHP_EDA_ROOT/venv/bin/python circuits/ctle56n/python/stage_vga.py
$IHP_EDA_ROOT/venv/bin/python circuits/ctle56n/python/stage_chain.py
$IHP_EDA_ROOT/venv/bin/python circuits/ctle56n/python/generate_reports.py
$IHP_EDA_ROOT/venv/bin/python circuits/ctle56n/python/size_ind.py --verify-all-cases
```

`size_ind.py` verifies the generated coil model against the committed EM S-parameters using ngspice
`sp` analysis and fails on regression. `run.sh` is the single source of truth for sizing — it is not
re-done inside `run_sims.py`.
