#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Read-only completeness gate. Deliberately browser-free and broker-free (see the
# module docstring): the monitor must not share the collectors' dependencies, which
# is how the 2026-08-26..31 outage stayed invisible -- data_readiness was crashing on
# the same dead-Chrome CDP timeout as the collectors it was supposed to be watching.
# Exits non-zero on a hard error, which run_with_markers surfaces as status=failed.

# Judge the night's data only after the morning repair has finished. data_readiness --fix
# starts at 02:00 UTC and pulls the previous session's Dhan bars until ~03:00; the gate
# fired at 02:30 mid-fetch and reported a "collapsed" feed on 2026-10-06 and 10-08 (387 and
# 549 of ~3,360 tickers) for data that was complete 30 minutes later.
"${SCRIPT_DIR}/scripts/wait_for_locks.sh" --timeout-seconds 5400 --sleep-seconds 30 \
    /tmp/stockey_data_readiness.lock || echo "[data_completeness] data_readiness lock still held after 90 min; checking anyway"
exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "data_completeness" \
  "${PYTHON_BIN}" -m scripts.data_completeness "$@"
