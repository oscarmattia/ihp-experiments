#!/usr/bin/env bash
# Install IHP Open PDK + analog EDA tools for Ubuntu 24.04 (idempotent).
#
# Follows https://ihp-open-pdk-docs.readthedocs.io/en/latest/install/installation.html
# with Cloud Agent–friendly sources (GitHub mirrors when SourceForge/KLayout hosts
# are blocked by egress policy).
#
# Usage:
#   ./scripts/install-ihp-eda.sh
#   IHP_EDA_ROOT=/custom/path ./scripts/install-ihp-eda.sh
#   ./scripts/install-ihp-eda.sh --skip-klayout --skip-xschem
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

IHP_EDA_ROOT="${IHP_EDA_ROOT:-$HOME/.local/share/ihp-eda}"
IHP_TOOLS_PREFIX="${IHP_TOOLS_PREFIX:-$IHP_EDA_ROOT/tools}"
IHP_SRC_DIR="${IHP_SRC_DIR:-$IHP_EDA_ROOT/src}"
PDK_ROOT="${PDK_ROOT:-$IHP_EDA_ROOT/IHP-Open-PDK}"
PDK="${PDK:-ihp-sg13g2}"
PDK_BRANCH="${PDK_BRANCH:-dev}"

# Prefer a release with OSDI >= 0.4 (required by OpenVAF-Reloaded).
# IHP versions.txt lists ngspice 43 (OSDI 0.3 / legacy openvaf); we pin newer.
NGSPICE_TAG="${NGSPICE_TAG:-ngspice-45.2}"
XSCHEM_TAG="${XSCHEM_TAG:-3.4.6}"
OPENVAF_R_VERSION="${OPENVAF_R_VERSION:-v24.0.1mob}"
UV_VERSION="${UV_VERSION:-0.12.5}"
KLAYOUT_DEB_VERSION="${KLAYOUT_DEB_VERSION:-0.30.3}"

SKIP_APT=0
SKIP_NGSPICE=0
SKIP_XSCHEM=0
SKIP_KLAYOUT=0
SKIP_PDK=0
SKIP_MODELS=0
SKIP_PYTHON=0
WITH_EM=0
FORCE_REBUILD=0

log()  { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

have_cmd() { command -v "$1" >/dev/null 2>&1; }

usage() {
  cat <<EOF
Install IHP Open PDK and analog EDA tools (ngspice/OSDI, openvaf-r, xschem, klayout).

Options:
  --skip-apt         Do not install OS packages
  --skip-ngspice     Skip ngspice build
  --skip-xschem      Skip xschem build
  --skip-klayout     Skip klayout install
  --skip-pdk         Skip PDK clone/update
  --skip-models      Skip Verilog-A / OSDI compilation
  --skip-python      Skip uv venv + requirements
  --with-em          After analog install, run scripts/install-ihp-em.sh (openEMS / Palace)
  --force-rebuild    Rebuild tools even if present
  -h, --help         Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-apt) SKIP_APT=1 ;;
    --skip-ngspice) SKIP_NGSPICE=1 ;;
    --skip-xschem) SKIP_XSCHEM=1 ;;
    --skip-klayout) SKIP_KLAYOUT=1 ;;
    --skip-pdk) SKIP_PDK=1 ;;
    --skip-models) SKIP_MODELS=1 ;;
    --skip-python) SKIP_PYTHON=1 ;;
    --with-em) WITH_EM=1 ;;
    --force-rebuild) FORCE_REBUILD=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
  shift
done

mkdir -p "$IHP_EDA_ROOT" "$IHP_TOOLS_PREFIX/bin" "$IHP_SRC_DIR" "$HOME/.local/bin"

export PATH="$IHP_TOOLS_PREFIX/bin:$HOME/.local/bin:$PATH"

