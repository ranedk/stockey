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
| `data/dhanlive/ohlcv.py` | `dhan_ohlcv_daily`, `dhan_ohlcv_intraday` | Dhan OHLCV for `stock`, `index`, and `benchmark`; default sync resumes from the latest stored candle with overlap, and backfills only when no local data exists |
| `data/dhanlive/ohlcv_pull.py` | none | Quick operator OHLCV pull utility; defaults to NSE equity, 5-minute candles, and the last 60 minutes |
| `advisory/intraday_features.py` | `advisory_intraday_features_daily` | On-demand advisory intraday feature builder; pulls missing intraday candles for the active screener universe and persists daily intraday pattern features |
| `data/screenerin/auth.py` | Browser session | Ensures Screener.in login through the running Chrome CDP session using `SCREENER_IN_LOGIN` and `SCREENER_IN_PASSWORD` |
| `data/screenerin/screener_parser.py` | `screenerin_screener_snapshots` | Stores parsed Screener.in screener snapshots by screener slug and date |
| `data/screenerin/screener_registry.py` | `screenerin_screeners` | Registry utility to add/list/remove Screener.in screeners and inspect latest stored snapshots |
| `data/screenerin/ad_hoc_query.py` | `screenerin_ad_hoc_query_runs`, `screenerin_ad_hoc_query_results` | Authenticated ad hoc Screener.in raw query runner; auto-logins through CDP when needed and stores parsed company rows plus queried metrics |
| `advisory/research_ledger.py` | `advisory_research_runs` | Research ledger for recording experiment configs, point-in-time context, validation protocol, and run outcomes |
| `advisory/training_universe.py` | `advisory_screener_constituents` | Sync broad ad hoc Screener.in training universes directly into normalized advisory screener rows for research-only event-model coverage |
| `advisory/event_meta_model.py` | `advisory_event_model_scores` | Train/score scaffold for XGBoost event meta-models using structured event tensors, anchor-day intraday response features, and future daily returns |
| `advisory/ts_forecast_features.py` | `advisory_ts_forecasts_daily` | Experimental OHLCV time-series forecast features; starts with `naive_momentum_v1` and is designed to host TimesFM / Chronos / Moirai adapters later |
| `advisory/ts_forecast_evaluator.py` | `advisory_ts_forecast_evaluations`, `advisory_ts_forecast_eval_summary` | Evaluates matured TS forecast rows against future Dhan OHLCV returns after costs |
| `advisory/ts_forecast_workflow.py` | `advisory_ts_forecasts_daily`, `advisory_ts_forecast_watchlist` | Optional Screener.in -> Dhan OHLCV refresh -> TimesFM forecast -> experimental TS watchlist workflow |
| `advisory/exchange_events.py` | `advisory_exchange_events` | Normalizes NSE block/bulk/short-selling/insider/corporate-action/earnings rows into point-in-time exchange events |
| `advisory/exchange_features.py` | `advisory_exchange_features_daily` | Builds daily symbol-level exchange-event features for LLM context, event-model features, review, and risk sizing |
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
| `scripts/ingestion_state_runner.py` | `ingestion_file_state` | Inspect failed/processed ingestion file state and clear specific failed keys before retry |

## Management scripts

These are the JSON-first scripts that are easiest to call from a human shell, an LLM tool wrapper, or an MCP server shim.

Regression checks:

```sh
python -m pytest tests/test_advisory_regression.py
python scripts/cleanup_deprecated_tables.py --dry-run
```

Ingestion state inspection:

```sh
python scripts/ingestion_state_runner.py list --status failed --limit 50
python scripts/ingestion_state_runner.py list --source bhavcopy --status failed
python scripts/ingestion_state_runner.py clear --source bhavcopy --key bhavcopy/bhavcopy_2015-01-16.zip
```

## General usage guidelines

- Prefer `python -m ...` from the repo root so relative config and `.env` loading behave consistently.
- DB reads, selected metadata calls, DB connects, and DB upserts retry transient statement-timeout and connection errors by default. Tune with `SQL_TO_DF_RETRIES`, `SQL_TO_DF_RETRY_SLEEP_SECONDS`, `SQL_TO_DF_STATEMENT_TIMEOUT_MS`, `SQL_TO_DF_CHUNK_SIZE`, `DB_OPERATION_ATTEMPTS`, and `DB_POOL_RECYCLE_SECONDS` if remote PostgreSQL is unstable.
- Ingestion Redis cursor/cache operations retry by default and fail soft after retries. Tune with `REDIS_OPERATION_ATTEMPTS`, `REDIS_RETRY_SLEEP_SECONDS`, `REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS`, `REDIS_SOCKET_TIMEOUT_SECONDS`, and `REDIS_FAIL_SOFT`.
- DB upserts use local temporary files for the `COPY` payload, which avoids holding very large CSV buffers fully in memory.
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
Ubunnt:
/opt/google/chrome/chrome --remote-debugging-port=9222 --user-data-dir=./chromesetup

