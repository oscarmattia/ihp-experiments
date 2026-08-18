# char/passive/ — Agent guide

## Agent workflow

- **Coordinator:** Grok.
- **Implementers:** Composer 2.5 sub-agents via the Task tool.
- **Update this AGENTS.md before any PR** that touches `char/passive/`.

## Purpose

Placeholder for future R / L / C characterization LUTs on IHP SG13G2. Not implemented yet.

## Planned scope

| Class | Example models | LUT idea |
| --- | --- | --- |
| Resistors | `rsil`, `rhigh`, `rppd`, … | R(W, L, T), mismatch hooks |
| Capacitors | `cmim`, `cmom`, MOSCAP | C(V), Q proxies |
| Inductors | RF spiral / line (as available) | L(f), Q(f) |

## Format (when implemented)

Match [`../common/lut.py`](../common/lut.py):

- Compressed **`.npz`** via `save_lut` / `load_lut`
- JSON metadata under `META_KEY` (`"__meta__"`)
- Optional `summary.csv` + plots under `out/`

Same archive shape as MOS `.npz` and BJT `.npz` so a common browser can load all device classes uniformly.

## Integration status

- **Not wired** into [`../run_all.sh`](../run_all.sh) beyond a placeholder message:
  `Passive characterization not implemented yet (see char/passive/).`
- `./char/passive/run_all.sh` does not exist yet.

When implementation lands:

1. Add sweep + summarize scripts here.
2. Hook `run_all.sh` in this directory.
3. Append a call from `char/run_all.sh`.
4. Update this file and [`../AGENTS.md`](../AGENTS.md).

## Agent notes

- Reuse `parse_wrdata` and `matrange` from `char.common.lut`; do not fork I/O.
- No pygmid pickle expected for passives.
- See [`../common/AGENTS.md`](../common/AGENTS.md) for shared helper contracts.
