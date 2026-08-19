# circuits/ — Agent guide

## Agent workflow

- **Coordinator:** the run's assigned coordinator model (Grok or Claude Opus 5).
- **Implementers:** Composer 2.5 sub-agents via the Task tool.
- **Update this AGENTS.md before any PR** that touches `circuits/` or adds experiments.
- Read [`../MEMORY.md`](../MEMORY.md) and [`../docs/PDK.md`](../docs/PDK.md) before sizing anything —
  they hold verified device facts and prior corrections.
- Layout for these circuits lives in [`../layout/`](../layout/AGENTS.md); it reads
  `ctle56n/spice/params.inc`, so a resize propagates rather than invalidating the layout.

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
| [`ctle56n/`](ctle56n/AGENTS.md) | 56 Gb/s NRZ RX front end: 50 Ω/ESD termination → CML CTLE → current-steering VGA, plus the combined chain |

## Conventions

- LUT inputs: `char.common.lut.load_lut` on `char/mos/out/`, `char/bjt/out/`, `char/passive/out/`.
- SPICE corners: HBT `cornerHBT.lib` / `hbt_typ`; MOS LV `cornerMOSlv.lib` / `mos_tt`; RES `cornerRES.lib` / `res_typ`; CAP `cornerCAP.lib` / `cap_typ`; DIO `cornerDIO.lib` / `dio_tt`.
- **Ideal passives first, then a fully real PDK pass** — real means `rppd`/`rsil` resistors, `cap_cmomi`
  metal-finger and `cap_cmim` caps, and an EM-extracted lumped inductor subcircuit. The PDK has no ngspice
  inductor model, so coils go through openEMS plus a fitted model, verified against the EM S-parameters.
- **Size PDK resistors by ngspice `op` measurement**, not from sheet resistance or a scaled LUT lookup.
- Multi-stage experiments give every stage the **same subcircuit port interface** so one set of testbench
  templates serves all of them, and keep stage token namespaces prefixed to avoid collisions.
- Generated parameters: `spice/params.inc` and `spice/ind_shunt.inc` (both committed).
- Results: `out/summary.csv`, per-pass `metrics.csv`, plots, `op.txt`; commit summaries and PNGs, not raw
  `.raw` logs.

## Run an experiment

```bash
source ~/.local/share/ihp-eda/env.sh
./circuits/ctle56n/run.sh
```

Or step-by-step from the experiment directory (see nested `AGENTS.md`).
