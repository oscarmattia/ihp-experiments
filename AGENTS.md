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
| **Implementers, `layout/`** | **Claude Sonnet 5 thinking** sub-agents (`Task` tool with `model: "claude-sonnet-5-thinking-high"`) | Physical layout: placement, routing, and driving DRC/LVS/PEX to clean. |
| **Implementers, everywhere else** | **Composer 2.5** sub-agents (`Task` tool with `model: "composer-2.5"` or `"composer-2.5-fast"`) | Write/edit code, scripts, netlists, tests, and docs from the coordinator’s plan. |

**Why layout is different.** Layout work is not mostly code generation; it is
diagnosis. A stage is finished by reading an extracted netlist's merged nets back
to geometry and finding which run crosses which on what layer, over and over.
Composer 2.5 produced structurally reasonable floorplans but did not reason
through those failures: on the VGA it left the coordinator and the user to find
five separate shorts it had introduced, which cost more than writing the routing
would have. Use the stronger reasoning model where the work is debugging, and keep
Composer 2.5 where the work is writing code to a specification.

**Rules**

- Prefer **parallel** sub-agents when workstreams are independent.
- Do **not** have the coordinator silently implement large code changes when a
  sub-agent can do it.
- **Split a layout task in two** and commit in between: "put it on the floorplan"
  gated on parity + DRC, then "make LVS match". See `MEMORY.md` — asking for both
  at once produces blocks that are structurally right and electrically shorted.
- Give a layout sub-agent the **failure signature**, not the failing rule: the
  extracted netlist line, the `MEMORY.md` entry describing the same failure, and
  the layer budget it has to respect.

## AGENTS.md maintenance (required before every PR)

- Before opening or updating a PR, review and update **all affected** `AGENTS.md` files (this file + any subdirectory whose conventions, layout, or commands changed).
- If agent workflow or directory contracts changed, the PR description or final commit must mention the `AGENTS.md` updates.
- Stale `AGENTS.md` is a **blocker** for “ready for review”.

## Repo map

| Path | Purpose | Nested guide |
| --- | --- | --- |
| `char/` | Device characterization LUTs (`mos/`, `bjt/`, `passive/`, `common/`) | `char/AGENTS.md` |
| `circuits/` | Circuit experiments (56G RX front end: termination, CTLE, VGA, pad driver) | `circuits/AGENTS.md` |
| `layout/` | Physical layout: PCell devices, the four RX stage cells, DRC/LVS/PEX gates | `layout/AGENTS.md` |
| `scripts/` | IHP EDA install / verify | `scripts/AGENTS.md` |
| `docs/` | Environment, setup, and PDK reference docs | `docs/AGENTS.md` |
| `.github/` | GitHub Actions workflows (lint gate today) | `.github/AGENTS.md` |
| `pyproject.toml` | Ruff lint config (and future pytest); not a published package | — |
| `MEMORY.md` | Agent learnings (PDK/sim pitfalls; update when new issues found) | — |
| `layout/debug_pex/` | Extraction investigations: findings plus the probes that produced them | — |
| `pdk/` | Gitignored **symlink** to `$PDK_ROOT`, so PDK files can be read at `pdk/ihp-sg13g2/...` | — |
| `.cursor/plans/` | Approved multi-stage plans not yet implemented (read before starting related work) | — |

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
2. `uv tool run ruff check` is clean (or the GitHub **lint** job passes).
3. Implementation landed via sub-agents when the change was non-trivial code —
   Claude Sonnet 5 thinking for `layout/`, Composer 2.5 elsewhere.
4. Commit + push on `cursor/<name>-b7e8`.
5. Create/update PR with ManagePullRequest; mention AGENTS.md changes if contracts moved.
