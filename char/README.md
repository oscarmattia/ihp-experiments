# Device characterization LUTs (IHP SG13G2)

Precomputed tables for fast design-space browsing without re-running SPICE.

| Folder | Contents |
| --- | --- |
| [`mos/`](mos/) | LV/HV × core/RF CMOS (gm/ID-style LUTs) |
| [`bjt/`](bjt/) | SiGe NPN flavors + PNP (`npn13G2` / `l` / `v`, `pnpMPA`) |
| [`passive/`](passive/) | R / L / C LUTs (`rsil`/`rppd`/`rhigh`, MIM/MoM/MOSCAP, openEMS inductors) |
| [`common/`](common/) | Shared `.npz` LUT helpers |

## Format

- Primary archive: compressed **NumPy `.npz`** (`char.common.lut`) with axis arrays + metadata JSON blob
- MOS also keeps **`.pkl`** for pygmid compatibility
- Human summaries: `summary.csv` + PNG plots under each `*/out/`

## Run everything that exists

```bash
source ~/.local/share/ihp-eda/env.sh
./char/run_all.sh
```

MOSFET family details and pygmid notes: [`mos/README.md`](mos/README.md).
Passive R/C/L and EM notes: [`passive/README.md`](passive/README.md).
Agent contracts: [`AGENTS.md`](AGENTS.md).
