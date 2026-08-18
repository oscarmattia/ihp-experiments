# ihp-experiments

Analog IC design experiments using the IHP SG13G2 open PDK.

## Quick start (EDA tools)

```bash
./scripts/install-ihp-eda.sh
source ~/.local/share/ihp-eda/env.sh
./scripts/verify-ihp-eda.sh
```

Environment details: [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md)

## Device characterization LUTs

Precomputed MOS + BJT tables for fast design-space browsing (no SPICE on every look-up). Passives (R/L/C) come later.

```bash
source ~/.local/share/ihp-eda/env.sh
./char/run_all.sh          # MOS + BJT
./char/mos/run_all.sh      # MOSFET gm/ID only
./char/bjt/run_all.sh      # HBT / PNP only
```

See [char/README.md](char/README.md).

Upstream PDK docs: https://ihp-open-pdk-docs.readthedocs.io/en/latest/install/installation.html
