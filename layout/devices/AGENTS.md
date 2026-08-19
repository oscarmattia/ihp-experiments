# Agent notes — `layout/devices/`

Single-device layout catalog and its DRC, LVS and PEX gates.

**Update this file** when the catalog, its gates, or the PCell findings below
change.

## Catalog

`catalog.py` reads `circuits/ctle56n/spice/params.inc`, so the devices are the
ones the front end actually instantiates at the sizes it chose. Add a device by
adding a `DeviceSpec`, not by editing generated output.

| Script | Role |
| --- | --- |
| `catalog.py` | the device list; `plan_fingers()` handles the MOS finger limits |
| `gen_devices.py` | GDS, CDL, ports, `manifest.json`, `specs.json`, PNGs |
| `run_drc.py` | DRC every device, `drc/*.json` + `drc_summary.json` |
| `run_lvs.py` | LVS every device plus an extracted-parameter diff |
| `run_pex.py` | Magic R+C, a klayout.pex resistance cross-check, post-layout sim |

## Status

12 of 13 devices are DRC-clean and LVS-clean with extracted geometry matching
intent. The exception is the wide tail: see below.

## PCell findings, all measured against the deck

These are the traps that cost real time. They are encoded in `devices.py` and
`catalog.py`; do not rediscover them.

- **`library_by_name` needs the technology name.** Without it, it returns `None`.
- **Resistors and capacitors ignore `w`/`l` unless `Calculate` changes.** `rppd`
  defaults to solving for `l` from a target `R`, `cmim` to solving for `w&l` from
  a target `C`.
- **`ng > 1` draws no straps.** A 4-finger device extracts as four transistors in
  series. Multi-finger devices need `blocks/mos_array.py`.
- **`m > 1` is netlist-only.** The layout is identical, so a CDL claiming `m=4`
  cannot match one drawn device.
- **A single finger is capped near 10 um.** Above that the PCell silently reverts
  to minimum width. `plan_fingers()` caps `ng` at 100 and snaps the total onto
  the 5 nm finger grid.
- **Extraction reports MOS width per finger**, and taps as area and perimeter
  rather than `w`/`l`. `DeviceKind.to_extracted` states what to expect;
  capacitors are compared on area because that is what sets `C`.

## Context rules at device level

An isolated device cannot satisfy `LU.b` (a substrate tie within 20 um) or
`LBE.a` (a 100 um chip-area marker). Both are reported separately rather than
dropped. This is evidence-based: the same deck on the PDK's own layouts under
`tech/lvs/testing/testcases/unit` trips `LU.b` 35 times on `sg13_lv_nmos`.

## PEX authority

Recorded in `run_pex.py` as `PEX_NOTES`, and worth repeating: Magic does not
recognise the metal-finger cap as a device and extracts its fingers with an
uncalibrated geometric model, so trust the compact model there. Coil behaviour
belongs to openEMS plus the fitted `ind_shunt`, not to Magic.

Magic's `extresist` pass segfaults on DC-shorted ports, which is the correct
topology for a coil and a tap; extraction falls back to capacitance-only and says
so in the JSON.
