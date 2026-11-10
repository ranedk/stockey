#!/usr/bin/env bash
# One autoscaler step (infra/fleet.py): grow or shrink the Hetzner (max 8) and RunPod (max 3)
# pools to the queue, within the monthly spend caps, and delete orphans. Run every minute.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"
cd "${SCRIPT_DIR}"
exec "${PYTHON_BIN}" -m infra.fleet tick
