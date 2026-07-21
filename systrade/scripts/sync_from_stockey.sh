#!/usr/bin/env bash
# Sync tables from the stockey database into the local systrade database.
# Table names are IDENTICAL in both DBs so they can be merged later.
#
# - FULL_TABLES: small; truncated and fully re-copied every run.
# - INCR_TABLES: large hypertables; only rows newer than local max(date).
#
# Run manually or from cron (daily, after stockey's own downloaders finish):
#   ./scripts/sync_from_stockey.sh
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a

SRC_HOST="${STOCKEY_PG_HOST}"
# Fall back to a local stockey copy when the remote is unreachable.
if ! PGPASSWORD="$STOCKEY_PG_PASSWORD" pg_isready -h "$SRC_HOST" -p "$STOCKEY_PG_PORT" -t 3 >/dev/null 2>&1; then
  echo "WARN: $SRC_HOST unreachable, falling back to ${STOCKEY_PG_FALLBACK_HOST}"
  SRC_HOST="${STOCKEY_PG_FALLBACK_HOST}"
fi

SRC="postgresql://${STOCKEY_PG_USER}:${STOCKEY_PG_PASSWORD}@${SRC_HOST}:${STOCKEY_PG_PORT}/${STOCKEY_PG_DB}"
DST="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"

FULL_TABLES=(
  master_dhan_instruments
  events_dividend
  events_capital_change
  rbi_bank_rates
  rbi_currency_rates
  fbil_gsec_par
  nseindia_mcap
  historical_mcap
  nseindia_corporate_actions
  nseindia_holidays
  dim_trading_days
)

INCR_TABLES=(   # incremental on "date" column
  nseindia_ohlcv
  nseindia_indices
)

src_has_table() {
  [ "$(psql "$SRC" -tAc "select count(*) from pg_tables where schemaname='public' and tablename='$1'")" = "1" ]
}

for t in "${FULL_TABLES[@]}"; do
  if ! src_has_table "$t"; then echo "skip $t (absent at source)"; continue; fi
  echo "full sync: $t"
  psql "$DST" -q -c "TRUNCATE $t" 2>/dev/null || true
  # --no-owner/--no-privileges + --clean keeps schemas aligned with source
  pg_dump "$SRC" --no-owner --no-privileges --clean --if-exists -t "$t" | psql "$DST" -q
done

for t in "${INCR_TABLES[@]}"; do
  if ! src_has_table "$t"; then echo "skip $t (absent at source)"; continue; fi
  last=$(psql "$DST" -tAc "select coalesce(max(date)::text,'1900-01-01') from $t")
  echo "incremental sync: $t since $last"
  psql "$SRC" -c "\copy (select * from $t where date > '$last') to stdout" \
    | psql "$DST" -c "\copy $t from stdin"
done

echo "done. row counts:"
psql "$DST" -tAc "select relname||': '||n_live_tup from pg_stat_user_tables order by 1"
