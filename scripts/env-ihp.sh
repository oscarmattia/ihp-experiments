#!/usr/bin/env bash
# Source this file to activate the IHP analog EDA toolchain.
# Usage: source scripts/env-ihp.sh

# Resolve repo root when sourced from the repository; fall back to IHP_EDA_ROOT.
_IHP_ENV_SRC="${BASH_SOURCE[0]:-$0}"
_IHP_SCRIPTS_DIR="$(cd "$(dirname "$_IHP_ENV_SRC")" && pwd)"
_IHP_REPO_ROOT="$(cd "$_IHP_SCRIPTS_DIR/.." && pwd)"

export IHP_EDA_ROOT="${IHP_EDA_ROOT:-$HOME/.local/share/ihp-eda}"
export IHP_TOOLS_PREFIX="${IHP_TOOLS_PREFIX:-$IHP_EDA_ROOT/tools}"
export PDK_ROOT="${PDK_ROOT:-$IHP_EDA_ROOT/IHP-Open-PDK}"
export PDK="${PDK:-ihp-sg13g2}"

export PATH="$IHP_TOOLS_PREFIX/bin:$HOME/.local/bin:$PATH"

# KLayout PDK tech / DRC / LVS / PyCells
export KLAYOUT_HOME="${KLAYOUT_HOME:-$HOME/.klayout}"
export KLAYOUT_PATH="${KLAYOUT_PATH:-$KLAYOUT_HOME:$PDK_ROOT/$PDK/libs.tech/klayout}"

# Python toolchain (uv-managed venv)
if [[ -d "$IHP_EDA_ROOT/venv" ]]; then
  # shellcheck disable=SC1091
  source "$IHP_EDA_ROOT/venv/bin/activate"
fi

# Optional: point xschem at the PDK top schematic when launched without args
export XSCHEM_USER_CONF_DIR="${XSCHEM_USER_CONF_DIR:-$HOME/.xschem}"

unset _IHP_ENV_SRC _IHP_SCRIPTS_DIR _IHP_REPO_ROOT
