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
| `data/sharpelydata/sharpely_data.py` | `stmt_income`, `stmt_balancesheet`, `stmt_cashflow`, `shareholding_category`, `shareholding_top_holders`, `historical_mcap`, `sharpely_stock_meta`, `sharpely_stock_peers` | Fundamental data plus current stock metadata and peer snapshots |
| `data/dhanlive/scrip_master.py` | `master_dhan_instruments` | Versioned Dhan instrument master |
| `data/dhanlive/auth_cli.py` | none | Dhan token status, refresh, validate, and cache-clear helper |
| `data/dhanlive/ohlcv.py` | `dhan_ohlcv_daily`, `dhan_ohlcv_intraday` | Dhan OHLCV for `stock`, `index`, and `benchmark`; default sync is 5 years daily plus last 1 day of 1-minute bars |
| `advisory/intraday_features.py` | `advisory_intraday_features_daily` | On-demand advisory intraday feature builder; pulls missing intraday candles for the active screener universe and persists daily intraday pattern features |
| `data/screenerin/screener_parser.py` | `screenerin_screener_snapshots` | Stores parsed Screener.in screener snapshots by screener slug and date |
| `data/screenerin/screener_registry.py` | `screenerin_screeners` | Registry utility to add/list/remove Screener.in screeners and inspect latest stored snapshots |
| `data/screenerin/ad_hoc_query.py` | `screenerin_ad_hoc_query_runs`, `screenerin_ad_hoc_query_results` | Authenticated ad hoc Screener.in raw query runner; blocks for manual login if needed and stores parsed company rows plus queried metrics |
| `advisory/research_ledger.py` | `advisory_research_runs` | Research ledger for recording experiment configs, point-in-time context, validation protocol, and run outcomes |
| `advisory/training_universe.py` | `advisory_screener_constituents` | Sync broad ad hoc Screener.in training universes directly into normalized advisory screener rows for research-only event-model coverage |
| `advisory/event_meta_model.py` | `advisory_event_model_scores` | Train/score scaffold for XGBoost event meta-models using structured event tensors, anchor-day intraday response features, and future daily returns |
| `advisory/event_model_data_prep.py` | varies | One-shot prep flow for event-model training: normalizes missing screener constituents, backfills historical event evaluations, refreshes price history, and reports label coverage |
| `advisory/model_training_runner.py` | varies | Gated model-training orchestrator: runs prep, checks label coverage for the requested horizon, then trains and scores only when ready |
| `advisory/sync_state.py` | `advisory_sync_state` | Shared incremental state storage for continuous polling and dashboard refresh tasks |
| `advisory/continuous_watch.py` | `advisory_live_watch_alerts`, `advisory_sync_state` | Lightweight watch loop over active watchlist OHLCV, announcements, ET/news, and static dashboard writes |
| `advisory/event_router.py` | `advisory_live_router_actions`, `advisory_sync_state` | Symbol-level router that turns fresh live alerts and events into targeted advisory reevaluation |
| `advisory/live_dashboard.py` | files in `live_dashboard/` | Writes a simple static HTML/JSON live dashboard for `python -m http.server` |
| `advisory/live_notifier.py` | files in `live_dashboard/` | Subscribes to Redis pub-sub from the continuous-watch stack and writes a human-readable operator feed |
| `advisory/adversarial_review.py` | `advisory_event_reviews` | Deterministic reviewer over structured event tensors; can clear, penalize, force manual review, or veto event-driven allocations |
| `utils/ocr` | none | Provider-agnostic PDF OCR utility using Gemini 3 Flash preview and OpenAI GPT-5 nano |
| `utils/transcribe` | none | Audio transcription utility for remote mp3/wav/mp4 links using Gemini 3 Flash preview and OpenAI transcription APIs |
| `data/nseindia/bhavcopy_parser.py` | `nseindia_*` daily tables | Parses downloaded NSE archives for the legacy/reference NSE pipeline |
| `data/nseindia/adjusted_prices.py` | `nseindia_corporate_actions_normalized`, `nseindia_ohlcv_adjusted` | Builds split/bonus-adjusted OHLCV for the legacy/reference NSE pipeline |
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
| `scripts/cleanup_deprecated_tables.py` | none | Drops deprecated tables that are no longer used by the active advisory stack |

