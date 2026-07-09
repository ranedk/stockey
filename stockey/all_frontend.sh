#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

COMPONENT="${OPERATOR_FRONTEND_COMPONENT:-both}"
API_HOST="${OPERATOR_API_HOST:-127.0.0.1}"
API_PORT="${OPERATOR_API_PORT:-8085}"
WEB_HOST="${OPERATOR_WEB_HOST:-127.0.0.1}"
WEB_PORT="${OPERATOR_WEB_PORT:-3035}"
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
restart_on_code_change="${OPERATOR_FRONTEND_RESTART_ON_CODE_CHANGE:-true}"
code_check_seconds="${OPERATOR_FRONTEND_CODE_CHECK_SECONDS:-60}"
script_marker_started=0

emit_script_marker() {
  local marker_status="$1"
  local exit_code="${2:-}"
  local timestamp
  timestamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  if [[ -n "${exit_code}" ]]; then
    echo "[stockey.script] name=all_frontend status=${marker_status} exit_code=${exit_code} timestamp=${timestamp}"
  else
    echo "[stockey.script] name=all_frontend status=${marker_status} timestamp=${timestamp}"
  fi
}

script_status_for_exit_code() {
  local exit_code="$1"
  if [[ "${exit_code}" == "130" || "${exit_code}" == "143" ]]; then
    printf "interrupted"
  elif [[ "${exit_code}" == "75" ]]; then
    printf "restart_requested"
  elif [[ "${exit_code}" == "0" ]]; then
    printf "done"
  else
    printf "failed"
  fi
}

usage() {
  cat <<'EOF'
Usage: ./all_frontend.sh [--both|--api-only|--web-only]

Modes:
  --both      Start and supervise the operator API plus Nuxt frontend. Default.
  --api-only Start and supervise only the FastAPI operator API.
  --web-only Start and supervise only the Nuxt frontend. Requires an existing API at NUXT_PUBLIC_API_BASE.

Environment alternative:
  OPERATOR_FRONTEND_COMPONENT=both|api|web
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --both)
      COMPONENT="both"
      ;;
    --api-only)
      COMPONENT="api"
      ;;
    --web-only)
      COMPONENT="web"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[all_frontend] unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

case "${COMPONENT}" in
  both|api|web)
    ;;
  api-only)
    COMPONENT="api"
    ;;
  web-only)
    COMPONENT="web"
    ;;
  *)
    echo "[all_frontend] invalid OPERATOR_FRONTEND_COMPONENT=${COMPONENT}; expected both, api, or web" >&2
    exit 2
    ;;
esac

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

prepare_web_runtime() {
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
}

api_pid=""
web_pid=""

cleanup() {
  local exit_code="$?"
  if [[ -n "${web_pid}" ]] && kill -0 "${web_pid}" >/dev/null 2>&1; then
    kill "${web_pid}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${api_pid}" ]] && kill -0 "${api_pid}" >/dev/null 2>&1; then
    kill "${api_pid}" >/dev/null 2>&1 || true
  fi
  if [[ "${script_marker_started}" == "1" ]]; then
    emit_script_marker "$(script_status_for_exit_code "${exit_code}")" "${exit_code}"
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

start_api() {
  echo "[all_frontend] starting operator API host=${API_HOST} port=${API_PORT}"
  "${PYTHON_BIN}" -m advisory.api.app --host "${API_HOST}" --port "${API_PORT}" &
  api_pid="$!"

  sleep 2
  if ! kill -0 "${api_pid}" >/dev/null 2>&1; then
    echo "[all_frontend] operator API failed to start" >&2
    exit 1
  fi
}

start_web() {
  prepare_web_runtime
  echo "[all_frontend] starting Nuxt operator frontend host=${WEB_HOST} port=${WEB_PORT} api=${API_BASE}"
  (
    cd "${WEB_DIR}"
    NUXT_PUBLIC_API_BASE="${API_BASE}" NUXT_PUBLIC_API_TIMEOUT_MS="${API_TIMEOUT_MS}" npm run dev -- --host "${WEB_HOST}" --port "${WEB_PORT}"
  ) &
  web_pid="$!"

  sleep 2
  if ! kill -0 "${web_pid}" >/dev/null 2>&1; then
    echo "[all_frontend] Nuxt frontend failed to start" >&2
    exit 1
  fi
}

health_check_url() {
  local url="$1"
  curl -fsS --max-time "${HEALTH_TIMEOUT_SECONDS}" "${url}" >/dev/null 2>&1
}

