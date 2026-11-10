#!/usr/bin/env bash
# Scale-out orchestrator (infra/orchestrator.py, docs/SCALE_OUT_PRD.md): workers on Hetzner and
# RunPod call it through nginx at /orchestrator/. Kept alive like all_fundamentals_api.sh: run
# under scripts/with_lock.sh, and a no-op when something already answers /health.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"
PORT="${INFRA_ORCH_PORT:-8100}"
if "${PYTHON_BIN}" -c "
import sys, urllib.request
try:
    urllib.request.urlopen('http://127.0.0.1:${PORT}/health', timeout=2)
except Exception:
    sys.exit(1)
"; then
  exit 0
fi
cd "${SCRIPT_DIR}"
exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "infra_orchestrator" "${PYTHON_BIN}" -m uvicorn infra.orchestrator:app --host 127.0.0.1 --port "${PORT}"
