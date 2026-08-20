#!/bin/bash
set -euo pipefail
source /home/ubuntu/.local/share/ihp-eda/env.sh
export QT_QPA_PLATFORM=offscreen
cd /workspace
python layout/blocks/driver_stage.py "$@"
