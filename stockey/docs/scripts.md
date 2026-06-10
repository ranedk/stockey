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
| `advisory/technical_threshold_calibration.py` | `advisory_technical_threshold_evaluations`, `advisory_technical_threshold_eval_summary` | Research-only calibration of technical-engine thresholds against realized forward Dhan OHLCV returns after costs |
| `advisory/technical_threshold_promotion.py` | `advisory_technical_threshold_promotion_reviews`, `advisory_technical_threshold_promotion_decisions` | LLM-assisted manual review of calibrated technical thresholds plus operator approval/rejection audit rows; produces patch guidance without applying config changes |
| `advisory/config_change_assistant.py` | `advisory_config_change_previews` | Generates reviewed unified diffs from approved technical-threshold or signal-quality promotion decisions; preview/audit only, never applies config changes |
| `advisory/prompt_registry.py` | read-only metadata | Central inventory of LLM/Codex prompt contracts, schemas, model env vars, source files, authority scope, fallbacks, and migration status |
| `advisory/event_policy.py` | `advisory_event_policy_actions` | Deterministic and bounded Codex-assisted mapping from structured event evaluations to buy-watch, manual review, reduce-exposure review, or no action, including operator notes for actionable manual reviews |
| `advisory/event_policy_evaluator.py` | `advisory_event_policy_evaluations`, `advisory_event_policy_eval_summary` | Research-only evaluation of event-policy action/classes against realized forward Dhan OHLCV returns after costs |
| `advisory/company_memory_review.py` | `advisory_company_memory_reviews` | Review-only company-memory signal summaries from compact announcement evidence, bhavcopy evidence, technical state, event policy, wait signals, and latest action rows; deterministic by default, optional Codex |
| `advisory/signal_quality_evaluator.py` | `advisory_signal_quality_evaluations`, `advisory_signal_quality_eval_summary` | Research-only comparison of technical-only signals versus technical signals enriched with event-policy, bhavcopy, and company-memory overlays after costs |
| `advisory/signal_quality_promotion.py` | `advisory_signal_quality_promotion_reviews`, `advisory_signal_quality_promotion_decisions` | Manual review of signal-quality overlay lift plus operator approval/rejection audit rows; produces review-only overlay rule guidance without changing live policy |
| `advisory/exchange_events.py` | `advisory_exchange_events` | Normalizes NSE block/bulk/short-selling/insider/corporate-action/earnings rows into point-in-time exchange events |
| `advisory/exchange_features.py` | `advisory_exchange_features_daily` | Builds daily symbol-level exchange-event features for LLM context, event-model features, review, and risk sizing |
| `advisory/event_data_quality.py` | read-only checks | Reports announcement and NSE/bhavcopy evidence readiness: source freshness, parse/OCR/text coverage, exchange-event typing, exchange-feature coverage, and raw-table scan risk |
| `advisory/feature_freshness.py` | read-only checks | Classifies per-symbol advisory inputs as fresh, stale, missing, error, or intentionally skipped; powers Action Queue data-input summaries and Symbol Detail freshness contracts |
| `advisory/event_evidence_store.py` | `advisory_bhavcopy_evidence_daily`, `advisory_announcement_evidence` | Builds compact query-friendly evidence stores from NSE bhavcopy/deals/short/circuit/volatility/margin rows and exchange announcements plus latest event evaluation fields |
| `advisory/market_context.py` | `advisory_market_context_universe_daily`, `advisory_market_context_summary_daily` | Builds the top-50% market-context universe from technical/liquidity/market-cap data and summarizes breadth, leadership, sector clusters, events, and regime context |
| `advisory/operator_health.py` | read-only checks | Operator smoke-test command for DB, Redis, Dhan token, cron logs, key table freshness, identity issues, signal-quality evidence, frontend dependencies, Poppler, Codex, and TimesFM; emits `fix_hints` plus an Advisory Trust Gate for the Nuxt health page |
| `advisory/identity_issues.py` | read-only by default; optional repair apply | Rechecks open Dhan/security identity issues after scrip/company master refresh; use `--apply` only after dry-run rows show `would_resolve` |
| `advisory/cron_status.py` | read-only checks | Parses `config/stockey.generated.crontab`, joins scheduled jobs to lock directories and cron log tails, and powers the Operations scheduled-jobs card |
| `advisory/operator_smoke.py` | read-only checks | Compact one-command operator preflight over Health that returns status, trust level, fix hints, current blockers, and next commands; also available as an audited Operations UI command |
| `advisory/superseded_failures.py` | `advisory_event_processing_runs`, `announcement_pipeline_documents` | Previews recovered failure rows that can be marked superseded; default is dry-run JSON, and `--apply` requires explicit operator intent before writing superseded metadata |
| `advisory/event_model_data_prep.py` | varies | One-shot prep flow for event-model training: normalizes missing screener constituents, backfills historical event evaluations, refreshes price history, and reports label coverage |
| `advisory/model_training_runner.py` | varies | Gated model-training orchestrator: runs prep, checks label coverage for the requested horizon, then trains and scores only when ready |
| `advisory/event_model_artifact_store.py` | S3/object store | Uploads trained event-model JSON, metadata JSON, and manifest JSON to versioned and `latest` S3 prefixes |
| `advisory/event_model_promotion_check.py` | read-only checks | Conservative evidence gate for deciding whether weekly event-model results are ready for manual operator review as a low-weight input |
| `advisory/sync_state.py` | `advisory_sync_state` | Shared incremental state storage for continuous polling and operator frontend status |
| `advisory/continuous_watch.py` | `advisory_live_watch_alerts`, `advisory_sync_state` | Lightweight watch loop over active watchlist OHLCV, announcements, ET/news, and operator status |
| `advisory/event_router.py` | `advisory_live_router_actions`, `advisory_sync_state` | Symbol-level router that turns fresh live alerts and events into fast signal-refresh intents |
| `advisory/signal_refresh.py` | `advisory_signal_refresh_actions`, `advisory_decision_traces` | Fast per-symbol live decision layer that reads latest action, lifecycle/rebalance, and event-policy rows without running full portfolio allocation |
| `advisory/wait_signals.py` | `advisory_wait_signals`, `advisory_wait_signal_matches` | Converts hypothesis/playbook action plans into machine-checkable price/news/announcement wait conditions and matches them deterministically |
| `advisory/decision_trace.py` | `advisory_decision_traces`, `advisory_decision_trace_steps`, `advisory_event_processing_runs`, `advisory_action_conflicts`, `advisory_action_conflict_rules` | Durable trace layer that links ingest, event evaluation, review, lifecycle, action consolidation, conflict rows, and seeded conflict-resolution rules |
| `advisory/action_conflict_resolver.py` | `advisory_action_conflicts`, `advisory_action_conflict_rules` | Re-applies enabled conflict-resolution rules to latest or historical action conflicts after rules are edited; dedupes duplicate conflict rows, keeps resolved conflicts as audit-only trace data, and marks only unresolved/manual-required conflicts for manual handling |
| `advisory/manual_review_state.py` | `advisory_manual_review_decisions`, `advisory_wait_signals` | Central state/effect contract for operator Manual Review decisions; defines closing versus annotating decisions, no-trade safety flags, and wait-signal side effects |
| `advisory/trace_summary_store.py` | `advisory_trace_summaries` | Materializes compact symbol/event trace summaries so the operator frontend does not rebuild large traces live on every page load |
| `advisory/live_dashboard.py` | JSON payload builder | Legacy static dashboard module; API still reuses its payload builder while Nuxt replaces static generation |
| `advisory/operator_snapshot.py` | `advisory_operator_snapshots` | Builds the compact DB-backed operator dashboard snapshot used by the API to avoid rebuilding the full dashboard payload on every frontend request |
| `advisory/performance_slowlog.py` | files under `logs/performance/` | Deduped slow-operation logger and state manager for API latency, snapshot builds, and future slow pipeline sections |
| `advisory/api/app.py` | operator HTTP API | Serves operator-facing JSON endpoints, normalized trace summaries, hypothesis write/audit endpoints, and technical-calibration review endpoints for the Nuxt app |
| `advisory/external_task_queue.py` | `advisory_external_task_queue` | Serialized queue foundation for single-client NSE/Dhan/Screener work so browser/API-heavy jobs do not run concurrently inside advisory |
| `scripts/wait_for_locks.sh` | none | Shell guard used by cron so advisory waits for data catch-up and external queue locks before reconciling actions |
| `advisory/live_notifier.py` | files in `live_dashboard/` | Subscribes to Redis pub-sub from the continuous-watch stack and writes a human-readable operator feed |
| `advisory/adversarial_review.py` | `advisory_event_reviews` | Deterministic reviewer over structured event tensors; can clear, penalize, force manual review, or veto event-driven allocations |
| `utils/ocr` | none | Provider-agnostic PDF OCR utility using Gemini 3 Flash preview and OpenAI GPT-5 nano |
| `utils/transcribe` | none | Audio transcription utility for remote mp3/wav/mp4 links using Gemini 3 Flash preview and OpenAI transcription APIs |
| `data/nseindia/bhavcopy_parser.py` | `nseindia_*` daily tables | Parses downloaded NSE archives for the legacy/reference NSE pipeline |
| `data/nseindia/adjusted_prices.py` | `nseindia_corporate_actions_normalized`, `nseindia_ohlcv_adjusted` | Builds split/bonus-adjusted OHLCV for the legacy/reference NSE pipeline |
| `data/nseindia/security_history.py` | `dim_security_history`, `dim_security_review_events`, `dim_security_overrides` | Builds canonical security identity history and review queue for renames / identity breaks |
| `data/nseindia/security_dimension.py` | `dim_security` | Current canonical security dimension keyed by `security_id` |
| `data/nseindia/indices_parser.py` | `nseindia_indices` | Index history |
| `data/benchmark_sync.py` | `dhan_ohlcv_daily` | Syncs canonical advisory benchmark rows such as `NIFTY` from parsed NSE index history so benchmark features do not depend on stale Dhan index candles |
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
| `scripts/db_size_report.py` | read-only Postgres stats | Reports largest tables, largest indexes, and text/json columns so performance work can be measured before and after changes |
| `scripts/db_table_retention_report.py` | read-only Postgres stats | Fast retention report for legacy NSE tables using indexed date bounds by default; exact counts are opt-in |
| `scripts/db_duplicate_index_report.py` | read-only Postgres stats | Finds exact duplicate indexes and emits reviewable `DROP INDEX CONCURRENTLY` candidates; does not drop anything |
| `scripts/drop_duplicate_indexes.py` | duplicate Postgres indexes | Guarded duplicate-index cleanup utility; dry-run by default and requires `--execute` to drop non-constraint duplicate indexes |
| `scripts/offload_announcement_text_to_s3.py` | `announcement_pipeline_documents`, `announcement_pipeline_reports` | Migrates heavy announcement OCR/transcript/report text to object storage while keeping S3 keys, hashes, counts, and excerpts in Postgres |
| `scripts/api_latency_probe.py` | operator API | Probes operator API endpoint latency and records slow endpoints through the deduped slow-operation log |
| `scripts/archive_legacy_nse_tables.py` | legacy NSE tables | Dry-run, S3 archive, and optional delete utility for old legacy NSE rows, chunked by month |

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

