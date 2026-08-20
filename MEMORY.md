# Agent memory — ihp-experiments

Factual notes for Cloud Agents (not a user README). Update when new PDK/sim pitfalls are found.

**Read these first, instead of re-parsing the repo:**

| Doc | Contents |
| --- | --- |
| [docs/PDK.md](docs/PDK.md) | SG13G2 devices, model/corner names, ports, verified device numbers, PDK traps |
| [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) | Installed tool versions, paths, activation, install/verify, egress caveats |
| this file | ngspice/flow pitfalls, corrections to earlier assumptions, workflow conventions |

Claims below are tagged `[sim]` when measured with ngspice on a Cloud Agent VM, `[model]` when read
from a PDK model file, `[est]` when still unverified.

## Environment

- Always `source ~/.local/share/ihp-eda/env.sh` before sims.
- Python: `$IHP_EDA_ROOT/venv/bin/python`, not system `python3`.
- **`matplotlib` and `scipy` are missing on a fresh VM** and the venv has no `pip`. Install with
  `uv pip install --python "$IHP_EDA_ROOT/venv/bin/python" matplotlib scipy`. `verify-ihp-eda.sh`
  does not catch this, so the first plotting run is where it shows up.
- **openEMS is not installed by default.** `./scripts/install-ihp-em.sh --skip-palace` builds it
  (v0.37.0-rc1 verified) and writes `em.env.sh`. It is fast enough to run inside one task on a 4-core
  host; do it in a background tmux session and keep working.
- `PDK_ROOT=$IHP_EDA_ROOT/IHP-Open-PDK`. ngspice must be **OSDI-enabled**.

## ngspice mechanics

- **Device currents are not saved unless listed in `save` before the analysis.**
  `.options savecurrents` and `save all` are insufficient.
- HBT probe: `@q.xu1.xq1.qnpn13g2[ic]` (the internal instance in `npn13G2` is `Qnpn13G2`) `[model]`.
  Top-level DUT pins are `v(outp)`, not `v(xu1.outp)`.
- MOS LV PSP parameter is **`ids`, not `id`**: `@n.xu1.xtail.nsg13_lv_nmos[ids]`. The error
  `no such parameter id` actually means the device *was* found.
- Nested-`dc` `wrdata` **freezes** small-signal gm/caps — fT needs a separate OP+AC pass
  (`char/bjt/ihp_bjt_sweep.py`).
- `wrdata` of real transient vectors often emits **time twice**: `time, time, v(a), v(b), …` even with
  `set wr_singlescale`. Parse a 6-column file as `[:,0], [:,2], [:,3], [:,4], [:,5]`. AC magnitude/phase
  `wrdata` has a different layout — see `parse_ac_raw`. With a **complex scale** it writes
  `freq, freq, freq_imag` before the data, so 6 requested vectors land in columns 3-8 of 9. This is not
  specific to `sp`: an `ac` sweep asked for `vr`/`vi` vectors is complex too.
- **An off-by-one column in an AC parse passes every low-frequency check.** Reading the pairs one column
  early rotates each phasor by 90 degrees and drops a term that is negligible while the imaginary parts
  are near zero, so DC gain comes out exactly right and only the high-frequency half is wrong. Here it
  moved the termination's 100 GHz insertion loss from −10.98 dB to −13.76 dB and its `f_IL_3dB` from
  67.8 GHz to 48.6 GHz, and "DC matches the baseline exactly" was taken as confirmation `[sim]`. The
  cheap guard is a quantity the testbench fixes by construction: with `ac 0.5 0` and `ac 0.5 180` ideal
  sources, `|v(vp) − v(vn)|` must be **exactly 1.0 at every frequency**. It read 1.0062 at 100 GHz.
  Assert it rather than eyeballing the sweep.
- **There is no Touchstone n-port device in ngspice 45.2** (grep-verified). The only Touchstone-reading
  element is the XSPICE `xfer` code model, and it is a **unilateral** transfer block (`in`/`out` ports,
  `file=` plus `span`/`offset` to pick a column) — a controlled source, not a bidirectional impedance, so
  it cannot stand in for a passive n-port such as a coil. Xyce/Spectre/ADS/Qucs-S do have real n-port
  blocks.
- **ngspice does have `sp` (S-parameter) analysis** (`RFSPICE` build option, present in our build). Declare
  ports on voltage sources: `V1 p 0 dc 0 ac 1 portnum 1 z0 50`. Use it to validate a lumped model against
  EM data in S-parameter space rather than via `L(f)`/`Q(f)`: our `ind_shunt` matches the openEMS
  touchstone to **0.60% mean |S21| error over 1-50 GHz and 0.75% out to 100 GHz**, phase within ~0.3 deg
  `[sim]`.
- Prefer the fitted lumped model over a raw n-port even where one is available: transient (PRBS/SBR) needs
  convolution or rational fitting from S-params, the AC sweep runs to 300 GHz while EM data stops at
  100 GHz, DC resistance is anchored to the PDK sheet resistance where touchstone `f=0` is degenerate, and
  a lumped model is parametric so coil choice can co-vary with `RD` and `C_L`.
- PWL sources: `PWL(` with `+` continuation lines; complementary PRBS on `inp`/`inn`.
- `.param` geometry (`w`, `l` for MOS, `rppd`, `rsil`, caps) must be in **metres**, not `{W}u` tokens.
- OSDI loads via `~/.spiceinit`. If a run has a redirected `HOME`, copy the PDK `.spiceinit` into the
  run directory or `rppd`/`rsil`/`cap_cmomi` fail as unknown models.
- PSRR of a perfectly matched differential pair is numerically infinite (`vod ~ 0`); clip to ~120 dB.
- `circuits/ctle56n/run.sh` is **bit-reproducible**: a clean re-run leaves `git status` empty. Use that
  as the regression check before/after refactors `[sim]`.

## Corrections to earlier notes

1. **"No compact IHP inductor that small (`l2n0` ~2 nH)" was wrong in kind, not degree.** There is
   **no ngspice inductor model at all** — `inductor`/`inductor3` are layout/LVS/EM-only devices
   `[model]`. The fix is not "keep L ideal forever" but EM-extract the actual coil and fit a lumped
   subcircuit. See [docs/PDK.md](docs/PDK.md).
2. **"BVceo of npn13G2 ~1.6 V" conflated two things.** The VBIC card gives `vbe_max = 1.6`,
   `vce_max = 1.6`, `vbc_max = 5.1` `[model]`. The 1.6 V number is the VBE/VCE soft limit, not a
   measured breakdown voltage. A ~1.6–1.7 V CML supply is still sensible, but justify it from VCE
   headroom, not from that figure.
3. **"rppd Rsh underestimates contact R" is more general and larger than stated.** Resistor R does not
   scale as `rsh*L/W` at all `[sim]`: `rsil w=0.5u l=2.35u` measures **50.3 Ω** against 32.9 Ω from
   sheet resistance, while the same `L/W` ratio at `w=2u l=9.4u` gives 37.2 Ω. Always size from
   `char/passive/out/*.npz` or a quick `op` sim.
