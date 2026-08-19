#!/usr/bin/env bash
# Render IHP SG13G2 passive device layout screenshots (GDS → PNG).
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
OUT_DIR="${OUT_DIR:-$PASSIVE_DIR/out/layouts}"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

mkdir -p "$OUT_DIR"
cd "$REPO"

echo "==> Layout screenshots → $OUT_DIR"
"$PYTHON" "$PASSIVE_DIR/render_layouts.py" --pdk-root "$PDK_ROOT" --out-dir "$OUT_DIR" "$@"

echo "Done. PNGs in $OUT_DIR, GDS copies in $OUT_DIR/gds/"
