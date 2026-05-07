# Operator Manual

This is the short runbook for daily use.

Use this file when you want the important commands, when to run them, and what each one is for.

## Python interpreter behavior

All top-level shell wrappers resolve Python in this order:

1. `PYTHON_BIN` if you set it explicitly
2. the active virtualenv `python` if you already ran `source .xstockey/bin/activate`
3. the repo-local fallback at `.xstockey/bin/python`
4. system `python3`, then `python`

That means:

- interactive use after activating the venv works naturally
- cron can call the shell wrappers directly without activating the venv first, as long as `.xstockey` exists
- if you want to force a specific interpreter, set `PYTHON_BIN`

Cron example:

```sh
cd /home/rane/code/stockey && ./all_advisory.sh
```

Bootstrap command:

```sh
python builder.py
```

That creates or refreshes the project venv, creates `logs/cron`, and installs `go-crond` in the repo root if it is missing.

TimesFM setup:

```sh
python builder.py
```

Builder installs `torch` and the current Google Research TimesFM package from GitHub by default because the TS forecast workflow uses TimesFM. It intentionally does not install the old PyPI `timesfm` package path, which can pull Pax/Lingvo dependencies on some Python/platform combinations.

Lightweight setup without TimesFM:

```sh
python builder.py --skip-timesfm-install
```

Equivalent env form:

```sh
STOCKEY_SKIP_TIMESFM_INSTALL=true python builder.py
```

If you need to pin a different TimesFM source, set:

```sh
STOCKEY_TIMESFM_PACKAGE='git+https://github.com/google-research/timesfm.git#egg=timesfm[torch]' python builder.py
```

## Scheduled runs

The repo now ships with a cron template at `config/stockey.crontab.template`.
`python builder.py` renders the runnable file at `config/stockey.generated.crontab`.

Install it with system cron:

```sh
mkdir -p /home/rane/code/stockey/logs/cron
python builder.py
crontab /home/rane/code/stockey/config/stockey.generated.crontab
```

Or run it with `go-crond`:

```sh
mkdir -p /home/rane/code/stockey/logs/cron
python builder.py
./go-crond config/stockey.generated.crontab --allow-unprivileged
```

Important:

- `go-crond` reads the generated file in system-crontab format in this setup, so each scheduled line includes the username field.
- If that field is missing, `go-crond` will treat `cd` as the username and the jobs will not execute even though the runner starts.

Current schedule:

- `07:10` weekdays: `./complete_data.sh`
- `03:10` weekdays: `./all_ml.sh`
- every `10` minutes from `09:00` to `15:59` on weekdays: one-shot `./all_watchers.sh`
- `16:05` weekdays: one final post-close `./all_watchers.sh`
- `11:20`, `14:20`, `17:20`, `20:20` weekdays: experimental TS forecast workflow using `config/ts_forecast_screeners.yaml`
- `18:20`, `21:20` weekdays: matured TS forecast evaluation after costs
- `19:10` weekdays: `./all_advisory.sh`

Why the split looks like this:

- slow daily and model-prep jobs are isolated from the intra-day watch loop
- CPI, FPI, WPI, macro, masters, and similar sources get covered by the daily raw refresh
- live OHLCV, news, and announcements are handled by the `10` minute watch cadence
- training remains a once-daily research process after 3am
- TS forecast rows remain research-only and are refreshed a few times per day; daily OHLCV means they should not run on every watcher tick
- the full advisory is not forced on every market tick; it runs once daily after 7pm

Important constraint:

- the cron file uses `scripts/with_lock.sh` so duplicate overlapping runs are skipped instead of piling up; it uses `flock` on Linux and `lockf` on macOS
- the shell wrappers resolve Python automatically, so cron does not need `source .xstockey/bin/activate`

## Database robustness knobs

Large advisory/model-prep runs can hit transient remote PostgreSQL failures or statement timeouts. The shared DB helper now retries transient read, connect, metadata, and upsert failures, disposes the stale SQLAlchemy pool, reconnects, and writes large upsert payloads through local temporary files before `COPY`.

Useful environment variables:

