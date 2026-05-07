#!/usr/bin/env bash

set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <lock-file> <command> [args...]" >&2
  exit 2
fi

LOCK_FILE="$1"
shift

mkdir -p "$(dirname "$LOCK_FILE")"

if command -v flock >/dev/null 2>&1; then
  exec flock -n "$LOCK_FILE" "$@"
fi

if command -v lockf >/dev/null 2>&1; then
  exec lockf -t 0 "$LOCK_FILE" "$@"
fi

echo "No supported lock utility found. Install flock or lockf." >&2
exit 127
