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
| `ihp_ind_em.py` | openEMS L(f), Q(f) for `l2n0`, `turn1`, `turn2` |
| `summarize_ind.py` | `ind_summary.csv` + L/Q vs frequency plots |
| `run_ind.sh` | EM sweep + summarize; supports `--skip-em` |
| `run_all.sh` | Runs `run_res.sh`, `run_cap.sh`, `run_ind.sh`; parses `--skip-em` |

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

| Case | Source | Validation |
| --- | --- | --- |
| `l2n0` | PDK `L_2n0_twoport.gds` | **Production** — ~2 nH @ 10 GHz |
| `turn1` | Synthesized 1-turn octagon | **Experimental** |
| `turn2` | Synthesized 2-turn octagon | **Experimental** |

CLI: `--cases l2n0 turn1`, `--preview-only`, `--coarse` / `--no-coarse`.

## Outputs (`char/passive/out/`)

**Commit:** `.npz`, `.meta.json`, `*_summary.csv`, PNG plots.

**Do not commit:** `out/em_work/` (openEMS mesh / S-parameter scratch; listed in root
`.gitignore`).

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
