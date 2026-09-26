#!/usr/bin/env bash
# Daily log rotation for the cron/operator logs: archive each sizable *.log as
# logs/cron/archive/<name>.<YYYY-MM-DD>.log.gz and truncate the live file in place.
#
# Copy-then-truncate (not rename) on purpose: long-running processes (watchers,
# frontend supervisor) hold their log fd open across days -- a rename would leave them
# writing to the archived inode forever while the fresh file stays empty. The small
# copy->truncate race can drop a few lines; acceptable for operational logs.
#
# Env:
#   LOG_ROTATE_KEEP_DAYS  prune archives older than this (default 14)
#   LOG_ROTATE_MIN_BYTES  skip logs smaller than this (default 1 MiB)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
keep_days="${LOG_ROTATE_KEEP_DAYS:-14}"
min_bytes="${LOG_ROTATE_MIN_BYTES:-1048576}"
stamp="$(date +%Y-%m-%d)"

file_size() {
  stat -f%z "$1" 2>/dev/null || stat -c%s "$1" 2>/dev/null || echo 0
}

rotated=0
skipped=0
failed=0
for dir in "$SCRIPT_DIR/logs" "$SCRIPT_DIR/logs/cron"; do
  [ -d "$dir" ] || continue
  archive_dir="$dir/archive"
  mkdir -p "$archive_dir"
  for f in "$dir"/*.log; do
    [ -e "$f" ] || continue
    size="$(file_size "$f")"
    if [ "$size" -lt "$min_bytes" ]; then
      skipped=$((skipped + 1))
      continue
    fi
    base="$(basename "$f" .log)"
    target="$archive_dir/${base}.${stamp}.log.gz"
    if [ -e "$target" ]; then
      target="$archive_dir/${base}.${stamp}.$(date +%H%M%S).log.gz"
    fi
    if gzip -c "$f" > "${target}.partial"; then
      mv "${target}.partial" "$target"
      : > "$f"
      rotated=$((rotated + 1))
      echo "[rotate_logs] rotated $(basename "$f") ($size bytes) -> ${target#$SCRIPT_DIR/}"
    else
      rm -f "${target}.partial"
      failed=$((failed + 1))
      echo "[rotate_logs] FAILED to compress $f (left untouched)" >&2
    fi
  done
  # prune old archives
  find "$archive_dir" -name "*.log.gz" -mtime "+${keep_days}" -delete 2>/dev/null || true
done

echo "[rotate_logs] done rotated=${rotated} skipped_small=${skipped} failed=${failed} keep_days=${keep_days}"
# BUG FOUND LIVE 2026-08-20 (re-audit, MEDIUM): this used to always fall through to a
# successful (0) exit regardless of `failed` -- the gzip failure was logged to stderr but
# never affected the script's own exit code, so nothing invoking rotate_logs.sh (go-crond,
# an operator eyeballing $?) could ever see a compression failure without reading the log
# text itself. A source log left uncompressed AND untouched (never truncated on failure, by
# design) will just keep growing every day this silently recurs.
if [ "$failed" -gt 0 ]; then
  exit 1
fi
