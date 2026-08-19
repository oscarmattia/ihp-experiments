#!/usr/bin/env bash
# Verify the IHP SG13G2 layout / verification toolchain.
#
# Checks, in the order that matters:
#   1. the PDK version triple check (klayout binary == pip module == versions.txt)
#   2. magic and netgen start headless with the PDK tech files
#   3. gdsfactory imports and the routing layer table matches the PDK
#   4. the PDK DRC deck runs and produces a parseable report
#   5. the PDK LVS deck compares its own mos_devices testcase clean
#   6. a foundry PCell can be placed, routed with gdsfactory and pass DRC
#
# Usage:
#   ./scripts/verify-ihp-layout.sh
#   ./scripts/verify-ihp-layout.sh --quick    # skip the DRC/LVS deck runs
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

IHP_EDA_ROOT="${IHP_EDA_ROOT:-$HOME/.local/share/ihp-eda}"
QUICK=0
[[ "${1:-}" == "--quick" ]] && QUICK=1

if [[ -f "$IHP_EDA_ROOT/env.sh" ]]; then
  # shellcheck disable=SC1091
  source "$IHP_EDA_ROOT/env.sh"
fi

PY="${IHP_PYTHON:-$IHP_EDA_ROOT/venv/bin/python}"
export IHP_PYTHON="$PY"
FAILURES=0

pass() { printf '  [ ok ] %s\n' "$*"; }
fail() { printf '  [FAIL] %s\n' "$*"; FAILURES=$((FAILURES + 1)); }
info() { printf '\n== %s\n' "$*"; }

require_pdk() {
  if [[ -z "${PDK_ROOT:-}" || ! -d "$PDK_ROOT" ]]; then
    fail "PDK_ROOT is unset or missing; run install-ihp-eda.sh and source env.sh"
    exit 1
  fi
}

# --- 1. versions -----------------------------------------------------------
check_versions() {
  info "Tool versions (PDK versions.txt is the source of truth)"
  local pinned binary pipver
  pinned="$(awk '$1=="klayout"{print $2}' "$PDK_ROOT/versions.txt" 2>/dev/null)"
  binary="$(klayout -b -v 2>/dev/null | tail -n1)"; binary="${binary##* }"
  pipver="$("$PY" -c 'import klayout; print(klayout.__version__)' 2>/dev/null)"

  if [[ -z "$pinned" ]]; then
    fail "could not read the klayout pin from $PDK_ROOT/versions.txt"
  elif [[ "$binary" == "$pinned" && "$pipver" == "$pinned" ]]; then
    pass "klayout $pinned (binary and pip module agree with versions.txt)"
  else
    fail "klayout version mismatch: versions.txt=$pinned binary=${binary:-none} pip=${pipver:-none}"
  fi

  local magic_pinned magic_have
  magic_pinned="$(awk '$1=="magic"{print $2}' "$PDK_ROOT/versions.txt" 2>/dev/null)"
  magic_have="$(magic --version 2>/dev/null | head -n1)"
  if [[ -n "$magic_have" && "$magic_have" == "$magic_pinned" ]]; then
    pass "magic $magic_have (matches versions.txt)"
  elif [[ -n "$magic_have" ]]; then
    fail "magic $magic_have but versions.txt pins $magic_pinned"
  else
    fail "magic not found on PATH"
  fi

  if command -v netgen >/dev/null 2>&1; then
    pass "netgen present: $(command -v netgen)"
  else
    fail "netgen not found on PATH"
  fi
}

# --- 2. magic + netgen headless -------------------------------------------
check_magic_netgen() {
  info "Magic and netgen headless startup with PDK tech files"
  local rc="$PDK_ROOT/$PDK/libs.tech/magic/ihp-sg13g2.magicrc"
  if [[ ! -f "$rc" ]]; then
    fail "magicrc missing at $rc"
    return
  fi
  local out
  out="$(printf 'quit -noprompt\n' | magic -dnull -noconsole -rcfile "$rc" 2>&1)"
  if grep -qi "technology.*sg13g2\|Loading ihp-sg13g2" <<<"$out"; then
    pass "magic loaded the ihp-sg13g2 technology"
  else
    fail "magic did not report loading ihp-sg13g2 (see output below)"
    printf '%s\n' "$out" | tail -n 5 | sed 's/^/         /'
  fi

  local setup="$PDK_ROOT/$PDK/libs.tech/netgen/ihp-sg13g2_setup.tcl"
  if [[ -f "$setup" ]]; then
    pass "netgen LVS setup present: $(basename "$setup")"
  else
    fail "netgen setup missing at $setup"
  fi
  if printf 'quit\n' | netgen -batch >/dev/null 2>&1; then
    pass "netgen ran in batch mode"
  else
    fail "netgen -batch failed"
  fi
}