install_apt_deps() {
  [[ "$SKIP_APT" -eq 1 ]] && { log "Skipping apt packages"; return; }
  log "Installing OS build/runtime packages"
  sudo apt-get update -qq
  # Build deps from IHP docs (dependencies.rst), trimmed for analog flow.
  # shellcheck disable=SC2086
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential autoconf automake libtool pkg-config bison flex gawk \
    git curl wget ca-certificates tar gzip xz-utils unzip \
    python3 python3-dev python3-venv python3-tk \
    tcl8.6 tcl8.6-dev tk8.6 tk8.6-dev \
    libx11-dev libxrender-dev libxpm-dev libxft-dev \
    libxaw7-dev libxmu-dev \
    libreadline-dev libncurses-dev \
    libfftw3-dev \
    libjpeg-dev zlib1g-dev libpng-dev \
    libgtk-3-dev \
    qtbase5-dev qttools5-dev libqt5svg5-dev libqt5xmlpatterns5-dev \
    qtmultimedia5-dev libqt5multimediawidgets5 \
    libgl1-mesa-dev \
    adwaita-icon-theme \
    xterm \
    || die "apt-get install failed"

  # OpenVAF-Reloaded Linux binaries need libLLVM.so.21.1 (not in Ubuntu 24.04 base).
  ensure_llvm21_runtime
}

ensure_llvm21_runtime() {
  if [[ -e /usr/lib/x86_64-linux-gnu/libLLVM.so.21.1 ]] || ldconfig -p 2>/dev/null | grep -q 'libLLVM\.so\.21'; then
    log "LLVM 21 runtime already present"
    return
  fi
  log "Installing LLVM 21 runtime (required by openvaf-r)"
  local llvm_sh
  llvm_sh="$(mktemp)"
  curl -fsSL https://apt.llvm.org/llvm.sh -o "$llvm_sh"
  # apt.llvm.org/llvm.sh installs clang+llvm; runtime (libllvm21) is included.
  sudo bash "$llvm_sh" 21
  rm -f "$llvm_sh"
  [[ -e /usr/lib/x86_64-linux-gnu/libLLVM.so.21.1 ]] \
    || die "libLLVM.so.21.1 still missing after LLVM 21 install"
}

install_uv() {
  if have_cmd uv && [[ "$FORCE_REBUILD" -eq 0 ]]; then
    log "uv already present: $(uv --version)"
    return
  fi
  log "Installing uv ${UV_VERSION} from GitHub releases"
  local tmp archive
  tmp="$(mktemp -d)"
  archive="uv-x86_64-unknown-linux-gnu.tar.gz"
  curl -fsSL \
    "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${archive}" \
    -o "$tmp/$archive"
  tar -xzf "$tmp/$archive" -C "$tmp"
  install -m 0755 "$tmp/uv-x86_64-unknown-linux-gnu/uv" "$HOME/.local/bin/uv"
  rm -rf "$tmp"
  uv --version
}

install_openvaf_r() {
  if [[ -x "$IHP_TOOLS_PREFIX/bin/openvaf-r" && "$FORCE_REBUILD" -eq 0 ]]; then
    if "$IHP_TOOLS_PREFIX/bin/openvaf-r" --help >/dev/null 2>&1; then
      log "openvaf-r already present: $IHP_TOOLS_PREFIX/bin/openvaf-r"
      return
    fi
    warn "openvaf-r present but not runnable; reinstalling"
  fi
  log "Installing OpenVAF-Reloaded ${OPENVAF_R_VERSION}"
  ensure_llvm21_runtime
  local tmp archive
  tmp="$(mktemp -d)"
  archive="openvaf-r-${OPENVAF_R_VERSION}-linux-x86_64.tar.gz"
  curl -fsSL \
    "https://github.com/OpenVAF/OpenVAF-Reloaded/releases/download/${OPENVAF_R_VERSION}/${archive}" \
    -o "$tmp/$archive"
  tar -xzf "$tmp/$archive" -C "$tmp"
  # Archive layout may be flat or nested; find the binary.
  local bin
  bin="$(find "$tmp" -type f -name 'openvaf-r' | head -n1)"
  [[ -n "$bin" ]] || die "openvaf-r binary not found in archive"
  install -m 0755 "$bin" "$IHP_TOOLS_PREFIX/bin/openvaf-r"
  ln -sfn "$IHP_TOOLS_PREFIX/bin/openvaf-r" "$HOME/.local/bin/openvaf-r"
  rm -rf "$tmp"
  "$IHP_TOOLS_PREFIX/bin/openvaf-r" --help >/dev/null \
    || die "openvaf-r installed but cannot run (check libLLVM.so.21.1)"
}

