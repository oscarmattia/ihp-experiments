#!/bin/bash
# Run 56G CML RX front-end: termination → CTLE → VGA (+ chain hook).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CTLE="$ROOT/circuits/ctle56n"
source ~/.local/share/ihp-eda/env.sh
PY="${IHP_EDA_ROOT}/venv/bin/python"

RUN_TERM=1
RUN_CTLE=1
RUN_VGA=1
RUN_CHAIN=0
POSTLAYOUT_DUT=""
NO_TRAN=""
NO_PDK=""

usage() {
  cat <<'EOF'
Usage: circuits/ctle56n/run.sh [OPTIONS]

Run the full 56G NRZ RX front-end (non-interactive):
  termination (stage_term) → CTLE size+sims → VGA (stage_vga)

Options:
  --no-tran       Skip PRBS/SBR transients on all stages
  --no-pdk        Skip PDK passive passes (CTLE pdk + vga_pdk)
  --no-term       Skip termination stage
  --no-ctle       Skip CTLE size + run_sims.py
  --no-vga        Skip VGA stage
  --with-chain    Run chain stage (term → CTLE → VGA cascade)
  --with-postlayout PATH
                  Run post-layout CTLE pass (stage_postlayout.py) on PATH
  -h, --help      Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-tran) NO_TRAN="--no-tran"; shift ;;
    --no-pdk) NO_PDK="--no-pdk"; shift ;;
    --no-term) RUN_TERM=0; shift ;;
    --no-ctle) RUN_CTLE=0; shift ;;
    --no-vga) RUN_VGA=0; shift ;;
    --with-chain) RUN_CHAIN=1; shift ;;
    --with-postlayout)
      if [[ $# -lt 2 ]]; then
        echo "error: --with-postlayout requires a DUT netlist path" >&2
        usage
        exit 1
      fi
      POSTLAYOUT_DUT="$2"
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ "$RUN_TERM" -eq 1 ]]; then
  echo "=== Termination stage ==="
  "$PY" "$CTLE/python/stage_term.py" $NO_TRAN
fi

if [[ "$RUN_CTLE" -eq 1 ]]; then
  echo "=== CTLE sizing + sims ==="
  "$PY" "$CTLE/python/size_ctle.py"
  "$PY" "$CTLE/python/run_sims.py" --no-iterate $NO_TRAN $NO_PDK
fi

if [[ "$RUN_VGA" -eq 1 ]]; then
  echo "=== VGA stage ==="
  VGA_ARGS=()
  [[ -n "$NO_TRAN" ]] && VGA_ARGS+=(--no-tran)
  [[ -n "$NO_PDK" ]] && VGA_ARGS+=(--no-pdk)
  "$PY" "$CTLE/python/stage_vga.py" "${VGA_ARGS[@]}"
  if [[ -z "$NO_TRAN" ]]; then
    echo "=== VGA large-signal / eye analysis ==="
    "$PY" "$CTLE/python/vga_analysis.py" --pass-name vga_ideal --dut vga_ideal.cir
    if [[ -z "$NO_PDK" ]]; then
      "$PY" "$CTLE/python/vga_analysis.py" --pass-name vga_pdk --dut vga_pdk.cir
    fi
  fi
fi

if [[ -n "$POSTLAYOUT_DUT" ]]; then
  echo "=== Post-layout CTLE pass ==="
  POST_ARGS=(--dut "$POSTLAYOUT_DUT" --pass-name postlayout)
  [[ -n "$NO_TRAN" ]] && POST_ARGS+=(--no-tran)
  "$PY" "$CTLE/python/stage_postlayout.py" "${POST_ARGS[@]}"
fi

if [[ "$RUN_CHAIN" -eq 1 ]]; then
  if [[ -f "$CTLE/python/stage_chain.py" ]]; then
    echo "=== Chain stage (optional) ==="
    CHAIN_ARGS=()
    [[ -n "$NO_TRAN" ]] && CHAIN_ARGS+=(--no-tran)
    [[ -n "$NO_PDK" ]] && CHAIN_ARGS+=(--no-pdk)
    "$PY" "$CTLE/python/stage_chain.py" "${CHAIN_ARGS[@]}"
  else
    echo "NOTE: --with-chain set but python/stage_chain.py not present; skipping"
  fi
fi

echo "=== Summary aggregate ==="
"$PY" "$CTLE/python/run_sims.py" --aggregate-summary

echo "=== Design reports (from committed out/) ==="
"$PY" "$CTLE/python/generate_reports.py"

echo "Done. Artifacts under $CTLE/out/"
