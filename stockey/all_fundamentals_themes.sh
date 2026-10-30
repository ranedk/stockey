#!/usr/bin/env bash

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

if [[ -f "${SCRIPT_DIR}/.pause_fundamentals" ]]; then
  echo "[stockey.pause] $(basename "$0") skipped: ${SCRIPT_DIR}/.pause_fundamentals exists ($(head -c 200 "${SCRIPT_DIR}/.pause_fundamentals"))"
  exit 0
fi

# Theme watchlist, daily (docs/THEMATIC_WATCHLIST_PRD.md build steps 4-6): new SEBI enforcement
# orders, revenue shares for members without one (Claude CLI), theme strength and the late flag, the ranked watchlist (reads systrader's theme
# snapshot from last night and the parties of any newly matched SEBI order), the written
# reasons (Claude CLI -- inside the 22:00-08:00 IST night window, hence the 06:15 start), and
# last, quarterly results for theme members outside the L1 crawl (capped: ~40 s a page through
# the screener.in gate). One step failing does not stop the next; the exit code is non-zero if
# any failed.
failed=0
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "theme_sebi_orders" "${PYTHON_BIN}" -m fundamentals.collectors.sebi_orders || failed=1
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "theme_share_finder" "${PYTHON_BIN}" -m fundamentals.themes.share_finder || failed=1
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "theme_strength" "${PYTHON_BIN}" -m fundamentals.themes.strength || failed=1
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "theme_watchlist" "${PYTHON_BIN}" -m fundamentals.themes.watchlist || failed=1
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "theme_text" "${PYTHON_BIN}" -m fundamentals.themes.theme_text || failed=1
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "theme_member_quarters" "${PYTHON_BIN}" -m fundamentals.themes.member_quarters --limit 60 || failed=1
exit "${failed}"
