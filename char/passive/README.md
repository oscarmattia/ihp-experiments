# IHP SG13G2 passive device characterization

DC / AC SPICE LUTs for resistors and capacitors, plus optional **openEMS** inductor
characterization. Archives use `char.common.lut` (`.npz` + JSON metadata) so MOS / BJT /
passive tables load the same way.

## Device matrix

| Class | Models / cases | Sweep axes | Solver |
| --- | --- | --- | --- |
| **Resistors** | `rsil`, `rppd`, `rhigh` | W × L × T (−40 / 27 / 125 °C) | ngspice DC |
| **Capacitors** | `cap_cmim`, `cap_cmomi`, `sg13_moscap_n`, `sg13_moscap_p` | geometry; MOSCAP also V | ngspice AC @ 100 MHz |
| **Inductors** | `l2n0`, `turn1`, `turn2` | L(f), Q(f) | openEMS (primary) |

### Inductor cases

| Case | Geometry | Status |
| --- | --- | --- |
| `l2n0` | Canonical PDK smoke GDS (`L_2n0_twoport.gds`) | **Validated** — L ≈ 2 nH @ 10 GHz |
| `turn1` | 1-turn synthesized octagon (TopMetal2) | **Experimental** — geometry synthesis |
| `turn2` | 2-turn synthesized octagon (TopMetal1) | **Experimental** — geometry synthesis |

Committed artifacts under `char/passive/out/` include `.npz`, `.meta.json`, `*_summary.csv`,
and PNG plots. Transient openEMS working files live in `out/em_work/` (gitignored).

## EM toolchain

Inductor sweeps need the optional EM tier:

```bash
./scripts/install-ihp-eda.sh --with-em
# or: ./scripts/install-ihp-em.sh
source ~/.local/share/ihp-eda/env.sh
./scripts/verify-ihp-em.sh
```

| Tier | When to use | Notes |
| --- | --- | --- |
| **openEMS** | **Primary** on ~16 GB hosts | Built by `install-ihp-em.sh`; Python workflow under `$PDK_ROOT/.../openems_ihp_sg13g2/workflow` |
| **Palace** | Optional FEM on large hosts | ≥32 GB RAM + Apptainer; pulled only when MemTotal ≥28 GB |

Headless runs skip the AppCSXCAD GUI; the installer provides a no-op stub at
`$IHP_EDA_ROOT/tools/bin/AppCSXCAD` (see [docs/APPCSXCAD-STUB.md](../../docs/APPCSXCAD-STUB.md)).

## Run

```bash
source ~/.local/share/ihp-eda/env.sh

# Full passive suite (R → C → L)
./char/passive/run_all.sh

# Skip openEMS; re-summarize existing inductor .npz only
./char/passive/run_all.sh --skip-em

# Per class
./char/passive/run_res.sh
./char/passive/run_cap.sh
./char/passive/run_ind.sh
./char/passive/run_ind.sh --skip-em

# Subset / quick grids (passed through to Python sweep scripts)
./char/passive/run_res.sh --quick
./char/passive/run_ind.sh --cases l2n0
```

Via parent orchestrator (MOS + BJT + passives):

```bash
./char/run_all.sh
./char/run_all.sh --skip-em   # passives skip EM only
```

## Outputs (`char/passive/out/`)

| Pattern | Contents |
| --- | --- |
| `sg13_{model}.npz` / `.meta.json` | Resistor LUTs (`rsil`, `rppd`, `rhigh`) |
| `sg13_cap_*.npz`, `sg13_moscap_*.npz` | Capacitor LUTs |
| `sg13_ind_{case}.npz` | Inductor L(f), Q(f) |
| `res_summary.csv`, `cap_summary.csv`, `ind_summary.csv` | Human-readable tables |
| `res_*.png`, `cap_*.png`, `ind_*_LQ.png` | Summary plots |

Agent contracts: [`AGENTS.md`](AGENTS.md). Shared LUT I/O: [`../common/lut.py`](../common/lut.py).
