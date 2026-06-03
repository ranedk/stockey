# Investment Advisory Roadmap

Updated: `2026-06-02`

This file is the current roadmap for the live advisory stack. It is not a historical design log.

## Primary operator paths

- Downloaders only: `./all_downloaders.sh`
- Queued downloader refresh: `./all_downloaders_queue.sh` followed by `./all_external_workers.sh`
- Parsers only: `./all_parsers.sh`
- Download + parse: `./complete_data.sh`
- Advisory only: `./all_advisory.sh`
- Fast advisory refresh: `./all_advisory.sh --fast`
- Symbol signal refresh: `python -m advisory.signal_refresh --symbol RELIANCE --reason manual`
- Optional research-only model prep/training: `./all_ml.sh`
- Continuous monitoring: `./all_watchers.sh --loop`

Use `python -m advisory.pipeline` only for stage-level debugging and targeted reruns.

## Current project state

The active stack already has:

- production Screener.in registry sync plus ad hoc Screener.in research queries
- normalized advisory screener universe in `advisory_screener_constituents`
- Dhan daily and intraday OHLCV
- Sharpely fundamentals and peer snapshots
- raw macro data, macro snapshots, base regime classification, and a lightweight news overlay
- normalized NSE exchange-event data and daily exchange-event features
- setup scoring with explicit candidate states such as `PASS_NOW`, `WATCH_*`, `ABSTAIN`, and `REJECT`
- watchlist building with screener, regime, overlay, and theme provenance
- exchange announcement ingest and ET RSS news matching
- LLM-based structured event extraction
- deterministic adversarial review over event tensors
- risk sizing, portfolio ranking, lifecycle, and execution planning
- research ledger logging
- intraday feature persistence and intraday-aware advisory rules
- event-model data prep, train, and score scaffolding retained for research only
- broad research-only training universes from ad hoc Screener queries, disabled from the default operating loop
- continuous watch, live alerting, event routing, consolidated action recommendations, and a Nuxt operator frontend backed by an operator-controlled Python API
- watcher-triggered signal refresh in `advisory_signal_refresh_actions`, which gives fast per-symbol buy/watch/review/exit visibility without rerunning full advisory
- hypothesis/playbook Wait Signals in `advisory_wait_signals` and `advisory_wait_signal_matches`, generated from action plans and matched by watchers/signal refresh
- DB-backed operator snapshots, slow-operation logging, health fix hints, and visible sync-state failure reporting

## What is working now

1. Extraction
   - ingest raw data
   - build snapshots
   - extract structured event tensors from announcements and news

2. Prediction
   - rule engine produces scored candidate states
   - investor playbooks and structured events are interpreted logically before technical entry/exit timing
   - event meta-model train/score path exists, but is not the primary production path
   - intraday confirmation features are available

3. Policy
   - abstain layer is explicit
   - adversarial review can penalize, force manual review, or veto
   - risk and portfolio layers apply caps, sizing, and overlap controls

4. Execution support
   - lifecycle and execution-planning outputs exist
   - continuous watch can trigger fast symbol-level signal refresh
   - hypothesis playbooks can create explicit price/news/announcement conditions to wait for before escalation
   - consolidated action recommendations are the current operator/execution handoff
   - the Nuxt operator frontend replaces the old static HTML dashboard path

## Current bottlenecks

1. The highest-priority gap is now UI-first operations: every normal operator action should be visible, explainable, and auditable from the Nuxt app.
2. Remaining high-risk gaps are observability and correctness gaps, not missing major architecture blocks.
3. Some manual workflows still require CLI/manual edits: operator smoke checks, event-model promotion checks, approved technical config diffs, S3 artifact inspection, cron log inspection, and some research-ledger review.
4. Fast signal refresh is intentionally not the authoritative portfolio allocator. Daily `all_advisory.sh` remains the reconciliation path until enough evidence proves incremental advisory is safe.
5. Some non-home API endpoints still return large raw rows and need pagination/compaction.
6. Decision trace summaries are still built live; old trace/intraday rows need hot/cold retention.
7. The serialized external task queue now has concrete NSE/Dhan/Screener handlers plus a queued downloader/worker cron path; direct `all_downloaders.sh` and `complete_data.sh` remain the catch-up/backfill path when a day is missed.
8. `all_advisory.sh` now defaults to bounded local-stage parallelism and skips hidden rule repair; next performance work is stage-budget reporting and moving remaining external repair into queue workers where safe.
9. Continuous watch should add stronger cooldowns, duplicate suppression, and explicit per-source failure counters.
10. Approved technical threshold reviews still require manual config edits; reviewed-diff generation would reduce operator mistakes.
11. Legacy “promotion audit” naming should be migrated to “reliability check” once DB migration is safe.

## Highest Priority: UI-First Operations

Goal:

- manage the whole project from the operator UI for normal workflows
- keep CLI commands available for debugging, cron, and emergency repair
- make every write action explicit, versioned, auditable, and non-trading by default
- never hide failures, fallbacks, stale data, or skipped stages

Required operator UI coverage:

1. System health and fix hints
   - show current status for Postgres, Redis, Dhan token/cache, API, Nuxt, cron jobs, source freshness, slow operations, and fallback spikes
   - add a UI action to run the read-only smoke check and display the resulting fix hints
   - expose recent cron logs with latest-run status, recovered/manual-interrupt state, and traceback snippets

2. Hypothesis and investor playbook management
   - create, preview, edit, version, activate/deactivate, and mark trusted-overlay playbooks from UI
   - run deterministic/LLM scans from UI with clear dry-run versus write mode
   - show reliability checks, matched evidence, action plans, safe action boundaries, and operator questions
   - rename operator-facing “promotion audit” language to “reliability check” everywhere once backend migration is safe

3. Manual-review workbench
   - one queue for `MANUAL_REVIEW`, event-policy review rows, action conflicts, technical threshold reviews, failed extraction rows, and execution blockers
   - allow operator decisions such as `approve_for_manual_config`, `needs_more_data`, `ignore`, `downgrade_to_no_action`, and `watch_for_event`
   - persist every decision with user, timestamp, rationale, before/after payload, and trace links

4. Model/research evidence UI
   - show `advisory.event_model_promotion_check` output in UI: passed gates, failed gates, label coverage, precision lift, ROC-AUC, score freshness, and weekly run count
   - show S3 artifact upload status and latest artifact keys for event-model runs
   - show TS forecast evaluation, event-policy evaluation, technical threshold calibration, and research ledger runs in one research evidence area
   - no model or threshold should become live policy from UI without an explicit manual review/audit trail

5. Config-change assistant, not auto-config
   - generate reviewed diffs for approved technical threshold changes and playbook/config changes
   - show copyable patch, expected impact, affected setup ids, and rollback notes
   - do not auto-apply production YAML changes until the review workflow is proven safe over multiple runs

6. Operations dashboard
   - show the full cron schedule, next/last run, current lock status, and log file links/snippets
   - show safe dry-run buttons for selected jobs: health, hypothesis scan, event-model promotion check, technical calibration review, S3 artifact dry-run
   - block or warn on expensive/long-running jobs from UI unless explicitly confirmed

7. Data/debug visibility
   - paginate and compact events/actions/portfolio/trace APIs so the UI remains fast
   - materialize trace summaries after advisory/watchers instead of rebuilding large traces live
   - show source-to-output lineage: raw event -> OCR/summary -> tensor -> policy/review -> action -> lifecycle/execution plan

Next implementation slices:

1. Add `/api/operations/smoke` and a Nuxt Health action that runs the read-only smoke command and renders fix hints.
2. Add `/api/research/event-model-promotion-check` and a Research Evidence page card for the weekly ML gate.
3. Add `/api/research/event-model-artifacts` to show latest local/S3 artifact metadata and upload status.
4. Add a Manual Review page that merges action conflicts, event-policy manual rows, failed extraction rows, execution blockers, and threshold-review decisions.
5. Add reviewed-diff generation for approved technical threshold decisions.
6. Add cron/log viewer endpoints with bounded log tails and latest marker parsing.
7. Add fallback telemetry persistence and Health-page fallback spike cards.
8. Add pagination/summary-first APIs for events, actions, portfolio, trace, research runs, and logs.

## Next development set

This is the recommended current implementation order.

1. Recommendation reason contract
   - done: define and persist `recommendation_reason_json` plus `reason_contract_status` for final consolidated action rows
   - done: fail-soft incomplete broker-action reason contracts into `MANUAL_REVIEW` before execution can see them
   - done: expose reason-contract status in action traces and dashboard detail views
   - done: enrich action-candidate raw context from latest `advisory_candidates` and `advisory_market_regime` snapshots before reason-contract validation
   - done: enrich action-candidate raw context from latest `advisory_event_evaluations`, `advisory_event_reviews`, and `advisory_playbook_action_plans` snapshots before reason-contract validation
   - done: add a frontend detail view that expands `recommendation_reason_json` into separate screener, technical, event, playbook, macro/regime, risk, and competing-candidate panels

2. Top 50% market context tracker
   - done: create a daily market-context universe table for the top 50% of investable Indian equities by configurable ranking, initially market cap plus traded value
   - done: track announcements, news, exchange events, technical leadership, sector movement, and macro sensitivity for this universe even when names are not current recommendations
   - done: summarize this universe into sector breadth, leadership rotation, event clusters, and risk-on/risk-off context
   - done: feed this context into event/playbook interpretation and macro/regime gates, not directly into trade execution
   - done: expose “Market Context: Top 50%” in the operator frontend
   - next: add a lower-priority watcher queue for top-context names so event coverage improves without evaluating every broad-market event with LLMs

3. Investor playbook action framework
   - done: treat these as investor playbooks in the operator UI instead of statistically validated trading policies
   - done: generate playbook action plans for matched news/announcements, including urgency, checks, safe action boundary, and trusted-overlay flag
   - done: use Codex as the action planner with deterministic fallback when LLM planning is disabled or unavailable
   - done: wire trusted playbook action plans into action consolidation as review/risk-overlay candidates, not direct trades
   - done: show playbook overlays as a first-class trace domain in the Operator trace UI
   - done: seed `config/hypotheses.yaml` with 8 starter playbooks, including the austerity/risk-off example
   - done: add YAML importer for versioned playbooks
   - done: add Operator UI support to preview normalized/generated playbooks before saving
   - done: add edit/status controls for existing playbooks in the Operator UI
   - done: replace statistical promotion language with `active_review` and `trusted_overlay` lifecycle statuses
   - done: add reliability checks that show historical coverage, point-in-time forward returns, benchmark excess returns, and sector excess returns without treating sparse playbooks as statistically validated
   - done: use `master_sharpely_equity.sector_code` for sector excess comparisons because live `sharpely_stock_meta` and peer snapshots are currently contaminated
   - done: sync canonical `NIFTY` benchmark rows from `nseindia_indices` into `dhan_ohlcv_daily` after index parsing, and prefer NSE index history for NIFTY technical/regime benchmark loaders
   - next: rename legacy backend table/API names from promotion-audit to reliability-check once data migration is safe

4. Event-to-action interpretation framework
   - done: add deterministic event-class policies for earnings beats, growth acceleration, margin expansion, order wins, promoter buying/selling, pledge reduction/increase, regulatory notices, management resignations, buybacks, dividends, dilution, analyst meets, and neutral corporate actions
   - done: persist why each evaluated event became `BUY_WATCH`, `MANUAL_REVIEW`, `REDUCE_EXPOSURE_REVIEW`, or `NO_ACTION` in `advisory_event_policy_actions`
   - done: bridge event-policy actions into consolidated action recommendations as review/risk overlays only, never direct broker actions
   - done: add bounded LLM manual-review refinement so review rows get operator notes, possible actions, future events to wait for, and questions; non-actionable rows are downgraded to `NO_ACTION`
   - done: expose event-policy action counts, policy checks, LLM notes, wait-for events, and operator questions in the Nuxt Event Inbox
   - done: make macro/regime state adjust positive action strength and sizing before action consolidation reaches portfolio/execution policy
   - done: add research-only realized forward-return evaluation for event-policy action types, classes, score buckets, and confidence buckets
   - next: use event-policy evaluation summaries plus operator review feedback to propose manual threshold/config changes

5. Technical/lifecycle calibration
   - keep `advisory.technical_engine` as the single swing-technical state machine
   - done: add research-only technical threshold calibration against realized forward OHLCV returns
   - done: add operator API/page for reviewing latest technical threshold calibration summaries and copying candidate configs
   - done: add LLM-assisted manual promotion review that writes review evidence and a pending patch but does not apply thresholds automatically
   - done: add explicit manual approval/rejection audit records for reviewed threshold patches, with copyable final patch guidance and no automatic config edits
   - calibrate target multiples, stop distances, partial-exit rules, and time-stop defaults using realized lifecycle outcomes
   - next: add optional reviewed-diff generation against `config/advisory_setups.yaml` so approved patches are easier to apply by hand
   - surface technical sub-scores, trigger archetype, stop, invalidation, and exit reason as first-class operator UI fields

6. Runtime robustness and observability
   - done: update generated cron/template to run data refresh, watchers, hypothesis scans, TS workflow/evaluation, advisory, event-policy evaluation, weekly technical calibration, and frontend supervision
   - done: add read-only operator health smoke test and Nuxt health page for DB, Redis, Dhan token, cron logs, table freshness, and optional dependencies
   - done: add deterministic `[stockey.script]` lifecycle markers to primary shell wrappers and prefer those in cron-log health parsing
   - done: add Data Health fix hints plus filters for errors, warnings, recovered rows, and OK rows
   - done: recover manual `KeyboardInterrupt` logs when mapped output tables have fresher rows than the interrupted log
   - done: add API self-check latency and Dhan cached-token age/expiry to the operator health payload and Data Health page
   - done: surface operator snapshot freshness, slow-operation issues, and failed watcher/router sync-state rows in operator health and the Nuxt Health page
   - done: record dashboard section-loader failures in payloads instead of only printing them
   - done: make watcher cycle failures persist `advisory_sync_state.status=error` and publish error messages before returning
   - next: add a single smoke-test command for API + DB + frontend dependency checks
   - next: add a health section for recent fallback usage by module/model/source so fallback spikes are visible without grepping logs
   - keep cron/frontend logs visible from the operator app without adding write/trading controls

## Next Most Important Tasks

1. Compact or paginate non-home operator API endpoints.
   - `/api/events`, `/api/actions`, `/api/portfolio`, and trace endpoints should return summary rows by default and detail rows on demand.
   - Add response-size tests and slowlog thresholds per endpoint.

2. Add a single operator smoke command.
   - Target command: `python -m advisory.operator_smoke`.
   - It should run API health, DB freshness, snapshot freshness, frontend type/dependency checks, and selected pure-Python regression smoke tests.

3. Add fallback telemetry.
   - Persist fallback events for LLM disabled/fallback, Codex fallback, Redis fail-soft, live-builder fallback, and Dhan identity fallback.
   - Surface fallback counts on the Health page and in fix hints when they spike.