4. **A metal-only finger capacitor does exist.** `cap_cmomi` (interdigitated MoM, OSDI) and
   `cap_cmomf` (fringe, low-frequency only) were missed earlier; `circuits/ctle56n/AGENTS.md` also
   claimed a `cap_cmim` second pass that the netlist never actually used. `cap_cmomi` with
   `feed=double` self-resonates at 30–47 GHz for large devices — use `feed=same` in 28 GHz signal
   paths `[model]`.
5. **The committed `turn2` EM inductor case is invalid**: `L = −12.24 nH`, `Q = 0`, yet
   `em_completed: True`. Port de-embedding is wrong for it. Only `l2n0` (2.02 nH) and `turn1`
   (0.215 nH) are usable `[EM]`.
6. **Branch suffixes are per-run, not repo conventions.** `-b7e8`, `-a928`, `-0561` were each assigned
   to a specific Cloud Agent run. Use the suffix given in the current run's instructions; never copy an
   old one.
7. **The coordinator model is not fixed.** Root `AGENTS.md` named Grok; runs also use Claude Opus 5 as
   coordinator. What matters is the split: coordinator plans/reviews/runs git and PR tools, sub-agents
   write the code — **Claude Sonnet 5 thinking for `layout/`, Composer 2.5 elsewhere.**
8. **`pdk/` is not a populated submodule** — it is gitignored and empty. The PDK lives at `$PDK_ROOT`.
9. **`sg13g2_bondpad.lib` is an empty placeholder** with no electrical content `[model]`; pad
   capacitance has to be hand-modelled.

## Design notes that keep coming back

- Shunt-peaking load order is **VDD → L → RD → collector**.
- Bessel maximally-flat-delay shunt peaking: `m = L/(RD^2*C_L) = 0.32`. Reference points: MFD 0.32,
  maximally-flat magnitude ~0.41, and beyond ~0.5 the step response overshoots and rings — which lands in
  the SBR post-cursors, so `m` is not a cosmetic target.
- **Verify a metric against the netlist under test, not against design intent.** A PDK pass reported
  `m = 0.349` computed from the *ideal* netlist's `RD = 87 Ohm` while its own `rppd w=5u l=1.0u` realized
  only **65.94 Ohm** (confirmed from the operating point and by a standalone sweep), so the shipped netlist
  was actually at `m = 0.607`. Emit realized device values into the metrics output, and derive reported
  figures from them. The tell was that the PDK metrics barely moved while the ideal pass changed a lot.
- **`RD` is not the gain knob.** It is pinned by the Bessel condition and the buildable-coil floor. Trim gain
  with the degeneration `Rs`, which is what sets gain and peaking. Two separate agents reached for `RD`
  (once directly, once via a resistor-LUT scale factor) to pull DC gain under its ceiling, and both broke
  `m` doing it.
- **The smallest realizable coil puts a floor on `RD`.** The `inductor` PCell has `dmin = 25.35 um`, and our
  EM fit is `L(28 GHz) = 1.774*D - 5.55` pH, so nothing below about **39 pH** can be built. Through the
  Bessel condition that becomes `RD_min = sqrt(L_min/(m*C_L))` — about **70 Ohm** at `C_L = 25 fF`. Since
  `L_target` goes as `RD^2`, cutting `RD` to fix a gain target quickly makes the required inductance
  unbuildable. `C_L` does not enter DC gain at all, so a bigger load never forces `RD` down.
- **A lumped inductor model needs port shunt capacitance to ground at BOTH ports**, and it is the
  dominant parasitic. Designer rule of thumb: **5-10 fF per 100 pH**. Confirmed by EM for the 66 pH
  `turn1_d40` coil: 5.79 fF and 5.80 fF (symmetric), against a 3.3-6.6 fF rule-of-thumb band `[EM]`.
  Plate area alone is not the right estimator and underestimates by ~6x here (541 µm² of TopMetal2 at
  3.233 aF/µm² gives only 1.75 fF), because the synthesized structure includes the Metal1 frame /
  SUBGND plane and the feed.
- **Extract the inductor pi-model from the EM Touchstone file, not from `L(f)`/`Q(f)` alone.** With
  `Y = (1/Z0)(I+S)^-1(I-S)`: series `Z = -1/Y12`, `C_port1 = imag(Y11+Y12)/w`,
  `C_port2 = imag(Y22+Y12)/w`, and `G_port = real(Y11+Y12)`.
  `char/passive/out/em_work/` is **gitignored**, so persist the extracted parameters into the committed
  `.npz`/`.meta.json` or nothing downstream survives a fresh checkout.
- **The coil port branch is capacitance ONLY — do not fit a loss resistor.** Both the ITF stack and the
  openEMS stackup model oxide as permittivity-only (`ER=4.1` / `Epsilon=4.1`, no conductivity term), so
  **dielectric loss is zero by construction** and fitting a resistor to it would be fitting noise.
  The residual shunt conductance the S-parameters do show (`G_port` flat at 0.177-0.198 mS over
  20-70 GHz for `turn1_d40`, i.e. an apparent 5.4 kOhm) is substrate coupling, not dielectric loss.
  **Deliberately omitted for now** — see the deferred-work note below.
- In shunt peaking the port branches are shunt elements, which is why omitting the loss term is safe:
  the VDD-side branch sits between two AC grounds, and any load-side loss resistance appears in parallel
  with `RD` (5.4 kOhm against 87 Ohm, negligible). The port *capacitance* is what matters — it adds
  directly to `C_L` and must be in the load budget.

### Deferred: substrate coupling in inductor models

Not modelled today, and intentionally so. If it later proves relevant (larger coils, coils close to
active circuitry, or a case where measured Q falls well below the metal-loss prediction), model it by its
actual physical mechanism: **current loops induced in the finite-resistance substrate**, i.e. **coupled
inductors with parallel resistors** (a transformer from the coil into a lossy substrate loop), not as a
lumped resistor bolted onto the port capacitance. A series or parallel port resistor is a curve-fitting
artifact that happens to absorb the effect over a narrow band while misrepresenting the physics.
- **PDK metal sheet resistance is the authority for the DC branch**, and there are two independent
  sources that agree: `libs.tech/parasitics/itf/sg13g2_typ.itf`
  (`CONDUCTOR TopMetal2 {THICKNESS=3.0 RPSQ=0.011}`) and
  `libs.tech/klayout/tech/lvs/rule_decks/res_extraction.lvs` (`RSH_RES_TOPMETAL2 = 0.011`). The ITF also
  has every layer thickness and `ER`, which is the right place to look for stack geometry.
- **The openEMS series resistance carries a systematic de-embedding offset** of about −0.4 Ω for these
  small coils: `Re{Z_series}` goes *negative* below ~15 GHz (−0.32 Ω at 5 GHz) `[EM]`. That is why `Q(f)`
  is unusable at low frequency. Anchor `R(f->0)` to a physically computed `R_dc` and take only the
  *shape* of the EM curve above ~20 GHz. Tell-tale of a bad fit: raw EM R at 28 GHz coming out *below*
  the DC value, which skin effect makes impossible.
