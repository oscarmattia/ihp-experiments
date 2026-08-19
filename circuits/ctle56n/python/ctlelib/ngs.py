"""ngspice testbench preparation, invocation, and raw/log parsing."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[4]
_EXP = Path(__file__).resolve().parents[2]

CTLE_DC_SAVE_LINES = (
    "save v(outp) v(outn) v(inp) v(inn) v(vdd)\n"
    "save v(xu1.e1) v(xu1.e2) v(mgate)\n"
    "save @q.xu1.xq1.qnpn13g2[ic] @q.xu1.xq2.qnpn13g2[ic]\n"
    "save @n.xu1.xtail1.nsg13_lv_nmos[ids] @n.xu1.xtail2.nsg13_lv_nmos[ids]\n"
    "save @n.xu1.xmdiode.nsg13_lv_nmos[ids]"
)
CTLE_DC_PRINT_LINES = (
    "print v(outp) v(outn) v(inp) v(inn) v(vdd)\n"
    "print v(xu1.e1) v(xu1.e2) v(mgate)\n"
    "print @q.xu1.xq1.qnpn13g2[ic] @q.xu1.xq2.qnpn13g2[ic]\n"
    "print @n.xu1.xtail1.nsg13_lv_nmos[ids] @n.xu1.xtail2.nsg13_lv_nmos[ids]\n"
    "print @n.xu1.xmdiode.nsg13_lv_nmos[ids]"
)

#: Port list a DUT presents to a testbench. The CTLE exposes vss and the mirror
#: gate because its bias current is an ideal source and therefore belongs to the
#: testbench, not to the cell: a cell that is going to be laid out can only
#: contain devices. `vss` is wired to the global 0 by the testbench.
CTLE_DUT_PORTS = "outp outn inp inn vdd 0 mgate"

#: Bias the testbench supplies for the CTLE, replacing the Iref that used to sit
#: inside the subcircuit.
CTLE_DUT_BIAS = "Iref vdd mgate {ITAIL}"

#: Convergence hints. mgate is a testbench node for the CTLE, so it is named
#: without the xu1 prefix; e1/e2 remain internal.
CTLE_NODESET = ".nodeset v(mgate)={MOS_VGS} v(xu1.e1)=0.28 v(xu1.e2)=0.28"

#: Stages that still carry their own sources keep the pre-existing 5-port
#: interface and hierarchical nodeset. They need the same treatment as the CTLE
#: before they can be laid out; see docs/LAYOUT.md.
LEGACY_DUT_PORTS = "outp outn inp inn vdd"
LEGACY_NODESET = (
    ".nodeset v(xu1.mgate)={MOS_VGS} v(xu1.em)=0.28 v(xu1.e1)=0.28 v(xu1.e2)=0.28"
)


def exp_root() -> Path:
    return _EXP


def pass_out(pass_name: str) -> Path:
    """Per-pass output directory: out/ideal or out/pdk."""
    return _EXP / "out" / pass_name


def pdk_models() -> Path:
    pdk = os.environ.get("PDK_ROOT")
    if not pdk:
        raise SystemExit("PDK_ROOT not set — source ~/.local/share/ihp-eda/env.sh")
    return Path(pdk) / "ihp-sg13g2" / "libs.tech" / "ngspice" / "models"


def ngspice_exe() -> str:
    exe = shutil.which("ngspice")
    if not exe:
        raise SystemExit("ngspice not found on PATH")
    return exe


def _read_param(inc: Path, name: str) -> str | None:
    if not inc.is_file():
        return None
    for line in inc.read_text().splitlines():
        m = re.match(rf"\.param\s+{name}=([\S]+)", line, re.I)
        if m:
            return m.group(1)
    return None


def apply_params(text: str, spice_dir: Path, extra: dict[str, str] | None = None) -> str:
    """Replace {PARAM} tokens from spice/params.inc and optional extras."""
    inc = spice_dir / "params.inc"
    params: dict[str, str] = {}
    if inc.is_file():
        for line in inc.read_text().splitlines():
            m = re.match(r"\.param\s+(\w+)=([\S]+)", line, re.I)
            if m:
                params[m.group(1)] = m.group(2)
    if extra:
        params.update(extra)
    for key, val in params.items():
        text = text.replace(f"{{{key}}}", val)
    return text


def prepare_tb(
    template: Path,
    dut_cir: Path,
    work: Path,
    models: Path,
    spice_dir: Path,
    extra_params: dict[str, str] | None = None,
    *,
    dut_name: str = "ctle_dut",
    cl_tb: str | None = None,
    dc_save_lines: str | None = None,
    dc_print_lines: str | None = None,
    dut_ports: str | None = None,
    dut_bias: str | None = None,
    dut_nodeset: str | None = None,
) -> Path:
    # The port list, the bias lines and the nodeset go in before parameters are
    # substituted, so tokens inside them (ITAIL, MOS_VGS) resolve too.
    text = template.read_text()
    text = text.replace("{DUT_PORTS}", dut_ports if dut_ports is not None else CTLE_DUT_PORTS)
    text = text.replace("{DUT_BIAS}", dut_bias if dut_bias is not None else CTLE_DUT_BIAS)
    text = text.replace("{DUT_NODESET}", dut_nodeset if dut_nodeset is not None else CTLE_NODESET)
    text = apply_params(text, spice_dir, extra_params)
    if cl_tb is None:
        cl_tb = _read_param(spice_dir / "params.inc", "CL")
        if cl_tb is None:
            raise ValueError("cl_tb not given and CL missing from params.inc")
    text = text.replace("{DUT_NAME}", dut_name)
    text = text.replace("{CL_TB}", cl_tb)
    text = text.replace("{DC_SAVE_LINES}", dc_save_lines or CTLE_DC_SAVE_LINES)
    text = text.replace("{DC_PRINT_LINES}", dc_print_lines or CTLE_DC_PRINT_LINES)
    text = text.replace("{PDK_MODELS}", str(models))
    dut_local = work / dut_cir.name
    dut_text = apply_params(dut_cir.read_text(), spice_dir, extra_params).replace(
        "{PDK_MODELS}", str(models)
    )
    dut_local.write_text(dut_text)
    text = text.replace("{DUT_CIR}", str(dut_local.resolve()))
    out = work / template.name
    out.write_text(text)
    return out


def run_ngspice(cir: Path, work: Path, log_name: str = "ngspice.log") -> Path:
    log = work / log_name
    exe = ngspice_exe()
    with log.open("w") as lf:
        proc = subprocess.run(
            [exe, "-b", "-o", str(log), str(cir)],
            cwd=work,
            stdout=lf,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if proc.returncode != 0:
        tail = log.read_text()[-4000:]
        raise RuntimeError(f"ngspice failed on {cir.name}:\n{tail}")
    return log


def parse_dc_log(log: Path) -> dict[str, float]:
    text = log.read_text()
    vals: dict[str, float] = {}
    for m in re.finditer(
        r"(@?[\w\.\[\]\(\)]+)\s*=\s*([-+eE0-9.]+)",
        text,
    ):
        key, val = m.group(1), float(m.group(2))
        vals[key] = val
    return vals


def complex_from_vm_vp(vm: np.ndarray, vp: np.ndarray) -> np.ndarray:
    phase = vp
    return vm * (np.cos(phase) + 1j * np.sin(phase))


def parse_psrr_raw(raw: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """wrdata frequency vm(outp) vp(outp) vm(outn) vp(outn) vm(vdd) vp(vdd)."""
    rows = []
    with raw.open() as f:
        for line in f:
            parts = line.split()
            if parts:
                rows.append([float(x) for x in parts])
    arr = np.asarray(rows)
    if arr.shape[1] >= 9:
        freq = arr[:, 0]
        voutp = complex_from_vm_vp(arr[:, 3], arr[:, 4])
        voutn = complex_from_vm_vp(arr[:, 5], arr[:, 6])
        vvdd = complex_from_vm_vp(arr[:, 7], arr[:, 8])
        return freq, voutp, voutn, vvdd
    raise RuntimeError(f"{raw}: unexpected PSRR wrdata width {arr.shape[1]}")


def parse_ac_raw(raw: Path) -> tuple[np.ndarray, ...]:
    import sys

    if str(_REPO) not in sys.path:
        sys.path.insert(0, str(_REPO))
    from char.common.lut import parse_wrdata

    rows = []
    with raw.open() as f:
        for line in f:
            parts = line.split()
            if parts:
                rows.append([float(x) for x in parts])
    arr = np.asarray(rows)
    if arr.shape[1] >= 11:
        vm_outp, vp_outp = arr[:, 3], arr[:, 4]
        vm_outn, vp_outn = arr[:, 5], arr[:, 6]
        vm_inp, vp_inp = arr[:, 7], arr[:, 8]
        vm_inn, vp_inn = arr[:, 9], arr[:, 10]
        freq = arr[:, 0]
    else:
        names = [
            "vm_outp", "vp_outp", "vm_outn", "vp_outn",
            "vm_inp", "vp_inp", "vm_inn", "vp_inn",
        ]
        data = parse_wrdata(raw, names)
        rows_arr = np.asarray(rows)
        freq = rows_arr[:, 0]
        vm_outp = data["vm_outp"]
        vp_outp = data["vp_outp"]
        vm_outn = data["vm_outn"]
        vp_outn = data["vp_outn"]
        vm_inp = data["vm_inp"]
        vp_inp = data["vp_inp"]
        vm_inn = data["vm_inn"]
        vp_inn = data["vp_inn"]

    voutp = complex_from_vm_vp(vm_outp, vp_outp)
    voutn = complex_from_vm_vp(vm_outn, vp_outn)
    vin_p = complex_from_vm_vp(vm_inp, vp_inp)
    vin_n = complex_from_vm_vp(vm_inn, vp_inn)
    return freq, voutp, voutn, vin_p, vin_n


def parse_tran_raw(raw: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """wrdata: time v(outp) v(outn) v(inp) v(inn)."""
    rows = []
    with raw.open() as f:
        for line in f:
            parts = line.split()
            if parts:
                rows.append([float(x) for x in parts])
    arr = np.asarray(rows)
    if arr.shape[1] >= 8:
        return arr[:, 0], arr[:, 1], arr[:, 3], arr[:, 5], arr[:, 7]
    if arr.shape[1] >= 6:
        return arr[:, 0], arr[:, 2], arr[:, 3], arr[:, 4], arr[:, 5]
    if arr.shape[1] >= 5:
        return arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4]
    raise RuntimeError(f"{raw}: expected ≥5 columns, got {arr.shape[1]}")
