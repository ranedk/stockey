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

## Chrome CDP session

Screener.in and Dhan automated login use the same Chrome remote-debugging session. Start it before running browser-backed flows:

```sh
scripts/start_chrome_cdp.sh
```

Then keep this env in `.env`:

```sh
CDP_ENDPOINT=http://localhost:9222
```

Useful overrides:

```sh
CDP_PORT=9223 CHROME_USER_DATA_DIR=~/.stockey/chrome-cdp-9223 scripts/start_chrome_cdp.sh
CHROME_BINARY="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" scripts/start_chrome_cdp.sh
```

This starts Chrome in the foreground with a separate user-data directory. Stop it with `Ctrl-C` when you are done.

Use the operator UI `/screeners` page before promoting a new Screener.in idea. The page validates known local syntax problems without opening Chrome, and its “fetch preview rows” action calls `POST /api/screeners/preview` with `persist=false`. This can use the authenticated Screener.in session and can record failure audit rows, but it does not store query results, register a screener, change recommendations, or submit broker orders. Register a screener only after preview rows look correct.

The same page shows read-only coverage metrics from `GET /api/screeners/coverage`: constituent count, candidate count, final action count, positive-action rate, manual-review count, and exit count by screener. Use this to retire noisy screeners or investigate why a screener is not contributing useful candidates. Do not treat coverage alone as proof that a screener is profitable; it is attribution, not outcome validation.

## Scheduled runs

The repo now ships with a cron template at `config/stockey.crontab.template`.
`python builder.py` renders the runnable file at `config/stockey.generated.crontab`.

Recommended: run it with `go-crond`:

```sh
mkdir -p /home/rane/code/stockey/logs/cron
python builder.py
python scripts/cron_preflight.py
./go-crond config/stockey.generated.crontab --allow-unprivileged
```

If you install it with a normal per-user `crontab`, first remove the username column from every job line. The generated file is system-crontab/go-crond style.

`python scripts/cron_preflight.py` is read-only. It fails on missing generated crontab, missing required environment lines, missing/non-executable referenced scripts, unresolved Python, or other setup errors. It warns on stale lock directories and reports whether the operator API/web ports are currently available or already reachable. The same check is available in the Operations UI as `Cron Preflight`.

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

- every `5` minutes: `./all_frontend.sh` under a lock, which keeps the operator API and Nuxt frontend running without duplicates and restarts after source-code changes
- `07:10` weekdays: `./complete_data.sh` broad morning safety net
- `08:30`, `12:30`, `16:30` weekdays: `./all_downloaders_queue.sh` to enqueue single-client NSE/Dhan/Screener downloader work while running safe downloader modules inline
- `08:35`, `12:35`, `16:35` weekdays: `./all_external_workers.sh` to drain Dhan, Screener, and NSE queues serially
- every `10` minutes from `09:00` to `15:59` on weekdays: one-shot `./all_watchers.sh`
- `16:05` weekdays: one final post-close `./all_watchers.sh`
- `07:55`, `11:55`, `16:55`, `21:55` weekdays: `./all_api_latency_probe.sh`
- `08:05`, `12:05`, `17:05`, `22:05` weekdays: `./all_operator_health.sh`
- `10:25`, `13:25`, `16:25`, `21:25` weekdays: `./all_hypothesis_scan.sh` over newly collected events
- `11:20`, `14:20`, `17:20`, `20:20` weekdays: `./all_ts_forecast_workflow.sh` using `config/ts_forecast_screeners.yaml`
- `18:20`, `21:20` weekdays: `./all_ts_forecast_evaluator.sh` after costs
- `21:35` weekdays: `./all_ts_forecast_paper_portfolio.sh` for research-only forecast paper-portfolio validation
- `17:30` weekdays: `./complete_data.sh` end-of-day catch-up before advisory
- `18:55` weekdays: `./all_advisory_preflight.sh` validates/refreshes Dhan auth and runs compact operator smoke before advisory
- `19:10` weekdays: `./all_advisory.sh`, after waiting for data catch-up and external worker locks to clear
- `23:10` weekdays: `./all_event_policy_evaluator.sh` after costs
- `04:20` Saturdays: `./all_technical_threshold_calibration.sh` after costs
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

Script groups:

- recurring cron scripts: `all_frontend.sh`, `all_watchers.sh`, `all_downloaders_queue.sh`, `all_external_workers.sh`, morning and pre-advisory `complete_data.sh`, post-close `all_advisory.sh`, `all_api_latency_probe.sh`, `all_operator_health.sh`, `all_hypothesis_scan.sh`, TS/event-policy/technical research wrappers, and weekly `all_ml.sh` if enabled
- manual / catch-up / long-running scripts: `all_downloaders.sh`, `all_parsers.sh`, manual `complete_data.sh`, manual `all_ml.sh`, and `all_advisory_codex.sh`
- preflight/debug scripts: `all_advisory_preflight.sh` validates Dhan/CDP/token readiness and compact Health before spending hours on `all_advisory.sh`; it can refresh the Dhan token cache but does not run advisory or submit orders
- use the manual group end-of-day, after missed runs, before major reruns, or during debugging; do not add them to high-frequency cron

Important constraint:

- cron commands use `scripts/with_lock.sh` where needed so duplicate overlapping runs are skipped instead of piling up
- `all_watchers.sh` also self-locks with `/tmp/stockey_watchers.lock`, so manual and cron watcher runs cannot overlap
- watcher source cursors live in `advisory_sync_state`; if a watcher tick is skipped because the previous run is still active, the next run resumes from the last successful cursor instead of only checking the last `10` minutes
- downloader/parser runner state also lives in `advisory_sync_state` under `download_runner:<module>` source names; failed rows are visible in Operator Health with classifications such as `source_unavailable`, `auth_unavailable`, `parse_failed`, and `no_data`
- the shell wrappers resolve Python automatically, so cron does not need `source .xstockey/bin/activate`

## Operator Health

Use the compact read-only smoke test before debugging strategy output:

```sh
python -m advisory.operator_smoke
python -m advisory.operator_smoke --format text
```

It returns the current `status`, `trust_level`, Advisory Trust Gate recommendation, top blockers, fix hints, and exact next commands. It skips Dhan token validation by default so it does not trigger broker login side effects.

Use the detailed health command when you need the full diagnostic payload:

```sh
python -m advisory.operator_health --skip-dhan
python -m advisory.operator_health
```

Together these checks cover:

