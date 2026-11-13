#!/usr/bin/env bash
# Platform serving tables (PLATFORM_PRD.md, task S3): clean financials, then one row per
# company. Run from the OS crontab, not go-crond, so adding it never needs builder.py or a
# go-crond restart. The company step reads stages from the systrader API (:8090), which
# reads the local DB synced at 15:00 UTC -- hence the 16:00 UTC slot. Idempotent: each step
# builds a new table and swaps it in, so a failed run leaves yesterday's table serving.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "serving_financials" "${PYTHON_BIN}" -m serving.financials || exit 1
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "serving_company" "${PYTHON_BIN}" -m serving.company
