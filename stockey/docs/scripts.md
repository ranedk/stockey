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
| `data/dhanlive/scrip_master.py` | `master_dhan_instruments` | Versioned Dhan instrument master; base table/index schema is registry-managed by `20260611_dhan_scrip_master_base`, while new broker columns are still added dynamically after CSV inspection |
| `data/dhanlive/auth_cli.py` | none | Dhan token status, refresh, validate, and cache-clear helper |
| `data/dhanlive/ohlcv.py` | `dhan_ohlcv_daily`, `dhan_ohlcv_intraday` | Dhan OHLCV for `stock`, `index`, and `benchmark`; default sync resumes from the latest stored candle with overlap, and backfills only when no local data exists |
| `data/dhanlive/ohlcv_pull.py` | none | Quick operator OHLCV pull utility; defaults to NSE equity, 5-minute candles, and the last 60 minutes |
| `advisory/intraday_features.py` | `advisory_intraday_features_daily` | On-demand advisory intraday feature builder; pulls missing intraday candles for the active screener universe and persists daily intraday pattern features |
| `data/screenerin/auth.py` | Browser session | Ensures Screener.in login through the running Chrome CDP session using `SCREENER_IN_LOGIN` and `SCREENER_IN_PASSWORD` |
| `data/screenerin/screener_parser.py` | `screenerin_screener_snapshots` | Stores parsed Screener.in screener snapshots by screener slug and date |
| `data/screenerin/screener_registry.py` | `screenerin_screeners` | Registry utility to add/list/remove Screener.in screeners and inspect latest stored snapshots |
| `data/screenerin/ad_hoc_query.py` | `screenerin_ad_hoc_query_runs`, `screenerin_ad_hoc_query_results` | Authenticated ad hoc Screener.in raw query runner; auto-logins through CDP when needed and stores parsed company rows plus queried metrics |
| `data/screenerin/query_validation.py` | none | Local Screener.in query validator for known bad syntax before browser/network execution |
| `advisory/research_ledger.py` | `advisory_research_runs` | Research ledger for recording experiment configs, point-in-time context, validation protocol, and run outcomes |
| `advisory/training_universe.py` | `advisory_screener_constituents` | Sync broad ad hoc Screener.in training universes directly into normalized advisory screener rows for research-only event-model coverage |
| `advisory/event_meta_model.py` | `advisory_event_model_scores` | Train/score scaffold for XGBoost event meta-models using structured event tensors, anchor-day intraday response features, and future daily returns; score schema setup is registry-managed by `20260611_advisory_event_meta_model_scores_base` |
| `advisory/regime_engine.py` | `advisory_market_regime` | Builds base market regime snapshots from benchmark, volatility, macro stress, and freshness inputs; schema setup is registry-managed by `20260611_advisory_market_regime_base` |
| `advisory/news_overlay_engine.py` | `advisory_market_overlay_daily` | Builds a lightweight market news overlay over base regime state; overlay schema is registry-managed by `20260611_advisory_market_overlay_daily_base` |
| `advisory/news_theme_engine.py` | `advisory_news_theme_screeners` | Classifies market news into configured investment themes and maps active themes to Screener.in screeners; mapping schema is registry-managed by `20260611_advisory_news_theme_screeners_base` |
| `advisory/rule_engine.py` | `advisory_candidates`, `advisory_candidate_rejections` | Scores setup candidates into pass/watch/reject states with technical, fundamental, intraday, bounded regime-fit, and screener context; missing market-regime snapshots default to auditable `regime_name=UNKNOWN` instead of zero candidates unless `RULE_ENGINE_REQUIRE_REGIME_CONTEXT=true`; setup blocked-regime and blocked-overlay labels default to context-only warnings unless their hard-block env flags are enabled; aggregate setup score can create watch pressure, but `technical_state=IGNORE` can no longer become `PASS_NOW` without a confirmed technical trigger; `soft_failures_json` records surviving non-fatal rule/context warnings, and `regime_fit_weight_effective` records the actual single-regime score weight used; action reason contracts expose the warnings as typed `soft_failures` plus the effective regime weight; output schemas are registry-managed by `20260611_advisory_rule_outputs_base` |
| `advisory/ts_forecast_features.py` | `advisory_ts_forecasts_daily` | Experimental OHLCV time-series forecast features; starts with `naive_momentum_v1` and is designed to host TimesFM / Chronos / Moirai adapters later |
| `advisory/ts_forecast_evaluator.py` | `advisory_ts_forecast_evaluations`, `advisory_ts_forecast_eval_summary` | Evaluates matured TS forecast rows against future Dhan OHLCV returns after costs using first close strictly after forecast as-of date |
| `advisory/ts_forecast_paper_portfolio.py` | `advisory_ts_forecast_paper_portfolio` | Builds research-only forecast paper decisions and compares them with naive momentum and current advisory alignment |
| `advisory/ts_forecast_promotion_check.py` | read-only checks | Conservative promotion gate for deciding whether TS forecast paper evidence is ready for manual operator review as a low-weight input |
| `advisory/ts_forecast_workflow.py` | `advisory_ts_forecasts_daily`, `advisory_ts_forecast_watchlist` | Optional Screener.in -> Dhan OHLCV refresh -> TimesFM forecast -> experimental TS watchlist workflow |
| `advisory/technical_threshold_calibration.py` | `advisory_technical_threshold_evaluations`, `advisory_technical_threshold_eval_summary` | Research-only calibration of technical-engine thresholds against realized forward Dhan OHLCV returns after costs; summary rows include setup-archetype outcome breakdowns and cannot change live policy |
| `advisory/technical_threshold_promotion.py` | `advisory_technical_threshold_promotion_reviews`, `advisory_technical_threshold_promotion_decisions` | LLM-assisted manual review of calibrated technical thresholds plus operator approval/rejection audit rows; includes setup-archetype outcome evidence and produces patch guidance without applying config changes |
| `advisory/technical_engine.py` | consumed by `advisory_candidates` | Dhan OHLCV technical scoring for trend, structure, participation, relative strength, tradability, entry triggers, lifecycle states, and explain-only setup archetype/quality contracts |
| `advisory/config_change_assistant.py` | `advisory_config_change_previews` | Generates reviewed unified diffs from approved technical-threshold or signal-quality promotion decisions; preview/audit only, never applies config changes |
| `advisory/prompt_registry.py` | read-only metadata | Central inventory of LLM/Codex prompt contracts, schemas, model env vars, source files, authority scope, fallbacks, and migration status |
| `advisory/llm_provenance_audit.py` | read-only checks | Audits core LLM/Codex-derived signal tables for prompt id/version, schema version, model, evidence, and forbidden authority metadata such as broker execution, auto-promotion, config mutation, or portfolio mutation; reports missing tables/columns/rows without changing policy or broker behavior |
| `advisory/event_policy.py` | `advisory_event_policy_actions` | Deterministic and bounded Codex-assisted mapping from structured event evaluations to buy-watch, manual review, reduce-exposure review, or no action, including actionability context, calibrated source quality for filings/news/OCR evidence, affected sectors/peers, point-in-time Dhan daily price reaction/latest portfolio exposure when available, and operator notes for actionable manual reviews; `--repair-llm-provenance --dry-run` previews metadata-only repair for legacy LLM fallback/skipped rows missing prompt contract fields; base schema is registry-managed by `20260611_advisory_event_policy_actions_base`, and actionability is added by `20260611_advisory_event_policy_actionability` |
| `advisory/event_policy_evaluator.py` | `advisory_event_policy_evaluations`, `advisory_event_policy_eval_summary` | Research-only evaluation of event-policy actions/classes, source-quality buckets, and market-scope buckets against realized forward Dhan OHLCV returns after costs, with benchmark-forward and benchmark-excess attribution |
| `advisory/event_policy_promotion.py` | `advisory_event_policy_promotion_reviews`, `advisory_event_policy_promotion_decisions` | Manual review of event-policy evaluation groups plus operator approval/rejection audit rows; produces review-only event-policy rule guidance only when absolute and benchmark-excess evidence pass, without changing live policy |
| `advisory/company_memory_review.py` | `advisory_company_memory_reviews` | Review-only company-memory signal summaries from taxonomy-gated compact announcement evidence, bhavcopy evidence, technical state, event policy, wait signals, and latest action rows; deterministic by default, optional Codex; schema setup is registry-managed by `20260611_advisory_company_memory_reviews_base` |
| `advisory/signal_quality_evaluator.py` | `advisory_signal_quality_evaluations`, `advisory_signal_quality_eval_summary` | Research-only comparison of technical-only signals versus technical signals enriched with event-policy, bhavcopy, company-memory, context-overlay evidence, and trusted context-rule action adjustments after costs |
| `advisory/research_evidence_runner.py` | research evaluator output tables | Research-only runner for refreshing signal-quality, narrowed split, fast context, adversarial-review, action-transition, causal-memory, and provenance evidence without event-model prep/training/scoring, portfolio mutation, config mutation, or broker execution; manual review row creation is disabled by default, the readiness summary fails closed if any child payload reports broker/config/policy/portfolio authority, and Operator Health surfaces those authority violations from `research_evidence.log` |
| `advisory/signal_quality_window_runner.py` | `advisory_signal_quality_evaluations`, `advisory_signal_quality_eval_summary`, optional `advisory_signal_quality_promotion_reviews` | Research-only multi-window runner for signal-quality evaluation, source-family reports, benchmark-attributed stability gates, split diagnostics, and optional gated promotion-review row creation |
| `advisory/signal_quality_split_report.py` | `advisory_signal_quality_evaluations` | Read-only diagnostics that split noisy context source families by context class/direction so inconsistent families can be researched without relying on one global regime label; fast context watch/de-risk evaluators also persist `context_class` for source-family outcome audits, and context-overlay reliability reports include per-family class diagnostics |
| `advisory/signal_quality_split_evaluator.py` | `advisory_signal_quality_split_evaluations`, `advisory_signal_quality_split_eval_summary`, `advisory_signal_quality_split_promotion_reviews`, `advisory_signal_quality_split_promotion_decisions` | Research-only narrowed split evaluation that consumes split-queue specs, compares each split subset with matching technical-only rows, classifies split stability, and can generate manual review rows for stable narrowed split candidates without policy/broker authority |
| `advisory/signal_quality_promotion.py` | `advisory_signal_quality_promotion_reviews`, `advisory_signal_quality_promotion_decisions` | Manual review of signal-quality overlay lift plus operator approval/rejection audit rows; context-overlay variants must pass selected/matured/breadth, source-family, fast reliability, narrowed-split stability, and complete technical-baseline gates; produces review-only overlay rule guidance without changing live policy |
| `advisory/negative_pressure_evaluator.py` | `advisory_negative_pressure_evaluations`, `advisory_negative_pressure_eval_summary` | Research-only realized outcome evaluator for `REDUCE_EXPOSURE_REVIEW` rows from signal refresh; checks whether review-only negative overlay pressure would have protected capital after costs, split by source family and compact context class |
| `advisory/exchange_events.py` | `advisory_exchange_events` | Normalizes NSE block/bulk/short-selling/insider/corporate-action/earnings rows into point-in-time exchange events |
| `advisory/exchange_context_overlays.py` | `advisory_exchange_context_overlays` | Converts clear exchange-event evidence into review-only positive/negative/watch pressure with `authority_scope=watchlist_pressure_only` |
| `advisory/exchange_features.py` | `advisory_exchange_features_daily` | Builds daily symbol-level exchange-event features for LLM context, event-model features, review, and risk sizing |
| `advisory/event_data_quality.py` | read-only checks | Reports announcement and NSE/bhavcopy evidence readiness: full CLI deep diagnostics cover source freshness, sparse-source poll state for corporate actions/earnings/insider rows, parse/OCR/text coverage, compact announcement taxonomy readiness versus archive-only rows, compact bhavcopy breadth/directional coverage, exchange-event typing, exchange-feature coverage, and raw-table scan risk. Raw-table risk is informational when fresh compact caches exist, and only warns when the compact replacement is missing or stale; Operator Health uses the compact timestamp-only health summary |
| `advisory/feature_freshness.py` | read-only checks | Classifies per-symbol advisory inputs as fresh, stale, missing, error, or intentionally skipped; powers Action Queue data-input summaries and Symbol Detail freshness contracts |
| `advisory/macro_context_overlays.py` | `advisory_macro_context_overlays` | Converts macro feature shocks into review-only sector pressure such as crude, rupee, yield, food-inflation, and broad-risk stress overlays |
| `advisory/event_evidence_store.py` | `advisory_bhavcopy_evidence_daily`, `advisory_bhavcopy_context_overlays`, `advisory_announcement_evidence`, `advisory_announcement_context_overlays` | Builds compact query-friendly evidence stores from NSE bhavcopy/deals/short/circuit/volatility/margin rows, review-only bhavcopy context overlays, exchange announcements plus latest event evaluation fields, announcement storage/LLM-readiness taxonomy, and review-only announcement context overlays |
| `advisory/context_overlay_refresh.py` | `advisory_news_theme_context_overlays`, `advisory_macro_context_overlays`, `advisory_exchange_context_overlays`, `advisory_bhavcopy_context_overlays`, `advisory_announcement_context_overlays` | Refreshes all review-only context overlays from already-ingested data without running full advisory, portfolio, or broker paths |
| `all_context_to_entry_repair.sh` | review-only context signal rows, `advisory_watchlist`, `advisory_technical_daily`, `advisory_candidates`, `advisory_action_recommendations` | Bounded post-close repair chain for positive context that has not reached BUY: context signal refresh, `CONTEXT_OVERLAY_WATCH` reconcile, targeted context-watch technical refresh, rules-through-actions pipeline, Action Queue refresh, and recommendation diagnostics; no broker execution |
| `advisory/market_context.py` | `advisory_market_context_universe_daily`, `advisory_market_context_summary_daily` | Builds the top-50% market-context universe from technical/liquidity/market-cap data and summarizes breadth, leadership, sector clusters, events, and regime context |
| `advisory/operator_health.py` | read-only checks | Operator smoke-test command for DB, Redis, Dhan token, cron logs, timestamp-only key table freshness, identity issues, context/regime gate policy, watcher source counters, review-only signal-refresh source state for context overlays / causal memory / bounded action refresh, signal-quality/context-overlay reliability evidence, macro/theme sector-alias coverage, trusted signal-quality overlay-rule eligibility, frontend dependencies, Poppler, Codex, and TimesFM; full mode runs independent sections through a configurable worker pool and process-isolated per-section timeout, and `--check <section_name>` runs one named read-only section for targeted debugging; uses sampled persisted decision-time feature-freshness snapshots for feature-stage gate Health instead of live OHLCV/technical recomputation, and uses local/db-retry fallback telemetry spools instead of grouped fallback-table scans during routine Health; emits `fix_hints` plus an Advisory Trust Gate for the Nuxt health page |
| `advisory/identity_issues.py` | read-only by default; optional repair apply | Rechecks open Dhan/security identity issues after scrip/company master refresh and open Dhan OHLCV history-unavailable issues after price history appears; use `--apply` only after dry-run rows show `would_resolve` |
| `advisory/cron_status.py` | read-only checks | Parses `config/stockey.generated.crontab`, joins scheduled jobs to lock directories and cron log tails, and powers the Operations scheduled-jobs card |
| `advisory/operator_smoke.py` | read-only checks | Compact one-command operator preflight over Health that returns status, trust level, fix hints, current blockers, and next commands; also available as an audited Operations UI command |
| `scripts/cron_preflight.py` | read-only checks | Validates generated go-crond setup before startup: required env, referenced scripts, stale locks, Python resolution, and operator API/web port availability; also available as the audited `cron_preflight` Operations command |
| `scripts/context_gate_policy_audit.py` | read-only checks | Audits current env policy for broad single-regime hard gates and other hard context gates across rule-engine, action-consolidation, and hypothesis/playbook planning; use `--fail-on-single-regime` in QA/cron checks to catch accidental reintroduction of one global regime as a BUY/watch/wait-signal blocker |
| `scripts/docs_state_audit.py` | read-only docs checks | Audits README, roadmap, analysis, and docs for stale operator terminology plus required current-state coverage |
| `utils/schema_migrations.py` | `stockey_schema_migrations` | Schema migration registry with checksum-protected apply, durable failure recording, dry-run, list, and ensure-table modes |
| `advisory/superseded_failures.py` | `advisory_event_processing_runs`, `announcement_pipeline_documents` | Previews recovered failure rows that can be marked superseded; default is dry-run JSON, the Health UI has a guarded audited apply path, and CLI `--apply` is the manual fallback requiring explicit operator intent before writing superseded metadata |
| `advisory/event_model_data_prep.py` | varies | One-shot prep flow for event-model training: normalizes missing screener constituents, backfills historical event evaluations, refreshes price history, and reports label coverage |
| `advisory/model_training_runner.py` | varies | Gated model-training orchestrator: runs prep, checks label coverage for the requested horizon, then trains and scores only when ready |
| `advisory/event_model_artifact_store.py` | S3/object store | Uploads trained event-model JSON, metadata JSON, and manifest JSON to versioned and `latest` S3 prefixes |
| `advisory/event_model_promotion_check.py` | read-only checks | Conservative evidence gate for deciding whether weekly event-model results are ready for manual operator review as a low-weight input |
| `advisory/sync_state.py` | `advisory_sync_state` | Shared incremental state storage for continuous polling and operator frontend status |
| `advisory/continuous_watch.py` | `advisory_live_watch_alerts`, `advisory_sync_state` | Lightweight watch loop over active watchlist OHLCV, announcements, ET/news, operator status, context-overlay signal refresh, causal-memory signal refresh, router signal refresh, and bounded review-only Action Queue refresh for affected symbols |
| `advisory/event_router.py` | `advisory_live_router_actions`, `advisory_sync_state` | Symbol-level router that turns fresh live alerts and events into fast signal-refresh intents, exposes all affected symbols for audit, and exposes only successfully executed/refreshed symbols for downstream review-only Action Queue refresh |
| `advisory/signal_refresh.py` | `advisory_signal_refresh_actions`, `advisory_decision_traces` | Fast per-symbol live decision layer that reads latest action, lifecycle/rebalance, event-policy rows, review-only context-overlay watch discovery, causal-memory watch/de-risk pressure from exact candidate-helpful groups with positive benchmark-excess evidence, and review-only de-risk pressure without running full portfolio allocation; action consolidation can now consume recent rows as review-only `WATCH`, `REDUCE_EXPOSURE_REVIEW`, `TIGHTEN_STOP`, or `MANUAL_REVIEW` candidates |
| `advisory/wait_signals.py` | `advisory_wait_signals`, `advisory_wait_signal_matches` | Converts hypothesis/playbook action plans into machine-checkable price/news/announcement wait conditions and matches them deterministically |
| `advisory/decision_trace.py` | `advisory_decision_traces`, `advisory_decision_trace_steps`, `advisory_event_processing_runs`, `advisory_action_conflicts`, `advisory_action_conflict_rules` | Durable trace layer that links ingest, event evaluation, review, lifecycle, action consolidation, conflict rows, and seeded conflict-resolution rules |
| `advisory/action_conflict_resolver.py` | `advisory_action_conflicts`, `advisory_action_conflict_rules` | Re-applies enabled conflict-resolution rules to latest or historical action conflicts after rules are edited; dedupes duplicate conflict rows, keeps resolved conflicts as audit-only trace data, and marks only unresolved/manual-required conflicts for manual handling |
| `advisory/manual_review_state.py` | `advisory_manual_review_decisions`, `advisory_wait_signals` | Central state/effect contract for operator Manual Review decisions; defines closing versus annotating decisions, no-trade safety flags, and wait-signal side effects |
| `advisory/trace_summary_store.py` | `advisory_trace_summaries` | Materializes compact symbol/event trace summaries so the operator frontend does not rebuild large traces live on every page load; schema setup is registry-managed by `20260611_advisory_trace_summaries_base` |
| `advisory/live_dashboard.py` | JSON payload builder | Legacy static dashboard module; API still reuses its payload builder while Nuxt replaces static generation |
| `advisory/operator_snapshot.py` | `advisory_operator_snapshots` | Builds the compact DB-backed operator dashboard snapshot used by the API to avoid rebuilding the full dashboard payload on every frontend request |
| `advisory/performance_slowlog.py` | files under `logs/performance/` | Deduped slow-operation logger and state manager for API latency, snapshot builds, and future slow pipeline sections |
| `advisory/api/app.py` | operator HTTP API | Serves operator-facing JSON endpoints, normalized trace summaries, hypothesis write/audit endpoints, and technical-calibration review endpoints for the Nuxt app |
| `advisory/external_task_queue.py` | `advisory_external_task_queue` | Serialized queue foundation for single-client NSE/Dhan/Screener work so browser/API-heavy jobs do not run concurrently inside advisory |
| `scripts/wait_for_locks.sh` | none | Shell guard used by cron so advisory waits for data catch-up and external queue locks before reconciling actions |
| `advisory/live_notifier.py` | legacy files in `live_dashboard/` | Subscribes to Redis pub-sub from the continuous-watch stack and writes a legacy human-readable operator feed if run directly |
| `advisory/adversarial_review.py` | `advisory_event_reviews` | Deterministic reviewer over structured event tensors; can clear, penalize, force manual review, or veto event-driven allocations |
| `advisory/adversarial_review_evaluator.py` | `advisory_adversarial_review_evaluations`, `advisory_adversarial_review_eval_summary` | Research-only evaluator for adversarial review actions; measures raw and benchmark-excess avoided loss plus false-positive rate for veto/penalize/manual-review rows using point-in-time Dhan OHLCV and benchmark returns after costs |
| `utils/ocr` | none | Provider-agnostic PDF OCR utility using Gemini 3 Flash preview and OpenAI GPT-5 nano |
| `utils/transcribe` | none | Audio transcription utility for remote mp3/wav/mp4 links using Gemini 3 Flash preview and OpenAI transcription APIs |
| `data/nseindia/bhavcopy_parser.py` | `nseindia_*` daily tables | Parses downloaded NSE archives for the legacy/reference NSE pipeline; empty valid archives are marked `empty_valid_source`, and failed rows classify as `bad_file_retryable`, `schema_changed`, or `parser_bug` in runner state |
| `data/nseindia/adjusted_prices.py` | `nseindia_corporate_actions_normalized`, `nseindia_ohlcv_adjusted` | Builds split/bonus-adjusted OHLCV for the legacy/reference NSE pipeline |
| `data/nseindia/security_history.py` | `dim_security_history`, `dim_security_review_events`, `dim_security_overrides` | Builds canonical security identity history and review queue for renames / identity breaks; schema setup is registry-managed by `20260611_nse_security_history_base` |
| `data/nseindia/security_dimension.py` | `dim_security` | Current canonical security dimension keyed by `security_id` |
| `data/nseindia/indices_parser.py` | `nseindia_indices` | Index history; empty/no-row archives are marked `empty_valid_source`, and failed rows classify as `bad_file_retryable`, `schema_changed`, or `parser_bug` in runner state |
| `data/benchmark_sync.py` | `dhan_ohlcv_daily` | Syncs canonical advisory benchmark rows such as `NIFTY` from parsed NSE index history so benchmark features do not depend on stale Dhan index candles |
| `data/nseindia/corporate_actions.py` | `nseindia_corporate_actions` | Corporate actions |
| `data/nseindia/earnings_events.py` | `nseindia_earnings_events` | Earnings calendar |
| `data/nseindia/insider_deals.py` | `nseindia_insider_deals` | Insider deals |
| `data/nseindia/offmarket_parser.py` | `nseindia_block_deals`, `nseindia_bulk_deals`, `nseindia_short_selling` | Off-market parsers; valid zero-row files are marked `empty_valid_source`, and failed rows classify as `schema_changed` or `parser_bug` in runner state |
| `data/nseindia/recent_events.py` | `nseindia_events` | NSE event feed |
| `data/nseindia/holidays.py` | `nseindia_holidays` | Trading holidays |
| `data/announcements/cli.py` | `announcement_pipeline_documents`, `announcement_pipeline_reports` | Exchange announcement ingest keyed by `company_master_id` |
| `data/backfill_company_master_ids.py` | many existing symbol-based tables | Adds and backfills `company_master_id` on historical rows |
| `scripts/cleanup_deprecated_tables.py` | none | Drops deprecated tables that are no longer used by the active advisory stack |
| `scripts/ingestion_state_runner.py` | `ingestion_file_state` | Inspect and summarize failed/processed/empty-valid ingestion file state, including parser failure classifications, and clear specific failed keys before retry; schema setup is registry-managed by `20260611_ingestion_file_state_base` |
| `scripts/db_size_report.py` | read-only Postgres stats | Reports largest tables, largest indexes, and text/json columns so performance work can be measured before and after changes |
| `scripts/heavy_payload_inventory.py` | read-only Postgres catalog stats | Classifies non-announcement text/json/blob-like columns by risk and recommended action so offload/retention work can be targeted |
| `scripts/db_table_retention_report.py` | read-only Postgres stats | Fast retention report for legacy NSE tables using indexed date bounds by default; exact counts are opt-in |
| `scripts/hot_table_retention.py` | hot trace/intraday tables | Report-first hot/cold retention utility for raw decision traces, trace steps, event-processing runs, action conflicts, trace-summary cache, Dhan intraday candles, and daily intraday feature rows; dry-run by default, S3 archive/delete are explicit |
| `scripts/db_duplicate_index_report.py` | read-only Postgres stats | Finds exact duplicate indexes and emits reviewable `DROP INDEX CONCURRENTLY` candidates; does not drop anything |
| `scripts/drop_duplicate_indexes.py` | duplicate Postgres indexes | Guarded duplicate-index cleanup utility; dry-run by default and requires `--execute` to drop non-constraint duplicate indexes |
| `scripts/offload_announcement_text_to_s3.py` | `announcement_pipeline_documents`, `announcement_pipeline_reports` | Migrates heavy announcement OCR/transcript/report text to object storage while keeping S3 keys, hashes, counts, excerpts, and optional JSON manifests in Postgres/operator logs |
| `scripts/validate_announcement_s3_pointers.py` | `announcement_pipeline_documents`, `announcement_pipeline_reports` | Read-only validator for offloaded announcement S3 pointers; supports dry-run, HEAD/size checks, and optional SHA-256 verification |
| `scripts/api_latency_probe.py` | operator API and `logs/performance/latest_api_latency_probe.json` | Probes operator API endpoint latency, writes the latest summary for Operator Health, and records slow endpoints through the deduped slow-operation log |
| `scripts/api_performance_report.py` | `logs/performance/latest_api_latency_probe.json`, `logs/performance/slow_operation_state.json` | Ranks slow/error/large operator API routes and emits endpoint-specific optimization guidance before adding indexes or changing payloads; fresh probe rows are ranked ahead of unprobed historical slowlog rows |
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
python scripts/ingestion_state_runner.py summary --status failed --limit 1000
python scripts/ingestion_state_runner.py summary --source bhavcopy --status empty_valid_source
python scripts/ingestion_state_runner.py list --status failed --limit 50
python scripts/ingestion_state_runner.py list --source bhavcopy --status failed
python scripts/ingestion_state_runner.py clear --source bhavcopy --key bhavcopy/bhavcopy_2015-01-16.zip
```

The same summary is exposed read-only in the operator API at `/api/operations/ingestion-state` and on the Nuxt Health page under File-Level Ingestion State. Failed file rows are split into active and stale historical lifecycle buckets. Only failures inside `OPERATOR_HEALTH_INGESTION_FAILURE_ACTIVE_DAYS` are treated as current Health blockers; older historical failures remain visible for audit and manual cleanup without polluting active operator review.

Performance inspection and text offload:

```sh
python scripts/db_size_report.py --limit 30
python scripts/heavy_payload_inventory.py --min-risk-score 1 --format json
python scripts/db_table_retention_report.py --retention-days 365
python scripts/hot_table_retention.py --format json
python scripts/hot_table_retention.py --group intraday --exact-counts --format json
python scripts/db_duplicate_index_report.py --limit 20
python scripts/drop_duplicate_indexes.py --limit 20 --min-mb 1
python scripts/api_latency_probe.py --output-path logs/performance/latest_api_latency_probe.json
python scripts/api_performance_report.py --limit 20
python scripts/docs_state_audit.py --strict
python -m advisory.performance_slowlog report --limit 20
python -m utils.schema_migrations --ensure-table
python -m utils.schema_migrations --list --limit 20
python scripts/offload_announcement_text_to_s3.py --dry-run --limit 100 --manifest-path logs/performance/announcement_text_offload_dry_run.json
python scripts/offload_announcement_text_to_s3.py --limit 500 --manifest-path logs/performance/announcement_text_offload_apply.json
python scripts/validate_announcement_s3_pointers.py --dry-run --limit 100
python scripts/validate_announcement_s3_pointers.py --limit 100
python scripts/validate_announcement_s3_pointers.py --limit 25 --verify-hash
```

Use `python -m utils.schema_migrations --migration-id <id> --description "..." --sql-file path/to/file.sql --dry-run` before applying any future schema SQL file. The helper records checksum, status, statements, metadata, and failures in `stockey_schema_migrations`. Missing, running, or failed registry rows are also surfaced by `python -m advisory.operator_health --skip-dhan` and the existing Data Health/fix-hints UI.

The announcement text offload script also uses this registry for its S3 key/hash/count/excerpt columns, so if the offload fails before uploading text, inspect `python -m utils.schema_migrations --list --limit 20` before rerunning.

Hot trace/intraday retention is also dry-run first. Use the report command before any archive/delete:

```sh
python scripts/hot_table_retention.py --format json
python scripts/hot_table_retention.py --archive-s3 --max-chunks 1 --format json
python scripts/hot_table_retention.py --archive-s3 --delete --max-chunks 1 --execute --format json
```

`--delete` is blocked unless `--archive-s3` is present or `--allow-delete-without-archive` is explicitly supplied. Archive/delete operations are chunked by month.

The first registry-managed runtime schemas are Dhan OHLCV via migration id `20260611_dhan_ohlcv_base_asset_type`, used by `data/dhanlive/ohlcv.py`; Dhan scrip master via migration id `20260611_dhan_scrip_master_base`, used by `data/dhanlive/scrip_master.py`; file-level ingestion state via migration id `20260611_ingestion_file_state_base`, used by `utils/ingestion_state.py`; NSE security identity history/review output via migration id `20260611_nse_security_history_base`, used by `data/nseindia/security_history.py`; Screener.in registered screener outputs via migration id `20260611_screenerin_registered_screeners_base`, used by `data/screenerin/screener_parser.py`; Screener.in ad hoc query outputs via migration id `20260611_screenerin_ad_hoc_query_base`, used by `data/screenerin/ad_hoc_query.py`; Screener.in parse/fetch failures via migration id `20260611_screenerin_parse_failures_base`, used by `data/screenerin/failure_log.py`; Economic Times RSS items via migration id `20260611_economictimes_rss_items_base`, used by `data/economictimes/rss.py`; market regime snapshot output via migration id `20260611_advisory_market_regime_base`, used by `advisory/regime_engine.py`; market news overlay output via migration id `20260611_advisory_market_overlay_daily_base`, used by `advisory/news_overlay_engine.py`; hypothesis/playbook outputs via migration id `20260611_advisory_hypothesis_engine_base`, used by `advisory/hypothesis_engine.py`; external task queue via migration id `20260611_advisory_external_task_queue_base`, used by `advisory/external_task_queue.py`; identity issue tracking via migration id `20260611_advisory_identity_issues_base`, used by `advisory/identity_issues.py`; fallback telemetry via migration id `20260611_advisory_fallback_telemetry_base`, used by `advisory/fallback_telemetry.py`; current-price cache via migration id `20260611_advisory_current_prices_base`, used by `advisory/current_prices.py`; wait-signal outputs via migration id `20260611_advisory_wait_signals_base`, used by `advisory/wait_signals.py`; live event-router actions via migration id `20260611_advisory_event_router_actions_base`, used by `advisory/event_router.py`; trace-summary cache via migration id `20260611_advisory_trace_summaries_base`, used by `advisory/trace_summary_store.py`; execution order/fill handoff via migration id `20260611_advisory_execution_orders_base`, used by `advisory/execution_engine.py`; company-memory review output via migration id `20260611_advisory_company_memory_reviews_base`, used by `advisory/company_memory_review.py`; rule-engine candidates/rejections via migration id `20260611_advisory_rule_outputs_base`, used by `advisory/rule_engine.py`; event-policy action overlay via migration id `20260611_advisory_event_policy_actions_base`, used by `advisory/event_policy.py`; adversarial event reviews via migration id `20260611_advisory_event_reviews_base`, used by `advisory/adversarial_review.py`; announcement watch outputs via migration id `20260611_advisory_announcement_watch_outputs_base`, used by `advisory/announcement_watch.py`; news watch outputs via migration id `20260611_advisory_news_events_base`, used by `advisory/news_watch.py`; news theme screener mappings via migration id `20260611_advisory_news_theme_screeners_base`, used by `advisory/news_theme_engine.py`; config-change previews via migration id `20260611_advisory_config_change_previews_base`, used by `advisory/config_change_assistant.py`; intraday feature cache via migration id `20260611_advisory_intraday_features_base`, used by `advisory/intraday_features.py`; watchlist builder output via migration id `20260611_advisory_watchlist_base`, used by `advisory/watchlist_builder.py`; operator API write-audit tables via migration id `20260611_advisory_operator_api_audit_base`, used by `advisory/api/app.py`; technical-threshold promotion review output via migration id `20260611_advisory_technical_threshold_promotion_base`, used by `advisory/technical_threshold_promotion.py`; signal-quality promotion review output via migration id `20260611_advisory_signal_quality_promotion_base`, used by `advisory/signal_quality_promotion.py`; event-policy promotion review output via migration id `20260611_advisory_event_policy_promotion_base`, used by `advisory/event_policy_promotion.py`; consolidated action recommendations via migration id `20260611_advisory_action_recommendations_base`, used by `advisory/action_recommender.py`; decision trace/action-conflict audit tables via migration id `20260611_advisory_decision_trace_base`, used by `advisory/decision_trace.py`; event-evaluation/risk outputs via migration id `20260611_advisory_event_evaluation_outputs_base`, used by `advisory/llm_event_evaluator.py`; event meta-model score outputs via migration id `20260611_advisory_event_meta_model_scores_base`, used by `advisory/event_meta_model.py`; event-policy evaluator outputs via migration id `20260611_advisory_event_policy_evaluator_base`, used by `advisory/event_policy_evaluator.py`; technical-threshold calibration outputs via migration id `20260611_advisory_technical_threshold_calibration_base`, used by `advisory/technical_threshold_calibration.py`; signal-quality evaluator outputs via migration id `20260611_advisory_signal_quality_evaluator_base`, plus trusted context-rule adjustment columns via `20260620_advisory_signal_quality_trusted_context_adjustments`, used by `advisory/signal_quality_evaluator.py`; risk allocations via migration id `20260611_advisory_allocations_base`, used by `advisory/risk_engine.py`; portfolio orders via migration id `20260611_advisory_portfolio_orders_base`, used by `advisory/portfolio_engine.py`; lifecycle/rebalance actions via migration id `20260611_advisory_position_lifecycle_base`, used by `advisory/position_lifecycle.py`; signal-refresh actions via migration id `20260611_advisory_signal_refresh_actions_base`, used by `advisory/signal_refresh.py`; shared sync-state via migration id `20260611_advisory_sync_state_base`, used by `advisory/sync_state.py`; continuous-watch alerts via migration id `20260611_advisory_live_watch_alerts_base`, used by `advisory/continuous_watch.py`; the research ledger via migration id `20260611_advisory_research_runs_base`, used by `advisory/research_ledger.py`; TS forecast feature output via migration id `20260611_advisory_ts_forecast_features_base`, used by `advisory/ts_forecast_features.py`; TS forecast workflow watchlist output via migration id `20260611_advisory_ts_forecast_workflow_base`, used by `advisory/ts_forecast_workflow.py`; TS forecast evaluator outputs via migration id `20260611_advisory_ts_forecast_evaluator_base`, used by `advisory/ts_forecast_evaluator.py`; announcement text offload columns via migration id `20260611_announcement_text_offload_storage_columns`, used by `scripts/offload_announcement_text_to_s3.py`; and dynamic company-master-id backfill columns via per-table `20260611_company_master_id_backfill_*` migrations, used by `data/backfill_company_master_ids.py`. Remaining direct DDL is limited to generic `utils.db` table/upsert helpers and source-specific temporary/staging tables.

News theme sector/context overlays use migration id `20260620_advisory_news_theme_context_overlays_base`, table `advisory_news_theme_context_overlays`, and module `advisory/news_theme_engine.py`. These rows are review/watchlist-pressure evidence only (`authority_scope=watchlist_pressure_only`), not trade execution or portfolio authority.

Exchange-event context overlays use migration id `20260620_advisory_exchange_context_overlays_base`, table `advisory_exchange_context_overlays`, and module `advisory/exchange_context_overlays.py`. The advisory `exchange_events` stage builds them from normalized insider, block/bulk, short-selling, and corporate-action rows. These rows are also review/watchlist-pressure evidence only and cannot bypass technical confirmation, liquidity checks, portfolio policy, or broker execution gates.

Macro context overlays use migration id `20260620_advisory_macro_context_overlays_base`, table `advisory_macro_context_overlays`, and module `advisory/macro_context_overlays.py`. The advisory `macro_features` stage builds them from point-in-time macro features and, when no fresh macro feature row is produced for the target date, uses the latest persisted macro feature row at or before the as-of date. Continuous news/announcement watchers can map active macro sector pressure to current market-context universe symbols with `authority_scope=watchlist_pressure_only`; these rows are context evidence, not regime hard gates or broker authority.

News-theme context overlays use migration id `20260620_advisory_news_theme_context_overlays_base`, table `advisory_news_theme_context_overlays`, and module `advisory/news_theme_engine.py`. Theme overlay metadata now includes `sector_alias_coverage`, and Operator Health full mode checks `theme_sector_alias_coverage` so new human theme-sector labels are flagged before they silently fail to map into coded market-context universe sectors.

Context-overlay signal refresh separates identity/source policy suppression from generic watchlist-policy suppression. `advisory.signal_refresh --from-context-overlays` reports `identity_policy_suppressed_target_rows` and per-source `policy_suppression_reason_counts`, and `advisory.recommendation_diagnostics` surfaces fully identity-suppressed families as `source_family_identity_unresolved` so operators repair company-master/Dhan/security mapping instead of changing regime/context thresholds. Operator Health `signal_refresh_source_state` also surfaces total and per-source identity suppression counts with repair commands for identity issues, Dhan scrip master, and bounded context-overlay refresh. The same Health section audits `CONTEXT_OVERLAY_SIGNAL_REFRESH_VERSION`; older sync-state rows without the current algorithm version are warnings and should be refreshed before interpreting context-overlay conversion or identity-suppression counts.

Bhavcopy context overlays use migration id `20260620_advisory_bhavcopy_context_overlays_base`, table `advisory_bhavcopy_context_overlays`, and module `advisory/event_evidence_store.py`. `complete_data.sh` refreshes these rows from compact bhavcopy evidence. They convert accumulation, distribution/short pressure, abnormal turnover, and circuit risk into symbol-level review/watchlist pressure with `authority_scope=watchlist_pressure_only`.

Announcement context overlays use migration id `20260620_advisory_announcement_context_overlays_base`, table `advisory_announcement_context_overlays`, and module `advisory/event_evidence_store.py`. `complete_data.sh` refreshes these rows from compact announcement evidence and latest event-evaluation fields. They convert official filing classes such as order wins, regulatory notices, promoter activity, guidance changes, approvals, analyst meets, and material neutral updates into symbol-level positive/negative/watch pressure with `authority_scope=watchlist_pressure_only`. Compact announcement evidence also records `announcement_storage_form`, `llm_evidence_mode`, `llm_review_ready`, and `raw_archive_required`, so downstream LLM/review code can distinguish compact tensors, summary-backed evidence, and archive-only references before decision use. `advisory/event_evidence_store.py` applies migration id `20260621_advisory_announcement_evidence_taxonomy_contract` and backfills missing taxonomy values on older compact evidence rows before refresh. `advisory/llm_event_evaluator.py` now filters archive-only or not-review-ready compact rows from compact-evidence use and lets those ids use raw-document fallback instead. `advisory/company_memory_review.py` applies the same gate and reports accepted storage-form counts in its evidence-source contract. `advisory/regime_overlay.py` applies the same gate when aggregating recent announcement event counts for review-only regime-overlay proposals.

Only run the duplicate-index cleanup with `--execute` after reviewing the dry-run output:

```sh
python scripts/drop_duplicate_indexes.py --limit 20 --min-mb 1 --execute
```

Slow-operation tracking writes:

- `logs/performance/slow_operations.jsonl`: append-only event log, one row per slow occurrence.
- `logs/performance/slow_operation_state.json`: deduped issue state keyed by fingerprint with first seen, last seen, count, max latency, and status.

Use `python -m advisory.performance_slowlog mark <fingerprint> triaged --note "..."` after adding a TODO or fix plan, so recurring slow events are counted but not treated as new work.

Operator Health treats slow-operation state as active only when the latest fresh API probe reproduces a slow or failing route. Unprobed historical slowlog rows remain visible as recovered/contextual degradation rows, but they do not keep the Health trust gate blocked after a fresh healthy probe.

For `/api/actions?compact=true` and `/api/portfolio?compact=true`, inspect `meta.<section>.payload_bytes`, `avg_row_bytes`, and `max_row_bytes` alongside the probe latency. This tells you whether the next fix should target query/index work or response compaction/detail endpoints. For Action Queue feature freshness, list calls use persisted decision-time snapshots unless `refresh_feature_freshness=true` is supplied; reserve live refresh for targeted diagnostics.

Legacy NSE retention workflow:

```sh
python scripts/db_table_retention_report.py --table nseindia_var1 --retention-days 365
python scripts/archive_legacy_nse_tables.py --table nseindia_var1 --retention-days 365 --max-chunks 3
python scripts/archive_legacy_nse_tables.py --table nseindia_var1 --retention-days 365 --archive-s3 --max-chunks 1 --execute
```

Only add `--delete` after validating the S3 archive. Deleting old rows does not immediately shrink the underlying Postgres files; schedule `VACUUM FULL` or `pg_repack` later if the goal is to return disk to the OS.

## General usage guidelines

- Prefer `python -m ...` from the repo root so relative config and `.env` loading behave consistently.
- Keep `.env.example` in sync with runtime settings by running `python scripts/env_example_audit.py --strict` after adding new `env(...)`, `os.getenv(...)`, shell, cron, or frontend public env usage.
- Keep docs aligned with the current operator flow by running `python scripts/docs_state_audit.py --strict` after changing top-level scripts, cron, UI paths, or roadmap status.
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

- Announcement document OCR, concise summaries, structured report parsing, and advisory event evaluation can run through Codex CLI instead of hosted ChatGPT/Gemini APIs. Set `OCR_USING=codex`, `SUMMARIZE_WITH=codex`, and `ADVISORY_EVENT_EVAL_MODEL=codex`; tune `CODEX_CLI_OCR_MODEL`, `CODEX_CLI_SUMMARIZE_MODEL`, `CODEX_CLI_EVENT_MODEL`, `CODEX_CLI_BIN`, and `CODEX_CLI_TIMEOUT_SECONDS` as needed. For cron, prefer an absolute `CODEX_CLI_BIN`; the wrapper also searches common nvm, local npm, Homebrew, and `/usr/local/bin` paths before failing with fallback telemetry.

- LLM/Codex prompt contracts are inventoried in `advisory.prompt_registry`. Use `python -m advisory.prompt_registry` or the Nuxt `/prompt-registry` page to review prompt ids, schema models, source files, model env vars, authority boundaries, output tables, and fallback behavior. This registry is audit metadata only; it does not execute prompts or grant trading authority.

- Core LLM/Codex output rows persist prompt-contract metadata so later audits can tell which prompt and response schema produced a row. Current coverage includes `advisory_event_evaluations.prompt_id/prompt_version/prompt_schema_version`, `advisory_event_policy_actions.llm_prompt_*`, `advisory_playbook_action_plans.prompt_*`, `advisory_company_memory_reviews.prompt_*`, `advisory_action_recommendations.manual_revision_prompt_*`, `advisory_technical_threshold_promotion_reviews.prompt_*`, `announcement_pipeline_documents` summary/OCR prompt metadata, and `announcement_pipeline_reports.prompt_*`.

- Runtime fallback/degraded-path events are persisted in `advisory_fallback_events` or, when Postgres itself may be unavailable, in the local fallback JSONL spool read by Operator Health. Current emitters cover source retries/session resets, broker identity/auth failures, DB retry exhaustion, ingestion/parser degradation, event/policy/playbook/company-memory fallbacks, feature/risk/portfolio/action/execution context degradation, operator API payload fallbacks, date/display formatting fallbacks, and maintenance-tool failures. Operator Health reads compact local/db-retry spools as `fallback_telemetry` for routine Health, adds them to fix hints and the trust gate, and avoids grouped scans of the large DB fallback table on frequent UI polls; use the direct fallback telemetry summary path for deep persisted-table investigation. Use `python scripts/fallback_telemetry_coverage_report.py --format json` to audit remaining source/API handlers. The 2026-06-12 cleaned baseline had zero normal `silent_fallback` / `silent_handler` rows; the current active branch is back to the same standard with `records_fallback=716`, `reraises=61`, `self_protection=3`, zero normal `silent_fallback`, and zero normal `silent_handler`, so `--fail-on-silent` should pass and should be run after new source/API paths are added.

- Consolidated action decisions can also get Codex-generated manual revision pointers. Set `ACTION_MANUAL_REVISION_POINTERS_ENABLED=true` and `ACTION_MANUAL_REVISION_POINTERS_MODEL=codex` or `codex:<model>`. The output is persisted on `advisory_action_recommendations` as `manual_revision_summary` and `manual_revision_pointers_json`; if Codex fails, deterministic fallback pointers are written instead.

- Final action rows also persist `recommendation_reason_json` and `reason_contract_status`. If a broker-action row is missing required reason, evidence, execution, or risk fields, action consolidation downgrades it to `MANUAL_REVIEW` before it can reach execution. The execution planner also blocks stale broker-action rows whose reason contract is missing or incomplete.
- Use `python -m advisory.recommendation_diagnostics --format text` after `all_advisory.sh` when the market looks constructive but `/recommendations` has few or no BUY rows. It is a read-only report over `advisory_action_recommendations`, plus same-date `advisory_candidates`, `advisory_candidate_rejections`, `advisory_allocations`, and `advisory_portfolio_orders`, that summarizes final action mix, BUY/BUY_MORE count, complete broker-candidate count, upstream PASS/watch/rejection state, candidate-to-final-action survival, `PASS_NOW` rows whose technical engine state is still `IGNORE`, technical entry readiness counts (`confirmed_entry`, `watch_or_near`, `ignore_or_reject`), feature-freshness blockers, market-context blocks, context-overlay conflicts, possible broad-regime-label hard gates, hard-vs-context-only regime/overlay rejection breakdowns, the current context-gate env policy, risk base-fallback rows lacking confirmed technical entries, portfolio rows deferred with `technical_entry_not_confirmed`, and whether persisted rows were generated before the current decision-code files. Text output is intentionally compact for cron logs; use `--format json` for full nested samples. It never mutates portfolio, action, or broker state. Treat `diagnostic_trust.status=current` as usable for post-advisory debugging; if it is `requires_advisory_rerun`, `code_data_freshness.status=source_rows_precede_current_code`, or `diagnostic_trust.stale_context_gate_conflict=true`, rerun `all_advisory.sh` before changing thresholds, regime/context policy, or recommendation rules unless `primary_no_buy_cause.next_commands_source=context_to_entry_pipeline_then_advisory_rerun_readiness`, in which case run the printed context-to-entry command chain first. `stale_context_gate_conflict=true` specifically means persisted hard regime/overlay rejection rows conflict with the current context-only rule-engine policy, so the missing-BUY state is stale evidence until a fresh advisory run replaces those rows. The post-advisory watcher section now separates `action_refresh_candidate_count` from `full_advisory_required_count` and adds `action_refresh_sync`, which compares both the latest `continuous_watch:action_refresh` success timestamp and its stored advisory `asof_date` against the newest action-refresh-eligible watcher row. If `action_refresh_recommended=true`, run the printed `python -m advisory.action_recommender --date ... --format text` command to refresh consolidated Action Queue rows from recent signal-refresh evidence without running the long advisory path; the direct CLI now records the same `continuous_watch:action_refresh` sync state that diagnostics read, so a later diagnostic run converges to `action_refresh_sync.status=current` only when timestamp and `asof_date` both match. If `action_refresh_sync.status=current`, the fast Action Queue visibility is already current and the command is suppressed; `stale_wrong_asof` means a refresh exists for a different advisory date and the printed date-specific command should still be run. Context-overlay refresh commands are suppressed the same way when `context_overlay_signal_refresh_state.status=current`, meaning the persisted bounded refresh already matches the preview as-of date, row counts, and no-portfolio/no-broker authority. This refresh is review-only and still keeps `full_advisory_required=true`, `portfolio_authority=none`, and `broker_execution_allowed=false`. `advisory.action_recommender` emits compact/log-safe samples by default; add `--full-sample` only when raw context and recommendation JSON are explicitly needed. If `full_advisory_rerun_recommended=true`, the quick action refresh is only for visibility; the next authoritative portfolio/risk reconciliation still requires `./all_advisory.sh`. `primary_no_buy_cause` is the prioritized operator runbook field: it ranks no persisted action rows, stale/untrusted diagnostics, risk/portfolio technical handoff, stale/missing inputs, technical entry non-confirmation, market-context suppression, or watch-only states, so use it as the first answer and the detailed sections as evidence. It also includes `safe_to_tune_policy` and `next_commands`; run the listed commands first. When context-to-entry diagnostics provide a bounded technical-refresh plus `advisory.pipeline --start-at rules --stop-at actions` chain, that chain supersedes generic full-advisory wording in the primary operator action; full `all_advisory.sh` is only fallback/catch-up if the bounded chain does not refresh the stale evidence. Empty recommendation rows produce `no_persisted_action_rows` and recommend `./all_advisory.sh` before any threshold or regime-policy diagnosis. Watch/near-entry technical rows without confirmed entry set `technical_entry_confirmation_not_met` and recommend dry-run technical threshold calibration before changing regime/context policy. Watch-only rows do not make live threshold tuning safe by themselves: they set `eligible_for_threshold_research=true` and recommend dry-run technical calibration instead of editing production policy.
- Recommendation diagnostics text output also includes a compact candidate survival line in the form `candidate_state/technical_state/trigger -> final_action(source)`. If this shows many `WATCH_BREAKOUT/IGNORE` rows, treat the no-BUY blocker as technical-engine trigger or handoff calibration, not as a global regime/context-policy failure.
- The same diagnostics report now includes `causal_memory_refresh_preview`. If fresh exact `candidate_helpful` causal-memory rows would create review-only signals, the recommended command is `python -m advisory.signal_refresh --from-causal-memory --asof-date YYYY-MM-DD --limit 25 --format text`. This is only Action Queue visibility and cannot create BUY/SELL authority, sizing changes, portfolio rows, or broker execution.
- Execution planning is action-contract first. If `advisory_action_recommendations` is empty, the planner returns no broker orders by default instead of falling back to raw portfolio/rebalance rows. Set `EXECUTION_ALLOW_LEGACY_PORTFOLIO_FALLBACK=true` only for legacy debugging.

- Action consolidation enriches reason contracts from latest `advisory_candidates` and `advisory_market_regime` snapshots before validation. This adds screener provenance, technical state/scores, setup score, candidate state, and macro/regime context without rerunning the full advisory pipeline.
- Action consolidation also consumes recent `advisory_signal_refresh_actions` rows as fast review-only candidates. Positive watcher/router/context rows map to `WATCH`; negative rows map to `REDUCE_EXPOSURE_REVIEW`; stop-policy rows map to `TIGHTEN_STOP`; and unresolved rows map to `MANUAL_REVIEW`. Rows from `signal_source=action_recommendation` are ignored to avoid echoing the previous consolidated action. This bridge preserves `full_advisory_required=true`, `portfolio_authority=none`, and `broker_execution_allowed=false`. `advisory.continuous_watch` now runs a bounded action refresh after both context-overlay signal refresh and event-router signal refresh; router refresh uses only `action_status=ok` symbols, while failed or planned-but-unexecuted symbols remain in router audit/fallback output. It writes `continuous_watch:action_refresh` sync-state and a bus event; set `WATCHER_ACTION_REFRESH_ENABLED=false` to disable this bridge or `WATCHER_ACTION_REFRESH_MAX_SYMBOLS` to cap the per-tick symbol count.
- Context-watchlist reconciliation is intentionally sensitive to upstream context signal-refresh state and downstream technical-refresh status. `--skip-if-current` skips only when the same-date `CONTEXT_OVERLAY_WATCH` snapshot is fresh against the matching context signal-refresh sync state and no newer active-symbol technical-refresh status exists. Hard technical/OHLCV blockers, including `technical_build:technical_rows_missing_after_build`, are persisted as rejected audit rows and cannot become active context-watch intake until repaired.
- Action consolidation treats hard risk-off score and hard macro-risk as positive-action blockers, but weak broad-market breadth alone only reduces BUY/BUY_MORE size by default. Set `ACTION_MARKET_CONTEXT_WEAK_BREADTH_HARD_BLOCK_ENABLED=true` to restore broad weak-breadth blocking.
- Reviewed signal-quality overlay rules are inert by default. Set `ACTION_SIGNAL_QUALITY_OVERLAY_RULE_CONSUMER_ENABLED=true` only after a manually-applied `signal_quality_overlay_rules` row has `status: trusted_overlay`, safe review-only authority, and an explicit supported `action_policy_effect`. Supported runtime effects currently cannot create broker-capable BUY authority: `negative_context_blocks_positive_to_watch` can downgrade BUY/BUY_MORE to WATCH when trusted negative context pressure is present, and `positive_context_boosts_watch_priority` can only boost WATCH ranking among non-broker rows.
- Full Operator Health checks trusted signal-quality overlay rules against current persisted source-family reliability and rule-level cross-window stability metadata. Signal-quality promotion also blocks broad source-family review generation when fast context reliability includes harmful context-class diagnostics, even if the family average is helpful. A `trusted_overlay` rule is runtime-eligible only when the matching source family is currently `candidate_helpful` and the rule carries `stability_classification=stable_candidate`; otherwise Health marks it as annotation-only and suggests rerunning `advisory.context_overlay_reliability_report`, `advisory.model_training_runner --run-signal-quality-window-runner --include-signal-quality-split-reports --skip-s3-upload`, and full Health.

- Event/playbook reason contracts are enriched from latest `advisory_event_evaluations`, `advisory_event_reviews`, and `advisory_playbook_action_plans`. This adds event ids/classes/verdicts/reviewer actions and matched playbook/review-check context before the final action row is accepted.

- `all_watchers.sh` runs `advisory.continuous_watch`, which now augments active watchlist symbols with lower-priority context queues from `advisory_market_context_universe_daily`, `advisory_news_theme_context_overlays`, `advisory_announcement_context_overlays`, `advisory_bhavcopy_context_overlays`, and `advisory_macro_context_overlays`. Explicit watchlist rows win first; official announcement context wins over theme/bhavcopy/macro/broad-market context for the same symbol. Market context supplies top-50% breadth/leadership names; theme context maps active positive/negative news-theme sector pressure to current market-context universe symbols; announcement context maps recent official filing evidence directly to symbols; bhavcopy context maps symbol-level accumulation/distribution/short/circuit pressure; macro context maps crude, rupee, yield, food-inflation, and broad-risk sector pressure to current market-context universe symbols. Context rows carry `authority_scope=watchlist_pressure_only`. Synthetic context queues now reuse persisted source-family reliability from `advisory_context_overlay_reliability_summary`: families classified as `hurts_or_no_lift`, `negative_after_cost`, or `inconsistent_or_horizon_sensitive` are excluded from theme/announcement/bhavcopy/macro watcher target selection, while explicit watchlist rows, broad market-context rows, and missing/immature reliability evidence remain neutral. Top-context, theme-context, announcement-context, bhavcopy-context, and macro-context news/announcements are persisted as `context_observed` unless deterministic materiality keywords mark them `triggered`; only `triggered` rows are routed for advisory refresh/evaluation. `python -m advisory.signal_refresh --from-context-overlays` now turns positive/watch overlays into review-only `WATCH` discovery rows and turns negative theme/direct symbol overlays into review-only `REDUCE_EXPOSURE_REVIEW` rows when they match existing watchlist/portfolio exposure; the old `--from-theme-context` flag remains an alias. Negative/de-risk rows also reuse source-family reliability: families classified as `hurts_or_no_lift`, `negative_after_cost`, or `inconsistent_or_horizon_sensitive` are suppressed from de-risk signal creation, and suppressed counts/samples are emitted in sync-state/API summaries. `advisory.continuous_watch` also runs a separate causal-memory cycle that calls `advisory.signal_refresh --from-causal-memory` semantics for fresh exact `candidate_helpful` memory groups; tune or disable it with `WATCHER_CAUSAL_MEMORY_REFRESH_ENABLED` and `WATCHER_CAUSAL_MEMORY_REFRESH_LIMIT`. These rows set `portfolio_authority=none`, `broker_execution_allowed=false`, and `full_advisory_required=true`. Market-context summaries and the operator home page show `Triggered` versus `Observed` counts so broad evidence noise is visible. Tune breadth with `MARKET_CONTEXT_WATCH_LIMIT` (default `50`), `THEME_CONTEXT_WATCH_LIMIT` (default `50`), `ANNOUNCEMENT_CONTEXT_WATCH_LIMIT` (default `50`), `ANNOUNCEMENT_CONTEXT_LOOKBACK_DAYS` (default `7`), `BHAVCOPY_CONTEXT_WATCH_LIMIT` (default `50`), `MACRO_CONTEXT_WATCH_LIMIT` (default `50`), and `THEME_CONTEXT_DERISK_LIMIT` (default `50`). Frequent watcher runs also cap expensive official-announcement ingest/OCR work with `WATCHER_ANNOUNCEMENT_MAX_INGEST_TARGETS` (default `12`); skipped targets are not marked checked, are reported as `skipped_ingest_target_count`, and remain due for later watcher runs or end-of-day catch-up. The wrapper self-locks with `/tmp/stockey_watchers.lock`; skipped overlaps exit successfully and the next run resumes from `advisory_sync_state`. Watcher output is an intraday evidence/signal-refresh layer: fresh OHLCV/news/announcement evidence, material context triggers, theme-context triggers, announcement-context triggers, bhavcopy-context triggers, macro-context triggers, causal-memory watch/de-risk pressure, watch discovery, de-risk pressure, and matched wait signals can write fast symbol-scoped rows, but final portfolio allocation, cross-sectional action reconciliation, and dry-run execution previews still require the full `all_advisory.sh` path.

`advisory.context_overlay_refresh` performs explicit schema setup for all selected context-overlay tables on non-dry runs before building overlays, so empty-but-valid sources still have durable tables for Health and signal-refresh diagnostics. `advisory.signal_refresh --from-context-overlays --dry-run` also returns diagnostics with per-source table status, row counts, latest dates, active-authorized counts, watchlist/portfolio freshness, and `empty_reason`. Use this first when the command emits zero signal rows; it distinguishes missing/empty context-overlay data from valid overlays that did not match current watchlist or portfolio exposure.

Fast signal refresh preserves event-policy intent while keeping authority review-only: `BUY_WATCH` becomes `WATCH`, `REDUCE_EXPOSURE_REVIEW` remains de-risk pressure, and only true event-policy `MANUAL_REVIEW` rows become manual-review signals. All of these still carry `portfolio_authority=none`, `broker_execution_allowed=false`, and `full_advisory_required=true`.

`python -m advisory.signal_refresh --from-causal-memory --dry-run --limit 25 --format json` checks fresh symbol-scoped causal event-memory rows against the latest exact evaluator summary. Only `candidate_helpful` groups with positive benchmark-excess directional helpfulness, clean contradiction state, and enough decayed pressure can create review-only `WATCH` or `REDUCE_EXPOSURE_REVIEW` rows. This is a fast evidence bridge only: it does not create BUY/SELL authority, portfolio rows, sizing changes, or broker execution.

Event-policy LLM manual-review refinement is also bounded to review-only classification. It can choose `NO_ACTION`, `BUY_WATCH`, `REDUCE_EXPOSURE_REVIEW`, or `MANUAL_REVIEW`; it cannot emit broker-capable `BUY` / `SELL`, sizing, portfolio mutation, or execution instructions.

`advisory.watchlist_builder` also persists positive/watch context overlays into `advisory_watchlist` under setup id `CONTEXT_OVERLAY_WATCH`. These rows have `watch_source=context_overlay`, `context_authority_scope=watchlist_pressure_only`, and `context_policy_effect=watch_only_no_buy_authority`. This lets fresh announcements, exchange events, bhavcopy evidence, news themes, and macro sector pressure add symbols to the durable watchlist even when no screener candidate exists. When signal-quality source-family evidence exists, the watchlist row records `context_reliability_classification` and excludes families classified as `hurts_or_no_lift`, `negative_after_cost`, `inconsistent_or_horizon_sensitive`, `needs_benchmark_attribution`, or `benchmark_beta_not_overlay_alpha` by default. Source families classified as `candidate_helpful` can receive a bounded watch-priority multiplier, persisted in `watch_reasons_json` as `watch_priority_score`; this only changes context-watch ranking and cannot create buy, portfolio, or broker authority. Newer same-symbol negative direct context from announcement, exchange, or bhavcopy overlays suppresses older positive/watch context intake by persisting an inactive `REJECT` context row with `context_policy_effect=suppress_context_watch_only_no_sell_authority`; this removes context-watch intake only and is not a sell recommendation. Missing or immature reliability evidence does not block emerging watch signals. These rows are not BUY recommendations and cannot bypass technical confirmation, risk, lifecycle, action consolidation, or broker gates. Tune with `WATCHLIST_CONTEXT_OVERLAY_ENABLED` (default `true`), `WATCHLIST_CONTEXT_OVERLAY_LOOKBACK_DAYS` (default `14`), `WATCHLIST_CONTEXT_OVERLAY_LIMIT` (default `50`), `WATCHLIST_CONTEXT_OVERLAY_RELIABILITY_PRIORITY_ENABLED` (default `true`), `WATCHLIST_CONTEXT_OVERLAY_HELPFUL_SCORE_MULTIPLIER` (default `1.25`), `WATCHLIST_CONTEXT_OVERLAY_NEGATIVE_SUPPRESSION_ENABLED` (default `true`), and `WATCHLIST_CONTEXT_OVERLAY_EXCLUDED_RELIABILITY_CLASSES` (default `hurts_or_no_lift,negative_after_cost,inconsistent_or_horizon_sensitive,needs_benchmark_attribution,benchmark_beta_not_overlay_alpha`).

- Investor playbooks live in `config/hypotheses.yaml`. Import them with `python -m advisory.hypothesis_engine --import-config config/hypotheses.yaml`; preview with `--dry-run`; run matching/action-plan generation with `python -m advisory.hypothesis_engine --run-scan`. Hypothesis/playbook tables are registry-managed by `20260611_advisory_hypothesis_engine_base`. Only `status: trusted_overlay` playbooks can affect consolidated actions, and only as `review_only` overlays. Playbook market-context adjustment matches action consolidation: hard downgrades require risk-off score, hard macro-risk, or explicit `HYPOTHESIS_MARKET_CONTEXT_REGIME_LABEL_HARD_BLOCK_ENABLED=true` / `HYPOTHESIS_MARKET_CONTEXT_WEAK_BREADTH_HARD_BLOCK_ENABLED=true`; weak breadth is caution evidence by default. Positive playbook `BUY_WATCH` rows default to `WATCH` / watchlist-pressure evidence (`ACTION_PLAYBOOK_BUY_WATCH_AS_WATCH=true`) so they support discovery without suppressing confirmed technical `BUY` / `BUY_MORE` rows. Negative playbook `REDUCE_EXPOSURE_REVIEW` rows default to a first-class review-only de-risk action (`ACTION_PLAYBOOK_REDUCE_EXPOSURE_AS_REVIEW_ACTION=true`) instead of generic Manual Review; they remain non-broker and require full advisory/lifecycle confirmation before portfolio or execution changes.
- Event-policy bridge rows follow the same action-boundary pattern: positive `BUY_WATCH` rows default to `WATCH` pressure (`ACTION_EVENT_POLICY_BUY_WATCH_AS_WATCH=true`), while negative `REDUCE_EXPOSURE_REVIEW` rows default to first-class review-only de-risk actions (`ACTION_EVENT_POLICY_REDUCE_EXPOSURE_AS_REVIEW_ACTION=true`). Neither mapping grants broker, portfolio, or sizing authority.

- Dhan OHLCV auth falls back in this order:
  1. `DHAN_ACCESS_TOKEN`
  2. cached token at `.cache/dhan_access_token.json`
  3. API key consent flow using `DHAN_CLIENT_ID`, `DHAN_API_KEY`, `DHAN_API_SECRET`
- In the API key flow, the default path opens the Dhan consent page in the browser and waits for you to paste the redirected URL back into the terminal. The access token is then cached until expiry.
- Dhan browser automation uses the running Chrome CDP session plus `DHAN_LOGIN_MOBILE`, `DHAN_TOTP_SECRET`, and `DHAN_LOGIN_PIN`. When those values and `CDP_ENDPOINT` are configured, Dhan clients auto-refresh through Playwright after token cache expiry instead of asking for manual pasted consent. You can also force this path with `DHAN_AUTO_LOGIN_ENABLED=true`. If token refresh happens while advisory/API code is already inside an asyncio loop, the auth layer runs the same Playwright login in a separate Python subprocess to avoid Playwright sync-API event-loop failures; control that wait with `DHAN_AUTO_LOGIN_SUBPROCESS_TIMEOUT_SECONDS`.

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
| `all_frontend.sh` | Operator frontend supervisor | Regular cron restarts/supervises `advisory.api.app` and the Nuxt operator app under a lock; exits with `restart_requested` when watched API/Nuxt source files change; supports `--api-only`, `--web-only`, and `--both` for targeted restarts |
| `all_watchers.sh` | Continuous monitoring wrapper | Self-locking one-shot/loop wrapper that polls active watchlist OHLCV, announcements, and ET/news incrementally from persisted cursors |
| `all_downloaders_queue.sh` | Queued download ingestion | Enqueues single-client NSE/Dhan/Screener downloader work and runs safe non-queued downloader modules inline |
| `all_external_workers.sh` | External queue worker drain | Drains Dhan, Screener, and NSE queues serially under one lock |
| `complete_data.sh` | Broad ingestion safety net and pre-advisory catch-up | Runs all downloaders and parsers in order; scheduled before market and again before advisory, not in the watcher loop |
| `all_advisory_preflight.sh` | Advisory readiness preflight | Runs Dhan auth refresh/validation and compact operator smoke with Dhan included; does not run `advisory.master_pipeline`, rebuild portfolios, refresh snapshots, or submit broker orders |
| `all_advisory.sh` | Advisory orchestrator | Runs bounded review-only context signal refresh, context-overlay watchlist reconciliation, and causal-memory signal refresh before post-close advisory and portfolio generation without raw downloads; defaults to bounded local parallel stages, skips hidden rule repair, refreshes operator snapshots/traces, and runs read-only recommendation diagnostics, including whether watcher/signal-refresh/wait-signal evidence arrived after the advisory decision and warrants a full rerun |
| `all_superseded_cleanup_audit.sh` | Superseded failure cleanup audit | Preview-only wrapper around `advisory.superseded_failures`; emits script markers and never passes `--apply` |
| `all_ml.sh` | Weekly research training | Long-running event-model research job plus research-only signal-quality/source-family/adversarial-review evidence refresh and stable multi-window promotion-review gating; scheduled only in a dedicated weekly window if enabled |
| `all_api_latency_probe.sh` | Operator API latency probe | Runs `scripts/api_latency_probe.py` with lifecycle markers and writes latency/slowlog evidence |
| `all_operator_health.sh` | Operator health check | Runs `advisory.operator_health --skip-dhan` by default with lifecycle markers |
| `all_hypothesis_scan.sh` | Hypothesis/playbook scan | Runs `advisory.hypothesis_engine --run-scan` with configured model/args and lifecycle markers |
| `all_ts_forecast_workflow.sh` | TS forecast workflow | Runs the research-only TS forecast watchlist refresh with configured model/max symbols and lifecycle markers |
| `all_ts_forecast_evaluator.sh` | TS forecast evaluator | Evaluates matured TS forecast rows after costs with lifecycle markers |
| `all_ts_forecast_paper_portfolio.sh` | TS forecast paper portfolio | Builds research-only forecast paper decisions with lifecycle markers |
| `all_llm_decisions.sh` | LLM decision cycle | Daily post-advisory wrapper around `advisory.llm_decision_runner --persist --label-outcomes`; generates a review-only graded/sized decision per symbol over the active universe (auto-resolves the latest advisory technical date), persists to `advisory_llm_decisions`, and matures elapsed-horizon outcomes into `advisory_llm_decision_outcomes`. Deterministic by default (`LLM_DECISION_CRON_ARGS=--no-llm`); never moves capital (`broker_execution_allowed=false`) |
| `all_research_evidence.sh` | Research evidence refresh | Daily post-close wrapper around `advisory.research_evidence_runner`; refreshes source-family, narrowed-split, context-watch, negative-pressure, adversarial-review, action-transition, causal-memory, provenance, and reliability evidence without event-model training, policy/config changes, portfolio mutation, or broker behavior |
| `all_event_policy_evaluator.sh` | Event-policy evaluator | Evaluates event-policy outcomes after costs with lifecycle markers |
| `all_technical_threshold_calibration.sh` | Technical threshold calibration | Runs weekly research-only technical threshold calibration with lifecycle markers |

`/api/manual-review` is intentionally compact by default. Use `include_raw=true` only for bounded debugging, for example `curl 'http://127.0.0.1:8085/api/manual-review?limit=5&include_raw=true'`, because full raw source rows can be much larger than the operator list needs.

`/api/health/details` is also compact by default. Use `compact=false` only for bounded debugging, for example `curl 'http://127.0.0.1:8085/api/health/details?mode=full&compact=false'`, because full health can include long tracebacks, log excerpts, and source-table diagnostic rows.

`/api/event-policy` omits bulky raw JSON source columns by default while preserving parsed checks, operator notes, LLM review, and compact raw context. Use `include_raw=true` only for short debugging, for example `curl 'http://127.0.0.1:8085/api/event-policy?limit=5&include_raw=true'`.

### Manual / catch-up / long-running scripts

| Script | Purpose | Scope |
| --- | --- | --- |
| `all_downloaders.sh` | Download-only ingestion | Manual broad catch-up for missing raw data |
| `all_parsers.sh` | Parse-only ingestion | Manual parser catch-up after raw files already exist |
| `complete_data.sh` | Combined ingestion, compact evidence, and context overlay refresh | Full download + parse catch-up/backfill plus `advisory.event_evidence_store` and `advisory.context_overlay_refresh`; useful end-of-day, after a missed day, or before a major rerun |
| `all_ml.sh` | Event-model training orchestrator | Long-running research training plus signal-quality/source-family/adversarial-review evidence refresh; weekly cron passes `--run-signal-quality-window-runner --include-signal-quality-split-reports`, which preflights first and only persists multi-window review rows/split diagnostics when readiness passes |
| `all_advisory_codex.sh` | Codex-supervised advisory orchestrator | Manual debug/repair wrapper that runs `all_advisory.sh`, captures logs, sends failure lines to Codex CLI, and reruns |
| `all_analysis_codex.sh` | Codex todo-development loop | Manual bounded loop that uses `todo.md` and `docs/analysis_agent_board.md` to pick the next slice, implement it, validate it, and update docs |

Top-level operator scripts emit `[stockey.script]` start/end markers to stdout while preserving the wrapped command's exit code. Most call `scripts/run_with_markers.sh`; `all_frontend.sh` emits markers internally so it can supervise and clean up API/Nuxt child processes. The health parser uses these markers to classify the latest run as `ok`, `failed`, `interrupted_by_operator`, `restart_requested`, `ok_after_historical_errors`, or `recovered_after_manual_interrupt`.

Fallback telemetry also carries source-specific NSE counters. Announcement NSE HTTP retries record `nse_retry`, cookie/session resets record `nse_session_reset`, `summarize_fallback_events` exposes `nse_retry_count`, `nse_session_reset_count`, and `nse_http_count`, and Operator Health emits a specific NSE session fix hint when these counters are nonzero.

`data.download_runner` also writes the latest standardized module run state to `advisory_sync_state` as `download_runner:<module>`. The runner records module, args, purpose, phase, elapsed time, return code, source/error classification, and whether state advanced. Every module configured in `DOWNLOAD_STEPS` and `PARSER_STEPS` exports or emits runner state. Modules can additionally export `STOCKEY_RUN_STATE`, `RUN_STATE`, or `RUN_RESULT` as a dictionary; the runner merges supported fields such as `from_date`, `to_date`, `rows`, `rows_read`, `rows_written`, `retries`, `retry_count`, `attempt_count`, `failed_attempt_count`, `fallback_count`, `source_unavailable_count`, `no_data_count`, `fallback_used`, and explicit `state_advanced` into the durable state. `advisory.event_evidence_store` uses this convention for compact bhavcopy/announcement evidence row counts and date windows. `advisory.context_overlay_refresh` uses it for all-context overlay date windows and rows written/read, so Health can see whether context evidence is available before `advisory.signal_quality_evaluator` runs. `data.company_master` uses it for identity row and identifier coverage counts. `data.dhanlive.ohlcv` uses it for daily/intraday OHLCV row counts, price windows, failed symbols, interval, exchange, and asset type. `data.dhanlive.scrip_master` uses it for master rows, column count, source file, and load timestamp. `data.sharpelydata.scrip_master` uses it for filtered fund/equity master counts versus raw Sharpely rows. `data.sharpelydata.sharpely_data` uses it for symbol/date-window counts plus meta, peer, statement, shareholding, and historical market-cap row counts. Screener registered/ad-hoc query CLIs use it for synced screener counts, query ids, row counts, and explicit no-data states. NSE holiday-calendar downloads use it for browser/CDP source-unavailable state plus row/product counts. NSE bhavcopy and indices parsers use it for file-level parser state: files seen/considered, date windows, lookback skips, already-processed keys, valid empty/incomplete files, failed keys, and whether parser state advanced. NSE off-market parser uses it for file-level skipped/parsed/failed counts and failure samples. NSE sparse event crawlers for corporate actions, earnings events, insider deals, and the recent event calendar use it for symbol/date-window rows plus queried/skipped counts. NSE off-market downloads use it for block/date attempts, retries, failed attempts, skipped blocks, downloaded blocks, and source-unavailable counts. NSE bhavcopy and indices archive downloaders use it for candidate/missing/downloaded date counts, failed attempts, source-unavailable counts, and consecutive-failure stop flags. Canonical benchmark sync uses it for symbol-level row, no-data, dry-run, and failure counters. WPI uses it for year/item counts, retries, failed attempts, failed item samples, and source-unavailable counts. CPI uses it for month counts, retries, failed attempts, failed month samples, and source-unavailable counts. NSDL FPI uses it for monthly catch-up counts, downloaded/skipped/failed month counts, failed month samples, and source-unavailable counts. RBI FBIL G-sec uses it for date counts, downloaded/skipped-weekend/failed dates, failed date samples, and source-unavailable counts. RBI bank rates uses it for browser/CDP source-unavailable state plus read/write row counts. Economic Times RSS uses it for feed-level success, empty-feed, failed-feed, and source-unavailable counters. FRED/ISM macro uses it for series/leg-level row counts, fallback counts, failed series/legs, and source-unavailable counters. Operator Health/Data Health reads the latest `download_runner:*` rows, summarizes run classifications and counters, and mirrors degraded rows into fix hints and Fallbacks & Degradation.

Recommended scheduler file:

- `config/stockey.crontab.template`
- `config/stockey.generated.crontab`

It schedules:

- `complete_data.sh` before market as a broad ingestion safety net and compact evidence refresh
- `complete_data.sh` again end-of-day before advisory to catch missed downloader/parser work and rebuild compact announcement/bhavcopy evidence plus all review-only context overlays
- `all_downloaders_queue.sh` plus `all_external_workers.sh` during the day for serialized single-client refreshes
- `all_watchers.sh` every `10` minutes during market hours; the script self-locks, skipped overlaps exit `0`, watcher routing writes fast live rows to `advisory_signal_refresh_actions`, active hypothesis wait signals are matched, and `CONTEXT_OVERLAY_WATCH` rows are reconciled from fresh context overlays
- `all_advisory_preflight.sh` at `18:55` on weekdays, before the expensive post-close advisory run, to validate/refresh Dhan auth and run compact operator smoke without changing portfolio/action state
- `all_advisory.sh` once daily after 7pm on weekdays, after `scripts/wait_for_locks.sh` confirms data catch-up and external worker locks are clear; by default it resolves the latest trading day from `dim_trading_days`, passes that date to the advisory run, runs bounded review-only context-overlay signal refresh only when the persisted sync state is not already current for the preview, then runs context-overlay watchlist reconciliation and causal-memory signal refresh before the long advisory reconciliation
- `all_frontend.sh` every `5` minutes under a lock so API/Nuxt are restarted if they exit or if the supervisor exits with `restart_requested` after source-code changes
- `all_advisory.sh` and `all_watchers.sh` refresh `advisory.operator_snapshot` and `advisory.trace_summary_store` after a successful run so frontend endpoints can serve cached dashboard and trace sections quickly
- `all_watchers.sh` runs `python -m advisory.watchlist_builder --setup CONTEXT_OVERLAY_WATCH --rebuild --skip-if-current` after wait-signal matching unless `WATCHER_SKIP_CONTEXT_WATCHLIST_RECONCILE=true`. By default it resolves the latest trading day with `python -m advisory.advisory_date --format date` and passes it as `--date YYYY-MM-DD`; set `WATCHER_AUTO_LATEST_TRADING_DATE=false` only for targeted debugging. The skip guard reuses the context signal-refresh sync state written by the watcher context cycle, so frequent cron runs do not rewrite durable context-watch rows unless new context signal evidence is newer than the watchlist snapshot or active legacy rows are missing `technical_confirmation_plan` metadata. Set `WATCHER_CONTEXT_WATCHLIST_FORCE=true` only when you deliberately want to rewrite durable context-watch rows during watcher runs. This step persists context-watch pressure only and cannot create BUY/SELL, portfolio, lifecycle, sizing, or broker authority.
- `all_advisory.sh` runs `python -m advisory.signal_refresh --from-context-overlays --skip-if-current --format text`, `python -m advisory.watchlist_builder --setup CONTEXT_OVERLAY_WATCH --rebuild --skip-if-current`, and `python -m advisory.signal_refresh --from-causal-memory --format text` before `advisory.master_pipeline` unless `ADVISORY_SKIP_PRE_SIGNAL_REFRESH=true`. The resolved latest trading day, or an explicit `./all_advisory.sh --date YYYY-MM-DD`, is passed to signal refresh as `--asof-date YYYY-MM-DD` and to watchlist reconciliation as `--date YYYY-MM-DD`. Signal-refresh `--skip-if-current` performs a cheap sync-state/input-diagnostic precheck and skips target loading plus duplicate writes when source/watchlist/portfolio counts and the no-broker authority contract are already current. Watchlist-builder `--skip-if-current` skips only when same-date active `CONTEXT_OVERLAY_WATCH` rows exist, the upstream context signal-refresh sync state is `ok`, the sync as-of date matches, the watchlist rows are at least as fresh as that signal-refresh run, and active rows have complete `technical_confirmation_plan` metadata; missing/stale sync state or legacy rows missing that plan rebuild instead of silently trusting old rows. Set `ADVISORY_PRE_SIGNAL_REFRESH_FORCE=true` only when you deliberately want to ignore current sync state and rewrite bounded context-overlay signal rows. Set `ADVISORY_PRE_CONTEXT_WATCHLIST_FORCE=true` only when you deliberately want to rewrite durable context-watch rows. Set `ADVISORY_AUTO_LATEST_TRADING_DATE=false` only for targeted debugging when you intentionally do not want the wrapper to pin the advisory date. Set `ADVISORY_SKIP_PRE_CONTEXT_WATCHLIST_RECONCILE=true` only to skip durable context-watchlist reconciliation while keeping signal refresh. Causal-memory pre-refresh rows also require positive benchmark-excess evaluator evidence. These pre-refresh rows are review-only Action Queue visibility and context-watchlist rows are watchlist pressure only; neither path can create BUY/SELL, portfolio, sizing, or broker authority.
- `all_advisory.sh` also runs `python -m advisory.recommendation_diagnostics --format text` after a successful post-refresh, so cron logs immediately show whether missing BUY rows are current, stale, blocked by technical confirmation, blocked by feature freshness, or suppressed by context/risk/portfolio handoff. Set `ADVISORY_SKIP_RECOMMENDATION_DIAGNOSTICS=true` only when you intentionally want to skip this read-only post-run report.
- Recommendation diagnostics uses the same context-watchlist freshness contract as `watchlist_builder --skip-if-current`: active `CONTEXT_OVERLAY_WATCH` rows are considered current only when the context signal-refresh sync state is `ok`, same-date, not newer than the watchlist snapshot, and active rows have complete `technical_confirmation_plan` metadata. If active rows are stale, unverified, or missing confirmation plans, the report recommends the bounded watchlist reconciliation before interpreting no-BUY behavior as regime or threshold suppression.
- Recommendation diagnostics also prints `Context watch confirmation`, a read-only summary of the `technical_confirmation_plan` stored on context-overlay watch rows. It shows how many active context-watch rows are waiting for breakout/pivot confirmation, constructive setup, feature freshness, risk, or lifecycle gates before any BUY can exist. It also prints active rows with/missing plans; older persisted rows that predate the plan field show `plans=0` and `active_missing_plan>0`, and the freshness status becomes `missing_technical_confirmation_plan`. Rerun the bounded context-watchlist reconciliation to refresh the metadata without granting portfolio or broker authority.
- Recommendation diagnostics also prints `Context watch technical precheck`, a read-only point-in-time coverage check over `advisory_technical_daily` for active `CONTEXT_OVERLAY_WATCH` symbols. The check accepts both legacy `daily` rows and Dhan equity `EQ` rows. If context rows are current but no constructive candidates exist, this separates a technical-feature coverage gap from a rule-engine candidate-generation gap. When coverage is missing, stale, or the latest refresh-status table shows per-symbol issues, the `Context-to-entry commands` section prefers a narrowed `python -m advisory.technical_features --targeted-context-refresh --from-date YYYY-MM-DD --to-date YYYY-MM-DD --symbols ...` command covering only symbols with missing, stale, or failed technical inputs; it falls back to all refreshable context-watch symbols only when that narrower set is unavailable. This fast path refreshes Dhan OHLCV and technical rows only, skips Sharpely fundamentals and peer sync, and still has no portfolio, sizing, or broker authority.
- Recommendation diagnostics also prints `Context source-family attribution`. This splits announcement, macro, bhavcopy, exchange, and theme context across source rows, conversion targets, reliability/policy suppression, watchlist rows, and review-only action pressure. The `top_blocked` field and Next steps name exact family/blocker pairs, so source-specific conversion gaps can be fixed before changing global regime/context policy.
- Theme-context sector joins use reviewed aliases from human theme labels to market-context sector codes, for example `capital_goods`, `engineering`, and `industrials` to `IN0702`, and `infrastructure` to `IN0701`/`IN0702`. Watchlist, watcher, signal-refresh, and action-reasoning loaders use the latest point-in-time market-context universe snapshot before the cutoff, so theme overlays do not require an exact same-date universe row. The context-watchlist builder contract version is `context_overlay_watchlist_v4_theme_sector_aliases`, so `--skip-if-current` forces one rebuild of older context-watch rows created before this alias mapping existed. Context-overlay signal refresh also persists and validates `context_overlay_signal_refresh_v2_theme_sector_aliases`, so old sync-state rows cannot skip the new conversion algorithm. These rows remain `watchlist_pressure_only` / `review_input_only` and cannot create broker or portfolio authority.
- When candidate-source rows are stale or missing and the context-to-entry funnel has a prerequisite repair, the top-level `Recommended commands` section now inherits those context-to-entry commands before full advisory fallback. This means automation and human operators see the same bounded repair order: reconcile stale context-watch rows or run targeted technical-feature refresh first, then run `python -m advisory.pipeline --date YYYY-MM-DD --start-at rules --stop-at actions --skip-peer-sync --skip-intraday --skip-rule-snapshot-refresh --skip-intraday-prefetch --include-lifecycle` when upstream inputs are already current enough to avoid the full wrapper. Use `./all_advisory.sh --date YYYY-MM-DD` when source data, early feature stages, or wrapper pre-refresh steps also need to run.
- Recommendation diagnostics also prints `Multi-context attribution`, a read-only split of action-row labels across breadth, macro stress, sector/symbol leadership, technical confirmation, and context-overlay alignment. Use this section before touching any regime flag: a no-BUY run with weak technical confirmation or missing sector leadership should not be treated as a single global-regime failure. It also prints `Signal-quality attribution`; if latest point-in-time signal-quality rows are `benchmark_beta_not_overlay_alpha` or `needs_benchmark_attribution`, the runbook points to `advisory.signal_quality_window_runner --include-split-reports` before any regime/context-policy tuning.
- If the `Multi-context attribution` line shows missing labels, the report prints a `Multi-context metadata repair commands` section. Start with the dry-run command there; it only previews reason-contract metadata repair and does not change actions, portfolio, sizing, ranking, or broker state.
- The `Market-context blocks` line now includes `blocked_labels`, the same multi-context label split scoped only to rows that were blocked by market context. Use this to distinguish hard macro-risk, weak breadth, weak technical confirmation, context-overlay conflict, and legacy regime-label artifacts before changing any market-context or regime setting.
- `advisory.action_recommender` now attaches market-context diagnostics to every consolidated row, not only positive broker candidates. Non-broker/review-only rows keep their existing action and execution boundary, but their reason contract can still explain breadth, macro stress, sector/symbol leadership, technical confirmation, and context-overlay alignment. This is annotation-only for review-only rows and has no portfolio, sizing, or broker authority.
- Use `python -m advisory.action_recommender --repair-reason-contracts --date YYYY-MM-DD --dry-run --format text` to preview backfilling these explanation fields on already-persisted rows. The text output reports `multi_context_missing_before`, `multi_context_missing_after`, and `multi_context_missing_delta` so operators can see whether the metadata repair will materially improve breadth/macro/sector/technical/context-overlay attribution before applying it. Run without `--dry-run` only when you want to update `raw_context_json`, `recommendation_reason_json`, and `reason_contract_status`; repair mode intentionally does not update action codes, allocations, ranking, portfolio state, or broker behavior.
- `advisory.technical_features --targeted-context-refresh` writes per-symbol outcomes to `advisory_technical_feature_refresh_status`. Use this table to distinguish Dhan sync failures, Dhan no-new-data, zero-row OHLCV syncs, and post-build missing technical rows before changing regime/context policy or technical thresholds.
- The context-watch technical precheck also checks latest Dhan daily OHLCV availability for the active context-watch symbols. If Dhan daily data lags the exchange trading calendar, the repair command is pinned to the latest Dhan-backed date and the report still surfaces newer failed refresh-status rows. This prevents operators from interpreting a no-BUY day as regime suppression when the input OHLCV feed has not advanced.
- Dhan daily failures from the targeted refresh are classified as `dhan_daily_sync_failed_no_history` or `dhan_daily_sync_failed_with_stale_history`. Treat no-history rows as hard input blockers. Treat stale-history rows as partial blockers: the symbol may still have older technical evidence, but should not be considered fully current until the Dhan/API issue is repaired or explicitly excluded.
- Recommendation diagnostics exposes the same split as `technical_input_blockers`. `hard_blocker_symbols` are symbols to suppress from context-watch entry interpretation until OHLCV history exists or the symbol is explicitly excluded. `stale_history_symbols` remain visible but are not fresh enough for a current entry decision. This diagnostic policy is read-only and has no portfolio or broker authority.
- Context watchlist rebuilds consume hard OHLCV blocker status. Hard blockers are persisted as rejected audit rows with `context_policy_effect=suppress_context_hard_ohlcv_blocker_no_trade_authority`, `current_state=REJECT`, and `watch_status=rejected`; they do not create active context-watch pressure until OHLCV history is repaired.
- Context watchlist rebuilds also consume stale-history OHLCV blocker status. Stale-history rows remain visible as active watch-only candidates, but their technical actionability score and technical priority bonus are forced to zero, and `watch_reasons_json` records `technical_fresh_entry_allowed=false` plus `technical_input_blocker_policy_effect=stale_history_watch_only_no_fresh_entry_authority`.
- Hard daily Dhan OHLCV failures with no stored history also create open `advisory_identity_issues` rows with `issue_type=dhan_ohlcv_history_unavailable`. These rows are not resolved by security-id mapping alone; `python -m advisory.identity_issues --apply` closes them only after local `dhan_ohlcv_daily` history exists. Context watchlist rebuilds consume open rows whose `attempt_count` is at least `WATCHLIST_CONTEXT_OVERLAY_OHLCV_ISSUE_EXCLUSION_ATTEMPTS` (default `2`) as hard blockers, so repeated no-history symbols are excluded from active context-watch intake until repaired.
- Context-overlay watchlist identity validation first uses `company_master` and then falls back to current `master_dhan_instruments` NSE/BSE equity symbols. The fallback is only an identity/provenance repair for review-only context rows; it does not add BUY, portfolio, or broker authority. Rows resolved this way carry `context_identity_validation_status=resolved_dhan_master_current_symbol`.
- Context-watchlist diagnostics use full-snapshot aggregate counts even when API/log samples are bounded. Treat `row_count`, `active_watch_count`, `breakout_watch_count`, `suppressed_count`, and `latest_load_ts` as full as-of snapshot fields; `sample_row_count` and `sample_limit` describe only the diagnostic sample shown.
- The text report prints the same contract in one line: `current`, `freshness`, `counts_scope`, full `rows`, `sample`, active/breakout/suppressed counts, and whether the watchlist as-of/load timestamp is fresh against context signal-refresh. Use this line before blaming a global regime or threshold policy for missing BUY recommendations.
- When candidate rows are stale or mismatched for a diagnostic date, recommendation diagnostics date-pins the repair command. It prefers the latest trading day on or before the diagnostic date, then candidate lookup/requested date, so a weekend or holiday diagnostic recommends `./all_advisory.sh --date YYYY-MM-DD` for the relevant trading session instead of mixing generic and date-pinned text. When no candidate rows exist anywhere, the first repair command remains plain `./all_advisory.sh` so the wrapper can initialize the latest advisory date normally. Stale technical-ignore artifacts also use current date-pinned rerun guidance before threshold or regime/context tuning.
- The text report now includes `Research context-policy alignment`. This line summarizes whether the run is exposed to broad single-regime hard gates, blocked by benchmark-attribution problems, missing layered context metadata, or aligned with layered context evidence. Use it before changing regime/context env flags: it is read-only and has no portfolio, config, or broker authority.
- The same selected trading-session date is used for the bounded pre-refresh commands in that runbook: `advisory.signal_refresh --from-context-overlays --asof-date YYYY-MM-DD`, `advisory.watchlist_builder --date YYYY-MM-DD --setup CONTEXT_OVERLAY_WATCH --rebuild --skip-if-current`, and `advisory.action_recommender --date YYYY-MM-DD --format text`. This keeps review-only context/watch/action visibility aligned with the authoritative `all_advisory.sh --date YYYY-MM-DD` rerun.
- If `all_advisory.sh` fails at `step=recommendation_diagnostics`, the advisory pipeline, operator snapshot, and trace-summary refresh already completed. Re-run only `python -m advisory.recommendation_diagnostics --format text` to debug the read-only report; do not treat that failure as a failed portfolio/advisory run unless earlier log markers also show an advisory-stage failure.
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
- `advisory.llm_decision_runner --persist --label-outcomes` through `all_llm_decisions.sh` at `22:00` on weekdays, after waiting for the post-close advisory lock to clear; review-only decision generation + outcome maturation, deterministic by default
- `advisory.research_evidence_runner` through `all_research_evidence.sh` at `22:20` on weekdays
- `advisory.event_policy_evaluator` at `23:10` on weekdays
- `advisory.technical_threshold_calibration` at `04:20` on Saturdays
- `all_ml.sh --run-signal-quality-window-runner --include-signal-quality-split-reports` at `03:10` on Sundays for research-only event-model prep/training plus preflight-gated stable multi-window signal-quality evidence and split diagnostics for unstable families

Hypothesis scans default to Codex-backed action-plan notes. Set `HYPOTHESIS_CRON_ARGS=--no-llm` to keep that cron path deterministic only.

The daily LLM decision cycle defaults to deterministic (`LLM_DECISION_CRON_ARGS=--no-llm`) so cron never makes LLM API calls. To opt a deployment into the LLM, set `LLM_DECISION_CRON_ARGS=""` and configure a model; decisions stay review-only regardless (`broker_execution_allowed=false`).

Not scheduled by default:

- `all_advisory_codex.sh`: self-fixing advisory wrapper; keep it manual so cron does not modify code unattended.
- `advisory.technical_threshold_promotion`: manual review/promotion helper; it creates patch guidance but should not run automatically.
- live Dhan order submission: controlled by execution settings and should remain explicitly gated; cron only prepares advisory/execution-planning state unless live trading is enabled deliberately.

Operator health and logs:

- `python -m advisory.operator_smoke` is the compact read-only preflight for DB/API/frontend/freshness/identity/signal-quality/cron trust. Use `python -m advisory.operator_health --skip-dhan` when you need the full detailed diagnostic payload.
- Smoke output truncates long nested details and bounds visible fix hints/current blockers by default. Tune `OPERATOR_SMOKE_COMPACT_LIST_LIMIT` and `OPERATOR_SMOKE_COMPACT_STRING_CHARS` only if the Operations page needs more context.
- It also checks local operator API latency and Dhan cached-token expiry metadata without initiating broker login.
- `fix_hints` are emitted in the health payload and surfaced in the Nuxt Health hub (`/health-hub`).
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

Dhan benchmark/index identity resolution accepts common aliases such as `NIFTY50`, `NIFTY 50`, `BANKNIFTY`, `NIFTY BANK`, `INDIAVIX`, and `INDIA VIX`. If one of these still lands in Identity Issues, refresh the Dhan master and run `python -m advisory.identity_issues --limit 100` before applying closure.

## Experimental time-series forecasts

Module: `advisory.ts_forecast_features`

This is a research-only forecast feature path over `dhan_ohlcv_daily`. It writes to `advisory_ts_forecasts_daily` and currently uses a dependency-free `naive_momentum_v1` baseline. The table schema is registry-managed by `20260611_advisory_ts_forecast_features_base` and is intentionally compatible with later TimesFM, Chronos, or Moirai adapters.

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

The experimental TS watchlist schema is registry-managed by `20260611_advisory_ts_forecast_workflow_base`.

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
- Ad hoc queries validate known local syntax pitfalls before opening Chrome or hitting Screener.in. Use `DMA 50` / `DMA 200`, not `50 Day Moving Average`, `200 Day Moving Average`, or compact `DMA200`.
- Operator UI Screener preview lives at `/screeners` and calls `POST /api/screeners/preview`. Validation-only mode is local and does not open Chrome; fetch-preview mode uses the authenticated Screener.in session with `persist=false`, returns bounded preview rows, and does not register a production screener.
- Screener contribution metrics live in `advisory.screener_coverage`, `GET /api/screeners/coverage`, and the same `/screeners` page. They read `advisory_screener_constituents`, `advisory_candidates`, and `advisory_action_recommendations` to attribute constituent, candidate, final-action, positive-action, manual-review, and exit rows by screener over a bounded lookback window.
- registered screener registry/snapshot schemas are registry-managed by `20260611_screenerin_registered_screeners_base`.
- ad hoc runs are stored in `screenerin_ad_hoc_query_runs`.
- normalized company rows for ad hoc runs are stored in `screenerin_ad_hoc_query_results`.
- ad hoc query run/result schemas are registry-managed by `20260611_screenerin_ad_hoc_query_base`.
- Screener fetch/parse failures are stored in `screenerin_parse_failures` before the original exception is re-raised. Rows include sanitized URL/query context, body excerpt, login/results-container flags, and error class/message for debugging bad syntax such as unsupported Screener.in field names.
- Screener failure schema is registry-managed by `20260611_screenerin_parse_failures_base`.
- `python -m advisory.operator_health --full --skip-dhan` summarizes recent Screener failure rows in Health fix hints, the Advisory Trust Gate, and Fallbacks & Degradation; default fast Health defers this history scan.
- parsed ad hoc output includes `company_name`, `ticker`, `company_url`, `rank`, and `metrics`.
- theme-to-screener discovery now uses only `config/investment_themes.yaml`; the older fallback theme config was removed.
- theme-to-screener mapping schema is registry-managed by `20260611_advisory_news_theme_screeners_base`.

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
./all_ml.sh --horizon-days 1 --to-date 2026-04-07 --cost-bps 25
./all_ml.sh --skip-signal-quality
./all_ml.sh --skip-signal-quality-promotion
./all_ml.sh --run-signal-quality-window-runner --include-signal-quality-split-reports --to-date 2026-06-20 --signal-quality-window-days 180 --signal-quality-step-days 90 --signal-quality-windows 4
./all_ml.sh --generate-causal-event-memory-config-previews --include-causal-event-memory-suppression-previews --max-causal-event-memory-config-previews 2 --to-date 2026-06-21
python -m advisory.event_model_promotion_check
./all_advisory.sh
```