Performance inspection and text offload:

```sh
python scripts/db_size_report.py --limit 30
python scripts/db_table_retention_report.py --retention-days 365
python scripts/db_duplicate_index_report.py --limit 20
python scripts/drop_duplicate_indexes.py --limit 20 --min-mb 1
python scripts/api_latency_probe.py
python -m advisory.performance_slowlog report --limit 20
python scripts/offload_announcement_text_to_s3.py --dry-run --limit 100
python scripts/offload_announcement_text_to_s3.py --limit 500
```

Only run the duplicate-index cleanup with `--execute` after reviewing the dry-run output:

```sh
python scripts/drop_duplicate_indexes.py --limit 20 --min-mb 1 --execute
```

Slow-operation tracking writes:

- `logs/performance/slow_operations.jsonl`: append-only event log, one row per slow occurrence.
- `logs/performance/slow_operation_state.json`: deduped issue state keyed by fingerprint with first seen, last seen, count, max latency, and status.

Use `python -m advisory.performance_slowlog mark <fingerprint> triaged --note "..."` after adding a TODO or fix plan, so recurring slow events are counted but not treated as new work.

Legacy NSE retention workflow:

```sh
python scripts/db_table_retention_report.py --table nseindia_var1 --retention-days 365
python scripts/archive_legacy_nse_tables.py --table nseindia_var1 --retention-days 365 --max-chunks 3
python scripts/archive_legacy_nse_tables.py --table nseindia_var1 --retention-days 365 --archive-s3 --max-chunks 1 --execute
```

