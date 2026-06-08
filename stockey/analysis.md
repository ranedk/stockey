# Stockey Pipeline Analysis

Date: 2026-06-07

Purpose: create one working audit of the codebase and pipeline so we can decide what to harden next. This is intentionally direct. `Done` means the code path exists and is wired. `Partial` means it exists but has confidence, UX, testing, or operational gaps. `To do` means the missing work can affect correctness, trust, or production safety.

## Executive Summary

The project has a broad end-to-end pipeline:

1. raw data ingestion
2. parsing and identity mapping
3. feature generation
4. screeners and technical/rule scoring
5. event extraction and event-policy overlays
6. risk, portfolio, lifecycle, and action consolidation
7. manual-review and wait-signal intervention
8. execution planning
9. operator UI, traces, health, and research evaluation

The main architectural idea is sound: LLMs extract/review text, while deterministic/statistical systems own action policy and execution gates. The confidence problem is not the idea. The confidence problem is that several side paths were added incrementally and not every operator decision, failure, fallback, and follow-up has been fully wired into the main deterministic state machine.

Highest-priority gaps:

- `Partial`: formal state machine for manual interventions and their downstream effects. Decision effects, matched wait-signal reopen/suppression, durable review-only action-candidate linkage, and a mocked end-to-end operator journey are enforced; broader superseded/downstream state coverage still needs work.
- `Done`: regression tests for every manual-review decision type and the core watch-signal downstream journey now cover decision recording, wait-signal creation, matching, Manual Review reopening, and review-only action-candidate creation.
- `Partial`: action/reason contract tests covering event-policy, playbook, lifecycle, execution, and wait-signal paths. Matched Manual Review wait-signal candidates now carry explicit wait-signal evidence, same-symbol source-precedence contracts expose losing candidates including market-adjustment context, enabled promoted exact-match conflict rules affect candidate ranking and preserve rule attribution in reason contracts, incomplete broker-capable rows downgrade with explicit Manual Review/no-broker boundary evidence, event-policy/playbook/lifecycle/generic Manual Review rows carry explicit review-only/no-broker boundary evidence, and compact API rows preserve execution-safety contracts even when only persisted in raw broker JSON; duplicate BUY screener collapse, portfolio BUY-over-WATCH, lifecycle HOLD-over-WATCH, market-gated portfolio BUY and BUY_MORE Manual Review-over-WATCH, cautious-market portfolio BUY_MORE-over-WATCH sizing reduction, event-policy Manual Review-over-market-adjusted BUY_MORE plus WATCH, adversarial-veto Manual Review-over-market-gated BUY_MORE plus WATCH, rebalance SELL-over-market-gated BUY_MORE plus WATCH, portfolio BUY_MORE-over-portfolio BUY, portfolio BUY_MORE-over-WATCH, portfolio SELL-over-portfolio BUY, SELL-over-WATCH, generic MANUAL_REVIEW-over-portfolio BUY, PARTIAL_SELL-over-portfolio BUY, PARTIAL_SELL-over-WATCH, review-only TIGHTEN_STOP-over-portfolio BUY, review-only TIGHTEN_STOP-over-WATCH, MANUAL_REVIEW-over-WATCH, and event-policy/playbook/lifecycle matrix permutations now have focused coverage, but broader contract coverage remains.
- `Done`: adversarial-review veto reasons are visible in compact Action Queue reason contracts/UI, not only trace/detail views.
- `Done`: compact Action Queue/API reason fields now humanize internal status/reason codes before display while preserving machine action codes.
- `Done`: stale operator snapshot warnings now use a shared API warning payload and render consistently on snapshot-backed operator pages, including symbol detail. Non-snapshot Manual Review, Wait Signals, and Identity Issues pages now expose source-specific freshness/unavailable-source warnings derived from their visible rows, and home/portfolio/events read APIs now publish explicit broker-disabled schema contracts.
- `Partial`: better cleanup and de-duplication of old processing failures after the underlying bug is fixed. Manual Review and Health suppress recovered/superseded failure rows at read time, Health groups active/recovered/superseded-ready degradations, Health shows superseded candidate samples plus dry-run/apply commands, `advisory/superseded_failures.py` can dry-run or mark durable superseded metadata, and cron/Operations can audit the dry-run path without applying cleanup. Operator UI bulk-close/apply workflow remains.
- `Partial`: explicit “what happens next” visibility everywhere an operator clicks a decision. Manual Review now distinguishes item impact category and selected decision boundary before save; the standalone identity issues page surfaces read-only repair boundaries; other operator write flows still need the same treatment.
- `Partial`: wait signals now have typed condition contracts, matching, Manual Review reopening, closed-original suppression, review-only action-candidate linkage, conservative signal-refresh escalation, and mocked end-to-end operator journey coverage; broader production journey/UI coverage remains.
- `Partial`: watchers can trigger fast signal refresh with operator-visible effect labels for action change, wait-signal match, or evidence-only refresh, but they are not yet equivalent to a full advisory state transition.
- `Partial`: execution planning exists, but live broker execution should remain disabled until dry-run approval and reconciliation flows are complete. Action Queue surfaces dry-run approval/reconciliation safety contracts, compact API rows preserve persisted raw broker safety contracts, mocked broker account/reconciliation coverage exercises nested account payload parsing/order/fill status mapping/persisted reconciliation safety-contract updates, and the operator header now shows the global live-trading disabled/enabled state from `/api/runtime`; it does not approve or submit orders.

## Status Legend

- `Done`: implemented and wired into normal scripts/API.
- `Partial`: implemented but incomplete, under-tested, too implicit, or not yet trusted.
- `To do`: missing work or known shortcoming.

## 1. Runtime And Environment

Status: `Partial`

Done:

- `scripts/resolve_python.sh` lets shell scripts use the active venv or configured interpreter.
- `.env.example` exists and documents many runtime variables.
- `builder.py` prepares environment pieces including logs and local `go-crond`.
- `all_frontend.sh` uses `nvm` when configured and runs FastAPI plus Nuxt.
- Cron template and generated crontab exist under `config/`.

Gaps:

- `To do`: `.env.example` needs a second pass against all `env(...)`, `os.getenv(...)`, and shell defaults to ensure it is complete.
- `To do`: split frontend supervisor into a more robust process manager. Current `all_frontend.sh` is acceptable for local work, but backgrounding it manually can be fragile.
- `To do`: document one canonical way to restart only API, only Nuxt, and both together.
- `Done`: add a health check for “running code hash/version” so the UI can tell when the API is stale after edits. `/api/runtime` reports git/process/source metadata, and the operator header warns when source files are newer than the API process.

## 2. Cron And Orchestration

Status: `Partial`

Done:

- `config/stockey.crontab.template` and `config/stockey.generated.crontab` schedule recurring jobs.
- Recurring scripts are split into `all_watchers.sh`, `all_downloaders_queue.sh`, `all_external_workers.sh`, `complete_data.sh`, `all_advisory.sh`, `all_frontend.sh`, and weekly `all_ml.sh`.
- `scripts/with_lock.sh` and `scripts/wait_for_locks.sh` prevent obvious overlaps.
- `all_watchers.sh` self-locks.
- `all_advisory.sh` waits for catch-up/data worker locks before running.

Gaps:

- `To do`: add a single operator page/card that shows next cron run, last run, duration, status, and latest log tail for every scheduled job.
- `To do`: detect stale cron processes and stale lock files.
- `To do`: move all one-off direct Python cron commands into named shell wrappers or command registry entries so the UI/docs stay consistent.
- `To do`: add script-level success markers to all wrappers, not just logs.
- `To do`: add cron simulation/dry-run command that verifies paths, locks, venv, ports, and required env before starting go-crond.

## 3. Raw Data Downloaders

Status: `Partial`

Done:

- Dhan scrip master and OHLCV loaders exist.
- NSE bhavcopy, indices, corporate actions, earnings, insider, off-market, and recent events loaders/parsers exist.
- Screener.in login and ad hoc query flows exist.
- Sharpely, FRED, CPI, WPI, NSDL FPI, RBI, and ET/news loaders exist.
- External task queue exists for serialized single-client work.
- NSE/Dhan/Screener flows have retry and auth improvements.

Gaps:

- `To do`: centralize downloader state. Some sources use DB max date, some Redis/sync state, some file state, and some source-specific logic.
- `To do`: every downloader should emit a standard run row: source, from/to, rows, skipped, retries, fallback used, error class, state advanced.
- `To do`: every downloader should have a “no data vs source unavailable vs auth unavailable vs parse failed” status.
- `To do`: Dhan identity failures should be stored in a first-class skipped-symbol/fix-hint table, not only surfaced through logs.
- `To do`: NSE stale cookie/session resets should be counted and visible in Health.
- `To do`: use one queue abstraction for all single-client/browser-bound sources, including Screener and Dhan auth-sensitive calls.

## 4. Parsing And Ingestion State

Status: `Partial`

Done:

- Bhavcopy parser no longer silently eats many parse failures.
- Empty/processed file state improvements exist.
- Ingestion file state scripts exist.
- Announcement pipeline tracks documents/reports and processing failures.
- Heavy announcement text offload tooling exists.
- Manual Review suppresses old `advisory_event_processing_runs` failures when a later successful run exists for the same `unique_id` and `stage`.
- Manual Review and Health suppress announcement document rows where stale `last_error` remains after OCR and parse statuses have recovered.
- `advisory/superseded_failures.py` previews by default and can explicitly mark superseded `advisory_event_processing_runs` rows plus recovered announcement `last_error` rows with durable metadata.
- `all_superseded_cleanup_audit.sh`, cron, and the audited Operations command registry expose the superseded cleanup dry-run path without passing `--apply`.

Gaps:

- `Partial`: old processing failures remain noisy after bugs are fixed. Read-time suppression, Health active/recovered/superseded-ready grouping, explicit dry-run/apply command visibility, a durable marking helper, and scheduled/audited dry-run visibility now exist; operator-facing bulk close/apply remains.
- `To do`: every parser should distinguish `empty_valid_source`, `bad_file_retryable`, `schema_changed`, and `parser_bug`.
- `To do`: stale historical parse failures should not keep polluting Manual Review unless still current and actionable.
- `To do`: add parser contract tests for the most important source files.
- `To do`: make all ingestion state inspectable from the operator UI.

## 5. Identity And Symbol Mapping

Status: `Partial`

Done:

- `company_master` exists.
- `dim_security`, `dim_security_history`, and review events exist.
- Dhan identity resolver supports NSE/BSE fallback behavior.
- Company master ID backfill exists.
- Missing Dhan stock security IDs are recorded in `advisory_identity_issues` with symbol, requested exchange, fallback attempts, company-master context, error text, and suggested repair steps.
- Manual Review surfaces open identity issues as technical items with fix instructions.
- Operator API/frontend now expose a read-only standalone Identity Issues page with open skipped-symbol rows, repair hints, attempted exchanges/fallbacks, Manual Review item linkage, and explicit no-broker/no-mutation boundaries.

Gaps:

- `Partial`: unresolved Dhan identity issues now have a durable table, Manual Review queue, and standalone read-only skipped-symbol/identity page; close/reopen lifecycle remains.
- `Done`: store every “No Dhan security id mapped” stock case in a durable table with symbol, source, exchange tried, fallback tried, and suggested action.
- `To do`: add daily auto-repair for NIFTY/index mappings and common symbol aliases.
- `To do`: assert no active advisory/action row lacks a resolvable company/security identity unless explicitly marked non-tradable.

## 6. Storage, DB Reliability, And Performance

Status: `Partial`

Done:

- DB retry wrappers exist for many query/upsert paths.
- Upserts use temp files for COPY payloads.
- Duplicate index, size, retention, slowlog, archive, and S3 offload scripts exist.
- Operator snapshot and trace summary materialization exist.
- API latency probe and slow-operation state exist.

Gaps:

- `To do`: standardize retry policy for `QueryCanceled`, deadlock, connection closed, and statement timeout across all DB access paths.
- `To do`: every fallback query path should log “fallback used” into a durable telemetry table or health payload.
- `To do`: add indexes for current API hot paths after reviewing `api_latency_probe` and Postgres query plans.
- `To do`: enforce compact API payloads by default for frontend pages.
- `To do`: finalize S3/object storage offload for heavy text and OCR payloads.
- `To do`: add DB schema migration/version tracking. Current `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ADD COLUMN` approach is pragmatic but makes compatibility hard to reason about.

## 7. Feature Generation

Status: `Partial`

Done:

- Macro snapshots/features exist.
- Regime snapshots exist.
- Market context exists.
- Technical features and technical engine exist.
- Intraday features exist.
- Fundamental snapshots and peer sync exist.
- Exchange event/features exist.

Gaps:

- `To do`: add feature freshness contract per feature table. Advisory should know whether a feature is fresh, stale, missing, or intentionally skipped.
- `To do`: add explicit feature dependency graph so a stage cannot silently use stale upstream rows.
- `To do`: explain in the UI which feature was missing or stale when an action became Manual Review.
- `To do`: reduce expensive full-table feature queries with materialized/current snapshots.
- `To do`: add feature-level tests for point-in-time behavior and no lookahead.

## 7A. Announcement, Bhavcopy, And LLM Signal Intelligence

Status: `Partial`

This is the next quality-upgrade program. The current Dhan OHLCV technical spine is the strongest part of the system. The weaker part is turning corporate announcements and NSE/bhavcopy-derived data into compact, point-in-time evidence that can be used by deterministic rules, LLM reviewers, action consolidation, and the operator UI.

Done:

- Dhan OHLCV is the canonical price source for active technical analysis.
- Exchange event and exchange feature tables exist and are consumed by event/risk/model paths.
- Announcement ingestion, OCR/transcription, summarization, structured reports, event evaluation, event policy, wait signals, and action consolidation exist.
- LLM/Codex usage is bounded to extraction/review/signal explanation, not direct broker authority.
- `advisory.event_evidence_store` builds compact V1 tables: `advisory_bhavcopy_evidence_daily` for liquidity/deals/short/circuit/volatility/margin evidence and `advisory_announcement_evidence` for announcement metadata plus latest LLM evaluation fields.
- `advisory.llm_event_evaluator` now consumes `advisory_announcement_evidence` before raw announcement documents, marks raw-document fallback explicitly, and adds latest point-in-time `advisory_bhavcopy_evidence_daily` into exchange context.
- `advisory.company_memory_review` writes review-only company-memory summaries into `advisory_company_memory_reviews` from compact announcement evidence, compact bhavcopy evidence, technical/candidate state, event policy, wait signals, and latest action rows. It is deterministic by default and Codex is opt-in.
- `advisory.signal_quality_evaluator` compares realized outcomes for `technical_only`, `technical_plus_event`, `technical_plus_bhavcopy`, `technical_plus_company_memory`, and `technical_plus_all` variants using point-in-time overlays and future Dhan OHLCV returns after costs. It is research-only and does not change live action policy.
- `/api/signal-quality` and the Nuxt `/signal-quality` page expose the latest signal-quality run with overlay coverage, lift versus technical-only, and selected matured examples for manual review.
- `advisory.exchange_events` now reads normalized and bhavcopy corporate-action sources, maps corporate-action `action_type` into stable event types, and exposes `--repair-missing-types` for legacy null-typed deal/short-selling/unclassified rows.
- Sparse NSE crawlers for corporate actions, earnings, and insider deals now persist `advisory_sync_state`, and `advisory.event_data_quality` uses that state to avoid confusing “checked recently, no new sparse events” with failed ingestion.

