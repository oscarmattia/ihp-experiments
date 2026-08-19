# Agent notes — `docs/`

**Coordinator:** Grok. Non-trivial code or doc edits should go through Composer 2.5 sub-agents.

**Before opening a PR that changes this directory, update this file** if doc scope, cross-links, or sync rules change.

## Documentation

- **`ENVIRONMENT.md`** — human-facing environment guide (install plan, activation, egress caveats, out-of-scope tools, optional EM tier). Keep it aligned with `scripts/install-ihp-eda.sh` and `scripts/install-ihp-em.sh` behavior and defaults.
- **`../MEMORY.md`** — agent memory for ngspice/IHP pitfalls (not the human install guide).
- **`APPCSXCAD-STUB.md`** — headless `AppCSXCAD` stub at `$IHP_EDA_ROOT/tools/bin/AppCSXCAD` (openEMS built without GUI).
- When installer **flags, paths, versions, or verification steps** change, update **`ENVIRONMENT.md` and this `AGENTS.md` in the same PR** as the script changes.
- When EM installer or verify scripts change, also update **`scripts/AGENTS.md`** in the same PR.
