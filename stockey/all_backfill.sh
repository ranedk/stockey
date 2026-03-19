#!/usr/bin/env bash

set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-/home/rane/code/stockey/.xstockey/bin/python}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODE="${1:-watchlist}"
YEARS="${2:-5}"
WATCHLIST_FILE="${WATCHLIST_FILE:-${REPO_ROOT}/config/watchlist_symbols.txt}"
TRACKED_FILE="${TRACKED_FILE:-${REPO_ROOT}/config/tracked_symbols.txt}"
TRUNCATE_DERIVED="${TRUNCATE_DERIVED:-0}"

load_symbols_file() {
    local file_path="$1"
    if [[ ! -f "${file_path}" ]]; then
        return 0
    fi
    {
        grep -v '^[[:space:]]*#' "${file_path}" || true
    } | sed '/^[[:space:]]*$/d' | paste -sd, -
}

FROM_DATE="$(date -u -d "${YEARS} years ago" +%F)"
TO_DATE="$(date -u +%F)"
SYMBOLS=""

case "${MODE}" in
    watchlist)
        SYMBOLS="$(load_symbols_file "${WATCHLIST_FILE}")"
        if [[ -z "${SYMBOLS}" ]]; then
            SYMBOLS="$(load_symbols_file "${TRACKED_FILE}")"
        fi
        ;;
    tracked)
        SYMBOLS="$(load_symbols_file "${TRACKED_FILE}")"
        ;;
    all)
        ;;
    *)
        echo "Usage: $0 [watchlist|tracked|all] [years]" >&2
        exit 1
        ;;
esac

if [[ "${MODE}" != "all" && -z "${SYMBOLS}" ]]; then
    echo "No symbols available for mode ${MODE}" >&2
    exit 1
fi

if [[ "${TRUNCATE_DERIVED}" == "1" ]]; then
    "${PYTHON_BIN}" scripts/sql_query_runner.py \
        "TRUNCATE TABLE nseindia_corporate_actions_normalized, nseindia_ohlcv_adjusted, features_price_daily"
fi

if [[ -n "${SYMBOLS}" ]]; then
    export STOCKEY_SYMBOLS="${SYMBOLS}"
fi

"${PYTHON_BIN}" -m data.sharpelydata.sharpely_data --from-date "${FROM_DATE}" --to-date "${TO_DATE}"
"${PYTHON_BIN}" -m data.nseindia.corporate_actions --from-date "${FROM_DATE}" --to-date "${TO_DATE}"
"${PYTHON_BIN}" -m data.nseindia.earnings_events --from-date "${FROM_DATE}" --to-date "${TO_DATE}"
"${PYTHON_BIN}" -m data.nseindia.insider_deals --from-date "${FROM_DATE}" --to-date "${TO_DATE}"
"${PYTHON_BIN}" -m data.nseindia.security_history
"${PYTHON_BIN}" -m data.nseindia.security_dimension

if [[ -n "${SYMBOLS}" ]]; then
    "${PYTHON_BIN}" -m data.nseindia.adjusted_prices --symbols "${SYMBOLS}" --only all
else
    "${PYTHON_BIN}" -m data.nseindia.adjusted_prices --only all
fi

"${PYTHON_BIN}" -m features.price_daily
