# char/passive/ — Agent guide

## Agent workflow

- **Coordinator:** Grok.
- **Implementers:** Composer 2.5 sub-agents via the Task tool.
- **Update this AGENTS.md before any PR** that touches `char/passive/`.

## Purpose

Precomputed R / C / L lookup tables for IHP SG13G2 passives. Resistors and capacitors use
ngspice; inductors use the PDK openEMS workflow (Palace optional, not required for the
committed `l2n0` smoke case).

## Scripts

| File | Role |
| --- | --- |
| `ihp_res_sweep.py` | DC R(W,L,T) for `rsil`, `rppd`, `rhigh` → `sg13_{model}.npz` |
| `summarize_res.py` | `res_summary.csv` + R vs geometry plots |
| `run_res.sh` | Sweep + summarize resistors |
| `ihp_cap_sweep.py` | AC C for MIM / MoM / MOSCAP → `sg13_cap_*.npz`, `sg13_moscap_*.npz` |
| `summarize_cap.py` | `cap_summary.csv` + C(V) / density plots |
| `run_cap.sh` | Sweep + summarize capacitors |
| `ihp_ind_em.py` | openEMS L(f), Q(f) for inductor cases (see matrix below); `--refresh-pimodel`, `--refresh-sparams` persist the 2-port fit and S-matrix into the committed `.npz` |
| `summarize_ind.py` | `ind_summary.csv` + L/Q vs frequency plots; flags invalid EM; `--sp-validate` writes the S-parameter validation CSV/plots |
| `ind_validate.py` | Shared L/Q sanity checks (`valid`, `invalid_reason`) |
| `ind_pimodel.py` | 2-port pi-model extraction from the EM Touchstone and ngspice `sp` verification (`run_ind_shunt_sp`, `verify_ind_shunt_sp`, `compare_sparams`, `load_em_sparams`) |
| `run_ind.sh` | EM sweep + summarize; supports `--skip-em` |
| `run_all.sh` | Runs `run_res.sh`, `run_cap.sh`, `run_ind.sh`; parses `--skip-em` |
| `render_layouts.py` | KLayout batch GDS→PNG for MIM/MoM/MOSCAP + inductor cells |
| `run_render_layouts.sh` | Driver for layout screenshots → `out/layouts/` |

Shared I/O: `char.common.lut` (`save_lut`, `load_lut`, `parse_wrdata`, `matrange`).

## Device / sweep matrix

### Resistors (`ihp_res_sweep.py`)

| Key | Subckt | W [µm] | L [µm] | T [°C] |
| --- | --- | --- | --- | --- |
| `rsil` | rsil | 0.5–5 | 0.5–5 | −40, 27, 125 |
| `rppd` | rppd | 0.5–5 | 0.5–5 | −40, 27, 125 |
| `rhigh` | rhigh | 0.5–5 | 0.96–5 | −40, 27, 125 |

`--quick` halves the W/L grid.

### Capacitors (`ihp_cap_sweep.py`)

| Key | Type | Geometries |
| --- | --- | --- |
| `cap_cmim` | MIM | 7×7 … 50×50 µm |
| `cap_cmomi` | interdigitated MoM | 5×5 … 30×30 µm |
| `sg13_moscap_n` / `_p` | MOSCAP | W=L ∈ {1, 3, 10, 30} µm; V ∈ {−1 … 1} V |

### Inductors (`ihp_ind_em.py`)

| Case | Source | f\_stop | Validation |
| --- | --- | --- | --- |
| `l2n0` | PDK `L_2n0_twoport.gds` | 30 GHz | **Production** — ~2 nH @ 10 GHz |
| `turn1` | Synthesized 1-turn octagon, d=120 µm | 30 GHz | **Experimental**, plausible |
| `turn1_d40` | Synthesized 1-turn octagon, d=40 µm, w=4 µm, s=2.1 µm | 100 GHz | Small coil for CTLE peaking |
| `turn1_d60` | Synthesized 1-turn octagon, d=60 µm | 100 GHz | Small coil for CTLE peaking |
| `turn1_d80` | Synthesized 1-turn octagon, d=80 µm | 100 GHz | Small coil for CTLE peaking |
| `turn2` | Synthesized 2-turn octagon | 30 GHz | **Invalid** — negative L, Q=0 (bad ports) |

All synthesized small cases use **TopMetal2** with the same SUBGND → top-metal via-port
convention as `turn1`. Per-case `fstop_hz` keeps legacy cases on 0–30 GHz so committed
`.npz` files do not churn.

