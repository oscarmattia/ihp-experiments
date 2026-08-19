#!/usr/bin/env bash
# Install IHP SG13G2 physical-layout / verification tooling — idempotent.
#
# Installs the tools the PDK's own signoff decks need:
#   - KLayout at the version pinned in the PDK's versions.txt (DRC + LVS decks)
#   - Magic + netgen at the pinned versions (parasitic extraction, netlist LVS)
#   - gdsfactory in the EDA venv (composition + electrical routing)
#
# Method follows the PDK CI (.github/workflows/drc_regression.yml): the KLayout
# version is read from versions.txt rather than hardcoded, and the official .deb
# is preferred. When klayout.org/klayout.de are blocked by egress policy the
# script falls back to a source build from GitHub, which is slower but produces
# the same binary.
#
# Usage:
#   ./scripts/install-ihp-layout.sh
#   ./scripts/install-ihp-layout.sh --only-klayout
#   ./scripts/install-ihp-layout.sh --klayout-from-source
#   IHP_EDA_ROOT=/custom/path ./scripts/install-ihp-layout.sh --skip-magic
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

IHP_EDA_ROOT="${IHP_EDA_ROOT:-$HOME/.local/share/ihp-eda}"
IHP_TOOLS_PREFIX="${IHP_TOOLS_PREFIX:-$IHP_EDA_ROOT/tools}"
IHP_SRC_DIR="${IHP_SRC_DIR:-$IHP_EDA_ROOT/src}"
PDK_ROOT="${PDK_ROOT:-$IHP_EDA_ROOT/IHP-Open-PDK}"
PDK="${PDK:-ihp-sg13g2}"

# versions.txt is the PDK's single source of truth for KLayout and Magic.
# These are only fallbacks used when the file cannot be read.
KLAYOUT_VERSION_FALLBACK="${KLAYOUT_VERSION_FALLBACK:-0.30.5}"
MAGIC_VERSION_FALLBACK="${MAGIC_VERSION_FALLBACK:-8.3.589}"
# versions.txt does not pin netgen; the PDK README lists RTimothyEdwards as source.
NETGEN_TAG="${NETGEN_TAG:-1.5.323}"

GDSFACTORY_SPEC="${GDSFACTORY_SPEC:-gdsfactory~=9.44.0}"
IHP_GDSFACTORY_SPEC="${IHP_GDSFACTORY_SPEC:-ihp-gdsfactory==2.0.0}"

LAYOUT_STATUS_FILE="${LAYOUT_STATUS_FILE:-$IHP_EDA_ROOT/layout.status}"

SKIP_APT=0
SKIP_KLAYOUT=0
SKIP_MAGIC=0
SKIP_NETGEN=0
SKIP_PYTHON=0
KLAYOUT_FROM_SOURCE=0
WITH_IHP_GDSFACTORY=0
FORCE=0

log()  { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

have_cmd() { command -v "$1" >/dev/null 2>&1; }

usage() {
  cat <<EOF
Install IHP SG13G2 layout / verification tooling (KLayout, Magic, netgen, gdsfactory).

Options:
  --skip-apt              Do not install OS packages
  --skip-klayout          Skip KLayout install/upgrade
  --skip-magic            Skip Magic build
  --skip-netgen           Skip netgen build
  --skip-python           Skip venv Python packages
  --only-klayout          Only do KLayout (implies skipping the rest)
  --only-magic            Only do Magic
  --only-netgen           Only do netgen
  --only-python           Only do venv Python packages
  --klayout-from-source   Build KLayout from source even if a .deb is reachable
  --with-ihp-gdsfactory   Also install ihp-gdsfactory --no-deps (layer map / cross-sections)
  --force                 Reinstall/rebuild even when already at the right version
  -h, --help              Show this help

Environment:
  IHP_EDA_ROOT            Install root (default: ~/.local/share/ihp-eda)
  PDK_ROOT                PDK checkout (default: \$IHP_EDA_ROOT/IHP-Open-PDK)
  NETGEN_TAG              netgen git tag (default: $NETGEN_TAG)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-apt) SKIP_APT=1 ;;
    --skip-klayout) SKIP_KLAYOUT=1 ;;
    --skip-magic) SKIP_MAGIC=1 ;;
    --skip-netgen) SKIP_NETGEN=1 ;;
    --skip-python) SKIP_PYTHON=1 ;;
    --only-klayout) SKIP_MAGIC=1; SKIP_NETGEN=1; SKIP_PYTHON=1 ;;
    --only-magic) SKIP_KLAYOUT=1; SKIP_NETGEN=1; SKIP_PYTHON=1 ;;
    --only-netgen) SKIP_KLAYOUT=1; SKIP_MAGIC=1; SKIP_PYTHON=1 ;;
    --only-python) SKIP_KLAYOUT=1; SKIP_MAGIC=1; SKIP_NETGEN=1 ;;
    --klayout-from-source) KLAYOUT_FROM_SOURCE=1 ;;
    --with-ihp-gdsfactory) WITH_IHP_GDSFACTORY=1 ;;
    --force) FORCE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
  shift
