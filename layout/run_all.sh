#!/usr/bin/env bash
# Regenerate the whole layout flow and report every gate.
#
# Each stage writes JSON summaries next to its artifacts, so a failure is a
# structured result rather than a log to read:
#   layout/devices/out/{manifest,drc_summary,lvs_summary,pex_summary}.json
#   layout/blocks/out/blocks_summary.json
#   layout/blocks/out/ctle_stage/ctle_stage_summary.json
#
# Usage:
#   ./layout/run_all.sh
#   ./layout/run_all.sh --quick     # skip PEX and the stage
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