Current findings:

- Legacy `advisory_exchange_events` null typing is now repairable by command. A first live repair classified legacy deal and short-selling rows, but the final catch-all repair still needs to be rerun after Postgres is stable because the DB was shutting down during verification.
- NSE block deals, bulk deals, short selling, circuit hits, CMVOLT, indices, and OHLCV have useful recent coverage, but they are not yet prepared as a broad queryable evidence layer.
- Earnings source coverage is stale in the current DB. Corporate-action and insider false positives are resolved by correct table mapping plus sparse-source sync-state support. Corporate-action feature influence is now proven nonzero on a rebuilt 2025-10-28 smoke day; broader historical/current rebuild coverage remains.
- Announcement documents and reports are recent and rich, but event-policy output is still too coarse for some cases; many rows become Manual Review without enough actionability context.
- Raw announcement text and large bhavcopy tables are too heavy for direct UI/advisory querying. V1 compact cached evidence tables now exist, and the event LLM path has switched to compact announcement/bhavcopy context. Remaining downstream consumers still need to switch from raw scans to these stores consistently.

Gaps:

- `To do`: add more OHLCV technical algorithms on top of Dhan data: breakout/retest/pullback state detection, volatility compression, relative strength, stop/target/time-horizon logic, partial-exit rules, and realized-outcome validation.
- `To do`: create a complete corporate-announcement taxonomy that maps every announcement into one of three storage forms: compact structured event, structured event plus summary text, or archived raw/unstructured evidence with metadata and source pointers.
- `To do`: create an announcement evidence tensor with materiality, direction, surprise, novelty, contradiction, confidence, expected decay, source reliability, affected peers/sectors, price reaction, current exposure, and suggested next evidence.
- `Partial`: create a bhavcopy evidence store that the LLM and deterministic rules can query without scanning raw tables. V1 exposes liquidity, turnover, circuit behavior, block/bulk accumulation, short-selling pressure, volatility, margins, and abnormal participation; the event LLM path consumes the latest row. Corporate actions, index/sector context, broader deterministic rules, and UI explainability consumption remain.
- `Partial`: add an LLM company-memory review flow that can inspect historical company events, prior decisions, current technical state, bhavcopy evidence, latest announcements, and wait signals to propose `BUY`, `SELL`, `HOLD`, `SELL_PARTIAL`, `BUY_MORE`, `WATCH`, or `NO_ACTION`. V1 is implemented as `review_input_only`, deterministic by default, persisted to `advisory_company_memory_reviews`, wired as the `company_memory` pipeline stage, attached to action API rows, and visible in Action Queue plus Symbol Detail. Outcome comparison is available through `advisory.signal_quality_evaluator`; review-only overlay promotion decisions are available through `advisory.signal_quality_promotion`; approved decisions can generate reviewed config diffs through `advisory.config_change_assistant`. Remaining work is action-consolidation influence rules after evidence proves lift and an operator manually applies a reviewed config/rule change.
- `To do`: keep final broker-executable action authority in deterministic action consolidation. LLM signal output must be an input with rationale/confidence, not the final execution decision.
- `Done`: centralize LLM/Codex prompt contracts in `advisory.prompt_registry` with prompt ids, versions, schema names, model env vars, source files, authority scopes, output tables, fallbacks, and migration status; expose it through `/api/research/prompt-registry` and the Nuxt `/prompt-registry` page.
- `To do`: progressively migrate prompt callers to reference registry ids/version constants directly and record prompt ids/schema versions on every LLM output row.
- `To do`: surface this evidence in the operator UI: what happened, what data was used, what the LLM concluded, what deterministic rules accepted/rejected, what evidence is stale/missing, and why the final action differs from raw signals.
- `Done`: add an evaluator that compares technical-only outcomes against technical-plus-event-policy, bhavcopy, and company-memory outcomes after costs, using point-in-time data.
- `Done`: add operator UI/API summaries for `advisory_signal_quality_eval_summary` so overlay lift/worsening can be reviewed before any production rule change.
- `Done`: add a manual signal-quality overlay-promotion review workflow that writes review/decision audit rows, shows Trust Gate context in the Nuxt `/signal-quality` page, and emits copyable patch guidance without changing live policy.
- `Done`: add reviewed config-change diff generation for approved threshold and signal-quality overlay decisions without applying the change.
- `To do`: add deterministic action-consolidation influence rules only after a generated diff is manually reviewed and applied.

## 8. Screener And Candidate Universe

Status: `Partial`

Done:

- Production and ad hoc Screener.in flows exist.
- Training universes can be synced from ad hoc screeners.
- Screener snapshots/constituents feed the advisory universe.
- Query syntax fixes such as `DMA 50` / `DMA 200` are known.

Gaps:

- `To do`: maintain a tested query library for Screener.in syntax so bad fields fail before runtime.
- `To do`: store screener parse failures with URL/query/body excerpt for debugging.
- `To do`: prevent repeated generic screener failures from flooding Manual Review.
- `To do`: add operator UI to test an ad hoc screener and preview rows before using it.
- `To do`: add coverage metrics: how much of action universe came from each screener and how often each screener produces useful outcomes.

## 9. Technical Engine

Status: `Partial`

Done:

- Swing technical PRD is reflected in code direction.
- Technical score buckets exist.
- Technical threshold calibration exists and writes research-only evidence.
- Technical Decision Panel in UI exposes technical reasons.
- Threshold promotion helper is manual-only and does not auto-edit config.

Gaps:

- `To do`: finish translating every PRD state into tested code paths: `WATCHLIST`, `NEAR_PIVOT`, `READY`, `BUY_TRIGGERED`, `HOLD`, `ADD_ON_PULLBACK`, `PARTIAL_EXIT`, `FULL_EXIT`, `EMERGENCY_EXIT`.
- `To do`: add tests for stop, target, time horizon, partial-profit, and technical invalidation behavior.
- `To do`: make technical conflict precedence explicit when event/risk/lifecycle disagree.
- `Done`: Action Queue compact rows now keep reason-contract/manual-revision explanations and show “why not approved/manual review” operator checks for blocked or review-only candidates.
- `To do`: connect calibration evidence to a manual config patch workflow with strong review gates.

## 10. LLM Event Extraction And Event Policy

Status: `Partial`

Done:

- Event evaluator produces structured event rows.
- Event policy maps event classes into bounded overlays.
- Low-information event-policy rows can downgrade to `NO_ACTION`.
- Event-policy manual-review rows now use human-facing text in the API.
- Event-policy evaluator checks realized outcomes after costs.
- LLM is used for extraction/review, not direct broker authority.

Gaps:

- `To do`: all LLM failures should create a durable, categorized issue with whether deterministic fallback was used.
- `To do`: do not allow old LLM/Codex failures to pollute current Manual Review after a successful rerun exists.
- `To do`: event-policy class rules need more outcome-driven calibration and operator review.
- `To do`: add a source-quality model for news versus exchange announcements versus OCR documents.
- `To do`: require “actionability” fields on every manual-review event: materiality, freshness, current holding exposure, price reaction, and suggested next evidence.
- `To do`: make event-policy rows explicitly symbol/date/current-state aware, not just event aware.
- `To do`: separate LLM outputs into extraction, company-memory review, proposed signal, adversarial challenge, and final deterministic policy input so prompts and schemas are auditable.
- `To do`: store prompt/skill version, input evidence IDs, output schema version, and deterministic acceptance/rejection reason for every LLM-generated signal.

## 11. Adversarial Review

Status: `Partial`

Done:

- Deterministic adversarial review exists.
- It can clear, penalize, force manual review, or veto.
- Risk engine consumes review context.
- Action ranking now has an explicit adversarial-veto precedence rule so vetoed event-policy manual reviews beat positive broker and watch candidates, even if raw candidate priorities are misleading.
- Compact operator action rows preserve adversarial-review event evidence, and the Action Queue reason-contract panel shows veto status, review action/status, and review reason.

Gaps:

- `Partial`: define exact precedence between adversarial review, event policy, market context, technical signals, and lifecycle exits. Adversarial veto over BUY/WATCH and over a risk-off market-gated BUY_MORE plus WATCH collision is now pinned; broader cross-system precedence remains.
- `Done`: add regression tests for veto beating buy/watch candidates.
- `Done`: show adversarial-review reason in the action queue, not only trace/details.
- `To do`: evaluate veto precision and false positives over realized outcomes.

## 12. Hypothesis, Playbooks, And Wait Signals

Status: `Partial`

Done:

- Hypothesis/playbook config and UI exist.
- Hypothesis scans create matches/action plans.
- Trusted playbooks can bridge as review-only overlays.
- Wait signals exist and match price/news/announcement conditions.
- `watch_for_event` manual-review decisions now create active `advisory_wait_signals` rows.
- New wait signals store typed `condition_json.condition_type` contracts for `price_level`, `event_keywords`, `clarification_filing`, `result_update`, `management_commentary`, `sector_event`, and `expiry_only`.
- The matcher routes by typed condition instead of only `signal_type`; invalid or unknown condition types return issue rows rather than crashing.
- Wait-signal matches now include originating wait-signal context in `evidence_json.wait_signal`.
- Signal refresh escalates matched waits conservatively: negative waits become reduce-review signals, positive waits become WATCH, and unknown waits become MANUAL_REVIEW. It does not create broker-executable trades.
- Watchers run wait-signal matching.
- Operator frontend now has a first-class `/wait-signals` page with active, matched, expired, and closed buckets, source labels, readable condition summaries, and latest match evidence.
- `/api/wait-signals` now returns compact `summary`, `sections`, and match-enriched signal rows while preserving raw-compatible `signals` and `matches` arrays.
- Matched manual-review wait signals now expose `manual_review_item_id`, source table/key, original wait question, and latest evidence in API/UI payloads.
- Manual Review now surfaces matched manual-review wait signals as follow-up review items without mutating portfolio, action recommendations, or broker execution.
- Regression coverage exists for the wait-signal API payload shape and manual-review/playbook signal visibility.
- A mocked operator journey regression covers Manual Review `watch_for_event` decision recording, wait-signal creation, event matching, stale waiting-item replacement with a reopened follow-up, and review-only action-candidate linkage.

Gaps:

- `Done`: wait-signal matching now has typed conditions for price level, event keywords, clarification filings, result updates, management commentary, sector events, and expiry-only waits.
- `Done`: UI shows wait signals created from manual review separately from playbook-generated wait signals.
- `Done`: when a wait signal matches, the UI/API shows “what changed,” links back to the original Manual Review item, suppresses the stale waiting item, and surfaces the matched wait as a reopened follow-up item.
- `Done`: matched Manual Review wait signals now create durable review-only action-recommendation candidates linked back to the original manual-review item and match evidence.
- `Done`: add tests for manual-review wait signal creation, matching, closure, conservative escalation, and the core operator journey. Creation, API visibility, typed parsing, price/event matching, expiry-only, invalid-condition handling, stale waiting-item replacement, closed-original suppression, review-only signal refresh, and review-only action-candidate linkage are covered.

## 13. Rules, Risk, Portfolio, Lifecycle

Status: `Partial`

Done:

- Rule engine creates candidates/rejections.
- Risk engine builds allocations.
- Portfolio engine plans position candidates.
- Lifecycle engine handles hold/exit/rebalance outputs.
- Stop, target, horizon, bucket, and lifecycle concepts exist.
- Lifecycle context now records price status, paper-entry assumption, stop/target status, and operator question for review-only states.
- Regression tests prevent backdated paper entries by requiring the lifecycle entry date to use the first available close on or after portfolio `published_on`.

Gaps:

- `To do`: formal state machine from recommendation to assumed entry to holding to exit.
- `Done`: no backdated paper lifecycle entries from newly approved portfolio rows; tests cover ignoring pre-approval price history and routing missing post-approval prices to review-only lifecycle state.
- `To do`: lifecycle needs stronger target/stop update rules and audit trail.
- `To do`: partial exits, add-on buys, time stops, and emergency exits need deterministic tests.
- `Partial`: lifecycle review-only rows now include human-readable reason, current price when available, entry assumption, target/stop status, and operator question in context. Lifecycle/rebalance action-consolidation Manual Review rows now add explicit review-only/no-broker reason-contract boundary evidence for manual, stale, horizon, and target review states; broader non-lifecycle portfolio/action-consolidation Manual Review permutations remain.

## 14. Action Consolidation And Conflict Resolution

Status: `Partial`

Done:

- `advisory_action_recommendations` consolidates to one current action per symbol.
- Action conflicts and conflict rules are persisted.
- Conflict rules page exists.
- Resolved conflicts are audit-only and do not enter Manual Review.
- Reason contracts exist and can downgrade incomplete broker actions.
- Matched Manual Review wait-signal action candidates now satisfy the reason contract through explicit `wait_signal` evidence while remaining review-only.
- Reason contracts now include same-symbol conflict/source-precedence evidence with winning source, losing candidates, and deterministic precedence reason; compact Action Queue rows preserve and render that conflict evidence.
- Reason contracts now carry explicit market-gate evidence when positive broker actions are blocked by weak/risk-off market context, including BUY and BUY_MORE review-only Manual Review boundaries and blocked original action; compact Action Queue rows preserve the original action plus gate reason and breadth/risk metrics.
- Compact Action Queue/API display payloads now humanize internal reason/status codes such as `blocked_by_adversarial_review`, `positive_action_blocked_by_market_context`, and missing-field names before they reach the UI, while keeping action codes machine-readable.
- Broker-capable candidates downgraded by incomplete reason contracts now persist explicit Manual Review boundary evidence in the reason contract: original blocked action, missing fields, operator question, review-only effect, and `broker_execution_allowed=false`.
- Event-policy, playbook, market-gated portfolio, generic action-consolidation, and lifecycle/rebalance Manual Review rows now infer explicit Manual Review reason-contract evidence with source-specific or generic review boundaries, operator question/reason, review-only effect, source attribution, and `broker_execution_allowed=false`; lifecycle/rebalance review states distinguish manual, stale, horizon, and target review boundaries.
- Enabled deterministic conflict rules now have regression coverage proving they can directly affect candidate ranking when raw candidate priorities disagree; disabling rules falls back to ordinary priority/freshness ranking.
- Conflict Rules UI can now enable or disable existing deterministic rules through a narrow operator API update endpoint; this affects future ranking/reads and does not rewrite historical action rows.
- Conflict Rules UI/API can edit stored rule explanations for existing deterministic rules, and rule-table bootstrap preserves operator-edited explanation text instead of restoring the default text.
- Conflict Rules UI/API can promote latest unresolved manual action conflicts into disabled exact-match rule candidates with stored condition metadata; enabled promoted rules are consumed by the conflict resolver for future matching conflicts and by candidate ranking for same-symbol exact action/source pairs.
- Conflict Rules UI/API can edit supported `action_pair_exact` condition metadata for promoted/manual-resolution rules with backend validation, so unsupported condition shapes are rejected instead of silently saved.

