#!/bin/bash
# Run 56G CML CTLE sizing and simulation.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source ~/.local/share/ihp-eda/env.sh
PY="${IHP_EDA_ROOT}/venv/bin/python"
# Single source of truth for sizing: size_ctle.py writes spice/params.inc.
# run_sims.py --no-iterate simulates that committed file (use --force-size to override).
"$PY" "$ROOT/circuits/ctle56n/python/size_ctle.py"
"$PY" "$ROOT/circuits/ctle56n/python/run_sims.py" --no-iterate
