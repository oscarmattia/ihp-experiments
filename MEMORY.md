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
  `wrdata` has a different layout — see `parse_ac_raw`.
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
   coordinator. What matters is the split: coordinator plans/reviews/runs git and PR tools, Composer 2.5
   sub-agents write the code.
8. **`pdk/` is not a populated submodule** — it is gitignored and empty. The PDK lives at `$PDK_ROOT`.
9. **`sg13g2_bondpad.lib` is an empty placeholder** with no electrical content `[model]`; pad
   capacitance has to be hand-modelled.

## Design notes that keep coming back

- Shunt-peaking load order is **VDD → L → RD → collector**.
- Bessel maximally-flat-delay shunt peaking: `m = L/(RD^2*C_L) = 0.32`.
- **Budget `C_L` honestly.** Raw LUT `CIN` of the next stage is not the load: add the Miller term
  `CBC*(1+|Av|)` and an interconnect estimate. Under-budgeting `C_L` yields an implausible `f_-3dB` and
  a design that looks over-peaked.
- **The Magic `defaultareacap` values are aF/µm², not fF/µm².** Misreading them is a 1000x capacitance
  error; it has happened here. Cross-check against the MIM row (`1500` aF/µm² = the independently known
  1.5 fF/µm²). Also: stacked plates shield each other, so use the bottom plate only, and area
  capacitance alone badly underestimates a narrow signal route (use ~0.15-0.2 fF/µm for wires). See
  [docs/PDK.md](docs/PDK.md).
- MOS tail devices at high `VGS` can slip into triode if `VDS < Vov`; size for `VGS` 0.55–0.70 V.
- One 2 kV ESD diode pair (`diodevdd_2kv` + `diodevss_2kv`) loads a pad with **50.9 fF** at 1.4 V
  `[sim]`. That dominates a 28 GHz input and is why the 50 Ω shunt termination matters.
- SBR: cursor = max `|vod_ac|` in the first 3 UI after an isolated 1-bit pulse; sample taps every UI.

## Agent workflow

- Coordinator plans, reviews, and runs git/PR tooling; **Composer 2.5 sub-agents implement**.
- Feature branch name comes from the current run's instructions (`cursor/<name>-<suffix>`).
- PRs via the ManagePullRequest tool; `gh` is read-only.
- Update every affected `AGENTS.md` (root + nested) before opening or updating a PR.