Behavior:

- runs `advisory.event_model_data_prep`
- checks whether the requested horizon is `train_ready`
- skips training cleanly if coverage is still insufficient
- trains `advisory.event_meta_model` only when the readiness gate passes
- stores research-control metadata for point-in-time leakage control, fixed-config false-discovery control, and after-cost baseline comparison
- uploads trained model artifacts to S3 after successful training unless `--skip-s3-upload` or `EVENT_MODEL_ARTIFACT_UPLOAD_ENABLED=false` is set
- scores current events after training unless `--skip-score` is used
- after scoring, runs `advisory.signal_quality_evaluator` and `advisory.signal_quality_family_report` for research-only overlay/source-family evidence unless `--skip-signal-quality` is used
- accepts `--signal-quality-horizons`, default `5 10 20`, so the weekly ML run refreshes context-family lift evidence across swing horizons without changing live policy
- after the family report, runs `advisory.signal_quality_promotion --auto-family-candidates` unless `--skip-signal-quality-promotion` is used; this can create manual promotion-review rows for `candidate_helpful` source families but still does not edit config, action rules, portfolio rows, or broker behavior. The auto-family path also checks persisted fast context-overlay reliability and skips families currently classified as anything other than `candidate_helpful`, so mixed-horizon `inconsistent_or_horizon_sensitive` evidence cannot create stale promotion guidance from older one-window results.
- `--run-signal-quality-window-runner` additionally runs `advisory.signal_quality_window_runner --preflight-only` across rolling windows; only when that preflight returns `ready_for_persisted_window_run=true` does it run the persisted window runner. When this is enabled, the older one-window auto-promotion step is skipped and stable-gated promotion-review rows are owned by the window runner instead.
- `--include-signal-quality-split-reports` passes `--include-split-reports` only to the persisted window-runner command, never to preflight, so unstable source-family split diagnostics are attached when rows exist without querying stale data during readiness checks.
- `--generate-causal-event-memory-config-previews` passes `--generate-config-previews` to `advisory.causal_event_memory_evaluator` and can persist disabled `causal_event_memory_rule` config-preview audit rows for readiness candidates. Add `--include-causal-event-memory-suppression-previews` when you also want disabled suppression-review previews for `hurts_or_no_lift` causal-memory groups. This is opt-in; default weekly ML runs only evaluate causal memory. Even when enabled, the generated rows are disabled review artifacts and do not create runtime policy, action recommendations, portfolio rows, or broker orders.
- full Operator Health now checks persisted `advisory_context_overlay_reliability_summary` freshness and matured-row coverage, and emits a fix hint to run `advisory.context_overlay_reliability_report` when the fast context-overlay reliability evidence is missing, stale, or too sparse. It also treats signal-quality summary rows labelled `benchmark_beta_not_overlay_alpha` or `needs_benchmark_attribution` as research blockers and points to `advisory.signal_quality_window_runner --include-split-reports` before any promotion review.
- the final `research_evidence` JSON includes `signal_quality_split_queue`, `signal_quality_split_evaluator`, `causal_event_memory_config_preview_generation`, `manual_review_rows_may_be_created`, and `manual_review_row_reason`; if preflight is not ready, this is explicitly `false` with reason `preflight_not_ready_for_persisted_window_run`. The causal-memory preview summary exposes generated/error/candidate counts plus no-runtime/no-broker safety fields without requiring automation to parse the full evaluator payload.
- use `python -m advisory.event_model_promotion_check --format json` after weekly runs to see whether the evidence is ready for manual review; the JSON includes a research-only `scorecard` with usable/not-usable status, key metrics, failed gates, and explicit no-broker/no-auto-promotion boundaries
- normal advisory/adversarial-review runs ignore persisted event-model scores by default via `STOCKEY_EVENT_MODEL_SCORE_POLICY_MODE=research_only`; set `STOCKEY_EVENT_MODEL_SCORE_POLICY_MODE=promoted` or pass `--event-model-score-policy-mode promoted` only after the promotion check is usable, and the code still fails closed if the gate does not pass

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
- caps expensive announcement ingest/OCR targets with `WATCHER_ANNOUNCEMENT_MAX_INGEST_TARGETS`; skipped targets stay due and are not advanced
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

