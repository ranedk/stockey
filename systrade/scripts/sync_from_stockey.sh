#!/usr/bin/env bash
# Sync tables from the stockey database into the local systrade database.
# Table names are IDENTICAL in both DBs. See docs/DATA_CONTRACT.md: the
# cloud DB is small and must never see heavy load — this daily pull is the
# ONLY thing that reads it from this side; all research reads run locally.
#
# Mechanism: pure \copy (survives cross-version/extension differences --
# note the source has ~100 active TimescaleDB hypertables, corrected in
# docs/DATA_CONTRACT.md 2026-08-05; the `\copy (select * from $t) ...` form
# below is required -- bare `\copy $t TO ...` only touches a hypertable's
# empty parent shell and silently copies 0 rows, found 2026-08-14). If a
# table is missing in systrade its DDL is generated from the source's
# information_schema.
#
# - FULL_TABLES: small; truncated and fully re-copied every run.
# - INCR_TABLES: large; only rows with date > local max(date).
#
# Run daily (cron) after stockey's own downloaders finish:
#   ./scripts/sync_from_stockey.sh
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a

# The source server enforces a statement_timeout that kills multi-million-row
# \copy selects mid-stream — after we may already have TRUNCATEd locally.
# Disable it for our sessions, and pin timestamp rendering to UTC so the
# drift fingerprints (max(load_ts)) compare equal across servers whose
# TimeZone settings differ.
export PGOPTIONS='-c statement_timeout=0'
export PGTZ=UTC

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
  company_master            # NSE<->BSE identity for the stage API's BSE series (2026-09-24)
  company_master_nse_alias  # NSE renames: current symbol -> existing company (2026-09-24)
  master_sharpely_equity    # sector / industry / basic-industry codes per stock (rotation view, 2026-10-01)
  fundamentals_sector_reference  # code -> name for those levels (rotation view, 2026-10-01)
)

INCR_TABLES=(               # incremental on the "date" column + reconciliation
  dhan_ohlcv_daily          # 2015+, ~2400 stocks (unadjusted)
  nseindia_ohlcv            # raw NSE bhavcopy feed
  nseindia_mto              # delivery position (delivery %), 2013+ — trait library, revived 2026-09-11
  nseindia_circuit_hit      # price-band hits, 2013+ — trait library, revived 2026-09-11
  nseindia_indices          # index OHLCV + PE/PB/divyield (carry inputs)
  advisory_adjusted_ohlcv_daily  # ADJUSTED closes 2013+, incl. delisted — primary backtest series.
  bse_advisory_adjusted_ohlcv_daily  # BSE adjusted series (2025-08+) — stage API only: BSE-only names + pre-NSE history (2026-09-24)
                                  # A view on the source side since 2026-08-14 (was a written table);
                                  # now also carries tr_adj_open/high/low/close (total-return-adjusted,
                                  # dividend-reinvested) -- previously only in the removed nseindia_ohlcv_adjusted.
)

# Atomic full copy: spool source rows to disk first, then swap the table
# contents in ONE transaction. A network/timeout failure mid-copy must never
# leave the local table truncated or half-filled (that happened 2026-07-26).
SPOOL_DIR="$(mktemp -d "${TMPDIR:-/tmp}/systrade_sync.XXXXXX")"
trap 'rm -rf "$SPOOL_DIR"' EXIT

full_copy() {
  local t="$1" spool="$SPOOL_DIR/$1.tsv"
  psql "$SRC" -Atc "\copy (select * from $t) to stdout" > "$spool"
  psql "$DST" -q -v ON_ERROR_STOP=1 <<SQL
BEGIN;
TRUNCATE $t;
\copy $t from '$spool'
COMMIT;
SQL
  rm -f "$spool"
}

