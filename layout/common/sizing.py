"""Read device sizes from the circuit sizing outputs.

The layout must follow the sized design, not a copy of it. ``size_ctle.py``
writes ``spice/params.inc`` (committed) and can also emit JSON; both are read
here so a resize propagates into layout without editing generator code.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from layout.common.paths import repo_root

_PARAM_RE = re.compile(r"^\s*\.param\s+(\w+)\s*=\s*([^\s*]+)", re.I)

#: Values in params.inc that are micron magnitudes rather than SI metres.
#: MOS_W/MOS_L are written in um next to MOS_W_m/MOS_L_m in metres; taking the
#: wrong one is a 1e6 error, so the metre-suffixed names are preferred and
#: these are only used as a fallback.
_UM_PARAMS = {"MOS_W", "MOS_L"}


def params_inc_path() -> Path:
    return repo_root() / "circuits" / "ctle56n" / "spice" / "params.inc"


def read_params(path: Path | None = None) -> dict[str, float]:
    """Parse ``.param NAME=VALUE`` lines into floats."""
    path = Path(path) if path else params_inc_path()
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} not found; run circuits/ctle56n/python/size_ctle.py first"
        )
    out: dict[str, float] = {}
    for line in path.read_text().splitlines():
        match = _PARAM_RE.match(line)
        if not match:
            continue
        name, raw = match.group(1), match.group(2)
        try:
            out[name] = float(raw)
        except ValueError:
            continue
    if not out:
        raise RuntimeError(f"No .param lines parsed from {path}")
    return out


def read_sizing_json(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def metres(params: dict[str, float], name: str) -> float:
    """Return a length in metres, preferring the ``_m`` variant when present."""
    if f"{name}_m" in params:
        return params[f"{name}_m"]
    value = params[name]
    return value * 1e-6 if name in _UM_PARAMS else value
