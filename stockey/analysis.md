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

- `To do`: formal state machine for manual interventions and their downstream effects.
- `To do`: regression tests for every manual-review decision type.
- `To do`: action/reason contract tests covering event-policy, playbook, lifecycle, execution, and wait-signal paths.
- `To do`: better cleanup and de-duplication of old processing failures after the underlying bug is fixed.
- `To do`: explicit “what happens next” visibility everywhere an operator clicks a decision.
- `Partial`: wait signals are now created from `watch_for_event`, but matching remains keyword-based and needs stronger condition types.
- `Partial`: watchers can trigger fast signal refresh, but they are not yet equivalent to a full advisory state transition.
- `Partial`: execution planning exists, but live broker execution should remain disabled until full dry-run/reconciliation tests are complete.

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
- `To do`: add a health check for “running code hash/version” so the UI can tell when the API is stale after edits.

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

Gaps:

- `To do`: old processing failures remain noisy after bugs are fixed. Need a cleanup workflow that marks failures superseded when a later successful parse exists for the same source key.
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

Gaps:

- `To do`: unresolved identity issues need one canonical UI queue with fix instructions.
- `To do`: store every “No Dhan security id mapped” case in a durable table with symbol, source, exchange tried, fallback tried, and suggested action.
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
- `To do`: show “why not approved buy” in action queue for technical candidates.
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

## 11. Adversarial Review

Status: `Partial`

Done:

- Deterministic adversarial review exists.
- It can clear, penalize, force manual review, or veto.
- Risk engine consumes review context.

Gaps:

- `To do`: define exact precedence between adversarial review, event policy, market context, technical signals, and lifecycle exits.
- `To do`: add regression tests for veto beating buy/watch candidates.
- `To do`: show adversarial-review reason in the action queue, not only trace/details.
- `To do`: evaluate veto precision and false positives over realized outcomes.

## 12. Hypothesis, Playbooks, And Wait Signals

Status: `Partial`

Done:

- Hypothesis/playbook config and UI exist.
- Hypothesis scans create matches/action plans.
- Trusted playbooks can bridge as review-only overlays.
- Wait signals exist and match price/news/announcement conditions.
- `watch_for_event` manual-review decisions now create active `advisory_wait_signals` rows.
- Watchers run wait-signal matching.

Gaps:

- `To do`: wait-signal matching is still mostly keyword-based. It needs typed conditions: price level, support break, clarification filing, result update, management commentary, sector event, and explicit expiry.
- `To do`: UI should show wait signals created from manual review separately from playbook-generated wait signals.
- `To do`: when a wait signal matches, the UI should show “what changed” and whether it reopens/escalates the original manual item.
- `To do`: matched wait signals should feed a deterministic action candidate only when the condition type and evidence quality are strong enough.
- `To do`: add tests for manual-review wait signal creation, matching, and closure.

## 13. Rules, Risk, Portfolio, Lifecycle

Status: `Partial`

Done:

- Rule engine creates candidates/rejections.
- Risk engine builds allocations.
- Portfolio engine plans position candidates.
- Lifecycle engine handles hold/exit/rebalance outputs.
- Stop, target, horizon, bucket, and lifecycle concepts exist.

Gaps:

- `To do`: formal state machine from recommendation to assumed entry to holding to exit.
- `To do`: no backdated portfolio changes. This needs tests ensuring new portfolio rows cannot be introduced with historical entry dates unless explicitly imported.
- `To do`: lifecycle needs stronger target/stop update rules and audit trail.
- `To do`: partial exits, add-on buys, time stops, and emergency exits need deterministic tests.
- `To do`: ensure every `MANUAL_REVIEW` in portfolio/lifecycle has human-readable reason, current price, entry assumption, target/stop status, and exact operator question.

## 14. Action Consolidation And Conflict Resolution

Status: `Partial`

Done:

