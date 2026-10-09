#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Trigger study tagger (fundamentals/trigger_study, systrader LEDGER row 56), 2026-10-08.
# Tags filings through the Claude CLI on the operator's plan -- no API spend. Cron starts it
# every 15 minutes under a lock. Each run has two independent halves:
#   - AI tagging: only 22:00-08:00 IST (the operator's daytime allowance is left alone); parks
#     until the reset time when the plan's usage limit is hit; at most TRIGGER_STUDY_MAX_CALLS;
#   - attachments for whatever is tagged so far: any time NSE is quiet (~90 per run). Scans go
#     to the research OCR queue, which all_fundamentals_ocr.sh reads whenever no filing waits.
# Nothing is queued unless someone ran `python -m fundamentals.trigger_study queue`, so an
# idle queue costs one DB read per run.
export PATH="${HOME}/.local/bin:${PATH}"
exec nice -n 10 "${SCRIPT_DIR}/scripts/run_with_markers.sh" "trigger_study" \
  "${PYTHON_BIN}" -m fundamentals.trigger_study auto --max-calls "${TRIGGER_STUDY_MAX_CALLS:-40}" "$@"