`summarize_ind.py` adds `valid` and `invalid_reason` to `ind_summary.csv` (and plots mark
`[INVALID]`). Criteria: `L` not finite above DC, `L(low‑f) ≤ 0`, or `Q_peak ≤ 0`.

CLI: `--cases l2n0 turn1_d40`, `--preview-only`, `--coarse` / `--no-coarse`.

### 2-port pi-model and S-parameter validation

`L(f)`/`Q(f)` alone is not enough to build a SPICE model, and `Q(f)` is unusable for these small coils
(it swings between roughly ±1500 because 65 pH at 10 GHz is only ~4 Ω of reactance). So `ind_pimodel.py`
extracts the full pi-model from the EM Touchstone with `Y = (1/Z0)(I+S)⁻¹(I−S)`:

- series `Z = −1/Y12`, `C_port1 = imag(Y11+Y12)/ω`, `C_port2 = imag(Y22+Y12)/ω`, `G_port = real(Y11+Y12)`
- **port capacitance is the dominant parasitic**, ~5–10 fF per 100 pH; plate area underestimates it ~6x
- `Re{Z_series}` carries a systematic **de-embedding offset of about −0.4 Ω** and goes negative below
  ~15 GHz, so anchor DC resistance to the PDK sheet resistance and take only the shape from EM above 20 GHz
- `G_port` is characterized but deliberately **not** consumed by the emitted model: the ITF and openEMS
  stacks both model oxide as permittivity-only, so dielectric loss is zero by construction and the residual
  is substrate coupling. If that ever needs modelling, use coupled inductors with parallel resistors
  (induced substrate current loops), not a lumped port resistor.

`em_work/` is gitignored, so the extracted pi-model **and** a downsampled S-matrix are persisted into the
committed `.npz` (`--refresh-pimodel`, `--refresh-sparams`), which is what lets the model be verified on a
fresh checkout. Verification uses ngspice **`sp` analysis** (ports declared on voltage sources with
`portnum`/`z0`) to compare the lumped model against the EM data in S-parameter space, gated on `|S21|`
magnitude and phase — `|S11|` is reported but not gated, being small and worst where the de-embedding
offset lives. Artifacts: `ind_sp_validate_summary.csv`, `ind_sp_validate_{case}.csv/.png`.

## Outputs (`char/passive/out/`)

**Commit:** `.npz`, `.meta.json`, `*_summary.csv`, PNG plots, `layouts/*.png` (+ `layouts/gds/`).

**Do not commit:** `out/em_work/` (openEMS mesh / S-parameter scratch; listed in root
`.gitignore`).

## Layout screenshots

```bash
source ~/.local/share/ihp-eda/env.sh
./char/passive/run_render_layouts.sh
```

Captures PDK GDS cells (MIM, RFMOM, MOSCAP, `L_2n0`, 1-/2-turn) and EM-synthesized
`turn1` / `turn2` spirals. See README **Layouts** section for the image gallery.

## EM notes

1. Source both `env.sh` and `em.env.sh` before inductor runs (`run_ind.sh` does this).
2. **openEMS** is the primary solver on ~16 GB Cloud Agent hosts
   (`./scripts/install-ihp-em.sh` or `--with-em`).
3. **Palace** FEM is optional (≥32 GB RAM + Apptainer); not needed for committed LUTs.
4. `BUILD_APPCSXCAD=NO` at install time; headless workflow uses the
   `$IHP_EDA_ROOT/tools/bin/AppCSXCAD` stub — see
   [`../../docs/APPCSXCAD-STUB.md`](../../docs/APPCSXCAD-STUB.md).
5. `run_ind.sh --skip-em` skips `ihp_ind_em.py` and only re-runs `summarize_ind.py`
   (useful when `.npz` artifacts are already present).
6. `ihp_ind_em.py` writes placeholder `.npz` with `em_completed: false` when openEMS is
   missing; sweeps do not hard-fail the shell driver.

## Run

```bash
source ~/.local/share/ihp-eda/env.sh
./char/passive/run_all.sh
./char/passive/run_all.sh --skip-em
```

Parent: `./char/run_all.sh` (after MOS + BJT).

## Agent notes

- Reuse `char.common.lut`; do not fork `.npz` I/O.
- No pygmid pickle for passives.
- Resistor / capacitor sweeps require `PDK_ROOT` and ngspice with OSDI.
- When adding devices, update sweep script, summarize script, this file, and
  [`README.md`](README.md).
- See [`../common/AGENTS.md`](../common/AGENTS.md) for shared helper contracts.
