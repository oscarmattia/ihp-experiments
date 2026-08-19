# circuits/ — Agent guide

## Agent workflow

- **Coordinator:** Grok.
- **Implementers:** Composer 2.5 sub-agents via the Task tool.
- **Update this AGENTS.md before any PR** that touches `circuits/` or adds experiments.

## Purpose

Circuit-level SPICE experiments (CTLE, drivers, receivers) sized from `char/` LUTs. Each experiment is a self-contained directory with `python/`, `spice/`, `out/`, and its own `AGENTS.md`.

## Environment

Source the EDA toolchain before sizing or simulation:

```bash
source ~/.local/share/ihp-eda/env.sh
```

| Item | Value |
| --- | --- |
| Python | `$IHP_EDA_ROOT/venv/bin/python` |
| `PDK_ROOT` | Set by `env.sh` |
| ngspice | OSDI-enabled (required for IHP models) |

Python scripts assume the caller has already sourced `env.sh` (`PDK_ROOT`, `ngspice` on `PATH`).

## Layout

| Path | Experiment |
| --- | --- |
| [`ctle56n/`](ctle56n/AGENTS.md) | 56 Gb/s NRZ CML CTLE (HBT + LV MOS tail) |

## Conventions

- LUT inputs: `char.common.lut.load_lut` on `char/mos/out/`, `char/bjt/out/`, `char/passive/out/`.
- SPICE corners: HBT `cornerHBT.lib` / `hbt_typ`; MOS LV `cornerMOSlv.lib` / `mos_tt`; RES `cornerRES.lib` / `res_typ`; CAP `cornerCAP.lib` / `cap_typ`.
- Ideal passives first; PDK `rppd` + `cap_cmim` second pass (ideal L — no compact inductor in ngspice).
- Generated parameters: `spice/params.inc` from `python/size_ctle.py`.
- Results: `out/summary.csv`, plots, `op.txt`; commit summaries and PNGs, not raw `.raw` logs.

## Run an experiment

```bash
source ~/.local/share/ihp-eda/env.sh
./circuits/ctle56n/run.sh
```

Or step-by-step from the experiment directory (see nested `AGENTS.md`).