done

mkdir -p "$IHP_EDA_ROOT" "$IHP_TOOLS_PREFIX/bin" "$IHP_SRC_DIR" "$HOME/.local/bin"
export PATH="$IHP_TOOLS_PREFIX/bin:$HOME/.local/bin:$PATH"

# ---------------------------------------------------------------------------
# versions.txt — the PDK's single source of truth
# ---------------------------------------------------------------------------

pdk_pinned_version() {
  # $1 = tool name as it appears in versions.txt (e.g. "klayout", "magic")
  local tool="$1" file="$PDK_ROOT/versions.txt" value=""
  if [[ -f "$file" ]]; then
    value="$(awk -v t="$tool" '$1 == t { print $2; exit }' "$file" || true)"
  fi
  printf '%s\n' "$value"
}

klayout_required_version() {
  local v
  v="$(pdk_pinned_version klayout)"
  printf '%s\n' "${v:-$KLAYOUT_VERSION_FALLBACK}"
}

magic_required_version() {
  local v
  v="$(pdk_pinned_version magic)"
  printf '%s\n' "${v:-$MAGIC_VERSION_FALLBACK}"
}

klayout_binary_version() {
  # KLayout prints e.g. "KLayout 0.30.5"; -b keeps it out of GUI mode.
  local out
  out="$(klayout -b -v 2>/dev/null | tail -n1 || true)"
  printf '%s\n' "${out##* }"
}

# ---------------------------------------------------------------------------
# OS packages
# ---------------------------------------------------------------------------

install_apt_deps() {
  [[ "$SKIP_APT" -eq 1 ]] && { log "Skipping apt packages"; return 0; }
  if ! have_cmd sudo; then
    warn "sudo not available; assuming build dependencies are present"
    return 0
  fi
  log "Installing layout build/runtime packages (apt)"
  sudo apt-get update -qq
  # KLayout source build needs Qt5 + ruby (DRC/LVS DSL) + python headers.
  # Magic/netgen need Tcl/Tk, Cairo and X11 headers.
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential git curl ca-certificates m4 \
    ruby ruby-dev \
    python3-dev \
    zlib1g-dev libpng-dev libcurl4-openssl-dev libexpat1-dev \
    qtbase5-dev qttools5-dev libqt5svg5-dev \
    tcl-dev tk-dev libcairo2-dev libx11-dev libxpm-dev \
    || warn "Some layout build packages could not be installed"
}

# ---------------------------------------------------------------------------
# KLayout
# ---------------------------------------------------------------------------

klayout_deb_urls() {
  local version="$1"
  # The PDK CI uses klayout.org; the PDK README points at klayout.de. Try both.
  printf '%s\n' \
    "https://www.klayout.org/downloads/Ubuntu-24/klayout_${version}-1_amd64.deb" \
    "https://www.klayout.de/downloads/Ubuntu-24/klayout_${version}-1_amd64.deb"
}

install_klayout_deb() {
  local version="$1" tmp deb url
  tmp="$(mktemp -d)"
  deb="$tmp/klayout_${version}_amd64.deb"

  while read -r url; do
    log "Trying KLayout .deb: $url"
    if curl -fL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
         -o "$deb" "$url"; then
      log "Installing $deb"
      if sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "$deb" \
         || { sudo dpkg -i "$deb"; sudo apt-get install -fy; }; then
        rm -rf "$tmp"
        return 0
      fi
      warn "dpkg/apt install of $deb failed"
    fi
  done < <(klayout_deb_urls "$version")

  rm -rf "$tmp"
  return 1
}

