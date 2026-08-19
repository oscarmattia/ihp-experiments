#!/usr/bin/env bash
# Run IHP SG13G2 inductor EM characterization (openEMS).
set -euo pipefail

PASSIVE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$PASSIVE_DIR/../.." && pwd)"
# shellcheck disable=SC1091
source "${HOME}/.local/share/ihp-eda/env.sh"
# shellcheck disable=SC1091
if [[ -f "${HOME}/.local/share/ihp-eda/em.env.sh" ]]; then
  source "${HOME}/.local/share/ihp-eda/em.env.sh"
fi

if [[ -x "${IHP_EDA_ROOT}/venv/bin/python" ]]; then
  PYTHON="${IHP_EDA_ROOT}/venv/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi

PDK_ROOT="${PDK_ROOT:-$HOME/.local/share/ihp-eda/IHP-Open-PDK}"
OUT_DIR="${OUT_DIR:-$PASSIVE_DIR/out}"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

SKIP_EM=0
EM_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-em)
      SKIP_EM=1
      shift
      ;;
    *)
      EM_ARGS+=("$1")
      shift
      ;;
  esac
done

mkdir -p "$OUT_DIR"
cd "$REPO"

if [[ "$SKIP_EM" -eq 0 ]]; then
  echo "==> Inductor EM → $OUT_DIR"
  set +e
  "$PYTHON" "$PASSIVE_DIR/ihp_ind_em.py" --pdk-root "$PDK_ROOT" --out-dir "$OUT_DIR" "${EM_ARGS[@]}"
  EM_RC=$?
  set -e
  if [[ "$EM_RC" -ne 0 ]]; then
    echo "WARNING: ihp_ind_em.py exited with code $EM_RC (see meta JSON / placeholder npz)"
  fi
else
  echo "==> Skipping EM (--skip-em); summarizing existing npz in $OUT_DIR"
fi

echo "==> Inductor summaries + plots"
"$PYTHON" "$PASSIVE_DIR/summarize_ind.py" --out-dir "$OUT_DIR"

echo "Done. See $OUT_DIR/ind_summary.csv and sg13_ind_*.npz / ind_*_LQ.png"
