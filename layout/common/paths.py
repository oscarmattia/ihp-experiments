"""Resolution of PDK and toolchain paths.

Everything is derived from ``PDK_ROOT`` so the layout flow follows whatever
PDK checkout ``env.sh`` activated, rather than embedding a copy of the tree.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


class PdkNotFoundError(RuntimeError):
    """Raised when PDK_ROOT is unset or does not contain the expected tree."""


@dataclass(frozen=True)
class PdkPaths:
    """Absolute paths into the installed IHP Open PDK."""

    root: Path
    pdk: str

    @property
    def tech(self) -> Path:
        return self.root / self.pdk / "libs.tech"

    # --- KLayout -----------------------------------------------------------
    @property
    def klayout(self) -> Path:
        return self.tech / "klayout"

    @property
    def klayout_tech(self) -> Path:
        return self.klayout / "tech"

    @property
    def lyt_file(self) -> Path:
        return self.klayout_tech / "sg13g2.lyt"

    @property
    def lyp_file(self) -> Path:
        return self.klayout_tech / "sg13g2.lyp"

    @property
    def drc_runner(self) -> Path:
        return self.klayout_tech / "drc" / "run_drc.py"

    @property
    def drc_deck(self) -> Path:
        return self.klayout_tech / "drc" / "ihp-sg13g2.drc"

    @property
    def drc_layers_def(self) -> Path:
        return self.klayout_tech / "drc" / "rule_decks" / "layers_def.drc"

    @property
    def drc_testcases(self) -> Path:
        return self.klayout_tech / "drc" / "testing" / "testcases" / "unit"

    @property
    def lvs_runner(self) -> Path:
        return self.klayout_tech / "lvs" / "run_lvs.py"

    @property
    def lvs_deck(self) -> Path:
        return self.klayout_tech / "lvs" / "sg13g2.lvs"

    @property
    def lvs_testcases(self) -> Path:
        return self.klayout_tech / "lvs" / "testing" / "testcases" / "unit"

    # --- Foundry PCells ----------------------------------------------------
    @property
    def pycell_path(self) -> Path:
        return self.klayout / "python"

    @property
    def pycell_api_path(self) -> Path:
        return self.klayout / "python" / "pycell4klayout-api" / "source" / "python"

    # --- Magic / netgen ----------------------------------------------------
    @property
    def magic(self) -> Path:
        return self.tech / "magic"

    @property
    def magic_rcfile(self) -> Path:
        return self.magic / "ihp-sg13g2.magicrc"

    @property
    def netgen_setup(self) -> Path:
        return self.tech / "netgen" / "ihp-sg13g2_setup.tcl"

    # --- Parasitics --------------------------------------------------------
    @property
    def itf_file(self) -> Path:
        return self.tech / "parasitics" / "itf" / "sg13g2_typ.itf"

    @property
    def versions_file(self) -> Path:
        return self.root / "versions.txt"

    def pinned_version(self, tool: str) -> str | None:
        """Return the version ``versions.txt`` pins for ``tool``.

        The PDK treats this file as the single source of truth and its own
        ``run_drc.py`` enforces the KLayout entry, so we read it rather than
        hardcoding versions anywhere in the flow.
        """
        if not self.versions_file.is_file():
            return None
        for line in self.versions_file.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == tool:
                return parts[1]
        return None


@lru_cache(maxsize=1)
def pdk_paths() -> PdkPaths:
    """Resolve the active PDK from the environment."""
    root = os.environ.get("PDK_ROOT")
    if not root:
        raise PdkNotFoundError(
            "PDK_ROOT is not set. Run: source ~/.local/share/ihp-eda/env.sh"
        )
    pdk = os.environ.get("PDK", "ihp-sg13g2")
    paths = PdkPaths(root=Path(root), pdk=pdk)
    if not paths.tech.is_dir():
        raise PdkNotFoundError(f"No libs.tech under {paths.root / paths.pdk}")
    return paths


def repo_root() -> Path:
    """Repository root, derived from this file's location."""
    return Path(__file__).resolve().parents[2]