Use the per-symbol detail page (`/symbols/{symbol}`) or the `/api/symbols/{symbol}/trace` endpoint for readable stage cards. The raw CLI commands are useful when debugging DB rows or API responses.

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
python -m advisory.event_meta_model train --horizon-days 1 --cost-bps 25
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
- `complete_data.sh` is the lower-level ingestion component used before advisory runs. It now also builds `advisory_bhavcopy_evidence_daily`, `advisory_announcement_evidence`, and all review-only context overlay tables so UI/LLM/advisory/research paths can read compact evidence instead of scanning raw bhavcopy, announcement text, exchange-event, macro, or news-theme sources.

Run context-overlay refresh directly when you need research coverage without the full downloader/parser chain:

```bash
python -m advisory.context_overlay_refresh --dry-run --from-date 2026-06-01 --to-date 2026-06-20
python -m advisory.context_overlay_refresh --from-date 2026-06-01 --to-date 2026-06-20
```
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
python -m advisory.technical_threshold_calibration --dry-run --from-date 2026-01-01 --to-date 2026-05-01 --horizons 5 10 20 --max-configs 512 --progress-every 128
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

`data.economictimes.rss` stores raw ET RSS items in `economictimes_rss_items`. Its item schema is registry-managed by `20260611_economictimes_rss_items_base`.

