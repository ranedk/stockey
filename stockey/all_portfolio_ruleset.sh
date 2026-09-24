#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Portfolio ruleset + LLM adjudication (docs/PORTFOLIO_RULESET_PRD.md).
#
# EXITS RUN BEFORE ENTRIES, and the order is not arbitrary: a name that exits today may
# also satisfy the entry rule today (stage 2 with no contradicting axis is exactly what a
# target_date exit looks like). Entering first would open a duplicate position that the
# exit pass then closes, leaving a one-day round trip in the record that never happened.
# Exiting first means the re-entry, if the rule still wants it, is tomorrow's decision
# with tomorrow's evidence.
#
# Neither half opens real positions unless --live is passed; the default is record-only
# and the crontab deliberately does NOT pass --live (rollout phase 1, see the PRD).
#
# WAIT FOR THE SCREENER FIRST (found 2026-09-23). The screener starts 15:30 UTC and ran
# 65-109 minutes on the last six weekdays; this job starts 16:30 UTC, so on five of those
# six days it decided on a half-refreshed mix -- today's L2 beside yesterday's confluence
# scores and technicals (confluence/technicals are near the END of the pipeline). A lock
# wait orders the two without touching the crontab. On timeout this exits non-zero and
# nothing below runs: deciding on stale evidence is worse than deciding a day late.
# 5h: OCR alone may use FUNDAMENTALS_OCR_MAX_RUNTIME_SECONDS (3h) on a heavy day, on top
# of ~1h of steps before it and ~15 min after.
"${SCRIPT_DIR}/scripts/wait_for_locks.sh" --timeout-seconds 18000 --sleep-seconds 60 \
    /tmp/stockey_fundamentals_screener.lock
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "portfolio_exit" \
    "${PYTHON_BIN}" -m fundamentals.screens.portfolio_exit
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "portfolio_ruleset" \
    "${PYTHON_BIN}" -m fundamentals.screens.portfolio_runner "$@"

# Forecast resolution runs LAST and is independent of both: a forecast resolves at its
# target date whether or not the position is still open. A name stopped out in month two
# still has a forecast to grade, and keeping the two separate is the only thing that
# distinguishes "the fundamental call was wrong" from "it was right and the market
# hasn't paid" -- which the source spec notes look identical in P&L and demand opposite
# corrections. There is no human resolver any more (2026-09-04), so if this does not run,
# nothing ever resolves and the scoring reports "0 resolved" forever without failing.
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "portfolio_resolution" \
    "${PYTHON_BIN}" -m fundamentals.screens.portfolio_resolution

# The action email goes LAST, once entries, exits and resolutions have all been decided --
# it reports what this run did. It must not move into the 21:00 screener pipeline: the
# decisions are not made until this job, so an email sent from there would report
# yesterday's actions every night while looking correct. It sends even when nothing
# happened, so that a quiet day and a broken pipeline are distinguishable from the inbox.
exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "portfolio_notify" \
    "${PYTHON_BIN}" -m fundamentals.screens.portfolio_notify
