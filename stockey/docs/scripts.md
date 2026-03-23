# Script Inventory

## Ingestion rules

Every ingestion script should declare or at least follow these operational fields:

- `script`: import path from repo root
- `db`: destination table
- `frequency`: source cadence, not cron cadence
- `handle_date`: how source dates map into the stored time index
- `redis_key`: optional cursor key for incremental loads

All crawlers are allowed to run daily. Non-daily sources should exit early when nothing new is available.

## Current loaders

| Script | Tables | Notes |
| --- | --- | --- |
| `data/eaindustry/wpi.py` | `eaindustry_wpi` | Monthly WPI |
| `data/fred/us_macro.py` | `macro_usa`, `macro_india_gdp`, `macro_usa_ism` | FRED + FXStreet ISM |
| `data/mospi/cpi.py` | `mospi_cpi` | Detailed CPI |
| `data/nsdl/fpi.py` | `fii_investments`, `fii_derivatives` | NSDL flows; now coerces numeric fields before DB load |
| `data/rbi/download_bank_rates.py` | `rbi_bank_rates` | RBI key policy rates |
| `data/rbi/download_fbil_gsec.py` | `fbil_gsec_quote`, `fbil_gsec_par` | G-sec quotes + par curve |
| `data/sharpelydata/scrip_master.py` | `master_sharpely_funds`, `master_sharpely_equity` | Security masters |
| `data/company_master.py` | `company_master` | Unified company identity built from Sharpely + Dhan masters |
| `data/sharpelydata/sharpely_data.py` | `stmt_income`, `stmt_balancesheet`, `stmt_cashflow`, `shareholding_category`, `shareholding_top_holders`, `historical_mcap` | Fundamental data |
| `data/dhanlive/scrip_master.py` | `master_dhan_instruments` | Versioned Dhan instrument master |
| `data/dhanlive/ohlcv.py` | `dhan_ohlcv_daily`, `dhan_ohlcv_intraday` | Dhan OHLCV for `stock`, `index`, and `benchmark`; default sync is 5 years daily plus last 1 day of 1-minute bars |
| `data/dhanlive/screener.py` | `dhan_screener_snapshots` | Stores rendered ScanX screener `ng-state` JSON by screener slug and date |
| `data/dhanlive/screener_registry.py` | `dhan_screeners` | Registry utility to add/list/remove screeners and inspect latest stored snapshots |
| `data/nseindia/bhavcopy_parser.py` | `nseindia_*` daily tables | Parses downloaded NSE archives |
| `data/nseindia/adjusted_prices.py` | `nseindia_corporate_actions_normalized`, `nseindia_ohlcv_adjusted` | Normalizes action text and builds split/bonus-adjusted OHLCV |
| `data/nseindia/security_history.py` | `dim_security_history`, `dim_security_review_events`, `dim_security_overrides` | Builds canonical security identity history and review queue for renames / identity breaks |
| `data/nseindia/security_dimension.py` | `dim_security` | Current canonical security dimension keyed by `security_id` |
| `data/nseindia/indices_parser.py` | `nseindia_indices` | Index history |
| `data/nseindia/corporate_actions.py` | `nseindia_corporate_actions` | Corporate actions |
| `data/nseindia/earnings_events.py` | `nseindia_earnings_events` | Earnings calendar |
| `data/nseindia/insider_deals.py` | `nseindia_insider_deals` | Insider deals |
| `data/nseindia/offmarket_parser.py` | `nseindia_block_deals`, `nseindia_bulk_deals`, `nseindia_short_selling` | Off-market parsers |
| `data/nseindia/recent_events.py` | `nseindia_events` | NSE event feed |
| `data/nseindia/holidays.py` | `nseindia_holidays` | Trading holidays |
| `data/announcements/cli.py` | `announcement_pipeline_documents`, `announcement_pipeline_reports` | Exchange announcement ingest keyed by `company_master_id` |
| `data/backfill_company_master_ids.py` | many existing symbol-based tables | Adds and backfills `company_master_id` on historical rows |

## Management scripts

These are the JSON-first scripts that are easiest to call from a human shell, an LLM tool wrapper, or an MCP server shim.

## General usage guidelines

- Prefer `python -m ...` from the repo root so relative config and `.env` loading behave consistently.
- Use the project venv when running ingestion jobs manually:

```sh
/home/rane/code/stockey/.xstockey/bin/python -m ...
```

