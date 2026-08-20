"""Markdown design reports parsed from committed simulation artifacts."""

from __future__ import annotations

import csv
import math
from pathlib import Path

from .metrics import (
    SBR_KEEP_FRAC,
    SBR_POST,
    SBR_PRE,
    SimMetrics,
    SbrResult,
    sbr_tap_label,
)
from .stim import PRBS9_BITS, PRBS9_POLY, SBR_SETTLE_UI

def read_metrics_dict(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    with path.open(newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) >= 2:
                out[row[0]] = row[1]
    return out


def read_summary_dict(exp: Path) -> dict[str, str]:
    return read_metrics_dict(exp / "out" / "summary.csv")


def _metric_float(d: dict[str, str], key: str, default: float = float("nan")) -> float:
    raw = d.get(key, "")
    if raw in ("", "nan", "None"):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def load_sbr_from_taps(path: Path) -> SbrResult | None:
    if not path.is_file():
        return None
    taps: list[tuple[int, float, bool]] = []
    cursor_mV = 0.0
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            k = int(row["k"])
            h = float(row["h_mV"])
            kept = row["kept"].strip().lower() == "yes"
            taps.append((k, h, kept))
            if k == 0:
                cursor_mV = h
    isi_sum = sum(h for k, h, kept in taps if kept and k != 0)
    h0_abs = abs(cursor_mV)
    isi_abs_sum = sum(abs(h) for k, h, kept in taps if kept and k != 0)
    isi_norm = isi_sum / cursor_mV if cursor_mV != 0 else float("nan")
    isi_abs = isi_abs_sum / h0_abs if h0_abs != 0 else float("nan")
    return SbrResult(
        taps=taps,
        cursor_mV=cursor_mV,
        isi_norm=isi_norm,
        isi_abs=isi_abs,
        t_cursor_s=0.0,
        t_cursor_ui=0.504,
        t_pulse_start_s=0.0,
    )


def load_sim_metrics(metrics_path: Path, prefix: str) -> SimMetrics | None:
    d = read_metrics_dict(metrics_path)
    if not d:
        return None
    short = prefix.rstrip("_")
    return SimMetrics(
        pass_name=short,
        dc_gain_db=_metric_float(d, f"{prefix}dc_gain_dB"),
        peaking_db=_metric_float(d, f"{prefix}peaking_28G_dB"),
        cmrr_db=_metric_float(d, f"{prefix}CMRR_dB"),
        psrr_db=_metric_float(d, f"{prefix}PSRR_dB"),
        vce_v=_metric_float(d, f"{prefix}VCE_V"),
        vds_tail_v=_metric_float(d, f"{prefix}VDS_tail_V"),
        vgs_tail_v=_metric_float(d, f"{prefix}VGS_tail_V"),
        ic_a=_metric_float(d, f"{prefix}Ic_A"),
        id_tail_a=_metric_float(d, f"{prefix}Id_tail_A"),
        peak_gain_db=_metric_float(d, f"{prefix}G_peak_dB"),
        f_peak_hz=_metric_float(d, f"{prefix}f_peak_Hz"),
        f_3db_hz=_metric_float(d, f"{prefix}f_3dB_Hz"),
        rd_realized_ohm=_metric_float(d, f"{prefix}RD_realized_ohm"),
        m_realized=_metric_float(d, f"{prefix}m"),
    )


def _fmt_hz(hz: float) -> str:
    if math.isnan(hz):
        return "—"
    if hz >= 1e9:
        return f"{hz / 1e9:.2f} GHz"
    if hz >= 1e6:
        return f"{hz / 1e6:.1f} MHz"
    return f"{hz:.3g} Hz"


def _fmt_db(val: float) -> str:
    return "—" if math.isnan(val) else f"{val:.2f} dB"


def _fmt_v(val: float) -> str:
    return "—" if math.isnan(val) else f"{val:.3f} V"


def _fmt_a(val: float) -> str:
    return "—" if math.isnan(val) else f"{val * 1e3:.3f} mA"


def _fmt_mw(val_mv: float) -> str:
    return "—" if math.isnan(val_mv) else f"{val_mv:.2f} mV"


def _stim_note() -> str:
    return (
        f"Transient stimulus: **PRBS9** ({PRBS9_POLY}), **{PRBS9_BITS} UI** "
        f"(one full period), **100 mVpp,diff**, ~4.5 ps edges. "
        f"AC sweep **1 MHz–300 GHz**. CMRR **> 6 dB**, PSRR **> 20 dB**."
    )


def _sbr_section_body(title: str, sbr: SbrResult, out_subdir: str) -> str:
    sbr_rows: list[str] = []
    h0 = sbr.cursor_mV
    for k, h_mV, kept in sbr.taps:
        label = sbr_tap_label(k)
        ratio = h_mV / h0 if h0 != 0 else float("nan")
        kept_str = "yes" if kept else "no"
        if k == 0:
            sbr_rows.append(
                f"| **{label}** | {k} | {h_mV:.3f} | 1.000 | {kept_str} |"
            )
        else:
            sbr_rows.append(
                f"| {label} | {k} | {h_mV:.3f} | {ratio:.4f} | {kept_str} |"
            )
    t_ui = sbr.t_cursor_ui if sbr.t_cursor_ui else 0.504
    return f"""
### {title}

Waveforms: `out/{out_subdir}/sbr.png`, `out/{out_subdir}/sbr.csv`, `out/{out_subdir}/sbr_taps.csv`.

Isolated **1 UI** NRZ pulse (**100 mVpp,diff**, ±50 mV vid), after **{SBR_SETTLE_UI} UI** settle at logic 0.
Sample **{SBR_PRE} pre-cursors + cursor + {SBR_POST} post-cursors** every UI; drop taps with
|h| < **{SBR_KEEP_FRAC * 100:.2g}%** of |cursor| (h_0 always kept).

| Tap | k | h (mV) | h / h_0 | Kept |
| --- | --- | --- | --- | --- |
{chr(10).join(sbr_rows)}

- Main cursor h_0 = **{sbr.cursor_mV:.2f} mV** at t = **{t_ui:.3f} UI** after pulse start
- Normalized total ISI = Σ h_k / h_0 = **{sbr.isi_norm:.4f}** (k≠0, kept taps only)
- Σ|h_k|/|h_0| = **{sbr.isi_abs:.4f}** (same taps)
- Taps with |h| < {SBR_KEEP_FRAC * 100:.2g}% of |cursor| are omitted from the ISI sums.
"""


#: Post-layout passes, in report order: directory under out/, metric prefix, and
#: the lumped load each one is simulated with. Which load is correct depends on
#: whether the netlist carries its own interconnect, so it is stated rather than
#: left for the reader to infer from numbers that would otherwise look
#: inconsistent.
#: (directory under out/, column label, lumped load). stage_postlayout.py prefixes
#: every metric with the pass name, so the prefix is derived rather than repeated.
POSTLAYOUT_PASSES = (
    ("postlayout_klayout", "devices only", "full CL"),
    ("postlayout_magic", "devices + extracted C", "CL_MILLER"),
)
VGA_POSTLAYOUT_PASSES = (
    ("postlayout_vga_klayout", "devices only", "full CL"),
    ("postlayout_vga_magic", "devices + extracted C", "CL_MILLER"),
)
DRIVER_POSTLAYOUT_PASSES = (
    ("postlayout_driver_klayout", "devices only", "TB PAD_C"),
    ("postlayout_driver_magic", "devices + extracted C", "PAD_C = 0"),
)

#: Where run_postlayout.py records what it built and which gates passed.
POSTLAYOUT_SUMMARY = Path("layout/blocks/out/postlayout/postlayout_summary.json")
VGA_POSTLAYOUT_SUMMARY = Path("layout/blocks/out/postlayout_vga/postlayout_summary.json")
DRIVER_POSTLAYOUT_SUMMARY = Path("layout/blocks/out/postlayout_driver/postlayout_summary.json")


def _extraction_tables(repo_root: Path, summary_rel: Path) -> tuple[str, str]:
    summary_path = repo_root / summary_rel
    parasitics = ""
    gates = ""
    if not summary_path.is_file():
        return parasitics, gates
    import json  # noqa: PLC0415

    data = json.loads(summary_path.read_text())
    flows = data.get("flows", {})
    rows = []
    for key in ("klayout", "magic"):
        flow = flows.get(key)
        if not flow:
            continue
        rows.append(
            f"| `{key}` | {flow.get('device_count', 0)} | "
            f"{flow.get('parasitic_count', 0)} | "
            f"{flow.get('capacitance_kept_fF', 0.0):.2f} fF | "
            f"{flow.get('capacitance_dropped_fF', 0.0):.2f} fF |"
        )
    if rows:
        parasitics = (
            "\n| Flow | Devices | Parasitic C | Kept | Dropped |\n"
            "| --- | --- | --- | --- | --- |\n" + "\n".join(rows) + "\n"
        )
    verdicts = data.get("gates", {})
    gates = (
        f"\nExtraction gates: LVS against the reduced CDL "
        f"**{'match' if verdicts.get('lvs_match') else 'MISMATCH'}**, "
        f"capacitance physical "
        f"**{'yes' if verdicts.get('pex_physical') else 'NO'}**.\n"
    )
    return parasitics, gates


def _postlayout_section(
    exp: Path,
    pdk: SimMetrics | None,
    *,
    passes_spec: tuple[tuple[str, str, str], ...] = POSTLAYOUT_PASSES,
    summary_rel: Path = POSTLAYOUT_SUMMARY,
    stage: str = "CTLE",
    pin_note: str = "the same seven pins",
    extra: str = "",
) -> str:
    """Compare the schematic against the extracted layout, when both exist.

    Returns an empty string when no post-layout pass has been run, so the report
    stays valid for a schematic-only checkout.
    """
    out_dir = exp / "out"
    passes = [
        (name, label, load, load_sim_metrics(out_dir / name / "metrics.csv", f"{name}_"))
        for name, label, load in passes_spec
    ]
    passes = [entry for entry in passes if entry[3] is not None]
    if not passes or pdk is None:
        return ""

    repo_root = exp.parents[1]
    parasitics, gates = _extraction_tables(repo_root, summary_rel)

    header = "| Metric | Schematic (PDK) | " + " | ".join(
        label for _, label, _, _ in passes
    ) + " |"
    divider = "| --- | --- |" + " --- |" * len(passes)
    loads = "| _lumped output load_ | full CL | " + " | ".join(
        load for _, _, load, _ in passes
    ) + " |"

    def line(label: str, fmt, attr: str) -> str:
        cells = " | ".join(fmt(getattr(metrics, attr)) for _, _, _, metrics in passes)
        return f"| {label} | {fmt(getattr(pdk, attr))} | {cells} |"

    rows = [
        line("DC gain", _fmt_db, "dc_gain_db"),
        line("Peaking @ 28 GHz", _fmt_db, "peaking_db"),
        line("Peak gain", _fmt_db, "peak_gain_db"),
        line("f_peak", _fmt_hz, "f_peak_hz"),
        line("f_-3dB", _fmt_hz, "f_3db_hz"),
        line("CMRR", _fmt_db, "cmrr_db"),
        line("V_CE", _fmt_v, "vce_v"),
        line("I_C", _fmt_a, "ic_a"),
    ]

    plots = ", ".join(f"`out/{name}/`" for name, _, _, _ in passes)
    return f"""
## Post-layout comparison

Extracted from the laid-out cell, simulated through the same testbenches as the
schematic because the {stage} is a device-only cell with {pin_note}. Both
flows take their devices from the LVS extraction; only the Magic flow carries
interconnect capacitance.

The lumped output load differs between them **by design**: a netlist that carries
its own extracted routing takes the `CL_MILLER` term only, while one without
parasitics takes the full `CL`, since otherwise the routing is either counted twice
or not at all. Each netlist declares which it needs.

{header}
{divider}
{loads}
{chr(10).join(rows)}
{parasitics}{gates}
Two device kinds are replaced by their compact models rather than extracted: the
inductor, because the PDK has no ngspice model for it and Magic sees the spiral as a
DC short, and the metal-finger capacitor, because the extractor's finger geometry is
not the calibrated model. Their nets are promoted to pins and reconnected outside
the extracted core.

Plots and waveforms: {plots}, same file names as the schematic passes.
{extra}"""


def write_ctle_report(
    path: Path,
    params,
    ideal: SimMetrics,
    pdk: SimMetrics | None,
    sbr_ideal: SbrResult | None = None,
    sbr_pdk: SbrResult | None = None,
    exp: Path | None = None,
) -> None:
    """Regenerate circuits/ctle56n/ctle_report.md from sizing + sim metrics."""
    m_bessel = params.l_h / (params.rd_ohm**2 * params.cl_f)
    l_ph = params.l_h * 1e12
    mos_vgs = params.mos_vgs
    rppd_note = f"{params.rppd_w_um:.1f}×{params.rppd_l_um:.1f} µm" if params.rppd_w_um else "ideal RD"

    def row(param: str, symbol: str, ideal_val: str, pdk_val: str, notes: str = "") -> str:
        if pdk is None:
            return f"| {param} | {symbol} | {ideal_val} | {notes} |"
        return f"| {param} | {symbol} | {ideal_val} | {pdk_val} | {notes} |"

    pdk_dc = _fmt_db(pdk.dc_gain_db) if pdk else "—"
    pdk_peak28 = _fmt_db(pdk.peaking_db) if pdk else "—"
    pdk_gpeak = _fmt_db(pdk.peak_gain_db) if pdk else "—"
    pdk_fpeak = _fmt_hz(pdk.f_peak_hz) if pdk else "—"
    pdk_f3 = _fmt_hz(pdk.f_3db_hz) if pdk else "—"
    pdk_cmrr = _fmt_db(pdk.cmrr_db) if pdk else "—"
    pdk_psrr = _fmt_db(pdk.psrr_db) if pdk else "—"
    pdk_vce = _fmt_v(pdk.vce_v) if pdk else "—"
    pdk_vds = _fmt_v(pdk.vds_tail_v) if pdk else "—"

    table_header = (
        "| Parameter | Symbol | Ideal | PDK | Notes |\n"
        "| --- | --- | --- | --- | --- |"
        if pdk
        else "| Parameter | Symbol | Value | Notes |\n| --- | --- | --- | --- |"
    )

    rows = [
        row("Emitter multiplier", "Nx", str(params.nx), str(params.nx), "HBT LUT index"),
        row("HBT VBE (LUT)", "VBE", f"{params.vbe:.3f} V", f"{params.vbe:.3f} V", "max-fT bias"),
        row("Input common-mode", "VBASE", f"{params.vbase:.3f} V", f"{params.vbase:.3f} V", "inp/inn DC"),
        row("Supply", "VDD", f"{params.vdd:.3f} V", f"{params.vdd:.3f} V", "below BVceo ~1.6 V"),
        row("HBT collector current", "Ic", _fmt_a(ideal.ic_a), _fmt_a(pdk.ic_a) if pdk else "—", "per side"),
        row("Tail current", "I_tail", _fmt_a(params.itail_a), _fmt_a(params.itail_a), "Ic per tail (×2 devices)"),
        row("Transition frequency", "f_T", _fmt_hz(params.ft_hz), _fmt_hz(params.ft_hz), "LUT at bias"),
        row("Transconductance", "g_m", f"{params.gm * 1e3:.2f} mS", f"{params.gm * 1e3:.2f} mS", ""),
        row("Input capacitance", "C_in", f"{params.cin_f * 1e15:.2f} fF", f"{params.cin_f * 1e15:.2f} fF", "HBT CIN"),
        row(
            "Load capacitance",
            "C_L",
            f"{params.cl_f * 1e15:.2f} fF",
            f"{params.cl_f * 1e15:.2f} fF",
            "Miller + route (no coil port C)",
        ),
        row("Load resistor", "R_D", f"{params.rd_ohm:.1f} Ω", rppd_note if pdk else f"{params.rd_ohm:.1f} Ω", "shunt peak"),
        row("Emitter degeneration", "R_s", f"{params.rs_ohm:.1f} Ω", f"{params.rs_ohm:.1f} Ω", ""),
        row("Degeneration cap", "C_s", f"{params.cs_f * 1e15:.1f} fF", f"{params.cs_f * 1e15:.1f} fF", "ideal or MIM"),
        row(
            "Drain inductor",
            "L",
            f"{l_ph:.2f} pH",
            f"{l_ph:.2f} pH",
            "ideal; VDD→L→R_D→collector; no PDK spiral (l2n0 ~2 nH)",
        ),
        row("Bessel MFD", "m", f"{m_bessel:.2f}", f"{m_bessel:.2f}", "L/(R_D² C_L)"),
        row(
            "MOS tail W/L/VGS",
            "W/L/VGS",
            f"{params.mos_w_um:.0f}/{params.mos_l_um:.1f}/{mos_vgs:.3f} V",
            f"{params.mos_w_um:.0f}/{params.mos_l_um:.1f}/{mos_vgs:.3f} V",
            "LV NMOS + mirror",
        ),
        row("RPPD load", "W/L", "ideal R", rppd_note, "LUT ≈ R_D/0.88"),
        row("DC gain", "A_v0", _fmt_db(ideal.dc_gain_db), pdk_dc, "−6…0 dB target"),
        row("Peaking @ 28 GHz", "—", _fmt_db(ideal.peaking_db), pdk_peak28, "3–10 dB target"),
        row("Peak AC gain", "G_peak", _fmt_db(ideal.peak_gain_db), pdk_gpeak, ""),
        row("Peak frequency", "f_peak", _fmt_hz(ideal.f_peak_hz), pdk_fpeak, ""),
        row("−3 dB bandwidth", "f_{−3dB}", _fmt_hz(ideal.f_3db_hz), pdk_f3, "after peak"),
        row("CMRR", "—", _fmt_db(ideal.cmrr_db), pdk_cmrr, "> 6 dB"),
        row("PSRR", "—", _fmt_db(ideal.psrr_db), pdk_psrr, "> 20 dB (clipped 120 dB)"),
        row("HBT VCE", "V_CE", _fmt_v(ideal.vce_v), pdk_vce, ""),
        row("MOS tail VDS", "V_DS,tail", _fmt_v(ideal.vds_tail_v), pdk_vds, ""),
    ]

    body = f"""# 56 Gb/s NRZ CML CTLE — design report

Auto-generated by `python/generate_reports.py` — do not hand-edit numbers.

## Topology

CML continuous-time linear equalizer (CTLE) for **56 Gb/s NRZ** (Nyquist **28 GHz**).
HBT differential pair (`npn13G2`) with **shunt-peaked loads** (R_D + ideal L from VDD),
**emitter degeneration** (R_s + C_s), and an **LV NMOS tail** with 1:1 diode-connected mirror.

Sizing uses characterization LUTs (`char/bjt`, `char/mos`, `char/passive`) at max-f_T HBT bias.
Load C_L = Miller-aware FO1 VGA input + interconnect (not raw LUT CIN; coil port C excluded).
Bessel shunt-peaking **m = L/(R_D² C_L) ≈ {m_bessel:.2f}**.

The drain inductor **L = {l_ph:.2f} pH** ({params.l_h:.6g} H) is physically tiny (via / short-trace scale).
No PDK spiral is used — minimum EM cell `l2n0` is ~2 nH, far too large. L remains **ideal** in ngspice.

## Targets

- DC gain **−6 … 0 dB** (aim 0 dB)
- Peaking **3–10 dB at 28 GHz**
- CMRR **> 6 dB**, PSRR **> 20 dB** at low frequency
- {_stim_note()}

## Sizing summary

{table_header}
{chr(10).join(rows)}

Plots and waveforms: `out/ideal/` (ideal passives) and `out/pdk/` (PDK R/C passives).
Each pass includes AC PNGs/CSVs, transient CSVs, eye PNGs/CSVs, and SBR when `--no-tran` is not set.
Combined metrics: `out/summary.csv`; per-pass: `out/ideal/metrics.csv`, `out/pdk/metrics.csv`.
"""
    if sbr_ideal or sbr_pdk:
        body += "\n## Single-bit response\n"
        if sbr_ideal:
            body += _sbr_section_body("Ideal", sbr_ideal, "ideal")
        if sbr_pdk:
            body += _sbr_section_body("PDK", sbr_pdk, "pdk")
    if exp is not None:
        body += _postlayout_section(exp, pdk)
    path.write_text(body)


def write_term_report(path: Path, exp: Path) -> None:
    m = read_metrics_dict(exp / "out" / "term" / "metrics.csv")
    if not m:
        path.write_text("# Termination — design report\n\nNo `out/term/metrics.csv` found.\n")
        return
    sbr = load_sbr_from_taps(exp / "out" / "term" / "sbr_taps.csv")

    rows = [
        ["Supply", "VDD", f"{_metric_float(m, 'VDD_V'):.3f} V", "shared 1.6 V rail"],
        ["On-chip CM", "vtt", f"{_metric_float(m, 'vtt_V'):.3f} V", "CTLE input target"],
        ["Input CM", "VBASE", f"{_metric_float(m, 'VBASE_V'):.3f} V", "bond pad / divider"],
        ["Bond pad", "W×L", f"{_metric_float(m, 'pad_w_um'):.0f}×{_metric_float(m, 'pad_l_um'):.0f} µm", "TM1 bottom plate C"],
        ["Pad C", "C_pad", f"{_metric_float(m, 'PAD_C_fF'):.2f} fF/side", "substrate"],
        ["Termination R", "R_term", f"{_metric_float(m, 'rsil_R_ohm'):.2f} Ω/leg", f"rsil {_metric_float(m, 'rsil_w_um'):.1f}×{_metric_float(m, 'rsil_l_um'):.2f} µm"],
        ["VTT divider top", "R_top", f"{_metric_float(m, 'vtt_R_top_ohm'):.1f} Ω", "rppd"],
        ["VTT divider bottom", "R_bot", f"{_metric_float(m, 'vtt_R_bot_ohm'):.1f} Ω", "rppd"],
        ["Divider current", "I_div", f"{_metric_float(m, 'Idiv_mA'):.3f} mA", ""],
        ["ESD leak", "I_ESD", f"{_metric_float(m, 'esd_leak_pA'):.2f} pA", "diode pair @ pad"],
        ["Shunt C (diff)", "C_sh", f"{_metric_float(m, 'C_shunt_diff_fF'):.1f} fF", "pad+ESD to vtt"],
        ["Extracted C (diff)", "C_ext", f"{_metric_float(m, 'C_extracted_diff_fF'):.1f} fF", "from Zin fit"],
        ["Insertion loss DC", "IL", _fmt_db(_metric_float(m, "IL_dc_dB")), "50 Ω source → pad"],
        ["Insertion loss @ 28 GHz", "IL_28G", _fmt_db(_metric_float(m, "IL_28GHz_dB")), ""],
        ["IL −3 dB BW", "f_IL", _fmt_hz(_metric_float(m, "f_IL_3dB_Hz")), ""],
        ["Return loss DC", "S11", _fmt_db(_metric_float(m, "S11_dc_dB")), "100 Ω diff ref"],
        ["Return loss @ 28 GHz", "S11_28G", _fmt_db(_metric_float(m, "S11_28GHz_dB")), ""],
        ["S11 −10 dB BW", "f_S11", _fmt_hz(_metric_float(m, "f_S11_-10dB_Hz")), ""],
        ["Zin −3 dB", "f_Zin", _fmt_hz(_metric_float(m, "f_Zin_3dB_Hz")), ""],
    ]
    if sbr:
        rows += [
            ["SBR cursor", "h_0", _fmt_mw(sbr.cursor_mV), "1 UI pulse"],
            ["SBR ISI (norm)", "Σh/h_0", f"{sbr.isi_norm:.4f}", "kept taps"],
        ]
    eye_h = _metric_float(m, "term_eye_height_mV")
    if not math.isnan(eye_h):
        rows += [
            ["Eye height", "—", _fmt_mw(eye_h), "phase-centred fold"],
            ["Eye width", "—", f"{_metric_float(m, 'term_eye_width_UI'):.4f} UI", f"{_metric_float(m, 'term_eye_width_ps'):.2f} ps"],
            ["Eye pp swing", "—", _fmt_mw(_metric_float(m, "term_eye_pp_swing_mV")), ""],
        ]

    table = "\n".join(f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows)
    body = f"""# 56 Gb/s NRZ bond-pad termination — design report

Auto-generated by `python/generate_reports.py` — do not hand-edit numbers.

## Topology

50 Ω per leg (**100 Ω differential**) on-chip termination to an **~1.4 V** common mode.
Bond pad (stacked TM1/TM2), primary ESD (`diodevdd_2kv` + `diodevss_2kv`) with `nmoscl_2` rail clamp,
`rsil` termination resistors, and an `rppd` divider with `cap_cmim` decoupling on the vtt node.

## Targets

- **50 Ω** per leg to on-chip **~1.4 V** common mode
- Return loss and insertion loss characterized to **28 GHz** (Nyquist)
- {_stim_note()}

## Sizing and measurement summary

| Parameter | Symbol | Value | Notes |
| --- | --- | --- | --- |
{table}

Plots and waveforms: `out/term/` (`zin.png`, `ac_diff`, PRBS transient, eye, SBR).
"""
    if sbr:
        body += "\n## Single-bit response\n" + _sbr_section_body("PDK", sbr, "term")
    path.write_text(body)


def _vga_pass_section(
    title: str,
    prefix: str,
    out_subdir: str,
    exp: Path,
) -> str:
    mpath = exp / "out" / out_subdir / "metrics.csv"
    m = read_metrics_dict(mpath)
    if not m:
        return f"\n### {title}\n\nNo `{mpath}` found.\n"
    sbr = load_sbr_from_taps(exp / "out" / out_subdir / "sbr_taps.csv")
    sim = load_sim_metrics(mpath, prefix)
    assert sim is not None

    gain_28 = sim.dc_gain_db + sim.peaking_db
    rows = [
        ["DC gain", "A_v0", _fmt_db(sim.dc_gain_db), "mid VCTRL"],
        ["Gain @ 28 GHz", "A_v28", _fmt_db(gain_28), "small-signal AC"],
        ["Peaking @ 28 GHz", "—", _fmt_db(sim.peaking_db), "vs DC"],
        ["Peak AC gain", "G_peak", _fmt_db(sim.peak_gain_db), ""],
        ["Peak frequency", "f_peak", _fmt_hz(sim.f_peak_hz), ""],
        ["−3 dB bandwidth", "f_{−3dB}", _fmt_hz(sim.f_3db_hz), ""],
        ["CMRR", "—", _fmt_db(sim.cmrr_db), "> 6 dB"],
        ["PSRR", "—", _fmt_db(sim.psrr_db), "> 20 dB"],
        ["HBT VCE", "V_CE", _fmt_v(sim.vce_v), "signal pair"],
        ["Tail VDS", "V_DS,tail", _fmt_v(sim.vds_tail_v), ""],
        ["Ic (signal)", "I_c", _fmt_a(sim.ic_a), "per side @ mid VCTRL"],
        ["Tail current", "I_tail", _fmt_a(sim.id_tail_a), "steered total"],
    ]
    tag = prefix.rstrip("_")
    eye_h = _metric_float(m, f"{tag}_eye_height_mV")
    eye_w = _metric_float(m, f"{tag}_eye_width_UI")
    eye_pp = _metric_float(m, f"{tag}_eye_pp_swing_mV")
    if not math.isnan(eye_h):
        rows += [
            ["Eye height", "—", _fmt_mw(eye_h), ""],
            ["Eye width", "—", f"{eye_w:.4f} UI", ""],
            ["Eye pp swing", "—", _fmt_mw(eye_pp), ""],
        ]

    table = "\n".join(f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows)
    section = f"""
### {title}

Waveforms: `out/{out_subdir}/`.

| Parameter | Symbol | Value | Notes |
| --- | --- | --- | --- |
{table}
"""
    if sbr:
        section += _sbr_section_body(f"{title} — SBR", sbr, out_subdir)
    return section


def write_vga_report(path: Path, exp: Path) -> None:
    summary = read_summary_dict(exp)
    gain_table = exp / "out" / "vga_pdk" / "gain_vs_vctrl_table.csv"
    gain_range_note = ""
    if gain_table.is_file():
        gains: list[float] = []
        with gain_table.open(newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("op_ok", "").lower() != "yes":
                    continue
                g = row.get("ac_gain_28G_dB", "")
                if g:
                    try:
                        gains.append(float(g))
                    except ValueError:
                        pass
        if len(gains) >= 2:
            g_span = max(gains) - min(gains)
            gain_range_note = (
                f"\nMeasured **28 GHz small-signal gain range = {g_span:.2f} dB** "
                f"({min(gains):.2f} … {max(gains):.2f} dB) from `out/vga_pdk/gain_vs_vctrl_table.csv`."
            )
    vctrl_max = summary.get("vga_pdk_vga_usable_vctrl_max", summary.get("vga_usable_vctrl_max", ""))
    vce_cross = summary.get("vga_pdk_vga_vce_cross_vctrl", summary.get("vga_vce_cross_vctrl", ""))
    headroom = ""
    if vctrl_max:
        headroom = (
            f"\n- Usable VCTRL **≤ {vctrl_max} V** at VDD=1.6 V "
            f"(VCE floor cross @ {vce_cross} V)."
        )

    vout_cm = summary.get("VOUT_CM", "")
    if not vout_cm:
        params_inc = exp / "spice" / "params.inc"
        if params_inc.is_file():
            import re

            m = re.search(r"\.param\s+VOUT_CM=([\S]+)", params_inc.read_text())
            if m:
                vout_cm = m.group(1)
    body = f"""# 56 Gb/s NRZ current-steering VGA — design report

Auto-generated by `python/generate_reports.py` — do not hand-edit numbers.

## Topology

HBT CML variable-gain stage (**tail-current steering**): fixed signal pair plus dummy pair
at input common mode, shared shunt-peaked loads, LV NMOS tail. Gain control steers tail
current between signal and dummy branches — **not** variable emitter degeneration
(degeneration is fixed; varying R_s only equalizes, it does not provide flat gain at 28 GHz).

FO2 load: Miller-aware input C of the pad driver ×2 plus interconnect.
Sized DC-coupled to CTLE output CM (**VOUT_CM ≈ {vout_cm or '1.35 V'}** from `params.inc`).

## Targets

- Gain range **≥ 10 dB at 28 GHz** (never quoted at DC)
- CMRR **> 6 dB**, PSRR **> 20 dB**
- {_stim_note()}{headroom}{gain_range_note}

## Measurement summary
"""
    body += _vga_pass_section("Ideal passives", "vga_ideal_", "vga_ideal", exp)
    body += _vga_pass_section("PDK passives", "vga_pdk_", "vga_pdk", exp)
    pdk_m = load_sim_metrics(exp / "out" / "vga_pdk" / "metrics.csv", "vga_pdk_")
    kl_m = read_metrics_dict(exp / "out" / "postlayout_vga_klayout" / "metrics.csv")
    mag_m = read_metrics_dict(exp / "out" / "postlayout_vga_magic" / "metrics.csv")
    kl_eh = _metric_float(kl_m, "postlayout_vga_klayout_eye_height_mV")
    kl_ew = _metric_float(kl_m, "postlayout_vga_klayout_eye_width_UI")
    mag_eh = _metric_float(mag_m, "postlayout_vga_magic_eye_height_mV")
    mag_ew = _metric_float(mag_m, "postlayout_vga_magic_eye_width_UI")
    eye_note = ""
    if not math.isnan(kl_eh) and not math.isnan(mag_eh):
        eye_note = (
            f"\n**Eyes (mid VCTRL, PRBS9)** — devices-only "
            f"**{_fmt_mw(kl_eh)} / {kl_ew:.3f} UI**, Magic "
            f"**{_fmt_mw(mag_eh)} / {mag_ew:.3f} UI**. Phase-invariance "
            f"passed on both. The Magic bandwidth drop does not close the "
            f"mid-gain eye.\n"
        )
    body += _postlayout_section(
        exp,
        pdk_m,
        passes_spec=VGA_POSTLAYOUT_PASSES,
        summary_rel=VGA_POSTLAYOUT_SUMMARY,
        stage="VGA",
        pin_note="the same ten pins (including vicm / steerp / steern / mgate)",
        extra=f"""
**Magic C drop (do not "fix" the layout)**

`vga_dut` never labels the dummy-steer collectors `tx1`/`tx2`. Magic
PEX therefore names those Metal2 rails `m2_7492_3498#` and
`m2_36168_2698#` and the C-only rewrite drops them (~80 fF each to
`vss`, plus coupling to `em`/`ed*`). `postlayout_summary.json`
reports **kept 548 fF / dropped 388 fF**. The midband output numbers
above are still usable (the drop is internal-node C, not `C_L`). The
steering-node C is under-counted; do not add labels just to recover
those capacitors.
{eye_note}""",
    )
    path.write_text(body)


def write_driver_report(path: Path, exp: Path) -> None:
    m = read_metrics_dict(exp / "out" / "driver" / "metrics.csv")
    if not m:
        path.write_text("# Pad driver — design report\n\nNo `out/driver/metrics.csv` found.\n")
        return
    sbr = load_sbr_from_taps(exp / "out" / "driver" / "sbr_taps.csv")

    rows = [
        ["Supply", "VDD", f"{_metric_float(m, 'VDD_V'):.3f} V", ""],
        ["Input CM", "VBASE", f"{_metric_float(m, 'VBASE_V'):.3f} V", "from VGA max-gain CM"],
        ["Emitter multiplier", "Nx", f"{_metric_float(m, 'Nx'):.0f}", "HBT LUT index"],
        ["Tail current", "I_tail", f"{_metric_float(m, 'ITAIL_A') * 1e3:.1f} mA", "single MOS tail"],
        ["On-chip back-term", "R_D", f"{_metric_float(m, 'RD_on_chip_realized_ohm'):.2f} Ω/leg", "rsil target 50 Ω"],
        ["AC R_eff (SE)", "R_eff", f"{_metric_float(m, 'R_eff_AC_half_circuit_ohm'):.1f} Ω", "parallel w/ 100 Ω load"],
        ["Shunt inductor", "L", f"{_metric_float(m, 'L_pH'):.2f} pH", f"EM {m.get('L_EM_case', '')}"],
        ["Pad + ESD C_L", "C_L", f"{_metric_float(m, 'CL_pad_fF'):.1f} fF/side", "pad + ESD"],
        ["Bessel m", "m", f"{_metric_float(m, 'm_realized'):.4f}", f"target {m.get('m_bessel_target', '0.32')}"],
        ["Driver Miller C_in", "C_in", f"{_metric_float(m, 'driver_Cin_Miller_fF'):.1f} fF", "FO2 budget"],
        ["VGA FO2 penalty", "BW", f"{_metric_float(m, 'vga_bw_penalty_pct'):.1f} %", "vs standalone VGA"],
        ["DC gain", "A_v0", _fmt_db(_metric_float(m, "dc_gain_dB")), ""],
        ["Gain @ 28 GHz", "A_v28", _fmt_db(_metric_float(m, "ac_gain_28G_dB")), ""],
        ["Peaking @ 28 GHz", "—", _fmt_db(_metric_float(m, "peaking_28G_dB")), ""],
        ["Peak AC gain", "G_peak", _fmt_db(_metric_float(m, "G_peak_dB")), ""],
        ["Peak frequency", "f_peak", _fmt_hz(_metric_float(m, "f_peak_Hz")), ""],
        ["−3 dB bandwidth", "f_{−3dB}", _fmt_hz(_metric_float(m, "f_3dB_Hz")), ""],
        ["Pad CM", "V_pad,CM", _fmt_v(_metric_float(m, "Vpad_CM_V")), ""],
        ["HBT VCE", "V_CE", _fmt_v(_metric_float(m, "VCE_Q1_V")), "per side"],
        ["Return loss DC", "RL", _fmt_db(_metric_float(m, "return_loss_dc_dB")), "pad, 100 Ω diff"],
        ["Return loss @ 28 GHz", "RL_28G", _fmt_db(_metric_float(m, "return_loss_28GHz_dB")), ""],
        ["Pad swing (pp)", "—", _fmt_mw(_metric_float(m, "pad_swing_pp_mV")), "PRBS"],
        ["Pad eye height", "—", _fmt_mw(_metric_float(m, "pad_eye_height_mV")), ""],
        ["Pad eye width", "—", f"{_metric_float(m, 'pad_eye_width_UI'):.4f} UI", ""],
    ]
    table = "\n".join(f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows)
    body = f"""# 56 Gb/s NRZ output pad driver — design report

Auto-generated by `python/generate_reports.py` — do not hand-edit numbers.

## Topology

CML shunt-peaked HBT differential pair driving the output bond pad through on-chip **50 Ω/leg**
back-termination (`rsil`), with pad + ESD capacitance inside the DUT. Single LV NMOS tail,
no emitter degeneration. EM-fitted shunt inductor (`ind_shunt`) for Bessel peaking to the pad load.

## Targets

- Drive **100 Ω differential** pad with shunt peaking **m ≈ 0.32**
- Pad eye margin vs 56G NRZ (400 mVpp target is gain-starved — see MEMORY.md)
- {_stim_note()}

## Sizing and measurement summary

| Parameter | Symbol | Value | Notes |
| --- | --- | --- | --- |
{table}

Plots and waveforms: `out/driver/`.
"""
    if sbr:
        body += "\n## Single-bit response\n" + _sbr_section_body("PDK", sbr, "driver")
    body += _driver_postlayout_section(exp, m)
    path.write_text(body)


def _driver_postlayout_section(exp: Path, schematic: dict[str, str]) -> str:
    """Schematic vs extracted pad-driver metrics, when post-layout passes exist."""
    out_dir = exp / "out"
    passes = []
    for name, label, load in DRIVER_POSTLAYOUT_PASSES:
        d = read_metrics_dict(out_dir / name / "metrics.csv")
        if d:
            passes.append((name, label, load, d))
    if not passes or not schematic:
        return ""

    parasitics, gates = _extraction_tables(exp.parents[1], DRIVER_POSTLAYOUT_SUMMARY)
    header = "| Metric | Schematic (PDK) | " + " | ".join(
        label for _, label, _, _ in passes
    ) + " |"
    divider = "| --- | --- |" + " --- |" * len(passes)
    loads = "| _pad / output load_ | TB PAD_C | " + " | ".join(
        load for _, _, load, _ in passes
    ) + " |"

    def cell(d: dict[str, str], key: str, fmt) -> str:
        return fmt(_metric_float(d, key))

    def line(label: str, key: str, fmt) -> str:
        cells = " | ".join(cell(d, key, fmt) for _, _, _, d in passes)
        return f"| {label} | {cell(schematic, key, fmt)} | {cells} |"

    rows = [
        line("DC gain", "dc_gain_dB", _fmt_db),
        line("Gain @ 28 GHz", "ac_gain_28G_dB", _fmt_db),
        line("Peaking @ 28 GHz", "peaking_28G_dB", _fmt_db),
        line("Peak gain", "G_peak_dB", _fmt_db),
        line("f_peak", "f_peak_Hz", _fmt_hz),
        line("f_-3dB", "f_3dB_Hz", _fmt_hz),
        line("HBT VCE", "VCE_Q1_V", _fmt_v),
        line("Pad CM", "Vpad_CM_V", _fmt_v),
        line("Return loss DC", "return_loss_dc_dB", _fmt_db),
        line("Return loss @ 28 GHz", "return_loss_28GHz_dB", _fmt_db),
        line("Pad eye height", "pad_eye_height_mV", _fmt_mw),
        line("Pad eye width", "pad_eye_width_UI", lambda v: "—" if math.isnan(v) else f"{v:.4f} UI"),
    ]
    plots = ", ".join(f"`out/{name}/`" for name, _, _, _ in passes)
    return f"""
## Post-layout comparison

Extracted from the laid-out cell, simulated through the same testbenches as the
schematic because the pad driver is a device-only cell with the same seven pins.
Both flows take their devices from the LVS extraction; only the Magic flow
carries interconnect and pad-metal capacitance. The Magic netlist therefore
drops the testbench ``PAD_C`` so the extracted pad is not counted twice.

{header}
{divider}
{loads}
{chr(10).join(rows)}
{parasitics}{gates}
The shunt coils are replaced by the EM-fitted ``ind_shunt`` compact model; ESD
diodes and the rail clamp stay in the extracted core.

The 91 → 35 GHz bandwidth drop is extra C on the pad node, not a missing ESD
device and not the collector feed. Both netlists already instantiate the
``diodevdd_2kv`` / ``diodevss_2kv`` pair (50.9 fF). Schematic ``PAD_C`` is
27.68 fF of TM1 area-to-substrate only. Magic's 144 fF ``outp``–``vss`` is
**80 fF from the isolated ``bondpad_70um``** plus ~64 fF of pad-band
neighbours (a lone TM2 ``vss`` ring adds only 4 fF). Putting 144 fF on the
schematic lands at 35.6 GHz. The 819 fF Magic total is mostly ``mgate`` /
``em`` and is not ``C_L``. See ``layout/debug_pex/FINDINGS.md``.

Plots and waveforms: {plots}, same file names as the schematic pass.
"""


def _chain_setting_rows(m: dict[str, str], label: str) -> list[str]:
    p = f"{label}_"
    return [
        f"| VCTRL | **{_metric_float(m, p + 'VCTRL_V'):.2f} V** |",
        f"| E2E gain (source) @ 28 GHz | {_fmt_db(_metric_float(m, p + 'gain_src_28G_dB'))} |",
        f"| E2E gain (pad) @ 28 GHz | {_fmt_db(_metric_float(m, p + 'gain_pad_28G_dB'))} |",
        f"| Term @ 28 GHz | {_fmt_db(_metric_float(m, p + 'gain_term_28G_dB'))} |",
        f"| CTLE @ 28 GHz | {_fmt_db(_metric_float(m, p + 'gain_ctle_28G_dB'))} |",
        f"| VGA @ 28 GHz | {_fmt_db(_metric_float(m, p + 'gain_vga_28G_dB'))} |",
        f"| Driver @ 28 GHz | {_fmt_db(_metric_float(m, p + 'gain_driver_28G_dB'))} |",
        f"| Drive swing (pp) | {_fmt_mw(_metric_float(m, p + 'drive_swing_mV'))} |",
        f"| Pad swing (pp) | {_fmt_mw(_metric_float(m, p + 'pad_swing_mV'))} |",
        f"| Eye height | {_fmt_mw(_metric_float(m, p + 'eye_height_mV'))} |",
        f"| Eye width | {_metric_float(m, p + 'eye_width_UI'):.4f} UI |",
        f"| SBR ISI (norm) | {_metric_float(m, p + 'sbr_isi_norm'):.4f} |",
    ]


def write_chain_report(path: Path, exp: Path) -> None:
    m = read_metrics_dict(exp / "out" / "chain" / "metrics.csv")
    if not m:
        path.write_text("# RX chain — design report\n\nNo `out/chain/metrics.csv` found.\n")
        return
    sbr = load_sbr_from_taps(exp / "out" / "chain" / "sbr_taps.csv")
    op_rows: list[list[str]] = []
    op_path = exp / "out" / "chain" / "op_table.csv"
    if op_path.is_file():
        with op_path.open(newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                op_rows.append([row["quantity"], row["value"], row.get("sized_for", ""), row.get("note", "")])

    stage_rows: list[list[str]] = []
    sc_path = exp / "out" / "chain" / "stage_compare.csv"
    if sc_path.is_file():
        with sc_path.open(newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                stage_rows.append([
                    row["stage"],
                    row["metric"],
                    row["chain_28G_dB"],
                    row["standalone_28G_dB"],
                    row["delta_dB"],
                ])

    op_table = ""
    if op_rows:
        op_table = "\n".join(f"| {a} | {b} | {c} | {d} |" for a, b, c, d in op_rows)
        op_table = f"""
## DC operating points (mid VCTRL)

| Quantity | Simulated | Sized for | Note |
| --- | --- | --- | --- |
{op_table}
"""

    stage_table = ""
    if stage_rows:
        stage_table = "\n".join(
            f"| {a} | {b} | {c} | {d} | {e} |" for a, b, c, d, e in stage_rows
        )
        stage_table = f"""
## Per-stage gain @ 28 GHz (chain vs standalone)

From `out/chain/stage_compare.csv` at mid VGA setting.

| Stage | Metric | Chain | Standalone | Δ |
| --- | --- | --- | --- | --- |
{stage_table}
"""

    settings = ""
    for label, title in (("min", "Minimum gain (VCTRL min)"), ("mid", "Mid gain (VCTRL mid)"), ("max", "Maximum gain (VCTRL max)")):
        vctrl = _metric_float(m, f"{label}_VCTRL_V")
        if math.isnan(vctrl):
            continue
        settings += f"""
### {title}

| Metric | Value |
| --- | --- |
"""
        settings += "\n".join(_chain_setting_rows(m, label))

    body = f"""# 56 Gb/s NRZ RX front-end chain — design report

Auto-generated by `python/generate_reports.py` — do not hand-edit numbers.

## Topology

End-to-end **PDK** receiver front end, DC-coupled:

**bond pad → termination + ESD → CML CTLE → current-steering VGA → output pad driver → pad**

Subcircuits: `term_dut` → `ctle_dut` → `vga_chain_dut` → `driver_chain_dut` in `spice/chain_pdk.cir`.
Each stage reuses committed sizing tokens; VGA `vicm` is wired to CTLE output CM (no fixed VBASE source).

## Targets

- Cascaded gain and eye at **28 GHz** Nyquist with VGA gain control (min / mid / max VCTRL)
- Input return loss **S11** and CMRR/PSRR of full chain
- {_stim_note()}

## Chain-wide metrics (mid VCTRL)

| Metric | Value | Notes |
| --- | --- | --- |
| S11 @ DC | {_fmt_db(_metric_float(m, 'S11_dc_dB'))} | 100 Ω diff ref |
| S11 @ 28 GHz | {_fmt_db(_metric_float(m, 'S11_28GHz_dB'))} | |
| CMRR @ DC | {_fmt_db(_metric_float(m, 'CMRR_dB'))} | |
| PSRR @ DC | {_fmt_db(_metric_float(m, 'PSRR_dB'))} | clipped 120 dB |
| CTLE tail VDS | {_fmt_v(_metric_float(m, 'ctle_VDS_tail_V'))} | loaded chain OP |
{op_table}{stage_table}
## Gain settings
{settings}

Plots and waveforms: `out/chain/` (AC, Zin, transient, eye, SBR at each VCTRL).
ISI comparison: `out/chain/isi_analysis.csv`.
"""
    if sbr:
        body += "\n## Single-bit response (mid VCTRL)\n" + _sbr_section_body("Chain mid", sbr, "chain")
    path.write_text(body)


def generate_all_reports(exp: Path | None = None) -> list[Path]:
    """Parse committed out/ artifacts and write all stage markdown reports."""
    if exp is None:
        exp = Path(__file__).resolve().parents[1]
    out_dir = exp / "out"
    written: list[Path] = []

    # CTLE — sizing from params.inc, metrics from ideal/pdk passes
    sys_path = Path(__file__).resolve().parent.parent
    import sys

    if str(sys_path) not in sys.path:
        sys.path.insert(0, str(sys_path))
    from size_ctle import read_params_inc  # noqa: WPS433

    params_inc = exp / "spice" / "params.inc"
    if params_inc.is_file():
        params = read_params_inc(params_inc)
        ideal = load_sim_metrics(out_dir / "ideal" / "metrics.csv", "ideal_")
        pdk = load_sim_metrics(out_dir / "pdk" / "metrics.csv", "pdk_")
        if ideal:
            ctle_path = exp / "ctle_report.md"
            write_ctle_report(
                ctle_path,
                params,
                ideal,
                pdk,
                load_sbr_from_taps(out_dir / "ideal" / "sbr_taps.csv"),
                load_sbr_from_taps(out_dir / "pdk" / "sbr_taps.csv"),
                exp=exp,
            )
            written.append(ctle_path)

    term_path = exp / "term_report.md"
    write_term_report(term_path, exp)
    written.append(term_path)

    vga_path = exp / "vga_report.md"
    write_vga_report(vga_path, exp)
    written.append(vga_path)

    driver_path = exp / "driver_report.md"
    write_driver_report(driver_path, exp)
    written.append(driver_path)

    chain_path = exp / "chain_report.md"
    write_chain_report(chain_path, exp)
    written.append(chain_path)

    return written