Gaps:

- `Done`: enabled conflict rules influence `rank_action_candidates()` directly, with a focused regression for exit precedence overriding a misleading lower raw priority and disabled rules falling back to ordinary ranking.
- `Done`: enabled promoted exact-match conflict rules influence `rank_action_candidates()` directly for same-symbol action/source pairs, with regression coverage that nonmatching source conditions fall back to ordinary ranking.
- `Partial`: add UI controls to edit/disable conflict rules and promote manual resolutions into deterministic rules. Enable/disable controls, explanation editing, disabled-by-default exact-match manual promotion, resolver consumption, ranking consumption, and exact-match condition editing are wired; broader semantic rule types remain.
- `Partial`: every final action should show winning candidate, losing candidates, rule used, and why the loser lost. Backend reason contracts and compact Action Queue UI now show same-symbol source-precedence evidence, losing market-adjustment evidence, and winning rows touched by deterministic or promoted exact-match conflict precedence carry the rule id/reason/score into the reason contract; broader action-row attribution remains.
- `Partial`: add tests for same-symbol duplicate screeners, conflicting buy/sell/manual/watch rows, and market-gate downgrades. Market-gate blocking, compact market-gate visibility, explicit market-gated Manual Review boundary evidence, market-gated portfolio BUY and BUY_MORE Manual Review-over-WATCH source precedence, cautious-market portfolio BUY_MORE-over-WATCH sizing-reduction source precedence, event-policy Manual Review-over-market-adjusted BUY_MORE plus WATCH source precedence, adversarial-veto Manual Review-over-market-gated BUY_MORE plus WATCH source precedence, rebalance SELL-over-market-gated BUY_MORE plus WATCH source precedence, incomplete-contract Manual Review downgrade boundaries, event-policy/playbook/lifecycle/generic Manual Review boundary evidence, wait-signal, duplicate BUY screener collapse, portfolio BUY-over-WATCH precedence, lifecycle HOLD-over-WATCH precedence, portfolio BUY_MORE-over-portfolio BUY precedence, portfolio BUY_MORE-over-WATCH precedence, portfolio SELL-over-portfolio BUY precedence, SELL-over-WATCH precedence, generic MANUAL_REVIEW-over-portfolio BUY, PARTIAL_SELL-over-portfolio BUY, PARTIAL_SELL-over-WATCH, review-only TIGHTEN_STOP-over-portfolio BUY, review-only TIGHTEN_STOP-over-WATCH, MANUAL_REVIEW-over-WATCH precedence, BUY/SELL/MANUAL_REVIEW/WATCH collision attribution, event-policy/playbook/lifecycle source matrix, lifecycle exit over review overlays, event review over lifecycle hold, equal-priority freshness tie-breaks, source-precedence reason-contract paths, conflict-rule enablement/explanation updates, promoted exact-match resolver rules, promoted exact-match ranking rules, and promoted exact-match reason-contract attribution have focused coverage; broader action-source permutations remain.
- `Done`: humanize compact Action Queue/API action reason fields before they reach the UI without rewriting stored recommendations or broker/execution contracts.

## 15. Manual Review Workbench

Status: `Partial`

Done:

- Manual Review page exists.
- Items are separated by lane: investment review, technical issue, research/config.
- Closing decisions remove items from active queue.
- `watch_for_event` now creates wait signals.
- Decision options now explain what happens.
- Manual Review rows show whether the item is investment-impacting, operational, research-only, or execution-blocking, plus the selected decision's queue effect before saving.
- Operator manual documents every Manual Review dropdown decision with its runtime state, active-queue effect, side effects, and explicit non-authority over portfolio/action/config/broker mutation.
- Technical/operational failures are visible separately.
- `advisory/manual_review_state.py` centralizes allowed decisions, closing/non-closing behavior, next state, safety flags, decision-row construction, and wait-signal side effects.
- API decision responses now include `next_state`, `creates_wait_signal`, `mutates_portfolio`, `mutates_action_recommendation`, and `submits_order`.
- Matched Manual Review wait signals now reopen as follow-up items and suppress the original waiting item from the active queue.
- Incomplete reason-contract downgrades now carry explicit Manual Review boundary evidence and preserve the blocked original action without enabling broker execution.

Gaps:

- `Done`: API-level regression tests cover every decision option:
  - `needs_more_data`
  - `watch_for_event`
  - `add_operator_note`
  - `approve_for_manual_config`
  - `ignore`
  - `downgrade_to_no_action`
  - `mark_fixed`
- `Partial`: build an operator-decision state diagram and enforce it in code. Initial state/effect table, incomplete-contract Manual Review downgrade boundary, matched wait-signal reopen state, review-only action-candidate linkage, and a mocked journey regression exist; superseded downstream states are not complete.
- `To do`: add bulk close for superseded technical failures.
- `Done`: link a manual-review item to its later wait-signal match and resulting review-only action candidate. Matched wait-signal follow-up is visible and the action candidate preserves the original item id plus match evidence.
- `Done`: make “closing decision” versus “annotating decision” visually impossible to miss on Manual Review items.
- `Done`: show whether each Manual Review item is investment-impacting, operational, research-only, or execution-blocking.

## 16. Watchers And Fast Signal Refresh

Status: `Partial`

Done:

- `all_watchers.sh` runs continuous watch one-shot.
- OHLCV, announcement, news, router, wait-signal matching, signal refresh, operator snapshot, and trace summary are wired.
- Watcher cursor state exists.
- Watcher lock prevents overlapping runs.
- Signal-refresh rows now classify watcher output as `action_changed`, `wait_match_created`, or `evidence_only`, and the operator home page surfaces that effect beside recent watcher-triggered symbol decisions.
- Skipped not-due watcher cycles now publish per-source watcher events instead of only appearing inside the aggregate run summary.
- Shell-level watcher lock skips now publish per-source skipped watcher events plus a skipped summary without advancing cursors, marking success, or running downstream watcher/snapshot/trace steps.
- News and announcement watcher cycles now use bounded replay/catch-up windows, persist requested-from/requested-to metadata, and mark `catchup_truncated` when stale cursors exceed configured lookback limits.

Gaps:

- `To do`: watchers still do not fully replace daily advisory. They produce fast signal refresh rows, not authoritative full portfolio reconciliation.
- `Done`: document which events trigger only signal refresh versus full advisory. The operator manual now lists watcher trigger boundaries for intraday OHLCV, news/announcements, context observations, material context triggers, wait-signal matches, skipped/failed/truncated watcher cycles, and the post-close advisory path.
- `Partial`: make missed watcher intervals provably catch up across all sources, not just some cursors. OHLCV, news, and announcement watcher cycles now have bounded catch-up metadata; broader downstream router/snapshot implications remain.
- `Done`: watcher output should show whether it changed an action, created a wait match, or only updated evidence.
- `Partial`: add tests for stale cursor, failed run, skipped lock, and catch-up behavior. OHLCV stale cursor/catch-up bounds, news/announcement bounded catch-up windows, failed-cycle sync-state recording, skipped not-due event publishing, and external lock-skipped event publishing now have focused coverage; broader downstream catch-up remains.

## 17. Execution And Broker Integration

Status: `Partial`

Done:

- Execution engine reads consolidated action recommendations.
- Execution planning is separated from live broker submission.
- Reason contracts block incomplete broker actions.
- Action-table order previews now carry an explicit safety contract requiring operator approval and broker reconciliation before live submission.
- Execution planning blocks closed, superseded, expired, or future-dated action rows before identity resolution or sizing.
- Operator snapshot/API compact action rows preserve execution safety contracts from dry-run order previews, and Action Queue renders a read-only approval/reconciliation gate.
- Mocked broker account/reconciliation coverage verifies nested broker cash payload parsing, holdings/positions aggregation, broker order status mapping, fill extraction, and persisted reconciliation safety-contract updates without live broker execution.
- Dhan auth/token refresh exists.

Gaps:

- `Partial`: keep live execution disabled until paper/dry-run reconciliation is proven. The engine now fail-closes live submission unless operator approval and reconciliation statuses are present; a UI approval flow still remains.
- `Partial`: add an execution approval UI gate with dry-run order preview. Backend dry-run plans and the read-only Action Queue now expose the approval/reconciliation contract, but there is still no operator approval workflow.
- `Partial`: add broker account/position reconciliation tests. Nested account payload parsing, holdings/positions aggregation, broker order status mapping, fill extraction, and persisted reconciliation safety-contract updates are now covered with mocked broker clients; UI approval workflow remains.
- `Done`: prevent live order submission from any stale or manually closed action. Execution planning now blocks closed/superseded/expired/future-dated action rows.
- `To do`: every order intent should link back to action recommendation, reason contract, risk sizing, stop, target, and operator approval if required.

## 18. Research And ML

Status: `Partial`

Done:

- Event model prep/training runner exists.
- Research ledger exists.
- Event model promotion check exists.
- Event model artifact store supports S3/object storage.
- TS forecast workflow/evaluator exists.
- Technical threshold calibration exists.
- Event-policy evaluator exists.

Gaps:

- `To do`: formal promotion gate before any model influences live policy.
- `To do`: model training should publish a simple “usable/not usable” scorecard to UI.
- `To do`: label coverage, leakage checks, false-discovery controls, and cost-adjusted baselines should be visible as first-class gates.
- `To do`: ensure all research jobs are point-in-time and cannot use future data by accident.
- `To do`: TimesFM/TS forecasts are still research-only and need validation before action integration.

## 19. Operator API

Status: `Partial`

Done:

- FastAPI serves dashboard, actions, traces, events, hypotheses, manual review, health, operations, calibration, artifacts, and conflict rules.
- Snapshot/caching is available for performance.
- API errors can be captured.
- Manual review decision endpoint persists operator decisions.
- Runtime metadata is exposed through `/api/runtime` with git revision, branch, dirty state, process start, latest source file mtime, stale-code detection, `api_schema` broker-disabled metadata, a permissive FastAPI/Pydantic response model, and mocked route-level smoke coverage.
- Critical Operator API payloads now include `api_schema` metadata with endpoint name, schema version, read-only status, and explicit broker-execution-disabled status for actions, health details, manual review, wait signals, and conflict-rule read/write responses.
- FastAPI/Pydantic response models are registered for critical Operator API routes: actions, health details, manual review queue/decision, wait signals/match, and conflict-rule read/write responses.
- Route-level FastAPI smoke coverage now exercises critical Operator API reads/writes with mocked payload builders, including actions, health details, manual review queue/decision, wait signals/match, and conflict-rule read/write responses. `/api/wait-signals/match` now also carries `api_schema` metadata.
- Symbol trace and symbol trace summary routes now publish permissive FastAPI/Pydantic response models, with route-level smoke coverage for success, query validation, and guarded 400/500 error paths.
- Event detail, event trace, and event trace summary routes now publish permissive FastAPI/Pydantic response models, with route-level smoke coverage for success and guarded 400/500 builder failures.
- Read-only Operations API routes for smoke checks, cron logs, command registry, and API errors now publish permissive FastAPI/Pydantic response models, include `api_schema` read-only/broker-disabled metadata, and have mocked route-level smoke coverage that avoids command execution.
- `python -m advisory.operator_smoke` now provides the canonical compact read-only preflight for status, trust level, fix hints, current blockers, and exact next commands. It is registered as an audited Operations UI command and skips Dhan validation by default to avoid login side effects.
- Read-only technical calibration GET routes now publish permissive FastAPI/Pydantic response models, include `api_schema` read-only/broker-disabled metadata, and have mocked route-level smoke coverage that avoids promotion-review writes.
- Read-only event-model research GET routes for promotion-check and artifact manifest now publish permissive FastAPI/Pydantic response models, include `api_schema` read-only/broker-disabled metadata, and have mocked route-level smoke coverage that avoids model promotion, S3 writes, command execution, and broker paths.
- Read-only hypotheses GET route now publishes a permissive FastAPI/Pydantic response model, includes `api_schema` read-only/broker-disabled metadata, and has mocked route-level smoke coverage that avoids hypothesis create/update/scan writes.
- Read-only event-policy/evaluation GET routes now publish permissive FastAPI/Pydantic response models, include `api_schema` read-only/broker-disabled metadata, and have mocked route-level smoke coverage that avoids evaluator runs, command execution, and broker paths.
- Read-only home, portfolio, and events GET routes now publish permissive FastAPI/Pydantic response models, include `api_schema` read-only/broker-disabled metadata, and have mocked route-level smoke coverage that avoids broker, command, and cleanup paths.
- Read-only summary, watchlist, and market-context GET routes now publish permissive FastAPI/Pydantic response models, include `api_schema` read-only/broker-disabled metadata, and have mocked route-level smoke coverage that avoids broker, command, and cleanup paths.
- Read-only signal-refresh GET route now publishes a permissive FastAPI/Pydantic response model, includes `api_schema` read-only/broker-disabled metadata, and has mocked route-level smoke coverage that avoids running refreshes, commands, cleanup, or broker paths.
- Read-only data-health GET route now publishes a permissive FastAPI/Pydantic response model, includes `api_schema` read-only/broker-disabled metadata, and has mocked route-level smoke coverage that avoids command, cleanup, and broker paths.
- Read-only action detail and portfolio detail GET routes now share a permissive FastAPI/Pydantic detail response model, include `api_schema` read-only/broker-disabled metadata, and have mocked route-level smoke coverage that avoids broker, command, and cleanup paths.
- Read-only runtime GET route now publishes a permissive FastAPI/Pydantic response model, includes `api_schema` read-only/broker-disabled metadata, and has mocked route-level smoke coverage that avoids broker, command, credential, cleanup, and DB paths.
- Read-only legacy health GET route now publishes a permissive FastAPI/Pydantic response model, includes `api_schema` broker-disabled metadata, and has mocked route-level smoke coverage that avoids broker, command, cleanup, credential, and DB paths.
- Read-only event detail, event trace, event trace summary, symbol trace, and symbol trace summary routes now enrich successful route responses with `api_schema` read-only/broker-disabled metadata even when mocked or alternate builders omit it, with focused route smoke assertions.
- Frontend TypeScript payload contracts now include `api_schema` metadata for event detail, event trace, symbol trace, and trace summary API responses, so the Nuxt client preserves the read-only/broker-disabled schema contract for trace/detail views.
- Frontend TypeScript payload contracts now also preserve read-only/broker-disabled `api_schema` metadata for Operations, event-model research, technical calibration, technical promotion-review listing, and event-policy/evaluation payloads.
- Frontend TypeScript payload contracts now preserve read-only/broker-disabled `api_schema` metadata for action detail and portfolio detail payloads.
- Frontend TypeScript payload contracts now preserve `api_schema` metadata for the wait-signal match result payload without changing matcher, broker, cleanup, command, credential, or DB behavior.
- Frontend TypeScript payload contracts now preserve `api_schema` metadata for action-conflict rule read and narrow rule-write responses without changing conflict ranking, broker, cleanup, command, credential, or DB behavior.
- Snapshot-backed operator payloads now expose a normalized nullable `snapshot_warning` when the API serves a stale cached operator snapshot.

