#!/usr/bin/env bash

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

if [[ -f "${SCRIPT_DIR}/.pause_fundamentals" ]]; then
  echo "[stockey.pause] $(basename "$0") skipped: ${SCRIPT_DIR}/.pause_fundamentals exists ($(head -c 200 "${SCRIPT_DIR}/.pause_fundamentals"))"
  exit 0
fi

# Theme watchlist, daily (docs/THEMATIC_WATCHLIST_PRD.md build step 4): new SEBI enforcement
# orders, quarterly results for theme members outside the L1 crawl (capped per run: ~20 s a
# page through the screener.in gate), then theme strength and the late flag. Morning, so it
# reads the quarters last night's 21:00 IST screener crawl stored. One step failing does not
# stop the next; the exit code is non-zero if any failed.
failed=0
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "theme_sebi_orders" "${PYTHON_BIN}" -m fundamentals.collectors.sebi_orders || failed=1
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "theme_member_quarters" "${PYTHON_BIN}" -m fundamentals.themes.member_quarters --limit 60 || failed=1
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "theme_strength" "${PYTHON_BIN}" -m fundamentals.themes.strength || failed=1
exit "${failed}"