- Do not accept a plausible-looking number with an invented physical story. Two separate cases here: a
  factor-of-2 inductance error blamed on "single-ended vs differential" (there is no such factor for a
  two-terminal component), and VGA bias drift of 18% called "expected for this topology" when it was
  caused by DC current flowing through the gain-control device.
- **Budget `C_L` honestly.** Raw LUT `CIN` of the next stage is not the load: add the Miller term
  `CBC*(1+|Av|)` and an interconnect estimate. Under-budgeting `C_L` yields an implausible `f_-3dB` and
  a design that looks over-peaked.
- **The Magic `defaultareacap` values are aF/µm², not fF/µm².** Misreading them is a 1000x capacitance
  error; it has happened here. Cross-check against the MIM row (`1500` aF/µm² = the independently known
  1.5 fF/µm²). Also: stacked plates shield each other, so use the bottom plate only, and area
  capacitance alone badly underestimates a narrow signal route (use ~0.15-0.2 fF/µm for wires). See
  [docs/PDK.md](docs/PDK.md).
- MOS tail devices at high `VGS` can slip into triode if `VDS < Vov`; size for `VGS` 0.55–0.70 V.
- **Variable emitter degeneration is not a gain control at 28 GHz.** Sweeping `Rs` on a CML pair (control
  device removed, so this is intrinsic) moves DC gain 10 dB but gain at 28 GHz only 2 dB, because the
  emitter capacitance shorts the degeneration out well below Nyquist `[sim]`:
  `Rs = 0.01 / 42 / 100 / 209 Ohm` gives DC `+3.88 / +0.51 / -2.56 / -6.20 dB` against 28 GHz
  `+3.86 / +1.82 / +1.92 / +2.32 dB`. So variable `Rs` is a variable *equalizer*, not a variable *gain*
  stage. **Always quote VGA gain range at the signal frequency, never at DC.** For real gain control use
  current steering (scale `gm`), which keeps the response shape fixed.
- **Tail-current steering with a dummy pair** is the working VGA architecture here: fixed signal pair plus a
  dummy pair whose bases sit at the input common mode, both feeding the same shunt-peaked loads, with the
  tail current steered between them through a **shared tail node** (separate mirrors per branch let both
  paths draw full current and cap the range at ~1 dB). It gives frequency-flat range (11.2 dB at 28 GHz
  measured), constant output common mode since total load current is fixed, and unchanged input
  capacitance so an FO1 relationship survives `[sim]`.
- **Steering starves the signal pair exactly when the input is largest.** A bipolar pair follows
  `Iout/Itail = tanh(Vid/(2*VT))`, so at `Vid = +/-50 mV` it is already at 0.748 of full switching against a
  linear 0.967, and `+/-100 mV` is essentially fully switched. Minimum gain is when the input is biggest, yet
  tail steering leaves the signal pair at a small fraction of full current there, so its linear range is
  smallest when most needed. Small-signal AC gain range therefore overstates large-signal range — always
  cross-check with a transient at the real test amplitude.
- Because steering sets gain by scaling `gm`, a **fixed** degeneration resistor costs the same gain at every
  setting and does not reduce the steering range — unlike variable degeneration. That makes fixed `Rs` a
  clean linearity knob, though its benefit scales with the pair's current and so fades at minimum gain.
- **An output driver into a 50 Ω interface is gain-starved, and `re` is why.** The differential load is
  pinned at `RD || 50` = 25 Ω by the interface, so gain is `gm_eff * 25`. With `re = 7.13*(4/Nx)` degenerating
  the pair, `Nx=2` gives `gm_eff ~ 49 mS` and a gain of only ~1.23 (+1.8 dB) `[sim]`. Consequences:
  - The full switching swing is `I_T * R_eff * 2` (400 mVpp,diff at `I_T` = 8 mA into 25 Ω), but reaching it
    needs roughly `400 mV / gain` ≈ **325 mVpp,diff of drive** — not the `4*VT ≈ 100 mV` an undegenerated
    `tanh` pair would need. `re` adds roughly `I_T*re` ≈ 114 mV to the required drive.
  - So a driver stage is **not** a free limiter: the preceding chain must be budgeted to deliver that drive,
    or `Nx` raised (since `re ~ 1/Nx`) at the cost of input capacitance.
  - Testing a driver with the same 100 mVpp,diff stimulus used for input-referred stages will understate its
    swing. Characterize pad swing versus input swing instead of quoting one amplitude.
- **The LUT `gm` for these HBTs is already `re`-degenerated.** `gm/Ic = 9.90 1/V` against the intrinsic
  `1/VT = 38.7 1/V` is the tell. Applying `gm/(1 + gm*re)` on top double-counts `re` — it cost one agent a
  3.3 dB error in a gain ceiling and nearly a wrong architecture decision. Stage gain from the LUT `gm` is
  `gm*RD/(1 + gm*Rs_external)`; simulation puts the `Rs -> 0` ceiling at `Nx=1`, `RD=68 Ohm` at +3.88 dB.
- **A coil's port capacitance does not load the output in a shunt-peaked stage.** With
  `L: vdd -> nlp1` and `RD: nlp1 -> out`, the port capacitance sits on `nlp1` and resonates with `L`
  (effective inductance `L/(1 - w^2*L*C)`, about +9% at 74 GHz for the 66 pH coil, self-resonance ~256 GHz).
  Keep it out of `C_L`. And remember `C_L` is a **per-side** quantity, so it takes one interconnect route,
  not two.
- One 2 kV ESD diode pair (`diodevdd_2kv` + `diodevss_2kv`) loads a pad with **50.9 fF** at 1.4 V
  `[sim]`. That dominates a 28 GHz input and is why the 50 Ω shunt termination matters.
- **The VGA's `Rs` was never a device.** `size_vga.RS_SIGNAL_OHM = 0.01` is a placeholder short: the
  smallest realizable `rsil` here is tens of ohms, so no layout can build it. Removing it and merging the
  signal pair's emitters into one node costs **+0.001 dB** of DC gain, i.e. nothing. Reintroducing fixed
  degeneration means `size_vga.py` emitting an `rsil` geometry, not editing the netlist.
- **The VGA's dummy emitters are not the same case and must stay separate.** `ed1` and `ed2` have no
  element between them and each has its own steering device off its own tail. Shorting them looks like
  the same cleanup and is a real circuit change: the dummy collectors sit on `outp`/`outn`, which carry
  differential signal, so the emitters carry differential AC too. Measured, the merge moved DC gain
  **+0.066 dB ideal / +0.088 dB PDK** and pushed ideal eye width from 0.515 UI to 0.785 UI against the
  PDK's 0.675, failing the ideal-vs-PDK agreement check `[sim]`. It was initially reported as the
  legitimate effect of removing `Rs`; the two were separated only by running the variants side by side.
- **In the termination, the pad node and the stage output are one net.** The bond pad, the ESD diodes,
  the 50 Ω terminator and the route on to the CTLE have no device between them, so the ~1 mΩ `Rout_ser`
  that used to separate them was a probe artifact. `term_dut` therefore has four pins, not six, and a
  layout `outp` separate from `inp` would be a net the schematic does not have.
