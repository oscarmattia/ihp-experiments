# Physical layout flow — IHP SG13G2

How layout is generated and signed off in this repo. Device facts belong in
[PDK.md](PDK.md); tool versions and paths in [ENVIRONMENT.md](ENVIRONMENT.md);
pitfalls in [../MEMORY.md](../MEMORY.md).

## Shape of the flow

Devices are **foundry PCells** from the PDK's own `sg13g2_pycell_lib`.
gdsfactory is used for **composition and electrical routing only**.

That split is deliberate. The PCells are the authoritative geometry and are what
the LVS deck's device extraction is written against, so placing them directly
removes any chance of our geometry disagreeing with the foundry's. gdsfactory
supplies what PCells lack — typed ports, hierarchy and bundle routing — without
having an opinion about what a transistor looks like.

```
DeviceSpec ──► PCell placement ──► GDS ──► PDK DRC deck   ──► JSON verdict
    │                                └──►  PDK LVS deck   ──► JSON verdict
    │                                └──►  Magic extract  ──► parasitic netlist
    └──► CDL netlist ─────────────────────► (LVS reference)
```

A single `DeviceSpec` feeds the PCell parameters, the CDL element line and the
port names, so layout and netlist cannot drift apart silently.

## Install and verify

```bash
./scripts/install-ihp-layout.sh          # KLayout, Magic, netgen, gdsfactory
source ~/.local/share/ihp-eda/env.sh     # sources layout.env.sh automatically
./scripts/verify-ihp-layout.sh           # --quick skips the deck runs
```

`verify-ihp-layout.sh` is the gate. It checks the version triple, starts Magic
and netgen headless against the PDK tech files, runs the DRC and LVS decks on
the PDK's own testcases, and routes two PCell devices through gdsfactory and
puts the result through DRC.

### Tool versions come from the PDK

`$PDK_ROOT/versions.txt` is the single source of truth, and the PDK enforces it:
`run_drc.py` refuses to run when the KLayout binary is older than the pin, with
`Minimum required KLayout version is 0.30.5`. The installer reads the file
rather than hardcoding versions.

Two version facts worth knowing:

- **KLayout is built from source here.** `klayout.org` and `klayout.de` are
  blocked by this environment's egress policy, so the official `.deb` cannot be
  fetched and the installer falls back to a GitHub source build. Add
  `klayout.org` and `klayout.de` to the Cloud Agent network allowlist to get the
  fast path. The build passes `-nolibgit2`, dropping the GUI package manager
  that would otherwise need `git2.h`.
- **The klayout pip module is pinned below what gdsfactory asks for.**
  gdsfactory 9.44 depends on kfactory >= 2.5, which declares `klayout >= 0.30.8`,
  while the PDK pins 0.30.5. The PDK version wins: it is the combination IHP
  tested, and it keeps PCell generation and the DRC binary on the same KLayout.
  The routing spike in verification exercises kfactory end to end so this is
  validated rather than assumed.

## Signoff decks

| Check | Entry point | Wrapper |
| --- | --- | --- |
| DRC | `tech/drc/run_drc.py` | `layout/common/drc.py` |
| LVS | `tech/lvs/run_lvs.py` | `layout/common/lvs.py` |
| PEX | Magic `extract` / `ext2spice` | `layout/common/pex.py` |
| PEX cross-check | `klayout.pex` (R only) | `layout/common/pex.py` |

Both wrappers reduce the tools' output to JSON — a verdict, counts per rule and
a few examples — so an agent never has to scrape a log.

### Do not restrict DRC tables by hand

Passing `-rd tables=...` straight to the deck breaks it:

```
ERROR: In .../ihp-sg13g2.drc: '-': Argument needs to be a DRC layer
```

The connectivity section runs unconditionally and references layers that only
the full table set defines. The PDK's own CI pairs `--table=` with
`--no_feol --no_beol ...` for this reason. Go through `run_drc.py`, which manages
the combinations.

This error is worth naming precisely because it looks like a version problem and
is not: it appears on any KLayout version when the table list is restricted. The
upgrade is still required, but for a different reason — `run_drc.py` refuses to
run below the `versions.txt` pin.

### Judge LVS on the report, not the exit code

`run_lvs.py` exits 0 even when the compare fails. Reading the return code
reported four mismatched blocks and one mismatched device as clean. `lvs.py`
looks for `Netlists match` in the output instead.

### Magic needs a newer version than versions.txt pins

`ihp-sg13g2.tech` carries `requires magic-8.3.617` while `versions.txt` says
8.3.589. Below the tech file's floor its `version` and `cifinput` sections fail to
load and Magic cannot read a GDS at all. The installer takes the higher of the
two.

### Numeric limits come from the PDK

`layout/common/rules.py` reads `rule_decks/sg13g2_tech_default.json`, the same
table the deck loads. Nothing in the layout code should carry a transcribed
limit: a hand-written width table had TopMetal1 at 1.50 um against the 1.64 um
minimum, and the deck only caught it once a route used that metal.

### Context rules

Some rules constrain a cell's *surroundings* and cannot be satisfied by an
isolated device:

| Rule | Why an isolated cell fails it |
| --- | --- |
| `LU.b` / `LU.a` | latch-up wants a substrate tie within 20 um; a lone device has none |
| `LBE.a` | local-back-end minimum width is 100 um, a chip-area marker |
| density | a bare device cannot meet metal density |
| antenna | needs the full connected net |

`layout/common/drc.py` lists these in `CONTEXT_RULES` and reports them
separately rather than dropping them, so `clean` means "no real violations" and
`strictly_clean` is the unfiltered verdict. The classification is evidence
based: running the same deck on the PDK's own device layouts under
`tech/lvs/testing/testcases/unit` trips `LU.b` 35 times on `sg13_lv_nmos`.
Pass `allow_context=False` at block level, where guard rings exist.

## Devices

`layout/common/devices.py` is the registry. Each entry ties a logical device to
its PCell, its CDL element form (taken from the PDK LVS testcases), its terminal
names, and what LVS extraction actually reports back.

Parameters are given in SI base units and translated there, so callers never
hand-format PCell unit strings.

### PCell traps encoded in the registry

- **`library_by_name` needs the technology name.**
  `pya.Library.library_by_name("SG13_dev")` returns `None`; the second argument
  is required.
- **Resistors and capacitors ignore `w`/`l` unless `Calculate` is changed.**
  `rppd` defaults to solving for `l` from a target `R`, `cmim` to solving for
  `w&l` from a target `C`. Passing a geometry without setting `Calculate`
  silently yields the default device.
- **`nmos` clamps `ng` at 100 and snaps finger width to 5 nm.** Asking for 122
  fingers of a 242.988 um device quietly draws 100 fingers of 2.425 um, i.e.
  242.5 um. `catalog.plan_fingers()` caps the count and puts the total on the
  finger grid.
- **The LVS deck reports MOS width per finger**, and reports taps as area and
  perimeter rather than `w`/`l`. `DeviceKind.to_extracted` states what to expect
  per device; capacitors are checked on area, which is what sets `C`.

## Ports

Ports are derived from what the PCells already draw, not hardcoded:

1. pin shapes on the `*_pin` layers mark the terminals,
2. annotation text on `text_drw` (63/0) names them where the PCell writes one —
   `npn13G2` labels its pins `C`, `B` and `E`,
3. otherwise a per-kind rule in `wrap.TERMINAL_RULES` names them by geometric
   order.

Step 3 is safe for the devices that need it because MOS source/drain and the two
terminals of a resistor or capacitor are permutable in the LVS device classes.

Ports advertise the **drawing** layer of their metal, not the pin layer, because
gdsfactory matches a route's cross-section layer against the port layer.

### Net labels

Net names go on the `*_text` layer of the metal a pin sits on (Metal1 is 8/25,
Metal2 10/25). They are not strictly required — the PDK's own testcase layouts
carry none, and the deck matches on connectivity with
`--ignore_top_ports_mismatch` — but they make the compare able to check the port
mapping, and they matter once several devices share a cell.

There is no `gatpoly_text` layer, so a MOS gate net can only be named where the
gate reaches metal. `gen_devices.py` records such terminals under
`unlabelled_terminals` in the manifest.

## Routing

```python
from layout.common.route import device_component, route_electrical
c = device_component(spec)
route_electrical(top, [a.ports["MINUS"]], [b.ports["PLUS"]], metal="Metal1")
```

**Every bundle stays on one metal.** A metal change needs a via stack built to
SG13G2 rules, which gdsfactory knows nothing about, so `route_electrical`
refuses a bundle whose ports are on another layer and points at the `via_stack`
PCell instead of letting gdsfactory invent a taper. `auto_taper` is off for the
same reason.

`layout/common/xsection.py` registers a minimal gdsfactory PDK: cross-section
factories per metal (`metal_routing` aliases TopMetal2, the thick RF metal), the
`wire_corner`/`straight`/`taper` primitives that electrical routing resolves by
name, and a `LayerMap` of the routing stack.

Two gdsfactory details that cost time:

- **`activate_pdk()` must run before any other gdsfactory call.** gdsfactory
  resolves its default PDK from `$PDK`, which `env.sh` sets to `ihp-sg13g2` for
  the SPICE flow. That is not an importable Python module, so `get_active_pdk()`
  raises until a PDK is activated explicitly.
- **Cross-sections must be registered as factories**, not instances, or
  kfactory raises `'CrossSection' object is not callable`.

The routing `LayerMap` is the one hand-written layer table in the flow, because
gdsfactory needs its members at class-definition time. Everything else parses
`rule_decks/layers_def.drc`. `layers.validate_routing_layers()` compares the two
and verification fails on drift.

## Running it

```bash
source ~/.local/share/ihp-eda/env.sh

python layout/devices/gen_devices.py     # GDS, CDL, ports, manifest, PNGs
python layout/devices/run_drc.py         # DRC every device
python layout/devices/run_lvs.py         # LVS every device
./layout/run_all.sh                      # everything, against committed goldens
```

Outputs land under `layout/devices/out/`: `gds/`, `cdl/`, `png/`,
`manifest.json`, `specs.json`, and per-device `drc/` and `lvs/` JSON plus
`drc_summary.json` / `lvs_summary.json`. Tool scratch directories
(`drc_run/`, `lvs_run/`, `pex_run/`) are gitignored.
