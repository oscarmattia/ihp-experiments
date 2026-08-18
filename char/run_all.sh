#!/usr/bin/env bash
# Run full IHP SG13G2 MOSFET characterization (LV/HV × core/RF × N/P).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "${HOME}/.local/share/ihp-eda/env.sh"
# Prefer the EDA uv venv (has pygmid + matplotlib)
if [[ -x "${IHP_EDA_ROOT}/venv/bin/python" ]]; then
  PYTHON="${IHP_EDA_ROOT}/venv/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi

PDK_ROOT="${PDK_ROOT:-$HOME/.local/share/ihp-eda/IHP-Open-PDK}"
OUT_DIR="${OUT_DIR:-$ROOT/char/out}"

mkdir -p "$OUT_DIR"
cd "$ROOT"

echo "==> Sweeping device families into $OUT_DIR"
"$PYTHON" char/ihp_sweep.py --pdk-root "$PDK_ROOT" --out-dir "$OUT_DIR" "$@"

echo "==> Summaries + plots"
"$PYTHON" char/summarize.py --out-dir "$OUT_DIR"

echo "Done. See $OUT_DIR/summary.csv and *.png"
