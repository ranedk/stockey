#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# OPERATOR PAUSE (2026-09-25): while the universe rebuild (docs/UNIVERSE_PRD.md) is in
# progress, the fundamentals jobs are paused by creating .pause_fundamentals at the repo
# root. Price/data jobs keep running. Delete the file to resume -- no crontab edit (a
# rewrite under a live go-crond silently stops all scheduling).
if [[ -f "${SCRIPT_DIR}/.pause_fundamentals" ]]; then
  echo "[stockey.pause] $(basename "$0") skipped: ${SCRIPT_DIR}/.pause_fundamentals exists ($(head -c 200 "${SCRIPT_DIR}/.pause_fundamentals"))"
  exit 0
fi

# Fundamental screener pipeline (docs/FUNDAMENTAL_SCREENER_PRD.md): L1/L2 refresh,
# event collectors, OCR + structured extraction, sector capital-cycle, L3 alerts
# (rule + LLM triage), descriptive technicals, and the watchlist/narrative/email
# pipeline -- run in dependency order by fundamentals/run_pipeline.py. One step
# failing does not abort the run (see that module's docstring); this script's own
# exit code only goes non-zero if every step failed.
# A 30-minute re-evaluation pass (all_fundamentals_reeval.sh, :17/:47) runs the same
# extraction / rule-trigger / tagging steps over the same pending rows; it stands aside once
# this job holds its lock, but one started at 15:17 UTC may still be running -- wait for it
# (45 min cap; on timeout the pipeline runs anyway, as it did before the pass existed).
"${SCRIPT_DIR}/scripts/wait_for_locks.sh" --timeout-seconds 2700 --sleep-seconds 30 \
    /tmp/stockey_fundamentals_reeval.lock || echo "[fundamentals_screener] re-evaluation lock still held after 45 min; running anyway"
exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "fundamentals_screener" "${PYTHON_BIN}" -m fundamentals.run_pipeline "$@"
