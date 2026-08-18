#!/usr/bin/env bash
# Run full IHP SG13G2 MOSFET characterization (LV/HV × core/RF × N/P).
set -euo pipefail

MOS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$MOS_DIR/../.." && pwd)"
# shellcheck disable=SC1091
source "${HOME}/.local/share/ihp-eda/env.sh"
if [[ -x "${IHP_EDA_ROOT}/venv/bin/python" ]]; then
  PYTHON="${IHP_EDA_ROOT}/venv/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi

PDK_ROOT="${PDK_ROOT:-$HOME/.local/share/ihp-eda/IHP-Open-PDK}"
OUT_DIR="${OUT_DIR:-$MOS_DIR/out}"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

mkdir -p "$OUT_DIR"
cd "$REPO"

echo "==> MOSFET sweeps → $OUT_DIR"
"$PYTHON" "$MOS_DIR/ihp_sweep.py" --pdk-root "$PDK_ROOT" --out-dir "$OUT_DIR" "$@"

echo "==> MOSFET summaries + plots"
"$PYTHON" "$MOS_DIR/summarize.py" --out-dir "$OUT_DIR"

echo "Done. See $OUT_DIR/summary.csv and *.png / *.npz"