- `SQL_TO_DF_RETRIES`: retry count for transient read failures, default `2`
- `SQL_TO_DF_RETRY_SLEEP_SECONDS`: base sleep between retries, default `1.0`
- `SQL_TO_DF_STATEMENT_TIMEOUT_MS`: optional per-query statement timeout override, default `0` which leaves server defaults unchanged
- `SQL_TO_DF_CHUNK_SIZE`: optional fetch chunk size for reads, default `0` which keeps the old fetch-all behavior
- `DB_OPERATION_ATTEMPTS`: minimum attempts for DB connects, metadata reads, and upserts, default `3`
- `DB_POOL_RECYCLE_SECONDS`: SQLAlchemy pool recycle interval, default `300`

Model-training backfills deliberately skip intraday prefetch during snapshot repair. This avoids wasting time on old 1-minute windows when the goal is event-label coverage, not perfect intraday reconstruction.

## OCR dependency

Announcement PDF OCR requires Poppler. On macOS:

```sh
brew install poppler
```

On Ubuntu:

```sh
sudo apt-get install poppler-utils
```

The OCR code resolves Poppler from `POPPLER_PATH`, then `PATH`, then common Homebrew/system locations. The generated cron `PATH` includes `/opt/homebrew/bin` for macOS Homebrew installs.

## Redis robustness knobs

Redis is used for lightweight cursors, parsed/downloaded sets, and live pub-sub. It is not the source of truth for advisory data; PostgreSQL upserts are. Ingestion jobs now retry Redis operations and, by default, continue without Redis state if Redis is unavailable.

Useful environment variables:

- `REDIS_OPERATION_ATTEMPTS`: retry attempts for Redis commands, default `3`
- `REDIS_RETRY_SLEEP_SECONDS`: base sleep between Redis retries, default `1.0`
- `REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS`: Redis connection timeout, default `2.0`
- `REDIS_SOCKET_TIMEOUT_SECONDS`: Redis command timeout, default `5.0`
- `REDIS_FAIL_SOFT`: continue without Redis state after retries, default `true`

Set `REDIS_FAIL_SOFT=false` only for flows where Redis itself is the product, such as live pub-sub debugging. For ingestion, keep it `true` so a Redis outage does not block DB writes.

## Main operating modes

### 1. Complete data refresh

Use when:

- you want all raw downloads and parser steps to run in order

Command:

```sh
./complete_data.sh
```

Useful variants:

```sh
./all_downloaders.sh
./all_parsers.sh
```

What it does:

1. runs every download module
2. runs every parser module
3. writes the combined status summary

### 2. Advisory and portfolio

Use when:

- raw data is already fresh
- you want the batch advisory output and current portfolio state

Command:

```sh
./all_advisory.sh
```

### 3. Model prep and training

Use when:

- you are working on the event model
- you want to backfill labels and see if training is justified

Command:

```sh
./all_ml.sh
```

Useful variants:

```sh
./all_ml.sh --prep-only
./all_ml.sh --horizon-days 1
```

What it does:

1. runs `advisory.event_model_data_prep`
2. checks label coverage
3. trains only if the requested horizon is ready
4. scores current events after training unless skipped

### 4. Experimental OHLCV forecasts

Use when:

- you want research-only forecast features from already downloaded Dhan OHLCV
- you want to compare future TimesFM / Chronos / Moirai-style adapters against a simple baseline

Command:

```sh
python -m advisory.ts_forecast_features --dry-run --symbols RELIANCE TCS
```

Useful variants:

```sh
python -m advisory.ts_forecast_features --symbols RELIANCE TCS --horizons 5 10 20
python -m advisory.ts_forecast_features --date 2026-04-30 --horizons 5 20
python -m advisory.ts_forecast_features --refresh-ohlcv --symbols RELIANCE TCS --model-name timesfm_2p5_200m --horizons 5 10 20
python -m advisory.ts_forecast_evaluator --dry-run --from-date 2026-04-01 --to-date 2026-04-30
python -m advisory.ts_forecast_evaluator --from-date 2026-04-01 --to-date 2026-04-30 --cost-bps 25
python -m advisory.ts_forecast_workflow --symbols RELIANCE TCS --model-name timesfm_2p5_200m
python -m advisory.ts_forecast_workflow --model-name timesfm_2p5_200m
```

Important behavior:

- writes to `advisory_ts_forecasts_daily` when not using `--dry-run`
- evaluator writes row-level checks to `advisory_ts_forecast_evaluations` and grouped metrics to `advisory_ts_forecast_eval_summary`
- workflow writes experimental positives to `advisory_ts_forecast_watchlist`
- live dashboard shows `advisory_ts_forecast_watchlist` positives near the top as TS Watch Recommendations and shows latest `advisory_ts_forecast_eval_summary` quality under Experimental TimesFM Watch
- dashboard collapses repeated symbol/horizon rows into one TS card with Swing window, Position window, combined state, and update history
- default dependency-free model is `naive_momentum_v1`
- optional TimesFM model is `timesfm_2p5_200m` and requires installing the TimesFM torch package
- when workflow runs without `--symbols` or `--query`, it uses `config/ts_forecast_screeners.yaml`
- the default TS screener is a liquid technical candidate generator; if Screener.in is unavailable, the workflow logs the failure and falls back to a capped Dhan/tracked universe
- cron caps the TS run with `TS_FORECAST_MAX_SYMBOLS`, default `80`, to avoid accidentally running TimesFM over the full market
- does not create buy/sell actions
- does not submit anything to Dhan
- should be treated as paper/research evidence until validated after costs and slippage

### 5. Continuous watch

Use when:

- you want the live watch loop running during market hours
- you want active watchlist names and open positions monitored continuously
- you want the static dashboard refreshed automatically

Command:

```sh
./all_watchers.sh --loop
```

Useful variants:

```sh
./all_watchers.sh --loop --sleep-seconds 300
./all_watchers.sh --loop --ohlcv-interval-seconds 300 --news-interval-seconds 1800 --announcement-interval-seconds 1800
```

What it watches:

- active advisory watchlist names
- open positions from portfolio and lifecycle outputs
- recent intraday OHLCV
- exchange announcements
- ET/news

Important behavior:

- there is no default cap on how many symbols the router may reevaluate
- symbols stop being watched once advisory removes them from the watch path
- open positions remain monitored for exit-related alerts
- alerts and cycle summaries are also published over Redis pub-sub

### 6. Split refreshes

Use when:

- you want to separate raw downloads from parser runs
- you are recovering only one half of the ingestion flow

Commands:

```sh
./all_downloaders.sh
./all_parsers.sh
```

## Dashboard

Generate and serve the lightweight dashboard with:

```sh
python -m advisory.live_dashboard --output-dir live_dashboard
python -m http.server --directory live_dashboard 8000
```

`python -m http.server` only serves files. The dashboard files themselves are refreshed by:

- `python -m advisory.live_dashboard --output-dir live_dashboard`
- `./all_watchers.sh` during watch cycles
- weekday cron refreshes when `./go-crond config/stockey.crontab --allow-unprivileged` is running

The dashboard files are:

- `live_dashboard/index.html`
- `live_dashboard/dashboard.json`

## Important inspection commands

### Portfolio

```sh
python -m advisory.portfolio_engine --include-planned --format text
```

### One setup trace

```sh
python -m advisory.setup_trace DEFENSIVE_REGIME_POSITION_V1 --format text
```

### One symbol trace

```sh
python -m advisory.symbol_trace LUPIN --format text
```

### Research ledger

```sh
python -m advisory.research_ledger --limit 20
```

### Live routing dry-run

```sh
python -m advisory.event_router --dry-run
```

## Recommended daily order

### Batch mode

1. `./complete_data.sh`
2. `./all_ml.sh`
3. optional: `python -m advisory.ts_forecast_features --symbols RELIANCE TCS --horizons 5 10 20`
4. `./all_advisory.sh`
5. inspect portfolio output and traces if something looks unusual

### Live monitoring mode

1. `./all_watchers.sh --loop`
2. `python -m advisory.live_dashboard --output-dir live_dashboard`
3. `python -m http.server --directory live_dashboard 8000`
4. inspect `advisory.event_router --dry-run` if routing volume looks suspicious

## Redis pub-sub channels

The continuous-watch stack publishes lightweight messages on these channels:

- `stockey:continuous_watch:alerts`
- `stockey:continuous_watch:ohlcv`
- `stockey:continuous_watch:news`
- `stockey:continuous_watch:announcements`
- `stockey:continuous_watch:router`
- `stockey:continuous_watch:dashboard`
- `stockey:continuous_watch:summary`

These are for loose coordination and observability. The database remains the source of truth.
