#!/usr/bin/env bash
#
# incident_audit.sh -- postgres write-amplification config + data-completeness audit.
#
# Written 2026-08-31 after the 25.6 GB OOM / ~1.6 TB-WAL-per-day incidents.
# Read-only by default. --apply-config makes the four postgres changes below
# (all SIGHUP-level: no restart, no downtime) and nothing else.
#
#   scripts/incident_audit.sh                 # audit only -- always safe to run
#   scripts/incident_audit.sh --apply-config  # audit + apply postgres settings (needs sudo)
#   scripts/incident_audit.sh --deep          # + whole-history intraday scans (chunk by chunk, slow)
#   scripts/incident_audit.sh --window-days=N # widen/narrow the intraday window (default 30)
#
# SAFETY (2026-08-31, after this script's own section 4 helped OOM the box):
#   dhan_ohlcv_intraday is 859M rows across 262 COMPRESSED chunks. Any unbounded
#   aggregate over it -- count(*), select distinct ticker, min(timestamp) group by
#   security_id -- makes TimescaleDB decompress every chunk at once and takes a 30 GB
#   box to OOM in ~15 seconds, killing postgres with it. So:
#     - every query here is time-bounded or reads catalog metadata instead of rows;
#     - whole-history questions live behind --deep and walk ONE CHUNK AT A TIME;
#     - PGOPTIONS pins a statement_timeout and a small work_mem as a backstop.
#   If you add a query to this script, bound it. There is no safe unbounded scan.
#
# It does NOT touch application code. The three code defects this incident traced
# to (backfill_intraday_5yr.py's uncleared batch list, its never-matching resume
# check, and upsert_to_db's ON CONFLICT DO UPDATE into compressed chunks) are
# reported here as measurements, not fixed here.

set -uo pipefail

STOCKEY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$STOCKEY_DIR" || exit 1

APPLY_CONFIG=0
DEEP=0
INTRADAY_WINDOW_DAYS=30
WAL_COMPRESSION_ALGO="zstd"
for arg in "$@"; do
  case "$arg" in
    --apply-config) APPLY_CONFIG=1 ;;
    --deep) DEEP=1 ;;
    --window-days=*) INTRADAY_WINDOW_DAYS="${arg#*=}" ;;
    # Accepted and ignored: every scan is bounded now, so the old opt-out is a no-op.
    # Kept so existing muscle memory / cron lines do not fail with "unknown arg".
    --quick) echo "note: --quick is now the default and does nothing; use --deep for full-history scans" >&2 ;;
    --wal-compression=*) WAL_COMPRESSION_ALGO="${arg#*=}" ;;
    -h|--help) sed -n '2,24p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------- env / helpers
envval() { grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2- | tr -d "\"'"; }
PGHOST_="$(envval POSTGRES_HOST)"; PGPORT_="$(envval POSTGRES_PORT)"
PGDB_="$(envval POSTGRES_DB)";     PGUSER_="$(envval POSTGRES_USER)"
export PGPASSWORD="$(envval POSTGRES_PASSWORD)"
: "${PGHOST_:=localhost}" "${PGPORT_:=5432}" "${PGDB_:=stockey}" "${PGUSER_:=stockey}"

# Hard safety net. This script exists because ONE unbounded aggregate over
# dhan_ohlcv_intraday (859M rows, 262 compressed chunks) took the box from normal to
# OOM in ~15 seconds on 2026-08-31, killing postgres. Every query below is bounded by
# construction, but a statement_timeout means a future edit that forgets cannot run
# long enough to exhaust memory either. work_mem is deliberately small: nothing here
# needs a large sort.
export PGOPTIONS="-c statement_timeout=120000 -c work_mem=32MB"

FINDINGS=0
RED=$'\033[31m'; YEL=$'\033[33m'; GRN=$'\033[32m'; DIM=$'\033[2m'; OFF=$'\033[0m'
[ -t 1 ] || { RED=; YEL=; GRN=; DIM=; OFF=; }

