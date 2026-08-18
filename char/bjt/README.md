# IHP SG13G2 BJT / HBT characterization

DC Gummel-style LUTs for the open-PDK bipolar devices:

| Key | Model | Geometry | Notes |
| --- | --- | --- | --- |
| `npn13G2` | VBIC HBT | `Nx` (1–10) | High-speed NPN |
| `npn13G2l` | VBIC HBT | `Nx`, `El` | Longer emitter |
| `npn13G2v` | VBIC HBT | `Nx`, `El` | Vertical / variant |
| `pnpMPA` | GP PNP (level 1) | `a` [m²], `p` [m] | Lateral PNP, 3-terminal |

Deferred: `*_5t` thermal-pin variants, fₜ / fₘₐₓ AC extraction.

## Method

- Nested DC sweep of |VBE| × |VCE| per geometry (emitter grounded).
- Corner: `cornerHBT.lib` / `hbt_typ`.
- Tables store **Ic, Ib**, derived **β = Ic/Ib**, numerical **gm = ∂Ic/∂VBE**, **go = ∂Ic/∂VCE**, **VA ≈ Ic/go**, **gm/Ic**.
- Cbe/Cbc placeholders are NaN in this DC pass (need per-bias OP; nested `wrdata` freezes small-signal caps).
- Primary archive: compressed **`.npz`** via `char.common.lut` (not pygmid).

## Run

```bash
source ~/.local/share/ihp-eda/env.sh
./char/bjt/run_all.sh
# quick / subset:
./char/bjt/run_all.sh --quick
./char/bjt/run_all.sh --device npn13G2 --device pnpMPA
```

## Outputs (`char/bjt/out/`)

- `sg13_{device}.npz` — LUT arrays + embedded metadata
- `sg13_{device}.meta.json` — human-readable copy of metadata
- `summary.csv` — peak β, gm/Ic, VA per geometry
- `{device}_gummel.png`, `beta_comparison.png`