build_ngspice() {
  [[ "$SKIP_NGSPICE" -eq 1 ]] && { log "Skipping ngspice"; return; }
  if have_cmd ngspice && ngspice -v 2>&1 | head -n1 | grep -q . && [[ "$FORCE_REBUILD" -eq 0 ]]; then
    # Prefer a prefix-built ngspice with OSDI; rebuild if only system ngspice exists
    # without our marker.
    if [[ -x "$IHP_TOOLS_PREFIX/bin/ngspice" ]]; then
      log "ngspice already installed at $IHP_TOOLS_PREFIX/bin/ngspice"
      "$IHP_TOOLS_PREFIX/bin/ngspice" -v | head -n3 || true
      return
    fi
  fi

  log "Building ngspice (${NGSPICE_TAG}) with --enable-osdi"
  local src="$IHP_SRC_DIR/ngspice"
  if [[ ! -d "$src/.git" ]]; then
    rm -rf "$src"
    # Daily GitHub mirror of SourceForge (egress-friendly).
    git clone --depth 1 --branch "$NGSPICE_TAG" \
      https://github.com/danchitnis/ngspice-sf-mirror.git "$src"
  else
    git -C "$src" fetch --depth 1 origin "refs/tags/${NGSPICE_TAG}:refs/tags/${NGSPICE_TAG}" || true
    git -C "$src" checkout -f "$NGSPICE_TAG"
  fi

  pushd "$src" >/dev/null
  ./autogen.sh
  rm -rf release
  mkdir -p release && cd release
  ../configure \
    --prefix="$IHP_TOOLS_PREFIX" \
    --enable-osdi \
    --enable-xspice \
    --enable-cider \
    --disable-debug \
    --with-readline=yes
  make -j"$(nproc)"
  make install
  popd >/dev/null
  ln -sfn "$IHP_TOOLS_PREFIX/bin/ngspice" "$HOME/.local/bin/ngspice"
  ngspice -v | head -n5
}

build_xschem() {
  [[ "$SKIP_XSCHEM" -eq 1 ]] && { log "Skipping xschem"; return; }
  if [[ -x "$IHP_TOOLS_PREFIX/bin/xschem" && "$FORCE_REBUILD" -eq 0 ]]; then
    log "xschem already installed"
    "$IHP_TOOLS_PREFIX/bin/xschem" --version || true
    return
  fi

  log "Building xschem ${XSCHEM_TAG}"
  local src="$IHP_SRC_DIR/xschem"
  if [[ ! -d "$src/.git" ]]; then
    rm -rf "$src"
    git clone --depth 1 --branch "$XSCHEM_TAG" \
      https://github.com/StefanSchippers/xschem.git "$src"
  else
    git -C "$src" fetch --depth 1 origin "refs/tags/${XSCHEM_TAG}:refs/tags/${XSCHEM_TAG}" || true
    git -C "$src" checkout -f "$XSCHEM_TAG"
  fi

  pushd "$src" >/dev/null
  ./configure --prefix="$IHP_TOOLS_PREFIX"
  make -j"$(nproc)"
  make install
  popd >/dev/null
  ln -sfn "$IHP_TOOLS_PREFIX/bin/xschem" "$HOME/.local/bin/xschem"
  xschem --version || true
}

