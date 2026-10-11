#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Trigger study (fundamentals/trigger_study, systrader LEDGER row 56, docs/TRIGGER_STUDY.md).
# Usage: all_trigger_study.sh llm|docs|supervise -- three cron jobs, each every 15 minutes
# under ITS OWN lock, so no part waits for another (operator, 2026-10-11):
#   llm       AI tagging through the Claude CLI on the operator's plan, 22:00-08:00 IST only;
#             parks until the reset time when the plan's usage limit is hit; at most
#             TRIGGER_STUDY_MAX_CALLS calls a run.
#   docs      NSE work whenever NSE is quiet: attachments for whatever is tagged so far
#             (~90 a run; scans go to the research OCR queue that all_fundamentals_ocr.sh
#             reads), then any missing quarterly results.
#   supervise is each stage moving when it is allowed to? Emails the operator once when one
#             stalls for 45 min and once when it recovers.
export PATH="${HOME}/.local/bin:${PATH}"
PART="${1:?usage: all_trigger_study.sh llm|docs|supervise}"
shift
case "${PART}" in
  llm|docs)
    exec nice -n 10 "${SCRIPT_DIR}/scripts/run_with_markers.sh" "trigger_study_${PART}" \
      "${PYTHON_BIN}" -m fundamentals.trigger_study auto --part "${PART}" \
      --max-calls "${TRIGGER_STUDY_MAX_CALLS:-40}" "$@" ;;
  supervise)
    exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "trigger_study_supervise" \
      "${PYTHON_BIN}" -m fundamentals.trigger_study supervise "$@" ;;
  *) echo "unknown part: ${PART}" >&2; exit 2 ;;
esac
