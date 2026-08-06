# Script Inventory

This is a maintained inventory of what actually runs. For the authoritative
table/module keep-vs-remove decision see `docs/DATA_INVENTORY.md`; this file
just lists commands. `data/download_runner.py`'s `DOWNLOADER_STEPS` /
`PARSER_STEPS` are the source of truth for what `complete_data.sh` runs and
in what order — check there first if this drifts.

## Cron scripts (top-level `*.sh`)

| Script | Schedule | What it does |
| --- | --- | --- |
| `complete_data.sh` | 07:10 + 17:30 | `python -m data.download_runner --phase all` — full download + parse pass |
| `all_downloaders_queue.sh` | 08:30/12:30/16:30 | `python -m data.download_queue` — enqueues single-client NSE/Dhan work |
| `all_external_workers.sh` | +5 min after queue | `python -m utils.external_task_queue --queue {dhan,nse} --worker --drain` — drains the queues |
| `all_ohlcv_reconcile.sh` | 18:45 | `python -m data.dhanlive.ohlcv_reconcile` — backfills symbols whose latest Dhan bar is stale |
| `all_price_adjustment.sh` | 18:50 | `python -m data.nseindia.price_adjustment` — rebuilds `advisory_adjusted_ohlcv_daily` (systrader's PRIMARY series) |
| `all_data_readiness.sh` | 22:30 | `python -m data.data_readiness --fix` — freshness checks + bounded repairs |
| `all_data_coverage_report.sh` | daily | `python scripts/data_coverage_report.py` — non-fatal per-table coverage/health report, see `docs/DATA_COVERAGE.md` |
| `start_cron.sh` | manual | supported way to (re)start `go-crond`: runs data readiness + OHLCV reconcile first, then execs `go-crond config/stockey.generated.crontab` |

All wrap through `scripts/run_with_markers.sh` (emits `[stockey.script] name=... status=start|done|failed`)
and most through `scripts/with_lock.sh` (skips overlapping runs instead of stacking them; `flock` on
Linux, `lockf` on macOS).

## `data.download_runner` registries (what `complete_data.sh` actually runs)

Downloaders (`DOWNLOADER_STEPS`), in order:

```
data.nseindia.holidays
data.dhanlive.scrip_master
data.sharpelydata.scrip_master
data.company_master
data.dhanlive.ohlcv
data.rbi.download_fbil_gsec
data.rbi.download_bank_rates
data.rbi.download_currency_rates
data.sharpelydata.sharpely_data          # mcap only — fundamentals slice was removed
data.nseindia.offmarket
data.nseindia.bhavcopy_downloader
data.nseindia.indices_downloader
```

Parsers (`PARSER_STEPS`), in order:

```
data.nseindia.offmarket_parser
data.nseindia.bhavcopy_parser
data.nseindia.adjusted_prices --only normalize
data.nseindia.indices_parser
data.benchmark_sync
```

`market_wide`, `benchmark_sync`, and `dhan_ohlcv_precheck` purposes are CRITICAL — a
failure there fails the whole run. Everything else (macro/RBI, mcap) is
non-critical: a flaky external source is visible but does not abort or block
the market-data steps ordered after it.

`data/nseindia/earnings_events.py` and `data/nseindia/recent_events.py` are
BORDERLINE (LLM-free, kept for possible FnO event-vol research) — present but
deliberately not registered above.

## Identity pipeline (run manually / after a rename event, not cron)

```sh
python -m data.nseindia.security_history
python -m data.nseindia.security_dimension
python -m data.nseindia.adjusted_prices --only all
```

Run sequentially, not in parallel — they touch the same derived identity
tables. See `docs/identity.md`.

## Dhan

```sh
python -m data.dhanlive.auth_cli status
python -m data.dhanlive.auth_cli refresh --clear-cache-first
python -m data.dhanlive.auth_cli validate
python -m data.dhanlive.ohlcv --symbols SHAKTIPUMP
python -m data.dhanlive.ohlcv --symbols NIFTY --asset-type benchmark --exchange NSE
python -m data.dhanlive.ohlcv_pull RELIANCE
python -m data.dhanlive.ohlcv_reconcile --dry-run --format json
```

## Management / DB-maintenance scripts (`scripts/`)

| Script | Use |
| --- | --- |
| `cron_preflight.py` | validates go-crond setup before startup (required env, referenced scripts, stale locks, Python resolution) |
| `docs_state_audit.py --strict` | audits docs for stale terminology |
| `env_example_audit.py --strict` | checks `.env.example` stays in sync |
| `price_data_sanity.py` | data-health: CA-splits/EQ-BE/cross-source/benchmark gap checks (informational) |
| `data_coverage_report.py` | per-KEEP-table row counts, staleness, dupes — see `docs/DATA_COVERAGE.md` |
| `fallback_telemetry_coverage_report.py --format json` | triage source/API degradation events recorded via `utils/fallback_telemetry.py` |
| `db_retry_coverage_report.py` | DB-retry telemetry coverage |
| `ingestion_state_runner.py` | inspect/clear failed/empty-valid file-level ingestion state |
| `db_size_report.py`, `heavy_payload_inventory.py`, `db_table_retention_report.py`, `hot_table_retention.py`, `db_duplicate_index_report.py`, `drop_duplicate_indexes.py` | Postgres size/retention/index maintenance — all report-first, dry-run by default |
| `archive_legacy_nse_tables.py` | dry-run / S3-archive / delete utility for old legacy NSE rows, chunked by month |
| `cleanup_deprecated_tables.py --dry-run` | drops tables no longer used by any KEEP-scope module |
| `rotate_logs.sh` | daily log rotation into per-day gzip archives under `logs/cron/archive/` |
| `sql_query_runner.py`, `redis_query_runner.py`, `s3_query_runner.py`, `agent_tool_runner.py` | JSON-first storage-inspection tool wrappers, see `docs/agents.md` |
| `start_chrome_cdp.sh` | starts the Chrome remote-debugging session Dhan/NSE browser automation needs |

Use `python -m utils.schema_migrations --migration-id <id> --description "..." --sql-file path/to/file.sql --dry-run`
before applying any schema change; it's checksum-protected and records status
in `stockey_schema_migrations`.

## General usage

- Prefer `python -m ...` from the repo root so relative config/`.env` loading
  behaves consistently.
- Symbol-specific loaders default to `config/tracked_symbols.txt`; daily
  derivation jobs use `config/watchlist_symbols.txt`. Override either with
  `--symbols` or `STOCKEY_SYMBOLS`.