- SBR: cursor = max `|vod_ac|` in the first 3 UI after an isolated 1-bit pulse; sample taps every UI.

## Layout / physical verification

Flow and full detail: [docs/LAYOUT.md](docs/LAYOUT.md), [layout/AGENTS.md](layout/AGENTS.md).
Devices are foundry PCells; gdsfactory does composition and routing only.

- **Never transcribe a DRC limit.** `layout/common/rules.py` reads the PDK's own
  `rule_decks/sg13g2_tech_default.json`. A hand-written table had TopMetal1 at 1.50 µm against the
  1.64 µm minimum (`TM1_a`), and the deck only caught it once a route used that metal `[model]`.
- **Electromigration limits exist in exactly one place and there is no signoff check for them.**
  `libs.ref/sg13g2_stdcell/lef/sg13g2_tech.lef` carries `DCCURRENTDENSITY AVERAGE`; the DRC decks, the
  ITF and the Magic tech files carry none. `layout/common/em.py` reads it, so no limit is transcribed,
  and the stage checks every drawn conductor itself. Values `[sim]`: Metal1 1 mA/µm, Metal2–Metal5
  2 mA/µm, TopMetal1 15 mA/µm, TopMetal2 16 mA/µm, Via1–Via4 0.4 mA/cut, TopVia1 1.4 mA/cut,
  TopVia2 10 mA/cut.
- **`Cont` has no current limit in the PDK.** It has a resistance but no `DCCURRENTDENSITY`, so MOS
  source/drain contact electromigration is not checkable from open PDK data. The closest analogue is
  the resistor PCells' `ikspec` (0.11–0.30 mA per contact), which is not stated for MOS contacts; if it
  applied it would be the binding constraint on a wide tail. `em.UNCHECKABLE` records this rather than
  letting silence pass for a verified limit.
- **On the top metals, minimum width beats electromigration by an order of magnitude.** 8.7 mA needs
  0.54 µm of TopMetal2 against a 2 µm minimum, so the power ring is width-limited, not EM-limited. The
  thin-metal buses are the opposite: `mos_array`'s old hand-picked 1.0 µm rail was 45% under what
  2.9 mA needs.
- **`run_lvs.py` exits 0 even when netlists do not match.** Judge the compare on "Netlists match" in
  the report, never on the return code. Trusting the exit status reported four mismatched blocks and
  one mismatched device as clean.
- **Do not pass `-rd tables=...` to the DRC deck.** Its connectivity section runs unconditionally and
  references layers only the full table set defines, so it dies with `'-': Argument needs to be a DRC
  layer`. That error is *not* a version problem. Go through `run_drc.py`, which does enforce the
  KLayout pin and refuses to run below it.
- **The Magic tech file requires more than `versions.txt` pins.** `ihp-sg13g2.tech` carries
  `requires magic-8.3.617` while `versions.txt` says 8.3.589. Below the tech file's floor its version
  and cifinput sections fail to load and Magic cannot read a GDS at all `[sim]`.
- **`klayout` pip and gdsfactory conflict.** gdsfactory 9.44 needs kfactory ≥ 2.5, which declares
  `klayout >= 0.30.8`, while the PDK pins 0.30.5. Keep the PDK version: it is what IHP tested and it
  keeps PCell generation and the DRC binary on one KLayout.
- **PCell traps** `[sim]`, all measured against the deck:
  - `pya.Library.library_by_name("SG13_dev")` returns `None`; the technology name is a required
    second argument.
  - `rppd` and `cmim` ignore `w`/`l` unless `Calculate` is changed — `rppd` defaults to solving for
    `l` from `R`, `cmim` for `w&l` from `C`. Passing a geometry without it silently gives the default.
  - `nmos` with `ng > 1` draws **no source/drain straps**: a 4-finger device extracts as four
    transistors in series. `m > 1` is netlist-only and changes nothing in the layout. A single finger
    is silently capped near 10 µm, above which the PCell reverts to minimum width. A wide device must
    be an array of single-finger units with drawn straps — see `layout/blocks/mos_array.py`.
  - **`ng` is clamped at 100 and `w` resolves onto a 0.01 µm grid by flooring**, so a total that does
    not divide onto that grid comes out narrower than asked. Measured against the PCell: `w=9.925u`
    draws 9.920 µm of active, `w=9.875u` draws 9.870 µm. This is **not** the 5 nm database grid —
    conflating the two is what let a driver tail be 784.075 µm in the schematic and 783.680 µm in the
    layout. `mos_array.MOS_W_GRID` and `size_ctle.MOS_W_GRID_UM` hold the one value and
    `circuits/ctle56n/python/check_mos_w_grid.py` pins them together, because the circuit side is not
    allowed to import from `layout/`. The CTLE never exposed it: 243 µm is 25 × 9.72 and already on
    the grid.
  - **`npn13G2`'s `Nx` is an extracted geometry parameter, not netlist multiplicity.** The deck's own
    `npn_adv.cdl` testcase writes `Nx=2 m=1` and extraction reports the same, so folding `Nx` into `m`
    fails LVS at `Nx > 1` — and `Nx = 1`, which is everything the CTLE uses, hides it.
  - **The `esd` PCell draws three different devices from one parameter**: `model` selects
    `diodevdd_2kv`, `diodevss_2kv` or `nmoscl_2`. `bondpad` draws the pad stack. All four are DRC
    clean and the ESD cells LVS clean standalone, so a violation involving them is in how they were
    connected, not in the cell.
- **Extraction semantics differ from the CDL.** The deck reports MOS width **per finger** and taps as
  area and perimeter rather than `w`/`l`. Compare capacitors on **area**, which is what sets `C`; the
  metal-finger cap snaps `w` and `l` onto its finger pitch and neither matches the request.
- **A strapped array is one device to the extractor, so the CDL carries one device.** Measured against
  the deck on the real 25-unit 243 µm tail `[sim]`: one `w=243u ng=1 m=1` **matches**, one
  `w=9.72u m=25` matches, 25 explicit 9.72 µm devices match, and one `w=242.988u` **fails**. So no
  layout-specific netlist restructuring is needed — but the width has to be drawable, because the deck
  compares MOS `W` and `L` with essentially no tolerance. `rule_decks/custom_devices.lvs` relaxes only
  inductors (5% on `w`/`s`/`d`) and diodes (1% on `A`/`P`). `size_ctle.snap_drawable_mos_w` puts the
  schematic on the array grid so the two agree; `layout/common/parity.py` fails if they drift.
- **LVS never checks the CDL against the schematic.** It compares layout against CDL, so a device
  missing from *both* is invisible — the bias diode was absent from the layout for several revisions.
  `layout/common/parity.py` closes that loop. `ind_shunt` → `inductor` is the one permitted
  substitution, and even there the geometry is compared: `size_ind.py` writes the case it solved into
  `ind_shunt.inc`'s header, so `w`, `s` and `d` are read from there rather than waved through.
- **A gate strap must clear active.** Poly over active is a transistor: a strap flush against the
  active edge merged 25 array units into one 241 µm device with gate, drain and source shorted. Keep
  `Gat_d` = 0.07 µm of clearance and strap inside the gate's overhang band.