hdr()  { printf '\n%s\n%s\n' "== $* ==" "$(printf '%.0s-' $(seq 1 ${#1}))"; }
ok()   { printf '  %sOK%s   %s\n'   "$GRN" "$OFF" "$*"; }
warn() { printf '  %sWARN%s %s\n'   "$YEL" "$OFF" "$*"; FINDINGS=$((FINDINGS+1)); }
bad()  { printf '  %sFAIL%s %s\n'   "$RED" "$OFF" "$*"; FINDINGS=$((FINDINGS+1)); }
info() { printf '  %s     %s%s\n'   "$DIM" "$*" "$OFF"; }

# psql -> single scalar (empty string on any error)
s() { psql -X -q -h "$PGHOST_" -p "$PGPORT_" -U "$PGUSER_" -d "$PGDB_" -tAc "$1" 2>/dev/null | tr -d '[:space:]'; }
# psql -> raw rows, pipe-separated
r() { psql -X -q -h "$PGHOST_" -p "$PGPORT_" -U "$PGUSER_" -d "$PGDB_" -tAc "$1" 2>&1; }
# psql -> formatted table, echoed
t() { psql -X -q -h "$PGHOST_" -p "$PGPORT_" -U "$PGUSER_" -d "$PGDB_" -c "$1" 2>&1 | sed 's/^/  /'; }

echo "stockey incident audit -- $(date -u '+%Y-%m-%dT%H:%M:%SZ')  host=$(hostname)  db=$PGDB_@$PGHOST_"
if ! s "select 1" | grep -q 1; then
  echo "cannot connect to postgres as $PGUSER_@$PGHOST_:$PGPORT_/$PGDB_ -- check .env" >&2
  exit 1
fi

# =============================================================================
hdr "1. Postgres write-amplification settings"
# =============================================================================
# Values that made 1.6 TB/day possible. All four are SIGHUP -- reload, no restart.
#
#   wal_compression=off  -> every 8 kB page touched after a checkpoint writes a
#                           FULL 8 kB image into WAL, uncompressed. This workload
#                           is FPI-dominated (whole-table rewrites), so this is
#                           the single biggest lever.
#   checkpoint_timeout=300 (postgres default, never tuned) -> checkpoints re-arm
#                           FPI logging every 5 min; a 3-minute full-table rewrite
#                           straddles enough checkpoints to re-image the same pages.
#   max_wal_size=1GB in postgresql.conf:252 -- the live value is 8 GB, set later
#                           via ALTER SYSTEM. During the Aug 23 incident the file
#                           value was in force, which is what drove the
#                           "checkpoints occurring too frequently" flood.
#   pg_monitor on the app role -> lets this audit read pg_ls_waldir() without sudo.

printf '%s\n' "  current values:"
t "select name, setting, unit, source from pg_settings
   where name in ('wal_compression','checkpoint_timeout','max_wal_size','min_wal_size',
                  'checkpoint_completion_target','full_page_writes','shared_buffers',
                  'archive_mode','archive_command','log_checkpoints')
   order by name;"

CUR_WALC="$(s "select setting from pg_settings where name='wal_compression'")"
CUR_CKPT="$(s "select setting from pg_settings where name='checkpoint_timeout'")"
CUR_MAXW="$(s "select setting from pg_settings where name='max_wal_size'")"
CONF_MAXW="$(grep -E '^\s*max_wal_size' /etc/postgresql/16/main/postgresql.conf 2>/dev/null | tail -1 | tr -d ' ')"

[ "$CUR_WALC" = "off" ] && bad "wal_compression=off -- full-page images are written uncompressed" \
                        || ok  "wal_compression=$CUR_WALC"
[ "${CUR_CKPT:-0}" -lt 900 ] 2>/dev/null && warn "checkpoint_timeout=${CUR_CKPT}s (postgres default; re-arms FPI logging every ${CUR_CKPT}s)" \
                                         || ok "checkpoint_timeout=${CUR_CKPT}s"
[ "${CUR_MAXW:-0}" -lt 4096 ] 2>/dev/null && bad "max_wal_size=${CUR_MAXW}MB -- too small for this write volume" \
                                          || ok "max_wal_size=${CUR_MAXW}MB (live)"