- Postgres query health and freshness of core advisory tables
- operator API reachability and latency through the read-only health endpoint
- Redis reachability
- Dhan token validity through a lightweight profile call; it does not initiate broker login
- Dhan cached-token metadata including cache age, expiry timestamp, and seconds to expiry
- identity readiness, including open Dhan/security mapping issue rows and latest broker-capable action rows that do not join to `company_master` or have no Dhan NSE/BSE security id
- announcement and NSE/bhavcopy evidence readiness through `advisory.event_data_quality`
- NSE announcement HTTP retry and cookie/session reset counts through fallback telemetry (`nse_retry`, `nse_session_reset`) with a specific Health fix hint when those counters are nonzero
- recent Screener.in query/fetch/parse failure rows from `screenerin_parse_failures` in full Health mode, including fix hints, trust-gate impact, and degradation rows for bad query syntax, login/session, or parse failures
- recent `logs/cron/*.log` tails for tracebacks, errors, failures, connection refusals, and timeouts
- Poppler, Codex CLI, Node/npm, TimesFM, and frontend dependency presence

The same data is exposed at `GET /api/health/details` and rendered in the Nuxt `Data Health` page. Warnings mean the system may still run with degraded functionality; errors mean a required dependency or recent cron run likely needs attention.

The health payload also includes `fix_hints`. These are generated from stale tables, cron log errors, missing optional dependencies, Dhan token failures, identity/action mapping gaps, Redis reachability, Postgres connectivity, event-evidence quality issues, Screener.in failures, and feature stage-gate blockers. The Nuxt `Data Health` page shows the hints near the top with the command to run first, usually followed by `python -m advisory.operator_health --skip-dhan` to verify the fix. The page can filter health rows by `All`, `Errors`, `Warnings`, `Recovered`, and `OK`.

## Operator Paper Portfolio

Use the Nuxt `/recommendations` page when you want to manually build a clean paper portfolio from the latest consolidated recommendations.

The workflow is intentionally separate from the advisory model portfolio:

- The page shows only action-capable rows: `BUY`, `BUY_MORE`, `SELL`, and `PARTIAL_SELL`.
- `WATCH`, `HOLD`, and `MANUAL_REVIEW` stay out of this page; use Watchlist or Manual Review for those.
- Each row shows exactly one operator action button: `BUY`, `BUY_50%`, `SELL`, or `SELL_50%`.
- The page is paper-portfolio-state aware. After a reset, only `BUY` rows are shown because there is no open paper position to sell. Once a symbol is open in the paper ledger, `BUY_MORE`, `SELL`, and `PARTIAL_SELL` rows for that symbol can appear.
- `BUY` writes one paper-ledger entry for the symbol at the visible current/reference price.
- `BUY_50%` records a paper add/half-entry action.
- `SELL` closes the currently open paper position at the visible current/reference price.
- `SELL_50%` records a partial-exit paper action without closing the position.
- `/paper-portfolio` shows only entry price, exit price, current price, and percentage gain/loss.
- The paper ledger does not mutate `advisory_action_recommendations`, `advisory_portfolio_orders`, Dhan execution rows, or live broker orders.

Reset command:

```sh
python scripts/reset_operator_portfolio.py --confirm
```

This deletes only rows from `advisory_operator_portfolio_ledger`. It does not reset advisory history, recommendations, model output, execution approvals, or Dhan state. Use it when you want the operator paper portfolio to start from scratch while keeping the research/advisory database intact.

Dhan cache health also reports whether a non-interactive refresh path is ready. `auth_refresh_ready=false` with `auto_login_configured=true` usually means `CDP_ENDPOINT` is configured but Chrome remote debugging is not reachable; Health fix hints put `scripts/start_chrome_cdp.sh` before the Dhan refresh command in this case. Dhan auto-login is intentionally fail-hard when Chrome/CDP is unavailable. Run that Chrome CDP session first, then run `python -m data.dhanlive.auth_cli ensure --auto-login`, then rerun `all_advisory.sh`. If the Dhan page is reachable but slow between mobile, TOTP, PIN, and redirect steps, tune `DHAN_AUTO_LOGIN_STEP_TIMEOUT_MS`.

Run the event-evidence quality gate directly when announcement/bhavcopy inputs look suspicious:

```sh
python -m advisory.event_data_quality --format json
python -m advisory.event_data_quality --format text
```

This check is read-only. It does not change portfolio state or action authority. It reports whether source tables are fresh, announcement documents have parse/OCR/text coverage, exchange events are typed, corporate-action/earnings/deal features are visible, and raw tables are large enough that UI/LLM paths should use compact evidence caches instead of direct scans.

Compact event evidence is rebuilt by `complete_data.sh` through `advisory.event_evidence_store`. Run it directly for targeted repair:

```sh
python -m advisory.event_evidence_store --from-date 2026-06-01 --to-date 2026-06-08
python -m advisory.event_evidence_store --dry-run --lookback-days 30
```

It writes `advisory_bhavcopy_evidence_daily` and `advisory_announcement_evidence`. These are intended for UI, LLM context, and future deterministic event intelligence so those paths do not scan raw bhavcopy tables or large announcement text columns.

The Health page also shows a superseded cleanup preview when recovered event-processing failures or recovered announcement-document errors can be marked superseded. Inspect the sample rows first, then either use the guarded Health-page `Mark Superseded` form with an operator reason or run `python -m advisory.superseded_failures --limit 500` / `./all_superseded_cleanup_audit.sh` for the dry-run JSON. Cron runs `./all_superseded_cleanup_audit.sh` after market close as a preview-only audit, and the Operations page exposes the same dry-run through `superseded_failure_cleanup_dry_run`. The UI apply path writes only durable superseded metadata, audits `superseded_failure_cleanup_apply`, and does not submit broker orders or change portfolio/action/config state. Only use the shell `python -m advisory.superseded_failures --apply --limit 500` as a manual fallback after explicit operator intent.

The Health page also reads `advisory_fallback_events` and shows a persisted fallback telemetry card. Treat warning spikes as review-only signals until understood; error-severity fallbacks such as synthetic event-evaluation fallback should block trust in the affected fresh advisory output. The first emitters cover Redis fail-soft, Dhan identity fallback/unresolved identity, NSE retry/session reset, and key LLM/Codex deterministic fallbacks.

Operator-facing telemetry and log snippets are redacted before they are written or returned by the API where practical. Fallback telemetry, Operator API error rows, Health cron summaries, and Operations cron-log tails mask common access tokens, auth URLs, bearer/basic auth headers, mobile numbers, PIN/TOTP/password fields, and sensitive mapping keys. This does not rewrite existing raw files on disk, so do not commit `.env`, `.cache`, Chrome profiles, token caches, or raw `logs/` output.

Useful API/token env knobs:

- `OPERATOR_API_HEALTH_URL`: endpoint checked by operator health, default `http://127.0.0.1:8765/api/health`
- `OPERATOR_API_RUNTIME_URL`: runtime metadata endpoint checked by operator health for stale API code, default `http://127.0.0.1:8765/api/runtime`
- `OPERATOR_API_HEALTH_TIMEOUT_SECONDS`: API health timeout, default `3`
- `OPERATOR_WEB_HEALTH_URL`: frontend URL checked by operator health, default `http://127.0.0.1:3000/`
- `OPERATOR_WEB_HEALTH_TIMEOUT_SECONDS`: frontend runtime health timeout, default `3`

