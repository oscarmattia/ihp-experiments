"""Gate-running harness shared by every stage layout."""

from __future__ import annotations

import json
from pathlib import Path

from layout.blocks.generators import Block
from layout.common import em
from layout.common.drc import CONTEXT_RULES, run_drc
from layout.common.lvs import run_lvs
from layout.common.netlist import write_block_cdl
from layout.common.parity import check_parity
from layout.common.pex import run_magic_pex, signal_resistors
from layout.common.render import render_gds


def run_stage_gates(
    block: Block,
    out_dir: Path,
    schematic: Path,
    subckt: str,
    params: dict[str, float] | None = None,
    allowed_rules: tuple[str, ...] = (),
    disable_tap_extraction: bool = True,
    no_render: bool = False,
    no_pex: bool = False,
) -> tuple[int, dict]:
    """Run parity, EM, render, DRC, LVS and PEX; write the JSON artifacts."""
    out_dir.mkdir(parents=True, exist_ok=True)
    entry = block.summary()

    gds = block.write(out_dir)
    entry["gds"] = str(gds)
    print(f"  placed  {entry['bbox_um']['width']:.1f} x {entry['bbox_um']['height']:.1f} um, "
          f"{len(block.instances)} device(s), axis x={block.symmetry['axis_x_um']}")

    cdl = write_block_cdl(subckt, block.port_nets, block.instances, out_dir / f"{subckt}.cdl")
    entry["cdl"] = str(cdl)

    # Parity against the schematic, before any geometry check: if the two
    # netlists disagree there is no point asking whether the layout matches one.
    parity = check_parity(schematic, cdl, subckt=subckt, params=params)
    parity.write(out_dir / "parity.json")
    entry["parity"] = parity.to_dict()
    print(f"  parity  {'ok' if parity.ok else 'FAIL'}  {parity.summary()[:110]}")

    em_report = em.check_segments(block.em_segments)
    (out_dir / "em.json").write_text(json.dumps(em_report, indent=2) + "\n")
    entry["em"] = em_report
    worst = max(
        (s for s in em_report["segments"] if s.get("checkable")),
        key=lambda s: s.get("utilisation") or 0.0,
        default=None,
    )
    detail = (
        f"worst {worst['net']} on {worst['layer']} at "
        f"{(worst['utilisation'] or 0) * 100:.0f}% of limit"
        if worst else "no checkable segments"
    )
    print(f"  EM      {'ok' if em_report['ok'] else 'FAIL ' + str(em_report['failures'])}  {detail}")

    if not no_render:
        png = render_gds(gds, out_dir / f"{subckt}.png", width=1400)
        entry["png"] = str(png) if png else None

    allowed = set(allowed_rules)
    drc = run_drc(gds=gds, run_dir=out_dir / "drc_run", cell_name=subckt, allow_context=True)
    remaining = {r: c for r, c in drc.by_rule.items() if r not in allowed}
    drc_ok = not remaining and not drc.error
    entry["drc"] = drc.to_dict()
    entry["drc"]["ok"] = drc_ok
    entry["drc"]["allowed_chip_level_rules"] = sorted(allowed)
    entry["drc"]["enforced_context_rules"] = sorted(set(CONTEXT_RULES) - allowed)
    entry["drc"]["remaining_violations"] = remaining
    chip = {r: c for r, c in drc.by_rule.items() if r in allowed}
    print(f"  DRC     {'ok' if drc_ok else 'FAIL'}  chip-level={chip} remaining={remaining}")

    lvs = run_lvs(gds=gds, cdl=cdl, run_dir=out_dir / "lvs_run", topcell=subckt,
                  disable_tap_extraction=disable_tap_extraction)
    entry["lvs"] = lvs.to_dict()
    print(f"  LVS     {'ok' if lvs.clean else 'FAIL'}  {lvs.summary[:70]}")

    if not no_pex:
        pex = run_magic_pex(gds=gds, cell=subckt, run_dir=out_dir / "pex_run")
        entry["pex"] = pex.to_dict()
        if pex.ok:
            signal = signal_resistors(pex.resistor_elements)
            entry["pex"]["signal_resistance_ohm"] = round(sum(e["ohm"] for e in signal), 6)
            print(f"  PEX     ok  {pex.capacitors} C totalling "
                  f"{pex.total_capacitance * 1e15:.2f} fF, {pex.resistors} R")
        else:
            print(f"  PEX     FAIL {pex.error[:60]}")

    (out_dir / f"{subckt}_summary.json").write_text(json.dumps(entry, indent=2) + "\n")
    return (0 if (drc_ok and lvs.clean and parity.ok and em_report["ok"]) else 1, entry)