[ -n "$CONF_MAXW" ] && info "postgresql.conf still says '$CONF_MAXW' -- live value comes from ALTER SYSTEM (postgresql.auto.conf)."
info "a postgres restart WILL keep the ALTER SYSTEM value; auto.conf wins over postgresql.conf."

if [ "$APPLY_CONFIG" = "1" ]; then
  hdr "1b. Applying postgres configuration (sudo -u postgres)"
  case "$WAL_COMPRESSION_ALGO" in
    zstd|lz4|pglz|on) : ;;
    *) echo "  invalid --wal-compression=$WAL_COMPRESSION_ALGO" >&2; exit 2 ;;
  esac
  if ! s "select enumvals from pg_settings where name='wal_compression'" | grep -q "$WAL_COMPRESSION_ALGO"; then
    warn "this build does not offer wal_compression=$WAL_COMPRESSION_ALGO; falling back to 'on'"
    WAL_COMPRESSION_ALGO=on
  fi
  info "you will be prompted for your sudo password."
  if sudo -u postgres psql -X -q -v ON_ERROR_STOP=1 -d postgres <<SQL
ALTER SYSTEM SET wal_compression = '$WAL_COMPRESSION_ALGO';
ALTER SYSTEM SET checkpoint_timeout = '15min';
ALTER SYSTEM SET max_wal_size = '8GB';
ALTER SYSTEM SET min_wal_size = '2GB';
GRANT pg_monitor TO $PGUSER_;
SELECT pg_reload_conf();
SQL
  then
    sleep 2
    ok "applied. now: wal_compression=$(s "show wal_compression"), checkpoint_timeout=$(s "show checkpoint_timeout"), max_wal_size=$(s "show max_wal_size")"
    info "all four are SIGHUP settings -- reloaded in place, no restart, no downtime."
    info "these reduce the COST of the writes. They do not reduce the write VOLUME;"
    info "that needs the three code fixes listed in section 7."
  else
    bad "ALTER SYSTEM failed (sudo declined, or no postgres superuser via sudo). Nothing changed."
  fi
else
  info "re-run with --apply-config to set wal_compression=$WAL_COMPRESSION_ALGO, checkpoint_timeout=15min,"
  info "max_wal_size=8GB, min_wal_size=2GB and GRANT pg_monitor TO $PGUSER_."
fi

# =============================================================================
hdr "2. WAL / checkpoint / archive throughput"
# =============================================================================
t "select checkpoints_timed, checkpoints_req, buffers_checkpoint,
          stats_reset, now() - stats_reset as window
   from pg_stat_bgwriter;"

CK_REQ="$(s "select checkpoints_req from pg_stat_bgwriter")"
CK_TIM="$(s "select checkpoints_timed from pg_stat_bgwriter")"
WIN_H="$(s "select greatest(1, round(extract(epoch from now()-stats_reset)/3600)) from pg_stat_bgwriter")"
if [ -n "$CK_REQ" ] && [ "${CK_REQ:-0}" -gt "${CK_TIM:-0}" ] 2>/dev/null; then
  bad "checkpoints_req ($CK_REQ) > checkpoints_timed ($CK_TIM) -- checkpoints are WAL-volume-driven, not timed."
  info "this is the '6,346 checkpoints are occurring too frequently' signature."
else
  ok "checkpoints_req=$CK_REQ vs checkpoints_timed=$CK_TIM over ~${WIN_H}h -- timed checkpoints dominate."
fi

WAL_N="$(s "select count(*) from pg_ls_waldir()")"
if [ -n "$WAL_N" ]; then
  t "select count(*) as segments, pg_size_pretty(sum(size)) as bytes from pg_ls_waldir();"
else
  info "pg_ls_waldir() denied for $PGUSER_ -- run with --apply-config once to GRANT pg_monitor, or:"
  info "  sudo du -sh /var/lib/postgresql/16/main/pg_wal"
fi

printf '%s\n' "  archiver:"
t "select archived_count, last_archived_wal, last_archived_time,
          failed_count, last_failed_wal, last_failed_time
   from pg_stat_archiver;"
ARCH_FAIL="$(s "select failed_count from pg_stat_archiver")"
[ "${ARCH_FAIL:-0}" -gt 0 ] 2>/dev/null && bad "pgbackrest archive-push has $ARCH_FAIL failures -- WAL cannot be recycled, disk drains." \
                                        || ok "archive-push has no failures in this stats window."
