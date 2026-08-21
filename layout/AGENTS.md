# Agent notes — `layout/`

Physical layout generation and signoff for IHP SG13G2.

**Coordinator:** the run's assigned coordinator model. Non-trivial code here goes
through **Claude Sonnet 5 thinking** sub-agents
(`model: "claude-sonnet-5-thinking-high"`), not Composer 2.5: layout is finished by
diagnosis rather than code generation, and the root
[`../AGENTS.md`](../AGENTS.md) records why. Split the work in two — floorplan, then
LVS — and commit in between.

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
| `blocks/` | matched device groups and the four RX stage layouts ([`AGENTS.md`](blocks/AGENTS.md)) |
| `spike_routing.py` | the PCell-plus-gdsfactory routing proof, also run by verify |
| `debug_pex/` | extraction investigations: [FINDINGS.md](debug_pex/FINDINGS.md) plus runnable probes |
| `run_all.sh` | regenerate everything and diff against committed goldens |

### `common/` modules

| Module | Role |
| --- | --- |
| `paths.py` | PDK paths; `pinned_version()` reads `versions.txt` |
| `rules.py` | DRC rule values read from the PDK's own JSON — **never transcribe a limit** |
| `em.py` | electromigration limits parsed from `sg13g2_tech.lef`, the only place they exist |
| `parity.py` | schematic netlist against layout CDL, device for device |
| `simview.py` | black-boxed device kinds, promoted pins, reduced CDL, post-layout wrapper |
| `postlayout.py` | ngspice comparison, model parameter filtering, extracted-netlist assembly |
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
| `sizing.py` | reads `circuits/ctle56n/spice/*_params.inc` (default `params.inc`) |

## Conventions

- **Sizes come from the sizing scripts.** `catalog.py` reads the committed
  `params.inc`, `term_params.inc`, `vga_params.inc` and `driver_params.inc`; never
  hardcode a device dimension that the circuit already decided.
- **Numeric limits come from `rules.py`.** A hand-written table had TopMetal1 at
  1.50 um against a 1.64 um minimum and the deck only caught it once a route
  used that metal.
- **Current limits come from `em.py`,** which reads `sg13g2_tech.lef` — the only
  machine-readable source of them in this PDK. There is no signoff deck for
  electromigration, so anything drawn here checks itself. `em.py` raises for a layer
  the PDK does not rate rather than returning a number: `Cont` has no stated limit,
  so MOS source/drain contact EM is **not checkable** from open PDK data.
- **The layout netlist must match the schematic, not just the layout.** LVS never
  compares the CDL against the schematic, which is how the CTLE lost its bias diode
  for several revisions. `parity.py` is the gate, and it runs before DRC. Only
  `ind_shunt` → `inductor` is allowed to differ, and even that has its geometry
  compared against the EM model's header.
- **A cell that gets laid out contains devices only.** Ideal sources belong to
  whatever instantiates it — the testbench for a standalone stage, the chain when
  cascaded — and `vss` is a pin rather than the global node `0`, so the port list
  matches. All four stage netlists (`term_pdk.cir`, `ctle_pdk.cir`, `vga_pdk.cir`,
  `driver_pdk.cir`) are converted; pad capacitance and bias sources live in the
  testbench (`circuits/ctle56n/python/ctlelib/ngs.py` `{DUT_BIAS}` tokens).
- **Two device kinds are black-boxed for simulation**, per kind, in
  `simview.BLACK_BOX_KINDS`: `inductor` (the PDK has no ngspice model) and `cmomi`
  (the extractor's finger geometry is not the calibrated compact model). Their nets
  are promoted to pins and a wrapper reconnects the compact models, so a
  post-layout DUT has the schematic's own pins and the existing testbenches
  need no changes. VGA and the pad driver have no `cmomi`; they still black-box
  the coils. `run_postlayout.py --stage {ctle,vga,driver}` writes each wrapper
  under `layout/blocks/out/postlayout{,_vga,_driver}/`.
- **A simulation view is not a tape-out view and must not be gated as one.**
  `build_*_stage(black_box=...)` is deliberately not exposed as a CLI flag on
  the stage scripts, because those entry points write the tape-out artifacts and
  judge them against the full schematic and full CDL. Go through
  `layout/blocks/run_postlayout.py --stage …`, which writes elsewhere and gates
  the reduced view against the reduced CDL.
- **Magic extracts flat.** See `pex._magic_script`: hierarchical extraction produced
  negative capacitance. `PexResult.physical` is the gate for that.
- **Keep extraction investigations, do not throw them away.** When an extraction
  result looks wrong, bisect it with a throwaway cell rather than reasoning about
  the layout, then leave the probe behind in `debug_pex/` with what it measured.
  Both probes there run against a scratch directory and write nothing into the
  repo, and the settings probe takes its geometry from git so a concurrent
  regeneration cannot change the answer mid-experiment.
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
- **`run_all.sh` leaves the tree clean.** A full regeneration reproduces every
  committed artifact byte for byte, so `git status` after it *is* the diff — any
  output means something really changed. Keeping that true needs three things, all
  of which had to be fixed once: GDS is written with `gds2_write_timestamps` off
  (every structure carries a date, which rewrote 176 bytes across the stage's 44
  cells), the stored LVS verdict has the deck's timestamp and memory figure
  stripped, and PEX values are rounded with sort ties broken on name, since Magic's
  sums depend on the order it emits elements.

## Run it

```bash
source ~/.local/share/ihp-eda/env.sh
python layout/devices/gen_devices.py     # GDS, CDL, ports, manifest, PNGs
python layout/devices/run_drc.py
python layout/devices/run_lvs.py
python layout/devices/run_pex.py
python layout/blocks/gen_blocks.py
python layout/blocks/term_stage.py       # term_dut
python layout/blocks/ctle_stage.py       # ctle_dut
python layout/blocks/vga_stage.py        # vga_dut (all five gates pass)
python layout/blocks/driver_stage.py     # driver_dut (all five gates pass)
./layout/run_all.sh                      # devices + blocks + ctle/vga/driver stages + postlayout
```

Each `*_stage.py` is argument parsing plus one call to `stage_gates.run_stage_gates`,
which runs parity, EM, render, DRC, LVS and PEX and writes the JSON artifacts.
Shared drawing lives in `blocks/draw.py` (`snap`, `place`, `via_between`, `trunk_net`,
`vertical_net`, …). `run_all.sh` gates the CTLE, VGA and pad-driver stages (not
`term_dut`) and writes a post-layout DUT netlist for each. Run `term_stage.py`
on its own.