build_klayout_from_source() {
  local version="$1"
  local src="$IHP_SRC_DIR/klayout-$version"
  local prefix="$IHP_TOOLS_PREFIX/klayout-$version"

  if [[ ! -d "$src" || "$FORCE" -eq 1 ]]; then
    log "Fetching KLayout v${version} source from GitHub"
    rm -rf "$src"
    mkdir -p "$src"
    local tarball="$IHP_SRC_DIR/klayout-${version}.tar.gz"
    curl -fL --retry 5 --retry-delay 3 --retry-all-errors \
      -o "$tarball" \
      "https://codeload.github.com/KLayout/klayout/tar.gz/refs/tags/v${version}" \
      || die "Could not download KLayout v${version} source"
    tar -xzf "$tarball" -C "$src" --strip-components=1
    rm -f "$tarball"
  else
    log "Reusing KLayout source at $src"
  fi

  # klayout_main (the batch-capable "klayout" binary) is Qt-gated in
  # src/klayout.pro, so Qt is required. Qt bindings are the dominant build
  # cost and are useless for batch DRC/LVS, so drop them along with the
  # optional Qt modules. Ruby must be enabled: the PDK decks are Ruby DSL,
  # and src/klayout.pro only builds drc/ and lvs/ when HAVE_RUBY=1.
  # -nolibgit2 drops the GUI package manager, which otherwise needs git2.h.
  log "Building KLayout v${version} (this takes a while on few cores)"
  ( cd "$src" && ./build.sh \
      -release \
      -prefix "$prefix" \
      -j"$(nproc)" \
      -without-qtbinding \
      -without-qt-multimedia \
      -without-qt-designer \
      -without-qt-uitools \
      -nolibgit2 \
      -ruby "$(command -v ruby)" \
      -python "$(command -v python3)" \
  ) || die "KLayout source build failed"

  [[ -x "$prefix/klayout" ]] || die "KLayout build finished but $prefix/klayout is missing"
  ln -sfn "$prefix/klayout" "$IHP_TOOLS_PREFIX/bin/klayout"
  ln -sfn "$prefix/klayout" "$HOME/.local/bin/klayout" 2>/dev/null || true
  # The buddy tools (strmrun, strm2gds, ...) ship next to the main binary.
  local buddy
  for buddy in "$prefix"/strm*; do
    [[ -x "$buddy" ]] && ln -sfn "$buddy" "$IHP_TOOLS_PREFIX/bin/$(basename "$buddy")"
  done
  log "Installed KLayout to $prefix"
}

install_klayout() {
  [[ "$SKIP_KLAYOUT" -eq 1 ]] && { log "Skipping KLayout"; return 0; }

  local required current
  required="$(klayout_required_version)"
  current="$(klayout_binary_version)"

  log "KLayout: required $required (PDK versions.txt), current ${current:-none}"

  if [[ "$current" == "$required" && "$FORCE" -eq 0 ]]; then
    log "KLayout already at $required"
    return 0
  fi

  if [[ "$KLAYOUT_FROM_SOURCE" -eq 0 ]]; then
    if install_klayout_deb "$required"; then
      hash -r
      log "KLayout .deb installed: $(klayout_binary_version)"
      return 0
    fi
    warn "KLayout .deb unreachable (egress policy blocks klayout.org/klayout.de?)."
    warn "Falling back to a source build from GitHub."
  fi

  build_klayout_from_source "$required"
  hash -r
  local built
  built="$(klayout_binary_version)"
  [[ "$built" == "$required" ]] \
    || warn "KLayout reports $built but versions.txt pins $required"
}

# ---------------------------------------------------------------------------
# Magic + netgen
# ---------------------------------------------------------------------------

clone_at_tag() {
  # $1 = repo url, $2 = tag, $3 = dest
  local url="$1" tag="$2" dest="$3"
  if [[ -d "$dest/.git" ]]; then
    git -C "$dest" fetch --depth 1 origin "refs/tags/${tag}:refs/tags/${tag}" 2>/dev/null || true
    git -C "$dest" checkout -f "$tag" 2>/dev/null && return 0
    warn "Could not check out $tag in $dest; re-cloning"
    rm -rf "$dest"
  fi
  git clone --depth 1 --branch "$tag" "$url" "$dest"
}

