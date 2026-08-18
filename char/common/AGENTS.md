# char/common/ — Agent guide

## Agent workflow

- **Coordinator:** Grok.
- **Implementers:** Composer 2.5 sub-agents via the Task tool.
- **Update this AGENTS.md before any PR** that touches `char/common/`.

## Purpose

Shared LUT I/O for all characterization suites. Every suite should read and write through `lut.py` so archives stay consistent.

## Module: `lut.py`

| Symbol | Role |
| --- | --- |
| `save_lut(path, arrays, meta)` | Write compressed `.npz` with named axis/result arrays |
| `load_lut(path)` | Return `(arrays, meta)` from an archive |
| `parse_wrdata(path, names)` | Parse ngspice `wrdata` (interleaved x,y columns) into named vectors |
| `matrange(start, step, stop)` | Inclusive sweep grid via `linspace` (matches SPICE step semantics) |

## `.npz` format

- Arrays are stored as named keys in a compressed NumPy archive.
- Metadata lives under the reserved key **`META_KEY`** (`"__meta__"`), serialized as JSON inside a 0-d unicode array.
- `load_lut` strips `META_KEY` from the returned `arrays` dict and parses JSON into `meta`.

Example metadata fields (suite-specific): device family, sweep axes, units, script version, corner.

## Compatibility constraints

- **Do not break the MOS pygmid pickle schema** when changing shared helpers. MOS dual-writes `.pkl` (pygmid dict layout) and `.npz`; BJT and future passives depend only on `.npz`.
- If you change `META_KEY`, serialization, or array naming conventions, update **all** consumers (`mos/`, `bjt/`, future `passive/`) and their `AGENTS.md` files in the same PR.

## Consumers

- `char/mos/ihp_sweep.py` — `save_lut`, `parse_wrdata`, `matrange`
- `char/bjt/ihp_bjt_sweep.py` — same
- Future passive scripts should import from here, not reimplement I/O.

## Testing changes

After edits, rerun at least one suite that uses the helper:

```bash
source ~/.local/share/ihp-eda/env.sh
./char/mos/run_all.sh --only lv_core   # exercises pickle + npz
./char/bjt/run_all.sh --quick          # npz only
```

Verify `load_lut` round-trips and that existing `.npz` files still load.