- Symbol-scoped loaders fall back in this order:
  1. `--symbols`
  2. `STOCKEY_SYMBOLS`
  3. [`config/tracked_symbols.txt`](/home/rane/code/stockey/config/tracked_symbols.txt)
- Browser-driven loaders require Chrome remote debugging when they connect over CDP:

```sh
/opt/google/chrome/chrome --remote-debugging-port=9222 --user-data-dir=./chromesetup
```

- Dhan OHLCV auth falls back in this order:
  1. `DHAN_ACCESS_TOKEN`
  2. cached token at `.cache/dhan_access_token.json`
  3. API key consent flow using `DHAN_CLIENT_ID`, `DHAN_API_KEY`, `DHAN_API_SECRET`
- In the API key flow, the script opens the Dhan consent page in the browser and waits for you to paste the redirected URL back into the terminal. The access token is then cached until expiry.

## Orchestration scripts

| Script | Purpose | Scope |
| --- | --- | --- |
| `all_downloads.sh` | Daily raw ingestion and parsing | Market-wide downloads plus tracked-symbol loaders |
| `all_daily_derivations.sh` | Daily incremental derived data | Watchlist symbols from `config/watchlist_symbols.txt` |
| `all_backfill.sh` | On-demand repair and historical rebuilds | `watchlist`, `tracked`, or `all` |
| `all_downloader.sh` | Compatibility wrapper | Delegates to `all_downloads.sh` |
| `all_features.sh` | Compatibility wrapper | Delegates to `all_daily_derivations.sh` |

## Symbol-specific module runs

These modules still work cleanly with `python -m ...`, which is a good fit for cron and shell orchestration. The entrypoints now support:

- `--symbols`
- `--from-date YYYY-MM-DD`
- `--to-date YYYY-MM-DD`

If `--symbols` is omitted, they fall back to:

1. `STOCKEY_SYMBOLS`
2. [`config/tracked_symbols.txt`](/home/rane/code/stockey/config/tracked_symbols.txt)

Examples:

```sh
python -m data.sharpelydata.sharpely_data --symbols RELIANCE TCS --from-date 2024-01-01 --to-date 2024-12-31
python -m data.dhanlive.ohlcv --symbols SHAKTIPUMP
python -m data.dhanlive.ohlcv --symbols SHAKTIPUMP --only daily
python -m data.dhanlive.ohlcv --symbols NIFTY --asset-type benchmark --exchange NSE
python -m data.dhanlive.ohlcv --symbols BANKNIFTY --asset-type index --exchange NSE --only intraday
python -m data.dhanlive.screener_registry add https://scanx.trade/stock-screener/momentum-stocks-290258
python -m data.dhanlive.screener_registry list
python -m data.dhanlive.screener_registry latest --screener momentum-stocks-290258
python -m data.dhanlive.screener https://scanx.trade/stock-screener/momentum-stocks-290258
python -m data.nseindia.corporate_actions --symbols RELIANCE,TCS
python -m data.nseindia.earnings_events
python -m data.nseindia.insider_deals
python -m data.nseindia.adjusted_prices --only all
python -m data.nseindia.security_history
python -m data.nseindia.security_dimension
python -m data.announcements.cli --ticker SHAKTIPUMP --exchange NSE --from-date 2026-03-01 --to-date 2026-03-22
python -m features.price_daily
```

## Dhan usage

### OHLCV

Module: `data.dhanlive.ohlcv`

Default behavior:

- syncs 5 years of daily candles
- syncs the last 1 day of 1-minute intraday candles
- defaults to `asset_type=stock`
- supports `asset_type=stock|index|benchmark`

Common usage:

```sh
python -m data.dhanlive.ohlcv --symbols SHAKTIPUMP
python -m data.dhanlive.ohlcv --symbols SHAKTIPUMP --only daily
python -m data.dhanlive.ohlcv --symbols SHAKTIPUMP --only intraday
python -m data.dhanlive.ohlcv --symbols NIFTY --asset-type benchmark --exchange NSE
python -m data.dhanlive.ohlcv --symbols BANKNIFTY --asset-type index --exchange NSE --only intraday
python -m data.dhanlive.ohlcv --symbols SHAKTIPUMP --from-date 2025-01-01 --to-date 2025-12-31 --only daily
```

Operational notes:

- Stocks resolve through `company_master`.
- Indices and benchmarks resolve directly from `master_dhan_instruments`.
- Stored tables:
  - `dhan_ohlcv_daily`
  - `dhan_ohlcv_intraday`
