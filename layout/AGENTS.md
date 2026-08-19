# Agent notes — `layout/`

Physical layout generation and signoff for IHP SG13G2.

**Coordinator:** the run's assigned coordinator model. Non-trivial code should go
through Composer 2.5 sub-agents.

**Before opening a PR that changes this directory, update this file** if the
device registry, port derivation, gate structure, or run commands change. Read
[`../MEMORY.md`](../MEMORY.md) and [`../docs/PDK.md`](../docs/PDK.md) first —
they hold verified device facts, and [`../docs/LAYOUT.md`](../docs/LAYOUT.md)
holds the flow and its traps.

## The one rule that shapes everything else

**Devices are foundry PCells. gdsfactory is for composition and routing only.**

The PCells are the authoritative geometry and are what the LVS deck's device
extraction is written against. Do not re-implement a device.

## Environment

```bash
source ~/.local/share/ihp-eda/env.sh     # sources layout.env.sh
./scripts/verify-ihp-layout.sh           # the gate; run it before trusting anything
```

Requires the layout tier: `./scripts/install-ihp-layout.sh`. KLayout must match
the `versions.txt` pin or the PDK's `run_drc.py` refuses to run.

## Layout

| Path | Contents |
| --- | --- |
| `common/` | PDK access, device registry, port derivation, DRC/LVS/PEX runners |
| `devices/` | single-device catalog and its gates ([`AGENTS.md`](devices/AGENTS.md)) |
| `blocks/` | matched device groups and the CTLE stage ([`AGENTS.md`](blocks/AGENTS.md)) |
| `spike_routing.py` | the PCell-plus-gdsfactory routing proof, also run by verify |
| `run_all.sh` | regenerate everything and diff against committed goldens |

### `common/` modules

| Module | Role |
| --- | --- |
| `paths.py` | PDK paths; `pinned_version()` reads `versions.txt` |
| `rules.py` | DRC rule values read from the PDK's own JSON — **never transcribe a limit** |
| `layers.py` | layer map parsed from `layers_def.drc`; `validate_routing_layers()` guards the one hand-written table |
| `pdk.py` | headless PCell bootstrap (reproduces the KLayout autorun macro) |
| `spec.py` | `DeviceSpec`, the single source of truth for a device instance |
| `devices.py` | device registry: PCell, CDL form, terminals, expected extraction |
| `wrap.py` | ports derived from the PCells' own pin shapes and labels |
| `gds.py` | GDS export, net labels, `write_for_magic()` |
| `guard.py` | guard rings from tap PCells |
| `xsection.py` | gdsfactory PDK: cross-sections, routing primitives, LayerMap |
| `route.py` | PCell to gdsfactory bridge and electrical routing |
| `drc.py` / `lvs.py` / `pex.py` | signoff runners, all returning JSON |
| `postlayout.py` | ngspice comparison of schematic against extracted |
| `sizing.py` | reads `circuits/ctle56n/spice/params.inc` |

## Conventions

- **Sizes come from the sizing scripts.** `catalog.py` reads `params.inc`; never
  hardcode a device dimension that the circuit already decided.
- **Numeric limits come from `rules.py`.** A hand-written table had TopMetal1 at
  1.50 um against a 1.64 um minimum and the deck only caught it once a route
  used that metal.
- **Every gate returns JSON.** `drc.py`, `lvs.py` and `pex.py` reduce tool output
  to a verdict plus counts, so an agent never scrapes a log.
- **Judge LVS on the report, not the exit code.** `run_lvs.py` exits 0 even when
  netlists do not match.
- **Do not restrict DRC tables by hand.** Go through `run_drc.py`; passing
  `-rd tables=...` to the deck breaks its connectivity section.
- **Context rules** (`drc.CONTEXT_RULES`) are reported separately at device level
  and enforced at block level. Pass `allow_context=False` once guard rings exist.
- Tool scratch directories (`drc_run/`, `lvs_run/`, `pex_run/`) are gitignored;
  GDS, CDL, PNG and JSON summaries are committed.

## Run it

```bash
source ~/.local/share/ihp-eda/env.sh
python layout/devices/gen_devices.py     # GDS, CDL, ports, manifest, PNGs
python layout/devices/run_drc.py
python layout/devices/run_lvs.py
python layout/devices/run_pex.py
python layout/blocks/gen_blocks.py
python layout/blocks/ctle_stage.py
./layout/run_all.sh                      # all of the above against goldens
```
