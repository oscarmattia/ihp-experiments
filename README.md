# ihp-experiments

Analog IC design experiments using the IHP SG13G2 open PDK.

## Agent workflow

See [AGENTS.md](AGENTS.md): a coordinator model plans and reviews; Composer 2.5 sub-agents implement. Keep nested `AGENTS.md` files updated before every PR.

Agent reference docs: [MEMORY.md](MEMORY.md) (flow pitfalls), [docs/PDK.md](docs/PDK.md) (SG13G2 devices), [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) (tools and setup).

## Quick start (EDA tools)

```bash
./scripts/install-ihp-eda.sh
source ~/.local/share/ihp-eda/env.sh
./scripts/verify-ihp-eda.sh
```

Environment details: [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md)

## Device characterization LUTs

Precomputed MOS + BJT + passive R/L/C tables for fast design-space browsing (no SPICE on every look-up).

```bash
source ~/.local/share/ihp-eda/env.sh
./char/run_all.sh              # MOS + BJT + passives
./char/mos/run_all.sh          # MOSFET gm/ID only
./char/bjt/run_all.sh          # HBT / PNP only
./char/passive/run_all.sh      # R / C / L (inductors need EM tier; --skip-em to re-summarize)
```

See [char/README.md](char/README.md).

## Circuit experiments

SPICE experiments sized from LUTs (e.g. 56 Gb/s CML CTLE):

```bash
source ~/.local/share/ihp-eda/env.sh
./circuits/ctle56n/run.sh
```

See [circuits/AGENTS.md](circuits/AGENTS.md).

Upstream PDK docs: https://ihp-open-pdk-docs.readthedocs.io/en/latest/install/installation.html
