# Agent memory — ihp-experiments

Factual notes for Cloud Agents (not a user README). Update when new PDK/sim pitfalls are found.

## Environment

- Always `source ~/.local/share/ihp-eda/env.sh` before sims.
- Python: `$IHP_EDA_ROOT/venv/bin/python`, not system `python3` (matplotlib may need `uv pip install matplotlib` in that venv).
- `PDK_ROOT=$IHP_EDA_ROOT/IHP-Open-PDK`. ngspice must be **OSDI-enabled** (OpenVAF IHP models).
- Install/verify: `./scripts/install-ihp-eda.sh`, `./scripts/verify-ihp-eda.sh`. Details: `docs/ENVIRONMENT.md`.

## ngspice / IHP models

- **Device currents are not saved unless listed in `save` before analysis.** `.options savecurrents` and `save all` are insufficient.
- HBT probe path: `@q.xu1.xq1.qnpn13g2[ic]` (prefix `q.`, instance under subckt `xu1.xq1`, model `qnpn13g2`). Pins of the DUT are top-level `v(outp)` not `v(xu1.outp)`.
- MOS LV PSP: parameter is **`ids` not `id`**: `@n.xu1.xtail.nsg13_lv_nmos[ids]`. Error `no such parameter id` means the device was found.
- Nested-dc `wrdata` **freezes** small-signal gm/caps — BJT fT uses a separate OP+AC pass (`char/bjt/ihp_bjt_sweep.py`).
- `wrdata` of real transient vectors often emits **time twice**: `time, time, v(outp), v(outn), …` even with `set wr_singlescale`. Parse 6-col as `[:,0], [:,2], [:,3], [:,4], [:,5]`. AC mag/phase wrdata has a different layout (see `parse_ac_raw`).
- PWL sources: use `PWL(` with `+` continuation lines; complementary PRBS on inp/inn.
- `.param` MOS `w`/`l` and `rppd` `w`/`l` must be **meters** (not `{W}u` tokens).
- Load order for shunt peaking: **VDD → L → RD → collector** (not RD then L to VDD in a way that kills DC gain).
- PSRR of a matched differential pair can be numerically infinite (`vod≈0`); clip to ~120 dB.
- BVceo of npn13G2 ~1.6 V — VDD for max-fT CML is ~1.6 V, not 2.5 V.
- MOS tail at high VGS can be **triode** if VDS < Vov; size tail at VGS 0.55–0.70 V so Vov < VDS (~0.25–0.35 V).
- Ideal L for 25 pH shunt peaking: no compact IHP inductor that small (`l2n0` ~2 nH). Keep L ideal in ngspice.
- rppd LUT/analytical Rsh underestimates contact R → PDK DC gain high; size rppd toward ~rd/0.88 or LUT R.

## Agent workflow

- Root `AGENTS.md`: Grok coordinates; Composer 2.5 implements.
- Feature branches `cursor/<name>-a928` (this agent) / `-b7e8` (repo convention). PRs via ManagePullRequest, not `gh` writes.
- Update nested `AGENTS.md` when directory contracts change.