`advisory.news_watch` matches ET RSS items onto the active watchlist and writes `advisory_news_events`.

`advisory.symbol_trace` reads the advisory state tables and produces a single-symbol trace across screener, rule, watch, event, allocation, portfolio, lifecycle, and execution stages.

`advisory.setup_trace` reads the advisory state tables and produces a setup-level trace across screener universe, candidates, rejections, watch, event, allocation, portfolio, lifecycle, and execution stages.

`advisory.dashboard` shows all configured setups in one compact table or JSON payload using the same setup-trace logic underneath.

`advisory.llm_event_evaluator` reads both `advisory_watch_events` and `advisory_news_events`, joins `advisory_announcement_evidence` when official filings exist, and explicitly falls back to `announcement_pipeline_documents` only when compact evidence has not been built for that `unique_id`. It adds point-in-time regime, technical, fundamentals, exchange-feature, and compact bhavcopy evidence context before writing `advisory_event_evaluations` and `advisory_event_risks`.

`advisory.company_memory_review` writes `advisory_company_memory_reviews` as a review-only company-memory input. Its schema is registry-managed by `20260611_advisory_company_memory_reviews_base`. It reads compact evidence plus latest technical, event-policy, wait-signal, and action rows for a bounded symbol set. It defaults to deterministic V1 and only uses Codex when `--llm` or `COMPANY_MEMORY_REVIEW_LLM_ENABLED=true` is set. These rows do not grant execution authority.