ARCH_LAG="$(s "select round(extract(epoch from now()-last_archived_time)/60) from pg_stat_archiver")"
[ -n "$ARCH_LAG" ] && [ "${ARCH_LAG:-0}" -gt 60 ] 2>/dev/null && warn "last successful archive was ${ARCH_LAG} min ago."

printf '%s\n' "  recent checkpoint log lines (needs sudo; skipped silently if denied):"
sudo -n grep -h 'checkpoint complete\|checkpoints are occurring' \
  /var/log/postgresql/postgresql-16-main.log 2>/dev/null | tail -5 | sed 's/^/    /' \
  || info "    (no passwordless sudo -- try: sudo tail -200 /var/log/postgresql/postgresql-16-main.log | grep checkpoint)"

# =============================================================================
hdr "3. Hypertable + compression state"
# =============================================================================
t "select h.hypertable_name,
          count(*) as chunks,
          count(*) filter (where c.is_compressed) as compressed,
          pg_size_pretty(max(h.bytes)) as size
   from (select hypertable_schema, hypertable_name,
                hypertable_size(format('%I.%I', hypertable_schema, hypertable_name)::regclass) as bytes
           from timescaledb_information.hypertables) h
   join timescaledb_information.chunks c
     on c.hypertable_schema = h.hypertable_schema and c.hypertable_name = h.hypertable_name
   group by 1 order by max(h.bytes) desc;"

# Uncompressed chunks older than the policy window = decompression debt: rows that
# an ON CONFLICT DO UPDATE decompressed and that are now sitting on disk in full,
# uncompressed form until the next policy run. This is what exhausted the disk.
DEBT="$(s "select count(*) from timescaledb_information.chunks
           where hypertable_name='dhan_ohlcv_intraday'
             and not is_compressed
             and range_end < now() - interval '7 days'")"
if [ "${DEBT:-0}" -gt 0 ] 2>/dev/null; then
  bad "$DEBT chunk(s) of dhan_ohlcv_intraday are past the 7-day compress_after window but still uncompressed."
  info "each holds its rows in full uncompressed form (~8x the compressed size)."
  info "force it now:  SELECT compress_chunk(c) FROM show_chunks('dhan_ohlcv_intraday', older_than => INTERVAL '7 days') c;"
else
  ok "no decompression debt: every chunk past the compress_after window is compressed."
fi

printf '%s\n' "  compression policy jobs:"
t "select j.job_id, j.hypertable_name, j.schedule_interval, j.config->>'compress_after' as compress_after,
          s.last_run_status, s.last_successful_finish, s.total_failures
   from timescaledb_information.jobs j
   left join timescaledb_information.job_stats s using (job_id)
   where j.proc_name = 'policy_compression';"

# =============================================================================
hdr "4. Intraday completeness (dhan_ohlcv_intraday)"
# =============================================================================
# EVERY read of dhan_ohlcv_intraday below is bounded. An unbounded aggregate over this
# table forces TimescaleDB to decompress all 262 chunks at once; that is what OOM'd the
# box on 2026-08-31 and killed postgres. Shape of the whole table therefore comes from
# catalog metadata (chunk ranges + reltuples), never from a scan.
CHUNK_LO="$(s "select min(range_start)::date from timescaledb_information.chunks
               where hypertable_name='dhan_ohlcv_intraday'")"
# Newest chunk's lower bound -- the only window we need to touch to find the latest bar.
# ::date deliberately: the s() helper strips ALL whitespace, so a bare timestamptz comes
# back as "2026-08-27 00:00:00+00" -> "2026-08-2700:00:00+00" and fails to parse. A date
# is whitespace-free and is still a lower bound inside (at worst just before) that chunk,
# so the scan stays confined to one chunk either way.
NEWEST_CHUNK_START="$(s "select max(range_start)::date from timescaledb_information.chunks
                         where hypertable_name='dhan_ohlcv_intraday'")"
INTRA_MAX="$(s "select max(timestamp)::date from dhan_ohlcv_intraday
                where timestamp >= '${NEWEST_CHUNK_START}'::date")"