4. Build trace summary materialization.
   - Precompute symbol/event trace summaries after advisory/watchers.
   - Keep raw trace tables for audit, but serve summary tables to frontend by default.

5. Implement hot/cold retention for intraday and trace rows.
   - Keep recent rows in hot Postgres tables.
   - Archive old rows to S3-compatible storage or compact tables.
   - Add retention reports before delete/archive.

6. Build the NSE ingestion queue.
   - One worker should drain queued NSE work with conservative rate limits.
   - Persist attempt count, next retry, last error, and cookie/session reset events.

7. Generate reviewed config diffs for technical threshold approvals.
   - Use approved `advisory_technical_threshold_promotion_decisions` rows to generate a copyable patch against `config/advisory_setups.yaml`.
   - Do not auto-apply config changes until operator review remains clean over multiple runs.

### 0A. Build a proper Nuxt operator app

Problem:

- The old static dashboard was useful, but it is not enough for debugging a live advisory system.
- It shows final state more than process state.
- It is hard to answer:
  - what happened when this news or announcement arrived?
  - did OCR/transcription/summarization/classification run?
  - what did the event evaluator extract?
  - what rules, model features, technical state, regime, and reviewer checks changed?
  - why did the system recommend buy, sell, hold, watch, or manual review?
  - whether duplicate screeners or conflicting theses were consolidated correctly

Target:

- create a first-class operator frontend in `apps/operator-web`
- stack:
  - Nuxt 3
  - Vue 3
  - TypeScript
  - Tailwind CSS
  - Pinia for client state
  - TanStack Query or Nuxt data fetching for API caching
  - Charting: ECharts or lightweight-charts for price, forecast, and regime views
  - Table/grid: TanStack Table or AG Grid community if needed
- keep the backend as Python-owned initially; the Nuxt app should consume JSON/API endpoints rather than reimplementing domain logic
- static `live_dashboard/` generation is deprecated; the Nuxt app is now the operator UI target

Core pages:

- Overview
  - current portfolio
  - today's recommendations
  - action queue at the top
  - current watchlist
  - exited recommendations
  - TS Watch / TimesFM research cards
  - live alerts
- Event Inbox
  - all latest news and announcements
  - processing status for each item: downloaded, OCR/transcribed, summarized, categorized, parsed, evaluated, reviewed, routed
  - raw source links and stored document artifacts
  - event tensor output
  - investor playbook matches
  - event-class policy interpretation
  - final action impact
- Decision Trace
  - symbol-level timeline of recommendations, action changes, watch updates, exits, and live alerts
  - one expandable trace per decision showing:
    - screener provenance
    - technical state
    - regime/macro context
    - exchange-event context
    - news/announcement context
    - event model score
    - adversarial review
    - risk sizing
    - lifecycle/exit policy
    - consolidated action winner and losers
- Investor Playbooks
  - manage investor playbooks as versioned reliability overlays
  - link each playbook to observable event patterns
  - run historical scans over news/announcements/macro data
  - generate action plans with checks, urgency, safe action boundary, and trusted-overlay permission
  - feed only trusted overlays into review/risk-overlay candidates
- Research Ledger
  - list experiments
  - show configs, date ranges, validation protocol, result metrics, and data snapshots
  - compare runs side by side
- Data Health
  - ingestion freshness by source
  - failed announcements/OCR/summarization/LLM/Codex runs
  - stale Dhan/OHLCV symbols
  - playbook/action-plan freshness
  - optional event-model label coverage for research-only runs
  - Redis/Postgres/cron status
- Admin / Runbook
  - show operator commands
  - trigger safe dry-run jobs where appropriate
  - show cron schedule and last run logs

Backend/API target:

- add a small Python API service, likely FastAPI, under `advisory/api` or `server/api`
- initial endpoints should be mostly read-only, with explicit operator/audit write controls:
  - `/api/summary`
  - `/api/actions`
  - `/api/portfolio`
  - `/api/watchlist`
  - `/api/events`
  - `/api/events/{unique_id}/trace`
  - `/api/symbols/{symbol}/trace`
  - `/api/hypotheses`
  - `/api/hypotheses/{id}/backtests`
  - `/api/research-runs`
  - `/api/data-health`
- expose Server-Sent Events or WebSocket later for live watcher updates
- keep write endpoints gated and explicit; no accidental live trading from the frontend

Implementation slices:

1. Done: add `docs/operator_app_prd.md` with UX, data contracts, and safety boundaries.
2. Done: scaffold `apps/operator-web` with Nuxt 3, Vue 3, TypeScript, Tailwind, Pinia, and starter pages.
3. Done: add FastAPI operator service with read endpoints plus controlled operator/audit write endpoints.
4. Done: reuse the existing `advisory.live_dashboard` payload builder behind an API-compatible JSON contract so the Nuxt app can reuse current data quickly.
5. Started: build the Overview page first with better hierarchy and filtering; it now shows the action queue and symbol trace loading, but trace rendering is still raw JSON.
6. Started: build Event Inbox and Decision Trace as readable stage/timeline pages; these are the main debugging gaps.
7. Started: richer Data Health now has fix hints, cron latest-run parsing, recovered/manual-interrupt handling, and status filters. Add frontend views for TS Watch and research ledger next.
8. Add SSE/WebSocket updates from Redis pub-sub once the operator app is stable.
9. Done: remove static `live_dashboard/` generation from cron/watch paths; serve operator state through API + Nuxt.
10. Next: add a single operator smoke-test command that validates API, DB reads, Node/npm, and Nuxt dependency health.

Do not:

- put trading submit buttons in V1
- make the frontend compute investment decisions
- let the frontend mutate production hypotheses or setup rules without versioning and validation
- hide failed OCR/LLM/Codex/event-router steps

### 0B. Add event decision traces and explainability tables

Problem:

- The pipeline has many useful intermediate outputs, but they are not tied together into one operator-visible trace.
- Debugging currently requires reading several tables and logs manually.

Target:

- create a durable decision trace layer that records every meaningful state transition and evidence bundle.
- every news/announcement that touches a watched or invested symbol should produce a trace record, even if the final action is `NO_CHANGE`.

Suggested tables:

- `advisory_event_processing_runs`
  - `unique_id`, `symbol`, `source_type`, `stage`, `status`, `started_at`, `completed_at`, `error`, `input_hash`, `output_hash`
- `advisory_decision_traces`
  - `trace_id`, `asof_date`, `symbol`, `unique_id`, `trigger_type`, `previous_action`, `new_action`, `action_changed`, `final_action`, `final_reason`
- `advisory_decision_trace_steps`
  - one row per stage:
    - source ingest
    - OCR/transcription
    - summary
    - categorization
    - structured extraction
    - event evaluation
    - hypothesis match
    - adversarial review
    - rule engine
    - technical state
    - risk sizing
    - lifecycle
    - action consolidation
- `advisory_action_conflicts`
  - losing action candidates per symbol/date with reason they lost to the winner

Trace payload should include:

- input row identifiers
- model names and prompt versions
- rule IDs and threshold values
- key feature values before/after
- reason text fit for UI display
- raw JSON payload path or compact JSON snapshot
- error details, retries, and fallback behavior

Implementation slices:

1. Done: add trace writer utility with append-only semantics.
2. Done: trace event ingest, event evaluation, adversarial review, lifecycle, rebalance actions, and final action consolidation.
3. Done: add `python -m advisory.symbol_trace` support for symbol-level timelines.
4. Done: add API endpoint `/api/events/{unique_id}/trace`.
5. Started: render traces in the Nuxt Event Inbox.
6. Done: add symbol-level action timeline endpoint so action consolidation traces without `unique_id` are visible from portfolio/action views.
7. Started: render symbol traces from the Nuxt Overview action and recommendation cards.
8. Done: normalize trace API responses into stage summaries so the frontend does not show raw JSON blobs by default.
9. Started: add domain-specific trace cards for technical state, event tensor, adversarial review, lifecycle, action consolidation, and execution eligibility.
10. Done: trace risk sizing, portfolio allocation, macro context, and exchange-event context so the UI can explain sizing and vetoes end to end.
11. Done: trace execution-order planning, live-safety checks, submission, reconciliation, broker ids/status, and skipped/blocked execution reasons.
12. Done: add frontend trace filters by domain, status, problems, execution blockers, action changes, and event-driven changes.
13. Done: persist trace filters in the URL and add deep links to specific trace decisions.
14. Done: create playbook action-plan framework over investor reliability overlays.
15. Done: wire trusted playbook action plans into action consolidation as review/risk-overlay candidates.

### 0B.1. Make every recommendation explainable

Problem:

- A stock can enter the advisory flow from screeners, events, technicals, playbooks, watchlists, or lifecycle state.
- If the final recommendation does not say why it was screened and selected, the operator cannot trust it.
- A recommendation without a clear reason chain should not appear as a clean investable idea.

Target:

- every final action row must carry a complete reason contract
- the reason contract should be readable by the frontend and auditable through traces
- missing reason fields should downgrade the action to `MANUAL_REVIEW`

Suggested `recommendation_reason_json` fields:

- `screened_by`
  - setup id
  - screener name/query
  - screener date
  - setup state before policy
- `selection_reason`
  - concise reason the name survived screening
  - key fundamental/technical/event features
  - whether it is a new entry, watch continuation, add-on, hold, reduce, or exit
- `event_reason`
  - event ids
  - event class
  - what happened
  - expected impact
  - materiality/confidence
- `playbook_reason`
  - matched playbook ids
  - matched evidence
  - action-plan checks
  - production-allowed flag
- `technical_reason`
  - technical state
  - score
  - trigger archetype
  - pivot/support/stop/invalidation
- `macro_reason`
  - regime
  - macro risk state
  - whether macro amplified, neutralized, or blocked the idea
- `risk_reason`
  - position-size logic
  - liquidity constraints
  - sector/exposure caps
  - abstain/manual-review reasons
- `action_consolidation`
  - winning action
  - losing actions
  - why winner won

Implementation slices:

1. Done: add reason-contract builder utility in advisory code.
2. Done: enrich final action reason contracts from candidates, portfolio/lifecycle candidates, event evaluations, playbook action plans, and macro/regime context.
3. Done: enforce reason completeness before a broker-action row can remain `BUY`, `BUY_MORE`, `SELL`, or `PARTIAL_SELL`; incomplete rows are downgraded to `MANUAL_REVIEW`.
4. Done: show a compact reason summary and expandable full reason contract in the Nuxt Overview and Decision Trace pages.
5. Done: add regression tests that assert clean broker actions cannot ship without a reason contract.
6. Done: add execution-layer defense so stale action rows with missing/incomplete contracts are blocked before broker handoff.

Do not:

- allow generic reasons such as “score passed” without feature/evidence detail
- let LLM-generated prose replace persisted deterministic fields
- hide rejected/losing reasons when multiple strategies disagree

### 0B.2. Track top 50% market context

Problem:

- The current pipeline focuses mostly on watched/recommended symbols.
- Many useful signals come from broader market leaders: announcement clusters, sector rotation, macro sensitivity, breadth, and leadership breakdowns.
- The system needs market context without trying to trade the entire market.

Target:

- maintain a daily “top 50% market” universe for context
- track news, announcements, exchange events, technical leadership, and sector movement for that universe
- summarize this into market/sector context that informs playbook/event interpretation and risk policy

Initial definition:

- start from the investable equity universe with valid Dhan identity and recent OHLCV
- rank by configurable blend of:
  - latest market cap when available
  - 20-day average traded value
  - liquidity/spread filters
- keep the top 50% by rank percentile
- persist membership point-in-time

Suggested tables:

- `advisory_market_context_universe`
  - `asof_date`
  - `symbol`
  - `rank_pct`
  - `market_cap`
  - `avg_traded_value_20d`
  - `sector`
  - `included_reason`
- `advisory_market_context_events`
  - mapped announcements/news/exchange events for top-50% names
- `advisory_market_context_daily`
  - sector breadth
  - event clusters
  - leadership rotation
  - macro-risk interpretation
  - risk-on/risk-off summary

Implementation slices:

1. Done: build `advisory.market_context` to persist top-50% membership and summary rows daily.
2. Done: extend announcement/news watchers to also ingest top-50% market-context names with lower priority than active watchlist names.
3. Done: build daily context features: sector breadth, leader breakdowns, event clusters, exchange-event context, and macro/regime summary.
4. Done: feed market context into event/playbook action planning as background evidence.
5. Done: show “Market Context: Top 50%” in the operator frontend.
6. Done: add a lower-priority watcher queue for top-context names so event coverage improves without evaluating every broad-market event with LLMs.
7. Done: add regression checks that positive playbook actions remain non-executable in weak/risk-off broad-market context.
8. Next: tune deterministic materiality keywords and add dashboard counts for `triggered` versus `context_observed` top-context events.

Do not:

- treat top-50% membership as a buy signal
- run full LLM evaluation on every top-50% event without prioritization
- let broad context override direct stock-level vetoes, liquidity rules, or exits

### 0C. Build an investor playbook action framework

Problem:

- There are many qualitative market playbooks from experienced investors.
- They should not be hardcoded directly into trades or treated as statistically validated because the data is sparse and high-dimensional.
- Example: if the Prime Minister or top authority calls for austerity, the market may fall and the system should go cash or short.

Target:

- represent playbooks as versioned operator rules
- scan news, announcements, macro releases, policy events, and market context for hypothesis triggers
- generate an action plan that decides what else to check and how to act safely
- use `trusted_overlay` status only to allow review/risk-overlay candidates, never direct automatic trades

Playbook object fields:

- `hypothesis_id`
- `title`
- `description`
- `source`
- `status`: `draft`, `active_review`, `trusted_overlay`, `retired`
- `trigger_scope`: market, sector, symbol, portfolio
- `trigger_patterns`
  - keywords
  - entity types
  - authority/person/role
  - source reliability
  - contradiction filters
  - required evidence count
- `expected_effect`
  - index down
  - sector down/up
  - volatility up
  - liquidity stress
  - go cash
  - short candidate
  - reduce exposure
- `holding_window`
- `test_universe`
- `validation_protocol`
- `reliability_rule`

Suggested tables/config:

- `config/hypotheses.yaml` for authored hypotheses
- `advisory_hypothesis_matches` for point-in-time trigger matches
- `advisory_playbook_action_plans` for LLM/deterministic action plans
- `advisory_action_recommendations` receives trusted-overlay playbook action plans as non-executable review/risk-overlay candidates

Implementation slices:

1. Done: add `docs/hypothesis_lab.md` with schema and examples.
2. Done: add `config/hypotheses.yaml` with the first 5-10 starter playbooks, including the austerity example.
3. Done: add `advisory/hypothesis_engine.py` to save hypotheses and match them against persisted news/announcements.
4. Done: persist hypotheses and matches in `advisory_hypotheses` and `advisory_hypothesis_matches`.
5. Done: add `advisory_playbook_action_plans` and generate action plans with Codex/fallback.
6. Done: surface playbook creation, scans, matches, and latest action plans in the Nuxt Playbooks page.
7. Done: bridge trusted-overlay action plans into `advisory.action_recommender` as review/risk-overlay candidates.
8. Only when status is `trusted_overlay`, allow use as:
   - market overlay
   - sector risk cap
   - portfolio de-risking flag
   - manual-review reason

Do not:

- treat a hypothesis match as automatic proof
- let one headline trigger portfolio liquidation without confirmation and validation
- enable shorting in production until separate broker/risk controls exist

### 0. Add an experimental time-series forecast layer

Problem:

- Dhan OHLCV is now available, but the system does not yet evaluate whether time-series foundation models add incremental predictive value.
- Models such as Google TimesFM, Amazon Chronos, and Salesforce Moirai may be useful, but they should not directly create trades without proof.
- OHLCV-only forecasts are noisy, so this must start as a research/paper layer rather than another live action source.

Target:

- create a separate forecast feature table, `advisory_ts_forecasts_daily`
- feed existing Dhan daily OHLCV into forecast adapters
- forecast multiple horizons, initially `5d`, `10d`, and `20d`
- store forecast outputs as features:
  - expected return
  - forecast price
  - downside / upside quantiles
  - probability of positive return
  - signal quality
  - explicit experimental action hint
- compare against simple baselines before trusting any foundation model

Design rules:

- do not let TimesFM / Chronos / Moirai directly issue `BUY` or `SELL`
- keep outputs out of Dhan execution until paper results are validated
- forecast returns or relative returns where possible, not just raw price levels
- use walk-forward validation with costs and slippage
- compare against naive momentum, technical engine, and XGBoost/event meta-model baselines
- preserve point-in-time discipline and never train/evaluate with future OHLCV leakage

Integration path:

- `advisory.ts_forecast_features` builds forecast features from `dhan_ohlcv_daily`
- event/risk/meta-model layers may later consume forecast features as one input
- dashboard shows TS forecast context under an experimental, non-execution section
- action recommender may consume TS forecasts only after paper validation shows incremental value

Implementation slices:

1. Done: add `advisory/ts_forecast_features.py` with a dependency-free `naive_momentum_v1` baseline adapter.
2. Done: add `advisory_ts_forecasts_daily` persistence contract.
3. Done: add regression coverage for experimental forecast row generation.
4. Done: add optional TimesFM adapter behind the same output schema.
5. Done: add `advisory/ts_forecast_evaluator.py` for matured forecast evaluation against future Dhan OHLCV returns after costs.
6. Done: add `advisory/ts_forecast_workflow.py` for Screener.in selection, Dhan OHLCV refresh, TimesFM forecast generation, and experimental TS watchlist persistence.
7. Add optional Chronos / Moirai adapters behind the same output schema.
8. Next: add paper-portfolio evaluator for forecast-only decisions.
9. Next: add research-ledger entries comparing TS forecasts against naive momentum and current advisory actions.
10. Done: surface forecast context in the dashboard as experimental, non-execution evidence.
11. Only after validation, add TS forecast features to the event meta-model / risk model as low-weight inputs.

## Active roadmap

### 1. Deferred: event-model dataset training

Priority:

- keep the code available for research-only experiments
- do not make event-model training a blocker for live advisory
- run `all_ml.sh` weekly as evidence generation only; do not let it change live policy automatically

Done already:

- `advisory.event_model_data_prep`
- `advisory.training_universe`
- `EVENT_MODEL_TRAINING_V1`
- `all_ml.sh`
- weekly Sunday cron for `all_ml.sh`
- `advisory.event_model_promotion_check` read-only gate for deciding whether weekly ML evidence is ready for manual operator review
- S3 artifact upload after successful training via `advisory.event_model_artifact_store`

Still needed:

- more matured labeled rows across dates, sectors, event classes, and regimes
- statistically usable walk-forward validation after costs
- baseline comparison against passive benchmark, deterministic event policy, and naive momentum
- research-ledger records for config, validation protocol, costs, baselines, and known failure cases
- promotion only as a low-weight review/risk/action input after repeated encouraging weekly runs

Pick this up when:

- `python -m advisory.event_model_promotion_check` returns `Decision: review_candidate`
- no failed gates are shown for label coverage, diversity, score freshness, successful weekly runs, precision lift, or ROC-AUC
- the operator review still confirms no leakage, no obvious event-class overfit, and no concentration in one symbol/date cluster

### 2. Improve the regime stack

Target direction:

- add explicit shock detection
- move toward a more persistent regime estimate
- keep regime separate from setup policy

Do not:

- invent new regime names every week
- let regime labels become an uncontrolled news bucket

### 3. Build a proper macro feature layer

Problem:

- CPI, WPI, FPI, RBI rates, FBIL/G-sec curves, US macro/FRED, ISM, and related inputs are ingested.
- Current usage is partial and too raw.
- The event model and risk engine should not join directly to many raw macro tables.

Target:

- create one normalized point-in-time table, for example `advisory_macro_features_daily`
- make it the only macro feature input for regime, event ML, setup policy, and risk sizing
- preserve publication lag and causal availability dates

Source mapping:

- `mospi_cpi`: inflation level, month-over-month change, rolling acceleration, food/core proxies where available
- `eaindustry_wpi`: WPI headline/category momentum, commodity and manufacturing inflation pressure
- `fii_investments` and `fii_derivatives`: FPI equity/debt flow, 1d/5d/20d rolling flow, derivative risk appetite
- `rbi_bank_rates`: policy-rate regime, last rate change, days since change
- `fbil_gsec_quote` and `fbil_gsec_par`: short/long yield levels, curve slope, yield shock
- `macro_usa`: US 10Y, dollar/liquidity proxy fields, global risk pressure
- `macro_usa_ism`: US demand/manufacturing cycle context
- `macro_india_gdp`: slower growth backdrop, used with explicit publication lag only

Feature groups:

- inflation pressure: CPI/WPI levels and momentum
- rates pressure: RBI policy, G-sec yield changes, curve slope
- foreign-flow pressure: FPI rolling flows and derivatives positioning
- global pressure: US yields, ISM, global risk indicators
- macro stress score: combined normalized signal for policy/risk sizing
- sector sensitivity tags: optional WPI/CPI category features for chemicals, pharma, metals, textiles, autos, energy, FMCG

Integration points:

- `advisory.regime_engine`: use macro features as inputs to base regime and shock/stress detection
- `advisory.news_overlay_engine`: use macro stress as context, not as a replacement for news overlay
- `advisory.event_meta_model`: join macro features on event anchor date as point-in-time predictors
- `advisory.risk_engine`: reduce sizing/caps when macro stress is high
- `advisory.portfolio_engine`: optionally tighten sector/overlap caps during adverse macro regimes

Implementation slices:

1. Done: add `advisory/macro_features.py`.
2. Done: create/persist `advisory_macro_features_daily`.
3. Done: add tests for macro feature ordering and null-tolerant feature generation.
4. Done: wire macro features into the regime engine.
5. Done: add macro feature columns to the event-model dataset.
6. Done: add a simple macro stress multiplier to risk sizing.
7. Done: document current raw-to-feature mappings in the advisory manual.

Do not:

- wire every raw macro table directly into every model
- use revised macro data as if it was known earlier
- make macro features a hard trade selector by themselves

### 4. Plug NSE deal and exchange-event data into advisory

Problem:

- NSE off-market and corporate-event data is already downloaded and parsed.
- Current advisory still relies mostly on screeners, OHLCV, fundamentals, announcements, news, macro, and intraday features.
- Block/bulk deals, insider deals, short selling, corporate actions, and earnings calendar rows should become point-in-time event/features instead of unused raw tables.

Target:

- create one normalized point-in-time exchange-event table, for example `advisory_exchange_events`
- create one daily per-symbol feature table, for example `advisory_exchange_features_daily`
- use exchange-event data as structured evidence for event model, adversarial review, setup scoring, risk sizing, and watch prioritization
- pass recent relevant exchange events/features into the LLM as bounded structured context when evaluating a symbol event
- preserve event date and disclosure date separately wherever available

Source mapping:

- `nseindia_block_deals`: institutional large trade signal, buyer/seller concentration, block-value shock
- `nseindia_bulk_deals`: unusual accumulation/distribution signal, repeated buyer/seller patterns
- `nseindia_short_selling`: bearish positioning pressure, repeated short interest pressure
- `nseindia_insider_deals`: promoter/director/insider buying or selling, holding change, confidence/governance signal
- `nseindia_corporate_actions`: split/bonus/dividend/rights/actions as technical adjustment and event context
- `nseindia_earnings_events`: upcoming/results calendar context and post-result event anchors
- `nseindia_events`: generic NSE event feed where available

Feature groups:

- block/bulk pressure:
  - block deal value as percent of market cap and ADV20
  - net buyer/seller side if inferable
  - repeated named buyer/seller in last 5/20 trading days
  - deal cluster count by symbol and counterparty
- insider pressure:
  - insider buy/sell count and traded value over 30/90 days
  - promoter/director category flag where inferable
  - holding percent change after transaction
  - insider accumulation/distribution score
- short-selling pressure:
  - short-selling value/quantity over 1/5/20 days
  - short pressure versus ADV20
  - repeated short-selling streak flag
- corporate-event context:
  - upcoming earnings flag
  - days to earnings/results
  - corporate-action adjustment flag
  - recent split/bonus/dividend context
- exchange event score:
  - combined normalized signal for event-model features and risk/reviewer context
  - should not directly force buy/sell decisions

Integration points:

- `advisory.event_meta_model`: join exchange features on event anchor date as point-in-time predictors
- `advisory.llm_event_evaluator`: include recent structured exchange events and exchange features in context so the LLM can classify event significance, materiality, surprise, direction, and expected decay
- `advisory.adversarial_review`: penalize or veto when insider selling, short pressure, or adverse exchange events contradict a setup

### 5. Add thesis buckets, horizon policy, and exit-event policy

Problem:

- `advisory_portfolio_orders` currently tells us allocation, priority, and invalidation, but not the intended holding contract.
- Some ideas are target-driven, some are explicitly time-window trades, and some should stay live only while the data continues to support them.
- The dashboard should show not just what was selected, but why it belongs in a given portfolio bucket and what would exit it.

Target:

- divide portfolio ideas into three thesis buckets:
  - `TARGET`
  - `TIME_HORIZON`
  - `DATA_DEPENDENT`
- add explicit exit-event rules that override target/time bucket behavior
- surface bucket rationale, screener provenance, and exit conditions in the dashboard and lifecycle views

Policy model:

- target bucket:
  - valuation or rerating driven
  - fields: `target_price`, `target_basis`, `target_confidence`, `target_review_date`
- time-horizon bucket:
  - defined event/trade window
  - fields: `expected_horizon_days`, `horizon_type`, `horizon_end_date`, `horizon_basis`
- data-dependent bucket:
  - thesis remains live while evidence remains supportive
  - fields: `continue_while`, `key_monitor_fields`, `recheck_frequency`
- exit-event layer:
  - invalidation hit
  - stop hit
  - thesis contradiction
  - adverse insider / exchange-event cluster
  - regime deterioration
  - liquidity breakdown

Implementation slices:

1. Add bucket and exit-policy columns to `advisory_portfolio_orders`.

### 5A. Add consolidated action recommendations

Problem:

- The system now has portfolio rows, lifecycle rows, rebalance rows, and execution rows.
- Even though portfolio/lifecycle snapshots are mostly deduped to one row per symbol, operator intent is still spread across multiple tables.
- The execution engine still reasons from separate portfolio and rebalance paths instead of one explicit action contract.
- The dashboard can show multiple adjacent concepts for the same stock, which is noisy when the operator just needs the single current action.

Target:

- create one point-in-time table, `advisory_action_recommendations`
- enforce one winning action per `asof_date + symbol`
- make this the single contract for:
  - operator dashboard
  - trading API handoff
  - execution dry-runs

Canonical action set:

- `BUY`
- `BUY_MORE`
- `PARTIAL_SELL`
- `SELL`
- `TIGHTEN_STOP`
- `MANUAL_REVIEW`
- `HOLD`
- `WATCH`

Priority model:

- `SELL` outranks everything
- `PARTIAL_SELL` outranks `BUY_MORE`
- `MANUAL_REVIEW` outranks `BUY` and `WATCH`
- `TIGHTEN_STOP` is lower than liquidation actions but higher than passive `HOLD`
- `BUY_MORE` outranks `BUY`
- `BUY` outranks `WATCH`

Source mapping:

- `advisory_rebalance_actions`
  - `exit_*` -> `SELL`
  - `trim_winner` -> `PARTIAL_SELL`
  - `add_on_pullback` -> `BUY_MORE`
  - `tighten_stop` -> `TIGHTEN_STOP`
  - `review_*` / `review_manual` -> `MANUAL_REVIEW`
- `advisory_portfolio_orders`
  - approved live recommendation -> `BUY`
- `advisory_position_lifecycle`
  - open with no stronger action -> `HOLD`
- `advisory_watchlist`
  - active non-abstain watch row -> `WATCH`

Execution integration:

- `advisory.execution_engine` should plan only from `advisory_action_recommendations`
- only broker-submittable actions should become order intents:
  - `BUY`
  - `BUY_MORE`
  - `PARTIAL_SELL`
  - `SELL`
- review-only actions must remain visible but non-submittable

Dashboard integration:

- show one current `Action Recommendation` per stock
- explain:
  - action code
  - action priority
  - winning reason
  - winning source table
  - action fraction if any
  - execution mode

Implementation slices:

1. Done: add `advisory/action_recommender.py`
2. Done: persist `advisory_action_recommendations`
3. Done: wire `actions` stage into `advisory.pipeline`
4. Done: make `advisory.execution_engine` read the consolidated action table
5. Done: make the dashboard/operator view prefer the consolidated action over raw rebalance rows
6. Next: make action conflicts readable in the Nuxt Decision Trace page instead of only persisting them.

Do not:

- let multiple active actions survive for the same symbol on the same advisory date
- allow both a plain `BUY` and a `BUY_MORE` execution plan for the same symbol in the same run
- hide manual-review conflicts; surface them explicitly as the winning action

### 6. Build a proper swing technical buy/exit engine

Problem:

- Current technical usage is useful but still too mixed between generic rule checks, intraday confirmation, and event/risk overlays.
- We need one explicit swing technical engine for Indian equities that answers:
  - is the stock in a tradable uptrend?
  - is it forming a constructive structure?
  - is there accumulation and leadership?
  - is it executable with sensible risk?
  - has the setup triggered?
  - has the technical thesis broken?
- This is not an intraday trading engine.

Target:

- create a dedicated technical state engine that outputs daily:
  - pre-entry:
    - `REJECT`
    - `IGNORE`
    - `WATCHLIST`
    - `NEAR_PIVOT`
    - `READY`
    - `BUY_TRIGGERED`
  - post-entry:
    - `HOLD`
    - `ADD_ON_PULLBACK`
    - `PARTIAL_EXIT`
    - `FULL_EXIT`
    - `EMERGENCY_EXIT`
- make the engine explicitly prefer:
  - liquid Indian equities
  - structurally strong names
  - breakout and trend-continuation setups
  - clean execution and realistic stop placement

Design rules:

- do not build this around standalone RSI / MACD / stochastic signals
- do not turn Elliott Wave, candlestick patterns, or Fibonacci into primary buy logic
- use indicators as tools, not as the edge
- keep the engine swing-oriented and based on trend, structure, participation, leadership, and tradability

Hard pre-score filters:

- reject before scoring when any of these fail:
  - minimum average daily traded value
  - minimum price
  - minimum median volume
  - maximum spread percent
  - maximum recent gap frequency
  - excessive recent circuit behavior
  - obvious event hazard for short swing holds
  - highly erratic chart with no clean structure
- use config-driven thresholds, not hardcoded literals inside scoring code

Technical score model:

- total score: `100`
- trend regime score: `25`
  - price vs 20 / 50 / 150 DMA
  - slope of moving averages
  - higher-high / higher-low behavior
  - distance from 52-week high
  - 1m / 3m / 6m trend persistence
- structure quality score: `30`
  - flat base / tight range / VCP-like contraction / ascending base / breakout shelf / constructive pullback
  - base duration and depth
  - volatility contraction
  - support respect
  - pivot clarity
  - overhead supply estimate
- participation / volume score: `20`
  - breakout-day volume vs 20-day average
  - accumulation vs distribution balance
  - dry-up on pullbacks and contractions
  - retest behavior
- relative strength score: `15`
  - 1m / 3m / 6m performance vs benchmark
  - sector-relative performance
  - RS improvement before breakout
  - resilience during market weakness
- tradability / risk score: `10`
  - ATR percent
  - spread percent
  - average traded value
  - gap risk
  - circuit frequency
  - realistic stop distance

Suggested V1 thresholds:

- minimum gate before `READY` / `BUY_TRIGGERED`:
  - trend `>= 15/25`
  - structure `>= 18/30`
  - participation `>= 10/20`
  - relative strength `>= 8/15`
  - tradability `>= 6/10`
- state guidance:
  - total `< 55`: `IGNORE` or `REJECT`
  - total `55-69`: `WATCHLIST`
  - total `70-77`: `READY`
  - total `>= 78`: trigger-eligible, but only `BUY_TRIGGERED` after valid entry confirmation

Entry trigger archetypes:

- breakout entry:
  - close above valid pivot
  - meaningful breakout width
  - strong close
  - breakout volume confirmation
- breakout-retest entry:
  - breakout already happened
  - muted selling into retest
  - support hold / reversal confirmation
- trend-pullback entry:
  - controlled pullback into 20 / 50 DMA or prior support shelf
  - volume contraction
  - reversal / hold confirmation
- reclaim entry:
  - reclaim after false breakdown / failed breakout trap
  - strong close and preferably volume support

Post-entry exit framework:

- full exit:
  - pivot failure
  - decisive support break
  - structure invalidation
  - leadership collapse
  - regime deterioration with broken structure
- partial exit:
  - sharp extension away from 20 DMA / base
  - blow-off behavior
  - event-risk reduction
  - reward/risk deterioration without thesis break
- trailing exit:
  - aggressive: below recent swing support
  - balanced: below 20 DMA / pivot support
  - slower: below 50 DMA
  - optional ATR-based trail
- abnormal distribution exit:
  - clustered high-volume down days
  - repeated failed rebounds
  - deteriorating RS
- time-stop:
  - breakout triggered but no follow-through within configurable bars
- emergency exit:
  - severe gap / event shock / execution-risk breach

Conviction and sizing guidance:

- conviction bucket from technical score:
  - `LOW_CONVICTION`: `70-75`
  - `MEDIUM_CONVICTION`: `76-84`
  - `HIGH_CONVICTION`: `85+`
- do not size from score alone
- final sizing must still respect:
  - stop distance
  - liquidity
  - event-risk proximity

Context tags:

- allow only low-weight tags:
  - trend maturity early / middle / late
  - climax extension risk
  - broad market favorable / neutral / hostile
  - earnings proximity
  - sector momentum strong / weak
- these are tags, not primary buy logic

Implementation direction:

- add a dedicated module, for example `advisory/technical_engine.py`
- keep raw daily feature generation in `advisory/technical_features.py`
- do not move this into `advisory/intraday_features.py`
- intraday should stay secondary and timing-oriented, not primary swing logic

Required data/model additions:

- extend daily technical feature set with:
  - 52-week high distance
  - 20 / 50 / 150 DMA slope features
  - base duration / depth
  - range contraction measures
  - support-touch / pivot clarity metrics
  - accumulation / distribution day counts
  - breakout-volume ratios
  - gap frequency
  - circuit frequency
  - spread / tradability metrics where available
  - benchmark and sector-relative performance windows
- add explicit technical state outputs to the advisory layer

Pipeline integration points:

- `advisory.technical_features`:
  - extend feature generation for swing-structure fields
- new `advisory.technical_engine`:
  - compute technical sub-scores, technical state, entry type, invalidation candidate, and exit state
- `advisory.rule_engine`:
  - consume technical state instead of only generic technical rule fragments
  - use `WATCHLIST`, `NEAR_PIVOT`, `READY`, `BUY_TRIGGERED` directly
- `advisory.watchlist_builder`:
  - preserve technical engine state, pivot, support, and trigger archetype
- `advisory.position_lifecycle`:
  - map technical post-entry states to `HOLD`, `ADD_ON_PULLBACK`, `PARTIAL_EXIT`, `FULL_EXIT`, `EMERGENCY_EXIT`
- `advisory.risk_engine`:
  - combine conviction with liquidity and stop realism
- Nuxt operator frontend:
  - show technical state, trigger archetype, structure commentary, and exit reason cleanly

Implementation slices:

1. Done: add missing swing feature fields in `advisory.technical_features`.
2. Done: add config block for hard filters and technical thresholds.
3. Done: add `advisory.technical_engine` scoring buckets and state machine.
4. Done: add `BUY_TRIGGERED` trigger logic for breakout / retest / pullback / reclaim.
5. Started: add post-entry technical exit states and map them into lifecycle.
6. Started: replace scattered technical-only rule fragments in `advisory.rule_engine` with the explicit technical engine output.
7. Next: surface technical state, pivot, trigger type, sub-scores, and exit condition in Nuxt operator traces.
8. Done: add regression tests for:
   - clean breakout
   - near-pivot base
   - loose / junk structure reject
   - failed breakout full exit
   - sharp extension partial exit
   - dead-money time stop
9. Done: add research-only technical threshold calibration against realized forward OHLCV outcomes after costs.
10. Done: add operator UI/API views for threshold calibration summaries and copyable configs.
11. Next: promote thresholds only through an explicit manual config-change workflow after reviewing calibration evidence.

Do not:

- let intraday confirmation become the primary swing buy engine
- make oscillator crosses the core logic
- overfit V1 with too many market-regime branches
- mix technical trigger confirmation with loose event-driven overrides in a way that hides the actual setup quality

### 4A. Rebuild exit policy, stop-loss policy, and profit-booking policy

Problem:

- current lifecycle logic is too shallow
- `stop_price` and `invalidation_price` are effectively the same number
- `trim_winner` and `tighten_stop` exist mostly as labels, not as a real management framework
- post-entry technical states exist in `advisory.technical_engine`, but lifecycle does not use them yet
- execution only handles full `exit_*` actions and does not translate partial exits into broker behavior

Target:

- separate execution stop from thesis invalidation
- route lifecycle through `technical_engine.evaluate_post_entry_state(...)`
- add real profit-booking and stop-tightening rules
- persist explicit exit-policy metadata and action semantics
- allow Dhan execution planning to handle:
  - full exits
  - partial exits
  - stop-based exits

Required semantics:

- `invalidation_price`
  - thesis-level break
  - wider, slower, structural
  - if broken, the original setup is wrong
- `stop_price`
  - execution/risk-control stop
  - tighter than invalidation where possible
  - can be tightened as the trade matures
- `partial exit`
  - reduce risk or lock profit without killing the whole position
- `full exit`
  - stop hit, invalidation hit, technical failure, emergency gap/event damage
- `time stop`
  - setup-family aware
  - should not be one flat `20 day` rule

Implementation slices:

1. Make `risk_engine.compute_invalidation(...)` return distinct `stop_price` and `invalidation_price`.
2. In `position_lifecycle`, load latest technical context and use `evaluate_post_entry_state(...)`.
3. Map post-entry technical states into:
   - `exit_emergency`
   - `exit_technical_failure`
   - `trim_winner`
   - `add_on_pullback`
   - `hold`
4. Keep stop/invalidation exits as highest-priority overrides.
5. Replace the flat stale-review rule with bucket/setup-aware time-stop logic.
6. Add action metadata for exit sizing:
   - full exit
   - partial exit fraction
   - no-execution review-only action
7. Update `execution_engine` so Dhan execution supports:
   - full `SELL`
   - partial `SELL`
   - skip non-executable review-only actions
8. Surface stop, invalidation, technical exit reason, and partial-exit plan in dashboard/lifecycle views.

Current implementation status:

- lifecycle now recomputes a deterministic management plan per open position:
  - target price
  - expected holding days
  - horizon end date
  - target review date
  - recommended stop
- target-hit positions become `trim_winner`
- time-horizon losers after horizon expiry become `exit_time_stop`
- action recommendations and execution planning understand the time-stop full-exit path
- `all_advisory.sh --fast` exists for quick refreshes without slow watch/news/repair work

Remaining:

- calibrate target multiples and horizon defaults against realized trade outcomes
- add optional low-worker parallel execution for pure DB/CPU stages only
- do not parallelize Chrome/browser-connected scraping in the same browser session

Do not:

- treat stop and invalidation as synonyms
- trigger partial exits only from arbitrary PnL thresholds without technical context
- let execution auto-submit ambiguous review actions
- let technical post-entry logic live separately from lifecycle
2. Add lifecycle fields so exit-event triggers and bucket state are visible in `advisory_position_lifecycle`.
3. Add first-pass deterministic bucket classification using setup family, holding horizon note, event context, and invalidation guidance.
4. Show bucket, bucket reason, exit-policy summary, and screener provenance in the live dashboard.
5. Later: allow richer target-price and horizon inputs from valuation/event models instead of only deterministic defaults.

Do not:

- force all ideas into target-price logic
- let bucket type replace invalidation/stop/event exits
- let the LLM invent unconstrained bucket policy without deterministic fields persisted downstream
- `advisory.rule_engine`: allow optional setup rules for insider accumulation, block-deal accumulation, or short-pressure avoidance
- `advisory.risk_engine`: reduce sizing when short pressure or insider distribution is elevated; optionally boost review priority for insider accumulation
- `advisory.watchlist_builder` and `advisory.continuous_watch`: prioritize watched symbols with fresh exchange-event activity
- `advisory.dashboard`, `advisory.symbol_trace`, and `advisory.setup_trace`: show latest exchange-event signals and feature provenance

Implementation slices:

1. Done: add `advisory/exchange_events.py` to normalize raw NSE deal/event rows into `advisory_exchange_events`.
2. Done: add `advisory/exchange_features.py` to build `advisory_exchange_features_daily`.
3. Done: add point-in-time joins from exchange features into `advisory.event_meta_model`.
4. Done: add recent exchange-event context to `advisory.llm_event_evaluator`, including compact raw event rows plus derived feature summaries.
5. Done: add exchange contradiction checks to `advisory.adversarial_review`.
6. Add optional setup-rule fields for exchange signals without making them mandatory for all setups.
7. Done: add risk sizing haircuts for elevated insider selling or short pressure.
8. Surface exchange-event data in dashboard and trace utilities.
9. Done: add tests for normalization, feature generation, point-in-time joins, LLM context, review, and risk behavior.
10. Done: document raw-to-feature mappings in the advisory manual and scripts docs.

Do not:

- treat one block/bulk deal as automatic alpha
- send unfiltered raw NSE history to the LLM
- use trade date as known date when disclosure date is later
- make low-quality counterparty inference mandatory
- let exchange-event features override abstention, liquidity, governance, or risk controls
- block all insider selling mechanically without checking materiality and context

### 5. Deferred: shift more prediction into tabular models

Target direction:

- keep LLMs in extraction and adversarial review
- keep tabular predictors as optional research
- prioritize deterministic event/playbook policies, macro gates, and technical timing for production

Likely next slices:

- only if resumed: learned reviewer or meta-reviewer
- only if resumed: richer event-memory and decay features
- only if resumed: more model-driven setup ranking

### 6. Strengthen event memory

Target direction:

- store event persistence and decay by event type, sector, and regime
- separate surprise, credibility, and persistence instead of using one flat event score

This should support:

- better forward-return labels
- better reviewer features
- less prompt-driven heuristics

### 7. Tighten the continuous watch loop

Current continuous watch is live, but can improve with:

- symbol cooldowns
- duplicate alert suppression
- better per-cycle prioritization
- richer dashboard summaries
- clearer operator-visible reasons for reruns

Keep the design:

- incremental
- watchlist-scoped
- fail-soft
- cheap enough to run every few minutes

### 8. Keep research discipline strict

The research ledger is implemented. The next step is using it consistently.

For all serious model work:

- log config
- log horizon
- log as-of date range
- log validation protocol
- keep point-in-time discipline
- prefer abstention over forced opinions

### 9. Low Priority: advisory scaling safeguards

These are intentionally tracked in docs only, not in Postgres. Full daily advisory remains the authoritative path; incremental advisory is not trusted enough to become the default workflow.

Low-priority performance backlog:

- bound the advisory candidate universe per run so expensive stages do not scan unbounded historical/cross-sectional data
- add stage-level freshness checks so unchanged macro, exchange, technical, intraday, trace, and snapshot outputs can be reused safely
- move heavy text/blob payloads out of hot Postgres rows, keeping excerpts, hashes, classifications, and object-store pointers hot
- add DB indexes only from slow-operation and slow-query evidence, while keeping duplicate-index reports clean
- add hot/cold retention for trace, intraday, event, and alert rows with dry-run cleanup reports
- evaluate incremental advisory only as research-only for watch/update/exit/manual-review flows, not as the source of truth

## Deprioritized or intentionally avoided

- free-form multi-agent debate systems
- LLM-led trade selection
- automatic mutation of production screeners from research ideas
- full-market LLM polling loops
- backtest theater without leakage control

## Working principle

Keep the stack separated into:

1. extraction
2. prediction
3. policy
4. execution

LLMs belong mainly in extraction and adversarial review. Execution authority stays in deterministic policy and risk code.
