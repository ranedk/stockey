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

## Screener.in login

Screener.in flows use the running Chrome CDP session and auto-login when needed.

Required env:

- `CDP_ENDPOINT`
- `SCREENER_IN_LOGIN`
- `SCREENER_IN_PASSWORD`

Commands:

```sh
python -m data.screenerin.auth --check
python -m data.screenerin.auth
```

If `https://www.screener.in/login/` redirects to `/dash/`, the session is already logged in. Otherwise the helper fills the login form from env, submits it, and verifies `/dash/`. Credentials are not printed.

## Scheduled runs

The repo now ships with a cron template at `config/stockey.crontab.template`.
`python builder.py` renders the runnable file at `config/stockey.generated.crontab`.

Recommended: run it with `go-crond`:

```sh
mkdir -p /home/rane/code/stockey/logs/cron
python builder.py
./go-crond config/stockey.generated.crontab --allow-unprivileged
```

If you install it with a normal per-user `crontab`, first remove the username column from every job line. The generated file is system-crontab/go-crond style.

```sh
mkdir -p /home/rane/code/stockey/logs/cron
python builder.py
# only after removing the username column:
crontab /home/rane/code/stockey/config/stockey.generated.crontab
```

Important:

- `go-crond` reads the generated file in system-crontab format in this setup, so each scheduled line includes the username field.
- If that field is missing, `go-crond` will treat `cd` as the username and the jobs will not execute even though the runner starts.

Current schedule:

- every `5` minutes: `./all_frontend.sh` under a lock, which keeps the operator API and Nuxt frontend running without duplicates
- `07:10` weekdays: `./complete_data.sh`
- every `10` minutes from `09:00` to `15:59` on weekdays: one-shot `./all_watchers.sh`
- `16:05` weekdays: one final post-close `./all_watchers.sh`
- `08:05`, `12:05`, `17:05`, `22:05` weekdays: `python -m advisory.operator_health --skip-dhan`
- `10:25`, `13:25`, `16:25`, `21:25` weekdays: investor hypothesis/playbook scan over newly collected events
- `11:20`, `14:20`, `17:20`, `20:20` weekdays: experimental TS forecast workflow using `config/ts_forecast_screeners.yaml`
- `18:20`, `21:20` weekdays: matured TS forecast evaluation after costs
- `19:10` weekdays: `./all_advisory.sh`
- `23:10` weekdays: event-policy realized-return evaluation after costs
- `04:20` Saturdays: technical threshold calibration after costs
- `03:10` Sundays: weekly `./all_ml.sh` for event-model research training

Why the split looks like this:

- slow daily and model-prep jobs are isolated from the intra-day watch loop
- CPI, FPI, WPI, macro, masters, and similar sources get covered by the daily raw refresh
- live OHLCV, news, and announcements are handled by the `10` minute watch cadence
- operator health runs a few times per day so stale data, cron errors, dependency failures, and fix hints stay visible without waiting for a manual check
- investor hypothesis scans run after watcher passes so newly collected news/announcements can become playbook matches and operator review notes
- event-model training runs weekly as research evidence only because the live path is still playbooks, deterministic policies, technical timing, macro/regime gating, and risk controls
- TS forecast rows remain research-only and are refreshed a few times per day; daily OHLCV means they should not run on every watcher tick
- event-policy and technical-threshold evaluators are research-only evidence jobs; they do not change live thresholds or submit actions
- the full advisory is not forced on every market tick; it runs once daily after 7pm

Important constraint:

- the cron file uses `scripts/with_lock.sh` so duplicate overlapping runs are skipped instead of piling up; it uses `flock` on Linux and `lockf` on macOS
- the shell wrappers resolve Python automatically, so cron does not need `source .xstockey/bin/activate`

## Operator Health

Use the read-only smoke test before debugging strategy output:

```sh
python -m advisory.operator_health
python -m advisory.operator_health --skip-dhan
```

It checks:

- Postgres query health and freshness of core advisory tables
- operator API reachability and latency through the read-only health endpoint
- Redis reachability
- Dhan token validity through a lightweight profile call; it does not initiate broker login
- Dhan cached-token metadata including cache age, expiry timestamp, and seconds to expiry
- recent `logs/cron/*.log` tails for tracebacks, errors, failures, connection refusals, and timeouts
- Poppler, Codex CLI, Node/npm, TimesFM, and frontend dependency presence

The same data is exposed at `GET /api/health/details` and rendered in the Nuxt `Data Health` page. Warnings mean the system may still run with degraded functionality; errors mean a required dependency or recent cron run likely needs attention.