- **Guard-ring taps are layout-only.** Run LVS with `--disable_tap_extraction` for any cell with a
  ring, or the deck extracts hundreds of tap devices no netlist declares. That stays true for a
  sub-block called hierarchically: the option is required at the top, not at the sub-block.
- **A hierarchical CDL only matches if the layout really keeps the hierarchy.** Measured with
  `layout/debug_pex/probe_hier_lvs.py` `[sim]`: an `X` call onto a **block** sub-cell (guard ring, several
  devices) matches, while the same construction onto **one-device** sub-cells does not, because the
  layout flattens them and the deck then sees devices the top cell's `X` lines do not account for. The
  flat control — same layout, CDL expanded — matches in both cases. Two further conditions the probe
  pins down: the top cell name must be passed explicitly, and each `.SUBCKT` name must equal the GDS
  cell name it stands for.
- **Route the vertical leg first.** When a device brings both terminals out at the same y — a rotated
  resistor does — a horizontal-first run leaves one via and passes over the other terminal's via pad,
  shorting the nets through an intermediate metal of the stack.
- **Put via stacks on a stub outside the device.** A stack's landing pads are wider than a pin, so
  dropping one on a pin pushes contact and via spacing rules against the device's own geometry.
- **A via stack is an obstruction on every layer it spans, not just its two endpoints.** Measured
  `[sim]`: `via_stack` from `Metal3` to `TopMetal1` draws metal on Metal3, Metal4, Metal5 *and*
  TopMetal1; `Metal2` to `Metal5` draws all four. So a run described as "the TopMetal1 vertical" is
  really a blockage on four layers wherever it changes level, and any horizontal on any of them
  crossing that x shorts into it. This is what merged `vdd` into `vss` in the driver's pad band, where
  Metal3 ESD and clamp ties crossed the channel's supply verticals at their transition points. Two
  consequences: give each vertical in a crowded channel its own metal for the whole run, and give each
  one a **unique y for its single layer transition**, so no horizontal ever crosses a stack.
- **Snap everything to the 5 nm grid**, including guard-ring tap positions. Distributing taps evenly
  produced offgrid violations on activ, cont, metal1, psd and substrate at once. This is the database
  grid for drawn geometry and is a different thing from the MOS `w` grid above, which is 0.01 µm.
- **`draw.trunk_net` is unsafe wherever its horizontal stubs share a y with something else.** It drops a
  stub from each terminal to a vertical trunk at the terminal's own y, and that stub crosses everything
  between them on the same metal. Two agents hit it independently on the same day: on the VGA a stub
  leaving `tail1`'s drain passed through `tail2`'s x and the two 246.75 µm tails extracted as **one
  493.5 µm device**; on the driver a collector stub shared the Metal5 emitter row and merged `outp` into
  `em` `[sim]`. It is safe in the CTLE only because that stage has a single MOS row and its trunks are
  outboard of everything they cross. With more than one row, take each array's terminal to **its own
  outboard column first** and change layer before running horizontally.
- **A merged pair of identical devices extracts as one device of exactly twice the width.** 493.5 µm for
  two 246.75 µm tails, 729 µm for the CTLE's three arrays. That signature identifies the failure faster
  than reading the netlist does.
- **Bisect a cell, do not reason about it.** Both the driver and the termination were resolved by
  building the cell in stages — devices alone, then the divider, then the clamp, then the ring — and
  extracting at each step. The driver's core was clean at every step and only broke when pads, clamp and
  ESD went in, which located nine separate merges in one pass. Reasoning about the full cell had not
  moved either block in two rounds.
- **The bond pad claims its footprint the way a coil does.** `Pad.fR_M1` through `Pad.fR_TM2` fire on
  anything drawn under the 70 µm square, so a feeder has to approach along the pad's edge and stop
  outside it — the same shape of constraint as the coil's `pwell_block` marker, and it wants the same
  answer. The `bondpad` cell also anchors on its **centre**, so placing it by bottom-left coordinates
  puts it 35 µm from where it was meant to go.
- **A block with no coil takes no DRC waiver at all.** `LBE.a`/`LBE.c` are chip-area back-end markers on
  the `inductor` cell; the termination has none and is clean with `CHIP_LEVEL_ALLOWED` empty. Do not
  carry the CTLE's waiver into a block that has nothing to justify it.
- **Widening a rail or narrowing a gap is not free at block level.** Recomputing the VGA's array gap
  from the rule minimum (12 µm down to 5.97 µm) traded 72 spacing violations for 1848 contact and via
  ones. The CTLE's 12 µm is not the rule minimum and was not meant to be.
- **Context rules** an isolated cell cannot satisfy: `LU.a`/`LU.b` (a substrate tie within 20 µm),
  `LBE.a`/`LBE.c` (100 µm chip-area back-end markers), density and antenna. This is not an assumption
  — the same deck on the PDK's own `sg13_lv_nmos` testcase layout trips `LU.b` 35 times `[sim]`. Guard
  rings make them pass at block level.
- **No substrate ring around an inductor.** Wrapping the coils tripped p-well block and contact-bar
  rules against their markers; the coil sits over blocked p-well and wants no ties near it.
- **Nothing with a substrate tie may sit inside a coil's footprint.** The `inductor` cell is 108 µm
  square and its `pwell_block` marker (46/21) covers all of it, while `PWB.f` wants 0.24 µm between
  that marker and any p-tap. Coils facing each other reach as far down as they reach up, so the pin
  row has to sit a full half-height (54 µm) above the highest p-tap — the HBTs' substrate ties. Get
  that wrong and both HBTs report `PWB.f` while being clean on their own `[sim]`.
- **How you approach a coil pin changes its extracted geometry.** The deck derives `w`, `s` and `d`
  from the winding inside `ind_drw`, and that marker covers the whole coil cell, so any connection
  meeting the feed inside it is measured as part of the winding. A perpendicular stub — turning a feed
  down at the pin — had both coils extracting as **`w=1.5 µm, d=45 µm` against a drawn 4 µm and
  40 µm** `[sim]`, far outside the deck's 5% inductor tolerance. Leave a coil pin *colinear* with its
  feed, at the feed's own width and y, and turn only once clear of the marker. Measured in isolation:
  a single coil at `M135` extracts at 4 µm, and so does a facing pair joined by a colinear strap, so
  rotation itself is harmless.
- **The HBT stacks all three terminals at one x.** Collector, emitter and base sit at the same x with
  only **0.23 µm** between the base's Metal1 bar and the emitter's Metal2 block, so via stacks dropped
  below each land 1.13 µm apart and short. LVS reports the base merged into the emitter. Take the base
  out sideways along its own bar and leave the emitter the downward offset.
- **A power ring with nothing attached is invisible to LVS.** The compare is device-level, so a ring
  that only shares a *label* with the supply passes: no device touches it, so it never enters the
  netlist. The CTLE's vdd ring was floating for several revisions. Check ring connectivity
  geometrically — merge the real polygons, not their bounding boxes, since a coil's octagon bbox
  swallows everything inside it and makes unrelated nets look connected.