Only add `--delete` after validating the S3 archive. Deleting old rows does not immediately shrink the underlying Postgres files; schedule `VACUUM FULL` or `pg_repack` later if the goal is to return disk to the OS.

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

- Announcement document OCR, concise summaries, structured report parsing, and advisory event evaluation can run through Codex CLI instead of hosted ChatGPT/Gemini APIs. Set `OCR_USING=codex`, `SUMMARIZE_WITH=codex`, and `ADVISORY_EVENT_EVAL_MODEL=codex`; tune `CODEX_CLI_OCR_MODEL`, `CODEX_CLI_SUMMARIZE_MODEL`, `CODEX_CLI_EVENT_MODEL`, `CODEX_CLI_BIN`, and `CODEX_CLI_TIMEOUT_SECONDS` as needed.

- LLM/Codex prompt contracts are inventoried in `advisory.prompt_registry`. Use `python -m advisory.prompt_registry` or the Nuxt `/prompt-registry` page to review prompt ids, schema models, source files, model env vars, authority boundaries, output tables, and fallback behavior. This registry is audit metadata only; it does not execute prompts or grant trading authority.

- Core LLM/Codex output rows persist prompt-contract metadata so later audits can tell which prompt and response schema produced a row. Current coverage includes `advisory_event_evaluations.prompt_id/prompt_version/prompt_schema_version`, `advisory_event_policy_actions.llm_prompt_*`, `advisory_playbook_action_plans.prompt_*`, `advisory_company_memory_reviews.prompt_*`, `advisory_action_recommendations.manual_revision_prompt_*`, `advisory_technical_threshold_promotion_reviews.prompt_*`, `announcement_pipeline_documents` summary/OCR prompt metadata, and `announcement_pipeline_reports.prompt_*`.

