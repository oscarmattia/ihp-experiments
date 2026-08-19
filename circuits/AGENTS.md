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
  templates serves all of them, and keep stage token namespaces prefixed to avoid collisions. Testbenches
  carry `{DUT_PORTS}`, `{DUT_BIAS}` and `{DUT_NODESET}` tokens, so a stage that has been converted to a
  device-only netlist and one that has not can share the same templates.
- **A stage that is going to be laid out contains devices only.** Ideal sources belong to whatever
  instantiates it — the testbench standalone, the chain when cascaded — and `vss` is a pin rather than the
  global node `0`, so the port list matches the layout. `ctle_pdk.cir` and `ctle_ideal.cir` are converted
  (`outp outn inp inn vdd vss mgate`); `vga_*`, `driver_pdk` and `term_pdk` still hold their own sources
  and need the same treatment before they can be laid out. `layout/common/parity.py` checks the CTLE's
  devices and pins against the layout on every run.
- **Emit drawable geometry.** The LVS deck compares MOS `W` and `L` with essentially no tolerance and a
  wide MOS is drawn as an array of 5 nm-snapped single-finger units, so `size_ctle.snap_drawable_mos_w`
  puts the sized width on that grid. A width that only exists in the schematic is a mismatch later.
- Generated parameters: `spice/params.inc` and `spice/ind_shunt.inc` (both committed).
- Results: `out/summary.csv`, per-pass `metrics.csv`, plots, `op.txt`; commit summaries and PNGs, not raw
  `.raw` logs.

## Run an experiment

```bash
source ~/.local/share/ihp-eda/env.sh
./circuits/ctle56n/run.sh
```

Or step-by-step from the experiment directory (see nested `AGENTS.md`).