- `advisory_action_recommendations` consolidates to one current action per symbol.
- Action conflicts and conflict rules are persisted.
- Conflict rules page exists.
- Resolved conflicts are audit-only and do not enter Manual Review.
- Reason contracts exist and can downgrade incomplete broker actions.

Gaps:

- `To do`: conflict rules currently annotate and resolve conflict rows, but not all approved rules necessarily influence `rank_action_candidates()` directly.
- `To do`: add UI controls to edit/disable conflict rules and promote manual resolutions into deterministic rules.
- `To do`: every final action should show winning candidate, losing candidates, rule used, and why the loser lost.
- `To do`: add tests for same-symbol duplicate screeners, conflicting buy/sell/manual/watch rows, and market-gate downgrades.
- `To do`: humanize all action reasons before they reach the UI.

## 15. Manual Review Workbench

Status: `Partial`

Done:

- Manual Review page exists.
- Items are separated by lane: investment review, technical issue, research/config.
- Closing decisions remove items from active queue.
- `watch_for_event` now creates wait signals.
- Decision options now explain what happens.
- Technical/operational failures are visible separately.

Gaps:

- `To do`: add regression tests for every decision option:
  - `needs_more_data`
  - `watch_for_event`
  - `add_operator_note`
  - `approve_for_manual_config`
  - `ignore`
  - `downgrade_to_no_action`
  - `mark_fixed`
- `To do`: build an operator-decision state diagram and enforce it in code.
- `To do`: add bulk close for superseded technical failures.
- `To do`: link a manual-review item to its later wait-signal match and any resulting action.
- `To do`: make “closing decision” versus “annotating decision” visually impossible to miss.
- `To do`: show whether the item is investment-impacting, operational, research-only, or execution-blocking.

## 16. Watchers And Fast Signal Refresh

Status: `Partial`

Done:

- `all_watchers.sh` runs continuous watch one-shot.
- OHLCV, announcement, news, router, wait-signal matching, signal refresh, operator snapshot, and trace summary are wired.
- Watcher cursor state exists.
- Watcher lock prevents overlapping runs.

Gaps:

- `To do`: watchers still do not fully replace daily advisory. They produce fast signal refresh rows, not authoritative full portfolio reconciliation.
- `To do`: document which events trigger only signal refresh versus full advisory.
- `To do`: make missed watcher intervals provably catch up across all sources, not just some cursors.
- `To do`: watcher output should show whether it changed an action, created a wait match, or only updated evidence.
- `To do`: add tests for stale cursor, failed run, skipped lock, and catch-up behavior.

## 17. Execution And Broker Integration

Status: `Partial`

Done:

- Execution engine reads consolidated action recommendations.
- Execution planning is separated from live broker submission.
- Reason contracts block incomplete broker actions.
- Dhan auth/token refresh exists.

Gaps:

- `To do`: keep live execution disabled until paper/dry-run reconciliation is proven.
- `To do`: add an execution approval UI gate with dry-run order preview.
- `To do`: add broker account/position reconciliation tests.
- `To do`: prevent live order submission from any stale or manually closed action.
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

Gaps:

- `To do`: API should expose schema/version metadata so frontend can detect stale API.
- `To do`: add typed response models for critical endpoints.
- `To do`: add endpoint-level tests for manual review, actions, symbol trace, health, wait signals, and conflict rules.
- `To do`: compact APIs should be default for large pages.
- `To do`: API should surface stale snapshot warnings consistently.

## 20. Frontend

Status: `Partial`

Done:

- Nuxt/Vue/Tailwind operator app exists.
- Main dashboard, events, symbol page, decision trace, health, hypotheses, manual review, operations, technical calibration, and conflict rules pages exist.
- Manual review now explains selected dropdown effects.
- Symbol links and UI chips have improved visual distinction.

Gaps:

- `To do`: add an end-to-end “operator task journey” view: issue -> decision -> wait signal -> match -> refreshed action -> portfolio/execution implication.
- `To do`: make stale data warnings visible on every page, not just health/snapshot contexts.
- `To do`: add UI tests or at least component-level smoke tests for key pages.
- `To do`: reduce tag/pill/link ambiguity everywhere.
- `To do`: add first-class pages for wait signals, skipped symbols, fallbacks, and unresolved identity issues.