## Management scripts

These are the JSON-first scripts that are easiest to call from a human shell, an LLM tool wrapper, or an MCP server shim.

Regression checks:

```sh
python -m pytest tests/test_advisory_regression.py
python scripts/cleanup_deprecated_tables.py --dry-run
```

## General usage guidelines

- Prefer `python -m ...` from the repo root so relative config and `.env` loading behave consistently.
- Use the project venv when running ingestion jobs manually:

```sh
python -m ...
```

- Use the same interpreter for the curated runner:

```sh
python scripts/agent_tool_runner.py list
```

- Symbol-scoped loaders fall back in this order:
  1. `--symbols`
  2. `STOCKEY_SYMBOLS`
  3. [`config/tracked_symbols.txt`](../config/tracked_symbols.txt)
- Browser-driven loaders require Chrome remote debugging when they connect over CDP:

```sh
/opt/google/chrome/chrome --remote-debugging-port=9222 --user-data-dir=./chromesetup
```

- Dhan OHLCV auth falls back in this order:
  1. `DHAN_ACCESS_TOKEN`
  2. cached token at `.cache/dhan_access_token.json`
  3. API key consent flow using `DHAN_CLIENT_ID`, `DHAN_API_KEY`, `DHAN_API_SECRET`
- In the API key flow, the script opens the Dhan consent page in the browser and waits for you to paste the redirected URL back into the terminal. The access token is then cached until expiry.

Quick Dhan token maintenance:

```sh
python -m data.dhanlive.auth_cli status
python -m data.dhanlive.auth_cli validate
python -m data.dhanlive.auth_cli refresh --clear-cache-first
python -m data.dhanlive.auth_cli clear-cache
```

## Orchestration scripts

| Script | Purpose | Scope |
| --- | --- | --- |
| `all_advisory.sh` | Primary advisory orchestrator | Runs raw ingestion, news/theme routing, advisory, risk, portfolio, lifecycle, and execution in one flow |
| `all_model_training.sh` | Event-model training orchestrator | Runs prep, trains the event meta-model only when coverage is sufficient, then scores current events |
| `all_full_advisory.sh` | Single operator wrapper | Runs model training if ready, then the advisory pipeline, then prints the current portfolio summary |
| `all_continuous_watch.sh` | Continuous monitoring wrapper | Polls active watchlist OHLCV, announcements, and ET/news incrementally and rewrites the static live dashboard |
| `all_live_notifier.sh` | Live notification wrapper | Subscribes to the continuous-watch Redis bus and writes rolling operator feed files |
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
2. [`config/tracked_symbols.txt`](../config/tracked_symbols.txt)

Examples:

```sh
python -m data.sharpelydata.sharpely_data --symbols RELIANCE TCS --from-date 2024-01-01 --to-date 2024-12-31
python -m data.dhanlive.ohlcv --symbols SHAKTIPUMP
python -m data.dhanlive.ohlcv --symbols SHAKTIPUMP --only daily
python -m data.dhanlive.ohlcv --symbols NIFTY --asset-type benchmark --exchange NSE
python -m data.dhanlive.ohlcv --symbols BANKNIFTY --asset-type index --exchange NSE --only intraday
python -m data.screenerin.screener_registry add "https://www.screener.in/screens/1234567/my-production-screen/"
python -m data.screenerin.screener_registry list
python -m data.screenerin.screener_registry query --name "Deep Value ROCE" --query "Market capitalization > 500 AND Price to earning < 15 AND Return on capital employed > 22%"
python -m data.screenerin.screener_registry latest --screener my-production-screen
python -m data.screenerin.screener_parser
python -m advisory.master_pipeline --dry-run
python -m utils.ocr /tmp/sample.pdf --provider gemini --pages 1
python -m utils.ocr /tmp/sample.pdf --provider openai --pages 1,3-5
python -m utils.transcribe https://example.com/audio.mp3 --provider gemini
python -m utils.transcribe https://example.com/audio.mp4 --provider openai
python -m data.nseindia.corporate_actions --symbols RELIANCE,TCS
python -m data.nseindia.earnings_events
python -m data.nseindia.insider_deals
python -m data.nseindia.adjusted_prices --only all
python -m data.nseindia.security_history
python -m data.nseindia.security_dimension
python -m data.announcements.cli --ticker SHAKTIPUMP --exchange NSE --from-date 2026-03-01 --to-date 2026-03-22
python -m features.price_daily
```