OSX:
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome  --remote-debugging-port=9222 --user-data-dir=./chromesetup
```

- NSE direct HTTP calls retry transient timeouts, connection errors, `429`, and `5xx` responses. `NSE_HTTP_MAX_ATTEMPTS=0` means keep retrying until the site recovers. Use `NSE_HTTP_RETRY_SLEEP_SECONDS` and `NSE_HTTP_RETRY_MAX_SLEEP_SECONDS` to control backoff. Retries are printed to stderr as `[announcement_pipeline.nse] ...` so cron logs show when the process is waiting; before each retry the announcement client clears stale NSE cookies, rebuilds headers, and tries to bootstrap fresh NSE cookies.

- Dhan OHLCV auth falls back in this order:
  1. `DHAN_ACCESS_TOKEN`
  2. cached token at `.cache/dhan_access_token.json`
  3. API key consent flow using `DHAN_CLIENT_ID`, `DHAN_API_KEY`, `DHAN_API_SECRET`
- In the API key flow, the default path opens the Dhan consent page in the browser and waits for you to paste the redirected URL back into the terminal. The access token is then cached until expiry.
- Optional Dhan browser automation uses the running Chrome CDP session plus `DHAN_LOGIN_MOBILE`, `DHAN_TOTP_SECRET`, and `DHAN_LOGIN_PIN`. It generates the TOTP with `pyotp`, fills the consent login, extracts `tokenId`, and then uses the same official consent-token exchange.

Quick Dhan token maintenance:

```sh
python -m data.dhanlive.auth_cli status
python -m data.dhanlive.auth_cli validate
python -m data.dhanlive.auth_cli refresh --clear-cache-first
python -m data.dhanlive.auth_cli refresh --clear-cache-first --auto-login
python -m data.dhanlive.web_login --consent-url "https://auth.dhan.co/login/consentApp-login?consentAppId=..."
python -m data.dhanlive.auth_cli clear-cache
```

## Orchestration scripts

| Script | Purpose | Scope |
| --- | --- | --- |
| `all_downloaders.sh` | Download-only ingestion | Runs the raw downloader modules only |
| `all_parsers.sh` | Parse-only ingestion | Runs the parser modules only |
| `complete_data.sh` | Combined ingestion | Runs all downloaders and parsers in order |
| `all_ml.sh` | Event-model training orchestrator | Runs prep, trains the event meta-model only when coverage is sufficient, then scores current events |
| `all_advisory.sh` | Advisory orchestrator | Runs the advisory pipeline and portfolio generation without raw downloads |
| `all_watchers.sh` | Continuous monitoring wrapper | Polls active watchlist OHLCV, announcements, and ET/news incrementally |

Recommended scheduler file:

- `config/stockey.crontab.template`
- `config/stockey.generated.crontab`

It schedules:

- `complete_data.sh` once daily on weekdays
- `all_ml.sh` once daily after 3am on weekdays
- `all_watchers.sh` every `10` minutes during market hours
- `all_advisory.sh` once daily after 7pm on weekdays

Bootstrap note:

- `python builder.py` creates `logs/cron`, installs `go-crond` locally as `./go-crond` unless `GO_CROND_INSTALL_DIR` overrides the target, and installs `torch` plus the current Google Research TimesFM package from GitHub for the `timesfm_2p5_200m` forecast adapter.
- Use `python builder.py --skip-timesfm-install` or `STOCKEY_SKIP_TIMESFM_INSTALL=true python builder.py` for lightweight setup runs without TimesFM.
- Override the TimesFM source with `STOCKEY_TIMESFM_PACKAGE` if a future release needs a pinned URL or version.

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
python -m advisory.ts_forecast_features --dry-run --symbols RELIANCE TCS
python -m advisory.ts_forecast_evaluator --dry-run --from-date 2026-04-01 --to-date 2026-04-30
python -m advisory.ts_forecast_workflow --dry-run --symbols RELIANCE TCS --model-name naive_momentum_v1
python -m features.price_daily
```

## Experimental time-series forecasts

Module: `advisory.ts_forecast_features`

This is a research-only forecast feature path over `dhan_ohlcv_daily`. It writes to `advisory_ts_forecasts_daily` and currently uses a dependency-free `naive_momentum_v1` baseline. The table schema is intentionally compatible with later TimesFM, Chronos, or Moirai adapters.