## 21. Observability, Health, And Error Surfacing

Status: `Partial`

Done:

- Operator health exists with fix hints.
- Health page shows skipped/fallback/partial-data concepts.
- Cron logs are written under `logs/cron`.
- Slow-operation log exists.
- Decision traces exist.
- Trace summaries exist.

Gaps:

- `To do`: all failures and fallbacks should have a durable row, not only log lines.
- `To do`: health page should group failures by active/recovered/superseded.
- `To do`: old resolved errors should be closeable or auto-superseded.
- `To do`: add “current blockers before advisory can be trusted” summary.
- `To do`: trace should link manual decisions and wait signals directly.

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
- `To do`: add tests that prevent stale/manual-closed rows from becoming actions or orders.
- `To do`: add tests for all action conflict precedence cases.
- `To do`: add frontend unit/e2e tests for Manual Review, Action Queue, Symbol page, and Health.

## 23. Security And Safety

Status: `Partial`

Done:

- `.env` is used for sensitive credentials.
- Dhan live execution requires explicit flags.
- Execution planner separates planning and submission.
- Manual/config approvals are audit-only in several places.

Gaps:

- `To do`: ensure `.env`, caches, Chrome profile, token cache, and logs never leak secrets into committed files or UI payloads.
- `To do`: add redaction for tokens, mobile numbers, auth URLs, and broker responses in logs.
- `To do`: add a global “live trading disabled” operator setting visible in UI.
- `To do`: require explicit per-run confirmation for live execution.

## 24. Documentation

Status: `Partial`

Done:

- README describes the current high-level flow.
- `docs/scripts.md` has a useful script/table inventory.
- Operator PRD, manual, implementation docs, and runbooks exist.

Gaps:

- `To do`: docs and code drift frequently because behavior is changing fast.
- `To do`: generate part of script inventory from code metadata instead of manually maintaining it.
- `To do`: add “operator decision effects” table to the manual.
- `To do`: document exact difference between full advisory, watcher signal refresh, wait signals, and manual review.

## 25. Recommended Next Development Order

1. `To do`: formalize the operator/manual-review state machine.
2. `To do`: add tests for every manual-review decision and downstream effect.
3. `To do`: create Wait Signals UI page with source, condition, status, matches, and originating manual-review item/playbook.
4. `To do`: add superseded-error cleanup for old processing failures.
5. `To do`: add action conflict edit/promote UI and wire approved conflict rules into candidate ranking where appropriate.
6. `To do`: add “why not approved buy/sell” explanation to Action Queue.
7. `To do`: add stale API/code version indicator to frontend.
8. `To do`: add current-blockers health card: what prevents trusting today’s advisory output.
9. `To do`: add full state-machine tests for recommendation -> action -> manual decision -> wait signal -> match -> refreshed action.
10. `To do`: keep live broker execution disabled until dry-run, reconciliation, and operator approval flows are tested end to end.

## 26. Immediate Confidence Statement

The system is useful as an operator-assisted research/advisory platform. It is not yet “perfect” or fully safe as an autonomous trading system.

Trust level today:

- Data ingestion: medium, with source-specific fragility.
- Technical/rule scoring: medium, needs more validation and state tests.
- Event extraction/policy: medium-low until LLM failures, stale failures, and actionability are better controlled.
- Action consolidation: medium, but conflict precedence needs deeper tests.
- Manual review: improving, but needs a formal state machine and full tests.
- Wait signals: newly wired for manual review, but matching is still basic.
- Execution: low for live trading; keep in dry-run/manual approval mode.
- UI/debuggability: improving, but not yet complete enough to explain every decision without opening raw rows.

The next safest path is not adding more models or more signals. The next safest path is making every state transition explicit, tested, visible, and reversible.