install_klayout() {
  [[ "$SKIP_KLAYOUT" -eq 1 ]] && { log "Skipping klayout"; return; }
  if have_cmd klayout && [[ "$FORCE_REBUILD" -eq 0 ]]; then
    log "klayout already present: $(command -v klayout) ($(klayout -v 2>/dev/null | head -n1 || echo unknown))"
    return
  fi

  log "Installing KLayout"
  local deb_url="https://www.klayout.de/downloads/Ubuntu-24/klayout_${KLAYOUT_DEB_VERSION}-1_amd64.deb"
  local tmp deb
  tmp="$(mktemp -d)"
  deb="$tmp/klayout.deb"

  if curl -fsSL "$deb_url" -o "$deb"; then
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "$deb" || \
      sudo dpkg -i "$deb" || sudo apt-get install -fy
    rm -rf "$tmp"
  else
    warn "Could not download KLayout ${KLAYOUT_DEB_VERSION} .deb (egress?). Falling back to Ubuntu package (older)."
    rm -rf "$tmp"
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends klayout
  fi
  have_cmd klayout || die "klayout installation failed"
  klayout -v 2>&1 | head -n3 || true
}

clone_pdk() {
  [[ "$SKIP_PDK" -eq 1 ]] && { log "Skipping PDK clone"; return; }
  log "Cloning/updating IHP-Open-PDK (branch ${PDK_BRANCH})"
  if [[ -d "$PDK_ROOT/.git" ]]; then
    git -C "$PDK_ROOT" fetch origin
    git -C "$PDK_ROOT" checkout "$PDK_BRANCH"
    git -C "$PDK_ROOT" pull --ff-only origin "$PDK_BRANCH" || true
    git -C "$PDK_ROOT" submodule sync --recursive
    git -C "$PDK_ROOT" submodule update --init --recursive
  else
    mkdir -p "$(dirname "$PDK_ROOT")"
    git clone --branch "$PDK_BRANCH" --recurse-submodules \
      https://github.com/IHP-GmbH/IHP-Open-PDK.git "$PDK_ROOT"
  fi
  [[ -d "$PDK_ROOT/$PDK" ]] || die "PDK tree missing at $PDK_ROOT/$PDK"
  link_pdk_into_repo
}

link_pdk_into_repo() {
  # The PDK is installed outside the repo, which makes browsing it from an editor
  # or referencing a file in it awkward. A symlink at the repo root fixes that
  # without vendoring 2 GB of upstream tree.
  #
  # It is gitignored (/pdk) rather than committed: the target is an absolute path
  # under $HOME, so a committed link would resolve only for whoever created it.
  # Recreating it here means it always points at this machine's actual PDK.
  local link="$REPO_ROOT/pdk"
  if [[ -e "$link" && ! -L "$link" ]]; then
    log "Not touching $link: it exists and is not a symlink"
    return
  fi
  ln -sfn "$PDK_ROOT" "$link"
  log "Linked $link -> $PDK_ROOT"
}

write_shell_env() {
  log "Writing environment activation helpers"
  # Persistent env file used by Cloud Agent install/start and interactive shells.
  cat > "$IHP_EDA_ROOT/env.sh" <<EOF
# Auto-generated by install-ihp-eda.sh — do not edit by hand.
export IHP_EDA_ROOT="$IHP_EDA_ROOT"
export IHP_TOOLS_PREFIX="$IHP_TOOLS_PREFIX"
export PDK_ROOT="$PDK_ROOT"
export PDK="$PDK"
export PATH="\$IHP_TOOLS_PREFIX/bin:\$HOME/.local/bin:\$PATH"
export KLAYOUT_HOME="\${KLAYOUT_HOME:-\$HOME/.klayout}"
export KLAYOUT_PATH="\$KLAYOUT_HOME:\$PDK_ROOT/\$PDK/libs.tech/klayout"
if [[ -d "\$IHP_EDA_ROOT/venv" ]]; then
  # shellcheck disable=SC1091
  source "\$IHP_EDA_ROOT/venv/bin/activate"
fi
EOF

  # Ensure interactive shells pick this up without conflicting with other RC logic.
  local marker="# >>> ihp-eda >>>"
  local env_line="[[ -f \"$IHP_EDA_ROOT/env.sh\" ]] && source \"$IHP_EDA_ROOT/env.sh\""
  if [[ -f "$HOME/.bashrc" ]] && ! grep -qF "$marker" "$HOME/.bashrc"; then
    cat >> "$HOME/.bashrc" <<EOF

$marker
$env_line
# <<< ihp-eda <<<
EOF
  fi
}

