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
