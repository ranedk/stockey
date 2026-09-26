#!/usr/bin/env bash
# Cron wrapper for scripts/ops_health_alert.py -- sets the PATH/interpreter cron does not
# have, same pattern as scripts/ensure_go_crond_alive.sh.
#
# MUST live in the OS-LEVEL user crontab (`crontab -e`), never in go-crond's generated
# crontab: go-crond being down is one of the two things this detects, so an alarm
# scheduled BY go-crond could not fire for the exact failure it exists to catch.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${SCRIPT_DIR}" || exit 2
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh" 2>/dev/null)" || PYTHON_BIN="${SCRIPT_DIR}/.xstockey/bin/python"
exec "${PYTHON_BIN}" -m scripts.ops_health_alert "$@"