- Runtime fallback/degraded-path events are persisted in `advisory_fallback_events`. Current emitters cover Redis fail-soft, Dhan identity fallback/unresolved identity, NSE retry/session reset, event-evaluation synthetic fallback, event-policy/manual-revision/playbook/company-memory deterministic fallbacks, and technical-threshold promotion fallback. Operator Health reads this table as `fallback_telemetry`, adds it to fix hints and the trust gate, and also mirrors recent rows into the Fallbacks & Degradation section.

- Consolidated action decisions can also get Codex-generated manual revision pointers. Set `ACTION_MANUAL_REVISION_POINTERS_ENABLED=true` and `ACTION_MANUAL_REVISION_POINTERS_MODEL=codex` or `codex:<model>`. The output is persisted on `advisory_action_recommendations` as `manual_revision_summary` and `manual_revision_pointers_json`; if Codex fails, deterministic fallback pointers are written instead.

- Final action rows also persist `recommendation_reason_json` and `reason_contract_status`. If a broker-action row is missing required reason, evidence, execution, or risk fields, action consolidation downgrades it to `MANUAL_REVIEW` before it can reach execution. The execution planner also blocks stale broker-action rows whose reason contract is missing or incomplete.
- Execution planning is action-contract first. If `advisory_action_recommendations` is empty, the planner returns no broker orders by default instead of falling back to raw portfolio/rebalance rows. Set `EXECUTION_ALLOW_LEGACY_PORTFOLIO_FALLBACK=true` only for legacy debugging.

- Action consolidation enriches reason contracts from latest `advisory_candidates` and `advisory_market_regime` snapshots before validation. This adds screener provenance, technical state/scores, setup score, candidate state, and macro/regime context without rerunning the full advisory pipeline.

- Event/playbook reason contracts are enriched from latest `advisory_event_evaluations`, `advisory_event_reviews`, and `advisory_playbook_action_plans`. This adds event ids/classes/verdicts/reviewer actions and matched playbook/review-check context before the final action row is accepted.

- `all_watchers.sh` runs `advisory.continuous_watch`, which now augments active watchlist symbols with a lower-priority top-50% market-context queue from `advisory_market_context_universe_daily`. Top-context news and announcements are persisted as `context_observed` unless deterministic materiality keywords mark them `triggered`; only `triggered` rows are routed for advisory refresh/evaluation. Tune breadth with `MARKET_CONTEXT_WATCH_LIMIT` (default `50`). The wrapper self-locks with `/tmp/stockey_watchers.lock`; skipped overlaps exit successfully and the next run resumes from `advisory_sync_state`. Watcher output is an intraday evidence/signal-refresh layer: fresh OHLCV/news/announcement evidence, material context triggers, and matched wait signals can write fast symbol-scoped rows, but final portfolio allocation, cross-sectional action reconciliation, and dry-run execution previews still require the full `all_advisory.sh` path.

- Investor playbooks live in `config/hypotheses.yaml`. Import them with `python -m advisory.hypothesis_engine --import-config config/hypotheses.yaml`; preview with `--dry-run`; run matching/action-plan generation with `python -m advisory.hypothesis_engine --run-scan`. Only `status: trusted_overlay` playbooks can affect consolidated actions, and only as `review_only` overlays.

