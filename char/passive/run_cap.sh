#!/usr/bin/env bash
# Run IHP SG13G2 passive capacitor characterization (MIM / MoM / MOSCAP).
set -euo pipefail

PASSIVE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$PASSIVE_DIR/../.." && pwd)"
# shellcheck disable=SC1091
source "${HOME}/.local/share/ihp-eda/env.sh"
if [[ -x "${IHP_EDA_ROOT}/venv/bin/python" ]]; then
  PYTHON="${IHP_EDA_ROOT}/venv/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi

PDK_ROOT="${PDK_ROOT:-$HOME/.local/share/ihp-eda/IHP-Open-PDK}"
OUT_DIR="${OUT_DIR:-$PASSIVE_DIR/out}"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

mkdir -p "$OUT_DIR"
cd "$REPO"

echo "==> Capacitor sweeps → $OUT_DIR"
"$PYTHON" "$PASSIVE_DIR/ihp_cap_sweep.py" --pdk-root "$PDK_ROOT" --out-dir "$OUT_DIR" "$@"

echo "==> Capacitor summaries + plots"
"$PYTHON" "$PASSIVE_DIR/summarize_cap.py" --out-dir "$OUT_DIR"

echo "Done. See $OUT_DIR/cap_summary.csv and *.png / *.npz"
