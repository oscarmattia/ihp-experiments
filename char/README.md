# IHP SG13G2 MOSFET characterization

Basic DC / `gm/ID` characterization for all released CMOS MOSFET classes:

| Family | Devices | Notes |
| --- | --- | --- |
| `lv_core` | `sg13_lv_nmos/pmos`, `rfmode=0` | 1.2 V core |
| `lv_rf` | same models, `rfmode=1` | LV RF / NQS card |
| `hv_core` | `sg13_hv_nmos/pmos`, `rfmode=0` | 3.3 V core |
| `hv_rf` | same models, `rfmode=1` | HV RF / NQS card |

There are **no LVT/SVT/HVT implant flavors** in the open PDK; LV vs HV (and RF vs core) is the available device matrix.

## Method

- Sweeps follow the **pygmid / gm/ID** methodology (lookup tables of ID, VT, GM, GDS, capacitances vs L, VGS, VDS, VSB).
- Upstream `pygmid` ngspice netlists hardcode LV probe names, so `char/ihp_sweep.py` generates IHP-aware netlists and writes **pygmid-compatible** `.pkl` LUTs.
- `pygmid` itself is installed in the EDA venv for later `Lookup` usage on those tables.

## Run

```bash
source ~/.local/share/ihp-eda/env.sh
./char/run_all.sh
# or a subset:
./char/run_all.sh --only lv_core lv_rf
```

## Outputs (`char/out/`)

- `{family}_n.pkl` / `{family}_p.pkl` — LUTs
- `summary.csv` — Vth, Ion, peak gm/ID per L
- `{family}_gm_id.png`, `{family}_idvg.png`, `vth_comparison.png`