printf '%s\n' "  table shape (catalog metadata -- approximate row count, NOT a scan):"
t "select (select count(*) from timescaledb_information.chunks
            where hypertable_name='dhan_ohlcv_intraday')                   as chunks,
          -- approximate_row_count() is compression-aware. Summing pg_class.reltuples
          -- across chunks is NOT: on a compressed chunk each stored tuple is a batch of
          -- ~1000 values, so that route under-reports by ~150x (3.5M vs 525M here).
          to_char(approximate_row_count('dhan_ohlcv_intraday'),
                  'FM999,999,999,999')                                     as approx_rows,
          '${CHUNK_LO}'::date                                              as earliest_chunk,
          '${INTRA_MAX}'::date                                             as latest_bar,
          pg_size_pretty(hypertable_size('dhan_ohlcv_intraday'))           as on_disk;"

LAST_TD="$(s "select max(date)::date from dim_trading_days where date <= now()")"
info "latest intraday bar = $INTRA_MAX ; last trading day = $LAST_TD"
if [ -n "$INTRA_MAX" ] && [ -n "$LAST_TD" ] && [ "$INTRA_MAX" \< "$LAST_TD" ]; then
  bad "intraday feed is stale: latest bar $INTRA_MAX, last trading day $LAST_TD."
  info "the 18:55 IST sync last failed on a Dhan auth/CDP timeout -- see logs/cron/dhan_intraday_sync.log."
else
  ok "intraday feed is current through $INTRA_MAX."
fi

# Universe symbols absent from recent intraday data.
# WAS: `except select distinct ticker from dhan_ohlcv_intraday` -- an unbounded
# distinct over all 859M rows, i.e. the same full-decompression scan that OOM'd the
# box. Bounded to the last N days instead, which is also the more useful question for
# an ongoing-completeness check: "is this symbol still being collected?" rather than
# "did it ever have a bar?". Use --deep for the whole-history version (chunk by chunk).
UNIV="$(s "select count(distinct symbol) from nseindia_ohlcv
           where series in ('EQ','BE')
             and date >= (select max(date) from nseindia_ohlcv) - interval '7 days'")"
MISSING="$(s "select count(*) from (
   select distinct symbol from nseindia_ohlcv
    where series in ('EQ','BE')
      and date >= (select max(date) from nseindia_ohlcv) - interval '7 days'
   except
   select distinct ticker from dhan_ohlcv_intraday
    where timestamp >= now() - interval '${INTRADAY_WINDOW_DAYS} days') x")"
info "active equity universe = $UNIV symbols"
[ "${MISSING:-0}" -gt 0 ] 2>/dev/null && warn "$MISSING universe symbol(s) have NO intraday rows in the last ${INTRADAY_WINDOW_DAYS} days." \
                                      || ok "every universe symbol has intraday data in the last ${INTRADAY_WINDOW_DAYS} days."

# Truncation is an early session END, measured per SESSION -- deliberately NOT
# "ticker-days with fewer than 375 bars". A 1-min bar only exists where a trade
# happened, so 20-33% of this universe legitimately has far fewer than a full
# session's 375 bars on any normal day (observed minimum: 2, on a day whose median
# was a full 375). Counting those measured illiquidity, not damage, and reported
# 13,256 "truncated ticker-days" on a window where every single session actually ran
# to the close. The `_ist_now` bug (fixed 2026-08-30) cut each day's CLOSE off, which
# shows up as the whole session's median last bar landing early -- robust to thin
# names, and the actual failure signature.
printf '%s\n' "  session end times (IST) over the last ${INTRADAY_WINDOW_DAYS} days -- a full session ends 15:29:"
t "with per_ticker as (
     select timestamp::date as session, ticker, max(timestamp) as last_bar
       from dhan_ohlcv_intraday
      where timestamp >= now() - make_interval(days => ${INTRADAY_WINDOW_DAYS})
      group by 1,2)
   select session, count(*) as tickers,
          (percentile_disc(0.5) within group (order by last_bar)
             at time zone 'Asia/Kolkata')::time as median_last_ist
     from per_ticker group by 1 order by 1 desc limit 10;"
