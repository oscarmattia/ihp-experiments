# Agent notes — `docs/`

**Coordinator:** the run's assigned coordinator model (Grok or Claude Opus 5). Non-trivial code or doc edits should go through Composer 2.5 sub-agents.

**Before opening a PR that changes this directory, update this file** if doc scope, cross-links, or sync rules change.

## Documentation

- **`ENVIRONMENT.md`** — single source of truth for **tools and setup**: installed versions, paths, activation, install/verify recipes, Python dependency gaps, egress caveats, optional EM tier, out-of-scope tools. Keep it aligned with `scripts/install-ihp-eda.sh` and `scripts/install-ihp-em.sh` behavior and defaults.
- **`PDK.md`** — SG13G2 **device and model reference** for agents: model/corner section names, OSDI requirements, per-device port orders and parameters, verified device measurements, and PDK traps (no ngspice inductor, empty bondpad, MoM feed resonance, resistor contact R). Tag new entries with provenance (`[model]`, `[sim]`, `[EM]`, `[est]`) and update it whenever a device fact is verified or disproved.
- Flow and simulator pitfalls belong in root [`../MEMORY.md`](../MEMORY.md), not here; cross-link rather than duplicate.
- **`../MEMORY.md`** — agent memory for ngspice/IHP pitfalls (not the human install guide).
- **`LAYOUT.md`** — physical layout flow: foundry PCells for devices, gdsfactory for routing only, DRC/LVS/PEX gates, and the PCell/gdsfactory traps. Keep it aligned with `scripts/install-ihp-layout.sh`, `scripts/verify-ihp-layout.sh` and `layout/`.
- **`APPCSXCAD-STUB.md`** — headless `AppCSXCAD` stub at `$IHP_EDA_ROOT/tools/bin/AppCSXCAD` (openEMS built without GUI).
- **`CI-CD-PLAN.md`** — **draft** CI/CD architecture proposal (pinned environment image on GHCR, tiered regression gates, DVC-tracked testbench results, CML PR reports). Not yet implemented; revise this doc rather than forking a second plan, and keep its "Prerequisite fixes" list in sync with what actually lands in `scripts/` and `char/`.
- When installer **flags, paths, versions, or verification steps** change, update **`ENVIRONMENT.md` and this `AGENTS.md` in the same PR** as the script changes.
- When EM installer or verify scripts change, also update **`scripts/AGENTS.md`** in the same PR.
- When the layout flow changes, update **`LAYOUT.md`**, **`layout/AGENTS.md`** and **`scripts/AGENTS.md`** in the same PR.