src_has_table() {
  # BUG FOUND LIVE 2026-08-21: this used to check pg_tables only, which
  # excludes VIEWS. advisory_adjusted_ohlcv_daily (systrader's PRIMARY equity
  # series) became a view over nseindia_ohlcv + nseindia_adjustment_factors
  # on 2026-08-14's pure-TA cutover (was a written table before that) -- this
  # check silently started reporting it "absent at source" and skipping it
  # ever since, with no error, because pg_tables genuinely doesn't have a row
  # for it. to_regclass resolves any relation kind (table, view, matview) in
  # one check, so it can't drift out of sync with a future table<->view
  # change on the source side again.
  [ "$(psql "$SRC" -tAc "select case when to_regclass('public.$1') is not null then 1 else 0 end")" = "1" ]
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
  full_copy "$t"
done

# RECONCILE BY KEY (2026-09-24). Incremental-on-date is blind to rows inserted or
# REWRITTEN at historical dates. The old guard compared row count + max(load_ts), which
# cannot see an in-place update at all: on 2026-09-24 systrader's copy of
# advisory_adjusted_ohlcv_daily held stale total-return factors for 524 symbols (same row
# counts; the view's timestamp column is not load_ts, so the check was a bare count), and
# 90k nseindia_ohlcv rows lacked the company_master_id stockey had since backfilled.
# Now: hash every key's VALUE columns on both sides (timestamp metadata such as
# load_ts/computed_at excluded -- it differs on every recompute without the values
# changing), then delete + re-copy only the keys that differ. A key present on one side
# only is covered too (count differs). More than half the keys differing -> full copy.
declare -A RECONCILE_KEY=(
  [dhan_ohlcv_daily]=security_id
  [nseindia_ohlcv]=symbol
  [nseindia_mto]=symbol
  [nseindia_circuit_hit]=symbol
  [nseindia_indices]=index_name
  [advisory_adjusted_ohlcv_daily]=symbol
  [bse_advisory_adjusted_ohlcv_daily]=scrip_code
)

reconcile_by_key() {
  local t="$1" key="$2"
  local cols src_list="$SPOOL_DIR/$t.src.fp" dst_list="$SPOOL_DIR/$t.dst.fp" keys="$SPOOL_DIR/$t.keys" spool="$SPOOL_DIR/$t.fix.tsv"
  cols=$(psql "$SRC" -tAc "select string_agg(column_name, ',' order by ordinal_position) from information_schema.columns
                             where table_schema='public' and table_name='$t'
                               and (column_name = 'date' or data_type not like 'timestamp%')")
  local fp="select $key::text, count(*), sum(hashtext(row($cols)::text)::bigint) from $t group by 1 order by 1"
  psql "$SRC" -Atc "$fp" > "$src_list"
  psql "$DST" -Atc "$fp" > "$dst_list"
  # keys whose (count, hash) differ, or that exist on one side only
  sort "$src_list" > "$src_list.s"; sort "$dst_list" > "$dst_list.s"
  comm -3 "$src_list.s" "$dst_list.s" | sed 's/^\t//' | cut -d'|' -f1 | sort -u > "$keys"
  local n total
  n=$(wc -l < "$keys"); total=$(wc -l < "$src_list")
  if [ "$n" -eq 0 ]; then
    echo "  reconcile $t: all $total ${key}s match"
    return 0
  fi
  if [ "$total" -gt 0 ] && [ $((n * 2)) -gt "$total" ]; then
    echo "  reconcile $t: $n of $total ${key}s differ → full re-copy"
    full_copy "$t"
    return 0
  fi
  echo "  reconcile $t: $n of $total ${key}s differ → re-copying those"
  psql "$SRC" -q -v ON_ERROR_STOP=1 <<SQL
CREATE TEMP TABLE fix_keys (k text);
\copy fix_keys from '$keys'
\copy (select t.* from $t t join fix_keys f on t.$key::text = f.k) to '$spool'
SQL
  psql "$DST" -q -v ON_ERROR_STOP=1 <<SQL
BEGIN;
CREATE TEMP TABLE fix_keys (k text) ON COMMIT DROP;
\copy fix_keys from '$keys'
DELETE FROM $t t USING fix_keys f WHERE t.$key::text = f.k;
\copy $t from '$spool'
COMMIT;
SQL
  rm -f "$spool"
}

for t in "${INCR_TABLES[@]}"; do
  if ! src_has_table "$t"; then echo "skip $t (absent at source)"; continue; fi
  ensure_table "$t"
  last=$(psql "$DST" -tAc "select coalesce(max(date)::text,'1900-01-01') from $t")
  echo "incremental sync: $t since $last"
  psql "$SRC" -Atc "\copy (select * from $t where date > '$last') to stdout" \
    | psql "$DST" -q -c "\copy $t from stdin"

  reconcile_by_key "$t" "${RECONCILE_KEY[$t]}"
done

# Indexes systrader queries rely on (idempotent).
psql "$DST" -q <<'EOF'
CREATE INDEX IF NOT EXISTS idx_dhan_ohlcv_ticker_date ON dhan_ohlcv_daily (ticker, date);
CREATE INDEX IF NOT EXISTS idx_dhan_ohlcv_date ON dhan_ohlcv_daily (date);
CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol_date ON nseindia_ohlcv (symbol, date);
CREATE INDEX IF NOT EXISTS idx_indices_name_date ON nseindia_indices (index_name, date);
CREATE INDEX IF NOT EXISTS idx_corpact_symbol ON nseindia_corporate_actions (symbol, date);
CREATE INDEX IF NOT EXISTS idx_advisory_adj_symbol_date ON advisory_adjusted_ohlcv_daily (symbol, date);
CREATE INDEX IF NOT EXISTS idx_bse_adv_adj_scrip_date ON bse_advisory_adjusted_ohlcv_daily (scrip_code, date);
CREATE INDEX IF NOT EXISTS idx_company_master_nse ON company_master (nse_ticker);
CREATE INDEX IF NOT EXISTS idx_company_master_bse_scrip ON company_master (bse_scrip_code);
EOF

echo "done. row counts:"
psql "$DST" -tAc "select relname||': '||n_live_tup from pg_stat_user_tables order by 1"