- Key columns:
  - `asset_type`
  - `exchange`
  - `ticker`
  - `security_id`
  - `company_master_id` for stocks, nullable for indices/benchmarks

### ScanX screeners

Registry utility: `data.dhanlive.screener_registry`

Downloader: `data.dhanlive.screener`

Recommended workflow:

1. Register one or more screeners.
2. Verify the registry contents.
3. Let `all_downloads.sh` or `python -m data.dhanlive.screener` sync the active registry daily.
4. Inspect latest snapshots with the registry utility.

Commands:

```sh
python -m data.dhanlive.screener_registry add https://scanx.trade/stock-screener/momentum-stocks-290258
python -m data.dhanlive.screener_registry list
python -m data.dhanlive.screener
python -m data.dhanlive.screener_registry latest
python -m data.dhanlive.screener_registry latest --screener momentum-stocks-290258
python -m data.dhanlive.screener_registry latest --screener momentum-stocks-290258 --raw
python -m data.dhanlive.screener_registry remove momentum-stocks-290258
```

Operational notes:

- `data.dhanlive.screener` uses the active rows in `dhan_screeners` when run without URL arguments.
- `data.dhanlive.screener` can still be pointed at explicit URLs directly.
- Snapshots are stored in `dhan_screener_snapshots` by `date + screener_slug`.
- `latest` is compact by default; use `--raw` to print the full stored JSON payload.

Recommended order for the identity-aware NSE pipeline:

```sh
python -m data.nseindia.bhavcopy_parser
python -m data.nseindia.security_history
python -m data.nseindia.security_dimension
python -m data.nseindia.adjusted_prices --only all
python -m features.price_daily
```

Top-level orchestration examples:

```sh
./all_downloads.sh
./all_daily_derivations.sh
./all_backfill.sh watchlist 5
./all_backfill.sh tracked 5
TRUNCATE_DERIVED=1 ./all_backfill.sh all 5
```

Notes:

- `all_daily_derivations.sh` loads symbols from [`config/watchlist_symbols.txt`](/home/rane/code/stockey/config/watchlist_symbols.txt), then falls back to [`config/tracked_symbols.txt`](/home/rane/code/stockey/config/tracked_symbols.txt)
- `all_backfill.sh` defaults to `watchlist 5`
- only use `TRUNCATE_DERIVED=1` when you intentionally want a full rebuild of derived price and feature tables
- `all_downloads.sh` now includes:
  - Dhan OHLCV sync
  - Dhan screener sync from the active screener registry

### SQL

```sh
python scripts/sql_query_runner.py "select * from fii_investments limit 5"
python scripts/sql_query_runner.py --read-only --params-json '{"symbol":"RELIANCE"}' "select * from historical_mcap where symbol = %(symbol)s order by date desc limit 3"
python scripts/sql_query_runner.py --file query.sql
cat query.sql | python scripts/sql_query_runner.py --read-only
```

### Redis

```sh
python scripts/redis_query_runner.py scan --pattern 'bhav:*'
python scripts/redis_query_runner.py get bhav:parsed --max-items 20
python scripts/redis_query_runner.py exists nsdl:fpi:downloaded
python scripts/redis_query_runner.py raw SMEMBERS bhav:parsed
```

### S3

```sh
python scripts/s3_query_runner.py list --prefix bhavcopy/ --delimiter /
python scripts/s3_query_runner.py head --key bhavcopy/example.zip
```

### Curated Agent Runner

```sh
python scripts/agent_tool_runner.py list
python scripts/agent_tool_runner.py list --category storage
python scripts/agent_tool_runner.py run sql_query -- --read-only "select * from macro_usa limit 5"
python scripts/agent_tool_runner.py run redis_get -- bhav:parsed --max-items 20
python scripts/agent_tool_runner.py run load_us_macro --allow-writes
```

## LLM-facing conventions

If you expose these through tools or MCP:

- Keep stdout machine-readable JSON.
- Treat non-zero exit code as failure.
- Prefer `--read-only` on SQL agents that should not mutate state.
- Pass SQL through `--file` or stdin for multi-line queries.
- Keep download/scrape agents separate from analysis agents.
- Prefer the registry in [`docs/tool_registry.json`](/home/rane/code/stockey/docs/tool_registry.json) instead of hard-coding shell commands in prompts.
- Prefer `security_id` over raw `symbol` when stitching history across renames.
- Prefer `company_master_id` over raw exchange tickers when joining company-level datasets across NSE and BSE.
