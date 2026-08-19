# Agent notes — `docs/`

**Coordinator:** Grok. Non-trivial code or doc edits should go through Composer 2.5 sub-agents.

**Before opening a PR that changes this directory, update this file** if doc scope, cross-links, or sync rules change.

## Documentation

- **`ENVIRONMENT.md`** — human-facing environment guide (install plan, activation, egress caveats, out-of-scope tools, optional EM tier). Keep it aligned with `scripts/install-ihp-eda.sh` and `scripts/install-ihp-em.sh` behavior and defaults.
- **`APPCSXCAD-STUB.md`** — headless `AppCSXCAD` stub at `$IHP_EDA_ROOT/tools/bin/AppCSXCAD` (openEMS built without GUI).
- **`CI-CD-PLAN.md`** — **draft** CI/CD architecture proposal (pinned environment image on GHCR, tiered regression gates, DVC-tracked testbench results, CML PR reports). Not yet implemented; revise this doc rather than forking a second plan, and keep its "Prerequisite fixes" list in sync with what actually lands in `scripts/` and `char/`.
- When installer **flags, paths, versions, or verification steps** change, update **`ENVIRONMENT.md` and this `AGENTS.md` in the same PR** as the script changes.
- When EM installer or verify scripts change, also update **`scripts/AGENTS.md`** in the same PR.