# --- 3. python side --------------------------------------------------------
check_python() {
  info "Python layout stack"
  if "$PY" - <<'PY' 2>/dev/null
import gdsfactory, klayout, klayout.db, klayout.pex
print(f"gdsfactory {gdsfactory.__version__} klayout {klayout.__version__}")
PY
  then
    pass "gdsfactory, klayout.db and klayout.pex import"
  else
    fail "gdsfactory / klayout Python imports failed"
  fi

  local out
  out="$(cd "$REPO_ROOT" && "$PY" -c '
import sys; sys.path.insert(0, ".")
from layout.common.layers import validate_routing_layers
p = validate_routing_layers()
print("OK" if not p else "DRIFT: " + "; ".join(p))
' 2>/dev/null | tail -n1)"
  if [[ "$out" == "OK" ]]; then
    pass "routing layer table matches the PDK layer definitions"
  else
    fail "routing layer table drifted from the PDK: $out"
  fi
}

# --- 4. DRC deck -----------------------------------------------------------
check_drc() {
  info "PDK DRC deck on its own unit testcase"
  local gds="$PDK_ROOT/$PDK/libs.tech/klayout/tech/drc/testing/testcases/unit/activ.gds"
  local dir; dir="$(mktemp -d)"
  # The PDK's testcases are deliberately dirty patterns, so the check is that
  # the deck runs and produces a parseable report, not that it comes back clean.
  local out
  out="$(cd "$REPO_ROOT" && "$PY" -c "
import sys; sys.path.insert(0, '.')
from pathlib import Path
from layout.common.drc import run_drc
r = run_drc(gds=Path('$gds'), run_dir=Path('$dir'), cell_name='activ')
print('RAN' if r.total >= 0 else 'BROKEN', r.total, sorted(r.by_rule)[:4])
" 2>/dev/null | tail -n1)"
  rm -rf "$dir"
  if [[ "$out" == RAN* ]]; then
    pass "DRC deck ran and produced a report ($out)"
  else
    fail "DRC deck did not produce a parseable report: ${out:-no output}"
  fi
}

# --- 5. LVS deck -----------------------------------------------------------
check_lvs() {
  info "PDK LVS deck on its own mos_devices testcase"
  local base="$PDK_ROOT/$PDK/libs.tech/klayout/tech/lvs/testing/testcases/unit/mos_devices"
  local dir; dir="$(mktemp -d)"
  local out
  out="$(cd "$REPO_ROOT" && "$PY" -c "
import sys; sys.path.insert(0, '.')
from pathlib import Path
from layout.common.lvs import run_lvs
r = run_lvs(gds=Path('$base/layout/sg13_lv_nmos.gds'),
            cdl=Path('$base/netlist/sg13_lv_nmos.cdl'),
            run_dir=Path('$dir'), topcell='sg13_lv_nmos')
print('CLEAN' if r.clean else 'FAIL', r.summary[:70])
" 2>/dev/null | tail -n1)"
  rm -rf "$dir"
  if [[ "$out" == CLEAN* ]]; then
    pass "LVS compared the PDK mos_devices testcase clean"
  else
    fail "LVS on the PDK testcase did not pass: ${out:-no output}"
  fi
}

# --- 6. PCell placement + gdsfactory routing ------------------------------
check_routing() {
  info "Foundry PCell placement plus gdsfactory electrical routing"
  local out
  out="$(cd "$REPO_ROOT" && QT_QPA_PLATFORM=offscreen "$PY" layout/spike_routing.py 2>&1 | tail -n1)"
  if [[ "$out" == "DRC clean"* ]]; then
    pass "routed two PCell devices and the result is DRC clean"
  else
    fail "routing spike failed: ${out:-no output}"
  fi
}

main() {
  printf 'IHP SG13G2 layout toolchain verification\n'
  require_pdk
  check_versions
  check_magic_netgen
  check_python
  if [[ "$QUICK" -eq 0 ]]; then
    check_drc
    check_lvs
    check_routing
  else
    info "Skipping deck runs (--quick)"
  fi

  printf '\n============================================================\n'
  if [[ "$FAILURES" -eq 0 ]]; then
    printf 'All layout toolchain checks passed.\n'
    printf '============================================================\n'
    exit 0
  fi
  printf '%d check(s) FAILED.\n' "$FAILURES"
  printf '============================================================\n'
  exit 1
}

main "$@"
