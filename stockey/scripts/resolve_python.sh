#!/usr/bin/env bash

set -euo pipefail

if [[ -n "${PYTHON_BIN:-}" ]]; then
  printf '%s\n' "${PYTHON_BIN}"
  exit 0
fi

if [[ -n "${VIRTUAL_ENV:-}" ]] && command -v python >/dev/null 2>&1; then
  command -v python
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_VENV_PYTHON="${REPO_ROOT}/.xstockey/bin/python"

if [[ -x "${REPO_VENV_PYTHON}" ]]; then
  printf '%s\n' "${REPO_VENV_PYTHON}"
  exit 0
fi

if command -v python3 >/dev/null 2>&1; then
  command -v python3
  exit 0
fi

if command -v python >/dev/null 2>&1; then
  command -v python
  exit 0
fi

printf 'Unable to locate a usable Python interpreter.\n' >&2
exit 1
