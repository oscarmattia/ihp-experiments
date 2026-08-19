# Environment — tools and setup

Single source of truth for **what is installed, where it lives, how to activate it, and how to fix it**
on a Cursor Cloud Agent VM or a local box.

- Device/model knowledge: [PDK.md](PDK.md)
- Simulator and flow pitfalls: [../MEMORY.md](../MEMORY.md)

Target technology: IHP **SG13G2** 130 nm SiGe BiCMOS open PDK.
Upstream install guide: https://ihp-open-pdk-docs.readthedocs.io/en/latest/install/installation.html

## Activate (do this first, every session)

```bash
source ~/.local/share/ihp-eda/env.sh          # analog: PATH, PDK_ROOT, venv
source ~/.local/share/ihp-eda/em.env.sh       # EM only: openEMS paths (if installed)
```

`env.sh` exports `IHP_EDA_ROOT`, `IHP_TOOLS_PREFIX`, `PDK_ROOT`, `PDK`, `KLAYOUT_HOME`,
`KLAYOUT_PATH`, prepends `$IHP_TOOLS_PREFIX/bin` to `PATH`, and activates the venv.

Always use the venv interpreter for PDK work, not system `python3`:

```bash
$IHP_EDA_ROOT/venv/bin/python <script>
```

## Layout

| Item | Path |
| --- | --- |
| Install root (`IHP_EDA_ROOT`) | `~/.local/share/ihp-eda` |
| Tool binaries | `$IHP_EDA_ROOT/tools/bin` (`ngspice`, `xschem`, `openvaf-r`, `rawtovcd`) |
| PDK (`PDK_ROOT`) | `$IHP_EDA_ROOT/IHP-Open-PDK`, `PDK=ihp-sg13g2` |
| Python venv | `$IHP_EDA_ROOT/venv` (uv-managed) |
| Build sources | `$IHP_EDA_ROOT/src` |
| Generated env | `$IHP_EDA_ROOT/env.sh`, `$IHP_EDA_ROOT/em.env.sh` (not committed) |
| ngspice init | `~/.spiceinit` → PDK `libs.tech/ngspice/.spiceinit` (loads OSDI) |

The repo's `pdk/` path is **gitignored and empty**; `$PDK_ROOT` is the only real PDK checkout.

## What is actually installed (verified on this VM)

| Tool | Version | Notes |
| --- | --- | --- |
| ngspice | **45.2**, KLU solver, `--enable-osdi` | built from source; apt ngspice is not OSDI-compatible with IHP models |
| OpenVAF-Reloaded | `openvaf-r` (reports "unknown") | Verilog-A → OSDI; needs LLVM 21 runtime |
| xschem | 3.4.6 | schematic capture |
| KLayout | **0.30.5** (source build) | the version `$PDK_ROOT/versions.txt` pins and `run_drc.py` enforces; `klayout.org`/`klayout.de` are blocked, so `install-ihp-layout.sh` builds from GitHub |
| Magic | **8.3.589** | `install-ihp-layout.sh`; the pin in `versions.txt`. apt's 8.3.105 is below the tech file's 8.3.573 floor |
| netgen | **1.5.323** | `install-ihp-layout.sh`; LVS netgen, **not** apt's FEM mesh generator of the same name |
| gdsfactory | **9.44.0** | composition and electrical routing only; devices are foundry PCells |
| Python | 3.12.3 in `$IHP_EDA_ROOT/venv` | |
| PDK | branch `dev`, `970a7688` | docs require `dev` for current ngspice examples |
| openEMS | **not installed by default** | `./scripts/install-ihp-em.sh`; no `em.env.sh` until then |
| Palace | not installed | needs ≥32 GB RAM + Apptainer |

Host class for these numbers: 4 cores, ~15 GB RAM, 232 GB free.

### Python packages — known gap

The PDK `requirements.txt` installs `flake8 docopt pandas klayout==0.30.5 pyyaml gdstk tqdm termcolor`.
It does **not** include `matplotlib` or `scipy`, and the venv has no `pip` module. Every plotting script
in this repo needs matplotlib, so on a fresh VM run:

```bash
uv pip install --python "$IHP_EDA_ROOT/venv/bin/python" matplotlib scipy
```