SHORT="$(s "with per_ticker as (
     select timestamp::date as session, ticker, max(timestamp) as last_bar
       from dhan_ohlcv_intraday
      where timestamp >= now() - make_interval(days => ${INTRADAY_WINDOW_DAYS})
      group by 1,2),
   per_session as (
     select session, (percentile_disc(0.5) within group (order by last_bar)
                        at time zone 'Asia/Kolkata')::time as median_last_ist
       from per_ticker group by 1)
   select count(*) from per_session
    where median_last_ist < time '15:20' and session < current_date")"
[ "${SHORT:-0}" -gt 0 ] 2>/dev/null && bad "$SHORT complete session(s) in the last ${INTRADAY_WINDOW_DAYS} days ended before 15:20 IST -- close truncated." \
                                    || ok "every complete session in the last ${INTRADAY_WINDOW_DAYS} days ran to the close."
info "the truncation cause is recorded in data/dhanlive/ohlcv.py:_ist_now (fixed 2026-08-30)."

if [ "$DEEP" = "1" ]; then
  # --deep: the whole-history questions. These CANNOT be asked with a single aggregate
  # -- `min(timestamp) group by security_id` over the full table is precisely the query
  # that decompressed 262 chunks at once and OOM'd postgres on 2026-08-31. So walk the
  # chunks one at a time: each step decompresses exactly one chunk, and peak memory is
  # bounded by the largest single chunk instead of the whole hypertable. Slower on
  # purpose. Results accumulate in a temp table.
  info "--deep: walking $(s "select count(*) from timescaledb_information.chunks where hypertable_name='dhan_ohlcv_intraday'") chunks one at a time (bounded memory, not fast)"

  DEEP_SQL="/tmp/stockey_audit_deep_$$.sql"
  {
    echo "create temp table _audit_sec (security_id bigint primary key, earliest timestamptz);"
    echo "create temp table _audit_tick (ticker text primary key);"
  } > "$DEEP_SQL"
  # One statement per chunk. Ordered oldest-first so `earliest` settles early and the
  # ON CONFLICT below degenerates to a no-op for most chunks.
  r "select format('%I.%I', chunk_schema, chunk_name)
       from timescaledb_information.chunks
      where hypertable_name='dhan_ohlcv_intraday'
      order by range_start" | while read -r chunk; do
    [ -z "$chunk" ] && continue
    cat >> "$DEEP_SQL" <<EOSQL
insert into _audit_sec (security_id, earliest)
  select security_id, min(timestamp) from $chunk group by 1
  on conflict (security_id) do update set earliest = least(_audit_sec.earliest, excluded.earliest);
insert into _audit_tick (ticker) select distinct ticker from $chunk on conflict do nothing;
EOSQL
  done
  cat >> "$DEEP_SQL" <<'EOSQL'
\echo '  securities and their earliest stored bar:'
select count(*) as securities,
       count(*) filter (where earliest > now() - interval '5 years' + interval '5 days') as never_skippable,
       round(100.0 * count(*) filter (where earliest > now() - interval '5 years' + interval '5 days')
             / nullif(count(*),0), 1) as pct
  from _audit_sec;
\echo '  universe symbols with ZERO intraday history, ever:'
select count(*) as symbols_never_collected
  from (select distinct symbol from nseindia_ohlcv
         where series in ('EQ','BE')
           and date >= (select max(date) from nseindia_ohlcv) - interval '7 days'
        except
        select ticker from _audit_tick) x;
EOSQL

  psql -X -q -h "$PGHOST_" -p "$PGPORT_" -U "$PGUSER_" -d "$PGDB_" -f "$DEEP_SQL" 2>&1 | sed 's/^/  /'
  rm -f "$DEEP_SQL"
  info "the 'never_skippable' count is the resume-check blast radius (backfill_intraday_5yr.py:154):"
  info "each of those securities is re-fetched and re-upserted in full on every backfill restart."
else
  info "(default: full-history scans skipped -- they need --deep, which walks chunk by chunk)"
fi

