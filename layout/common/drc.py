"""DRC through the PDK's own rule deck, with machine-readable results.

``run_drc.py`` writes a KLayout report database (``.lyrdb``) and a log. Neither
is convenient for an agent to act on, so this module drives the PDK runner and
reduces the report to a JSON summary: total violations, a count per rule, and a
few example coordinates per rule for the ones that failed.

Density and antenna checks are off by default. Both are context checks that
need the surrounding chip — a bare device cell always violates metal density —
so running them on an isolated device would report failures that mean nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from layout.common.paths import pdk_paths

#: Examples recorded per failing rule.
MAX_EXAMPLES = 5


@dataclass
class DrcResult:
    """Outcome of one DRC run."""

    cell: str
    gds: str
    clean: bool
    total: int
    by_rule: dict[str, int] = field(default_factory=dict)
    examples: dict[str, list[str]] = field(default_factory=dict)
    tables: list[str] = field(default_factory=list)
    report: str = ""
    log: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "cell": self.cell,
            "gds": self.gds,
            "clean": self.clean,
            "total_violations": self.total,
            "by_rule": self.by_rule,
            "examples": self.examples,
            "tables": self.tables,
            "report": self.report,
            "log": self.log,
            "error": self.error,
        }

    def write(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        return path


def parse_lyrdb(path: Path) -> tuple[int, dict[str, int], dict[str, list[str]]]:
    """Reduce a KLayout report database to counts and examples per rule."""
    tree = ET.parse(path)
    root = tree.getroot()
    by_rule: dict[str, int] = {}
    examples: dict[str, list[str]] = {}
    total = 0
    for item in root.iter("item"):
        category = item.findtext("category", default="").strip().strip("'\"")
        rule = category or "unknown"
        by_rule[rule] = by_rule.get(rule, 0) + 1
        total += 1
        if len(examples.setdefault(rule, [])) < MAX_EXAMPLES:
            values = item.find("values")
            if values is not None:
                text = " ".join(
                    (value.text or "").strip() for value in values.iter("value")
                ).strip()
                if text:
                    examples[rule].append(text)
    examples = {rule: shots for rule, shots in examples.items() if shots}
    return total, dict(sorted(by_rule.items())), examples


def run_drc(
    gds: Path,
    run_dir: Path,
    cell_name: str | None = None,
    tables: list[str] | None = None,
    density: bool = False,
    antenna: bool = False,
    extra_rules: bool = False,
    threads: int = 1,
    timeout: int = 3600,
) -> DrcResult:
    """Run the PDK DRC deck on ``gds`` and summarize the report."""
    paths = pdk_paths()
    gds = Path(gds).resolve()
    run_dir = Path(run_dir).resolve()
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    name = cell_name or gds.stem
    python = os.environ.get("IHP_PYTHON", "python3")
    cmd = [
        python,
        str(paths.drc_runner),
        f"--path={gds}",
        f"--run_dir={run_dir}",
        f"--mp={threads}",
        "--run_mode=deep",
    ]
    if not density:
        cmd.append("--no_density")
    if antenna:
        cmd.append("--antenna")
    if not extra_rules:
        # These are the PDK's own "residual" rules, which its README flags as
        # unverified and slow; the CI regression also runs with them disabled.
        cmd.append("--disable_extra_rules")
    for table in tables or []:
        cmd.append(f"--table={table}")

    result = DrcResult(cell=name, gds=str(gds), clean=False, total=-1, tables=tables or [])
    try:
        completed = subprocess.run(
            cmd, cwd=run_dir, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        result.error = f"DRC timed out after {timeout}s"
        return result

    log_path = run_dir / "drc_run.log"
    log_path.write_text(
        f"$ {' '.join(cmd)}\n\n--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}\n"
    )
    result.log = str(log_path)

    reports = sorted(run_dir.rglob("*.lyrdb"))
    if not reports:
        result.error = (
            f"run_drc.py produced no .lyrdb (exit {completed.returncode}); see {log_path}"
        )
        return result

    total = 0
    by_rule: dict[str, int] = {}
    examples: dict[str, list[str]] = {}
    for report in reports:
        count, rules, shots = parse_lyrdb(report)
        total += count
        for rule, value in rules.items():
            by_rule[rule] = by_rule.get(rule, 0) + value
        for rule, values in shots.items():
            examples.setdefault(rule, []).extend(values[:MAX_EXAMPLES])

    result.total = total
    result.by_rule = dict(sorted(by_rule.items()))
    result.examples = {rule: values[:MAX_EXAMPLES] for rule, values in examples.items()}
    result.report = str(reports[0])
    result.clean = total == 0
    if completed.returncode != 0 and total == 0:
        # A non-zero exit with an empty report means the deck itself failed
        # rather than the layout being clean.
        result.clean = False
        result.error = f"run_drc.py exited {completed.returncode}; see {log_path}"
    return result