```sh
python -m advisory.company_memory_review --dry-run --symbols RELIANCE --limit 1
python -m advisory.company_memory_review --dry-run --limit 12
python -m advisory.company_memory_review --dry-run --symbols RELIANCE --llm
```

The operator API attaches the latest company-memory review to matching action rows, including compact `/api/actions?compact=true` responses. The Nuxt Action Queue and Symbol Detail pages show this as read-only company-memory evidence.

`advisory.signal_quality_evaluator` is the research comparator for deciding whether non-price evidence is helping. It starts from `advisory_candidates`, joins only point-in-time rows from event policy, compact bhavcopy evidence, company-memory review tables, review-only context overlay tables, and final action rows that actually recorded trusted context-rule adjustments, attaches future Dhan OHLCV returns with a shared point-in-time return contract that excludes same-day prices, and writes variant-level results for `technical_only`, `technical_plus_event`, `technical_plus_bhavcopy`, `technical_plus_company_memory`, `technical_plus_all`, `technical_plus_context_overlay`, `technical_plus_all_context`, per-source-family context variants for announcement, exchange, bhavcopy, theme, and macro overlays, and `technical_after_trusted_context_rules`. Context overlays include direct symbol overlays from announcement, exchange-event, and bhavcopy context tables plus sector overlays from news-theme and macro context tables mapped through `advisory_market_context_universe_daily`. Trusted context-rule adjustment evaluation measures whether the disabled-by-default reviewed runtime consumer would have improved outcomes by excluding trusted negative-context downgrades and including trusted positive WATCH boosts. It does not change live action policy or broker execution.