`python -m advisory.operator_health --skip-dhan` defaults to JSON for cron/API consumers. Use `--format text` for a short terminal summary, or `--format json` when piping into another script.

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
- `all_frontend.sh`
- `all_advisory_codex.sh`
- `all_analysis_codex.sh`
- `all_api_latency_probe.sh`
- `all_operator_health.sh`
- `all_hypothesis_scan.sh`
- `all_ts_forecast_workflow.sh`
- `all_ts_forecast_evaluator.sh`
- `all_ts_forecast_paper_portfolio.sh`
- `all_event_policy_evaluator.sh`
- `all_technical_threshold_calibration.sh`

Most wrappers use `scripts/run_with_markers.sh`; `all_frontend.sh` emits markers internally so it can still clean up supervised API/Nuxt child processes on exit or interruption.

## Database robustness knobs

Large advisory/model-prep runs can hit transient remote PostgreSQL failures or statement timeouts. The shared DB helper now retries transient read, connect, metadata, and upsert failures, disposes the stale SQLAlchemy pool, reconnects, and writes large upsert payloads through local temporary files before `COPY`.

Useful environment variables:

- `SQL_TO_DF_RETRIES`: retry count for transient read failures, default `2`
- `SQL_TO_DF_RETRY_SLEEP_SECONDS`: base sleep between retries, default `1.0`
- `SQL_TO_DF_STATEMENT_TIMEOUT_MS`: optional per-query statement timeout override, default `0` which leaves server defaults unchanged
- `SQL_TO_DF_CHUNK_SIZE`: optional fetch chunk size for reads, default `0` which keeps the old fetch-all behavior
- `DB_OPERATION_ATTEMPTS`: minimum attempts for DB connects, metadata reads, and upserts, default `3`
- `DB_POOL_RECYCLE_SECONDS`: SQLAlchemy pool recycle interval, default `300`
- `DB_RETRY_TELEMETRY_FILE`: local JSONL spool for Postgres retry/exhaustion events, default `logs/fallback/db_retry_events.jsonl`
- `LOCAL_FALLBACK_TELEMETRY_FILE`: local JSONL spool for fallback events that should not attempt a DB write, default `logs/fallback/local_fallback_events.jsonl`

DB retry telemetry and selected hot-path fallback telemetry are file-backed instead of DB-backed so they still work when Postgres is the failing component. Full Operator Health folds recent rows from these spools into the fallback/degradation summary and emits specific fix hints, including Postgres retry spikes, operator current-price cache fallback, operator snapshot fallback when DB snapshots or snapshot price refresh fail, risk sizing that had to use max allocation as the liquidity cap because ADV20 was missing, risk sizing that skipped macro or exchange-event context after lookup failures, event-router failures that can skip or delay watcher-triggered fast refreshes, external-task-queue failures that can delay or hide serialized NSE/Dhan/Screener work, config-change assistant failures that block reviewed config diff generation without applying config, research-ledger failures that can leave false-discovery or model-validation audit rows incomplete, signal refreshes that continued without action/lifecycle/event-policy/router context after lookup failures, action consolidation that had to use default conflict rules or ignore promoted dynamic conflict rules after lookup failures, wait-signal matching that skipped news/announcement/source evidence after lookup or schema failures, feature freshness checks that marked required inputs errored after lookup failures, company-memory reviews that skipped source context after lookup/load failures, news-theme routing that returned empty output after as-of or market-news lookup failures, event-policy evaluation that returned empty research output after schema/source-load failures, event-data-quality checks that degraded while checking source freshness/readiness, trace-summary cache or rebuild failures that force symbol/event pages toward live or partial trace fallback, compact event-evidence refresh failures that leave announcement/bhavcopy evidence stale or unavailable, event meta-model failures that prevent labels or omit optional intraday/macro/exchange context, exchange-feature failures that leave deal/insider/short/corporate-action context missing, market-context failures that can make broad-market gating stale or partial, fundamental-snapshot failures that leave peer/fundamental context unavailable or partial, TS forecast feature/evaluator failures that leave experimental TS Watch rows or validation summaries missing, watchlist-builder failures that can skip event-driven state changes or prior watch metadata, symbol-trace source failures that can make symbol detail pages incomplete or unavailable, setup-trace source failures that can make setup funnel pages incomplete or unavailable, technical-threshold calibration failures that can make threshold review output missing or incomplete, signal-quality evaluator failures that can make overlay-promotion evidence incomplete or unavailable, promotion-review evidence lookup failures that can prevent manual review rows from being created, event-model promotion-check failures that can make model readiness evidence unavailable, macro-feature failures that can make macro/regime context stale or unavailable, regime-engine failures that can make market-regime snapshots stale or unavailable, technical-feature failures that can make rule-scoring inputs stale or unavailable, intraday-feature failures that can make intraday breakout/context inputs stale or unavailable, rule-engine failures that can make candidate pass/watch/rejection output stale or unavailable, portfolio-engine failures that can make portfolio approval/defer/overlap output stale or unavailable, position-lifecycle failures that can make hold/exit/partial-exit output stale or unavailable, and execution-engine failures that can make broker handoff or reconciliation state stale, incomplete, or blocked.

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

By default, announcement OCR, concise document summaries, structured report parsing, and event evaluation can run through Codex CLI instead of hosted ChatGPT/Gemini APIs. Use `OCR_USING=codex`, `SUMMARIZE_WITH=codex`, `ADVISORY_EVENT_EVAL_MODEL=codex`, `CODEX_CLI_OCR_MODEL`, `CODEX_CLI_SUMMARIZE_MODEL`, and `CODEX_CLI_EVENT_MODEL`. Cron often has a narrower `PATH` than your shell; set `CODEX_CLI_BIN` to an absolute path when possible. The Python wrapper also searches common nvm, local npm, Homebrew, and `/usr/local/bin` locations and records fallback telemetry if Codex is still unavailable.

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

Queued downloader mode:

```sh
./all_downloaders_queue.sh
./all_external_workers.sh
```

