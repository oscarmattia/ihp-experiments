"""Shared LUT I/O for device characterization.

Prefer NumPy ``.npz`` for fast, portable design-space tables. Optional
pickle export remains available for pygmid-compatible MOS tables.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

META_KEY = "__meta__"


def save_lut(path: Path, arrays: dict[str, np.ndarray], meta: dict[str, Any]) -> Path:
    """Write arrays + JSON-serializable metadata into a compressed npz."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: np.asarray(v) for k, v in arrays.items()}
    # Store meta as a 0-d unicode array so np.savez_compressed accepts it
    payload[META_KEY] = np.asarray(json.dumps(meta, sort_keys=True))
    np.savez_compressed(path, **payload)
    return path


def load_lut(path: Path) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    path = Path(path)
    with np.load(path, allow_pickle=False) as data:
        meta = json.loads(str(data[META_KEY]))
        arrays = {k: data[k] for k in data.files if k != META_KEY}
    return arrays, meta


def parse_wrdata(path: Path, names: list[str]) -> dict[str, np.ndarray]:
    """Parse ngspice wrdata (interleaved x,y columns) into named vectors."""
    rows: list[list[float]] = []
    with Path(path).open() as f:
        for line in f:
            parts = line.split()
            if parts:
                rows.append([float(x) for x in parts])
    data = np.asarray(rows, dtype=float)
    values = np.delete(data, list(range(0, data.shape[1], 2)), axis=1).T
    if values.shape[0] != len(names):
        raise RuntimeError(
            f"{path}: expected {len(names)} vectors, got {values.shape[0]}"
        )
    return {name: values[i] for i, name in enumerate(names)}


def matrange(start: float, step: float, stop: float) -> np.ndarray:
    n = int(round((stop - start) / step + 1))
    return np.linspace(start, stop, n)