Announcement pipeline model controls:

```sh
OCR_USING=gemini-3-flash-preview \
TRANSCRIBE_WITH=gemini-3-flash-preview \
SUMMARIZE_WITH=gpt-5-mini-2025-08-07 \
python -m data.announcements.cli --ticker SHAKTIPUMP --exchange NSE --from-date 2026-03-01 --to-date 2026-03-22
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

### Screener.in screeners

Registry utility: `data.screenerin.screener_registry`

Downloader: `data.screenerin.screener_parser`

Ad hoc query runner: `data.screenerin.ad_hoc_query`

Recommended workflow:

1. Use `data.screenerin.ad_hoc_query` for one-off research and idea generation.
2. Only register a screener when it becomes a recurring production input for a setup or theme.
3. Let `./all_advisory.sh` or `python -m advisory.master_pipeline` sync the active production registry and run the downstream advisory flow daily.
4. Inspect latest production snapshots with the registry utility.

Commands:

```sh
python -m data.screenerin.screener_registry add "https://www.screener.in/screens/1234567/my-production-screen/"
python -m data.screenerin.screener_registry list
python -m data.screenerin.screener_registry query --name "Deep Value ROCE" --query "Market capitalization > 500 AND Price to earning < 15 AND Return on capital employed > 22%"
python -m data.screenerin.ad_hoc_query --name "Deep Value ROCE" --query "Market capitalization > 500 AND Price to earning < 15 AND Return on capital employed > 22%"
python -m data.screenerin.screener_parser
python -m data.screenerin.screener_registry latest
python -m data.screenerin.screener_registry latest --screener my-production-screen
python -m data.screenerin.screener_registry latest --screener my-production-screen --raw
python -m data.screenerin.screener_registry remove my-production-screen
```

Operational notes:

- `./all_downloads.sh` and `./all_advisory.sh` no longer seed default screeners automatically.
- the recurring downloader only syncs screeners that are already registered as active production inputs.
- use `data.screenerin.ad_hoc_query` first; register only the queries that graduate into recurring setups or themes.
- `data.screenerin.screener_parser` uses the active rows in `screenerin_screeners` when run without URL arguments.
- `data.screenerin.screener_parser` can still be pointed at explicit Screener.in URLs directly.
- Snapshots are stored in `screenerin_screener_snapshots` by `date + screener_slug`.
- `latest` is compact by default; use `--raw` to print the full stored JSON payload.
- `data.screenerin.ad_hoc_query` assumes Chrome is already running with remote debugging enabled.
- `data.screenerin.ad_hoc_query` checks `https://www.screener.in/dash/` first and blocks in the terminal if manual login is required.
- ad hoc runs are stored in `screenerin_ad_hoc_query_runs`.
- normalized company rows for ad hoc runs are stored in `screenerin_ad_hoc_query_results`.
- parsed ad hoc output includes `company_name`, `ticker`, `company_url`, `rank`, and `metrics`.
- theme-to-screener discovery now uses only `config/investment_themes.yaml`; the older fallback theme config was removed.

## Research Priorities

Current research focus is not multi-agent orchestration. It is:

1. point-in-time discipline and validation
2. structured event extraction from announcements and news
3. tabular prediction and ranking
4. abstention and turnover control
5. strict separation of prediction from policy and execution

Use ad hoc Screener.in queries and normal notebooks or scripts for exploration. Only promote a query into the registered production path after it survives validation.

## Event Meta-Model Training

Recommended one-shot prep flow:

```sh
python -m advisory.event_model_data_prep --format text
```

This command is the operator shortcut for:

1. normalizing any missing `advisory_screener_constituents` dates from stored Screener snapshots
2. rerunning historical advisory event extraction/evaluation over those actual screener dates
3. refreshing `dhan_ohlcv_daily` for the evaluated event symbols
4. summarizing label coverage by horizon before training
5. optionally syncing broad ad hoc training universes for the current date into a research-only setup