install_magic() {
  [[ "$SKIP_MAGIC" -eq 1 ]] && { log "Skipping Magic"; return 0; }

  local required
  required="$(magic_required_version)"

  if [[ -x "$IHP_TOOLS_PREFIX/bin/magic" && "$FORCE" -eq 0 ]]; then
    local current
    current="$("$IHP_TOOLS_PREFIX/bin/magic" --version 2>/dev/null | head -n1 || true)"
    if [[ "$current" == "$required" ]]; then
      log "Magic already at $required"
      return 0
    fi
    log "Magic present at ${current:-unknown}; PDK pins $required — rebuilding"
  fi

  log "Building Magic $required (PDK versions.txt)"
  local src="$IHP_SRC_DIR/magic"
  clone_at_tag https://github.com/RTimothyEdwards/magic.git "$required" "$src" \
    || { warn "Magic clone failed"; return 1; }

  ( cd "$src" \
    && ./configure --prefix="$IHP_TOOLS_PREFIX" \
    && make -j"$(nproc)" \
    && make install ) || { warn "Magic build failed"; return 1; }

  ln -sfn "$IHP_TOOLS_PREFIX/bin/magic" "$HOME/.local/bin/magic" 2>/dev/null || true
  log "Magic installed: $("$IHP_TOOLS_PREFIX/bin/magic" --version 2>/dev/null || echo unknown)"
}

install_netgen() {
  [[ "$SKIP_NETGEN" -eq 1 ]] && { log "Skipping netgen"; return 0; }

  if [[ -x "$IHP_TOOLS_PREFIX/bin/netgen" && "$FORCE" -eq 0 ]]; then
    log "netgen already installed: $("$IHP_TOOLS_PREFIX/bin/netgen" -batch quit 2>&1 | head -n1 || true)"
    return 0
  fi

  # Note: apt's "netgen" is an unrelated FEM mesh generator. The LVS netgen is
  # RTimothyEdwards/netgen, which the PDK pairs with its magic extract deck.
  log "Building netgen $NETGEN_TAG (LVS, not the apt mesh generator)"
  local src="$IHP_SRC_DIR/netgen"
  clone_at_tag https://github.com/RTimothyEdwards/netgen.git "$NETGEN_TAG" "$src" \
    || { warn "netgen clone failed"; return 1; }

  ( cd "$src" \
    && ./configure --prefix="$IHP_TOOLS_PREFIX" \
    && make -j"$(nproc)" \
    && make install ) || { warn "netgen build failed"; return 1; }

  ln -sfn "$IHP_TOOLS_PREFIX/bin/netgen" "$HOME/.local/bin/netgen" 2>/dev/null || true
  log "netgen installed at $IHP_TOOLS_PREFIX/bin/netgen"
}

# ---------------------------------------------------------------------------
# Python packages
# ---------------------------------------------------------------------------

venv_install() {
  if have_cmd uv; then
    uv pip install --python "$IHP_EDA_ROOT/venv/bin/python" "$@"
  else
    "$IHP_EDA_ROOT/venv/bin/pip" install "$@"
  fi
}

install_python_deps() {
  [[ "$SKIP_PYTHON" -eq 1 ]] && { log "Skipping Python packages"; return 0; }
  [[ -x "$IHP_EDA_ROOT/venv/bin/python" ]] \
    || die "EDA venv missing at $IHP_EDA_ROOT/venv — run install-ihp-eda.sh first"

  log "Installing layout Python packages into the EDA venv"
  # gdsfactory is used for composition and electrical routing only; devices
  # come from the PDK's own PCells. psutil silences a warning from the PCell
  # library; the rest are used by the PDK DRC/LVS wrappers and our reports.
  venv_install "$GDSFACTORY_SPEC" psutil tqdm termcolor matplotlib \
    || warn "Some layout Python packages failed to install"

  if [[ "$WITH_IHP_GDSFACTORY" -eq 1 ]]; then
    # --no-deps is deliberate: ihp-gdsfactory declares a hard dependency on
    # gdsfactoryplus, which is proprietary. No module in the ihp package
    # imports it (verified against the published wheel), so the Apache-2.0
    # layer map and cross-sections are usable without it.
    log "Installing $IHP_GDSFACTORY_SPEC --no-deps (layer map / cross-sections only)"
    venv_install --no-deps "$IHP_GDSFACTORY_SPEC" \
      || warn "ihp-gdsfactory install failed; layout/common/xsection.py fallback applies"
  fi

  "$IHP_EDA_ROOT/venv/bin/python" - <<'PY'
import gdsfactory, klayout, klayout.db, klayout.pex
print("gdsfactory", gdsfactory.__version__, "| klayout", klayout.__version__)
PY
}

# ---------------------------------------------------------------------------
# Environment file
# ---------------------------------------------------------------------------

