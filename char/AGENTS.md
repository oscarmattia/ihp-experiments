# char/ — Agent guide

## Agent workflow

- **Coordinator:** Grok (this repo’s top-level agent).
- **Implementers:** Composer 2.5 sub-agents via the Task tool.
- **Update this AGENTS.md before any PR** that touches `char/` or its subdirectories.

## Purpose

Precomputed lookup tables (LUTs) for IHP SG13G2 so design-space browsing and sizing tools can avoid re-running SPICE on every query.

## Layout

| Path | Role |
| --- | --- |
| [`mos/`](mos/AGENTS.md) | LV/HV × core/RF CMOS (gm/ID-style LUTs) |
| [`bjt/`](bjt/AGENTS.md) | SiGe NPN flavors + lateral PNP (DC + HBT fT/Cin AC) |
| [`passive/`](passive/AGENTS.md) | R / L / C LUTs (ngspice + optional openEMS) |
| [`common/`](common/AGENTS.md) | Shared `.npz` I/O (`char.common.lut`) |
| [`run_all.sh`](run_all.sh) | Runs all implemented suites |

## Data format

- **Primary:** compressed NumPy **`.npz`** via `char.common.lut` (axis arrays + JSON metadata).
- **MOS only:** **`.pkl`** pickle LUTs for `pygmid.Lookup` compatibility (dual-written with `.npz`).
- **Summaries:** `summary.csv` and PNG plots under each suite’s `out/`.

## Commands

Source the EDA environment first:

```bash
source ~/.local/share/ihp-eda/env.sh
```

Run everything implemented:

```bash
./char/run_all.sh
./char/run_all.sh --skip-em   # passive inductors: summarize existing .npz only
```

Or run a suite directly:

```bash
./char/mos/run_all.sh [--only lv_core lv_rf …]
./char/bjt/run_all.sh [--quick] [--device npn13G2 …]
./char/passive/run_all.sh [--skip-em] [--quick]
```

Passive EM tier (openEMS primary on ~16 GB; Palace optional): see
[`passive/README.md`](passive/README.md) and [docs/ENVIRONMENT.md](../docs/ENVIRONMENT.md).

## Nested docs

Each subdirectory owns its own `AGENTS.md`. Read and update the relevant file before changing that area:

- [`common/AGENTS.md`](common/AGENTS.md)
- [`mos/AGENTS.md`](mos/AGENTS.md)
- [`bjt/AGENTS.md`](bjt/AGENTS.md)
- [`passive/AGENTS.md`](passive/AGENTS.md)
