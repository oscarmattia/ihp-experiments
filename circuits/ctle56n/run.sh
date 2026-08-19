#!/bin/bash
# Run 56G CML CTLE sizing and simulation.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source ~/.local/share/ihp-eda/env.sh
PY="${IHP_EDA_ROOT}/venv/bin/python"
"$PY" "$ROOT/circuits/ctle56n/python/size_ctle.py"
"$PY" "$ROOT/circuits/ctle56n/python/run_sims.py" --no-iterate
