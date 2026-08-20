# Agent notes — `.github/`

**Coordinator:** the run's assigned coordinator model (Grok or Claude Opus 5). Non-trivial workflow or config edits should go through Composer 2.5 sub-agents.

**Before opening a PR that changes this directory, update this file** if workflow scope, triggers, or CI contracts change.

## CI today

- **`workflows/lint.yml`** is the **only** GitHub Actions workflow in this repo.
- It runs **`ruff check`** (no `format --check`) using rules and paths from root [`pyproject.toml`](../pyproject.toml).
- Runs on hosted **`ubuntu-latest`** outside any future GHCR image — lint does not need ngspice, the PDK, or `$IHP_EDA_ROOT`.
- It does **not** run ngspice, DRC, LVS, PEX, or layout gates.

## Local lint

```bash
uv tool run ruff check
```

Ruff is a **repo-dev** tool. Do **not** install it into `$IHP_EDA_ROOT/venv`. The PDK venv may ship flake8; that is unrelated — do not add flake8, pylint, or a second lint tool here.

## Future CI

Sim/layout regression CI is still planned in [`docs/CI-CD-PLAN.md`](../docs/CI-CD-PLAN.md). Do not add those workflows in this directory until that plan's later phases land. Revise the single plan doc rather than forking a second one.
