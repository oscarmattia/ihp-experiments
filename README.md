# ihp-experiments

Analog IC design experiments using the IHP SG13G2 open PDK.

## Quick start (EDA tools)

```bash
./scripts/install-ihp-eda.sh
source ~/.local/share/ihp-eda/env.sh
./scripts/verify-ihp-eda.sh
```

Environment details: [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md)

## MOSFET characterization

```bash
source ~/.local/share/ihp-eda/env.sh
./char/run_all.sh
```

Covers LV/HV × core/RF × NMOS/PMOS. See [char/README.md](char/README.md).

Upstream PDK docs: https://ihp-open-pdk-docs.readthedocs.io/en/latest/install/installation.html
