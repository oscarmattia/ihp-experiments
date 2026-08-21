#!/usr/bin/env bash
# Regenerate the whole layout flow and report every gate.
#
# Each stage writes JSON summaries next to its artifacts, so a failure is a
# structured result rather than a log to read:
#   layout/devices/out/{manifest,drc_summary,lvs_summary,pex_summary}.json
#   layout/blocks/out/blocks_summary.json
#   layout/blocks/out/ctle_stage/ctle_stage_summary.json
#   layout/blocks/out/vga_stage/vga_dut_summary.json
#   layout/blocks/out/driver_stage/driver_dut_summary.json
#
# Usage:
#   ./layout/run_all.sh
#   ./layout/run_all.sh --quick     # skip PEX, stages, and post-layout netlists
#   ./layout/run_all.sh --no-render
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
IHP_EDA_ROOT="${IHP_EDA_ROOT:-$HOME/.local/share/ihp-eda}"

QUICK=0
RENDER_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --quick) QUICK=1 ;;
    --no-render) RENDER_ARGS+=(--no-render) ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

if [[ -f "$IHP_EDA_ROOT/env.sh" ]]; then
  # shellcheck disable=SC1091
  source "$IHP_EDA_ROOT/env.sh"
fi

PY="${IHP_PYTHON:-$IHP_EDA_ROOT/venv/bin/python}"
export IHP_PYTHON="$PY"
# Rendering goes through the KLayout application, which wants a Qt platform even
# with no display.
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"

cd "$REPO_ROOT" || exit 1

FAILED=()

step() {
  local label="$1"; shift
  printf '\n== %s\n' "$label"
  if "$@"; then
    return 0
  fi
  printf '   -> %s reported failures\n' "$label"
  FAILED+=("$label")
  return 0
}

step "devices: generate"  "$PY" layout/devices/gen_devices.py "${RENDER_ARGS[@]+"${RENDER_ARGS[@]}"}"
step "devices: DRC"       "$PY" layout/devices/run_drc.py
step "devices: LVS"       "$PY" layout/devices/run_lvs.py
if [[ "$QUICK" -eq 0 ]]; then
  step "devices: PEX"     "$PY" layout/devices/run_pex.py
fi

step "blocks: build + gate" "$PY" layout/blocks/gen_blocks.py "${RENDER_ARGS[@]+"${RENDER_ARGS[@]}"}" \
  $([[ "$QUICK" -eq 1 ]] && printf '%s' --no-pex)

if [[ "$QUICK" -eq 0 ]]; then
  step "ctle stage"       "$PY" layout/blocks/ctle_stage.py "${RENDER_ARGS[@]+"${RENDER_ARGS[@]}"}"
  step "vga stage"        "$PY" layout/blocks/vga_stage.py "${RENDER_ARGS[@]+"${RENDER_ARGS[@]}"}"
  step "driver stage"     "$PY" layout/blocks/driver_stage.py "${RENDER_ARGS[@]+"${RENDER_ARGS[@]}"}"
  # Builds the black-boxed simulation view, gates it on LVS against the reduced
  # CDL and on the extraction being physical, and writes both post-layout DUT
  # netlists. CTLE is a few seconds; VGA and the pad driver take longer because
  # Magic extracts the full cell including pads. They belong in the regression
  # rather than rotting as opt-in scripts.
  step "ctle post-layout netlists"   "$PY" layout/blocks/run_postlayout.py --stage ctle
  step "vga post-layout netlists"    "$PY" layout/blocks/run_postlayout.py --stage vga
  step "driver post-layout netlists" "$PY" layout/blocks/run_postlayout.py --stage driver
fi

printf '\n============================================================\n'
if [[ ${#FAILED[@]} -eq 0 ]]; then
  printf 'All layout stages passed their gates.\n'
  printf '============================================================\n'
  exit 0
fi
printf 'Stages with failures:\n'
for name in "${FAILED[@]}"; do
  printf '  - %s\n' "$name"
done
printf '\nRead the JSON summaries for details; they carry the per-rule counts\n'
printf 'and the compare verdicts.\n'
printf '============================================================\n'
exit 1
