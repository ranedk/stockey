#!/usr/bin/env bash

set -euo pipefail

CDP_HOST="${CDP_HOST:-127.0.0.1}"
CDP_PORT="${CDP_PORT:-9222}"
CHROME_USER_DATA_DIR="${CHROME_USER_DATA_DIR:-${HOME}/.stockey/chrome-cdp}"
CHROME_BINARY="${CHROME_BINARY:-}"

find_chrome_binary() {
  if [[ -n "${CHROME_BINARY}" ]]; then
    printf "%s\n" "${CHROME_BINARY}"
    return 0
  fi
  for candidate in \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    "/Applications/Chromium.app/Contents/MacOS/Chromium" \
    "google-chrome" \
    "google-chrome-stable" \
    "chromium" \
    "chromium-browser"; do
    if [[ -x "${candidate}" ]]; then
      printf "%s\n" "${candidate}"
      return 0
    fi
    if command -v "${candidate}" >/dev/null 2>&1; then
      command -v "${candidate}"
      return 0
    fi
  done
  return 1
}

usage() {
  cat <<'EOF'
Usage: scripts/start_chrome_cdp.sh

Starts Chrome/Chromium with remote debugging enabled for Screener.in and Dhan browser automation.

Environment:
  CDP_HOST                Default: 127.0.0.1
  CDP_PORT                Default: 9222
  CHROME_USER_DATA_DIR    Default: ~/.stockey/chrome-cdp
  CHROME_BINARY           Optional explicit Chrome/Chromium binary

After starting, set:
  CDP_ENDPOINT=http://localhost:9222
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

chrome_binary="$(find_chrome_binary || true)"
if [[ -z "${chrome_binary}" ]]; then
  echo "[start_chrome_cdp] Chrome/Chromium binary not found. Set CHROME_BINARY." >&2
  exit 1
fi

mkdir -p "${CHROME_USER_DATA_DIR}"
echo "[start_chrome_cdp] starting ${chrome_binary}"
echo "[start_chrome_cdp] CDP endpoint: http://${CDP_HOST}:${CDP_PORT}"
echo "[start_chrome_cdp] user data dir: ${CHROME_USER_DATA_DIR}"

exec "${chrome_binary}" \
  --remote-debugging-address="${CDP_HOST}" \
  --remote-debugging-port="${CDP_PORT}" \
  --user-data-dir="${CHROME_USER_DATA_DIR}" \
  --no-first-run \
  --no-default-browser-check
