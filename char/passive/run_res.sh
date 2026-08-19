#!/usr/bin/env bash
# Run IHP SG13G2 resistor DC characterization.
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

OUT_DIR="${OUT_DIR:-$PASSIVE_DIR/out}"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

mkdir -p "$OUT_DIR"
cd "$REPO"

echo "==> Resistor sweeps → $OUT_DIR"
"$PYTHON" "$PASSIVE_DIR/ihp_res_sweep.py" --out-dir "$OUT_DIR" "$@"

echo "==> Resistor summaries + plots"
"$PYTHON" "$PASSIVE_DIR/summarize_res.py" --out-dir "$OUT_DIR"

echo "Done. See $OUT_DIR/res_summary.csv and *.npz / *.png"
