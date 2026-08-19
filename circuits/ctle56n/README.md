# 56 Gb/s NRZ RX front end (ctle56n)

An IHP SG13G2 receiver front end sized from characterization LUTs: **50 Ω/ESD termination → HBT CML CTLE
→ current-steering VGA**, plus a combined chain testbench. The directory keeps its original `ctle56n`
name; the CTLE is still the centrepiece, and the surrounding stages share its sizing, simulation, and
measurement code.

## Stages

| Stage | What it is |
| --- | --- |
| **Termination** | Bond pad, primary ESD (`diodevdd_2kv` + `diodevss_2kv`) with an `nmoscl_2` rail clamp, 50 Ω per leg to an on-chip ~1.4 V common mode from an `rppd` divider with a `cap_cmim` decap |
| **CTLE** | HBT differential pair, shunt-peaked loads, `Rs`∥`Cs` emitter degeneration, two per-emitter tail current sources |
| **VGA** | Fixed signal pair plus a dummy pair sharing the loads, with the tail current steered between them — frequency-flat gain control |
| **Chain** | All three cascaded, DC-coupled, driven from a 50 Ω per leg source |

## Targets

- 56 Gb/s NRZ → Nyquist **28 GHz**; AC swept to **300 GHz**
- CTLE DC gain **−6 … 0 dB**, peaking **3–10 dB at 28 GHz**
- Bessel shunt peaking **m = L/(RD²·C_L) = 0.32** (accepted 0.30–0.45), from *realized* device values
- Fan-out: the CTLE sees **FO1** (one VGA input unit); the VGA drives **FO2** (2× its own input)
- CMRR **> 6 dB**, PSRR **> 20 dB**
- VGA range **≥ 10 dB measured at 28 GHz**
- Transient: **PRBS9** (`x^9+x^5+1`), 511 UI, 100 mVpp,diff
- **SBR**: isolated 1 UI pulse, 3 pre / 10 post cursors, **0.5%** tap truncation, normalized total ISI

## Design points worth knowing

- **Load capacitance is budgeted honestly**: the Miller-inclusive input capacitance of the next stage
  (`CBE + CBC·(1+|Av|)`) plus interconnect at ~0.17 fF/µm. Using the raw LUT `CIN` under-loads the stage and
  makes it look over-peaked.
- **The coil's port capacitance is excluded from `C_L`** — it sits on the internal node between `L` and
  `RD`, where it resonates with the coil instead of loading the output.
- **`RD` is set by the Bessel condition, not by the gain target.** The smallest buildable coil (PCell
  `dmin` = 25.35 µm → ~39 pH) puts a floor on it; gain is trimmed with `Rs`.
- **Gain control is current steering, not variable degeneration.** Varying `Rs` moves DC gain ~10 dB but
  in-band gain at 28 GHz only ~2 dB, because the emitter capacitance shorts the degeneration out — it is a
  variable equalizer, not a variable gain stage. VGA range is therefore always quoted at 28 GHz.
- **Two per-emitter tail sources**, so the degeneration resistor carries no DC and the tail device actually
  gets the `VDS` the sizing assumes.

## Run

```bash
source ~/.local/share/ihp-eda/env.sh
./circuits/ctle56n/run.sh                 # termination -> CTLE -> VGA -> summary
./circuits/ctle56n/run.sh --with-chain    # also the combined chain
./circuits/ctle56n/run.sh --no-tran       # skip PRBS + SBR everywhere
```

Requires prior LUT generation (`./char/run_all.sh`, or at least the MOS, BJT, and passive R/C/L sweeps).

## Outputs

`out/summary.csv` aggregates every pass. Auto-generated markdown reports (`ctle_report.md`, `term_report.md`,
`vga_report.md`, `driver_report.md`, `chain_report.md`) are built by `python/generate_reports.py` from
committed `out/` CSVs — no ngspice re-run. Per-pass directories `out/{term,ideal,pdk,vga_ideal,vga_pdk,driver,chain}/` each contain:

| File | Description |
| --- | --- |
| `op.txt` | DC operating point |
| `metrics.csv` | Per-pass metrics, including realized `RD` and `m`, eye metrics, and SBR |
| `ac_diff.png`, `ac_diff.csv` | Bode + group delay with 28 GHz, `f_peak`, `f_-3dB` markers |
| `cmrr.png`, `psrr.png` | CMRR / PSRR vs frequency |
| `zin.png`, `zin.csv` | Input impedance and return loss (termination and chain), 100 Ω differential reference |
| `tran_se.png`, `tran_diff.png`, `tran.csv` | PRBS transient |
| `eye_se.png`, `eye_diff.png` + CSVs | Eye diagrams (CSVs are 2-UI folded samples) |
| `sbr.png`, `sbr.csv`, `sbr_taps.csv` | Single-bit response and taps |
| `work/` | ngspice scratch (gitignored) |

Eye height is the vertical opening at the optimal sampling phase and eye width the threshold crossings
around it — both measured on a phase-centred fold, and peak-to-peak swing is reported separately. Device
currents in the DC operating point require explicit `save` lines; see [AGENTS.md](AGENTS.md).

## PDK passives

The PDK pass is fully real: `rppd` loads, `rsil` degeneration and 50 Ω termination, a `cap_cmomi`
metal-only finger cap for degeneration (`feed=same` to avoid its in-band self-resonance, `mmin=2` to keep
M1 off the substrate), `cap_cmim` decoupling, and an EM-extracted lumped inductor.

SG13G2 has **no ngspice inductor model at all**, so shunt-peaking coils are openEMS-extracted as small
1-turn TopMetal2 octagons, fitted to a lumped model by `python/size_ind.py`, and verified against the EM
S-parameters with ngspice `sp` analysis. Resistors are sized by `op` measurement rather than sheet
resistance, since head and contact resistance dominate and do not scale with `L/W`.

See [AGENTS.md](AGENTS.md) for topology details and agent contracts, and
[`../../MEMORY.md`](../../MEMORY.md) for the accumulated pitfalls.