frontend_code_signature() {
  "${PYTHON_BIN}" - "${SCRIPT_DIR}" <<'PY'
import hashlib
import os
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
paths = [
    root / "all_frontend.sh",
    root / "advisory" / "api",
    root / "advisory" / "operator_health.py",
    root / "advisory" / "operator_snapshot.py",
    root / "advisory" / "ts_forecast_promotion_check.py",
    root / "apps" / "operator-web",
]
ignored_dirs = {
    ".git",
    ".nuxt",
    ".output",
    "__pycache__",
    "coverage",
    "dist",
    "node_modules",
}
digest = hashlib.sha256()
for base in paths:
    if not base.exists():
        digest.update(f"missing\0{base.relative_to(root)}\0".encode())
        continue
    if base.is_file():
        files = [base]
    else:
        files = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [name for name in dirnames if name not in ignored_dirs]
            current = Path(dirpath)
            for filename in filenames:
                files.append(current / filename)
    for path in sorted(files, key=lambda item: str(item.relative_to(root))):
        try:
            stat = path.stat()
        except OSError:
            digest.update(f"stat_error\0{path.relative_to(root)}\0".encode())
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(str(stat.st_mtime_ns).encode())
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode())
        digest.update(b"\0")
print(digest.hexdigest())
PY
}

emit_script_marker "start"
script_marker_started=1

# Refuse to fight an already-healthy instance: if every component this run would start is
# already serving on its port, exit cleanly (a second supervisor otherwise crash-loops on
# EADDRINUSE every respawn -- e.g. a manual ./all_frontend.sh alongside the cron entry).
already_healthy=1
if [[ "${COMPONENT}" == "both" || "${COMPONENT}" == "api" ]]; then
  health_check_url "http://${API_HOST}:${API_PORT}/api/health" || already_healthy=0
fi
if [[ "${COMPONENT}" == "both" || "${COMPONENT}" == "web" ]]; then
  health_check_url "http://${WEB_HOST}:${WEB_PORT}/" || already_healthy=0
fi
if [[ "${already_healthy}" == "1" ]]; then
  echo "[all_frontend] already_running: healthy instance detected on api=${API_HOST}:${API_PORT} web=${WEB_HOST}:${WEB_PORT}; exiting instead of competing"
  exit 0
fi

echo "[all_frontend] mode=${COMPONENT}"
if [[ "${COMPONENT}" == "both" || "${COMPONENT}" == "api" ]]; then
  start_api
fi
if [[ "${COMPONENT}" == "both" || "${COMPONENT}" == "web" ]]; then
  start_web
fi

started_at="$(date +%s)"
next_health_check=$((started_at + HEALTH_STARTUP_GRACE_SECONDS))
next_code_check=$((started_at + code_check_seconds))
health_failures=0
status="0"
initial_code_signature=""
if [[ "${restart_on_code_change}" == "1" || "${restart_on_code_change}" == "true" ]]; then
  initial_code_signature="$(frontend_code_signature)"
  echo "[all_frontend] code_change_restart enabled check_seconds=${code_check_seconds} signature=${initial_code_signature}"
fi
while true; do
  if [[ -n "${api_pid}" ]] && ! kill -0 "${api_pid}" >/dev/null 2>&1; then
    wait "${api_pid}" || status="$?"
    break
  fi
  if [[ -n "${web_pid}" ]] && ! kill -0 "${web_pid}" >/dev/null 2>&1; then
    wait "${web_pid}" || status="$?"
    break
  fi
  now="$(date +%s)"
  if [[ -n "${initial_code_signature}" ]] && (( now >= next_code_check )); then
    current_code_signature="$(frontend_code_signature)"
    if [[ "${current_code_signature}" != "${initial_code_signature}" ]]; then
      echo "[all_frontend] code_change detected old_signature=${initial_code_signature} new_signature=${current_code_signature}; requesting restart"
      status="75"
      break
    fi
    next_code_check=$((now + code_check_seconds))
  fi
  if (( now >= next_health_check )); then
    api_ok=1
    web_ok=1
    if [[ -n "${api_pid}" ]]; then
      api_ok=0
      if health_check_url "http://${API_HOST}:${API_PORT}/api/health"; then
        api_ok=1
      fi
    fi
    if [[ -n "${web_pid}" ]]; then
      web_ok=0
      if health_check_url "http://${WEB_HOST}:${WEB_PORT}/"; then
        web_ok=1
      fi
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
echo "[all_frontend] supervised process exited status=${status}; stopping remaining process"
exit "${status}"
