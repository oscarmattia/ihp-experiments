# Agent notes — `scripts/`

**Coordinator:** Grok. Non-trivial code or doc edits should go through Composer 2.5 sub-agents.

**Before opening a PR that changes this directory, update this file** if behavior, flags, paths, or verification expectations change.

## Scripts

| Script | Role |
| --- | --- |
| `install-ihp-eda.sh` | Idempotent installer: OpenVAF-Reloaded, ngspice 45.2 (`--enable-osdi`), xschem, KLayout, IHP-Open-PDK clone, uv venv. Default root: `~/.local/share/ihp-eda/` (`IHP_EDA_ROOT`). Writes `$IHP_EDA_ROOT/env.sh`. Pass `--with-em` to also run `install-ihp-em.sh`. |
| `install-ihp-em.sh` | Optional EM tier: openEMS Python deps + binary build, `$IHP_EDA_ROOT/em.env.sh`, optional Palace/Apptainer, headless `AppCSXCAD` stub. Flags: `--skip-openems-build`, `--skip-palace`, `--force`. Never fails solely on Palace/openEMS binary issues if Python deps succeed. |
| `verify-ihp-em.sh` | EM smoke test: Python imports (`gdspy`, `skrf`, `numpy`), openEMS binary (soft-fail), PDK openEMS workflow dir, optional mesh preview via `run_line_noGDSII.py`. Exit 1 only when Python deps are missing. |
| `env-ihp.sh` | Shell entrypoint; sourced by users and by the installer. Prefers the generated `$IHP_EDA_ROOT/env.sh`, with a minimal fallback before first install. |
| `verify-ihp-eda.sh` | Smoke test: tools on PATH, OSDI models, `~/.spiceinit`, MOSFET op-point via ngspice (expect `Id` ≈ `1.14e-6` A). |

## Conventions

- **Idempotent installs** — safe to re-run; skip flags (`--skip-*`) for partial updates.
- **Egress** — prefer GitHub mirrors over SourceForge / blocked upstream hosts when network policy limits downloads.
- **Do not commit** — repo-root `pdk` symlink (local checkout convenience), generated `$IHP_EDA_ROOT/env.sh`, or secrets/tokens.