- **The metal-finger cap's feed is on its edge, not its centre.** `cap_cmomi` with `feed=same` brings
  PLUS on Metal4 and MINUS on Metal5 out at one point on its *left* edge. Approach it from that edge,
  or rotate it so the feed faces the wiring; routing in from another side crosses the finger field and
  reports Metal4 and Metal5 spacing violations.
- **The `nmos` PCell has no gate contact at all.** The gate is bare poly with 0.18 µm of overhang past
  active and nothing above it, so a gate net cannot leave the device without a hand-drawn contact:
  Cont at exactly 0.16 µm (`Cnt.a` is a maximum as well as a minimum), 0.07 µm of poly around it
  (`Cnt.d`) and 0.05 µm of Metal1 (`M1.c1`). Metal drawn near the strap without a Cont looks connected
  and is not — LVS reported the bias device with an unnamed drain net.
- **A shared supply rail has to grow away from the devices, not be centred on them.** An EM-sized vss
  rail is several times wider than one array's own source rail; centred on it, the extra width reached
  into the drain via columns, which start at the bottom of the active area. LVS reported e1, e2 and vss
  as one net with a single 729 µm transistor `[sim]`.
- **Guard-ring taps are not one piece of metal.** They sit at a pitch, so the ring is a single net only
  through the substrate. To make the substrate be `vss`, land one via *inside* a tap's own Metal1;
  a strap drawn along the ring edge leaves a 0.1 µm sliver against the tap pads and reports Metal1 and
  contact spacing all along the row. `add_guard_ring` returns `tap_centres_um` for this.
- **Top vias are single large-area cuts.** `via_stack` draws one TopVia1 or TopVia2 cut per instance;
  the row and column parameters only multiply the thin-metal vias lower in a stack. More cuts means
  more instances, which in turn sets a minimum conductor width — that, not electromigration, is what
  decides the power ring's width.
- **An EM-derived width is a computed number and lands off-grid.** Snap widths and the offsets derived
  from them, or a rail reports `metal2_drw_Offgrid`.
- **Extract with Magic *flat*.** Hierarchical extraction with this PDK's extract deck does not produce
  a capacitance once a cell has subcells. Minimal case — one NMOS in a subcell, N instances in a top
  cell `[sim]`: flat equals the subcell alone at N=1 to the last digit and then goes as
  `N × sub + (N−1) × 0.442 fF` of neighbour coupling (0.4423 and 0.4426 fF per pair at N=4 and N=8),
  while hierarchical is wrong at every N including N=1 (1.352 fF against the correct 2.607) and its
  total goes negative from N=2 on, with 2N negative terms. On the CTLE stage that was nine negative
  substrate terms, worst −85 fF. Neither plausible cause explains it: a bare array is clean at 1, 5 and
  25 units and `cthresh` 0/0.01/1 give identical negatives. Only the stage was affected — the device
  flow already flattens through `write_for_magic`.
- **Do not quote a ratio between hierarchical and flat totals.** The flat total is ~5x the hierarchical
  one on the stage, but the hierarchical figure has negative terms inside it, so the ratio measures how
  negative those were rather than capacitance lost. Also keep two effects apart: a hierarchical netlist
  states a subcell's parasitics once and instantiates it N times, so a *textual* sum under-reports by
  about the instance count even when extraction is perfect. `probe_hierarchy_total.py` reports both
  sums for that reason.
- Nothing is clamped, and `PexResult.physical` gates negatives, because ngspice accepts a negative
  capacitor without complaint and it silently moves the AC result. Full bisection, the reusable probes
  and the method notes: **[layout/debug_pex/FINDINGS.md](layout/debug_pex/FINDINGS.md)**.
- **Flat extraction emits no `.subckt` at all**, just a bare deck, so anything that needs an
  includable subcircuit has to supply the header and the port list itself.
- **Do not pre-flatten a GDS with `write_for_magic` before extracting a block.** It **merges nets**:
  the CTLE sim view, which LVS-matches its CDL, extracted with `e1`, `e2` and `vss` collapsed into one
  node — all 75 array devices reading `d=e2 s=e2`, and `e1` and `vss` absent from the deck entirely
  `[sim]`. Passing the same GDS straight to `run_magic_pex` gives three distinct arrays and symmetric
  couplings. Flatten in the netlist (`ext2spice hierarchy off`), not in the layout. The tell was
  asymmetry: `nlp1` and `nlp2` both coupling to `e2` with byte-identical values, which a differential
  layout cannot do. `write_for_magic` is still used by the single-device flow, where 2–3 nets per cell
  makes the collapse unlikely and the numbers check out, but it should not be trusted on a block.
- **Check parasitics for symmetry before believing them.** A correctly extracted differential layout
  gives matched pairs — `mgate–e1` 11.406 fF against `mgate–e2` 11.383 fF, `nlp1–vdd` and `nlp2–vdd`
  identical. One-sided couplings mean merged nets, not a real asymmetry.
