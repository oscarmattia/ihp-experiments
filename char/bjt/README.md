# IHP SG13G2 BJT / HBT characterization

DC Gummel-style LUTs plus HBT small-signal tables for the open-PDK bipolar devices:

| Key | Model | Geometry | Notes |
| --- | --- | --- | --- |
| `npn13G2` | VBIC HBT | `Nx` (1–10) | High-speed NPN |
| `npn13G2l` | VBIC HBT | `Nx`, `El` | Longer emitter |
| `npn13G2v` | VBIC HBT | `Nx`, `El` | Vertical / variant |
| `pnpMPA` | GP PNP (level 1) | `a` [m²], `p` [m] | Lateral PNP, 3-terminal |

Deferred: `*_5t` thermal-pin variants, **fₘₐₓ** AC extraction.

## Method

- Nested DC sweep of |VBE| × |VCE| per geometry (emitter grounded).
- Corner: `cornerHBT.lib` / `hbt_typ`.
- Tables store **Ic, Ib**, derived **β = Ic/Ib**, numerical **gm = ∂Ic/∂VBE**, **go = ∂Ic/∂VCE**, **VA ≈ Ic/go**, **gm/Ic**.
- **HBT AC pass** (default): per-bias `op` + 10 GHz AC on grounded-emitter CE testbench; **Cbe/Cbc** from `@q`, **fT** = 10 GHz × |h21|, **CIN** from Y11. Not extracted via nested-dc `wrdata`.
- PNP/lateral devices skip AC; FT/CIN/CBE/CBC are NaN.
- Primary archive: compressed **`.npz`** via `char.common.lut` (not pygmid).

## Run

```bash
source ~/.local/share/ihp-eda/env.sh
./char/bjt/run_all.sh
# quick / subset:
./char/bjt/run_all.sh --quick
./char/bjt/run_all.sh --device npn13G2 --device pnpMPA
./char/bjt/run_all.sh --skip-ac
```

## Outputs (`char/bjt/out/`)

- `sg13_{device}.npz` — LUT arrays + embedded metadata
- `sg13_{device}.meta.json` — human-readable copy of metadata
- `summary.csv` — peak β, peak fT, gm/Ic, VA per geometry
- `{device}_gummel.png`, `{device}_ft.png`, `beta_comparison.png`