The output is not a live action source. It should be evaluated through a paper portfolio and research-ledger comparison before being allowed to affect `advisory_action_recommendations` or Dhan execution.

Useful commands:

```sh
python -m advisory.ts_forecast_features --dry-run --symbols RELIANCE TCS
python -m advisory.ts_forecast_features --symbols RELIANCE TCS --horizons 5 10 20
python -m advisory.ts_forecast_features --date 2026-04-30 --horizons 5 20
python -m advisory.ts_forecast_features --refresh-ohlcv --symbols RELIANCE TCS --model-name timesfm_2p5_200m --horizons 5 10 20
python -m advisory.ts_forecast_evaluator --dry-run --from-date 2026-04-01 --to-date 2026-04-30
python -m advisory.ts_forecast_evaluator --from-date 2026-04-01 --to-date 2026-04-30 --cost-bps 25
python -m advisory.ts_forecast_workflow --symbols RELIANCE TCS --model-name timesfm_2p5_200m
python -m advisory.ts_forecast_workflow --model-name timesfm_2p5_200m
```

Current outputs include expected return, forecast price, upside/downside quantiles, probability of positive return, signal quality, and an `EXPERIMENTAL_*` action hint.

Evaluation writes row-level realized-return checks to `advisory_ts_forecast_evaluations` and grouped model/horizon/action-hint metrics to `advisory_ts_forecast_eval_summary`.

The workflow command uses `config/ts_forecast_screeners.yaml` when no symbols or query are supplied. It runs an ad hoc Screener.in query to find liquid/technical candidates, refreshes their Dhan daily OHLCV, runs the selected forecast model, and writes positive experimental names to `advisory_ts_forecast_watchlist`. For TimesFM, install the optional package first; otherwise use `--model-name naive_momentum_v1` for a dependency-free baseline.

The default cron run uses `--max-symbols "${TS_FORECAST_MAX_SYMBOLS:-80}"` and runs a few times per weekday, not every watcher tick, because the current TS workflow consumes daily OHLCV. If Screener.in returns no parseable results or is temporarily unavailable, the workflow logs the failure and falls back to a capped Dhan/tracked symbol universe so the research job still emits an auditable result.

The live dashboard keeps raw forecast rows for evaluation but displays one TS card per symbol. The card separates Swing and Position windows and shows recent update history so a new TS recommendation is interpreted as an update to the prior symbol history, not a duplicate position.

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

- daily sync starts from the latest stored daily candle minus `DHAN_DAILY_REFRESH_OVERLAP_DAYS`
- intraday sync starts from the latest stored intraday candle minus `DHAN_INTRADAY_REFRESH_OVERLAP_MINUTES`
- if no local data exists, daily sync backfills 5 years and intraday sync backfills the last 1 day
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

### Quick OHLCV utility

Module: `data.dhanlive.ohlcv_pull`

Default behavior:

- defaults to `exchange=NSE`
- defaults to `asset_type=stock`
- defaults to `mode=intraday`
- defaults to `interval_minutes=5`
- defaults to the last `60` minutes
- prints a readable text table by default

Common usage:

```sh
python -m data.dhanlive.ohlcv_pull RELIANCE
python -m data.dhanlive.ohlcv_pull HDFCBANK --interval-minutes 1
python -m data.dhanlive.ohlcv_pull RELIANCE --last-minutes 180
python -m data.dhanlive.ohlcv_pull RELIANCE --mode daily --last-days 90
python -m data.dhanlive.ohlcv_pull NIFTY --asset-type benchmark
python -m data.dhanlive.ohlcv_pull RELIANCE --source db --format json
```

Notes:

- use `--source api` to hit Dhan directly
- use `--source db` to inspect what is already stored locally
- use `--help` for the full operator manual and sample values

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

Login helper: `data.screenerin.auth`

Recommended workflow:

1. Set `CDP_ENDPOINT`, `SCREENER_IN_LOGIN`, and `SCREENER_IN_PASSWORD`.
2. Use `data.screenerin.auth` to check or establish login when debugging.
3. Use `data.screenerin.ad_hoc_query` for one-off research and idea generation.
4. Only register a screener when it becomes a recurring production input for a setup or theme.
5. Let `./all_advisory.sh` or `python -m advisory.master_pipeline` sync the active production registry and run the downstream advisory flow daily.
6. Inspect latest production snapshots with the registry utility.

Commands:

