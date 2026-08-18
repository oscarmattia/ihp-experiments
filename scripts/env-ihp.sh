#!/usr/bin/env bash
# Source this file to activate the IHP analog EDA toolchain.
# Usage: source scripts/env-ihp.sh
#
# Prefers the installer-generated env at $IHP_EDA_ROOT/env.sh when present.

_IHP_ENV_SRC="${BASH_SOURCE[0]:-$0}"
_IHP_SCRIPTS_DIR="$(cd "$(dirname "$_IHP_ENV_SRC")" && pwd)"

export IHP_EDA_ROOT="${IHP_EDA_ROOT:-$HOME/.local/share/ihp-eda}"

if [[ -f "$IHP_EDA_ROOT/env.sh" ]]; then
  # shellcheck disable=SC1091
  source "$IHP_EDA_ROOT/env.sh"
  unset _IHP_ENV_SRC _IHP_SCRIPTS_DIR
  return 0 2>/dev/null || true
fi

# Fallback when the installer has not been run yet (paths still useful for docs).
export IHP_TOOLS_PREFIX="${IHP_TOOLS_PREFIX:-$IHP_EDA_ROOT/tools}"
export PDK_ROOT="${PDK_ROOT:-$IHP_EDA_ROOT/IHP-Open-PDK}"
export PDK="${PDK:-ihp-sg13g2}"
export PATH="$IHP_TOOLS_PREFIX/bin:$HOME/.local/bin:$PATH"
export KLAYOUT_HOME="${KLAYOUT_HOME:-$HOME/.klayout}"
export KLAYOUT_PATH="${KLAYOUT_PATH:-$KLAYOUT_HOME:$PDK_ROOT/$PDK/libs.tech/klayout}"
export XSCHEM_USER_CONF_DIR="${XSCHEM_USER_CONF_DIR:-$HOME/.xschem}"

unset _IHP_ENV_SRC _IHP_SCRIPTS_DIR
