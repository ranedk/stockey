#!/usr/bin/env bash

set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-./.xstockey/bin/python}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCHLIST_FILE="${WATCHLIST_FILE:-${REPO_ROOT}/config/watchlist_symbols.txt}"
TRACKED_FILE="${TRACKED_FILE:-${REPO_ROOT}/config/tracked_symbols.txt}"

load_symbols_file() {
    local file_path="$1"
    if [[ ! -f "${file_path}" ]]; then
        return 0
    fi
    {
        grep -v '^[[:space:]]*#' "${file_path}" || true
    } | sed '/^[[:space:]]*$/d' | paste -sd, -
}

WATCHLIST_SYMBOLS="$(load_symbols_file "${WATCHLIST_FILE}")"
if [[ -z "${WATCHLIST_SYMBOLS}" ]]; then
    WATCHLIST_SYMBOLS="$(load_symbols_file "${TRACKED_FILE}")"
fi

if [[ -z "${WATCHLIST_SYMBOLS}" ]]; then
    echo "No symbols found in ${WATCHLIST_FILE} or ${TRACKED_FILE}" >&2
    exit 1
fi

export STOCKEY_SYMBOLS="${WATCHLIST_SYMBOLS}"

"${PYTHON_BIN}" -m features.calendar_creator
"${PYTHON_BIN}" -m data.nseindia.security_history
"${PYTHON_BIN}" -m data.nseindia.security_dimension
"${PYTHON_BIN}" -m data.nseindia.adjusted_prices --symbols "${STOCKEY_SYMBOLS}" --only all
"${PYTHON_BIN}" -m features.price_daily
