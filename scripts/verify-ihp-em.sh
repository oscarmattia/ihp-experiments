#!/usr/bin/env bash
# Smoke-test the IHP EM toolchain (openEMS primary; Palace optional).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IHP_EDA_ROOT="${IHP_EDA_ROOT:-$HOME/.local/share/ihp-eda}"
PDK="${PDK:-ihp-sg13g2}"

if [[ -f "$IHP_EDA_ROOT/env.sh" ]]; then
  # shellcheck disable=SC1091
  source "$IHP_EDA_ROOT/env.sh"
elif [[ -f "$SCRIPT_DIR/env-ihp.sh" ]]; then
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/env-ihp.sh"
else
  echo "ERROR: environment file not found; run scripts/install-ihp-eda.sh first" >&2
  exit 1
fi

if [[ -f "$IHP_EDA_ROOT/em.env.sh" ]]; then
  # shellcheck disable=SC1091
  source "$IHP_EDA_ROOT/em.env.sh"
fi

fail=0
soft_warn=0
python_bin="${IHP_EDA_ROOT}/venv/bin/python"

check() {
  local name="$1"; shift
  if "$@"; then
    printf 'PASS  %s\n' "$name"
  else
    printf 'FAIL  %s\n' "$name"
    fail=1
  fi
}

warn_check() {
  local name="$1"; shift
  if "$@"; then
    printf 'PASS  %s\n' "$name"
  else
    printf 'WARN  %s\n' "$name"
    soft_warn=1
  fi
}

if [[ ! -x "$python_bin" ]]; then
  echo "ERROR: Python venv missing at $python_bin; run scripts/install-ihp-em.sh" >&2
  exit 1
fi

check "Python import gdspy" "$python_bin" -c 'import gdspy'
check "Python import skrf" "$python_bin" -c 'import skrf'
check "Python import numpy" "$python_bin" -c 'import numpy'

openems_bin=""
for candidate in \
  "${OPENEMS_ROOT:+$OPENEMS_ROOT/bin/openEMS}" \
  "${IHP_TOOLS_PREFIX:-$IHP_EDA_ROOT/tools}/bin/openEMS" \
  "$(command -v openEMS 2>/dev/null || true)"; do
  if [[ -n "$candidate" && -x "$candidate" ]]; then
    openems_bin="$candidate"
    break
  fi
done

if [[ -n "$openems_bin" ]]; then
  printf 'PASS  openEMS binary (%s)\n' "$openems_bin"
else
  printf 'WARN  openEMS binary missing (Python EM deps OK)\n'
  soft_warn=1
fi

if [[ -n "${PDK_ROOT:-}" ]]; then
  workflow="$PDK_ROOT/$PDK/libs.tech/openems/openems_ihp_sg13g2/workflow"
  check "openEMS workflow dir" test -d "$workflow"
else
  warn_check "openEMS workflow dir (PDK_ROOT unset)" false
fi

# Optional mesh preview — only when openEMS binary exists and preview script is present.
preview_script="${OPENEMS_WORKFLOW:-}"
if [[ -z "$preview_script" && -n "${PDK_ROOT:-}" ]]; then
  preview_script="$PDK_ROOT/$PDK/libs.tech/openems/openems_ihp_sg13g2/workflow"
fi
preview_script="${preview_script}/run_line_noGDSII.py"

if [[ -n "$openems_bin" && -f "$preview_script" ]]; then
  log_dir="$(mktemp -d)"
  set +e
  (
    cd "$(dirname "$preview_script")"
    "$python_bin" "$(basename "$preview_script")" >"$log_dir/preview.log" 2>&1
  )
  preview_rc=$?
  set -e
  if [[ $preview_rc -eq 0 ]]; then
    printf 'PASS  mesh preview (run_line_noGDSII.py, preview_only)\n'
  else
    printf 'WARN  mesh preview failed (see %s)\n' "$log_dir/preview.log"
    tail -n 20 "$log_dir/preview.log" 2>/dev/null || true
    soft_warn=1
  fi
else
  printf 'SKIP  mesh preview (needs openEMS binary + %s)\n' "$preview_script"
fi

if [[ "$fail" -ne 0 ]]; then
  echo "EM verification failed (Python dependencies missing)." >&2
  exit 1
fi

if [[ "$soft_warn" -ne 0 ]]; then
  echo "EM verification passed with warnings (Python OK; openEMS binary or preview optional)."
  exit 0
fi

echo "All EM checks passed."
