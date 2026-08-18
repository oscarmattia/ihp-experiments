#!/usr/bin/env bash
# Run all available characterization suites.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${HOME}/.local/share/ihp-eda/env.sh"

"$ROOT/mos/run_all.sh" "$@"
"$ROOT/bjt/run_all.sh" "$@"
echo "Passive characterization not implemented yet (see char/passive/)."
