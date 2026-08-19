#!/usr/bin/env bash
# Run full IHP SG13G2 passive characterization (R, C, L).
set -euo pipefail

PASSIVE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${HOME}/.local/share/ihp-eda/env.sh"

SKIP_EM=0
ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-em)
      SKIP_EM=1
      shift
      ;;
    *)
      ARGS+=("$1")
      shift
      ;;
  esac
done

"$PASSIVE_DIR/run_res.sh" "${ARGS[@]}"
"$PASSIVE_DIR/run_cap.sh" "${ARGS[@]}"

if [[ "$SKIP_EM" -eq 1 ]]; then
  "$PASSIVE_DIR/run_ind.sh" --skip-em "${ARGS[@]}"
else
  "$PASSIVE_DIR/run_ind.sh" "${ARGS[@]}"
fi
