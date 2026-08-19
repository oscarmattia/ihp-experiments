"""GDS to PNG rendering for layout artifacts.

Uses the PDK layer properties file so devices render with foundry colours,
matching the look of the existing screenshots in ``char/passive/out/layouts``.
Rendering runs through the KLayout application in batch mode because the
standalone Python module has no layout view.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from layout.common.paths import pdk_paths

# gds, lyp, png and width arrive as -rd globals set on the command line.
_RENDER_SCRIPT = """
import pya
view = pya.LayoutView()
view.load_layout(gds, 0)
if lyp:
    view.load_layer_props(lyp)
view.max_hier()
view.zoom_fit()
box = view.box()
aspect = (box.height() / box.width()) if box.width() > 0 else 1.0
w = int(width)
height = max(120, min(2400, int(w * aspect)))
view.save_image(png, w, height)
"""


def render_gds(
    gds: Path,
    png: Path,
    width: int = 900,
    timeout: int = 300,
) -> Path | None:
    """Render ``gds`` to ``png``; returns None when KLayout is unavailable."""
    klayout = shutil.which("klayout")
    if klayout is None:
        return None

    gds = Path(gds).resolve()
    png = Path(png).resolve()
    png.parent.mkdir(parents=True, exist_ok=True)
    lyp = pdk_paths().lyp_file
    lyp_arg = str(lyp) if lyp.is_file() else ""

    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "render.py"
        script.write_text(_RENDER_SCRIPT)
        env = dict(os.environ)
        # Rendering needs no display, but Qt still wants a platform plugin.
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        completed = subprocess.run(
            [
                klayout, "-b",
                "-r", str(script),
                "-rd", f"gds={gds}",
                "-rd", f"lyp={lyp_arg}",
                "-rd", f"png={png}",
                "-rd", f"width={width}",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    if png.is_file() and png.stat().st_size > 0:
        return png
    # Batch rendering is a convenience, never a gate; report and move on.
    detail = (completed.stderr or completed.stdout or "").strip().splitlines()
    if detail:
        print(f"render: {gds.name} failed: {detail[-1]}")
    return None
