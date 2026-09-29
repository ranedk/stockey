#!/usr/bin/env bash

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Event-driven re-evaluation (docs/FUNDAMENTAL_REEVALUATION_PRD.md 4.2), every 30 minutes:
# today's new BSE filings -> structured extraction -> rule triggers -> news tagging ->
# re-score the touched companies -> story read of material changes and substantive filings. Each step is incremental (pending rows / watermarks), so
# a quiet half hour costs a couple of requests. One step failing does not stop the next.
if [[ -f "${SCRIPT_DIR}/.pause_fundamentals" ]]; then
  echo "[stockey.pause] $(basename "$0") skipped: ${SCRIPT_DIR}/.pause_fundamentals exists ($(head -c 200 "${SCRIPT_DIR}/.pause_fundamentals"))"
  exit 0
fi
# The nightly screener runs the same steps over the same pending rows; stand aside for it.
SCREENER_PID_FILE="/tmp/stockey_fundamentals_screener.lock.d/pid"
if [[ -f "${SCREENER_PID_FILE}" ]] && kill -0 "$(cat "${SCREENER_PID_FILE}" 2>/dev/null)" 2>/dev/null; then
  echo "[stockey.reeval] skipped: the fundamentals screener is running"
  exit 0
fi

cd "${SCRIPT_DIR}"
failed=0
run_step() {
  "${SCRIPT_DIR}/scripts/run_with_markers.sh" "fundamentals_reeval:$1" "${PYTHON_BIN}" -m "${@:2}" || failed=1
}
run_step bse_intraday fundamentals.collectors.bse_announcements --intraday
run_step structured_extraction fundamentals.collectors.structured_extraction
run_step l3_triggers fundamentals.screens.l3_triggers
run_step news_tagging fundamentals.screens.news_tagging
run_step reeval fundamentals.screens.reeval
run_step story_read fundamentals.screens.story_read
run_step watchlist fundamentals.screens.watchlist
exit "${failed}"