```sh
python -m advisory.signal_quality_evaluator --dry-run --from-date 2026-01-01 --to-date 2026-05-01 --horizons 5 10 20
python -m advisory.signal_quality_evaluator --from-date 2026-01-01 --to-date 2026-05-01 --horizons 5 10 20
python -m advisory.signal_quality_evaluator --dry-run --from-date 2026-01-01 --to-date 2026-05-01 --horizons 5 10 20 --context-lookback-days 30
```

Use `advisory.signal_quality_family_report` after the evaluator runs to answer which context source families are actually helping after costs. It reads the latest signal-quality summary rows, ranks announcement/exchange/bhavcopy/theme/macro context families plus the `trusted_context_rules` runtime-adjustment family by lift versus `technical_only`, and classifies each as `candidate_helpful`, `monitor`, `needs_more_data`, `negative_after_cost`, `hurts_or_no_lift`, or `inconsistent_or_horizon_sensitive`. A family becomes `inconsistent_or_horizon_sensitive` when enough matured horizon rows contain both helpful and harmful/negative evidence, so it remains context/watch evidence but is not a promotion candidate. The report also includes `market_participation_diagnostic`; if the technical-only baseline is positive after costs but no context family is ready, it flags underparticipation risk and tells operators to inspect candidate loss after screening/risk/lifecycle instead of loosening a global regime gate. This is research-only; it does not promote overlays or change policy.

