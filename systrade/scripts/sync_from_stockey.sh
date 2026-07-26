#!/usr/bin/env bash
# Sync tables from the stockey database into the local systrade database.
# Table names are IDENTICAL in both DBs so the two can be merged later.
#
# Mechanism: pure \copy (no pg_dump — the source is TimescaleDB and dump
# output doesn't restore into a vanilla postgres). If a table is missing in
# systrade its DDL is generated from the source's information_schema.
#
# - FULL_TABLES: small; truncated and fully re-copied every run.
# - INCR_TABLES: large; only rows with date > local max(date).
#
# Run daily (cron) after stockey's own downloaders finish:
#   ./scripts/sync_from_stockey.sh
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a

SRC_HOST="${STOCKEY_PG_HOST}"
if ! PGPASSWORD="$STOCKEY_PG_PASSWORD" pg_isready -h "$SRC_HOST" -p "$STOCKEY_PG_PORT" -t 3 >/dev/null 2>&1; then
  echo "WARN: $SRC_HOST unreachable, falling back to ${STOCKEY_PG_FALLBACK_HOST}"
  SRC_HOST="${STOCKEY_PG_FALLBACK_HOST}"
fi

SRC="postgresql://${STOCKEY_PG_USER}:${STOCKEY_PG_PASSWORD}@${SRC_HOST}:${STOCKEY_PG_PORT}/${STOCKEY_PG_DB}"
DST="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"

FULL_TABLES=(
  master_dhan_instruments   # Dhan security ids, lot sizes, tick sizes
  dim_security              # identity mapping
  events_dividend
  events_capital_change
  rbi_bank_rates            # funding rate for carry
  rbi_currency_rates
  fbil_gsec_par             # G-sec yields for carry / bond sleeve
  nseindia_mcap
  historical_mcap           # point-in-time universe
  nseindia_corporate_actions
  nseindia_holidays
  dim_trading_days
  nseindia_ohlcv_adjusted   # adjustment factors + TR-adjusted prices
)

INCR_TABLES=(               # incremental on the "date" column + reconciliation
  dhan_ohlcv_daily          # 2015+, ~2400 stocks (unadjusted)
  nseindia_ohlcv            # raw NSE bhavcopy feed
  nseindia_indices          # index OHLCV + PE/PB/divyield (carry inputs)
  advisory_adjusted_ohlcv_daily  # ADJUSTED closes 2013+, incl. delisted — primary backtest series
)

src_has_table() {
  [ "$(psql "$SRC" -tAc "select count(*) from pg_tables where schemaname='public' and tablename='$1'")" = "1" ]
}

dst_has_table() {
  [ "$(psql "$DST" -tAc "select count(*) from pg_tables where schemaname='public' and tablename='$1'")" = "1" ]
}

# Create the table in systrade with the source's current column layout.
ensure_table() {
  local t="$1"
  if dst_has_table "$t"; then
    # Column drift check: recreate only if layouts differ (data is re-copyable).
    local src_cols dst_cols
    src_cols=$(psql "$SRC" -tAc "select string_agg(column_name||':'||data_type, ',' order by ordinal_position) from information_schema.columns where table_schema='public' and table_name='$t'")
    dst_cols=$(psql "$DST" -tAc "select string_agg(column_name||':'||data_type, ',' order by ordinal_position) from information_schema.columns where table_schema='public' and table_name='$t'")
    if [ "$src_cols" != "$dst_cols" ]; then
      echo "  schema drift on $t → recreating"
      psql "$DST" -q -c "DROP TABLE $t"
    else
      return 0
    fi
  fi
  local ddl
  ddl=$(psql "$SRC" -tAc "
    select 'CREATE TABLE $t ('||string_agg(column_name||' '||
      case when data_type='character varying' then 'varchar' else data_type end,
      ', ' order by ordinal_position)||')'
    from information_schema.columns
    where table_schema='public' and table_name='$t'")
  psql "$DST" -q -c "$ddl"
}

for t in "${FULL_TABLES[@]}"; do
  if ! src_has_table "$t"; then echo "skip $t (absent at source)"; continue; fi
  ensure_table "$t"
  echo "full sync: $t"
  psql "$DST" -q -c "TRUNCATE $t"
  psql "$SRC" -Atc "\copy (select * from $t) to stdout" | psql "$DST" -q -c "\copy $t from stdin"
done

for t in "${INCR_TABLES[@]}"; do
  if ! src_has_table "$t"; then echo "skip $t (absent at source)"; continue; fi
  ensure_table "$t"
  last=$(psql "$DST" -tAc "select coalesce(max(date)::text,'1900-01-01') from $t")
  echo "incremental sync: $t since $last"
  psql "$SRC" -Atc "\copy (select * from $t where date > '$last') to stdout" \
    | psql "$DST" -q -c "\copy $t from stdin"

  # RECONCILE: incremental-on-date is blind to rows inserted/rewritten at
  # historical dates (gap backfills, corporate-action re-adjustment). Compare
  # a cheap fingerprint (row count + max load_ts when the column exists);
  # any drift → truncate and re-copy the whole table. Self-healing.
  fp_sql="select count(*)::text from $t"
  if [ "$(psql "$SRC" -tAc "select count(*) from information_schema.columns where table_schema='public' and table_name='$t' and column_name='load_ts'")" = "1" ]; then
    fp_sql="select count(*)::text||'|'||coalesce(max(load_ts)::text,'-') from $t"
  fi
  src_fp=$(psql "$SRC" -tAc "$fp_sql")
  dst_fp=$(psql "$DST" -tAc "$fp_sql")
  if [ "$src_fp" != "$dst_fp" ]; then
    echo "  drift on $t (src $src_fp != local $dst_fp) → full re-copy"
    psql "$DST" -q -c "TRUNCATE $t"
    psql "$SRC" -Atc "\copy (select * from $t) to stdout" | psql "$DST" -q -c "\copy $t from stdin"
  fi
done

# Indexes systrader queries rely on (idempotent).
psql "$DST" -q <<'EOF'
CREATE INDEX IF NOT EXISTS idx_dhan_ohlcv_ticker_date ON dhan_ohlcv_daily (ticker, date);
CREATE INDEX IF NOT EXISTS idx_dhan_ohlcv_date ON dhan_ohlcv_daily (date);
CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol_date ON nseindia_ohlcv (symbol, date);
CREATE INDEX IF NOT EXISTS idx_indices_name_date ON nseindia_indices (index_name, date);
CREATE INDEX IF NOT EXISTS idx_corpact_symbol ON nseindia_corporate_actions (symbol, date);
CREATE INDEX IF NOT EXISTS idx_advisory_adj_symbol_date ON advisory_adjusted_ohlcv_daily (symbol, date);
EOF

echo "done. row counts:"
psql "$DST" -tAc "select relname||': '||n_live_tup from pg_stat_user_tables order by 1"