# =============================================================================
hdr "5. Daily completeness (bhavcopy, Dhan daily, adjustment factors)"
# =============================================================================
t "select 'nseindia_ohlcv' as tbl, max(date)::date as latest,
          count(*) filter (where date = (select max(date) from nseindia_ohlcv where series='EQ')) as latest_day_rows
     from nseindia_ohlcv where series='EQ'
   union all
   select 'dhan_ohlcv_daily', max(date)::date,
          count(*) filter (where date = (select max(date) from dhan_ohlcv_daily))
     from dhan_ohlcv_daily
   union all
   select 'nseindia_indices (NIFTY 50)', max(date)::date, null
     from nseindia_indices where index_name ilike 'nifty 50'
   union all
   select 'nseindia_adjustment_factors', max(date)::date, null
     from nseindia_adjustment_factors;"

BHAV_ROWS="$(s "select count(*) from nseindia_ohlcv where series='EQ' and date=(select max(date) from nseindia_ohlcv where series='EQ')")"
[ "${BHAV_ROWS:-0}" -lt 1500 ] 2>/dev/null && bad "latest bhavcopy day has only $BHAV_ROWS EQ rows (< DATA_READINESS_BHAVCOPY_MIN_ROWS=1500)." \
                                           || ok "latest bhavcopy day has $BHAV_ROWS EQ rows."

# advisory_adjusted_ohlcv_daily is an INNER JOIN of nseindia_ohlcv x
# nseindia_adjustment_factors. Any EQ/BE bar without a factor row silently
# vanishes from systrader's PRIMARY equity series -- no error, just missing data.
printf '%s\n' "  adjustment-factor join integrity (the view is an INNER JOIN):"
t "select (select count(*) from nseindia_ohlcv where series in ('EQ','BE')) as price_bars,
          (select count(*) from nseindia_adjustment_factors)                as factor_rows,
          (select count(*) from advisory_adjusted_ohlcv_daily)              as view_rows;"
ORPHAN="$(s "select (select count(*) from nseindia_ohlcv where series in ('EQ','BE'))
                  - (select count(*) from advisory_adjusted_ohlcv_daily)")"
if [ "${ORPHAN:-0}" -gt 0 ] 2>/dev/null; then
  bad "$ORPHAN EQ/BE price bars have no adjustment-factor row -- silently dropped from the adjusted view."
  t "select symbol, min(date)::date as from_date, max(date)::date as to_date, count(*) as bars
     from nseindia_ohlcv o where series in ('EQ','BE')
       and not exists (select 1 from nseindia_adjustment_factors f
                        where f.symbol = o.symbol and f.date = o.date)
     group by 1 order by 4 desc limit 10;"
else
  ok "every EQ/BE price bar has a matching adjustment-factor row."
fi

# The nightly full rewrite: load_ts is stamped fresh on every row, so every row
# differs from what is stored and DO UPDATE writes a new version for all of them.
FACT_ROWS="$(s "select count(*) from nseindia_adjustment_factors")"
FACT_CHUNKS="$(s "select count(*) from timescaledb_information.chunks where hypertable_name='nseindia_adjustment_factors'")"
warn "price_adjustment rewrites all $FACT_ROWS rows across $FACT_CHUNKS chunks every night (price_adjustment.py:463 stamps load_ts on every row)."
info "measured 13:20:00 -> 13:23:03 on 2026-08-28; ~2-4 GB WAL/night for data that almost never changes."

DTD_MAX="$(s "select max(date)::date from dim_trading_days")"
info "dim_trading_days populated through $DTD_MAX"
if [ -n "$DTD_MAX" ] && [ "$DTD_MAX" \< "$(date -u -d '+90 days' +%Y-%m-%d)" ]; then
  warn "dim_trading_days runs out on $DTD_MAX and has no producer in the codebase (known backlog item)."
fi

# =============================================================================
hdr "6. Host / process state"
# =============================================================================
printf '%s\n' "  disk:"; df -h / | sed 's/^/    /'
printf '%s\n' "  memory:"; free -g | sed 's/^/    /'
SB_MB="$(s "select setting::bigint*8/1024 from pg_settings where name='shared_buffers'")"
TOT_GB="$(free -g | awk '/^Mem:/{print $2}')"
info "shared_buffers = ${SB_MB}MB of ${TOT_GB}GB RAM -- a 25 GB python process on top of this is what froze the box."

