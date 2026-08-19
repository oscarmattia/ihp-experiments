# Agent guide — ihp-experiments

Analog IC / IHP SG13G2 experiments repo. This file is the **root** contract for Cursor Cloud Agents.

## Read these first

Start here instead of re-parsing the repo or the PDK tree on every prompt:

| Doc | Contents |
| --- | --- |
| [MEMORY.md](MEMORY.md) | ngspice/flow pitfalls, corrections to earlier assumptions, recurring design notes |
| [docs/PDK.md](docs/PDK.md) | SG13G2 devices, model/corner names, port orders, verified device numbers, PDK traps |
| [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) | Installed tool versions, paths, activation, install/verify, egress caveats |

Keep all three current: when a sim pitfall, device fact, or tool version changes, update the doc in the
same PR as the code.

## Role split

| Role | Who | Responsibilities |
| --- | --- | --- |
| **Coordinator** | The coordinator model assigned for the run (Cursor Grok or Claude Opus 5) | Plan and scope work; review diffs; merge decisions; run git/PR/env tools; delegate implementation. |
| **Implementers** | **Composer 2.5** sub-agents (`Task` tool with `model: "composer-2.5"` or `composer-2.5-fast`) | Write/edit code, scripts, netlists, and tests from the coordinator’s plan. |

**Rules**

- Prefer **parallel Composer 2.5** sub-agents when workstreams are independent.
- Do **not** have the coordinator silently implement large code changes when Composer 2.5 sub-agents can do it.

## AGENTS.md maintenance (required before every PR)

- Before opening or updating a PR, review and update **all affected** `AGENTS.md` files (this file + any subdirectory whose conventions, layout, or commands changed).
- If agent workflow or directory contracts changed, the PR description or final commit must mention the `AGENTS.md` updates.
- Stale `AGENTS.md` is a **blocker** for “ready for review”.

## Repo map

| Path | Purpose | Nested guide |
| --- | --- | --- |
| `char/` | Device characterization LUTs (`mos/`, `bjt/`, `passive/`, `common/`) | `char/AGENTS.md` |
| `circuits/` | Circuit experiments (CTLE, drivers) | `circuits/AGENTS.md` |
| `scripts/` | IHP EDA install / verify | `scripts/AGENTS.md` |
| `docs/` | Environment, setup, and PDK reference docs | `docs/AGENTS.md` |
| `MEMORY.md` | Agent learnings (PDK/sim pitfalls; update when new issues found) | — |
| `pdk/` | Gitignored and empty; the real PDK lives at `$PDK_ROOT` | — |

Read the nested `AGENTS.md` in a directory before changing files there.

## Environment

Activate the toolchain before sims, sweeps, or verification:

```bash
source ~/.local/share/ihp-eda/env.sh
```

| Item | Value / note |
| --- | --- |
| Python | `$IHP_EDA_ROOT/venv/bin/python` (not system `python3` for PDK work) |
| `PDK_ROOT` | `$IHP_EDA_ROOT/IHP-Open-PDK` (set by `env.sh`) |
| ngspice | Built with **OSDI**; required for OpenVAF-compiled IHP models |
| Install / verify | `./scripts/install-ihp-eda.sh`, `./scripts/verify-ihp-eda.sh` |
| EM (passive L) | Not installed by default: `./scripts/install-ihp-em.sh` or `--with-em`; `./scripts/verify-ihp-em.sh` |
| Plotting deps | `matplotlib`/`scipy` are absent on a fresh VM: `uv pip install --python "$IHP_EDA_ROOT/venv/bin/python" matplotlib scipy` |
| Details | [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md), devices in [docs/PDK.md](docs/PDK.md) |

## Git / Cloud Agent

| Item | Convention |
| --- | --- |
| Base branch | `main` |
| Feature branches | `cursor/<descriptive-name>-b7e8` |
| Workflow | Commit → push → **ManagePullRequest** for PRs (do not use `gh` for write ops) |

### Pre-PR checklist

1. Affected `AGENTS.md` files reviewed/updated (root + nested).
2. Implementation landed via Composer 2.5 sub-agents when the change was non-trivial code.
3. Commit + push on `cursor/<name>-b7e8`.
4. Create/update PR with ManagePullRequest; mention AGENTS.md changes if contracts moved.
