# 56 Gb/s NRZ CML CTLE (ctle56n)

HBT CML continuous-time linear equalizer with shunt-peaked loads and emitter degeneration, sized from characterization LUTs for IHP SG13G2.

## Targets

- 56 Gb/s NRZ → Nyquist **28 GHz**
- DC gain **−6 … 0 dB** (aim 0 dB)
- Peaking **3–10 dB at 28 GHz** (aim ~6 dB)
- FO1 load: C_L = HBT CIN at max-fT bias
- CMRR **> 6 dB** (Adm/Acm, input common-mode)
- PSRR **> 20 dB** (|vdd/vod|, VDD supply noise on the differential output)
- AC sweeps from 1 MHz to **300 GHz**
- Transient: 56G NRZ **PRBS9** (`x^9+x^5+1`), **511 UI**, **100 mVpp,diff** stimulus
- **Single-bit response (SBR):** isolated **1 UI** pulse (32 UI settle + 1 UI high + 24 UI low), 3 pre / 10 post cursors, 2.5% tap truncation, normalized total ISI
- Bessel shunt-peaking: m = L/(RD² C_L) = 0.32

## Supply voltage

**VDD ≈ 1.58 V** (at scale 1.05 sizing).

At max-fT bias (Nx=1, VBE ≈ 0.95 V, Ic ≈ 2.8 mA), load **RD ≈ 87 Ω** drops ~0.25 V. With **VBASE = 1.23 V** (VBE + tail VDS) and **VCE ≈ 1.0–1.1 V**, **VDD ≈ 1.58 V** keeps the HBT below BVceo (1.6 V).

MOS tail runs at VDS ≈ 0.25–0.3 V (source at 0 V), VGS ≤ 1.2 V.

## Run

```bash
source ~/.local/share/ihp-eda/env.sh
./circuits/ctle56n/run.sh
```

Requires prior LUT generation (`./char/run_all.sh` or at least MOS + BJT + passive R/C sweeps).

Outputs land under `out/`:

- `summary.csv` — combined DC gain, peaking, G_peak, f_peak, f_{−3dB}, CMRR, PSRR (ideal and PDK)
- `ctle_report.md` — design narrative + sizing table (auto-generated)

Per-pass directories `out/ideal/` and `out/pdk/` (same file set in each):

| File | Description |
| --- | --- |
| `op.txt` | DC operating point |
| `metrics.csv` | Per-pass metrics (+ SBR when run) |
| `ac_diff.png`, `ac_diff.csv` | Bode + group delay (28 GHz, f_peak, f_{−3dB}) |
| `cmrr.png`, `psrr.png` | CMRR / PSRR vs frequency |
| `tran_se.png`, `tran_diff.png` | PRBS transient (full 511 UI + 40-UI zoom) |
| `tran.csv` | Full transient waveform |
| `eye_se.png`, `eye_diff.png` | 2-UI eye diagrams |
| `eye_se.csv`, `eye_diff.csv` | Folded post-settle eye samples (t_ui 0–2) |
| `sbr.png`, `sbr.csv`, `sbr_taps.csv` | Single-bit pulse response |
| `work/` | ngspice scratch (gitignored) |

Both **ideal** and **PDK** passes run PRBS transient, SBR, and AC plots. Use `--no-tran` to skip PRBS and SBR for both passes.

Device currents in DC OP require explicit `save` lines in the testbench (see AGENTS.md).

## PDK passives

Second pass replaces ideal RD and Cs (when practical) with `rppd` and `cap_cmim`. Inductors remain **ideal** — no compact spiral model in ngspice; minimum EM cell `l2n0` (~2 nH) is too large for this shunt-peaking network.

RPPD sizing picks the LUT entry closest to **rd/0.88** to offset contact resistance and keep PDK DC gain within −6…0 dB.

If Cs < ~73 fF (7×7 µm MIM), the README notes ideal Cs is kept in the PDK netlist.

See [AGENTS.md](AGENTS.md) for topology and agent contracts.
