# IHP Open PDK — environment plan & setup

This repository targets **analog IC experiments** on IHP’s open **SG13G2** 130 nm BiCMOS PDK.

Official guide: https://ihp-open-pdk-docs.readthedocs.io/en/latest/install/installation.html

## Plan (what we install)

| Layer | Choice | Notes |
| --- | --- | --- |
| PDK | `IHP-Open-PDK` **`dev`** branch + submodules | Docs require `dev` for current ngspice examples |
| Verilog-A → OSDI | **OpenVAF-Reloaded** (`openvaf-r`) | Needs **LLVM 21** runtime (`apt.llvm.org`); GitHub releases |
| Circuit sim | **ngspice** ≥43 with `--enable-osdi` | Built from GitHub SourceForge mirror |
| Schematic | **xschem** 3.4.x from source | Then `python3 libs.tech/xschem/install.py` |
| Layout | **KLayout** (~0.30.x .deb, else Ubuntu apt) | Needs `KLAYOUT_PATH` / `KLAYOUT_HOME` |
| Python | **uv** venv + PDK `requirements.txt` | Does not replace system Python for Tcl/Tk tools |

Default install root: `~/.local/share/ihp-eda`  
(`PDK_ROOT=$IHP_EDA_ROOT/IHP-Open-PDK`, tools under `$IHP_EDA_ROOT/tools`).

## Reinstall (environment died)

```bash
./scripts/install-ihp-eda.sh
source ~/.local/share/ihp-eda/env.sh
./scripts/verify-ihp-eda.sh
```

Cloud Agent `install` should run the same executable so a fresh VM converges without manual steps.

## What’s intentionally out of scope (analog-first)

Not installed by default (add later if needed):

- **Xyce** + ADMS (alternate simulator)
- **Qucs-S** (alternate schematic)
- **Magic / netgen** (PDK has Magic tech files)
- **OpenEMS** (EM)
- **OpenROAD / LibreLane** (digital place-and-route)
- **GDSFactory / pygmid** beyond whatever lands via `requirements.txt`

## Known gaps / Cloud Agent caveats

1. **Network egress** — several hosts used by upstream docs are often blocked until allowlisted:
   - `ihp-open-pdk-docs.readthedocs.io` (docs)
   - `git.code.sf.net` / `downloads.sourceforge.net` (official ngspice git/tarball; installer uses a GitHub mirror instead)
   - `www.klayout.de` / `klayout.org` (official .deb; falls back to older Ubuntu `klayout`)
   - `astral.sh` (uv installer; we use GitHub releases)
   - `datashare.tu-dresden.de` (legacy openvaf; we use OpenVAF-Reloaded)
   - `apt.llvm.org` (already commonly allowlisted) — required for **LLVM 21** runtime used by `openvaf-r`
2. **GUI tools** (`xschem`, `klayout`) need a display (`DISPLAY` is usually set on Cloud Agents). Headless work can use `ngspice -b` and KLayout batch (`klayout -b`).
3. **Apt ngspice** is *not* used as the primary simulator — Ubuntu’s package may lack a proper OSDI-enabled build matching IHP’s models. The installer builds from source with `--enable-osdi`.
4. **KLayout apt fallback** (0.28.x on Ubuntu 24.04) is older than IHP’s tested 0.30.3; prefer the official .deb once egress allows it.

## Manual activation

```bash
source scripts/env-ihp.sh
# or
source ~/.local/share/ihp-eda/env.sh
```

Useful paths after install:

- Examples: `$PDK_ROOT/$PDK/libs.tech/xschem/`
- Models: `$PDK_ROOT/$PDK/libs.tech/ngspice/`
- OSDI: `$PDK_ROOT/$PDK/libs.tech/ngspice/osdi/`
