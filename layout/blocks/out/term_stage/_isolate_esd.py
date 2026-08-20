#!/usr/bin/env python3
"""Which ESD supply strap first merges vdd with vss?"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

ROOT = Path(__file__).resolve().parents[2] / "term_stage.py"
OUT = Path(__file__).resolve().parent / "_isolate"
OUT.mkdir(exist_ok=True)

INP_VDD = textwrap.dedent("""
            if pad_net == "inp":
                lx, ly = via_up(layout, cell, evdd_t["VDD"], "Metal2")
                vdd_col = snap(evdd_t["VDD"].center[0] - ROW_GAP)
                rect(layout, cell, "Metal2", min(lx, vdd_col) - m2_w / 2, ly - m2_w / 2,
                      max(lx, vdd_col) + m2_w / 2, ly + m2_w / 2)
                _edge_route(layout, cell, "Metal2", vdd_col, ly, vdd_strap_y, m2_w)
                rect(layout, cell, "Metal2", min(vdd_col, vdd_bus_x) - m2_w / 2, vdd_strap_y - m2_w / 2,
                      max(vdd_col, vdd_bus_x) + m2_w / 2, vdd_strap_y + m2_w / 2)
""")

INP_VSS = textwrap.dedent("""
            if pad_net == "inp":
                vss_col = snap(evss_t["VSS"].center[0] - ROW_GAP)
                sx, sy = via_up(layout, cell, evss_t["VSS"], "Metal2")
                via_between(layout, cell, sx, sy, "Metal2", "Metal3")
                if abs(vss_col - sx) > 1e-6:
                    rect(layout, cell, "Metal3", min(sx, vss_col) - m3_w / 2, sy - m3_w / 2,
                          max(sx, vss_col) + m3_w / 2, sy + m3_w / 2)
                _edge_route(layout, cell, "Metal3", vss_col, sy, esd_vss_feed_y, m3_w)
                rect(layout, cell, "Metal3", min(vss_col, vss_bus_x) - m3_w / 2, esd_vss_feed_y - m3_w / 2,
                      max(vss_col, vss_bus_x) + m3_w / 2, esd_vss_feed_y + m3_w / 2)
""")

INN_VDD = textwrap.dedent("""
            if pad_net == "inn":
                lx, ly = via_up(layout, cell, evdd_t["VDD"], "Metal2")
                vdd_col = snap(evdd_t["VDD"].center[0] + ROW_GAP)
                rect(layout, cell, "Metal2", min(lx, vdd_col) - m2_w / 2, ly - m2_w / 2,
                      max(lx, vdd_col) + m2_w / 2, ly + m2_w / 2)
                _edge_route(layout, cell, "Metal2", vdd_col, ly, vdd_strap_y, m2_w)
                rect(layout, cell, "Metal2", min(vdd_col, axis) - m2_w / 2, vdd_strap_y - m2_w / 2,
                      max(vdd_col, axis) + m2_w / 2, vdd_strap_y + m2_w / 2)
""")

INN_VSS = textwrap.dedent("""
            if pad_net == "inn":
                vss_col = snap(evss_t["VSS"].center[0] + ROW_GAP)
                sx, sy = via_up(layout, cell, evss_t["VSS"], "Metal2")
                via_between(layout, cell, sx, sy, "Metal2", "Metal3")
                if abs(vss_col - sx) > 1e-6:
                    rect(layout, cell, "Metal3", min(sx, vss_col) - m3_w / 2, sy - m3_w / 2,
                          max(sx, vss_col) + m3_w / 2, sy + m3_w / 2)
                _edge_route(layout, cell, "Metal3", vss_col, sy, vss_ring_y, m3_w)
                rect(layout, cell, "Metal3", min(vss_col, axis) - m3_w / 2, vss_ring_y - m3_w / 2,
                      max(vss_col, axis) + m3_w / 2, vss_ring_y + m3_w / 2)
""")


def patch(body: str) -> str:
    src = ROOT.read_text()
    start = src.index("        for pad_net, (evdd_t, evss_t) in esd_terms.items():")
    end = src.index("            for terminal in (evdd_t[\"VDD\"], evss_t[\"VSS\"]):", start)
    loop = "        for pad_net, (evdd_t, evss_t) in esd_terms.items():\n" + body
    return src[:start] + loop + src[end:]


def run(tag: str, body: str) -> str:
    tmp = OUT / f"{tag}.py"
    tmp.write_text(patch(body))
    env = "source ~/.local/share/ihp-eda/env.sh && export QT_QPA_PLATFORM=offscreen && "
    subprocess.run(
        ["bash", "-lc", env + f"cd /workspace && $IHP_EDA_ROOT/venv/bin/python {tmp} 2>/dev/null"],
        check=False,
    )
    ext = Path("/workspace/layout/blocks/out/term_stage/lvs_run/term_dut_extracted.cir")
    line = ext.read_text().splitlines()[2] if ext.exists() else "(missing)"
    return line


if __name__ == "__main__":
    cases = {
        "none": "",
        "inp_vdd": INP_VDD,
        "inp_vss": INP_VSS,
        "inn_vdd": INN_VDD,
        "inn_vss": INN_VSS,
        "all_inp": INP_VDD + INP_VSS,
        "all_inn": INN_VDD + INN_VSS,
        "all": INP_VDD + INP_VSS + INN_VDD + INN_VSS,
    }
    for tag, body in cases.items():
        print(tag, "->", run(tag, body))