printf '%s\n' "  largest resident processes:"
ps -eo pid,rss,etime,comm --sort=-rss 2>/dev/null | head -6 | sed 's/^/    /'
# Match only real python invocations, and never this script's own process tree --
# `pgrep -f <pattern>` happily matches the subshell that carries the pattern in its
# own argv (the pkill -f self-match footgun).
while read -r pid rss args; do
  [ -z "${pid:-}" ] && continue
  warn "long job RUNNING: pid $pid, rss $(( rss / 1024 )) MB -- $(echo "$args" | cut -c1-90)"
done < <(ps -eo pid=,rss=,args= 2>/dev/null \
  | grep -E '(^| )[^ ]*python[0-9.]* .*(backfill_intraday_5yr|intraday_daily_sync|run_pipeline|data_readiness|price_adjustment)' \
  | grep -v incident_audit)
pgrep -f '[g]o-crond' >/dev/null 2>&1 && ok "go-crond is alive" || bad "go-crond is NOT running -- no stockey cron job will fire."

printf '%s\n' "  stale job locks in /tmp (a held lock silently no-ops the next cron run):"
found_lock=0
for l in /tmp/stockey_*.lock; do
  [ -e "$l" ] || continue
  found_lock=1
  age_min=$(( ( $(date +%s) - $(stat -c %Y "$l") ) / 60 ))
  if [ "$age_min" -gt 360 ]; then warn "$l held for ${age_min} min"; else info "$l (${age_min} min)"; fi
done
[ "$found_lock" = "0" ] && ok "no stockey job locks held."

pgrep -x earlyoom >/dev/null 2>&1 \
  && { ok "earlyoom is running (kills a runaway before the box freezes)"
       systemctl is-enabled earlyoom >/dev/null 2>&1 \
         && info "and it is enabled at boot" \
         || warn "earlyoom is NOT enabled at boot -- it will not come back after a reboot"; } \
  || warn "earlyoom is not running -- nothing will pre-empt the next runaway allocation."

# =============================================================================
hdr "7. Code defects this audit does NOT fix"
# =============================================================================
cat <<'NOTES' | sed 's/^/  /'
These are the write-volume and memory causes. Config alone cannot fix them.

  scripts/backfill_intraday_5yr.py:131-171
      flush_batch() is called inside the per-symbol try, and batch_frames.clear()
      is its last statement. Any upsert failure unwinds to the handler at :169,
      which counts it and continues -- the list is never cleared and grows for the
      rest of the run. ~59 MB per retained symbol; 25.6 GB is ~430 symbols.
      -> clear in a finally:, and cap retained rows, not retained frames.

  scripts/backfill_intraday_5yr.py:154
      target_start is recomputed as now-5y each launch, so a symbol whose history
      genuinely starts later can never be skipped and is re-fetched in full every
      restart. Section 4 above prints exactly how many symbols that is.
      -> persist per-symbol completion state.

  utils/db.py:551,706-719
      every write is ON CONFLICT DO UPDATE SET <all columns>. Against
      dhan_ohlcv_intraday (859M rows, all 262 chunks compressed) each one forces
      decompress -> update -> recompress, all WAL-logged.
      -> DO NOTHING for immutable historical bars; stage in an uncompressed table.

  data/nseindia/price_adjustment.py:463
      out["load_ts"] = now guarantees every one of the 5.86M rows differs from
      what is stored, so the nightly DO UPDATE rewrites the whole table.
      -> drop load_ts from the update set and add an IS DISTINCT FROM predicate.
NOTES

# =============================================================================
hdr "Summary"
# =============================================================================
if [ "$FINDINGS" = "0" ]; then
  printf '  %sno findings%s\n' "$GRN" "$OFF"
else
  printf '  %s%d finding(s) above%s\n' "$YEL" "$FINDINGS" "$OFF"
fi
[ "$APPLY_CONFIG" = "0" ] && printf '  %sread-only run -- nothing was changed. Re-run with --apply-config to apply section 1b.%s\n' "$DIM" "$OFF"
exit 0