```sh
python -m advisory.signal_quality_family_report --format text
python -m advisory.signal_quality_family_report --horizons 5 10 20 --format json
python -m advisory.signal_quality_family_report --evaluated-at 2026-05-01T00:00:00Z --horizons 5
```

Use `advisory.signal_quality_window_runner` when deciding whether a context family is repeatedly useful instead of just lucky in the latest run. It runs fixed rolling windows, builds a source-family report per window, emits a stability report that classifies families as `stable_candidate`, `one_window_candidate`, `inconsistent_or_regime_sensitive`, or `needs_more_data_or_monitor`, and returns a compact `promotion_gate` decision for each family. Window-level `inconsistent_or_horizon_sensitive` rows are treated as unstable and become `inconsistent_or_regime_sensitive` at the rolling-window layer. Inconsistent and one-window families include `split_guidance` with source-family-specific axes such as announcement event class, exchange event type/side, bhavcopy pressure class, theme/sector, macro signal/sector, or trusted rule id. In persisted mode it can create promotion-review rows only for families classified as `stable_candidate`; one-window or inconsistent families remain research/watch evidence. Dry-run mode never writes evaluator rows or promotion-review rows.

```sh
python -m advisory.signal_quality_window_runner --to-date 2026-06-20 --window-days 180 --step-days 90 --windows 4 --horizons 5 10 20 --preflight-only
python -m advisory.signal_quality_window_runner --to-date 2026-06-20 --window-days 180 --step-days 90 --windows 4 --horizons 5 10 20 --min-stable-windows 2 --dry-run
python -m advisory.signal_quality_window_runner --to-date 2026-06-20 --window-days 180 --step-days 90 --windows 4 --horizons 5 10 20 --min-stable-windows 2
python -m advisory.signal_quality_window_runner --to-date 2026-06-20 --window-days 180 --step-days 90 --windows 4 --horizons 5 10 20 --min-stable-windows 2 --include-split-reports
python -m advisory.signal_quality_window_runner --from-date 2026-01-01 --to-date 2026-06-20 --horizons 5 10 20 --skip-auto-promotion
```

Use `--preflight-only` before a persisted run. It forces dry-run/no-promotion behavior and returns a compact `decision`, `ready_for_persisted_window_run`, stable family list, and per-window coverage counts instead of the full evaluation payload. Proceed to a persisted run only when it reports `ready_for_persisted_window_run=true`.

Use `--include-split-reports` only on persisted runs when you want the runner to attach source-family split diagnostics automatically for `one_window_candidate` and `inconsistent_or_regime_sensitive` families. It reads the persisted evaluator rows for the latest window and adds compact context-class/direction candidates under `split_diagnostics`. The diagnostics payload also includes a top-level `research_queue` aggregating per-family split recommendations so the next research slice can be picked without reading every nested family report. In dry-run or preflight mode this field is reported as skipped because no persisted rows are written. Split diagnostics are research-only and cannot change ranking, action policy, portfolio state, or broker behavior.

Use `advisory.signal_quality_split_report` when the family report or window runner says a source family is inconsistent. It reads selected matured evaluation rows and splits them by compact context class plus direction, for example `ORDER_WIN|positive` versus `REGULATORY_NOTICE|negative`. When no `--evaluated-at` is supplied, the loader chooses the latest window with context-family rows, not a newer technical-only-only window, then joins matching `technical_only` rows for the same timestamp. A split is classified as `candidate_split_helpful` only when it has enough matured rows, positive after-cost return, and positive lift over matching `technical_only` rows. Positive raw return without technical-only lift is classified as a negative control, and missing technical baseline evidence fails closed as collect-more-data. The output includes `research_recommendations` that mark each split as `build_narrower_evaluator_variant`, `track_as_negative_control`, or `collect_more_data`, plus a `research_queue` with stable recommendation ids and bucket counts for the next research slice. This is research-only and helps decide what narrower evaluator variant or hypothesis to build next; it does not write rows or change policy.

```sh
python -m advisory.signal_quality_split_report --source-family announcement_context --horizons 5 10 20 --format text
python -m advisory.signal_quality_split_report --source-family macro_context --min-matured-rows 10 --format json
python -m advisory.signal_quality_split_report --horizons 5 10 20 --queue-only --format json
```

Use `advisory.signal_quality_split_evaluator` after a split queue exists. It consumes `build_narrower_evaluator_variant` and negative-control split specs, rebuilds research-only narrowed variants such as `technical_plus_announcement_context_split_order_win_positive`, compares only the selected split subset with matching `technical_only` rows for the same symbol/date/setup/horizon keys, and writes `advisory_signal_quality_split_evaluations` plus `advisory_signal_quality_split_eval_summary`. Non-matching source-family rows are not allowed to dilute the baseline. If matching technical-only baseline coverage is missing, the split summary is classified as `technical_baseline_unavailable` and cannot become helpful. Missing base signal-quality tables return an explicit `evidence_unavailable` meta state instead of failing first-time setups. Normal output also includes a `stability_report` that classifies persisted narrowed split summaries as `stable_candidate`, `unstable_or_horizon_sensitive`, `harmful_negative_control`, or `needs_more_data`. Stability also fails closed when any persisted window for that split has `technical_baseline_unavailable`; such windows are counted as `baseline_unavailable_window_count` and prevent review generation until the matching baseline is rebuilt. `--generate-stability-reviews` can create manual review rows for stable narrowed split candidates in `advisory_signal_quality_split_promotion_reviews`; these rows contain disabled review-candidate patches with allowed watch/review effects and forbidden buy/broker/portfolio effects. This is still research-only; it does not change source-family rules, action ranking, portfolio state, or broker behavior. `all_ml.sh --include-signal-quality-split-reports` runs it through `advisory.model_training_runner` after the split queue.

```sh
python -m advisory.signal_quality_split_evaluator --dry-run --horizons 5 10 20 --top-n 10 --format text
python -m advisory.signal_quality_split_evaluator --horizons 5 10 20 --top-n 10 --format json
python -m advisory.signal_quality_split_evaluator --source-family announcement_context --dry-run --horizons 10
python -m advisory.signal_quality_split_evaluator --stability-report --horizons 5 10 20 --format json
python -m advisory.signal_quality_split_evaluator --stability-report --generate-stability-reviews --dry-run --horizons 5 10 20 --format json
```

The operator API exposes the latest persisted signal-quality run at `/api/signal-quality`. The Nuxt operator app shows it at `/signal-quality`, including overlay coverage, lift versus `technical_only`, and selected matured examples. Treat this as manual review evidence only; production action rules are unchanged until a separate explicit config/rule change is approved.

Action reason contracts also attach the latest point-in-time source-family reliability classification to `context_overlay_summary` when recent context overlays are present. They prefer persisted fast reliability from `advisory_context_overlay_reliability_summary`, then recompute from fast context evaluator summaries, then fall back to the full `advisory.signal_quality_family_report`. Runtime consumers prefer the explicit `runtime_policy_contract` from those reliability rows when available: watcher synthetic context target selection, OHLCV watcher exchange-context priority, and context watch priority require `allowed_runtime_uses.watch_priority=true` for positive/watch pressure, hypothesis/playbook risk-off softening requires `allowed_runtime_uses.watch_priority=true`, company-memory positive/watch pressure requires `allowed_runtime_uses.watch_priority=true`, action-consolidation positive context softening and trusted overlay-rule consumption also require `allowed_runtime_uses.watch_priority=true`, signal-quality promotion review creation requires `allowed_runtime_uses.watch_priority=true` before broad positive/watch family promotion is considered, and watcher negative context target selection plus signal-refresh/company-memory de-risk pressure require `allowed_runtime_uses.de_risk_review=true` or an exact protective context class. OHLCV exchange-context priority may use protective/de-risk context too, but only to order already-monitored symbols, not to create actions. Older rows without the contract fall back to legacy classification semantics for compatibility. If disabled/active `signal_quality_overlay_rules` exist in config, matching source-family rules are also attached as reviewed config annotations with `review_only_annotation_no_ranking_change`. Each reviewed rule also carries `runtime_reliability_gate=runtime_policy_contract_watch_priority_required`, `runtime_stability_gate=stable_candidate_required`, and `runtime_eligible_for_signal_quality_overlay_consumer`; without both current runtime-contract watch-priority permission and stable cross-window `stable_candidate` metadata, the disabled-by-default runtime consumer will ignore that rule even if config marks it `trusted_overlay`. Operator Health reads the same contract and exact-class diagnostics from persisted reliability `report_json`, so the Health row matches the real runtime boundary. WATCH rows loaded from `CONTEXT_OVERLAY_WATCH` also preserve `watch_source`, `context_source`, `context_overlay_id`, `context_authority_scope`, `context_policy_effect`, and reliability fields in the `watchlist_context` evidence section. Context-overlay action reasoning is schema-tolerant for optional columns such as `production_status`, `authority_scope`, `overlay_id`, `pressure_score`, class labels, and reason text; required symbol/date/direction fields still have to exist. This helps explain whether an announcement/exchange/bhavcopy/theme/macro overlay family has historically helped after costs and whether an operator has manually added a reviewed config marker. It is explicitly `annotation_only_no_ranking_change` / `watch_only_no_buy_authority`; it does not create context-driven BUY/SELL authority, portfolio state changes, or broker execution.

Use `advisory.signal_quality_promotion` only after the evaluator has enough matured rows. It writes manual review/decision audit rows and copyable patch guidance; it does not edit config, action rules, portfolio rows, or broker behavior. Source-family variants such as `technical_plus_announcement_context` must also pass the source-family report gate as `candidate_helpful`; `needs_more_data`, `inconsistent_or_horizon_sensitive`, and harmful classifications fail closed to `needs_more_data`. Source-family promotion also checks persisted fast context-overlay reliability and narrowed split stability evidence: if the latest fast reliability runtime contract does not allow `watch_priority`, or if `advisory.signal_quality_split_evaluator` has classified any same-family/horizon split as `harmful_negative_control`, broad family promotion is blocked or skipped even if the family average looks helpful. Approved signal-quality preview patches render `signal_quality_overlay_rules` with `authority=review_input_only`, `policy_auto_promotion_allowed=false`, and `broker_execution_allowed=false`; `advisory.setup_registry.load_signal_quality_overlay_rules` and `advisory.config_change_assistant.verify_preview_against_config` can verify that a manually-applied rule exists, but no live action policy consumes it automatically. The `technical_after_trusted_context_rules` variant is different: it evaluates existing trusted runtime-rule adjustments, requires enough adjusted selected/matured/symbol coverage, and returns review guidance for the existing per-source-family trusted rules only. It intentionally does not render a generic `signal_quality_overlay_rules` patch because that would not match what the runtime consumer actually consumes; `advisory.config_change_assistant` also rejects generic config-diff generation for this operation and verifies any existing preview as `not_applicable_existing_runtime_rules_only`.

```sh
python -m advisory.signal_quality_promotion --evaluated-at 2026-05-01T00:00:00Z --horizon-days 5 --variant technical_plus_all --dry-run
python -m advisory.signal_quality_promotion --auto-family-candidates --horizons 5 10 20 --dry-run
```

The operator API exposes this workflow at `/api/signal-quality/promotion-review`, `/api/signal-quality/promotion-reviews`, and `/api/signal-quality/promotion-review/decision`. The Nuxt `/signal-quality` page can create a review, show the Advisory Trust Gate context, record an approve/reject/needs-more-data decision, and copy patch guidance for a separate reviewed code/config change.

Use `advisory.research_evidence_runner` when you want to refresh research evidence after advisory/watchers have created rows, without rerunning event-model prep/training/scoring. It runs the same research-only evaluators used by model training: signal quality, optional narrowed split evaluation, optional context-overlay signal backfill, negative-pressure, context-watch, adversarial-review, action-transition, causal-memory, provenance, and context-overlay reliability. By default it disables signal-quality promotion review rows, does not create config previews, and never changes recommendations, portfolio rows, config, or broker behavior. Its readiness summary scans child payloads for broker execution, policy auto-promotion, portfolio/config mutation, or runtime-consumer authority and marks the run `error` with `authority_violation` blockers if any child reports those flags. Operator Health parses those blocker components from `research_evidence.log` and adds them to fix hints and the Trust Gate. Add `--allow-manual-review-rows` only when you explicitly want stable signal-quality promotion review rows.

The JSON output includes `research_evidence.readiness_summary` and `research_evidence.component_status`. Use `readiness_summary.status` as the first operator signal: `candidate_evidence_available` means one or more components produced research candidates for manual review; `not_ready` means evidence is still empty, immature, beta-only, or under-baselined; `all_skipped` means the run was diagnostic and did not execute evidence components. Operator Health also parses the latest `logs/cron/research_evidence.log` into `research_evidence_run_summary`, so missing/failed/not-ready daily evidence refreshes are visible without opening raw cron logs. These fields are research-only and do not grant policy, portfolio, or broker authority.

```sh
./all_research_evidence.sh
python -m advisory.research_evidence_runner --include-signal-quality-split-reports --to-date 2026-06-20
python -m advisory.research_evidence_runner --skip-signal-quality --skip-event-evidence-store --skip-causal-event-memory --to-date 2026-06-20
```

Use `advisory.negative_pressure_evaluator` after signal refresh has created review-only `REDUCE_EXPOSURE_REVIEW` rows from negative announcement, exchange, bhavcopy, macro, or theme overlays. It labels each warning with future Dhan OHLCV returns using a point-in-time contract: the entry price is the first available close strictly after the signal date, and the exit is the horizon-th available close after that. For negative pressure, `avoided_return_after_cost` is positive when reducing/exiting would have helped versus holding. The evaluator writes research-only row and summary tables and does not change action recommendations, portfolio rows, config, or broker behavior. `all_ml.sh` runs it through `advisory.model_training_runner` unless `--skip-negative-pressure-evaluator` is passed. Before the fast context evaluators run, `advisory.model_training_runner` also performs a bounded review-only context-overlay signal backfill through `advisory.signal_refresh --from-context-overlays`, so research evidence is not empty only because cron/watchers did not persist historical context rows. By default the backfill ends at `--to-date - (ceil(max(signal_quality_horizons) * 7 / 5) + 3 calendar days)` because labels use trading bars, not calendar days; set `CONTEXT_OVERLAY_SIGNAL_BACKFILL_MATURITY_BUFFER_DAYS=0` or pass `--context-overlay-signal-backfill-maturity-buffer-days 0` only for diagnostic runs that intentionally seed fresh immature rows. Tune breadth with `--context-overlay-signal-backfill-days`, `--context-overlay-signal-backfill-step-days`, and `--context-overlay-signal-backfill-limit`; disable it with `--skip-context-overlay-signal-backfill`.

```sh
python -m advisory.negative_pressure_evaluator --horizons 5 10 20 --format json
python -m advisory.negative_pressure_evaluator --from-date 2026-05-01 --to-date 2026-06-20 --dry-run --format text
```

Use `advisory.context_watch_evaluator` after signal refresh has created review-only `WATCH` rows from positive/watch announcement, exchange, bhavcopy, macro, or theme overlays. It uses the same point-in-time return contract and labels whether the watch row later found after-cost upside. For positive/watch pressure, `watch_return_after_cost` is positive when the overlay-led watch candidate rose after estimated costs. The evaluator writes research-only row and summary tables and does not change action recommendations, portfolio rows, config, or broker behavior. `all_ml.sh` runs it through `advisory.model_training_runner` unless `--skip-context-watch-evaluator` is passed.

```sh
python -m advisory.context_watch_evaluator --horizons 5 10 20 --format json
python -m advisory.context_watch_evaluator --from-date 2026-05-01 --to-date 2026-06-20 --dry-run --format text
```

