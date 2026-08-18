# char/bjt/ — Agent guide

## Agent workflow

- **Coordinator:** Grok.
- **Implementers:** Composer 2.5 sub-agents via the Task tool.
- **Update this AGENTS.md before any PR** that touches `char/bjt/`.

## Purpose

DC Gummel-style LUTs for open-PDK bipolar devices. Portable **`.npz` only** (no pygmid pickle).

## Devices

| Key | Model | Geometry | Notes |
| --- | --- | --- | --- |
| `npn13G2` | VBIC HBT | `Nx` (1–10) | High-speed NPN |
| `npn13G2l` | VBIC HBT | `Nx`, `El` | Longer emitter |
| `npn13G2v` | VBIC HBT | `Nx`, `El` | Vertical / variant |
| `pnpMPA` | GP PNP (level 1) | `a` [m²], `p` [m] | Lateral PNP, **3-terminal** |

Deferred: `*_5t` thermal-pin variants, fₜ / fₘₐₓ AC extraction.

## Simulation setup

- **Corner:** `cornerHBT.lib` / `hbt_typ`.
- **Bias:** nested **control-section DC** sweep of \|VBE\| × \|VCE\| per geometry; **emitter must be grounded**.
- **Probes:** Ic and Ib from `@q` probes (`IC`, `IB` for both NPN and PNP).
- **Derived quantities:** β = Ic/Ib; **gm** = ∂Ic/∂VBE and **go** = ∂Ic/∂VCE computed **numerically** from the Ic surface (nested `wrdata` freezes small-signal values).
- **Cbe / Cbc:** placeholders are **NaN** in this DC pass (need per-bias OP; wrdata does not track caps reliably).

## Key scripts

| File | Role |
| --- | --- |
| `ihp_bjt_sweep.py` | Builds netlists, runs ngspice, writes `.npz` via `char.common.lut` |
| `summarize_bjt.py` | `summary.csv` + Gummel / β comparison plots |
| `run_all.sh` | Full pipeline (`--quick`, `--device` filters) |

## Outputs (`char/bjt/out/`)

- `sg13_{device}.npz` — LUT arrays + embedded metadata
- `sg13_{device}.meta.json` — human-readable metadata copy
- `summary.csv`, `{device}_gummel.png`, `beta_comparison.png`

## Run

```bash
source ~/.local/share/ihp-eda/env.sh
./char/bjt/run_all.sh
./char/bjt/run_all.sh --quick
./char/bjt/run_all.sh --device npn13G2 --device pnpMPA
```

Or via parent: `./char/run_all.sh`.

## Agent notes

- Do not assume pygmid pickle layout here; only `char.common.lut` `.npz` format applies.
- When adding capacitance extraction, plan a separate OP or AC pass — do not rely on nested-dc wrdata for Cbe/Cbc.
- See [`../common/AGENTS.md`](../common/AGENTS.md) for shared I/O conventions.
