# circuits/ctle56n/ — 56 Gb/s NRZ CML CTLE

## Agent workflow

- **Coordinator:** Grok.
- **Implementers:** Composer 2.5 sub-agents.
- **Update this AGENTS.md before any PR** that touches this experiment.

## Targets

| Item | Value |
| --- | --- |
| Rate | 56 Gb/s NRZ, Nyquist **28 GHz** |
| DC gain | **−6 dB to 0 dB** (aim 0 dB) |
| Peaking | **3–10 dB at 28 GHz** (aim ~6 dB) |
| Load | R+L shunt peaking, Bessel MFD **m = L/(RD² C_L) = 0.32** |
| Fan-out | **FO1**: C_L = CIN of one npn13G2 input at max-fT bias |
| CMRR | **> 6 dB** at low frequency (`Adm/Acm`, input common-mode) |
| PSRR | **> 20 dB** at low frequency (`|vdd/vod|`, VDD noise → differential out) |
| Analyses | DC OP + AC differential (to **300 GHz**), CM, PSRR + transient 56G NRZ PRBS |

## Topology

CML CTLE: HBT differential pair (`npn13G2`), shunt-peaked loads (RD + ideal L), emitter degeneration (Rs + Cs), LV NMOS tail + 1:1 diode-connected mirror with ideal tail current from VDD.

Corners: `.lib cornerHBT.lib hbt_typ` + `.lib cornerMOSlv.lib mos_tt`.

## LUT inputs

| LUT | Path | Use |
| --- | --- | --- |
| HBT | `char/bjt/out/sg13_npn13G2.npz` | FT, CIN, GM, IC vs VBE/VCE; bias at max FT |
| MOS | `char/mos/out/lv_core_n.npz` | Tail W/L for I_tail at VDS ≈ 0.25–0.3 V |
| R | `char/passive/out/sg13_rppd.npz` | PDK RD sizing (`rppd`, LUT closest to rd/0.88) |
| C | `char/passive/out/sg13_cap_cmim.npz` | PDK Cs if area ≥ ~7×7 µm (~73 fF); else ideal Cs |

## Passives

- **First pass:** ideal R, L, C in `spice/ctle_ideal.cir`.
- **Second pass:** PDK `rppd` + `cap_cmim` in `spice/ctle_pdk.cir`; **L stays ideal** (no ngspice compact inductor; EM `l2n0` ~2 nH is too large).

## ngspice `save` requirement

Device currents are **not** available for `print`/`wrdata` unless listed explicitly in `save` before the analysis. `.options savecurrents` and `save all` are insufficient. HBT: `@q.xu1.xq1.qnpn13g2[ic]`. MOS LV PSP: **`ids`** (not `id`) at `@n.xu1.xtail.nsg13_lv_nmos[ids]`.

## Transient (56G NRZ)

- PRBS7 (`x^7+x^6+1`), ≥254 bits, 100 mVpp,diff (±50 mV vid), ~4.5 ps edges
- Stimulus generated at runtime into `work_*/prbs_stim.inc` (not committed)
- Ideal pass only by default (`--no-tran` to skip); PDK transient skipped to keep runtime reasonable
- Plots: `tran_se.png`, `tran_diff.png`, `eye_se.png`, `eye_diff.png`

## Files

| Path | Role |
| --- | --- |
| `python/size_ctle.py` | LUT sizing → `spice/params.inc` |
| `python/run_sims.py` | ngspice DC/AC/tran, plots, `out/summary.csv` |
| `spice/ctle_ideal.cir` / `ctle_pdk.cir` | DUT subcircuits |
| `spice/tb_dc.cir`, `tb_ac_diff.cir`, `tb_ac_cm.cir`, `tb_ac_psrr.cir`, `tb_tran.cir` | Testbenches |
| `run.sh` | Source env + size + sims |
| `out/` | Results (commit `summary.csv`, PNG, `op.txt`) |

## Commands

```bash
source ~/.local/share/ihp-eda/env.sh
./circuits/ctle56n/run.sh
```

Or:

```bash
source ~/.local/share/ihp-eda/env.sh
$IHP_EDA_ROOT/venv/bin/python circuits/ctle56n/python/size_ctle.py
$IHP_EDA_ROOT/venv/bin/python circuits/ctle56n/python/run_sims.py
$IHP_EDA_ROOT/venv/bin/python circuits/ctle56n/python/run_sims.py --no-tran   # skip transient
```

`run_sims.py` runs ideal pass first, iterates RD/Rs/Cs/L if targets miss, then PDK passives. PSRR is clipped to 120 dB when `vod≈0` (matched differential).