compile_models() {
  [[ "$SKIP_MODELS" -eq 1 ]] && { log "Skipping Verilog-A model compile"; return; }
  log "Compiling Verilog-A models to OSDI (openvaf)"
  export PDK_ROOT PDK PATH
  # shellcheck disable=SC1091
  source "$IHP_EDA_ROOT/env.sh"

  have_cmd openvaf-r || have_cmd openvaf || die "openvaf-r/openvaf required to compile models"

  local va_dir="$PDK_ROOT/$PDK/libs.tech/verilog-a"
  [[ -d "$va_dir" ]] || die "Missing $va_dir"

  pushd "$va_dir" >/dev/null
  if [[ -f openvaf-compile-va.sh ]]; then
    # Prefer generic CPU target for snapshot portability across hosts.
    bash openvaf-compile-va.sh --compile-model-generic || bash openvaf-compile-va.sh
  else
    die "openvaf-compile-va.sh not found"
  fi
  popd >/dev/null

  # Also run xschem install.py (compiles models + ~/.spiceinit symlink)
  local xschem_dir="$PDK_ROOT/$PDK/libs.tech/xschem"
  if [[ -f "$xschem_dir/install.py" ]]; then
    log "Running PDK xschem/install.py"
    python3 "$xschem_dir/install.py"
  fi

  local osdi="$PDK_ROOT/$PDK/libs.tech/ngspice/osdi"
  ls -la "$osdi"/*.osdi 2>/dev/null || warn "No .osdi files found under $osdi"
}

install_python() {
  [[ "$SKIP_PYTHON" -eq 1 ]] && { log "Skipping Python deps"; return; }
  log "Creating uv venv and installing PDK Python requirements"
  have_cmd uv || die "uv is required"
  local req="$PDK_ROOT/requirements.txt"
  [[ -f "$req" ]] || die "Missing $req — clone PDK first"

  if [[ ! -x "$IHP_EDA_ROOT/venv/bin/python" || "$FORCE_REBUILD" -eq 1 ]]; then
    uv venv "$IHP_EDA_ROOT/venv" --python python3 ${FORCE_REBUILD:+--clear}
  else
    log "uv venv already present at $IHP_EDA_ROOT/venv"
  fi
  # Use uv pip for compatibility with the PDK requirements.txt (no pyproject yet).
  uv pip install --python "$IHP_EDA_ROOT/venv/bin/python" -r "$req"
}

print_summary() {
  # shellcheck disable=SC1091
  source "$IHP_EDA_ROOT/env.sh"
  cat <<EOF

============================================================
IHP analog EDA environment ready
============================================================
IHP_EDA_ROOT     = $IHP_EDA_ROOT
PDK_ROOT         = $PDK_ROOT
PDK              = $PDK
ngspice          = $(command -v ngspice || echo MISSING)
openvaf-r        = $(command -v openvaf-r || echo MISSING)
xschem           = $(command -v xschem || echo MISSING)
klayout          = $(command -v klayout || echo MISSING)

Activate in a new shell:
  source $IHP_EDA_ROOT/env.sh
  # or: source $REPO_ROOT/scripts/env-ihp.sh

Verify:
  $REPO_ROOT/scripts/verify-ihp-eda.sh
============================================================
EOF
}

main() {
  log "IHP EDA install starting (root=$IHP_EDA_ROOT)"
  install_apt_deps
  install_uv
  install_openvaf_r
  build_ngspice
  build_xschem
  install_klayout
  clone_pdk
  write_shell_env
  install_python
  compile_models
  if [[ "$WITH_EM" -eq 1 ]]; then
    if [[ -x "$SCRIPT_DIR/install-ihp-em.sh" ]]; then
      log "Running EM installer (--with-em)"
      "$SCRIPT_DIR/install-ihp-em.sh"
    else
      warn "install-ihp-em.sh not found; skipping EM tier"
    fi
  fi
  print_summary
}

main "$@"