Use `advisory.context_overlay_reliability_report` to combine fast positive/watch context outcomes with negative de-risk pressure outcomes by source family. It maps positive watch outcomes into the same reliability labels used by context-overlay watchlist intake (`candidate_helpful`, `monitor`, `needs_more_data`, `hurts_or_no_lift`, `negative_after_cost`) and keeps negative pressure as protective research evidence. If one matured horizon is helpful while another is harmful or negative after costs, the fast report classifies the family as `inconsistent_or_horizon_sensitive` instead of `candidate_helpful`. The report also emits `runtime_policy_contract` on family, class, and watch-state aggregates, so downstream code can read explicit boundaries instead of inferring from labels: helpful context can only raise watch priority, protective context can only create de-risk review evidence, inconsistent context requires splitting, and harmful/unattributed context stays suppressed annotation-only. Non-dry runs persist compact source-family rows to `advisory_context_overlay_reliability_summary`. `advisory.watchlist_builder` prefers the persisted fast report when available, recomputes from evaluator summaries when it is missing, then falls back to the full `advisory.signal_quality_family_report` evidence. This only gates review-only watchlist intake; it does not create BUY, portfolio, config, or broker authority.

For context-overlay signal-refresh backfills, portfolio exposure is point-in-time: `advisory.signal_refresh` selects the latest portfolio row per symbol at or before the requested `asof_date`, then applies active/allocation filters. Older active rows are ignored after a later closed/exited row exists, so historical de-risk labels do not use stale exposure.

```sh
python -m advisory.context_overlay_reliability_report --horizons 5 10 20 --format json
python -m advisory.context_overlay_reliability_report --asof-date 2026-06-20 --format text
python -m advisory.context_overlay_reliability_report --dry-run --horizons 5 --format json
```

Use `advisory.ts_forecast_promotion_check` after `advisory.ts_forecast_paper_portfolio` has enough matured rows. It is read-only and never writes action recommendations, portfolio rows, config changes, or broker orders.

```sh
python -m advisory.ts_forecast_promotion_check --format json
python -m advisory.ts_forecast_promotion_check --model-name timesfm_2p5_200m --horizon-days 10
```

Default gates require enough evaluated paper trades, positive win rate, positive average after-cost return, lift versus the simple momentum baseline, low advisory exit-conflict rate, enough distinct forecast dates, and enough symbols. A passing result means eligible for manual review only, not automatic policy integration.

Use `advisory.ts_forecast_promotion` only after the read-only TS promotion gate returns `review_candidate`. It writes manual review/decision audit rows and copyable TS forecast review-rule guidance; it does not edit config, action rules, portfolio rows, or broker behavior.

```sh
python -m advisory.ts_forecast_promotion --model-name timesfm_2p5_200m --horizon-days 10 --dry-run
```

The operator API exposes this workflow at `/api/research/ts-forecast-promotion-review`, `/api/research/ts-forecast-promotion-reviews`, and `/api/research/ts-forecast-promotion-review/decision`.

Use `advisory.event_policy_promotion` only after `advisory.event_policy_evaluator` has enough matured rows for a specific `group_type/group_value`. It writes manual review/decision audit rows and copyable event-policy review-rule guidance; it does not edit config, action rules, portfolio rows, or broker behavior.

```sh
python -m advisory.event_policy_promotion --evaluated-at 2026-05-01T00:00:00Z --horizon-days 5 --group-type source_quality --group-value high --dry-run
```

The operator API exposes this workflow at `/api/event-policy/promotion-review`, `/api/event-policy/promotion-reviews`, and `/api/event-policy/promotion-review/decision`. The Nuxt Event Inbox can create a manual-only review from an actionability calibration group, list pending reviews, and record approve/reject/needs-more-data decisions. These flows record review/decision evidence only; they do not apply config, action rules, portfolio rows, or broker behavior.

`advisory.config_change_assistant` turns approved review decisions into reviewed unified diffs. It is intentionally one step short of applying the change: the generated diff is an audit artifact and copyable operator aid only.

```sh
python -m advisory.config_change_assistant --source-type technical_threshold --setup-id EVENT_OPPORTUNITY_V1 --config-id CONFIG_ID --dry-run
python -m advisory.config_change_assistant --source-type signal_quality_overlay --evaluated-at 2026-05-01T00:00:00Z --horizon-days 5 --variant technical_plus_all --dry-run
python -m advisory.config_change_assistant --source-type signal_quality_split_overlay --horizon-days 10 --variant technical_plus_announcement_context_split_order_win_positive --source-family announcement_context --split-axis context_class_direction --split-value 'ORDER_WIN|positive' --dry-run
python -m advisory.config_change_assistant --source-type causal_event_memory_rule --horizon-days 5 --context-source bhavcopy_context --context-class circuit_risk --event-type circuit_risk --direction negative --event-state negative_derisk_pressure --matured-count 24 --dry-run
python -m advisory.config_change_assistant --source-type event_policy_review_rule --evaluated-at 2026-05-01T00:00:00Z --horizon-days 5 --group-type source_quality --group-value high --dry-run
python -m advisory.config_change_assistant --source-type ts_forecast_review_rule --model-name timesfm_2p5_200m --horizon-days 10 --dry-run
```

`signal_quality_split_overlay` is the narrowed-split version of `signal_quality_overlay`: it is meant for approved stable split review rows from `advisory.signal_quality_split_evaluator --generate-stability-reviews`, for example one announcement event class plus direction. The rendered config row preserves `context_source_family`, `split_axis`, `split_value`, optional `context_class`, and `direction`, and still writes `authority=review_input_only`, `status=disabled_review_candidate`, `policy_auto_promotion_allowed=false`, and `broker_execution_allowed=false`.

`causal_event_memory_rule` is the compact causal-memory version of the same disabled overlay-review artifact. It turns a `candidate_helpful` memory evaluator group such as `bhavcopy_context|circuit_risk|negative|5d` into a disabled `signal_quality_overlay_rules` preview with causal source/class/type/direction/state fields, `memory_policy_effect=review_only_low_weight_context_no_buy_sell_authority`, `policy_auto_promotion_allowed=false`, and `broker_execution_allowed=false`. It does not create a runtime consumer, action recommendation, portfolio row, or broker order.

`advisory.causal_event_memory_evaluator` can also generate those disabled previews directly from its readiness contract:

```sh
python -m advisory.causal_event_memory_evaluator --from-date 2026-06-01 --to-date 2026-06-21 --horizons 1 5 --generate-config-previews --include-suppression-config-previews --max-config-previews 2 --dry-run
```

With `--dry-run`, preview rows are returned in `config_preview_generation` but not persisted. Without `--dry-run`, only disabled config-preview audit rows are persisted; the evaluator still does not create runtime policy, action recommendations, portfolio rows, or broker orders. Helpful candidate groups render `causal_event_memory_low_weight_review_rule`; harmful/no-lift groups render `causal_event_memory_suppression_review_rule` with `memory_policy_effect=review_only_suppress_memory_context_no_sell_authority`.

The operator API exposes recent previews at `/api/config-change/previews` and diff generation at `/api/config-change/technical-threshold-preview`, `/api/config-change/signal-quality-preview`, `/api/config-change/event-policy-preview`, and `/api/config-change/ts-forecast-preview`. It also exposes audit-only application decisions at `/api/config-change/applications` and `/api/config-change/application-decision`. The Event Inbox shows a `Reviewed Diff` action for approved event-policy promotion reviews. Generated signal-quality, narrowed-split, event-policy, and TS forecast diffs add disabled review-rule entries only; they do not change live policy. Application decisions record whether an operator approved, rejected, requested more data, or marked the reviewed diff as manually applied; Stockey does not edit config files or enable policy through this endpoint.

After manually applying a disabled TS forecast review-rule diff, inspect `/api/research/ts-forecast-review-rules` to confirm the rule parses and remains `review_input_only`, broker-disabled, and excluded from automatic policy promotion. If you record `marked_applied` through `/api/config-change/application-decision`, the backend verifies the matching TS rule when possible and stores the result in `advisory_config_change_applications`; this remains audit-only.

`advisory.risk_engine` reads `advisory_event_evaluations`, joins the latest point-in-time technical and fundamental context, and writes `advisory_allocations` with risk bucket, conviction bucket, suggested INR allocation, and invalidation guidance. Its base-candidate fallback from `advisory_watchlist` is defensive: only `current_state=PASS_NOW` with `technical_state=BUY_TRIGGERED` can allocate capital; `WATCH_BREAKOUT`/watch-only rows and stale `PASS_NOW` rows with unconfirmed technical state stay rejected/watch-only with explicit notes. Allocation rows persist `candidate_state`, `current_state`, `is_base_candidate_fallback`, `technical_state`, `technical_trigger_type`, `technical_trigger_note`, and `technical_entry_confirmed`, so portfolio planning can independently defer unconfirmed base fallback rows. Its allocation schemas are registry-managed by `20260611_advisory_allocations_base` and `20260621_advisory_allocations_technical_confirmation`.

`advisory.adversarial_review` writes deterministic review/veto rows to `advisory_event_reviews` before risk sizing. Its review schema is registry-managed by `20260611_advisory_event_reviews_base`.

`advisory.adversarial_review_evaluator` evaluates those review rows against realized point-in-time Dhan OHLCV forward returns and point-in-time benchmark returns after costs. It treats `clear` / `no_veto` rows as baselines and scores `veto`, `penalize`, and `review_manual` as adversarial interventions. Positive `avoided_loss_return_after_cost` means the intervention avoided a bad forward return; positive `excess_avoided_loss_return_after_cost` means the reviewed stock also underperformed the benchmark after costs. High raw or benchmark-excess false-positive rate means the reviewer may be blocking good trades. Output includes a `research_queue` with `candidate_keep_or_tighten`, beta-only `keep_research_only_collect_excess_veto_evidence`, `candidate_relax_or_review_false_positives`, `needs_more_data`, and baseline buckets so rule work can be picked from evidence rather than prose. Raw-helpful vetoes that only worked because the benchmark also fell are labelled `benchmark_beta_not_veto_alpha` and stay research-only. This is research-only and does not change event policy, risk sizing, action recommendations, portfolio rows, config, or broker behavior. `all_ml.sh` runs it through `advisory.model_training_runner` unless `--skip-adversarial-review-evaluator` is passed.

```sh
python -m advisory.adversarial_review_evaluator --dry-run --horizons 5 10 20
python -m advisory.adversarial_review_evaluator --from-date 2026-05-01 --to-date 2026-06-20 --horizons 5 --min-matured-rows 5
```

`advisory.announcement_watch` writes active announcement-watch state to `advisory_watchlist` and matched announcement events to `advisory_watch_events`. Its watch output schemas are registry-managed by `20260611_advisory_announcement_watch_outputs_base`.

`advisory.news_watch` writes matched RSS/news rows to `advisory_news_events`. Its news event schema is registry-managed by `20260611_advisory_news_events_base`.

`advisory.config_change_assistant` writes reviewed config preview diffs to `advisory_config_change_previews` and audit-only application decisions to `advisory_config_change_applications`. The preview schema is registry-managed by `20260611_advisory_config_change_previews_base`; the application-audit schema is registry-managed by `20260612_advisory_config_change_applications_base`.

`advisory.intraday_features` writes intraday confirmation features to `advisory_intraday_features_daily` and ensures supporting Dhan OHLCV read indexes. Its feature-cache schema is registry-managed by `20260611_advisory_intraday_features_base`.

`advisory.watchlist_builder` writes the full advisory watchlist to `advisory_watchlist`. Its builder-owned watchlist schema is registry-managed by `20260611_advisory_watchlist_base`.

`advisory.api.app` writes operator API errors, operator command runs, and manual-review decisions to API audit tables. These schemas are registry-managed together by `20260611_advisory_operator_api_audit_base`. Operator API error messages, traceback tails, request context, and cron-log tails returned by Operations routes are redacted for common broker/API secrets before reaching the frontend.

`advisory.technical_threshold_promotion` writes manual technical-threshold promotion reviews and operator decisions. Its review/decision schemas are registry-managed by `20260611_advisory_technical_threshold_promotion_base`.

`advisory.signal_quality_promotion` writes manual signal-quality overlay promotion reviews and operator decisions. Its review/decision schemas are registry-managed by `20260611_advisory_signal_quality_promotion_base`. For aggregate context variants such as `technical_plus_context_overlay` and `technical_plus_all_context`, the reviewer also reads `advisory_signal_quality_evaluations` and fails closed to `needs_more_data` unless enough selected, matured, distinct-symbol, and distinct source-family rows actually used context overlays. For source-family variants such as `technical_plus_announcement_context`, it fails closed unless that specific family has enough selected, matured, and distinct-symbol coverage, `advisory.signal_quality_family_report` classifies the family as `candidate_helpful` for the same evaluated-at/horizon window, and the latest fast context-overlay runtime contract allows watch-priority use when that reliability row exists. For `technical_after_trusted_context_rules`, it fails closed unless enough rows were actually adjusted by trusted runtime rules and emits comments to review those existing trusted rules rather than a generic config patch; config-change preview generation fails closed for that operation to prevent applying a rule shape the runtime consumer would not consume. The `--auto-family-candidates` mode reads the latest family report and creates promotion review rows only for family/horizon rows classified as `candidate_helpful` and not blocked by fast runtime-contract or split negative-control gates; it still does not edit config, action rules, portfolio rows, or broker behavior. This prevents sparse news/macro/announcement/bhavcopy/exchange context rows from being promoted based on aggregate lift alone.

`advisory.ts_forecast_promotion` writes manual TS forecast promotion reviews and operator decisions. Its review/decision schemas are registry-managed by `20260612_advisory_ts_forecast_promotion_base`.

`advisory.portfolio_engine` reads `advisory_allocations`, ranks approved allocations by conviction/risk/liquidity-aware priority, applies portfolio-level capital and setup caps, and writes `advisory_portfolio_orders`. It also fail-closes clear unconfirmed base technical fallbacks: rows marked `is_base_candidate_fallback=true` with `technical_entry_confirmed=false` are deferred with `portfolio_reason=technical_entry_not_confirmed` before capital or overlap slots are consumed.

`advisory.position_lifecycle` reads approved `advisory_portfolio_orders`, marks paper entry/current prices from `dhan_ohlcv_daily`, and writes `advisory_position_lifecycle` plus `advisory_rebalance_actions` with hold/trim/exit/review suggestions.

`advisory.execution_engine` reads consolidated `advisory_action_recommendations`, builds broker handoff orders in `advisory_execution_orders`, and can reconcile order/trade state from Dhan into `advisory_execution_orders` plus `advisory_execution_fills`. Its order/fill schema is registry-managed by `20260611_advisory_execution_orders_base`. Staged order sizing prefers fresh `dhan_ohlcv_intraday` prices and falls back to `dhan_ohlcv_daily` close when intraday is unavailable. Dhan identity failures are fail-closed in dry-run previews: the row becomes `submit_blocked`, the safety contract records `broker_identity_status=failed`, and execution fallback telemetry records the blocker. Action-table order previews also write `order_intent_lineage` into `safety_checks_json` and raw broker context, linking the order back to the action recommendation, reason-contract summary/status, risk sizing, stop/target levels, and approval/reconciliation gate status.

Use `--use-broker-account` on a dry run when you want staged quantities capped by live Dhan cash and holdings without submitting orders. `--live` enables the same broker-account sizing automatically before safety checks and submission.

Live Dhan submission is fail-closed. `--live` is not enough by itself; set `STOCKEY_LIVE_TRADING_ENABLED=true` only when you intentionally want broker submission. Each live run also requires a per-run confirmation token tied to that exact planned order set, including symbols, quantities, security ids, and estimated values. Run once without the token to see the expected token in `safety_checks_json.live_run_confirmation_expected`, then pass `--live-confirmation <token>` or set `STOCKEY_EXECUTION_LIVE_RUN_CONFIRMATION=<token>` for that specific run. Even after operator approval and broker reconciliation, live submission remains blocked by default until the safety contract has `live_evidence_status=passed` and at least `STOCKEY_EXECUTION_MIN_EVIDENCE_SUCCESSFUL_RUNS` successful dry-run/reconciliation cycles. Persisted dry-run rows also expire for live handoff after `STOCKEY_EXECUTION_MAX_ROW_AGE_HOURS`, so old approved/reconciled previews must be regenerated. Use `/api/execution/evidence-review` from the operator UI to preview/apply that evidence status; apply writes `advisory_execution_evidence_reviews`, updates only `live_evidence_*`, keeps `live_submission_allowed=false`, and never submits broker orders. Use `/api/execution/live-allowance` only after approval, reconciliation, and evidence are passed; apply writes `advisory_execution_live_allowance_reviews`, sets only `live_submission_allowed=true`, and still never submits broker orders.

Keep these caps configured before live use:

- `STOCKEY_EXECUTION_MAX_LIVE_ORDERS_PER_RUN`, default `5`
- `STOCKEY_EXECUTION_MAX_ORDER_VALUE_INR`, default `50000`
- `STOCKEY_EXECUTION_REQUIRE_FRESH_INTRADAY_PRICE`, default `true`
- `STOCKEY_EXECUTION_MAX_INTRADAY_PRICE_AGE_MINUTES`, default `30`
- `STOCKEY_EXECUTION_MAX_ROW_AGE_HOURS`, default `24`; set `0` only if you intentionally want to disable stale dry-run row blocking
- `STOCKEY_EXECUTION_REQUIRE_EVIDENCE_CHECKLIST`, default `true`
- `STOCKEY_EXECUTION_MIN_EVIDENCE_SUCCESSFUL_RUNS`, default `3`
- `STOCKEY_EXECUTION_LIVE_RUN_CONFIRMATION`, default blank and must match the current run token

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