Useful variants:

```sh
python -m advisory.event_model_data_prep --from-date 2026-03-01 --to-date 2026-04-01 --format text
python -m advisory.event_model_data_prep --skip-price-refresh --dry-run --format text
python -m advisory.training_universe --dry-run
```

Training universe notes:

- broad event-model training universes now come from ad hoc Screener.in raw queries, not from manually registered recurring screeners
- those queries are stored in `config/event_model_training_universes.yaml`
- the resulting screener rows feed the research-only setup `EVENT_MODEL_TRAINING_V1`
- normal advisory runs ignore `EVENT_MODEL_TRAINING_V1` unless it is explicitly selected

Recommended model-training flow:

```sh
./all_model_training.sh
./all_model_training.sh --prep-only
./all_model_training.sh --horizon-days 1 --to-date 2026-04-07
./all_full_advisory.sh
```

Behavior:

- runs `advisory.event_model_data_prep`
- checks whether the requested horizon is `train_ready`
- skips training cleanly if coverage is still insufficient
- trains `advisory.event_meta_model` only when the readiness gate passes
- scores current events after training unless `--skip-score` is used

Operational constraints:

- historical prep dates are treated as read-mostly
- old dates do not trigger on-the-fly daily/fundamental snapshot repair inside the rule engine
- old dates do not trigger on-the-fly intraday prefetch inside the rule engine
- current-date prep can still sync the broad training universes and use already stored recent data

Single-command operator flow:

```sh
./all_full_advisory.sh
./all_full_advisory.sh --date 2026-04-07
./all_full_advisory.sh --skip-model-training
./all_full_advisory.sh --skip-downloads --portfolio-planned
```

Behavior:

- runs `advisory.model_training_runner`
- runs `advisory.master_pipeline`
- prints `advisory.portfolio_engine --format text` at the end

Continuous-watch flow:

```sh
./all_continuous_watch.sh --loop
./all_live_notifier.sh
./all_continuous_watch.sh --loop --sleep-seconds 300
python -m advisory.live_dashboard --output-dir live_dashboard
python -m http.server --directory live_dashboard 8000
```

Behavior:

- keeps incremental source state in `advisory_sync_state`
- polls 1-minute intraday OHLCV for active advisory watchlist symbols and open positions
- polls announcements and ET/news on their own intervals
- routes new alerts and events into symbol-level advisory reevaluation
- writes live price alerts like `ENTRY_ZONE_HIT` and `INVALIDATION_HIT`
- rewrites `live_dashboard/index.html` and `live_dashboard/dashboard.json`
- publishes cycle summaries and alert payloads over Redis pub-sub

Notifier behavior:

- subscribes to `stockey:continuous_watch:*`
- converts bus messages into short human-readable operator lines
- writes:
  - `live_dashboard/operator_feed.json`
  - `live_dashboard/operator_feed.jsonl`
  - `live_dashboard/operator_feed.txt`
- the dashboard page also reads `operator_feed.json` and shows the latest feed items

Routing constraints:

- price-alert symbols are prioritized ahead of event-only symbols
- `POSITION_INVALIDATION_HIT`, `STOP_HIT`, and `INVALIDATION_HIT` rank above softer breakout-follow alerts
- lower watchlist rank values are preferred when multiple names compete for the same cycle
- there is no default hard cap on reevaluation volume; explicit caps are an operator override

Before training `advisory.event_meta_model`, make sure:

1. `advisory_event_evaluations` spans enough historical dates.
2. `dhan_ohlcv_daily` is fresh enough for the event symbols to cover the target horizon.
3. the chosen horizon has enough labeled rows to clear the minimum training floor.

Useful checks:

```sh
python scripts/sql_query_runner.py --read-only "select date(published_on) as published_date, count(*) as eval_count from advisory_event_evaluations group by 1 order by 1"
python scripts/sql_query_runner.py --read-only "select max(date) as max_price_date from dhan_ohlcv_daily"
python -m advisory.event_meta_model train --horizon-days 1
python -m advisory.event_meta_model score --dry-run
```

Research ledger commands:

```sh
python -m advisory.research_ledger --limit 20
python -m advisory.pipeline --dry-run --log-research-ledger --ledger-label "baseline-v1" --ledger-objective "daily ranking sanity check"
python -m advisory.master_pipeline --dry-run --log-research-ledger --ledger-label "full-run-v1" --ledger-validation-protocol '{"split":"purged_walk_forward"}'
```

Abstain behavior:

- low-edge setups can now be marked `ABSTAIN` instead of being forced into `WATCH_*` or hidden inside generic rejects
- risk can now emit `allocation_status=abstained`, which makes the do-nothing class measurable

## Advisory docs

The current roadmap for the investment advisory system lives in [`todo.md`](../todo.md). It tracks current bottlenecks and the next implementation priorities rather than historical build phases.

The current architecture summary lives in [`docs/implementation.md`](implementation.md).

The practical operator guide lives in [`docs/advisory_manual.md`](advisory_manual.md). Use it for:

- daily runs
- adding screeners
- changing setup rules
- understanding which modules and tables to inspect
- debugging rule, watch, event, and execution outputs

For shorter example-driven edits, use [`docs/advisory_change_cookbook.md`](advisory_change_cookbook.md).

## OCR usage

Module: `utils.ocr`

Purpose:

- OCR one PDF using LLM vision models
- return text by page number
- support either provider independently or both together

Providers:

- OpenAI: `gpt-5-nano`
- Gemini: `gemini-3-flash-preview`

Auth:

- reads `OPENAI_API_KEY` from `.env`
- reads `GEMINI_KEY` from `.env`

Page selection:

- `all`
- `1`
- `1,3,5`
- `1-3,7`

Commands:

```sh
python -m utils.ocr /path/to/file.pdf
python -m utils.ocr /path/to/file.pdf --provider gemini --pages 1
python -m utils.ocr /path/to/file.pdf --provider openai --pages 1,3-5
python -m utils.ocr /path/to/file.pdf --provider both --pages all
```

Output shape:

- JSON object keyed by provider
- each provider contains page-number-to-text mappings

## Audio transcription usage

Module: `utils.transcribe`

Purpose:

- download an audio URL
- transcribe the spoken content
- support OpenAI, Gemini, or both

Accepted inputs:

- `mp3`
- `wav`
- `mp4`
- other formats supported by the provider APIs, as long as the URL is downloadable

Providers:

- OpenAI transcription model: `gpt-4o-mini-transcribe`
- Gemini audio understanding model: `gemini-3-flash-preview`

Note:

- OpenAI GPT-5 nano does not currently support audio input, so the OpenAI side uses the official transcription model instead.

Auth:

- reads `OPENAI_API_KEY` from `.env`
- reads `GEMINI_KEY` from `.env`

Commands:

```sh
python -m utils.transcribe https://example.com/audio.mp3
python -m utils.transcribe https://example.com/audio.wav --provider gemini
python -m utils.transcribe https://example.com/audio.mp4 --provider openai
python -m utils.transcribe https://example.com/audio.mp3 --provider both
```

Output shape:

- JSON object keyed by provider
- each provider contains the transcribed text string

Recommended order for the legacy/reference identity-aware NSE pipeline:

```sh
python -m data.nseindia.bhavcopy_parser
python -m data.nseindia.security_history
python -m data.nseindia.security_dimension
python -m data.nseindia.adjusted_prices --only all
python -m features.price_daily
```

This NSE bhavcopy plus adjusted-price path is now a reference and reconciliation pipeline. The advisory runtime path uses Dhan OHLCV directly and does not depend on `nseindia_ohlcv_adjusted`.

Top-level orchestration examples:

```sh
./all_advisory.sh
python -m advisory.master_pipeline --dry-run
./all_daily_derivations.sh
./all_backfill.sh watchlist 5
./all_backfill.sh tracked 5
TRUNCATE_DERIVED=1 ./all_backfill.sh all 5
```

Notes:

- `all_daily_derivations.sh` loads symbols from [`config/watchlist_symbols.txt`](../config/watchlist_symbols.txt), then falls back to [`config/tracked_symbols.txt`](../config/tracked_symbols.txt)
- `all_backfill.sh` defaults to `watchlist 5`
- only use `TRUNCATE_DERIVED=1` when you intentionally want a full rebuild of derived price and feature tables
- `all_advisory.sh` now includes the full raw ingestion flow plus the advisory master pipeline:
  - market data downloads and parsers
  - Dhan OHLCV sync
  - Screener.in registry sync and advisory screener normalization
  - advisory theme routing
  - advisory rule, watch, news, event, risk, portfolio, lifecycle, and execution stages

- `all_downloads.sh` remains the lower-level raw ingestion component used by the master pipeline.

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
python scripts/agent_tool_runner.py run load_economic_times_rss --allow-writes -- --dry-run
python scripts/agent_tool_runner.py run build_advisory_screener_constituents --allow-writes -- --dry-run
python scripts/agent_tool_runner.py run build_advisory_macro_daily --allow-writes -- --dry-run
python scripts/agent_tool_runner.py run build_advisory_fundamentals_daily --allow-writes -- --dry-run
python scripts/agent_tool_runner.py run build_advisory_market_regime --allow-writes -- --dry-run
python scripts/agent_tool_runner.py run sync_advisory_peers --allow-writes -- --symbols HDFCBANK
python scripts/agent_tool_runner.py run build_advisory_technical_daily --allow-writes -- --dry-run
python scripts/agent_tool_runner.py run run_advisory_rule_engine --allow-writes -- --dry-run
python scripts/agent_tool_runner.py run build_advisory_watchlist --allow-writes -- --dry-run
python scripts/agent_tool_runner.py run run_advisory_announcement_watch --allow-writes -- --dry-run
python scripts/agent_tool_runner.py run run_advisory_news_watch --allow-writes -- --refresh-feeds --dry-run
python scripts/agent_tool_runner.py run trace_advisory_symbol -- HDFCBANK --format text
python scripts/agent_tool_runner.py run trace_advisory_setup -- LARGECAP_BREAKOUT_V1 --format text
python scripts/agent_tool_runner.py run show_advisory_dashboard -- --format text
python scripts/agent_tool_runner.py run run_advisory_llm_event_evaluator --allow-writes -- --dry-run
python scripts/agent_tool_runner.py run build_advisory_allocations --allow-writes -- --dry-run
python scripts/agent_tool_runner.py run build_advisory_portfolio_orders --allow-writes -- --dry-run
python scripts/agent_tool_runner.py run build_advisory_position_lifecycle --allow-writes -- --dry-run
python scripts/agent_tool_runner.py run build_advisory_execution_orders --allow-writes -- --dry-run
python scripts/agent_tool_runner.py run run_master_advisory_pipeline --allow-writes -- --dry-run
python scripts/agent_tool_runner.py run run_advisory_pipeline --allow-writes -- --dry-run --stop-at portfolio
```

### Advisory modules

```sh
python -m advisory.screener_parser --dry-run
python -m advisory.screener_parser
python -m advisory.macro_snapshot --dry-run
python -m advisory.macro_snapshot
python -m advisory.fundamental_snapshot --dry-run
python -m advisory.fundamental_snapshot
python -m advisory.regime_engine --dry-run
python -m advisory.regime_engine
python -m advisory.peer_sync --symbols HDFCBANK
python -m advisory.technical_features --dry-run
python -m advisory.technical_features
python -m advisory.rule_engine --dry-run
python -m advisory.rule_engine
python -m advisory.watchlist_builder --dry-run
python -m advisory.watchlist_builder
python -m data.economictimes.rss --dry-run
python -m data.economictimes.rss
python -m advisory.announcement_watch --dry-run
python -m advisory.announcement_watch
python -m advisory.news_watch --dry-run
python -m advisory.news_watch --refresh-feeds
python -m advisory.symbol_trace HDFCBANK --format text
python -m advisory.setup_trace LARGECAP_BREAKOUT_V1 --format text
python -m advisory.dashboard --format text
python -m advisory.llm_event_evaluator --dry-run
python -m advisory.llm_event_evaluator
python -m advisory.risk_engine --dry-run
python -m advisory.risk_engine
python -m advisory.portfolio_engine --dry-run
python -m advisory.portfolio_engine
python -m advisory.position_lifecycle --dry-run
python -m advisory.position_lifecycle
python -m advisory.execution_engine --dry-run
python -m advisory.execution_engine --reconcile-only --dry-run
python -m advisory.pipeline --dry-run --stop-at portfolio
python -m advisory.pipeline --include-watch --include-lifecycle --dry-run
```

`advisory.fundamental_snapshot` and `advisory.technical_features` refresh peer snapshots automatically on normal write runs. Use `--skip-peer-sync` if you want a pure build against already-synced data.

For the advisory stack, `dhan_ohlcv_daily` is the canonical OHLCV source. The NSE bhavcopy plus adjusted-price pipeline remains available for reference and reconciliation, but advisory technicals and rule evaluation no longer depend on `nseindia_ohlcv_adjusted`.

`data.economictimes.rss` stores raw ET RSS items in `economictimes_rss_items`.

`advisory.news_watch` matches ET RSS items onto the active watchlist and writes `advisory_news_events`.

`advisory.symbol_trace` reads the advisory state tables and produces a single-symbol trace across screener, rule, watch, event, allocation, portfolio, lifecycle, and execution stages.

`advisory.setup_trace` reads the advisory state tables and produces a setup-level trace across screener universe, candidates, rejections, watch, event, allocation, portfolio, lifecycle, and execution stages.

`advisory.dashboard` shows all configured setups in one compact table or JSON payload using the same setup-trace logic underneath.

`advisory.llm_event_evaluator` reads both `advisory_watch_events` and `advisory_news_events`, joins `announcement_pipeline_documents` when official filings exist, and adds point-in-time regime, technical, and fundamentals context before writing `advisory_event_evaluations` and `advisory_event_risks`.

`advisory.risk_engine` reads `advisory_event_evaluations`, joins the latest point-in-time technical and fundamental context, and writes `advisory_allocations` with risk bucket, conviction bucket, suggested INR allocation, and invalidation guidance.

`advisory.portfolio_engine` reads `advisory_allocations`, ranks approved allocations by conviction/risk/liquidity-aware priority, applies portfolio-level capital and setup caps, and writes `advisory_portfolio_orders`.

`advisory.position_lifecycle` reads approved `advisory_portfolio_orders`, marks paper entry/current prices from `dhan_ohlcv_daily`, and writes `advisory_position_lifecycle` plus `advisory_rebalance_actions` with hold/trim/exit/review suggestions.

`advisory.execution_engine` reads approved `advisory_portfolio_orders`, builds broker handoff orders in `advisory_execution_orders`, and can reconcile order/trade state from Dhan into `advisory_execution_orders` plus `advisory_execution_fills`. Use `--live` only when you explicitly want to place live orders through Dhan. Live Dhan order placement requires the API static IP to be whitelisted.

`advisory.master_pipeline` is the single top-level orchestrator over the full repo flow. Use `./all_advisory.sh` for the shell entry point, or run `python -m advisory.master_pipeline` directly. Lower-level modules such as `all_downloads.sh` and `advisory.pipeline` remain available for component runs and targeted debugging.

The advisory flow now includes an `intraday` stage between daily technicals and rule evaluation. That stage:

- resolves the current symbol universe from the active screener snapshot
- backfills missing Dhan intraday bars on demand
- can build from Dhan intervals `1`, `5`, `15`, `25`, and `60`
- persists derived daily intraday pattern features into `advisory_intraday_features_daily`
- makes those fields available to the rule engine for optional setup scoring

Example:
```sh
python -m advisory.intraday_features --date 2026-04-01 --intervals 1 5 15
```

## LLM-facing conventions

If you expose these through tools or MCP:

- Keep stdout machine-readable JSON.
- Treat non-zero exit code as failure.
- Prefer `--read-only` on SQL agents that should not mutate state.
- Pass SQL through `--file` or stdin for multi-line queries.
- Keep download/scrape agents separate from analysis agents.
- Prefer the registry in [`docs/tool_registry.json`](tool_registry.json) instead of hard-coding shell commands in prompts.
- Prefer `security_id` over raw `symbol` when stitching history across renames.
- Prefer `company_master_id` over raw exchange tickers when joining company-level datasets across NSE and BSE.