- **Post-layout ports are not the same set as extracted ports.** The LVS netlist lists only nets a
  *device* touches. In the reduced view nothing touches `vdd` — both coils are black-boxed — so `vdd`
  is missing from its `.SUBCKT` line, and building the core from that list silently discarded 137 fF
  of supply capacitance (134.78 fF of it the ring's coupling to `vss`) and left the wrapper connecting
  `ind_shunt` to a `vdd` that was not the layout's. Build the core from the intended interface.
- **A deck total is not a load on anything.** The CTLE stage's 496 fF is 27% `vdd`–`vss` from the power
  ring and 30% `mgate`–`vss` from the gate strap across 75 array devices; only **11% touches a signal
  net**, and an output node carries 15.26 fF `[sim]`. Against the 6.8 fF in `CL_INTERCONNECT` that is
  2.2x, not the 70x a deck total suggests. Sum the terms touching the node in question.
- **Routing capacitance per µm is layer-dependent and `ROUTING_CAP_FF_PER_UM` is a mid-range value.**
  An isolated Metal4 trunk measures **0.085 fF/µm**, half the 0.17 assumed in `size_ctle.py`, because a
  high metal sits far from the substrate. What blows the budget is length, not the coefficient: the
  output trunks run 135.6 µm against the 40 µm behind `CL_INTERCONNECT`, which is the price of bringing
  signals to the cell edge past a 108 µm coil and a power ring. In situ the same wire reads 1.3–1.5x
  its standalone value, which is the neighbour coupling `[sim]`.
- **Magic's MOS `as`/`ad`/`ps`/`pd` are unusable for a strapped array.** Parallel units share drain and
  source nets, so it puts the merged node's whole diffusion on one arbitrary instance and gives the
  rest `as=0` — which the model file defines as "calculate it", not "none here", so 24 estimated
  junctions land on top of one measurement. Take devices from the KLayout LVS extraction, which merges
  the array into the single device it is (`W=243u AS=82.62p PS=503u`), and take only capacitance from
  Magic; restricted to shared nets that keeps 493 fF and discards 3 fF (0.6%) `[sim]`.
- **`pre_layout` is a real model switch, and post-layout must set it to 0.** The MOS models default to
  `pre_layout=1`, which bakes in a layout allowance: `dlq = '-1.3721e-08 -((1-pre_layout)*2e-08)'` and
  `lov = '2.9423e-08 -((1-pre_layout)*9e-09)'`. A post-layout netlist that supplies extracted
  `AS`/`AD`/`PS`/`PD` and leaves `pre_layout` at 1 double-counts 20 nm of channel length and 9 nm of
  overlap. Only the MOS and moscap models have it; the HBT and passives do not.
- **The extracted netlist carries parameters the models reject.** Extraction emits `A` and `P` on
  `cap_cmomi`, which declares neither, so a post-layout netlist has to be filtered against each
  model's own accepted parameter list rather than fed to ngspice raw.
- **LVS `D$` instances are `.subckt` wrappers.** `diodevdd_2kv`, `diodevss_2kv` and `nmoscl_2` are
  subcircuits, so a post-layout core has to rewrite the LVS `D$` prefix to `X` the same way it
  rewrites `M$`/`Q$`/`R$`. Leave `D$` and ngspice treats them as primitive diodes `[model]`.
  Pin order also differs: the deck writes BJT3 `C B E` (and the clamp as `VSS VDD`); the compact
  models want `VDD PAD VSS` / `VDD VSS`. Remap before simulating or the diodes sit on the rails.
- **Driver Magic flow must drop the testbench `PAD_C`.** The bond pad is metal, so C-only PEX already
  has it; keeping the hand cap double-counts. The `* postlayout-cl-model: miller` marker is the
  switch. KLayout (devices only) still needs the hand cap `[sim]`.
- **The driver's 91 → 35 GHz BW drop is the pad model, not missing ESD and not the feed.**
  Schematic already has ESD compact models (50.9 fF/pair). Magic keeps those ESD
  subcircuits and extracts **143.56 fF `outp`–`vss`**. Sweeping schematic `PAD_C` to
  that value lands at 35.60 GHz against Magic's 34.88 GHz. `PAD_C=0` *widens* the
  schematic to 128 GHz, so ESD was never omitted. Collector-to-pad wiring is 2.56 fF
  (`nlp`–`out`). The 819 fF Magic total is `mgate`+`em`+pads; do not quote it as
  `C_L`. **144 fF is not the pad alone:** `probe_standalone_pad.py` extracts the same
  `bondpad_70um` at **80.45 fF** to substrate. Tying the ESD column (9 um gap, Metal2
  PAD bar) adds **21 fF** of `pad`–`vss` (101.6 fF). An unconnected column adds
  nothing. A TM2 vss ring at 6 um adds 4 fF. The remaining ~42 fF is still the rest
  of the pad band. See `layout/debug_pex/FINDINGS.md` `[sim]`. **Driver schematic
  sizing now uses that Magic in-situ metal (143.56 fF `PAD_C`) and Butterworth
  shunt peaking m = 0.414 → EM case `turn1` ~215 pH in `ind_shunt_drv.inc`. CTLE/VGA
  stay Bessel m = 0.32 in shared `ind_shunt.inc`. After the retune, schematic
  and Magic agreed at **43.67 / 43.61 GHz** before the GDS coil swap. After
  placing `turn1` (d=120 µm) the cell is 78 µm taller; Magic `outp`–`vss` is
  **152.23 fF** and BW is **42.16 GHz / −0.14 dB @ 28 GHz** `[sim]`. Layout
  and schematic coils now match. Termination still uses the hand 27.7 fF
  TM1-area formula.
- **VGA Magic drops unlabeled `tx1`/`tx2`.** Those dummy-steer collectors are drawn but never
  labelled, so PEX emits `m2_7492_3498#` / `m2_36168_2698#` (~80 fF each to `vss`, plus coupling
  to `em`/`ed*`) and the C-only rewrite throws them away: kept 548 fF, dropped 388 fF. The
  midband output numbers are still usable (internal-node C, not `C_L`); steering-node C is
  under-counted. Do not add labels just to recover those capacitors `[sim]`.
- **`--flow magic` used to wipe the KLayout row from `postlayout_summary.json`.** `run_stage`
  now merges the previous `flows` dict so a one-flow rebuild keeps the other flow's numbers.
- **Magic `extresist` segfaults on DC-shorted ports** — the correct topology for a coil (one
  continuous piece of TopMetal2) and for a tap. Fall back to capacitance-only extraction. With the
  coil black-boxed the crash goes away and the pass completes on every cell, so the coil is the
  trigger and the guard-ring taps alone are not `[sim]`.
- **Magic gives no resistance into the netlist as we invoke it.** `ext2spice extresist on` emits zero
  `R` lines flat or hierarchical, with `rthresh 0`, even though `extresist` runs and writes
  `.res.ext` — so it is the netlist writing, not the extraction. Probably a fixable invocation detail;
  deferred by decision. Resistance therefore comes from `klayout.pex`, which needs explicit port
  points that a generated layout already knows.
- **Do not extract resistance if you only want capacitance.** `extract do resistance` re-partitions
  nodes at resistive elements and moves the capacitance by ~11% — 496.43 fF against 549.37 fF on the
  same geometry with the same 39 capacitors `[sim]`.
- **Magic emits capacitors in a nondeterministic order, and their two terminals in a
  nondeterministic order within a line.** Two identical runs gave netlists differing only in
  `C0 mgate inp` versus `C0 inp mgate`. Canonicalise the whole line — sort the terminals as well as
  the lines — before committing an extracted netlist as an artifact.
- **Magic does not recognise the metal-finger cap as a device**: it extracts the fingers with an
  uncalibrated geometric model and reads ~58% high against `cap_cmomi`. Trust the compact model.
- **A gdsfactory GDS needs rewriting for Magic.** kfactory stores state in a `$$$CONTEXT_INFO$$$`
  cell that Magic cannot parse, and `gf.Component()` without a name lands as `Unnamed_1`. Use
  `gds.write_for_magic()`.
- **gdsfactory needs a PDK activated before anything else.** It resolves its default from `$PDK`,
  which `env.sh` sets to `ihp-sg13g2` for the SPICE flow and which is not an importable module.
  Cross-sections must be registered as factories, not instances.
- **`Pad.fR` is a minimum exit length, not a keepout.** The deck takes the metal touching a pad,
  subtracts `dfpad`, and asks whether what is left covers 7 um outward from the pad edge. A pad with
  no feed reports nothing, and a feed only has to run 7 um before it stops or turns. Treating it as a
  7 um exclusion ring around the pad — which the driver's `_PAD_KEEPOUT` did — costs channel width for
  no reason. And the `bondpad_70um` PCell is a *filled* Metal3-to-TopMetal2 via stack over the whole
  70 × 70 um, so a feed leaves on whichever of those five layers is convenient with no stack of its own.
- **A vertical on the ring's own horizontal metal shorts the two supplies.** `add_power_ring` puts
  both nets' horizontals on TopMetal2, so a TopMetal2 trunk crossing the ring joins vdd to vss. The
  driver's pad-channel `vss` trunk did exactly that. Cross a ring on TopMetal1 or below, or give the
  band its own ring and stitch the two with TopMetal1 straps.
- **`PowerRing.ports[net]` is ordered `[top, bottom, left, right]`.** Index 1 is the *bottom*. Using it
  for a strap up to a band above the ring drew a TopMetal1 bar the length of the core, over the coils.
- **A via field is placed about its centre, so the conductor has to be as wide as the field.** Landing a
  2 × 2 TopVia2 field on a run centre-to-centre leaves its outer row of TopMetal1 pads 1.1 um outside the
  strap as islands: 24 × `TM1.b`. Run the strap the full width of both conductors it lands on.
- **A `mos_array` cell box starts 4 um left of its own origin**, because the gate strap inside it
  overhangs the source rail. Derive a guard ring from the placed boxes; off the placement offsets the
  ring sat on that strap and reported `Gat.d` against the tap activ either side of it.
- **A shunt-peaked load cannot have its output column continued past it.** `nlp` sits directly above
  `out` on the same x, and the via stack taking `nlp` up to TopMetal2 passes through Metal5, so a
  Metal5 riser at the column's x shorts the resistor out — LVS reports `nlp1|outp` with both terminals
  of `R` on one net. Offset the riser and jog across at the collector row.
- **A via stack shorts on every layer it passes through, not just its endpoints.** A Metal2-to-Metal5
  stack carries Metal3 and Metal4 pads. One landed in the VGA's `vicm` bus and merged `vicm` into
  `outp`/`outn`; the next one sat 0.04 um from that bus and reported `M3.b`. When budgeting a routing
  band's height, count the via pads of every lane crossing it, not just the lanes' own widths.
- **Two nets that have to cross in x cannot share a y.** The VGA's `em` and `ed1`/`ed2` both climb from
  the steering row to the same emitter row, and each steering drain sits outboard of the emitter it
  feeds. Two lanes fix it, but only in one order: put `em` on the *lower* lane, so its riser crosses
  `ed`'s lane at a y where `ed`'s horizontal has already stopped, and `ed`'s riser sits entirely above
  `em`'s lane. The other order shorts.
- **Route a bus on the side its device's pin faces.** An HBT's base is its *bottom* terminal, so a
  common-mode bus belongs below the pair row; above it, every riser is dragged past that device's own
  emitter and collector. Same argument horizontally: tapping the mirror gate on `mdiode`'s right when
  the port leaves on the left put the whole route inside the MOS row, and it came back merged with the
  substrate and `tx1`.
- **A supply drop is as wide as its rail, and that width has to fit the channel.** An 8.6 um
  `vss_rail_w` drop placed as if it were a thin wire landed 0.3 um inside an array's box, where it
  touched that array's source rail *and* its drain rail on the way past.
- **Two disjoint pieces of geometry carrying the same label are two nets.** The VGA's source rail was
  drawn as two bars with no link and extracted as two nets both named `vss`, only one of which reached
  the ring. A shared rail across a row is safe when every array in that row has that net on the rail —
  check the row, not the habit.
- **`place()` transforms a terminal's centre but not its recorded orientation.** After `M90` a pin that
  reads `orientation=0` is on the cell's *left*. `via_up` derives its stub direction from that field, so
  on a mirrored device it walks the stub into the cell body and onto whatever net is there. It is still
  correct for pins at 90/270, which `M90` does not move.
- **Post-layout numbers measured here** `[sim]`: the rppd load goes 86.71 → 90.68 Ω (+4.6%, exactly
  the 2 × 1.98 Ω of terminal metal), the 0.5 µm-wide rsil 86.63 → 116.8 Ω (+34.9%, the same contact
  resistance the LUT underestimates), the MIM decap +1.2%. Magic and `klayout.pex` agree on a routed
  Metal1 wire to 0.6% (22.486 Ω vs 22.350 Ω). The coil's 13.7 fF of substrate capacitance corroborates
  the ~11.6 fF port capacitance in the EM-fitted `ind_shunt`.

## Agent workflow

- Coordinator plans, reviews, and runs git/PR tooling; **sub-agents implement**.
- **Match the implementer model to the kind of work, not to the repo.** Composer 2.5 is the default, but
  `layout/` goes to **Claude Sonnet 5 thinking** (`model: "claude-sonnet-5-thinking-high"`). Layout is
  finished by diagnosis — reading an extracted netlist's merged nets back to geometry and finding which
  run crosses which on what layer — and Composer 2.5 did not reason through that. On the VGA it produced
  a structurally reasonable two-row floorplan and five shorts, and left the coordinator and the user to
  find all five; the debugging cost more than writing the routing would have. It remains the right tool
  where the work is writing code to a specification.
- **Commit each verified sub-agent result before launching another agent over the same files.** A broad
  multi-task agent that regresses something is otherwise unrevertable, because the good intermediate state
  was never a commit. This happened here: a final agent covering three tasks at once broke the VGA bias
  target, split `VDD` into two values, and destabilized the eye metric, on top of four earlier agents'
  uncommitted work.
- **Verify sub-agent numbers against the files, not the report.** Recurring failure modes seen here: a
  metric computed from design intent rather than the netlist under test (`m = 0.349` reported while the
  netlist realized 0.607), a "fix" that reproduced the reference case only because it sat mid-window, and a
  plausible-looking number defended with an invented physical story rather than debugged.
- When a constraint of yours forces a bad trade, say so and reverse it explicitly. A "peaking <= 2 dB"
  instruction here pushed an agent into shipping a 1.8 dB VGA; the constraint was wrong, not the work.
- **Giving an agent reference values invites it to fit to them.** After being handed target eye widths, an
  agent implemented the measurement with hardcoded acceptance windows (`eye_w_lo, eye_w_hi = 0.63, 0.73`;
  discard candidates wider than 0.85 UI), so it could only ever return a value inside the expected band —
  agreement with the reference then proves nothing, and a genuinely closing eye would be misreported. Give
  references for *validation*, and separately demand a **property-based** acceptance test the implementation
  cannot fake (here: phase invariance under a full-UI offset sweep, and ideal-vs-PDK agreement).
- **"That point is skipped" in a report is a finding, not a footnote.** A silently skipped operating point
  (VGA max gain failing to converge after a supply reduction squeezed `VCE` below its floor) hides a real
  headroom limit. Require the usable range be emitted into the metrics output instead.
- **Give a sub-agent the failure signature, not just the failing rule.** The productive prompts on this
  branch quoted the extracted netlist line, named the `MEMORY.md` entry that describes the same failure,
  and asked for a bisection. The unproductive ones restated the acceptance criteria.
- **A stage layout is not one task.** Splitting it into "write the floorplan" and "make LVS clean" worked;
  asking for both at once produced blocks that were structurally right and electrically shorted, and one
  agent that regressed from 72 DRC violations to 1848 while trying to fix LVS. Commit the structurally
  right version first — that regression was recoverable only because it was.
- **Sub-agents commit and push even when told not to.** Two did here. It was harmless because their work
  was on the feature branch and was reviewed before the next step, but do not rely on the working tree
  being what you left it.
- Feature branch name comes from the current run's instructions (`cursor/<name>-<suffix>`).
- PRs via the ManagePullRequest tool; `gh` is read-only.
- Update every affected `AGENTS.md` (root + nested) before opening or updating a PR.
