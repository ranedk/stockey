# Setup

## Python environment

Create the project environment and VS Code settings:

```sh
python builder.py
source .xstockey/bin/activate
```

`builder.py` is now safe to import and only runs when executed directly.

## PostgreSQL

```sh
sudo -u postgres psql
```

```sql
CREATE DATABASE stockey;
CREATE USER stockey WITH ENCRYPTED PASSWORD 'stockey';
GRANT ALL PRIVILEGES ON DATABASE stockey TO stockey;
ALTER DATABASE stockey OWNER TO stockey;
\c stockey
GRANT ALL ON SCHEMA public TO stockey;
GRANT USAGE ON SCHEMA public TO stockey;
CREATE EXTENSION IF NOT EXISTS timescaledb;
```

## Redis backup

```sh
python utils/redis_bkp_restore.py --host localhost --port 6379 --db 0 --file backups/redis_global_backup.json backup
```

## S3 backup layout

```sh
aws s3 ls s3://stockeydata/
```

Expected prefixes:

- `bhavcopy/`
- `nsedeals/`
- `pgdump/`
- `rdbdump/`

## Data loaders

Symbol-specific loaders default to [`config/tracked_symbols.txt`](../config/tracked_symbols.txt). Daily derivation jobs can instead use [`config/watchlist_symbols.txt`](../config/watchlist_symbols.txt). You can override either flow per run with `--symbols` or the `STOCKEY_SYMBOLS` env var.

## Orchestration scripts

The repo now has three top-level orchestration layers:

1. Raw daily downloads:

```sh
./all_downloads.sh
```

2. Daily incremental derivations for the watchlist:

```sh
./all_daily_derivations.sh
```

3. On-demand backfills:

```sh
./all_backfill.sh
./all_backfill.sh watchlist 5
./all_backfill.sh tracked 5
./all_backfill.sh all 5
```

`all_downloader.sh` and `all_features.sh` are kept as compatibility wrappers for the first two flows.

## Advisory Flow Summary

1. Run raw ingestion for Dhan, Sharpely, macro, NSE, announcements, and ET RSS.
2. Sync only registered production Screener.in screeners.
3. Use ad hoc Screener.in queries separately for research.
4. Normalize stored Screener snapshots into `advisory_screener_constituents`.
5. Build daily advisory snapshots for macro, fundamentals, technicals, and optional intraday features.
6. Build the base regime and the lightweight news overlay.
7. Detect active investment themes and map them to theme-linked production screeners when available.
8. Run the rule engine on each setup using screener universe plus snapshots, regime, overlay, and optional intraday confirmation.
9. Score candidates and assign states like `PASS_NOW`, `WATCH_*`, `ABSTAIN`, or `REJECT`.
10. Build the watchlist with screener, regime, overlay, and theme provenance.
11. Ingest announcements and relevant news for watched names.
12. Use the LLM only to extract structured event tensors from text.
13. Run deterministic adversarial review to clear, penalize, flag manual review, or veto.
14. Feed candidate state plus event outputs into risk sizing and allocation.
15. Rank approved allocations in the portfolio engine with overlap and setup caps.
16. Build lifecycle and execution-planning outputs.
17. Record research runs in the research ledger.
18. Prepare event-model training data with `python -m advisory.event_model_data_prep`.
19. Train the XGBoost event meta-model only when label coverage is sufficient.
20. Keep prediction separate from policy and execution.

Primary operator commands:

```sh
./all_full_advisory.sh
./all_model_training.sh
./all_continuous_watch.sh --loop
./all_live_notifier.sh
```

Use:

- `./all_full_advisory.sh` for the main operator flow
- `./all_model_training.sh` for research prep, readiness checks, model train, and score
- `./all_continuous_watch.sh --loop` for the lightweight live monitoring loop
- `./all_live_notifier.sh` for a human-readable operator feed from the Redis bus

### Dhan master

```sh
python -m data.dhanlive.scrip_master
```

### Dhan OHLCV

The Dhan historical loader supports two auth modes:

- `DHAN_ACCESS_TOKEN` directly, if you already have a valid user token
- API key consent flow using `DHAN_CLIENT_ID`, `DHAN_API_KEY`, and `DHAN_API_SECRET`

With the API key flow, the loader opens the Dhan consent URL in a normal browser. After login, paste the full redirected URL back into the same terminal; the loader extracts `tokenId`, exchanges it for an access token, and caches that token under `.cache/dhan_access_token.json` for later runs until expiry.

If the cached token becomes invalid before its stored expiry, refresh it directly with:

```sh
python -m data.dhanlive.auth_cli status
python -m data.dhanlive.auth_cli refresh --clear-cache-first
python -m data.dhanlive.auth_cli validate
```

```sh
python -m data.dhanlive.ohlcv --symbols SHAKTIPUMP
python -m data.dhanlive.ohlcv --symbols NIFTY --asset-type benchmark --exchange NSE
```

By default the loader syncs:

- 5 years of daily candles
- the last 1 day of 1-minute intraday candles

Supported asset types are `stock`, `index`, and `benchmark`.

### Screener.in screeners

The recurring Screener.in downloader reads active production screener URLs from `screenerin_screeners`.

Typical flow:

```sh
python -m data.screenerin.screener_registry add "https://www.screener.in/screens/1234567/my-production-screen/"
python -m data.screenerin.screener_registry list
python -m data.screenerin.screener_parser
python -m data.screenerin.screener_registry latest --screener my-production-screen
```

Use `--raw` with `latest` if you want the full stored JSON payload instead of the summary view.

For one-off research, use ad hoc queries instead of registering everything:

```sh
python -m data.screenerin.ad_hoc_query --name "Deep Value ROCE" --query "Market capitalization > 500 AND Price to earning < 15 AND Return on capital employed > 22%"
```

### Sharpely masters

```sh
python -m data.sharpelydata.scrip_master
```

### Sharpely fundamentals

```sh
python -m data.sharpelydata.sharpely_data
python -m data.sharpelydata.sharpely_data --symbols RELIANCE TCS --from-date 2024-01-01 --to-date 2024-12-31
```

Key extractors:

- `get_financial_statement(symbol)`
- `get_shareholding(symbol)`
- `get_historical_mcap(symbol)`

### US macro and ISM

```sh
python -m data.fred.us_macro
```

This writes to `macro_usa`, `macro_india_gdp`, and `macro_usa_ism`.

### NSE bhavcopy and related parsers

This is the legacy/reference NSE pipeline. It is still useful for identity maintenance, reconciliation, and historical audit work, but it is no longer required for the advisory runtime path.

Downloaders:

```sh
python -m data.nseindia.bhavcopy_downloader
python -m data.nseindia.indices_downloader
python -m data.nseindia.offmarket
```

Parsers:

```sh
python -m data.nseindia.bhavcopy_parser
python -m data.nseindia.security_history
python -m data.nseindia.security_dimension
python -m data.nseindia.adjusted_prices --only all
python -m features.price_daily
python -m data.nseindia.indices_parser
python -m data.nseindia.offmarket_parser
python -m data.nseindia.corporate_actions --symbols RELIANCE TCS --from-date 2024-01-01 --to-date 2024-12-31
python -m data.nseindia.earnings_events --symbols RELIANCE TCS
python -m data.nseindia.insider_deals --symbols RELIANCE TCS
```

Recommended cron shape:

```sh
./all_downloads.sh
./all_daily_derivations.sh
```

Use `all_backfill.sh` manually or from a separate weekly/repair schedule.

Chrome remote debugging is still required for the Playwright/browser-driven flows:

```sh
/opt/google/chrome/chrome --remote-debugging-port=9222 --user-data-dir=./chromesetup
```

### RBI

```sh
python -m data.rbi.download_bank_rates
python -m data.rbi.download_fbil_gsec
```

### CPI / WPI / FPI

```sh
python -m data.mospi.cpi
python -m data.eaindustry.wpi
python -m data.nsdl.fpi
```

