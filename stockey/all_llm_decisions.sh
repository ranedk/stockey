#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Daily review-only LLM decision cycle: generate a graded/sized decision per symbol over the active
# universe (as of the latest advisory technical date), persist them to advisory_llm_decisions, and
# mature any decisions whose horizon elapsed into advisory_llm_decision_outcomes (feeds the monitor).
#
# Deterministic by default: LLM_DECISION_CRON_ARGS defaults to --no-llm so cron never makes LLM API
# calls unless a deployment deliberately opts in (set LLM_DECISION_CRON_ARGS="" and configure a model).
# Nothing here moves capital: broker_execution_allowed is always False.

exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "llm_decisions" "${PYTHON_BIN}" -m advisory.llm_decision_runner \
  --persist --label-outcomes ${LLM_DECISION_CRON_ARGS:---no-llm} "$@"
