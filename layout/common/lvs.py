"""LVS through the PDK's own rule deck, with machine-readable results.

``run_lvs.py`` compares extracted layout against a CDL netlist and writes an
``.lvsdb`` plus an extracted netlist. This module drives it and reduces the
outcome to JSON, and additionally diffs the *extracted device parameters*
against what the DeviceSpec asked for. That second check matters: a compare can
pass on topology while the device is the wrong size, which is exactly the
failure mode a PCell ``Calculate`` mistake produces.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from layout.common.devices import kind_of
from layout.common.paths import pdk_paths
from layout.common.spec import DeviceSpec

#: Fractional tolerance when comparing extracted geometry against intent.
PARAM_TOL = 0.02

_SI = {
    "t": 1e12, "g": 1e9, "meg": 1e6, "k": 1e3,
    "m": 1e-3, "u": 1e-6, "µ": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15, "a": 1e-18,
}

_NUM_RE = re.compile(r"^([+-]?\d*\.?\d+(?:[eE][+-]?\d+)?)\s*([a-zA-Zµ]*)$")


_LVS_PREFIX = re.compile(
    r"^\s*\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}: "
    r"Memory Usage \(\d+K\) : (?:INFO|ERROR|WARNING) : "
)


def _verdict_only(line: str) -> str:
    """Strip the deck's timestamp and memory prefix from a verdict line.

    The verdict is the result; the wall-clock time and the resident size are
    telemetry. These summaries are committed, so leaving them in meant every run
    rewrote the JSON and a real change had to be picked out of that noise.
    """
    return _LVS_PREFIX.sub("", line).strip()


def parse_si(text: str) -> float | None:
    """Parse a SPICE number with an optional SI suffix."""
    match = _NUM_RE.match(text.strip())
    if not match:
        return None
    value = float(match.group(1))
    suffix = match.group(2).lower()
    if not suffix:
        return value
    if suffix in _SI:
        return value * _SI[suffix]
    # A trailing unit letter after a multiplier, e.g. "1uF" or "900nm".
    if len(suffix) >= 2 and suffix[0] in _SI:
        return value * _SI[suffix[0]]
    return value


@dataclass
class LvsResult:
    """Outcome of one LVS run."""

    cell: str
    layout: str
    netlist: str
    clean: bool = False
    devices_matched: int = 0
    summary: str = ""
    extracted_netlist: str = ""
    param_checks: list[dict] = field(default_factory=list)
    params_ok: bool = True
    log: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "cell": self.cell,
            "layout": self.layout,
            "netlist": self.netlist,
            "clean": self.clean,
            "params_ok": self.params_ok,
            "devices_matched": self.devices_matched,
            "summary": self.summary,
            "extracted_netlist": self.extracted_netlist,
            "param_checks": self.param_checks,
            "log": self.log,
            "error": self.error,
        }

    def write(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        return path


def run_lvs(
    gds: Path,
    cdl: Path,
    run_dir: Path,
    topcell: str | None = None,
    implicit_nets: str = "sub,sub!",
    ignore_top_ports: bool = True,
    disable_tap_extraction: bool = False,
    timeout: int = 3600,
) -> LvsResult:
    """Run the PDK LVS deck on ``gds`` against ``cdl``.

    ``disable_tap_extraction`` is for cells with guard rings. Ring taps are
    layout-only: they exist to satisfy latch-up, not as schematic devices, so the
    deck extracts hundreds of ntap1/ptap1 devices that no netlist declares and
    the compare cannot match. The PDK provides this switch for exactly that case.
    """
    paths = pdk_paths()
    gds = Path(gds).resolve()
    cdl = Path(cdl).resolve()
    run_dir = Path(run_dir).resolve()
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    python = os.environ.get("IHP_PYTHON", "python3")
    cmd = [
        python,
        str(paths.lvs_runner),
        f"--layout={gds}",
        f"--netlist={cdl}",
        f"--run_dir={run_dir}",
        "--run_mode=deep",
    ]
    if topcell:
        cmd.append(f"--topcell={topcell}")
    if implicit_nets:
        cmd.append(f"--implicit_nets={implicit_nets}")
    if ignore_top_ports:
        # The PDK's own testcase layouts carry no top-level ports, so its
        # runner is documented and invoked with this flag; matching that keeps
        # us on the supported path.
        cmd.append("--ignore_top_ports_mismatch")
    if disable_tap_extraction:
        cmd.append("--disable_tap_extraction")

    result = LvsResult(cell=topcell or gds.stem, layout=str(gds), netlist=str(cdl))
    try:
        completed = subprocess.run(
            cmd, cwd=run_dir, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        result.error = f"LVS timed out after {timeout}s"
        return result

    log_path = run_dir / "lvs_run.log"
    combined = f"{completed.stdout}\n{completed.stderr}"
    log_path.write_text(
        f"$ {' '.join(cmd)}\n\n--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}\n"
    )
    result.log = str(log_path)

    # The verdict has to come from the report, not the exit status: run_lvs.py
    # exits 0 even when the compare fails, so trusting the return code reports
    # a mismatched netlist as clean.
    matched = "Netlists match" in combined
    mismatched = "Netlists don't match" in combined
    result.clean = matched and not mismatched

    for marker in ("Congratulations", "Netlists don't match", "LVS Check Failed", "ERROR"):
        for line in combined.splitlines():
            if marker in line:
                result.summary = _verdict_only(line)
                break
        if result.summary:
            break
    if not result.summary:
        result.summary = (
            f"run_lvs.py exited {completed.returncode} with no compare verdict in its output"
        )

    extracted = sorted(run_dir.rglob("*_extracted.cir"))
    if extracted:
        result.extracted_netlist = str(extracted[0])
    if not result.clean and not result.error:
        if mismatched:
            result.error = f"LVS compare failed: netlists do not match; see {log_path}"
        else:
            result.error = (
                f"no LVS verdict found (run_lvs.py exited {completed.returncode}); see {log_path}"
            )
    return result


def check_extracted_params(
    spec: DeviceSpec, extracted_netlist: Path, tol: float = PARAM_TOL
) -> tuple[bool, list[dict]]:
    """Compare geometry in the extracted netlist against the DeviceSpec.

    LVS can pass on topology while the device is the wrong size, so the
    extracted ``w``/``l``/``m`` are checked against what the spec asked for.
    """
    path = Path(extracted_netlist)
    if not path.is_file():
        return False, [{"error": f"missing extracted netlist {path}"}]

    kind = kind_of(spec)
    if kind.to_extracted is not None:
        wanted = {key.lower(): value for key, value in kind.to_extracted(spec.params).items()}
    else:
        wanted = {
            key.lower(): parse_si(value) for key, value in kind.to_cdl(spec.params).items()
        }
    text = path.read_text()

    device_line = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("*", ".")):
            continue
        if kind.spice_model in stripped:
            device_line = stripped
            break

    if device_line is None:
        return False, [{"error": f"no {kind.spice_model} device found in {path.name}"}]

    # The deck writes MOS geometry uppercase (L=0.13u W=1u) and passive
    # geometry lowercase (w=5u l=1.4u), so match case-insensitively.
    found: dict[str, float | None] = {}
    for token in device_line.split():
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        found[key.strip().lower()] = parse_si(value)

    # "area" is not reported directly; derive it from the extracted w and l so
    # capacitors can be checked on the quantity that sets their capacitance.
    if "area" in wanted and found.get("w") is not None and found.get("l") is not None:
        found["area"] = found["w"] * found["l"]

    checks: list[dict] = []
    ok = True
    for key, want in wanted.items():
        if want is None:
            continue
        got = found.get(key)
        if got is None:
            # Not every CDL parameter is reported back: the deck omits ng and
            # folds multiplicity into the device count. Record it as skipped
            # rather than failing on a parameter it never claims to extract.
            checks.append({"param": key, "intended": want, "extracted": None, "skipped": True})
            continue
        rel = abs(got - want) / abs(want) if want else abs(got)
        passed = rel <= tol
        ok = ok and passed
        checks.append(
            {
                "param": key,
                "intended": want,
                "extracted": got,
                "rel_error": round(rel, 6),
                "ok": passed,
            }
        )

    compared = [c for c in checks if not c.get("skipped")]
    if not compared:
        return False, [
            {
                "error": (
                    f"{kind.spice_model} found in {path.name} but none of "
                    f"{sorted(wanted)} were reported; extracted keys: {sorted(found)}"
                )
            }
        ]
    return ok, checks