`numpy` arrives transitively via `pandas`. `verify-ihp-eda.sh` does not check for matplotlib, so a fresh
VM looks "verified" and still fails the first plotting run with `ModuleNotFoundError: matplotlib`.

## Install / reinstall

```bash
./scripts/install-ihp-eda.sh            # analog tier
source ~/.local/share/ihp-eda/env.sh
./scripts/verify-ihp-eda.sh             # expects MOSFET Id ~ 1.14e-6 A
```

Analog + EM in one pass:

```bash
./scripts/install-ihp-eda.sh --with-em
source ~/.local/share/ihp-eda/env.sh
./scripts/verify-ihp-em.sh
```

EM only, after the analog tier:

```bash
./scripts/install-ihp-em.sh [--skip-palace] [--skip-openems-build] [--force]
source ~/.local/share/ihp-eda/em.env.sh
./scripts/verify-ihp-em.sh
```

Installers are idempotent. The Cloud Agent `install` hook should call the same scripts so a fresh VM
converges without manual steps. The openEMS build clones `openEMS-Project` with submodules and compiles
VTK/CGAL-dependent C++ — budget a long build on a 4-core host and run it in a background tmux session.

## Running simulations

```bash
source ~/.local/share/ihp-eda/env.sh
ngspice -b -o run.log deck.cir
```

- OSDI models load from `~/.spiceinit`. If `HOME` is redirected or the sim runs in a sandbox, copy
  `$PDK_ROOT/$PDK/libs.tech/ngspice/.spiceinit` into the run directory (see
  `char/passive/ihp_cap_sweep.py::_spiceinit_src`). Without it, `rppd`, `rsil`, `cap_cmomi` fail as
  unknown models.
- Model include lines and corner section names: [PDK.md](PDK.md).
- Repo entry points: `./char/run_all.sh`, `./char/{mos,bjt,passive}/run_all.sh`,
  `./circuits/ctle56n/run.sh`.

## GUI vs headless

`xschem` and `klayout` need a display; `DISPLAY` is normally set on Cloud Agents. Headless work uses
`ngspice -b` and `klayout -b`. openEMS is built with `BUILD_APPCSXCAD=NO`; a no-op stub sits at
`$IHP_EDA_ROOT/tools/bin/AppCSXCAD` — see [APPCSXCAD-STUB.md](APPCSXCAD-STUB.md).

## Network egress caveats

Hosts commonly blocked until allowlisted, and what the installer does instead:

| Host | Used for | Workaround in `scripts/` |
| --- | --- | --- |
| `git.code.sf.net`, `downloads.sourceforge.net` | official ngspice | GitHub mirror |
| `www.klayout.de`, `klayout.org` | KLayout `.deb` 0.30.x | Ubuntu apt 0.28.x fallback |
| `astral.sh` | uv installer | GitHub release |
| `datashare.tu-dresden.de` | legacy OpenVAF | OpenVAF-Reloaded |
| `apt.llvm.org` | LLVM 21 runtime for `openvaf-r` | required, usually allowlisted |
| `ihp-open-pdk-docs.readthedocs.io` | docs only | — |

## Layout tier (optional)

Magic, netgen, a PDK-pinned KLayout and gdsfactory are **not** part of the analog
installer. Add them with:

```bash
./scripts/install-ihp-layout.sh          # or: ./scripts/install-ihp-eda.sh --with-layout
source ~/.local/share/ihp-eda/env.sh     # sources layout.env.sh automatically
./scripts/verify-ihp-layout.sh
```

This writes `$IHP_EDA_ROOT/layout.env.sh` (KLayout tech path, PCell Python paths,
`MAGIC_RCFILE`, `NETGEN_SETUP`) and hooks it into `env.sh`. See
[LAYOUT.md](LAYOUT.md) for the flow and its pitfalls.

Egress note: `klayout.org` and `klayout.de` are blocked here, so KLayout is built
from GitHub source instead of installed from the official `.deb`. Allowlisting
either domain switches the installer to the fast path automatically.

## Out of scope (analog-first)

Not installed by default: Xyce + ADMS, Qucs-S, OpenROAD/LibreLane,
`ihp-gdsfactory` (its cells are re-implementations of the foundry PCells, and it
declares a proprietary `gdsfactoryplus` dependency), pygmid beyond
`requirements.txt`.