```sh
python -m data.screenerin.auth --check
python -m data.screenerin.auth
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

- `./complete_data.sh` and `./all_advisory.sh` no longer seed default screeners automatically.
- the recurring downloader only syncs screeners that are already registered as active production inputs.
- use `data.screenerin.ad_hoc_query` first; register only the queries that graduate into recurring setups or themes.
- `data.screenerin.screener_parser` uses the active rows in `screenerin_screeners` when run without URL arguments.
- `data.screenerin.screener_parser` can still be pointed at explicit Screener.in URLs directly.
- Snapshots are stored in `screenerin_screener_snapshots` by `date + screener_slug`.
- `latest` is compact by default; use `--raw` to print the full stored JSON payload.
- `data.screenerin.ad_hoc_query` assumes Chrome is already running with remote debugging enabled.
- Screener.in flows check `https://www.screener.in/login/`; if already authenticated, it redirects to `/dash/`.
- If login is required, `data.screenerin.auth` fills the username/password form from env and verifies that `/dash/` is reached.
- The Screener.in password is never printed.
- `data.screenerin.ad_hoc_query` and `data.screenerin.screener_parser` ensure logged-in mode before fetching Screener.in data.
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
./all_ml.sh
./all_ml.sh --prep-only
./all_ml.sh --horizon-days 1 --to-date 2026-04-07
./all_advisory.sh
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

Recommended operator flow:

```sh
./complete_data.sh
./all_ml.sh
./all_advisory.sh
./all_advisory.sh --fast
./all_advisory.sh --date 2026-04-07
```

Behavior:

- runs `advisory.master_pipeline --skip-downloads`
- expects data and model artifacts to have been refreshed separately through `./complete_data.sh` and `./all_ml.sh`
- writes the advisory outputs consumed by the portfolio view and live dashboard
- `--fast` skips watch/news refresh, peer sync, and on-demand intraday repair; use it for quick portfolio/lifecycle/action refreshes when data is already current

Continuous-watch flow:

```sh
./all_watchers.sh --loop
./all_watchers.sh --loop --sleep-seconds 300
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
- weekday cron also refreshes the static dashboard every 15 minutes outside the watcher loop

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
./all_downloaders.sh
./all_parsers.sh
./complete_data.sh
```

Notes:

- `all_watchers.sh` loads symbols from [`config/watchlist_symbols.txt`](../config/watchlist_symbols.txt), then falls back to [`config/tracked_symbols.txt`](../config/tracked_symbols.txt) where needed
- `all_advisory.sh` is advisory-only and skips raw downloads by default
- `all_advisory.sh --fast` is the low-latency advisory mode; it avoids slow repair/watch stages and is safer than blindly parallelizing browser-connected work
- `complete_data.sh` is the lower-level raw ingestion component used before advisory runs

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
python scripts/agent_tool_runner.py run build_advisory_macro_features_daily --allow-writes -- --dry-run
python scripts/agent_tool_runner.py run build_advisory_exchange_events --allow-writes -- --dry-run
python scripts/agent_tool_runner.py run build_advisory_exchange_features_daily --allow-writes -- --dry-run
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
python -m advisory.macro_features --dry-run
python -m advisory.macro_features
python -m advisory.exchange_events --dry-run
python -m advisory.exchange_features --dry-run
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
python -m advisory.execution_engine --dry-run --use-broker-account
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

`advisory.execution_engine` reads consolidated `advisory_action_recommendations`, builds broker handoff orders in `advisory_execution_orders`, and can reconcile order/trade state from Dhan into `advisory_execution_orders` plus `advisory_execution_fills`. Staged order sizing prefers fresh `dhan_ohlcv_intraday` prices and falls back to `dhan_ohlcv_daily` close when intraday is unavailable.

Use `--use-broker-account` on a dry run when you want staged quantities capped by live Dhan cash and holdings without submitting orders. `--live` enables the same broker-account sizing automatically before safety checks and submission.

Live Dhan submission is fail-closed. `--live` is not enough by itself; set `STOCKEY_LIVE_TRADING_ENABLED=true` only when you intentionally want broker submission. Keep these caps configured before live use:

- `STOCKEY_EXECUTION_MAX_LIVE_ORDERS_PER_RUN`, default `5`
- `STOCKEY_EXECUTION_MAX_ORDER_VALUE_INR`, default `50000`
- `STOCKEY_EXECUTION_REQUIRE_FRESH_INTRADAY_PRICE`, default `true`
- `STOCKEY_EXECUTION_MAX_INTRADAY_PRICE_AGE_MINUTES`, default `30`

Live Dhan order placement also requires the API static IP to be whitelisted.

`advisory.master_pipeline` is the single top-level advisory orchestrator. Use `./all_advisory.sh` for the shell entry point, or run `python -m advisory.master_pipeline --skip-downloads` directly. Lower-level modules such as `complete_data.sh` and `advisory.pipeline` remain available for component runs and targeted debugging.

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
