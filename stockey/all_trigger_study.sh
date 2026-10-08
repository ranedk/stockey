#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Trigger study tagger (fundamentals/trigger_study, systrader LEDGER row 56), 2026-10-08.
# Tags filings through the Claude CLI on the operator's plan -- no API spend. Cron starts it
# every 15 minutes under a lock. Each run:
#   - exits at once while parked: when the plan's usage limit is hit the runner records the
#     reset time and every run until then is a no-op, so the work resumes by itself;
#   - calls only 22:00-08:00 IST, so it does not use the operator's daytime allowance;
#   - stops after TRIGGER_STUDY_MAX_CALLS calls; the next run picks up the queue.
# Nothing is queued unless someone ran `python -m fundamentals.trigger_study queue`, so an
# idle queue costs one DB read per run.
export PATH="${HOME}/.local/bin:${PATH}"
exec nice -n 10 "${SCRIPT_DIR}/scripts/run_with_markers.sh" "trigger_study" \
  "${PYTHON_BIN}" -m fundamentals.trigger_study auto --max-calls "${TRIGGER_STUDY_MAX_CALLS:-40}" "$@"