The health payload also includes `fix_hints`. These are generated from stale tables, cron log errors, missing optional dependencies, Dhan token failures, Redis reachability, and Postgres connectivity. The Nuxt `Data Health` page shows the hints near the top with the command to run first, usually followed by `python -m advisory.operator_health --skip-dhan` to verify the fix. The page can filter health rows by `All`, `Errors`, `Warnings`, `Recovered`, and `OK`.

Useful API/token env knobs:

- `OPERATOR_API_HEALTH_URL`: endpoint checked by operator health, default `http://127.0.0.1:8765/api/health`
- `OPERATOR_API_HEALTH_TIMEOUT_SECONDS`: API health timeout, default `3`

Cron log health is run-aware. A traceback followed by a later success marker is shown as a warning with `latest_run_status=ok_after_historical_errors`; a traceback or failed status after the latest success marker remains an error. This prevents old failures from keeping the page red after a recovered run while still preserving historical errors for audit.

Manual interrupts are handled separately. If a log ends with `KeyboardInterrupt` but the mapped output tables have fresher rows than the log file timestamp, the status becomes `recovered_after_manual_interrupt` and the UI shows the recovery evidence. This avoids treating an operator-stopped stale cron log as a live code failure after a later successful manual run.

Operator shell scripts emit deterministic lifecycle markers:

```text
[stockey.script] name=all_ml status=start timestamp=...
[stockey.script] name=all_ml status=done exit_code=0 timestamp=...
[stockey.script] name=all_ml status=failed exit_code=1 timestamp=...
```

The health parser prefers these markers over log-text heuristics. A successful `done` marker after earlier tracebacks is shown as recovered/historical; a `failed` marker is an active error; an `interrupted` marker is treated as operator interruption.

Current marker-enabled wrappers:

- `all_downloaders.sh`
- `all_parsers.sh`
- `complete_data.sh`
- `all_ml.sh`
- `all_advisory.sh`
- `all_watchers.sh`

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

By default, announcement OCR, concise document summaries, structured report parsing, and event evaluation can run through Codex CLI instead of hosted ChatGPT/Gemini APIs. Use `OCR_USING=codex`, `SUMMARIZE_WITH=codex`, `ADVISORY_EVENT_EVAL_MODEL=codex`, `CODEX_CLI_OCR_MODEL`, `CODEX_CLI_SUMMARIZE_MODEL`, and `CODEX_CLI_EVENT_MODEL`.

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

Codex-supervised variant:

```sh
./all_advisory_codex.sh
```

Use this for long unattended runs where Codex CLI should inspect a failure, patch the repo, and rerun with a bounded attempt count. Logs, Codex prompts, and Codex outputs are written to `logs/codex_supervisor/`. Tune it with `CODEX_SUPERVISOR_MAX_ATTEMPTS`, `CODEX_SUPERVISOR_TAIL_LINES`, `CODEX_SUPERVISOR_TIMEOUT_SECONDS`, and `CODEX_SUPERVISOR_MODEL`.

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
- you want live alerts and router updates available to the operator frontend

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
- watcher/router updates now write fast per-symbol rows to `advisory_signal_refresh_actions`
- signal refresh is for live visibility and manual/operator reaction; the full post-close advisory remains the authoritative portfolio reconciliation
- alerts and cycle summaries are also published over Redis pub-sub

### Fast signal refresh

Use when:

- a watcher found fresh OHLCV/news/announcement data and you want a quick symbol-level decision
- you want to inspect one symbol without running full advisory
- you want to replay recent router intents into the live signal table

Commands:

```sh
python -m advisory.signal_refresh --symbol RELIANCE --reason manual --format text
python -m advisory.signal_refresh --symbol RELIANCE --unique-id <event-id> --reason announcement --format json
python -m advisory.signal_refresh --from-router --limit 25 --format text
```

What it does:

- reads the latest consolidated action, lifecycle/rebalance, and event-policy rows for the symbol
- checks matched hypothesis Wait Signals for the symbol
- gives priority to exit/reduce lifecycle signals over stale buy/watch signals
- writes `advisory_signal_refresh_actions`
- appends decision trace rows and refreshes materialized symbol/event trace summaries
- does not run cross-sectional portfolio allocation or mutate the authoritative portfolio

### Hypothesis Wait Signals

Use when:

- a playbook action plan says “wait for price/news/announcement confirmation”
- you want to see or manually check what conditions are currently active
- you want watcher-triggered matches to become visible without running full advisory

Commands:

```sh
python -m advisory.wait_signals --generate --format text
python -m advisory.wait_signals --match --format text
python -m advisory.wait_signals --match --symbol RELIANCE --format json
```

Behavior:

- hypothesis scans generate `advisory_wait_signals` after action plans are persisted
- price waits are matched against `dhan_ohlcv_daily`
- news/announcement waits are matched against persisted advisory news and announcement event tables
- matches are written to `advisory_wait_signal_matches`
- `./all_watchers.sh` runs a lightweight wait-signal match pass after each watcher cycle
- the Nuxt Playbooks page shows active, matched, and manually checked waits

