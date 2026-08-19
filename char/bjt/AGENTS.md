# char/bjt/ — Agent guide

## Agent workflow

- **Coordinator:** Grok.
- **Implementers:** Composer 2.5 sub-agents via the Task tool.
- **Update this AGENTS.md before any PR** that touches `char/bjt/`.

## Purpose

DC Gummel-style LUTs plus HBT small-signal tables for open-PDK bipolar devices. Portable **`.npz` only** (no pygmid pickle).

## Devices

| Key | Model | Geometry | Notes |
| --- | --- | --- | --- |
| `npn13G2` | VBIC HBT | `Nx` (1–10) | High-speed NPN |
| `npn13G2l` | VBIC HBT | `Nx`, `El` | Longer emitter |
| `npn13G2v` | VBIC HBT | `Nx`, `El` | Vertical / variant |
| `pnpMPA` | GP PNP (level 1) | `a` [m²], `p` [m] | Lateral PNP, **3-terminal** |

Deferred: `*_5t` thermal-pin variants, **fₘₐₓ** AC extraction.

## Simulation setup

- **Corner:** `cornerHBT.lib` / `hbt_typ`.
- **DC bias:** nested **control-section DC** sweep of \|VBE\| × \|VCE\| per geometry; **emitter must be grounded**.
- **Probes:** Ic and Ib from `@q` probes (`IC`, `IB` for both NPN and PNP).
- **Derived quantities:** β = Ic/Ib; **gm** = ∂Ic/∂VBE and **go** = ∂Ic/∂VCE computed **numerically** from the Ic surface (nested `wrdata` freezes small-signal values).
- **HBT AC pass** (default; `--skip-ac` to omit): grounded-emitter CE, collector AC-shorted via `Vce` DC source, `Vbe` has `ac 1`. Per (VBE, VCE) point: `op` → read `@q…[cbe]` / `[cbc]`; `ac lin 1 10GHz 10GHz`; **fT** = 10 GHz × \|h21\| with h21 = \|i(Vce)/i(Vbe)\|; **CIN** = −imag(i(Vbe))/(2π·10 GHz). One ngspice process per geometry with nested `dowhile` over VCE×VBE and `wrdata` (do **not** use nested-dc `wrdata` for caps/fT).
- **PNP / lateral:** DC only; **FT**, **CIN**, **CBE**, **CBC** remain **NaN**.

## Key scripts

| File | Role |
| --- | --- |
| `ihp_bjt_sweep.py` | Builds netlists, runs ngspice DC + HBT AC, writes `.npz` via `char.common.lut` |
| `summarize_bjt.py` | `summary.csv` + Gummel / fT / β comparison plots |
| `run_all.sh` | Full pipeline (`--quick`, `--device`, `--skip-ac` filters) |

## Outputs (`char/bjt/out/`)

- `sg13_{device}.npz` — LUT arrays + embedded metadata
- `sg13_{device}.meta.json` — human-readable metadata copy
- `summary.csv`, `{device}_gummel.png`, `{device}_ft.png`, `beta_comparison.png`

## Run

```bash
source ~/.local/share/ihp-eda/env.sh
./char/bjt/run_all.sh
./char/bjt/run_all.sh --quick
./char/bjt/run_all.sh --device npn13G2 --device pnpMPA
./char/bjt/run_all.sh --skip-ac   # DC only (HBT caps/fT NaN)
```

Or via parent: `./char/run_all.sh`.

## Agent notes

- Do not assume pygmid pickle layout here; only `char.common.lut` `.npz` format applies.
- Do not rely on nested-dc `wrdata` for Cbe/Cbc or fT — use the separate OP/AC pass in `ihp_bjt_sweep.py`.
- ngspice `compose` with a single value is a scalar (not indexable); AC grids need ≥2 VBE and ≥2 VCE points.
- See [`../common/AGENTS.md`](../common/AGENTS.md) for shared I/O conventions.