Use queued mode during normal operation when NSE/Dhan/Screener work should be serialized. Use direct `./all_downloaders.sh` or `./complete_data.sh` as the catch-up/backfill path when a day was missed or you explicitly want a broad refresh.

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
./all_advisory_preflight.sh
./all_advisory.sh
```

Default performance behavior:

- runs `dhan_auth_preflight` first unless `ADVISORY_DHAN_PREFLIGHT=false`; this validates or refreshes Dhan auth before expensive advisory stages
- runs independent local/DB feature stages with bounded threads
- skips hidden rule-engine daily/intraday repair by default
- expects data gaps to be handled by `complete_data.sh`, watchers, or the external task queue
- emits machine-readable `stage_timings`, `slow_stages`, and `stage_budget` in the JSON summary so long runs can be diagnosed by stage without reading the full log

Useful overrides:

```sh
ADVISORY_LOCAL_STAGE_WORKERS=2 ./all_advisory.sh
ADVISORY_PARALLEL_LOCAL_STAGES=0 ./all_advisory.sh
ADVISORY_DISABLE_RULE_REPAIR=0 ./all_advisory.sh
ADVISORY_STAGE_BUDGET_SECONDS=900 ./all_advisory.sh
ADVISORY_STAGE_BUDGET_OVERRIDES=rules=1800,exchange_features=900 ./all_advisory.sh
ADVISORY_DHAN_PREFLIGHT=false ./all_advisory.sh
```

Use `ADVISORY_DISABLE_RULE_REPAIR=0` only when you deliberately want the advisory batch to repair missing Dhan/fundamental inputs inline. That can make the run much slower.

Use `ADVISORY_DHAN_PREFLIGHT=false` only for targeted dry-runs that do not need Dhan-backed price refresh. Normal advisory runs should keep the preflight enabled so an expired token fails or refreshes before the expensive pipeline starts. If this preflight fails, `all_advisory.sh` reports `step=dhan_auth_preflight`, prints `scripts/start_chrome_cdp.sh` as the first repair step when CDP may be unavailable, and then prints the auth refresh command instead of an old advisory stage report. `python -m data.dhanlive.auth_cli ensure --auto-login` returns structured JSON for refresh failures such as CDP/Chrome being unavailable, so cron logs should show `status=error`, `error_type`, `error`, and `operator_action` rather than a Python traceback. Related knobs are `ADVISORY_DHAN_PREFLIGHT_AUTO_LOGIN`, `ADVISORY_DHAN_PREFLIGHT_MIN_FRESH_MINUTES`, and `ADVISORY_DHAN_PREFLIGHT_SKIP_VALIDATE`.

Use `./all_advisory_preflight.sh` before manual long advisory runs when you want to verify Dhan/CDP/token readiness and compact operator trust state without launching `advisory.master_pipeline`. It accepts the same Dhan preflight env knobs plus `ADVISORY_PREFLIGHT_SKIP_SMOKE=true` and `ADVISORY_PREFLIGHT_FIX_HINT_LIMIT`.

If `./all_advisory_preflight.sh` fails before smoke, it prints the failing step plus the same CDP-first recovery path directly in the log. If smoke fails after Dhan auth succeeds, it tells you to inspect the compact smoke output and rerun Health.

Operator Health inspects `logs/cron/all_advisory_preflight.log` separately from the full advisory log. If CDP or Dhan auth fails there, Health and the degradation feed classify it as a Dhan auth/CDP preflight failure and show the repair order: inspect the log, start `scripts/start_chrome_cdp.sh`, rerun `./all_advisory_preflight.sh`, then rerun Health.

The compact smoke output from `./all_advisory_preflight.sh` includes a `dhan_readiness` block with token validation status, cache status, `auth_refresh_ready`, auto-login configuration, CDP status, and whether CDP recovery is required. Treat `auth_refresh_ready=false` or `cdp_recovery_required=true` as a blocker before running `./all_advisory.sh`.

Stage budgets are reporting-only. They mark slow stages in stderr and in the final JSON summary but do not fail or stop the run. Use them to decide whether to move more work into downloader/external queues, add indexes, or tighten stage inputs.

After a long cron/manual advisory run, extract the latest stage timing summary from the mixed log with:

```sh
python scripts/advisory_stage_report.py --log-path logs/cron/all_advisory.log --limit 20
python scripts/advisory_stage_report.py --format json
```

The same read-only report is also available from the Operations page as the audited `advisory_stage_report` command. Use the UI command when you want the run recorded in operator command history.

Operator Health also checks this report. If the latest `all_advisory.log` has no parseable stage summary, if any stage is over budget, or if a failed/incomplete run only emitted stage markers before the traceback, Health emits a fix hint pointing to the CLI command and the audited Operations command. Failed marker-only runs should show the nearest failed stage, for example `intraday`, instead of a generic missing-summary warning.

`all_advisory.sh` also prints this stage report automatically when the wrapped advisory command fails. If Dhan automated login hits Playwright's sync API inside an existing asyncio loop, the auth layer retries the automated login in a subprocess before giving up; this avoids the historical intraday-stage crash where the traceback ended with `Playwright Sync API inside the asyncio loop`.

For targeted dry-run verification, set `ADVISORY_SKIP_POST_REFRESH=true` to skip the post-run `operator_snapshot` and `trace_summary_store` refreshes. Do not use this for scheduled production advisory runs, because the frontend depends on those refreshes after a successful full run.

Use the report before guessing where to optimize. If `rules`, `exchange_features`, or another stage is repeatedly over budget, fix that specific stage instead of changing the whole pipeline.

Codex-supervised variant:

```sh
./all_advisory_codex.sh
```

Use this for long unattended runs where Codex CLI should inspect a failure, patch the repo, and rerun with a bounded attempt count. Logs, Codex prompts, and Codex outputs are written to `logs/codex_supervisor/`. Tune it with `CODEX_SUPERVISOR_MAX_ATTEMPTS`, `CODEX_SUPERVISOR_TAIL_LINES`, `CODEX_SUPERVISOR_TIMEOUT_SECONDS`, and `CODEX_SUPERVISOR_MODEL`.

Analysis-development loop:

```sh
ANALYSIS_AGENT_MAX_CYCLES=1 ./all_analysis_codex.sh
```

Use this when you want Codex CLI to continue development from `analysis.md`. The loop reads `analysis.md` and `docs/analysis_agent_board.md`, picks the next bounded slice, edits code/docs/tests, runs focused validation, and updates the board. It is manual-only and should not run from cron.

Operational guardrails:

- Default max cycles is `1`; increase deliberately, for example `ANALYSIS_AGENT_MAX_CYCLES=3 ./all_analysis_codex.sh`.
- Runs above the hard cap, default `3`, are refused unless you pass `--allow-large-runs` or set `ANALYSIS_AGENT_ALLOW_LARGE_RUNS=true`.
- A single cycle is capped at `ANALYSIS_AGENT_MAX_FILES_PER_CYCLE`, default `20`, to prevent broad unattended diffs. Split larger work manually.
- If both `apps/operator-web/package-lock.json` and `apps/operator-web/yarn.lock` change in one cycle, the wrapper stops unless `--allow-lockfile-drift` is explicitly passed. Prefer one package manager path before accepting frontend dependency changes.
- After each completed Codex cycle, the wrapper now runs deterministic gates before continuing: `git diff --check`, Python compile for changed `.py` files, full `pytest -q tests/test_advisory_regression.py` when backend/Python paths changed, and Nuxt typecheck/tests when `apps/operator-web` changed.
- The loop stops if root-level generated NSE CSV artifacts are visible to git. Move them under an approved data/download path or add a specific ignore rule before continuing.
- The loop requires every Codex response to include `CYCLE_STATUS: complete`, `CYCLE_STATUS: blocked`, or `CYCLE_STATUS: no_open_slices`; missing markers stop the run.
- Prompts require Planner, Builder, Reviewer, and Integrator phases. If Codex CLI has subagent tools in that environment, it may use them for bounded planner/reviewer sidecars, but the wrapper still treats local post-checks as the source of truth.
- Logs, prompts, stdout, and last Codex messages are written to `logs/analysis_agents/`.
- It fails closed on Codex errors and is prompted to stop instead of editing broker execution, destructive DB/data cleanup, credential-dependent work, or unclear production-safety changes.
- After each cycle, review `docs/analysis_agent_board.md`, `analysis.md`, `git diff`, and the validation lines before running another cycle.
- Manual Review follow-up: open `/manual-review` and `/wait-signals` in the operator UI after cycles that touch Manual Review, wait signals, action policy, or health. Confirm new items are understandable and not duplicated before accepting the slice.

## Advisory, Watcher, Wait Signal, And Manual Review Boundaries

Use this table when deciding whether to run a full advisory pass, rely on watcher output, inspect wait signals, or make a Manual Review decision.

| Flow | What starts it | What it reads | What it writes | What it can change | What it cannot do |
| --- | --- | --- | --- | --- | --- |
| Full advisory | `./all_advisory.sh` after fresh data, normally post-close | Screener universe, snapshots, event policy, adversarial review, risk, portfolio, lifecycle, action history | Current advisory tables including portfolio/order plans, lifecycle, consolidated action recommendations, execution previews, traces, and operator snapshot | Authoritative daily portfolio/action reconciliation and dry-run execution-plan state | Submit live broker orders without explicit live-execution gates |
| Watchers | `./all_watchers.sh` cron or loop during market hours | Active watchlist names, open positions, recent OHLCV, news, announcements, wait signals, persisted watcher cursors | Watch alerts, fresh source rows, wait-signal matches, signal-refresh rows, trace summaries, operator snapshot | Fast operator visibility for fresh evidence and per-symbol action/evidence changes | Replace the full cross-sectional advisory, recompute authoritative portfolio allocation, or submit orders |
| Fast signal refresh | Watcher router or `python -m advisory.signal_refresh ...` | Latest consolidated action, lifecycle/rebalance rows, event-policy rows, matched wait signals for one symbol, and router price/news/announcement trigger context when available | `advisory_signal_refresh_actions`, trace rows, materialized trace summaries | Show whether a symbol-level signal changed, created a wait-match action, matched a review-only stop/entry watcher trigger, or only refreshed evidence, including previous action and action-changed trace fields | Run full allocation/risk sizing across the universe or mutate authoritative portfolio rows |
| Execution Approvals | `/execution-approvals` or `/api/execution/approvals` after dry-run execution previews exist | Latest `advisory_execution_orders` rows, execution safety contracts, audit-only approval decisions, broker order-state reads when reconciliation is requested, historical execution evidence rows when evidence review is requested, and current planned rows when live-submit preflight is requested | Optional audit rows in `advisory_execution_approval_decisions`; optional safety-contract update to `operator_approval_status=approved` after latest `approve_dry_run` audit; optional reconciliation/fill persistence through `/api/execution/reconcile` with `confirm=true`; optional evidence review rows in `advisory_execution_evidence_reviews`; optional live-allowance rows in `advisory_execution_live_allowance_reviews` | Show missing operator approval, broker reconciliation, live-evidence checklist, live-submission, and dry-run blockers; record review intent; mark operator approval status after reviewed audit; preview or persist broker reconciliation; mark evidence passed after enough reviewed cycles; set `live_submission_allowed=true` after all safety gates and exact phrase are satisfied; generate the read-only live-submit preflight token and manual CLI command | Bypass approval, bypass evidence checklist, submit orders from the UI/API, or treat reconciliation/evidence/live allowance/preflight as broker submission |
| Regime Review | `python -m advisory.regime_overlay` creates proposals; `/regime-overlays` records decisions | Base market regime, market context, macro features, recent news, announcement-event counts, proposed rules | `advisory_regime_overlay_proposals` and `advisory_regime_overlay_decisions` | Approve a regime overlay for testing, promote it to a review-rule candidate, reject it, or request more evidence | Directly relax market gates, change sizing, create actions, mutate portfolio rows, or submit broker orders |
| Wait signals | Playbook action plans, Manual Review `watch_for_event`, or `python -m advisory.wait_signals ...` | Typed wait conditions plus price/news/announcement evidence | `advisory_wait_signals` and `advisory_wait_signal_matches` | Record that a future condition is active, matched, expired, or closed | Trade, approve actions, or change portfolio state by itself |
| Manual Review | Operator decision in `/manual-review` or API decision endpoint | Active manual items, source row context, decision/effect table, optional wait-signal fields | `advisory_manual_review_decisions`; for `watch_for_event`, an active wait signal | Close or annotate a review item; create a watched condition; reopen matched Manual Review wait-signal follow-up work | Submit broker orders, directly mutate portfolio rows, or directly rewrite action recommendations |

Operator rule of thumb:

- Use `./all_advisory.sh` when the question is "what is the authoritative current action/portfolio state?"
- Use watcher and signal-refresh output when the question is "what changed intraday for a watched symbol?"
- Use `/wait-signals` when the question is "which future evidence did we decide to wait for, and did it arrive?"
- Use `/regime-overlays` when the question is "does this proposed macro/news regime layer deserve testing, rejection, or future rule implementation?"
- Use `/manual-review` when the question is "what explicit operator decision should be recorded for this item?"
- If a matched wait signal looks actionable, review the evidence first, then run the relevant refresh/advisory flow. A wait match is evidence, not approval.

Watcher trigger boundaries:

| Trigger or observation | Immediate watcher effect | Full advisory required before treating as authoritative? | Notes |
| --- | --- | --- | --- |
| Fresh intraday OHLCV for a watched symbol or open position | Writes watcher alerts and can refresh that symbol into `advisory_signal_refresh_actions` | Yes, for portfolio sizing, final allocation, and dry-run execution previews | Intraday refresh is symbol-scoped visibility. It does not rebuild the universe or reconcile the portfolio. |
| Fresh exchange announcement or news for an active watch/open position | Persists the source evidence, routes material events, and refreshes the affected symbol | Yes, when the evidence changes investability, sizing, or final action state | The watcher can show `previous_action`, `action_changed`, or `evidence_only`; that is not final approval. |
| Top market-context news or announcement without deterministic materiality keywords | Persists `context_observed` evidence only | Yes, if the operator wants it considered in cross-sectional advisory state | Context-only observations do not trigger action authority by themselves. |
| Top market-context news or announcement with deterministic materiality keywords | Marks the item `triggered` and routes it for symbol-level refresh/evaluation | Yes, before using it as authoritative portfolio/action state | Materiality routing improves freshness, but still remains a watcher overlay. |
| Active wait signal matches price/news/announcement evidence | Writes `advisory_wait_signal_matches`; signal refresh may create a review-only `WATCH`, `MANUAL_REVIEW`, or reduce-review signal | Yes, before any broker-capable action or portfolio mutation | Matched waits are evidence. Manual Review follow-up is expected for operator-created waits. |
| Watcher tick skipped because the self-lock is held | Publishes skipped watcher events and exits without advancing cursors | No immediate advisory action; inspect only if skips persist | The next successful watcher resumes from persisted source cursors. |
| Watcher run fails before cursor persistence | Leaves the last successful cursor in place for retry | Usually yes after recovery, especially if the failure spans a trading session | Use `complete_data.sh` for broad repair if intervals were missed beyond bounded watcher catch-up. |
| Bounded watcher catch-up is truncated by lookback limits | Publishes catch-up metadata with `catchup_truncated` | Yes | Run `complete_data.sh` or the relevant downloader/parser catch-up before trusting daily advisory output. |
| Scheduled post-close advisory window arrives after data catch-up | `./all_advisory.sh` performs full advisory reconciliation | This is the authoritative path | It recomputes cross-sectional advisory state and dry-run execution-plan state, but still does not submit live broker orders without explicit gates. |

### Operator Decision Effects

| Decision | Runtime state after save | Active queue effect | Side effects | What it never does |
| --- | --- | --- | --- | --- |
| `needs_more_data` | `open_needs_more_data` | Keeps the item open | Records the operator rationale | Mutate portfolio rows, action recommendations, config, or broker orders |
| `watch_for_event` | `waiting_for_event` | Keeps the item open until the wait signal matches or the item is later closed | Records the decision and creates an active typed wait signal from the follow-up text | Approve the future matched evidence, mutate portfolio rows, action recommendations, config, or broker orders |
| `add_operator_note` | `annotated` | Keeps the item open | Records operator context only | Close the item, mutate portfolio rows, action recommendations, config, or broker orders |
| `approve_for_manual_config` | `closed_approved_for_manual_config` | Removes the item from the active queue | Records approval for a later manual code/config change | Apply the config change automatically, mutate portfolio rows, action recommendations, or broker orders |
| `ignore` | `closed_ignored` | Removes the item from the active queue | Records that the item is noise or not worth further review | Mutate source rows, portfolio rows, action recommendations, config, or broker orders |
| `downgrade_to_no_action` | `closed_no_action` | Removes the item from the active queue | Records an explicit no-action operator decision | Rewrite the stored recommendation, mutate portfolio rows, or broker orders |
| `mark_fixed` | `closed_fixed` | Removes the item from the active queue | Records that an operational issue was fixed or a rerun succeeded | Clean old DB rows, mutate source rows, portfolio rows, action recommendations, or broker orders |

The `/api/manual-review/decision` write response returns the saved `manual_review_state` and `decision_effect` payloads immediately. The Manual Review UI uses that to show the next state after save before the refreshed queue removes, keeps, or reopens the item.

Matched Manual Review wait-signal behavior:

- If the original item is still in `watch_for_event`, a matched wait signal suppresses the stale waiting item and reopens follow-up work as `reopened_wait_signal_matched`.
- If the original item was closed later by `ignore`, `mark_fixed`, `downgrade_to_no_action`, or `approve_for_manual_config`, the matched wait signal is suppressed from active Manual Review instead of reopening stale work.
- If the matched follow-up item itself is later closed by an operator decision, that follow-up is also suppressed from active Manual Review and does not keep reappearing.
- A matched wait can create a review-only action candidate linked to the original item and match evidence. It remains evidence for operator review; it is not approval and cannot submit an order.

Manual Review also collapses repeated unresolved conflict rows for the same symbol/action pair to the newest active item. The API summary reports `duplicate_suppressed` so repeated source rows are visible as queue hygiene rather than multiple decisions for the same conflict. If an operator closes one of those repeated conflict items, later rows with the same canonical symbol/action conflict are suppressed even when the transient source key changes.

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

- the script self-locks via `scripts/with_lock.sh`; if another watcher is still running, the new run logs `[stockey.lock] skip already_running` and exits `0`
- OHLCV, news, and announcements use persisted cursors in `advisory_sync_state`, not cron wall-clock assumptions
- OHLCV keeps a small overlap on every pull and does not advance `last_item_ts` when no fresh intraday candle was actually observed
- OHLCV watcher catch-up is capped by `WATCHER_OHLCV_MAX_LOOKBACK_MINUTES` to avoid a stale cursor making every `10` minute cron run download days of 1-minute candles; `complete_data.sh` remains the broad catch-up path
- Live price alerts use `WATCHER_ALERT_COOLDOWN_SECONDS` to suppress repeated alerts with the same setup/symbol/type/source/state fingerprint. The watcher publishes `alert_input_count`, `alert_persisted_count`, and `alert_suppressed_count`; if the dedupe lookup fails, it records fallback telemetry and persists alerts fail-open so evidence is not lost.
- Each watcher cycle writes normalized `source_counters` into its result and `advisory_sync_state`: OHLCV reports symbols, sync results, latest-price rows, alert persisted/suppressed counts; news reports watch rows, RSS item count, matched/persisted event counts, triggered/context counts, and feed count; announcements report watch rows, unique ingest targets, ingest run/discovered/parsed/failed counts, matched/persisted event counts, and watch-update count.
- Operator Health and the Health page show the latest watcher `source_counters`, including stale/missing/error status and a fix hint to rerun `./all_watchers.sh` when OHLCV/news/announcement watcher output is not healthy.
- if a run fails before cursor persistence, the next due run retries from the previous successful cursor
- `complete_data.sh` or `all_downloaders.sh` remains the broad end-of-day catch-up path if a full day was missed
- there is no default cap on how many symbols the router may reevaluate

Advisory intraday behavior:

- `ADVISORY_INTRADAY_LOOKBACK_DAYS` defaults to `30`; this is the history window maintained/refreshed for advisory intraday inputs
- feature construction reads only the target `asof_date` session from `dhan_ohlcv_intraday`, because the persisted advisory feature row is daily
- use `complete_data.sh` for broad intraday repair instead of increasing the live advisory lookback by default
- symbols stop being watched once advisory removes them from the watch path
- open positions remain monitored for exit-related alerts
- watcher/router updates now write fast per-symbol rows to `advisory_signal_refresh_actions`
- signal refresh is for live visibility and manual/operator reaction; the full post-close advisory remains the authoritative portfolio reconciliation
- signal-refresh rows/API payloads carry `authority_scope=review_input_only`, `portfolio_authority=none`, `broker_execution_allowed=false`, and `full_advisory_required=true`
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
- uses watcher-router trigger context when available: stop/invalidation price alerts become review-only `REDUCE_EXPOSURE_REVIEW`, entry-zone/breakout alerts become review-only `WATCH`, and fresh news/announcement router context becomes `MANUAL_REVIEW` until event-policy/full advisory catches up
- writes `advisory_signal_refresh_actions`
- appends decision trace rows and refreshes materialized symbol/event trace summaries
- does not run cross-sectional portfolio allocation or mutate the authoritative portfolio
- does not create broker authority; use the persisted authority fields above to verify this in SQL/API/UI

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
- wait conditions are typed in `condition_json.condition_type`
- supported wait condition types are `price_level`, `event_keywords`, `clarification_filing`, `result_update`, `management_commentary`, `sector_event`, and `expiry_only`
- price-level waits are matched against `dhan_ohlcv_daily`
- news/announcement waits are matched against persisted advisory news and announcement event tables with source/evidence assumptions captured in the condition payload
- invalid or unknown wait condition types are reported as matcher issues instead of crashing the watcher
- matches are written to `advisory_wait_signal_matches`
- `./all_watchers.sh` runs a lightweight wait-signal match pass after each watcher cycle
- signal refresh treats matched waits as evidence, not direct execution: negative waits can become `REDUCE_EXPOSURE_REVIEW`, positive waits become `WATCH`, and ambiguous waits become `MANUAL_REVIEW`
- the Nuxt `/wait-signals` page shows active, matched, expired, and closed waits with source labels and latest evidence

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

The supervisor watches a lightweight source signature for the operator API and Nuxt app. With `OPERATOR_FRONTEND_RESTART_ON_CODE_CHANGE=true`, a long-running frontend process exits with a `[stockey.script] ... status=restart_requested` marker when relevant Python or Nuxt files change. The cron lock is released and the next `all_frontend.sh` cron tick starts a fresh API/frontend process, which prevents stale API code from serving fixed endpoints for hours. Use `OPERATOR_FRONTEND_CODE_CHECK_SECONDS=60` to tune the check interval.

Targeted restart commands:

```sh
./all_frontend.sh --api-only
./all_frontend.sh --web-only
./all_frontend.sh --both
```

Use `--api-only` after Python/API changes, `--web-only` after Nuxt-only changes, and `--both` when ports or shared API contracts changed. The equivalent environment knob is `OPERATOR_FRONTEND_COMPONENT=both|api|web`.

The operator API reads `advisory_operator_snapshots` by default. `all_advisory.sh` and `all_watchers.sh` refresh this snapshot after successful runs. To refresh it manually:

```sh
python -m advisory.operator_snapshot
```

The same repair is available from the Operations page as the audited `operator_snapshot_refresh` command. It rebuilds only DB-backed operator snapshot/cache rows and does not change recommendations, portfolio state, config, or broker orders.

Set `OPERATOR_API_USE_SNAPSHOT=false` only when debugging the live dashboard builder directly. `OPERATOR_SNAPSHOT_MAX_AGE_SECONDS=86400` means the API treats snapshots generated in the last day as fresh. With `OPERATOR_API_ALLOW_STALE_SNAPSHOT=true`, the operator UI still serves the latest stale snapshot instead of blocking a page load on a live rebuild; the payload metadata marks it as stale. `OPERATOR_API_PAYLOAD_CACHE_SECONDS=15` keeps the parsed snapshot in API memory briefly so dashboard pages do not reload the same JSON for every section. `OPERATOR_API_LARGE_RESPONSE_BYTES=250000` records oversized API responses in the slow-operation log so endpoints can be compacted or paginated deliberately. Keep `OPERATOR_API_INTRADAY_PRICE_FALLBACK=false` unless debugging prices; ad-hoc intraday scans can make the main Action Queue slow, while watcher alerts should already persist live `last_price`.

Manual Review is lane-filtered at the API boundary. `/api/manual-review` defaults to `lane=investment_review` so the active queue is not polluted by parser, identity, OCR, or execution-planning repair work. Use `lane=technical_issue` for system/data fixes, `lane=research_config` for research/config approvals, or `lane=all` for a full audit view. The payload still returns `all_active_by_lane` and `lane_filtered_out` so the UI can show hidden technical/research counts while keeping the default investment queue focused.

Action Queue and Symbol Detail pages also show feature freshness. Consolidated action rows preserve the decision-time freshness snapshot, while the separate current panel shows present source state. The advisory pipeline also emits `feature_gate` summaries under `rules`, `risk`, `portfolio`, `lifecycle`, and `actions`, which tells you whether required `daily_ohlcv` / `technical_daily` inputs were blocked for the symbols being processed. Operator Health samples the latest action symbols and reports these stage gates in `feature_stage_gates`, fix hints, current blockers, and the trust gate. Symbol Detail shows `Stage Gate Effects`, which explains the concrete row-level impact when available: watch downgrade, review-only allocation, deferred capital, lifecycle warning, or final action downgrade. Blocked `rules` gates move immediate `PASS_NOW` candidates to `WATCH_EVENT`; blocked `risk` gates move automatic allocations to `review_manual`; blocked `portfolio` gates defer approved/trimmed capital; blocked lifecycle gates add warnings without suppressing exit/risk-reduction actions; blocked final `BUY` or `BUY_MORE` rows become `MANUAL_REVIEW`, review-only/no-broker-execution. Action consolidation also downgrades broker-capable winners to `MANUAL_REVIEW` when company/Dhan identity is missing; Health still reports active identity coverage gaps for visibility and repair. Execution dry-run previews add a final guard for older or non-standard broker-capable action rows: unresolved Dhan identity becomes `submit_blocked` with `broker_identity_status=failed` in the execution safety contract and an execution fallback telemetry row. The Action Queue execution section also shows the portfolio handoff boundary when present: `PLANNED_ENTRY` versus non-entry state, required entry evidence, broker-direct block status, and transition-contract issues. Each action-table order preview also carries `order_intent_lineage`, so an operator can trace the order back to the action row, reason-contract summary/status, risk sizing, stop/target levels, and approval/reconciliation gates before any live broker handoff. Live submission additionally needs an order-set-specific confirmation token, so a token from another same-day batch cannot approve a different symbol/quantity set.

For cross-step debugging, use the Nuxt `/operator-journey` page. It is backed by the read-only `/api/operator-journey` endpoint and stitches manual-review decisions, wait signals, wait-signal matches, signal-refresh rows, action recommendations, portfolio rows, and execution previews into stage buckets plus a single newest-first timeline. Use filters such as `symbol`, `item_id`, or `unique_id` to narrow the journey.

Slow API and snapshot operations are recorded under `logs/performance/`:

```sh
python scripts/api_latency_probe.py --output-path logs/performance/latest_api_latency_probe.json
python scripts/api_performance_report.py --limit 20
python -m advisory.performance_slowlog report --limit 20
python -m advisory.performance_slowlog mark <fingerprint> triaged --note "tracked in todo.md"
```

Cron runs the API latency probe at 07:55, 11:55, 16:55, and 21:55 on weekdays. The default probe uses compact, paged list routes for Actions, Portfolio, and Watchlist so it measures normal operator traffic rather than full debug payloads. The latest JSON summary is read by Operator Health; stale, slow, or failed probes appear in fix hints and the degradation feed. Use `scripts/api_performance_report.py` to rank which endpoint to fix next before adding indexes or changing payload shapes. When probe evidence is fresh, report rows are ranked by the current probe first and labelled `fresh_probe`; older unprobed slowlog rows remain visible as `historical_slowlog` and should be targeted only after a route-specific probe confirms the issue. The JSONL file keeps every slow occurrence. The state file dedupes by fingerprint, so the same slow endpoint or snapshot stage is counted repeatedly but does not create a new issue every run.

Manual Review list payloads are compact by default because this page can aggregate many source tables. The source-row expander shows a compact preview and tells you how many raw keys were omitted. Use `/api/manual-review?include_raw=true&limit=<n>` only for short debugging sessions when you explicitly need full raw rows.

Action Queue and Portfolio API reads are also compact by default. Use `/api/actions?compact=false&limit=<n>` or `/api/portfolio?compact=false&limit=<n>` only for short debugging sessions; normal UI and probe traffic should rely on compact list routes plus `/api/actions/detail` or `/api/portfolio/<symbol>/detail` for row-level evidence. `/api/home` omits duplicated action/today cards by default; use `/api/home?include_action_cards=true` only for legacy/debug reads. The Portfolio UI should pass `bucket=<selected section>` to `/api/portfolio` so only the selected tab is returned; omit `bucket` only for symbol/detail/debug reads that need every section. `/api/actions?include_feature_freshness=true` uses persisted decision-time freshness snapshots by default; use `refresh_feature_freshness=true` only for explicit current-live diagnostics because it can perform per-symbol freshness checks.

Watchlist API reads support `section=<selected section>` and compact rows. Use `/api/watchlist?section=ts_forecast_watch&compact=true&limit=25` for TS Watch lists and `/api/watchlist?compact=false&section=ts_forecast_watch&limit=<n>` only when inspecting raw TS forecast swing/position windows.

Operator Health details are also compact by default. The Health page calls `/api/health/details?compact=true`, which bounds list sizes and truncates very long strings so tracebacks or full diagnostic rows do not make the page slow. Fast Health still checks the operator snapshot freshness because stale snapshots can make the UI show old advisory actions. It intentionally defers heavier DB diagnostics such as trace-cache scans, identity coverage, signal-quality evidence, feature stage gates, downloader state, ingestion file-state, and schema registry inspection; the API returns a `deferred_diagnostics` summary and the Health page shows the skipped checks plus the full-health command to run when you need that evidence. Use `/api/health/details?mode=full&compact=false` only for short debugging sessions. `OPERATOR_HEALTH_FAST_WORKERS`, `OPERATOR_HEALTH_COMPACT_LIST_LIMIT`, and `OPERATOR_HEALTH_COMPACT_STRING_CHARS` control the default API bounds.

The operator smoke preflight is also compacted at source. `OPERATOR_SMOKE_COMPACT_LIST_LIMIT` and `OPERATOR_SMOKE_COMPACT_STRING_CHARS` bound fix hints and blocker details so `/api/operations/smoke` and `python -m advisory.operator_smoke` stay lightweight even when Health contains long tracebacks.

Actions and Portfolio responses include payload-size telemetry in compact mode. Check `/api/actions?compact=true` and `/api/portfolio?compact=true` response `meta.<section>.payload_bytes`, `avg_row_bytes`, and `max_row_bytes` before changing indexes or payload shapes. High latency with low payload bytes points to DB/query work; high `max_row_bytes` points to row compaction or detail-endpoint work.

Event-policy list rows are compact by default. `/api/event-policy` keeps parsed `checks`, `operator_notes`, `llm_review`, and compact `raw_context`, but omits bulky source JSON strings like `raw_context_json` and `operator_notes_json`. Use `/api/event-policy?limit=<n>&include_raw=true` only for bounded debugging.

Legacy NSE retention and archive:

```sh
python scripts/db_table_retention_report.py --retention-days 365
python scripts/archive_legacy_nse_tables.py --table nseindia_var1 --retention-days 365 --max-chunks 3
```

The archive script is dry-run unless `--execute` is passed. Use `--archive-s3` first, validate the uploaded monthly CSV.GZ chunks, and only then add `--delete`. A delete lowers live row count but does not shrink the physical Postgres table file until a table rewrite such as `VACUUM FULL` or `pg_repack`.

Serialized external task queue:

```sh
python -m advisory.external_task_queue --queue nse --enqueue --task-type smoke --task-args-json '{}'
python -m advisory.external_task_queue --queue nse --worker --once
python -m advisory.external_task_queue --queue nse --status
```

Concrete task types:

```sh
python -m advisory.external_task_queue --queue dhan --enqueue --task-type dhan_daily_ohlcv --task-args-json '{"symbols":["RELIANCE"],"exchange":"NSE","asset_type":"stock"}'
python -m advisory.external_task_queue --queue dhan --enqueue --task-type dhan_intraday_ohlcv --task-args-json '{"symbols":["RELIANCE"],"interval_minutes":5}'
python -m advisory.external_task_queue --queue dhan --enqueue --task-type dhan_scrip_master --task-args-json '{}'
python -m advisory.external_task_queue --queue screener --enqueue --task-type screener_login --task-args-json '{"check_only":false}'
python -m advisory.external_task_queue --queue nse --enqueue --task-type nse_module --task-args-json '{"module":"data.nseindia.recent_events","args":[]}'
```

This queue is the foundation for single-client NSE/Dhan/Screener work. Keep these queues single-lane; do not parallelize Chrome/CDP-heavy or broker-token-heavy work from the advisory process.

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

Static `live_dashboard/` generation is deprecated. The frontend reads current state directly from `advisory.api.app`, so cron no longer runs the legacy static dashboard generator.

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
