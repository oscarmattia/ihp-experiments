#!/usr/bin/env bash
# Run IHP SG13G2 BJT / HBT DC characterization.
set -euo pipefail

BJT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$BJT_DIR/../.." && pwd)"
# shellcheck disable=SC1091
source "${HOME}/.local/share/ihp-eda/env.sh"
if [[ -x "${IHP_EDA_ROOT}/venv/bin/python" ]]; then
  PYTHON="${IHP_EDA_ROOT}/venv/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi

OUT_DIR="${OUT_DIR:-$BJT_DIR/out}"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

mkdir -p "$OUT_DIR"
cd "$REPO"

echo "==> BJT sweeps → $OUT_DIR"
"$PYTHON" "$BJT_DIR/ihp_bjt_sweep.py" --out-dir "$OUT_DIR" "$@"

echo "==> BJT summaries + plots"
"$PYTHON" "$BJT_DIR/summarize_bjt.py" --out-dir "$OUT_DIR"

echo "Done. See $OUT_DIR/summary.csv and *.npz / *.png"
