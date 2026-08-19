"""Headless access to the foundry PCell library.

The PDK bootstraps ``sg13g2_pycell_lib`` from a KLayout autorun macro
(``tech/pymacros/autorun.lym``), which only fires inside the KLayout
application. This module reproduces that bootstrap for the standalone
``klayout`` Python module so device layout can be generated in batch.

Two things here are easy to get wrong and cost real debugging time:

* ``pya.Library.library_by_name("SG13_dev")`` returns ``None``. The library is
  registered against a technology, so the technology name is a required second
  argument even though it reads as optional.
* Resistor and capacitor PCells ignore ``w``/``l`` unless ``Calculate`` is
  switched away from its default. ``rppd`` defaults to solving for ``l`` from a
  target ``R``, and ``cmim`` to solving for ``w&l`` from a target ``C``, so
  passing a geometry without setting ``Calculate`` silently yields the default
  device. See ``devices.py``, which sets it for every affected PCell.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
from functools import lru_cache
from typing import Any

from layout.common.paths import pdk_paths

#: KLayout database unit: 1 nm, matching sg13g2.lyt.
DBU = 0.001


@contextlib.contextmanager
def _quiet():
    """Swallow the PCell library's import chatter.

    Importing sg13g2_pycell_lib prints technology banners and one "psutil not
    found" warning per registered cell. Callers that emit JSON need clean
    stdout, so the noise goes to stderr-free limbo unless debugging.
    """
    if os.environ.get("IHP_LAYOUT_VERBOSE"):
        yield
        return
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield


def _ensure_sys_path() -> None:
    paths = pdk_paths()
    for entry in (paths.pycell_api_path, paths.pycell_path):
        text = str(entry)
        if text not in sys.path:
            sys.path.insert(0, text)


@lru_cache(maxsize=1)
def pya_module():
    """Import and return the KLayout Python API."""
    _ensure_sys_path()
    import pya  # noqa: PLC0415  (import must follow sys.path setup)

    return pya


@lru_cache(maxsize=1)
def tech_name() -> str:
    """Technology name the PCell library registers itself under."""
    _ensure_sys_path()
    with _quiet():
        from sg13g2_pycell_lib.sg13_tech import SG13_Tech  # noqa: PLC0415

    return SG13_Tech.TECH_NAME


@lru_cache(maxsize=1)
def pcell_library():
    """Return the ``SG13_dev`` PCell library, bootstrapped headlessly."""
    pya = pya_module()
    _ensure_sys_path()
    with _quiet():
        import sg13g2_pycell_lib  # noqa: F401, PLC0415

    lib = pya.Library.library_by_name("SG13_dev", tech_name())
    if lib is None:
        available = list(pya.Library.library_names())
        raise RuntimeError(
            "SG13_dev PCell library not found. "
            f"Registered libraries: {available}. "
            "library_by_name() needs the technology name as its second argument."
        )
    return lib


def pcell_names() -> list[str]:
    """Names of every PCell the foundry library provides."""
    lib = pcell_library()
    ld = lib.layout()
    names = []
    for pcell_id in ld.pcell_ids():
        decl = ld.pcell_declaration(pcell_id)
        names.append(decl.name() if hasattr(decl, "name") else str(pcell_id))
    return sorted(names)


def pcell_parameters(pcell: str) -> dict[str, Any]:
    """Default parameter values for a PCell, as declared by the PDK."""
    ld = pcell_library().layout()
    decl = ld.pcell_declaration(pcell)
    if decl is None:
        raise KeyError(f"No such PCell: {pcell}")
    return {p.name: p.default for p in decl.get_parameters()}


def new_layout():
    """An empty layout with the PDK database unit already set."""
    pya = pya_module()
    ly = pya.Layout()
    ly.dbu = DBU
    return ly


def instantiate(pcell: str, params: dict[str, Any], layout=None):
    """Place ``pcell`` with ``params`` and return ``(layout, cell)``.

    Parameter values are passed through as given: the PCells parse strings
    with SI suffixes (``"5u"``) as well as plain numbers, and the PDK's own
    examples use the string form.
    """
    pya = pya_module()
    lib = pcell_library()
    ld = lib.layout()
    if ld.pcell_id(pcell) is None:
        raise KeyError(f"No such PCell: {pcell}")

    ly = layout if layout is not None else new_layout()
    with _quiet():
        variant_id = ly.add_pcell_variant(lib, ld.pcell_id(pcell), params)
    cell = ly.cell(variant_id)
    if cell is None:
        raise RuntimeError(f"PCell variant for {pcell} could not be created")
    _ = pya  # kept for symmetry with callers that need the module
    return ly, cell