write_layout_env() {
  log "Writing $IHP_EDA_ROOT/layout.env.sh"
  local kl="$PDK_ROOT/$PDK/libs.tech/klayout"
  cat > "$IHP_EDA_ROOT/layout.env.sh" <<EOF
# Auto-generated by install-ihp-layout.sh — do not edit by hand.
export PATH="$IHP_TOOLS_PREFIX/bin:\$HOME/.local/bin:\$PATH"

# KLayout technology + DRC/LVS decks.
export KLAYOUT_HOME="\${KLAYOUT_HOME:-\$HOME/.klayout}"
export KLAYOUT_PATH="\$KLAYOUT_HOME:$kl"
export IHP_KLAYOUT_TECH="$kl/tech"
export IHP_DRC_RUNNER="$kl/tech/drc/run_drc.py"
export IHP_LVS_RUNNER="$kl/tech/lvs/run_lvs.py"

# Foundry PCells need both of these on sys.path; library_by_name() also needs
# the technology name (see layout/common/pdk.py).
export IHP_PYCELL_PATH="$kl/python"
export IHP_PYCELL_API_PATH="$kl/python/pycell4klayout-api/source/python"
export PYTHONPATH="\$IHP_PYCELL_PATH:\$IHP_PYCELL_API_PATH:\${PYTHONPATH:-}"

# Magic parasitic extraction + netgen LVS.
export MAGIC_RCFILE="$PDK_ROOT/$PDK/libs.tech/magic/ihp-sg13g2.magicrc"
export MAGIC_TECH_DIR="$PDK_ROOT/$PDK/libs.tech/magic"
export NETGEN_SETUP="$PDK_ROOT/$PDK/libs.tech/netgen/ihp-sg13g2_setup.tcl"
EOF

  if [[ -f "$IHP_EDA_ROOT/env.sh" ]]; then
    local marker="# >>> ihp-layout >>>"
    if ! grep -qF "$marker" "$IHP_EDA_ROOT/env.sh"; then
      cat >> "$IHP_EDA_ROOT/env.sh" <<EOF

$marker
[[ -f "\$IHP_EDA_ROOT/layout.env.sh" ]] && source "\$IHP_EDA_ROOT/layout.env.sh"
# <<< ihp-layout <<<
EOF
      log "Appended layout source hook to $IHP_EDA_ROOT/env.sh"
    fi
  else
    warn "Main env.sh not found at $IHP_EDA_ROOT/env.sh — source layout.env.sh manually"
  fi
}

write_status() {
  cat > "$LAYOUT_STATUS_FILE" <<EOF
# Auto-generated by install-ihp-layout.sh — do not edit by hand.
klayout_required=$(klayout_required_version)
klayout=$(klayout_binary_version 2>/dev/null || echo missing)
magic=$("$IHP_TOOLS_PREFIX/bin/magic" --version 2>/dev/null | head -n1 || echo missing)
netgen=$([[ -x "$IHP_TOOLS_PREFIX/bin/netgen" ]] && echo ok || echo missing)
gdsfactory=$("$IHP_EDA_ROOT/venv/bin/python" -c 'import gdsfactory;print(gdsfactory.__version__)' 2>/dev/null || echo missing)
updated_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
}

print_summary() {
  write_status
  cat <<EOF

============================================================
IHP layout / verification tooling
============================================================
IHP_EDA_ROOT     = $IHP_EDA_ROOT
PDK_ROOT         = $PDK_ROOT
klayout required = $(klayout_required_version)   (PDK versions.txt)
klayout          = $(command -v klayout || echo MISSING) $(klayout_binary_version 2>/dev/null || true)
magic            = $(command -v magic || echo MISSING) $("$IHP_TOOLS_PREFIX/bin/magic" --version 2>/dev/null | head -n1 || true)
netgen           = $(command -v netgen || echo MISSING)
gdsfactory       = $("$IHP_EDA_ROOT/venv/bin/python" -c 'import gdsfactory;print(gdsfactory.__version__)' 2>/dev/null || echo MISSING)
Status file      = $LAYOUT_STATUS_FILE

Activate:
  source $IHP_EDA_ROOT/env.sh     # sources layout.env.sh automatically

Verify:
  $REPO_ROOT/scripts/verify-ihp-layout.sh
============================================================
EOF
}

main() {
  log "IHP layout install starting (root=$IHP_EDA_ROOT)"
  [[ -d "$PDK_ROOT/$PDK" ]] || die "PDK not found at $PDK_ROOT/$PDK — run install-ihp-eda.sh first"

  install_apt_deps
  install_klayout
  install_magic || warn "Magic unavailable; PEX will fall back to klayout.pex (R only)"
  install_netgen || warn "netgen unavailable; LVS runs through the KLayout deck only"
  install_python_deps
  write_layout_env
  print_summary
}

main "$@"
