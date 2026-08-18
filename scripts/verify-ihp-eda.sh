#!/usr/bin/env bash
# Smoke-test the IHP analog EDA toolchain.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IHP_EDA_ROOT="${IHP_EDA_ROOT:-$HOME/.local/share/ihp-eda}"

if [[ -f "$IHP_EDA_ROOT/env.sh" ]]; then
  # shellcheck disable=SC1091
  source "$IHP_EDA_ROOT/env.sh"
elif [[ -f "$SCRIPT_DIR/env-ihp.sh" ]]; then
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/env-ihp.sh"
else
  echo "ERROR: environment file not found; run scripts/install-ihp-eda.sh first" >&2
  exit 1
fi

fail=0
check() {
  local name="$1"; shift
  if "$@"; then
    printf 'PASS  %s\n' "$name"
  else
    printf 'FAIL  %s\n' "$name"
    fail=1
  fi
}

check "PDK_ROOT exists" test -d "${PDK_ROOT:?}/$PDK"
check "openvaf-r on PATH" command -v openvaf-r >/dev/null
check "ngspice on PATH" command -v ngspice >/dev/null
check "xschem on PATH" command -v xschem >/dev/null
check "klayout on PATH" command -v klayout >/dev/null
check "OSDI models present" bash -c 'compgen -G "$PDK_ROOT/$PDK/libs.tech/ngspice/osdi/*.osdi" >/dev/null'
check "~/.spiceinit symlink" test -L "$HOME/.spiceinit" -o -f "$HOME/.spiceinit"

# Minimal MOSFET operating-point test from IHP docs (ngspice.rst)
TMP="$(mktemp -d)"
cat > "$TMP/mostest.spice" <<EOF
.lib '$PDK_ROOT/$PDK/libs.tech/ngspice/models/cornerMOSlv.lib' mos_tt
Vgs net1 GND 0.4
Vds net3 GND 1.0
Vd net3 net2 0
.param temp=27
XM1 net2 net1 GND GND sg13_lv_nmos w=1.0u l=0.13u ng=1 m=1
.control
save all
op
let Id = @n.xm1.nsg13_lv_nmos[ids]
print Id
.endc
.GLOBAL GND
.end
EOF

# Preload OSDI if .spiceinit is not picked up in batch mode
OSDI_DIR="$PDK_ROOT/$PDK/libs.tech/ngspice/osdi"
if [[ -d "$OSDI_DIR" ]]; then
  {
    echo "* auto-generated for verify-ihp-eda.sh"
    for f in "$OSDI_DIR"/*.osdi; do
      echo "pre_osdi $f"
    done
  } > "$TMP/.spiceinit"
fi

set +e
(
  cd "$TMP"
  HOME="$TMP" ngspice -b mostest.spice >"$TMP/out.txt" 2>&1
)
rc=$?
set -e

if [[ $rc -eq 0 ]] && grep -Eq 'id[[:space:]]*=' "$TMP/out.txt"; then
  printf 'PASS  ngspice MOSFET op-point (docs example)\n'
  grep -E 'id[[:space:]]*=' "$TMP/out.txt" | head -n1
else
  printf 'FAIL  ngspice MOSFET op-point (docs example)\n'
  tail -n 40 "$TMP/out.txt" || true
  fail=1
fi

rm -rf "$TMP"

if [[ "$fail" -ne 0 ]]; then
  echo "Verification failed." >&2
  exit 1
fi
echo "All checks passed."
