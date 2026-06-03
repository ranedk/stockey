#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"
MAX_TASKS="${EXTERNAL_WORKER_MAX_TASKS:-50}"

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "external_worker_dhan" "${PYTHON_BIN}" -m advisory.external_task_queue --queue dhan --worker --drain --max-tasks "${MAX_TASKS}"
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "external_worker_screener" "${PYTHON_BIN}" -m advisory.external_task_queue --queue screener --worker --drain --max-tasks "${MAX_TASKS}"
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "external_worker_nse" "${PYTHON_BIN}" -m advisory.external_task_queue --queue nse --worker --drain --max-tasks "${MAX_TASKS}"
