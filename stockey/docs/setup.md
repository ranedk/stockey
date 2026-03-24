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

Symbol-specific loaders default to [`config/tracked_symbols.txt`](/home/rane/code/stockey/config/tracked_symbols.txt). Daily derivation jobs can instead use [`config/watchlist_symbols.txt`](/home/rane/code/stockey/config/watchlist_symbols.txt). You can override either flow per run with `--symbols` or the `STOCKEY_SYMBOLS` env var.

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

### Dhan master

```sh
python -m data.dhanlive.scrip_master
```

### Dhan OHLCV

The Dhan historical loader supports two auth modes:

- `DHAN_ACCESS_TOKEN` directly, if you already have a valid user token
- API key consent flow using `DHAN_CLIENT_ID`, `DHAN_API_KEY`, and `DHAN_API_SECRET`

With the API key flow, the loader opens the Dhan consent URL in a normal browser. After login, paste the full redirected URL back into the same terminal; the loader extracts `tokenId`, exchanges it for an access token, and caches that token under `.cache/dhan_access_token.json` for later runs until expiry.

```sh
python -m data.dhanlive.ohlcv --symbols SHAKTIPUMP
python -m data.dhanlive.ohlcv --symbols NIFTY --asset-type benchmark --exchange NSE
```

By default the loader syncs:

- 5 years of daily candles
- the last 1 day of 1-minute intraday candles

Supported asset types are `stock`, `index`, and `benchmark`.

### Dhan screeners

The ScanX screener downloader uses Chrome remote debugging and reads active screener URLs from `dhan_screeners`.

Typical flow:

```sh
python -m data.dhanlive.screener_registry add https://scanx.trade/stock-screener/momentum-stocks-290258
python -m data.dhanlive.screener_registry list
python -m data.dhanlive.screener
python -m data.dhanlive.screener_registry latest --screener momentum-stocks-290258
```

Use `--raw` with `latest` if you want the full stored JSON payload instead of the summary view.

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

Use [`docs/crawl_schema.md`](/home/rane/code/stockey/docs/crawl_schema.md) and [`docs/feature_schema.md`](/home/rane/code/stockey/docs/feature_schema.md) as the maintained references. [`docs/schema.sql`](/home/rane/code/stockey/docs/schema.sql) now contains only targeted admin SQL instead of a full `pg_dump`.

## Agent-facing tool surface

The curated agent-safe commands live in:

- [`docs/tool_registry.json`](/home/rane/code/stockey/docs/tool_registry.json)
- [`scripts/agent_tool_runner.py`](/home/rane/code/stockey/scripts/agent_tool_runner.py)

Quick checks:

```sh
/home/rane/code/stockey/.xstockey/bin/python scripts/agent_tool_runner.py list
/home/rane/code/stockey/.xstockey/bin/python scripts/agent_tool_runner.py list --category storage
```

The runner uses the invoking interpreter for downstream Python commands, so starting it from the project venv keeps the entire agent tool chain in the same environment.

## Advisory implementation checklist

Use [`todo.md`](/home/rane/code/stockey/todo.md) as the maintained gap list for the investment advisory system described in [`docs/implementation.md`](/home/rane/code/stockey/docs/implementation.md). That file is the source of truth for what is already present and what still needs to be built.

Current advisory bootstrap commands:

```sh
/home/rane/code/stockey/.xstockey/bin/python -m advisory.screener_parser
/home/rane/code/stockey/.xstockey/bin/python -m advisory.macro_snapshot
/home/rane/code/stockey/.xstockey/bin/python -m advisory.fundamental_snapshot
/home/rane/code/stockey/.xstockey/bin/python -m advisory.regime_engine
/home/rane/code/stockey/.xstockey/bin/python -m advisory.peer_sync --symbols HDFCBANK
/home/rane/code/stockey/.xstockey/bin/python -m advisory.technical_features
/home/rane/code/stockey/.xstockey/bin/python -m advisory.rule_engine
/home/rane/code/stockey/.xstockey/bin/python -m advisory.watchlist_builder
/home/rane/code/stockey/.xstockey/bin/python -m advisory.announcement_watch
```

The fundamentals and technical builders refresh peer membership on normal runs before computing peer-relative features. The technical builder also fills missing peer OHLCV before computing `rs_vs_sector`. Use `--skip-peer-sync` to disable that preflight.

## Notes

1. `ISIN` is not unique. A single underlying can trade in multiple series.
2. `symbol + ISIN` is not unique across series.
3. Company renames usually change symbol, but not `ISIN`.
4. `dim_security_history` is the canonical source for rename continuity and review flags.
5. `dim_security_overrides` is where manual merger / demerger / scheme mappings should be curated.
6. Large runtime artifacts such as `base_chromed_data/` and `http_cache/` should stay out of git.
7. Run `security_history`, `security_dimension`, `adjusted_prices`, and `features.price_daily` sequentially, not in parallel.
8. `all_daily_derivations.sh` uses `config/watchlist_symbols.txt` and falls back to `config/tracked_symbols.txt` if the watchlist file is empty.
