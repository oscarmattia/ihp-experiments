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

#: Termination — devices only; pad caps live in the testbench.
TERM_DUT_PORTS = "inp inn vdd 0"
TERM_DUT_BIAS = (
    "* Bond-pad hand cap (sg13g2_bondpad.lib is empty)\n"
    "Cpadp inp 0 {PAD_C}\n"
    "Cpadn inn 0 {PAD_C}"
)
TERM_NODESET = ""

#: VGA — tail mirror, CM reference and steering gates are testbench-side.
VGA_DUT_PORTS = "outp outn inp inn vicm steerp steern vdd 0 mgate"
VGA_DUT_BIAS = (
    "Iref vdd mgate {ITAIL}\n"
    "Vicm vicm 0 dc {VBASE}\n"
    "Vsp steerp 0 dc {VCTRL_P}\n"
    "Vsn steern 0 dc {VCTRL_N}"
)
VGA_NODESET = (
    ".nodeset v(mgate)={MOS_VGS} v(xu1.em)=0.28 v(xu1.ed1)=0.28 v(xu1.ed2)=0.28 "
    "v(xu1.tx1)=0.28 v(xu1.tx2)=0.28"
)

#: Pad driver — mirror reference and pad caps are testbench-side.
DRIVER_DUT_PORTS = "outp outn inp inn vdd 0 mgate"
DRIVER_DUT_BIAS = (
    "Iref vdd mgate {ITAIL_HALF}\n"
    "Cpadp outp 0 {PAD_C}\n"
    "Cpadn outn 0 {PAD_C}"
)
DRIVER_NODESET = ".nodeset v(mgate)={MOS_VGS} v(xu1.em)=0.28"

#: Chain top — stages are device-only inside; chain_dut carries their sources.
CHAIN_DUT_PORTS = "outp outn inp inn vdd"
CHAIN_DUT_BIAS = ""
CHAIN_NODESET = (
    ".nodeset v(xu1.ctle_mgate)={MOS_VGS} v(xu1.vga_mgate)={MOS_VGS} "
    "v(xu1.drv_mgate)={MOS_VGS} v(outp)=1.40 v(outn)=1.40"
)


def exp_root() -> Path:
    return _EXP


def pass_out(pass_name: str) -> Path:
    """Per-pass output directory: out/ideal or out/pdk."""
    return _EXP / "out" / pass_name


CL_MARKER = "* postlayout-cl-model:"


def declared_cl_model(dut: Path) -> str | None:
    """The load model the netlist itself asks for, if it says.

    A netlist that carries its own interconnect capacitance needs the Miller term
    only; one that carries no parasitics needs the full CL. The generator knows
    which it built and records it, so the right answer does not depend on the
    caller remembering. On the pad driver, ``miller`` also means drop the
    testbench ``PAD_C`` (Magic already extracted the bond-pad metal).
    """
    path = Path(dut)
    if not path.is_file():
        return None
    for line in path.read_text().splitlines()[:8]:
        if line.startswith(CL_MARKER):
            value = line[len(CL_MARKER):].strip()
            if value in ("full", "miller"):
                return value
    return None


def resolve_dut_path(dut: str | Path, spice_dir: Path, repo: Path | None = None) -> Path:
    """Resolve a DUT path: existing file, spice-dir relative, or repo relative."""
    path = Path(dut)
    if path.is_file():
        return path.resolve()
    spice_cand = spice_dir / path
    if spice_cand.is_file():
        return spice_cand.resolve()
    root = repo if repo is not None else _REPO
    repo_cand = root / path
    if repo_cand.is_file():
        return repo_cand.resolve()
    raise FileNotFoundError(f"DUT netlist not found: {dut}")


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


#: Leading columns in AC ``wrdata`` with ``wr_singlescale`` (complex scale): freq, freq, freq_imag.
AC_WRDATA_COMPLEX_SCALE_COLS = 3


def parse_ac_vm_vp_raw(raw: Path, n_pairs: int) -> tuple[np.ndarray, list[np.ndarray]]:
    """Parse AC ``wrdata`` with ``wr_singlescale`` (complex scale).

    Layout: ``freq, freq, freq_imag`` then ``n_pairs`` of ``(vm, vp)`` columns.
    Total width must be ``AC_WRDATA_COMPLEX_SCALE_COLS + 2 * n_pairs``.
    """
    rows = []
    with raw.open() as f:
        for line in f:
            parts = line.split()
            if parts:
                rows.append([float(x) for x in parts])
    arr = np.asarray(rows)
    n_data = 2 * n_pairs
    ncol_expected = AC_WRDATA_COMPLEX_SCALE_COLS + n_data
    if arr.shape[1] != ncol_expected:
        raise RuntimeError(
            f"{raw}: expected {ncol_expected} columns "
            f"({AC_WRDATA_COMPLEX_SCALE_COLS} scale + {n_data} data) "
            f"for {n_pairs} vm/vp pairs, got {arr.shape[1]}"
        )
    freq = arr[:, 0]
    start = AC_WRDATA_COMPLEX_SCALE_COLS
    pairs = [
        complex_from_vm_vp(arr[:, start + 2 * i], arr[:, start + 2 * i + 1])
        for i in range(n_pairs)
    ]
    return freq, pairs


def assert_unit_diff_source(
    v_p: np.ndarray,
    v_n: np.ndarray,
    *,
    tol: float = 1e-3,
    label: str = "vsrc",
) -> None:
    """Ideal 0.5/0° + 0.5/180° sources must give |v_p - v_n| == 1 at every frequency."""
    vsrc = v_p - v_n
    mag = np.abs(vsrc)
    err = float(np.max(np.abs(mag - 1.0)))
    if err > tol:
        idx = int(np.argmax(np.abs(mag - 1.0)))
        raise ValueError(
            f"{label}: |v_p - v_n| must be 1.0 at all frequencies "
            f"(max err {err:.2e} at index {idx}, |vsrc|={mag[idx]:.4f})"
        )


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
    """Parse standard 4-pair AC diff wrdata (outp, outn, inp, inn or equivalent order)."""
    freq, pairs = parse_ac_vm_vp_raw(raw, n_pairs=4)
    voutp, voutn, vin_p, vin_n = pairs
    return freq, voutp, voutn, vin_p, vin_n


def parse_tran_raw(raw: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Parse transient wrdata: ``time`` duplicated, then real voltage vectors."""
    rows = []
    with raw.open() as f:
        for line in f:
            parts = line.split()
            if parts:
                rows.append([float(x) for x in parts])
    arr = np.asarray(rows)
    ncol = arr.shape[1]
    # wr_singlescale on real vectors: time, time, then one column per signal.
    if ncol >= 6:
        return arr[:, 0], arr[:, 2], arr[:, 3], arr[:, 4], arr[:, 5]
    if ncol >= 5:
        return arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4]
    raise RuntimeError(
        f"{raw}: expected ≥5 columns (time + signals), got {ncol}"
    )
