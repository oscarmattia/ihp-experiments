# char/mos/ — Agent guide

## Agent workflow

- **Coordinator:** Grok.
- **Implementers:** Composer 2.5 sub-agents via the Task tool.
- **Update this AGENTS.md before any PR** that touches `char/mos/`.

## Purpose

DC / gm/ID characterization LUTs for all released SG13G2 CMOS classes. Follows pygmid methodology but generates IHP-aware ngspice netlists.

## Device families

| Family | Models | `rfmode` | Notes |
| --- | --- | --- | --- |
| `lv_core` | `sg13_lv_nmos/pmos` | 0 | 1.2 V core |
| `lv_rf` | same | 1 | LV RF / NQS card |
| `hv_core` | `sg13_hv_nmos/pmos` | 0 | 3.3 V core |
| `hv_rf` | same | 1 | HV RF / NQS card |

Each family produces **n** and **p** tables. There are **no LVT/SVT/HVT implant flavors** in the open PDK — LV vs HV and core vs RF is the full matrix.

## Key scripts

| File | Role |
| --- | --- |
| `ihp_sweep.py` | SPICE sweeps; **dual-writes** `{family}_{n\|p}.pkl` + `.npz` |
| `summarize.py` | Reads LUTs; **prefers `.npz`**, falls back to `.pkl` |
| `run_all.sh` | Full sweep + summarize (supports `--only`) |

Shared I/O: `char.common.lut` (`save_lut`, `parse_wrdata`, `matrange`).

## Outputs (`char/mos/out/`)

- `{family}_n.pkl` / `{family}_p.pkl` — pygmid-compatible (for `from pygmid import Lookup`)
- `{family}_n.npz` / `{family}_p.npz` — same arrays + metadata (preferred for browsing)
- `summary.csv`, `{family}_gm_id.png`, `{family}_idvg.png`, `vth_comparison.png`

## Run

```bash
source ~/.local/share/ihp-eda/env.sh
./char/mos/run_all.sh
./char/mos/run_all.sh --only lv_core lv_rf
```

Or via parent: `./char/run_all.sh`.

## Agent notes

- Upstream pygmid netlists hardcode LV probe names; keep pickle dict keys compatible with pygmid when editing sweep logic.
- RF vs core DC (Id, Vth, gm) should match to numerical noise; capacitance columns differ at ~fF level for RF.
- Grids are coarse by default; densify via `default_families()` in `ihp_sweep.py`.
- See also [`../common/AGENTS.md`](../common/AGENTS.md) for `.npz` format rules.