Gaps:

- `Done`: API exposes schema/version metadata for critical endpoints and runtime stale-code metadata for frontend detection.
- `Done`: add typed response models for critical endpoints. Frontend types accept `api_schema` metadata for critical, trace/detail, action/portfolio detail, read-only operations/research, technical-calibration, event-policy, wait-signal match, and action-conflict rule route contracts, and FastAPI/OpenAPI now publishes Pydantic response models for critical route contracts.
- `Partial`: add endpoint-level tests for manual review, actions, symbol/event trace/detail, health, wait signals, and conflict rules. Focused schema-metadata, OpenAPI response-model coverage, and route-level FastAPI smoke coverage now exists for legacy health, health details, actions, manual review, wait signals, conflict rules, symbol trace, event detail/trace routes, event/symbol trace schema metadata, action/portfolio detail routes, read-only Operations routes, read-only technical calibration GET routes, read-only event-model research GET routes, the read-only hypotheses route, read-only event-policy/evaluation routes, read-only home/portfolio/events routes, read-only summary/watchlist/market-context routes, read-only signal-refresh route, read-only data-health route, and runtime route; broader low-priority API pages remain open.
- `To do`: compact APIs should be default for large pages.
- `Done`: API should surface stale snapshot warnings consistently on snapshot-backed payloads.

## 20. Frontend

Status: `Partial`

Done:

- Nuxt/Vue/Tailwind operator app exists.
- Main dashboard, events, symbol page, decision trace, health, hypotheses, manual review, operations, technical calibration, signal quality, and conflict rules pages exist.
- Operations now has a first-class “Run Operator Smoke Check” action backed by the audited command-run table.
- Manual review now explains selected dropdown effects.
- Manual Review now shows item-impact badges and decision-boundary panels so operators can distinguish closing, annotating, and wait-signal decisions before save.
- Symbol links and UI chips have improved visual distinction.
- Global header shows a stale API restart warning when `/api/runtime` reports running code older than source files on disk.
- Global header shows whether live broker submission is disabled or enabled according to `STOCKEY_LIVE_TRADING_ENABLED`, with the default-disabled state exposed by `/api/runtime`.
- A shared snapshot warning banner renders stale/fresh operator snapshot metadata on dashboard, events, health, and symbol-detail contexts backed by operator snapshots.
- Manual Review, Wait Signals, and Identity Issues show source-specific freshness warnings for stale visible rows, missing source timestamps, or skipped source queries.
- Action Queue shows read-only execution approval/reconciliation gates from dry-run order-preview safety contracts when available.
- Action Queue reason-contract panels now surface adversarial-review veto status and review reason from compact API rows.
- Action Queue reason-contract panels now show a dedicated market-gate block for positive actions downgraded to Manual Review by weak/risk-off market context.
- Event detail and decision trace client calls now use typed payloads that preserve operator `api_schema` metadata, including the read-only/broker-disabled contract added by the API.
- Action and portfolio detail client calls now use typed payloads that preserve operator `api_schema` metadata, including the read-only/broker-disabled contract added by the API.
- Wait Signal match client calls now use a typed payload that preserves operator `api_schema` metadata, including the broker-disabled contract added by the API.
- Action Conflict Rules client calls now use typed payloads that preserve operator `api_schema` metadata for read and narrow write responses, including the broker-disabled contract added by the API.
- Identity Issues is now a first-class read-only page for open Dhan security mapping failures and skipped-symbol repair context.
- Vitest component smoke coverage now mounts shared operator-safety components for snapshot warnings, source warnings, reason contracts, and trace manual-review wait links.
- Vitest page-level smoke coverage now mounts Manual Review, Action Queue, Health, and Symbol Detail pages with mocked read-only API payloads, asserting source freshness, decision boundaries, matched wait-signal follow-ups, execution approval/reconciliation gates, current blockers, superseded cleanup preview boundaries, stale snapshot warnings, and reason-contract safety evidence remain visible without broker execution or cleanup execution.

Gaps:

- `To do`: add an end-to-end “operator task journey” view: issue -> decision -> wait signal -> match -> refreshed action -> portfolio/execution implication.
- `Partial`: make stale data warnings visible on every page, not just health/snapshot contexts. Snapshot-backed dashboard, events, health, and symbol-detail contexts now share the same warning component; Manual Review, Wait Signals, and Identity Issues now show source-specific warnings, while lower-priority non-snapshot pages still need coverage.
- `Partial`: add UI tests or at least component-level smoke tests for key pages. Shared safety components plus page-level Manual Review, Action Queue, Health, and Symbol Detail safety flows now have Vitest mount coverage; lower-priority operator pages and write-flow interactions remain open.
- `To do`: reduce tag/pill/link ambiguity everywhere.
- `Partial`: add first-class pages for wait signals, skipped symbols, fallbacks, and unresolved identity issues. Wait Signals and read-only Identity Issues are first-class; broader fallback pages and identity close/reopen workflow remain.

## 21. Observability, Health, And Error Surfacing

Status: `Partial`

Done:

- Operator health exists with fix hints.
- Health page shows skipped/fallback/partial-data concepts.
- Health payload and page now show a prioritized “current blockers before advisory can be trusted” summary plus an Advisory Trust Gate derived from runtime/API state, core freshness, event evidence readiness, identity issues, signal-quality usability, and active degradation rows.
- Cron logs are written under `logs/cron`.
- Slow-operation log exists.
- Decision traces exist.
- Trace summaries exist.
- Symbol/event trace summaries now include direct manual-review decision -> wait-signal -> match links, and the Trace Timeline UI renders them when present.
- Recovered announcement OCR/parse rows with stale `last_error` are no longer treated as active Health degradations.

Gaps:

- `To do`: all failures and fallbacks should have a durable row, not only log lines.
- `To do`: health page should group failures by active/recovered/superseded.
- `Partial`: old resolved errors should be closeable or auto-superseded. Read-time suppression, Health active/recovered/superseded-ready grouping, Health cleanup preview samples/commands, scheduled/audited dry-run visibility, and durable superseded marking exist for event-processing and recovered announcement-document errors, but operator bulk close/apply remains.
- `Done`: add “current blockers before advisory can be trusted” summary and Advisory Trust Gate with `blocked`, `review_required`, and `usable` operating states.
- `Done`: trace should link manual decisions and wait signals directly.
- `Done`: add health checks for announcement/bhavcopy evidence readiness and LLM prompt/schema visibility. `advisory.event_data_quality` reports stale source tables, compact evidence freshness, announcement parse/OCR/text/S3 coverage, null exchange-event typing, missing corporate-action/earnings/deal feature influence, and raw-table scan risk, and `operator_health` surfaces these as fix hints/current blockers. The prompt registry is visible in API/UI, and core LLM/Codex output rows now persist prompt id, prompt version, and response schema version.

## 22. Testing

Status: `Partial`

Done:

- `tests/test_advisory_regression.py` is large and covers many regressions.
- Current targeted changes compile and typecheck.
- Manual-review wait-signal creation has a regression test update.

Gaps:

- `To do`: break giant regression test into domain-specific files.
- `To do`: add integration tests for the full advisory state machine using fixture tables.
- `To do`: add tests for every operator UI write action and its downstream effect.
- `Partial`: add tests that prevent stale/manual-closed/incomplete-contract rows from becoming actions or orders. Execution planning blocks stale/manual-closed rows, and incomplete reason-contract broker actions are downgraded with explicit review-only/no-broker boundary evidence; broader stale/manual-closed action/order permutations remain.
- `To do`: add tests for all action conflict precedence cases.
- `Partial`: add frontend unit/e2e tests for Manual Review, Action Queue, Symbol page, and Health. All four now have page-level safety smoke coverage for read-only safety evidence; write-flow interactions and full browser E2E remain open.
- `Partial`: add tests for announcement/bhavcopy evidence point-in-time behavior, exchange-event normalization, compact evidence table freshness, LLM signal schema validation, prompt-version recording, and technical-only versus technical-plus-evidence evaluation. Prompt contract metadata now has focused regression coverage across event evaluation, event-policy manual review, playbook action plans, company-memory reviews, announcement parsing, and technical-threshold promotion reviews; broader evidence/outcome tests remain.

## 23. Security And Safety

Status: `Partial`

Done:

- `.env` is used for sensitive credentials.
- Dhan live execution requires explicit flags.
- Execution planner separates planning and submission.
- Manual/config approvals are audit-only in several places.
- `/api/runtime` and the global operator header expose the current live-trading env gate, defaulting to disabled, without adding any approval or broker-submission control.

Gaps:

- `To do`: ensure `.env`, caches, Chrome profile, token cache, and logs never leak secrets into committed files or UI payloads.
- `To do`: add redaction for tokens, mobile numbers, auth URLs, and broker responses in logs.
- `Done`: add a global “live trading disabled” operator setting visible in UI.
- `To do`: require explicit per-run confirmation for live execution.

## 24. Documentation

Status: `Partial`

Done:

- README describes the current high-level flow.
- `docs/scripts.md` has a useful script/table inventory.
- Operator PRD, manual, implementation docs, and runbooks exist.
- `docs/operators_manual.md` now has an operator boundary table explaining what full advisory, watchers, fast signal refresh, wait signals, and Manual Review each read, write, can change, and cannot do.

Gaps:

- `To do`: docs and code drift frequently because behavior is changing fast.
- `To do`: generate part of script inventory from code metadata instead of manually maintaining it.
- `Done`: add “operator decision effects” table to the manual.
- `Done`: document exact difference between full advisory, watcher signal refresh, wait signals, and manual review.

## 25. Recommended Next Development Order

1. `Done`: build the announcement/bhavcopy evidence quality gate, compact evidence store, and centralized LLM prompt/skill registry before increasing signal authority. The read-only quality gate is implemented and wired into Health, V1 compact evidence tables now exist, the prompt registry is visible in API/UI, and core LLM/Codex output rows persist prompt ids, prompt versions, and response schema versions for audit.
2. `Partial`: formalize the operator/manual-review state machine. Decision effects, incomplete-contract Manual Review downgrades, matched wait-signal reopen/suppression, and durable review-only action-candidate linkage are enforced; superseded downstream states remain.
3. `Done`: add tests for every manual-review decision and the core wait-signal downstream effect.
4. `Done`: connect matched wait signals back to the original manual-review item/action-candidate workflow. Original item suppression, Manual Review follow-up, and durable review-only action-candidate linkage are integrated.
5. `Partial`: add superseded-error cleanup for old processing failures. Active Manual Review/Health suppression, Health grouping, Health preview samples/commands, scheduled/audited dry-run visibility, and durable dry-run/apply marking exist; operator bulk close/apply remains.
6. `Partial`: add action conflict edit/promote UI and wire approved conflict rules into candidate ranking where appropriate. Built-in and promoted exact-match ranking influence, enable/disable UI, explanation editing, disabled-by-default manual-resolution promotion, and validated exact-match condition editing are covered; broader semantic rule types remain.
7. `Done`: add “why not approved buy/sell” explanation to Action Queue.
8. `Done`: add stale API/code version indicator to frontend.
9. `Done`: add current-blockers health card and Advisory Trust Gate: Health now explains what prevents trusting today’s advisory output and whether recommendations are usable, review-only, or blocked.
10. `Done`: add full mocked state-machine tests for recommendation -> action -> manual decision -> wait signal -> match -> refreshed review-only action. Production/UI journey coverage remains broader follow-up work.
11. `Partial`: keep live broker execution disabled until dry-run, reconciliation, and operator approval flows are tested end to end. Backend order previews and live-safety checks now require approval/reconciliation, mocked broker account/order reconciliation plus persisted reconciliation safety-contract coverage exist, and compact API rows preserve raw broker safety contracts; UI approval workflow remains.
12. `Done`: add focused adversarial-review precedence regression for veto beating BUY/WATCH and risk-off market-gated BUY_MORE/WATCH candidates. The deterministic ranking rule is explicit and reason-contract evidence carries veto/review-reason context while losing market-gate context remains visible.
13. `Done`: add focused market-gate reason-contract visibility for positive actions downgraded to Manual Review. Compact API rows preserve the original action, gate reason, breadth, risk-off score, and symbol context for the Action Queue panel.

## 26. Immediate Confidence Statement

The system is useful as an operator-assisted research/advisory platform. It is not yet “perfect” or fully safe as an autonomous trading system.

Trust level today:

- Data ingestion: medium, with source-specific fragility.
- Technical/rule scoring: medium, needs more validation and state tests.
- Event extraction/policy: medium. LLM failures, stale failures, and actionability remain important, but event-evidence quality gates and prompt/schema version visibility are now materially better controlled.
- Announcement/bhavcopy intelligence: medium-low. Useful data exists, but compact evidence stores, null typing repair, corporate-action influence, and LLM company-memory signal review are not yet complete.
- Action consolidation: medium, with conflict precedence now pinned for enabled-rule ranking and an enable/disable UI; edit/promote workflows and broader permutations still need tests.
- Manual review: improving, with explicit decision effects and matched wait reopen handling, but still needs durable downstream state tests.
- Wait signals: newly wired for manual review, but matching is still basic.
- Execution: low for live trading; keep in dry-run/manual approval mode.
- UI/debuggability: improving, but not yet complete enough to explain every decision without opening raw rows.

The next safest path is not giving more authority to more models. The next safest path is upgrading announcement and bhavcopy data into clean, compact, point-in-time evidence, making LLM signal proposals auditable, and keeping final action authority inside explicit, tested, visible deterministic policy.
