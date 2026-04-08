#!/usr/bin/env bash

set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-/home/rane/code/stockey/.xstockey/bin/python}"

exec "${PYTHON_BIN}" -m advisory.model_training_runner "$@"