## Schema docs

Human-readable table summaries:

```sh
python -m utils.db_schema_dump --schemas public
```

Use [`docs/crawl_schema.md`](crawl_schema.md) and [`docs/feature_schema.md`](feature_schema.md) as the maintained references. [`docs/schema.sql`](schema.sql) now contains only targeted admin SQL instead of a full `pg_dump`.

## Agent-facing tool surface

The curated agent-safe commands live in:

- [`docs/tool_registry.json`](tool_registry.json)
- `scripts/agent_tool_runner.py`

Quick checks:

```sh
python scripts/agent_tool_runner.py list
python scripts/agent_tool_runner.py list --category storage
```

The runner uses the invoking interpreter for downstream Python commands, so starting it from the project venv keeps the entire agent tool chain in the same environment.

## Advisory roadmap

Use [`todo.md`](../todo.md) as the maintained roadmap for current priorities, bottlenecks, and next implementation steps.

Use [`docs/implementation.md`](implementation.md) as the current architecture summary for the live advisory stack.

For day-to-day operation and maintenance, use [`docs/advisory_manual.md`](advisory_manual.md). That is the practical runbook for:

- running the advisory stack
- adding or removing screeners
- changing setup rules
- understanding stage ownership
- debugging outputs and failures

For the short command-focused runbook, use [`docs/operators_manual.md`](operators_manual.md).

For copy-paste change recipes, use [`docs/advisory_change_cookbook.md`](advisory_change_cookbook.md).

Current advisory bootstrap commands:

```sh
python -m advisory.screener_parser
python -m advisory.macro_snapshot
python -m advisory.fundamental_snapshot
python -m advisory.regime_engine
python -m advisory.peer_sync --symbols HDFCBANK
python -m advisory.technical_features
python -m advisory.rule_engine
python -m advisory.watchlist_builder
python -m data.economictimes.rss
python -m advisory.announcement_watch
python -m advisory.news_watch --refresh-feeds
python -m advisory.symbol_trace HDFCBANK --format text
python -m advisory.setup_trace LARGECAP_BREAKOUT_V1 --format text
python -m advisory.dashboard --format text
python -m advisory.llm_event_evaluator
python -m advisory.risk_engine
python -m advisory.portfolio_engine
python -m advisory.position_lifecycle
python -m advisory.execution_engine
python -m advisory.pipeline --dry-run --stop-at portfolio
python -m advisory.pipeline --include-watch --include-news --dry-run
python -m pytest tests/test_advisory_regression.py
python scripts/cleanup_deprecated_tables.py --dry-run
```

For advisory execution, use Dhan daily OHLCV as the canonical price source. The NSE bhavcopy and adjusted-price jobs remain optional reference pipelines and are no longer required by the advisory technical/rule stack.

The fundamentals and technical builders refresh peer membership on normal runs before computing peer-relative features. The technical builder also fills missing peer OHLCV before computing `rs_vs_sector`. Use `--skip-peer-sync` to disable that preflight.

For live order placement through Dhan, the API static IP must be whitelisted on the Dhan side. Use `advisory.execution_engine` without `--live` to stage and inspect broker handoff rows safely before any live submission.

## Notes

1. `ISIN` is not unique. A single underlying can trade in multiple series.
2. `symbol + ISIN` is not unique across series.
3. Company renames usually change symbol, but not `ISIN`.
4. `dim_security_history` is the canonical source for rename continuity and review flags.
5. `dim_security_overrides` is where manual merger / demerger / scheme mappings should be curated.
6. Large runtime artifacts such as `base_chromed_data/` and `http_cache/` should stay out of git.
7. Run `security_history`, `security_dimension`, `adjusted_prices`, and `features.price_daily` sequentially, not in parallel when you need the legacy NSE identity/adjusted-price reference pipeline.
8. `all_daily_derivations.sh` uses `config/watchlist_symbols.txt` and falls back to `config/tracked_symbols.txt` if the watchlist file is empty.
