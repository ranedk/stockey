#!/usr/bin/env bash
# DEPRECATED 2026-08-26: superseded by a postgres_fdw foreign table
# (dhan_ohlcv_intraday -> stockey_server -> stockey's actual table). Both
# Postgres instances run on the SAME box/disk, so mirroring this table
# duplicated ~800M+ rows for zero real isolation benefit, right after a
# day of real disk-exhaustion incidents. Cron entry removed. Kept here only
# as a reference/fallback for if the machines are ever truly separated.
#
# Sync dhan_ohlcv_intraday (1-min OHLCV bars) from stockey into the local
# systrade mirror. HF_DATA_PLATFORM_PLAN.md Phase 2 -- deliberately a
# SEPARATE script from sync_from_stockey.sh, not folded into its FULL_TABLES/
# INCR_TABLES arrays:
#   - 22M+ rows already, growing ~230K/day (635 tickers x ~375 min/day) --
#     an order of magnitude past anything else that script syncs.
#   - No `date` column at all -- the natural watermark is `timestamp`
#     (minute-level), not the date-based incremental logic the other script
#     already has.
#   - Lands as a TimescaleDB HYPERTABLE on this side too (chunked by time),
#     not a plain table -- this is a heavy time-range-scan workload
#     (backtests reading a symbol's whole history), exactly what hypertables
#     are for. sync_from_stockey.sh's ensure_table() only ever creates plain
#     tables; that's correct for its own small/daily tables but wrong here.
#
# Same \copy (SELECT * FROM ...) requirement as the daily sync (see
# DATA_CONTRACT.md's hypertable notes: bare `\copy tablename` silently copies
# zero rows for a hypertable).
#
# Run daily (cron), after sync_from_stockey.sh:
#   ./scripts/sync_intraday_from_stockey.sh
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a

export PGOPTIONS='-c statement_timeout=0'
export PGTZ=UTC

TABLE="dhan_ohlcv_intraday"

SRC_HOST="${STOCKEY_PG_HOST}"
if ! PGPASSWORD="$STOCKEY_PG_PASSWORD" pg_isready -h "$SRC_HOST" -p "$STOCKEY_PG_PORT" -t 3 >/dev/null 2>&1; then
  echo "WARN: $SRC_HOST unreachable, falling back to ${STOCKEY_PG_FALLBACK_HOST}"
  SRC_HOST="${STOCKEY_PG_FALLBACK_HOST}"
fi

SRC="postgresql://${STOCKEY_PG_USER}:${STOCKEY_PG_PASSWORD}@${SRC_HOST}:${STOCKEY_PG_PORT}/${STOCKEY_PG_DB}"
DST="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"

dst_has_table() {
  [ "$(psql "$DST" -tAc "select case when to_regclass('public.$TABLE') is not null then 1 else 0 end")" = "1" ]
}

# Create the table (+ hypertable + the source's own unique index) from the
# source's current column layout. Idempotent; only runs the DDL once.
ensure_intraday_table() {
  if dst_has_table; then
    return 0
  fi
  echo "creating $TABLE on destination (first run)"
  local ddl
  ddl=$(psql "$SRC" -tAc "
    select 'CREATE TABLE $TABLE ('||string_agg(column_name||' '||
      case when data_type='character varying' then 'varchar' else data_type end,
      ', ' order by ordinal_position)||')'
    from information_schema.columns
    where table_schema='public' and table_name='$TABLE'")
  psql "$DST" -q -c "$ddl"
  psql "$DST" -q -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
  psql "$DST" -q -c "SELECT create_hypertable('$TABLE', 'timestamp', if_not_exists => TRUE);"
  # Same unique key as the source (exchange, security_id, interval_minutes,
  # timestamp) -- lets a future upsert-based sync use ON CONFLICT DO NOTHING
  # even though this first version relies on a clean timestamp watermark.
  psql "$DST" -q -c "CREATE UNIQUE INDEX IF NOT EXISTS idx_${TABLE}_unique ON $TABLE (exchange, security_id, interval_minutes, \"timestamp\");"
  psql "$DST" -q -c "CREATE INDEX IF NOT EXISTS idx_${TABLE}_ticker_window ON $TABLE (exchange, asset_type, interval_minutes, ticker, \"timestamp\");"
}

ensure_intraday_table

last=$(psql "$DST" -tAc "select coalesce(max(timestamp)::text, '1900-01-01') from $TABLE")
echo "incremental sync: $TABLE since $last"

SPOOL_DIR="$(mktemp -d "${TMPDIR:-/tmp}/systrade_intraday_sync.XXXXXX")"
trap 'rm -rf "$SPOOL_DIR"' EXIT
SPOOL="$SPOOL_DIR/$TABLE.tsv"

psql "$SRC" -Atc "\copy (select * from $TABLE where timestamp > '$last' order by timestamp) to stdout" > "$SPOOL"
rows=$(wc -l < "$SPOOL" | tr -d ' ')
echo "spooled $rows new rows"
if [ "$rows" -gt 0 ]; then
  psql "$DST" -q -c "\copy $TABLE from '$SPOOL'"
fi

echo "done. row counts:"
psql "$DST" -tAc "select count(*), min(timestamp), max(timestamp) from $TABLE"