- Dhan OHLCV auth falls back in this order:
  1. `DHAN_ACCESS_TOKEN`
  2. cached token at `.cache/dhan_access_token.json`
  3. API key consent flow using `DHAN_CLIENT_ID`, `DHAN_API_KEY`, `DHAN_API_SECRET`
- In the API key flow, the default path opens the Dhan consent page in the browser and waits for you to paste the redirected URL back into the terminal. The access token is then cached until expiry.
- Dhan browser automation uses the running Chrome CDP session plus `DHAN_LOGIN_MOBILE`, `DHAN_TOTP_SECRET`, and `DHAN_LOGIN_PIN`. When those values and `CDP_ENDPOINT` are configured, Dhan clients auto-refresh through Playwright after token cache expiry instead of asking for manual pasted consent. You can also force this path with `DHAN_AUTO_LOGIN_ENABLED=true`.

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

### Recurring cron scripts

| Script | Purpose | Scope |
| --- | --- | --- |
| `all_frontend.sh` | Operator frontend supervisor | Regular cron restarts/supervises `advisory.api.app` and the Nuxt operator app under a lock |
| `all_watchers.sh` | Continuous monitoring wrapper | Self-locking one-shot/loop wrapper that polls active watchlist OHLCV, announcements, and ET/news incrementally from persisted cursors |
| `all_downloaders_queue.sh` | Queued download ingestion | Enqueues single-client NSE/Dhan/Screener downloader work and runs safe non-queued downloader modules inline |
| `all_external_workers.sh` | External queue worker drain | Drains Dhan, Screener, and NSE queues serially under one lock |
| `complete_data.sh` | Broad ingestion safety net and pre-advisory catch-up | Runs all downloaders and parsers in order; scheduled before market and again before advisory, not in the watcher loop |
| `all_advisory.sh` | Advisory orchestrator | Runs post-close advisory pipeline and portfolio generation without raw downloads; defaults to bounded local parallel stages and skips hidden rule repair |
| `all_superseded_cleanup_audit.sh` | Superseded failure cleanup audit | Preview-only wrapper around `advisory.superseded_failures`; emits script markers and never passes `--apply` |
| `all_ml.sh` | Weekly research training | Long-running event-model research job; scheduled only in a dedicated weekly window if enabled |

Cron also runs selected Python modules directly for operator health, hypothesis scans, TS research refresh/evaluation, event-policy evaluation, technical-threshold calibration, and weekly event-model research training.

### Manual / catch-up / long-running scripts

| Script | Purpose | Scope |
| --- | --- | --- |
| `all_downloaders.sh` | Download-only ingestion | Manual broad catch-up for missing raw data |
| `all_parsers.sh` | Parse-only ingestion | Manual parser catch-up after raw files already exist |
| `complete_data.sh` | Combined ingestion and compact evidence refresh | Full download + parse catch-up/backfill plus `advisory.event_evidence_store`; useful end-of-day, after a missed day, or before a major rerun |
| `all_ml.sh` | Event-model training orchestrator | Long-running research training; use outputs only after validation/promotion checks |
| `all_advisory_codex.sh` | Codex-supervised advisory orchestrator | Manual debug/repair wrapper that runs `all_advisory.sh`, captures logs, sends failure lines to Codex CLI, and reruns |
| `all_analysis_codex.sh` | Codex analysis-development loop | Manual bounded loop that uses `analysis.md` and `docs/analysis_agent_board.md` to pick the next slice, implement it, validate it, and update docs |

The primary operator scripts call `scripts/run_with_markers.sh`, which emits `[stockey.script]` start/end markers to stdout while preserving the wrapped command's exit code. The health parser uses these markers to classify the latest run as `ok`, `failed`, `interrupted_by_operator`, `ok_after_historical_errors`, or `recovered_after_manual_interrupt`.

Recommended scheduler file:

- `config/stockey.crontab.template`
- `config/stockey.generated.crontab`

It schedules:

- `complete_data.sh` before market as a broad ingestion safety net and compact evidence refresh
- `complete_data.sh` again end-of-day before advisory to catch missed downloader/parser work and rebuild compact announcement/bhavcopy evidence
- `all_downloaders_queue.sh` plus `all_external_workers.sh` during the day for serialized single-client refreshes
- `all_watchers.sh` every `10` minutes during market hours; the script self-locks, skipped overlaps exit `0`, and watcher routing writes fast live rows to `advisory_signal_refresh_actions` while matching active hypothesis wait signals
- `all_advisory.sh` once daily after 7pm on weekdays, after `scripts/wait_for_locks.sh` confirms data catch-up and external worker locks are clear
- `all_frontend.sh` every `5` minutes under a lock so API/Nuxt are restarted if they exit
- `all_advisory.sh` and `all_watchers.sh` refresh `advisory.operator_snapshot` and `advisory.trace_summary_store` after a successful run so frontend endpoints can serve cached dashboard and trace sections quickly
- `all_advisory.sh` passes `--intraday-lookback-days ${ADVISORY_INTRADAY_LOOKBACK_DAYS:-30}`; intraday feature reads are session-scoped for the target date so advisory does not scan months of 1-minute candles on every run
- `advisory.operator_health --skip-dhan` at `08:05`, `12:05`, `17:05`, and `22:05` on weekdays
- `all_superseded_cleanup_audit.sh` at `17:35` on weekdays as a dry-run-only audit for recovered failure rows; apply mode is intentionally unscheduled
- `advisory.hypothesis_engine --run-scan` at `10:25`, `13:25`, `16:25`, and `21:25` on weekdays for investor playbook/hypothesis matching over newly collected events
- `advisory.ts_forecast_workflow` at `11:20`, `14:20`, `17:20`, and `20:20` on weekdays

Trace summary cache:

- Rebuild warm cache: `python -m advisory.trace_summary_store --symbol-limit 150 --event-limit 150 --trace-limit 100`
- Check cleanup candidates: `python -m advisory.trace_summary_store --cleanup --keep-latest-per-entity 1 --older-than-days 14 --dry-run`
- The Operations UI exposes these as safe audited commands; cache rebuild writes only `advisory_trace_summaries` and does not touch broker or recommendation state.
- `advisory.ts_forecast_evaluator` at `18:20` and `21:20` on weekdays
- `advisory.event_policy_evaluator` at `23:10` on weekdays
- `advisory.technical_threshold_calibration` at `04:20` on Saturdays
- `all_ml.sh` at `03:10` on Sundays for research-only event-model prep/training

Hypothesis scans default to Codex-backed action-plan notes. Set `HYPOTHESIS_CRON_ARGS=--no-llm` to keep that cron path deterministic only.

Not scheduled by default:

- `all_advisory_codex.sh`: self-fixing advisory wrapper; keep it manual so cron does not modify code unattended.
- `advisory.technical_threshold_promotion`: manual review/promotion helper; it creates patch guidance but should not run automatically.
- live Dhan order submission: controlled by execution settings and should remain explicitly gated; cron only prepares advisory/execution-planning state unless live trading is enabled deliberately.

Operator health and logs:

- `python -m advisory.operator_smoke` is the compact read-only preflight for DB/API/frontend/freshness/identity/signal-quality/cron trust. Use `python -m advisory.operator_health --skip-dhan` when you need the full detailed diagnostic payload.
- It also checks local operator API latency and Dhan cached-token expiry metadata without initiating broker login.
- `fix_hints` are emitted in the health payload and rendered at the top of the Nuxt Data Health page.
- The Data Health page has filters for `All`, `Errors`, `Warnings`, `Recovered`, and `OK`.
- Recovered manual interrupts are detected by comparing mapped output table timestamps against the interrupted log timestamp.

Bootstrap note:

- `python builder.py` creates `logs/cron`, installs `go-crond` locally as `./go-crond` unless `GO_CROND_INSTALL_DIR` overrides the target, and installs `torch` plus the current Google Research TimesFM package from GitHub for the `timesfm_2p5_200m` forecast adapter.
- The generated crontab includes a user field because it is meant for `go-crond --allow-unprivileged` or a system crontab style runner. Do not install it into a normal per-user `crontab` unless you first remove the user column.
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
python -m data.benchmark_sync --symbols NIFTY
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
python -m advisory.event_model_promotion_check
./all_advisory.sh
```

Behavior:

- runs `advisory.event_model_data_prep`
- checks whether the requested horizon is `train_ready`
- skips training cleanly if coverage is still insufficient
- trains `advisory.event_meta_model` only when the readiness gate passes
- uploads trained model artifacts to S3 after successful training unless `--skip-s3-upload` or `EVENT_MODEL_ARTIFACT_UPLOAD_ENABLED=false` is set
- scores current events after training unless `--skip-score` is used
- use `python -m advisory.event_model_promotion_check --format json` after weekly runs to see whether the evidence is ready for manual review

Event-model artifact upload:

```sh
python -m advisory.event_model_artifact_store --dry-run
python -m advisory.event_model_artifact_store --s3-prefix models/advisory_event_meta_model
./all_ml.sh --skip-s3-upload
```

The upload writes versioned keys and `latest` keys under `EVENT_MODEL_ARTIFACT_S3_PREFIX`, default `models/advisory_event_meta_model`. A failed upload fails the ML run so cron does not silently train a model that was not backed up.

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
- writes the advisory outputs consumed by the portfolio and operator frontend views
- `--fast` skips watch/news refresh, peer sync, and on-demand intraday repair; use it for quick portfolio/lifecycle/action refreshes when data is already current

Continuous-watch flow:

```sh
./all_watchers.sh --loop
./all_watchers.sh --loop --sleep-seconds 300
```

Behavior:

- keeps incremental source state in `advisory_sync_state`
- polls 1-minute intraday OHLCV for active advisory watchlist symbols and open positions
- polls announcements and ET/news on their own intervals
- self-locks so cron/manual overlap cannot create multiple watcher processes
- resumes from last successful source cursors; missed cron ticks are caught up by the next successful watcher run
- caps stale intraday catch-up with `WATCHER_OHLCV_MAX_LOOKBACK_MINUTES` so live watchers stay bounded; use `complete_data.sh` for broad repair
- routes new alerts and events into symbol-level advisory reevaluation
- writes live price alerts like `ENTRY_ZONE_HIT` and `INVALIDATION_HIT`
- records operator frontend status in `advisory_sync_state`; the Nuxt app reads current data from `advisory.api.app`
- publishes cycle summaries and alert payloads over Redis pub-sub

Notifier behavior:

- subscribes to `stockey:continuous_watch:*`
- converts bus messages into short human-readable operator lines
- writes legacy operator-feed files under `live_dashboard/` if you run it directly
- the Nuxt operator frontend should use API endpoints rather than the static feed files

Trace inspection:

```sh
python -m advisory.symbol_trace --symbol RELIANCE
python -m advisory.decision_trace --unique-id <event-id>
./all_frontend.sh
```

Use the Nuxt Decision Trace page for readable stage cards. The raw CLI commands are useful when debugging DB rows or API responses.

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
- `complete_data.sh` is the lower-level ingestion component used before advisory runs. It now also builds `advisory_bhavcopy_evidence_daily` and `advisory_announcement_evidence` so UI/LLM/advisory paths can read compact evidence instead of scanning raw bhavcopy or announcement text tables.
- `advisory.llm_event_evaluator` uses `advisory_announcement_evidence` first for official filing context and falls back to `announcement_pipeline_documents` only for missing compact rows. Its exchange context also includes the latest point-in-time `advisory_bhavcopy_evidence_daily` row for the symbol.
- Repair legacy exchange-event typing after old parser bugs with `python -m advisory.exchange_events --repair-missing-types --dry-run`, then `python -m advisory.exchange_events --repair-missing-types` after reviewing the matched row counts. The repair is explicit and classifies known legacy deals, short-selling rows, and otherwise unclassified rows rather than leaving `event_source` or `event_type` null.
- Sparse NSE event crawlers for corporate actions, earnings events, and insider deals write `advisory_sync_state` rows after each run. `advisory.event_data_quality` uses these rows to distinguish “source checked recently but no new event rows existed” from genuinely stale ingestion.

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
python -m advisory.technical_threshold_calibration --dry-run --from-date 2026-01-01 --to-date 2026-05-01 --horizons 5 10 20
python -m advisory.technical_threshold_promotion --setup-id EVENT_OPPORTUNITY_V1 --config-id CONFIG_ID --dry-run
python -m advisory.config_change_assistant --source-type technical_threshold --setup-id EVENT_OPPORTUNITY_V1 --config-id CONFIG_ID --dry-run
python -m advisory.event_policy --dry-run
python -m advisory.event_policy --dry-run --no-llm
python -m advisory.signal_quality_evaluator --dry-run --from-date 2026-01-01 --to-date 2026-05-01 --horizons 5 10 20
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

`advisory.llm_event_evaluator` reads both `advisory_watch_events` and `advisory_news_events`, joins `advisory_announcement_evidence` when official filings exist, and explicitly falls back to `announcement_pipeline_documents` only when compact evidence has not been built for that `unique_id`. It adds point-in-time regime, technical, fundamentals, exchange-feature, and compact bhavcopy evidence context before writing `advisory_event_evaluations` and `advisory_event_risks`.

`advisory.company_memory_review` writes `advisory_company_memory_reviews` as a review-only company-memory input. It reads compact evidence plus latest technical, event-policy, wait-signal, and action rows for a bounded symbol set. It defaults to deterministic V1 and only uses Codex when `--llm` or `COMPANY_MEMORY_REVIEW_LLM_ENABLED=true` is set. These rows do not grant execution authority.

```sh
python -m advisory.company_memory_review --dry-run --symbols RELIANCE --limit 1
python -m advisory.company_memory_review --dry-run --limit 12
python -m advisory.company_memory_review --dry-run --symbols RELIANCE --llm
```

The operator API attaches the latest company-memory review to matching action rows, including compact `/api/actions?compact=true` responses. The Nuxt Action Queue and Symbol Detail pages show this as read-only company-memory evidence.

`advisory.signal_quality_evaluator` is the research comparator for deciding whether non-price evidence is helping. It starts from `advisory_candidates`, joins only point-in-time rows from event policy, compact bhavcopy evidence, and company-memory review tables, attaches future Dhan OHLCV returns, and writes variant-level results for `technical_only`, `technical_plus_event`, `technical_plus_bhavcopy`, `technical_plus_company_memory`, and `technical_plus_all`. It does not change live action policy or broker execution.

```sh
python -m advisory.signal_quality_evaluator --dry-run --from-date 2026-01-01 --to-date 2026-05-01 --horizons 5 10 20
python -m advisory.signal_quality_evaluator --from-date 2026-01-01 --to-date 2026-05-01 --horizons 5 10 20
```

The operator API exposes the latest persisted signal-quality run at `/api/signal-quality`. The Nuxt operator app shows it at `/signal-quality`, including overlay coverage, lift versus `technical_only`, and selected matured examples. Treat this as manual review evidence only; production action rules are unchanged until a separate explicit config/rule change is approved.

Use `advisory.signal_quality_promotion` only after the evaluator has enough matured rows. It writes manual review/decision audit rows and copyable patch guidance; it does not edit config, action rules, portfolio rows, or broker behavior.

```sh
python -m advisory.signal_quality_promotion --evaluated-at 2026-05-01T00:00:00Z --horizon-days 5 --variant technical_plus_all --dry-run
```

The operator API exposes this workflow at `/api/signal-quality/promotion-review`, `/api/signal-quality/promotion-reviews`, and `/api/signal-quality/promotion-review/decision`. The Nuxt `/signal-quality` page can create a review, show the Advisory Trust Gate context, record an approve/reject/needs-more-data decision, and copy patch guidance for a separate reviewed code/config change.

`advisory.config_change_assistant` turns approved review decisions into reviewed unified diffs. It is intentionally one step short of applying the change: the generated diff is an audit artifact and copyable operator aid only.

```sh
python -m advisory.config_change_assistant --source-type technical_threshold --setup-id EVENT_OPPORTUNITY_V1 --config-id CONFIG_ID --dry-run
python -m advisory.config_change_assistant --source-type signal_quality_overlay --evaluated-at 2026-05-01T00:00:00Z --horizon-days 5 --variant technical_plus_all --dry-run
```

The operator API exposes recent previews at `/api/config-change/previews` and diff generation at `/api/config-change/technical-threshold-preview` and `/api/config-change/signal-quality-preview`. The Nuxt Technical Calibration and Signal Quality pages show `Reviewed Diff` actions for approved decisions.

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

For long unattended advisory runs where Codex CLI should inspect failures and attempt a bounded fix/rerun cycle, use:

```sh
./all_advisory_codex.sh
```

It writes command logs, Codex prompts, and Codex outputs to `logs/codex_supervisor/`. Defaults:

- `CODEX_SUPERVISOR_MAX_ATTEMPTS=3`
- `CODEX_SUPERVISOR_TAIL_LINES=100`
- `CODEX_SUPERVISOR_TIMEOUT_SECONDS=1800`
- `CODEX_SUPERVISOR_SANDBOX=danger-full-access`
- `CODEX_SUPERVISOR_MODEL` or `CODEX_CLI_MODEL` controls the Codex model

Use this only when you are comfortable with Codex making code changes. The supervisor tells Codex not to rerun the long command itself; it reruns the command after Codex exits.

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
