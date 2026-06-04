#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

API_HOST="${OPERATOR_API_HOST:-127.0.0.1}"
API_PORT="${OPERATOR_API_PORT:-8765}"
WEB_HOST="${OPERATOR_WEB_HOST:-127.0.0.1}"
WEB_PORT="${OPERATOR_WEB_PORT:-3000}"
API_BASE="${NUXT_PUBLIC_API_BASE:-http://${API_HOST}:${API_PORT}}"
API_TIMEOUT_MS="${NUXT_PUBLIC_API_TIMEOUT_MS:-12000}"
WEB_DIR="${SCRIPT_DIR}/apps/operator-web"
INSTALL_DEPS="${OPERATOR_WEB_INSTALL_DEPS:-auto}"
USE_NVM="${OPERATOR_WEB_USE_NVM:-true}"
NVM_DIR="${NVM_DIR:-${HOME}/.nvm}"
NVM_VERSION="${OPERATOR_WEB_NVM_VERSION:-default}"
NPM_LEGACY_PEER_DEPS="${OPERATOR_WEB_NPM_LEGACY_PEER_DEPS:-true}"
NPM_INSTALL_ARGS="${OPERATOR_WEB_NPM_INSTALL_ARGS:-}"
HEALTH_INTERVAL_SECONDS="${OPERATOR_FRONTEND_HEALTH_INTERVAL_SECONDS:-30}"
HEALTH_FAILURE_LIMIT="${OPERATOR_FRONTEND_HEALTH_FAILURE_LIMIT:-3}"
HEALTH_STARTUP_GRACE_SECONDS="${OPERATOR_FRONTEND_HEALTH_STARTUP_GRACE_SECONDS:-45}"
HEALTH_TIMEOUT_SECONDS="${OPERATOR_FRONTEND_HEALTH_TIMEOUT_SECONDS:-5}"

if [[ ! -d "${WEB_DIR}" ]]; then
  echo "[all_frontend] missing operator web directory: ${WEB_DIR}" >&2
  exit 1
fi

if [[ "${USE_NVM}" == "1" || "${USE_NVM}" == "true" || "${USE_NVM}" == "auto" ]]; then
  if [[ -s "${NVM_DIR}/nvm.sh" ]]; then
    # shellcheck disable=SC1090
    . "${NVM_DIR}/nvm.sh"
    nvm use "${NVM_VERSION}" >/dev/null
    echo "[all_frontend] using node=$(command -v node) version=$(node --version) npm=$(npm --version)"
  elif [[ "${USE_NVM}" == "1" || "${USE_NVM}" == "true" ]]; then
    echo "[all_frontend] nvm requested but not found at ${NVM_DIR}/nvm.sh" >&2
    exit 1
  fi
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "[all_frontend] npm is required to run the Nuxt operator frontend" >&2
  exit 1
fi

install_frontend_deps() {
  local -a install_args=()
  if [[ -n "${NPM_INSTALL_ARGS}" ]]; then
    # shellcheck disable=SC2206
    install_args=(${NPM_INSTALL_ARGS})
  fi
  (cd "${WEB_DIR}" && npm install "${install_args[@]}")
}

install_frontend_deps_legacy() {
  local -a install_args=()
  if [[ -n "${NPM_INSTALL_ARGS}" ]]; then
    # shellcheck disable=SC2206
    install_args=(${NPM_INSTALL_ARGS})
  fi
  (cd "${WEB_DIR}" && npm install --legacy-peer-deps "${install_args[@]}")
}

if [[ "${INSTALL_DEPS}" == "1" || "${INSTALL_DEPS}" == "true" || ( "${INSTALL_DEPS}" == "auto" && ! -d "${WEB_DIR}/node_modules" ) ]]; then
  echo "[all_frontend] installing frontend dependencies"
  if ! install_frontend_deps; then
    if [[ "${NPM_LEGACY_PEER_DEPS}" == "1" || "${NPM_LEGACY_PEER_DEPS}" == "true" ]]; then
      echo "[all_frontend] npm install failed; retrying with --legacy-peer-deps"
      install_frontend_deps_legacy
    else
      exit 1
    fi
  fi
fi

api_pid=""
web_pid=""

cleanup() {
  if [[ -n "${web_pid}" ]] && kill -0 "${web_pid}" >/dev/null 2>&1; then
    kill "${web_pid}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${api_pid}" ]] && kill -0 "${api_pid}" >/dev/null 2>&1; then
    kill "${api_pid}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

echo "[all_frontend] starting operator API host=${API_HOST} port=${API_PORT}"
"${PYTHON_BIN}" -m advisory.api.app --host "${API_HOST}" --port "${API_PORT}" &
api_pid="$!"

sleep 2
if ! kill -0 "${api_pid}" >/dev/null 2>&1; then
  echo "[all_frontend] operator API failed to start" >&2
  exit 1
fi

echo "[all_frontend] starting Nuxt operator frontend host=${WEB_HOST} port=${WEB_PORT} api=${API_BASE}"
(
  cd "${WEB_DIR}"
  NUXT_PUBLIC_API_BASE="${API_BASE}" NUXT_PUBLIC_API_TIMEOUT_MS="${API_TIMEOUT_MS}" npm run dev -- --host "${WEB_HOST}" --port "${WEB_PORT}"
) &
web_pid="$!"

health_check_url() {
  local url="$1"
  curl -fsS --max-time "${HEALTH_TIMEOUT_SECONDS}" "${url}" >/dev/null 2>&1
}

started_at="$(date +%s)"
next_health_check=$((started_at + HEALTH_STARTUP_GRACE_SECONDS))
health_failures=0
status="0"
while true; do
  if ! kill -0 "${api_pid}" >/dev/null 2>&1; then
    wait "${api_pid}" || status="$?"
    break
  fi
  if ! kill -0 "${web_pid}" >/dev/null 2>&1; then
    wait "${web_pid}" || status="$?"
    break
  fi
  now="$(date +%s)"
  if (( now >= next_health_check )); then
    api_ok=0
    web_ok=0
    if health_check_url "http://${API_HOST}:${API_PORT}/api/health"; then
      api_ok=1
    fi
    if health_check_url "http://${WEB_HOST}:${WEB_PORT}/"; then
      web_ok=1
    fi
    if (( api_ok == 1 && web_ok == 1 )); then
      health_failures=0
    else
      health_failures=$((health_failures + 1))
      echo "[all_frontend] health_check failed api_ok=${api_ok} web_ok=${web_ok} failures=${health_failures}/${HEALTH_FAILURE_LIMIT}" >&2
      if (( health_failures >= HEALTH_FAILURE_LIMIT )); then
        status="1"
        break
      fi
    fi
    next_health_check=$((now + HEALTH_INTERVAL_SECONDS))
  fi
  sleep 2
done
echo "[all_frontend] one frontend process exited status=${status}; stopping remaining process"
exit "${status}"