### 6. Split refreshes

Use when:

- you want to separate raw downloads from parser runs
- you are recovering only one half of the ingestion flow

Commands:

```sh
./all_downloaders.sh
./all_parsers.sh
```

## Operator Frontend

Run the operator API and Nuxt app together with:

```sh
./all_frontend.sh
```

This starts `advisory.api.app` on `127.0.0.1:8765` and Nuxt on `127.0.0.1:3000` by default. It loads `nvm use default` before running Node/npm, logs the resolved Node path/version, and installs frontend dependencies automatically if `apps/operator-web/node_modules` is missing.

The operator API reads `advisory_operator_snapshots` by default. `all_advisory.sh` and `all_watchers.sh` refresh this snapshot after successful runs. To refresh it manually:

```sh
python -m advisory.operator_snapshot
```

Set `OPERATOR_API_USE_SNAPSHOT=false` only when debugging the live dashboard builder directly. `OPERATOR_SNAPSHOT_MAX_AGE_SECONDS=86400` means the API accepts snapshots generated in the last day by default; set it lower for stricter freshness or `0` only for deliberate “latest regardless of age” debugging. `OPERATOR_API_PAYLOAD_CACHE_SECONDS=15` keeps the parsed snapshot in API memory briefly so dashboard pages do not reload the same JSON for every section. `OPERATOR_API_LARGE_RESPONSE_BYTES=250000` records oversized API responses in the slow-operation log so endpoints can be compacted or paginated deliberately.

Slow API and snapshot operations are recorded under `logs/performance/`:

```sh
python scripts/api_latency_probe.py
python -m advisory.performance_slowlog report --limit 20
python -m advisory.performance_slowlog mark <fingerprint> triaged --note "tracked in performance_todo.md"
```

The JSONL file keeps every slow occurrence. The state file dedupes by fingerprint, so the same slow endpoint or snapshot stage is counted repeatedly but does not create a new issue every run.

Legacy NSE retention and archive:

```sh
python scripts/db_table_retention_report.py --retention-days 365
python scripts/archive_legacy_nse_tables.py --table nseindia_var1 --retention-days 365 --max-chunks 3
```

The archive script is dry-run unless `--execute` is passed. Use `--archive-s3` first, validate the uploaded monthly CSV.GZ chunks, and only then add `--delete`. A delete lowers live row count but does not shrink the physical Postgres table file until a table rewrite such as `VACUUM FULL` or `pg_repack`.

Frontend wrapper controls:

```sh
OPERATOR_WEB_USE_NVM=true
OPERATOR_WEB_NVM_VERSION=default
OPERATOR_WEB_INSTALL_DEPS=auto
OPERATOR_WEB_NPM_LEGACY_PEER_DEPS=true
```

To run them separately:

```sh
python -m advisory.api.app --host 127.0.0.1 --port 8765
cd apps/operator-web
npm install
NUXT_PUBLIC_API_BASE=http://127.0.0.1:8765 npm run dev
```

Static `live_dashboard/` generation is deprecated. The frontend reads current state directly from `advisory.api.app`, so cron no longer runs `python -m advisory.live_dashboard`.

Use the Decision Trace page when you need to understand why a symbol changed action or why an event did not change the action. It reads normalized trace summaries from:

```sh
python -m advisory.symbol_trace --symbol RELIANCE
python -m advisory.decision_trace --unique-id <event-id>
```

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
python -m advisory.signal_refresh --from-router --limit 25 --dry-run --format text
```

## Recommended daily order

### Batch mode

1. `./complete_data.sh`
2. `./all_advisory.sh`
3. inspect portfolio output and traces if something looks unusual
4. optional research: `./all_ml.sh`
5. optional research: `python -m advisory.ts_forecast_features --symbols RELIANCE TCS --horizons 5 10 20`

### Live monitoring mode

1. `./all_watchers.sh --loop`
2. `./all_frontend.sh`
3. open `http://127.0.0.1:3000`
4. inspect `advisory.event_router --dry-run` if routing volume looks suspicious
5. inspect `python -m advisory.signal_refresh --from-router --limit 25 --dry-run --format text` if live signal rows look stale

## Redis pub-sub channels

The continuous-watch stack publishes lightweight messages on these channels:

- `stockey:continuous_watch:alerts`
- `stockey:continuous_watch:ohlcv`
- `stockey:continuous_watch:news`
- `stockey:continuous_watch:announcements`
- `stockey:continuous_watch:router`
- `stockey:continuous_watch:operator_frontend`
- `stockey:continuous_watch:summary`

These are for loose coordination and observability. The database remains the source of truth.
