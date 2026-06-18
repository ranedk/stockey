# Operator App PRD

## Objective

Build a Nuxt operator app that explains what the advisory system is doing, not just what it finally recommends.

The app should answer:

- What changed today?
- What should I act on first?
- Why did a symbol become buy, sell, hold, watch, or manual review?
- What happened when a news item or announcement arrived?
- Which evidence, rules, models, and reviewer checks affected the decision?
- Which data sources are stale, failing, or missing?

## Stack

- Nuxt 3
- Vue 3
- TypeScript
- Tailwind CSS
- Pinia
- TanStack Query or Nuxt data fetching
- ECharts or lightweight-charts
- TanStack Table or AG Grid Community if tables become complex
- Vitest for components
- Playwright for smoke tests

## Safety

The app is operator-controlled, not auto-trading controlled.

Broker execution remains in the Python execution engine and CLI/operator workflow. The app may display staged execution orders and Dhan readiness, but it must not submit orders. Current write paths are limited to operator/audit workflows such as hypothesis creation, hypothesis scans, and technical-calibration review decisions.

The operator paper portfolio is also a UI write path, but it is explicitly paper-only. `/recommendations` Buy/Sell buttons write only to `advisory_operator_portfolio_ledger`; `/paper-portfolio` reads that ledger and displays entry price, exit price, current price, and percentage P&L. These pages must not mutate advisory recommendations, authoritative portfolio rows, Dhan execution rows, execution approvals, or broker orders.

The target operating model is UI-first:

- normal operator work should happen from the Nuxt app
- CLI commands remain available for cron, debugging, and emergency repair
- every UI write must be explicit, versioned, and auditable
- no UI workflow should hide failed extraction, stale data, fallback usage, skipped stages, or execution blockers

## Pages

### Overview

Shows the current state:

- top action queue
- today's recommendations
- current portfolio
- watch recommendations
- exited recommendations
- TS Watch / TimesFM research recommendations
- live alerts
- current regime and macro overlay
- Market Context: Top 50% breadth, leadership, event clusters, and leading symbols

The action queue should be first because it is the operator's priority list.

### Event Inbox

Shows every relevant news item and exchange announcement.

Each event should display:

- source, symbol, timestamp, and source URL
- processing status
- OCR/transcription status
- concise summary
- categories
- structured extraction output
- event tensor
- matched hypotheses
- final action impact
- errors and retries

Status stages:

- discovered
- downloaded
- OCR/transcribed
- summarized
- categorized
- parsed
- evaluated
- hypothesis matched

### Action Queue

The Action Queue shows the final consolidated action per symbol after portfolio, lifecycle, watchlist, event-policy, market-gate, and conflict-rule inputs are resolved.

The Overview page must show source-specific payload freshness near the top of the page before the operator reads individual rows. At minimum this includes Home, Actions, Portfolio, and Signal Refresh generated-at/status/stale-warning metadata. Stale payloads should be visibly marked as a trust issue, not hidden inside raw JSON.

The `Broker candidate` status filter maps to the backend `approved` filter value for compatibility, but its meaning is strict: it returns only rows whose final consolidated action is a broker-candidate action. Rows that became `MANUAL_REVIEW` or review-only must not appear in this filter just because an upstream source row had `approved` text such as `portfolio_status=approved`.

Each row should show its `action_queue_contract`, final action, source action, reason contract, conflict winner/losers, execution safety gate, and whether it is review-only, blocked, watch/hold, or broker-candidate.

Each row must also render the backend `final_state_trust` contract before the detailed panels. This is the operator's first-read checklist: final action, queue status, broker-candidate flag, reason-contract state, action-transition preconditions, feature-freshness state, execution-boundary state, blockers, and warnings. This panel is read-only and must not submit broker orders.

Each row must also show a plain-English Portfolio Eligibility panel before the detailed trace blocks. This panel should say whether the row is a portfolio-handoff candidate, broker-candidate but blocked, manual review, watch-only, hold/context, exit/risk-reduction, or not portfolio-eligible, and it must include the next operator step plus the top blockers.

Rows with open symbol-level identity/source issues from `/api/identity-issues` must show an Identity / Source Blocker panel in the Action Queue before deeper trace evidence. The panel must explain that the problem is operational data quality, not investment judgment, include the source error and repair hint, link to the Identity Issues workbench, and include the issue in portfolio/readiness blockers.
- adversarially reviewed
- routed
- action updated

### Recommendations And Paper Portfolio

The `/recommendations` page is the operator-controlled paper-entry surface. It loads the latest consolidated `advisory_action_recommendations`, enriches each row with current price, and shows whether the symbol is already open in the operator paper portfolio.

Required behavior:

- show only action-capable recommendations: `BUY`, `BUY_MORE`, `SELL`, and `PARTIAL_SELL`
- hide `WATCH`, `HOLD`, and `MANUAL_REVIEW` rows from this page because they belong in watch/manual-review surfaces, not the paper-action surface
- filter action-capable rows through current paper state: `BUY` is shown only when the symbol is not open, while `BUY_MORE`, `SELL`, and `PARTIAL_SELL` are shown only when the symbol is already open in the operator paper ledger
- render exactly one visible button per recommendation row: `BUY`, `BUY_50%`, `SELL`, or `SELL_50%`
- visible safety copy that the action is paper-only and does not submit broker orders
- clear price fields: current price, reference price, target, stop, score

The `/paper-portfolio` page is the compact paper-state view. It should show only the operator ledger-derived position state: symbol, open/closed status, entry price, exit price, current price, and percentage P&L. The reset control must require explicit confirmation and call the paper-ledger reset API only.

### Decision Trace

Shows why a recommendation exists.

For a symbol/date/action, display:

- previous action
- new action
- whether action changed
- winning consolidated action
- losing/conflicting actions
- reason contract status, missing evidence, original action, and grouped rationale sections
- screener provenance
- technical state
- feature freshness and missing/stale data-input blockers
- technical threshold calibration summaries with copyable config JSON
- LLM-assisted technical threshold promotion reviews with manual-only pending patch payloads
- manual approval/rejection audit state for threshold-review patches
- regime and macro context
- exchange-event context
- news/announcement context
- company-memory compact evidence coverage, including whether required announcement, bhavcopy, and technical evidence was present or missing
- event model output
- event-policy action (`BUY_WATCH`, `MANUAL_REVIEW`, `REDUCE_EXPOSURE_REVIEW`, `NO_ACTION`) with policy class, checks, LLM operator notes, wait-for events, and operator questions
- adversarial review
- risk sizing
- lifecycle and exit policy
- execution eligibility

### Symbol Detail

Symbol Detail must surface symbol-scoped operational blockers before the operator interprets price/action data. Open identity issues from `/api/identity-issues?symbol=...`, especially Dhan security-id mapping failures, should show as identity/source blockers with repair hints and a link to the Identity Issues workbench. These blockers are operational data issues, not investment recommendations, and explain skipped OHLCV pulls, missing latest prices, or blocked execution previews.

Symbol Detail must also show source-specific payload freshness for Actions, Portfolio, Events, Trace, Data Inputs, and Identity. This panel is separate from the stale snapshot warning and exists so the operator can tell whether a symbol page is mixing fresh and stale payloads before interpreting final action, P&L, target, stop, or event evidence.

Resolved action conflicts are shown here for audit/debug. They should not appear in Manual Review unless the conflict still requires operator action.

Event-policy rows refined to `NO_ACTION` are also audit/debug records, not Manual Review work. If action consolidation created a generic manual-review row for the same event, the Manual Review API should suppress that shadow row and keep only the detailed actionable event-policy item when one exists.

Manual Review copy must be written for an operator, not for a developer. The visible card title, reason, summary, questions, and wait signals should use plain English. Internal labels and enums belong in the source-row drawer or trace details, not in the primary decision text.

Manual Review cards must show compact source evidence before the raw source drawer. The evidence contract should include source kind, a short headline, and a small fact list derived from the already-loaded source row so operators can understand event-policy rows, action conflicts, execution blockers, failures, identity issues, threshold reviews, action reviews, and wait-signal follow-ups without loading bulky raw JSON.

The Manual Review page defaults to the `Investment review` lane. Operational failures such as OCR/parser/API/Codex errors remain available under `Technical issues`, but they should not be mixed into the primary investment-decision queue.

The Manual Review API and UI must show backend-authored queue-level category counts before the item list: investment judgment, technical/data repair, research/config review, and item-impact buckets such as entry/watch, exit/risk, follow-up, execution-blocking, and ops/data. These summaries are informational only and must not change portfolio, action, config, or broker state.

The Manual Review API includes an active-queue contract. The UI must show that only displayed active rows require operator action; closed, duplicate, and matched-wait-suppressed rows are audit/debug history. The contract must also state that Manual Review decisions do not mutate portfolio rows, action recommendations, or broker orders.

Each active Manual Review item must also show a `visibility_lifecycle` contract. This explains why the row is active now, whether it is new, annotated, reopened after newer source evidence, or a matched wait-signal follow-up, which decisions will close it, which decisions will keep it active, and that Manual Review decisions do not mutate portfolio, action recommendation, or broker state.

Manual Review decisions have bounded effects:

- `downgrade_to_no_action`, `ignore`, `approve_for_manual_config`, and `mark_fixed` are closing decisions. They remove the item from the active Manual Review queue through `advisory_manual_review_decisions`; they do not mutate portfolio, action recommendations, or broker orders.
- `watch_for_event` is non-closing. It requires an explicit `Event to wait for`, records the operator note, and creates an active `advisory_wait_signals` row from that text. Watchers/signal refresh can later match that wait signal against fresh news, announcements, and announcement documents.
- `needs_more_data` and `add_operator_note` annotate the item only.

The Manual Review UI must show a decision guide for all dropdown choices and a selected-choice effect checklist before save. The checklist must state active-queue effect, wait-signal effect, portfolio effect, action-recommendation effect, and broker effect so operators do not confuse a review decision with an action/portfolio mutation.

### Wait Signals

The `/wait-signals` page is the operator view for conditions created by playbooks and Manual Review `watch_for_event` decisions. It separates active, matched, expired, and closed waits, labels whether a row came from Manual Review or a playbook action plan, and shows the latest matched evidence in plain language. A match is evidence that a condition fired; it is not a trade by itself.

When a Manual Review-created wait signal matches, the API keeps the original `manual_review_item_id`, source key, wait question, and matched evidence together. The Manual Review page surfaces that matched wait as follow-up work so the operator can close it, keep watching, or run a refresh/advisory flow. This follow-up item does not mutate portfolio rows, action recommendations, or broker execution.

### Conflict Rules

Shows deterministic action-conflict rules and recent conflict outcomes.

Use this page to inspect:

- enabled conflict-resolution rules
- priority and resolution action for each rule
- recent conflicts matched by each rule
- unresolved/manual-required conflict combinations that need a new deterministic rule or explicit operator decision

Resolved conflicts are audit records, not manual-review tasks. Manual Review should only contain conflicts where `requires_manual_resolution = true` or `resolution_status` is `unresolved` / `manual_required`.

### Execution Approvals

The global navigation must link to `/execution-approvals` because live-broker safety is an operator-critical workflow, not a hidden debug page.

This page shows dry-run execution rows, approval/reconciliation/evidence/live-allowance gates, blockers, and the manual live-submit preflight command/token. UI actions may write approval audit, safety-contract, reconciliation, evidence-review, or live-allowance records, but they must not submit broker orders. Live submission remains a deliberate CLI-only operation unless a separate live-submit UX is designed with equal or stronger safety gates.

Live allowance is not sufficient by itself. Applying live allowance marks `live_submission_allowed=true` but also requires a fresh post-allowance approval decision; preflight and CLI live submission must block until the safety contract records that fresh approval after the allowance timestamp.

Each execution approval row must show a live-readiness checklist, not just a free-text blocker list. Required gates include dry-run row status, broker identity, positive quantity, positive reference price, operator approval, broker reconciliation, repeated dry-run evidence, live allowance, and fresh post-allowance approval. The checklist is explanatory only; it must not submit broker orders.

### Investor Playbooks

Allows the operator to manage investor playbooks as reliability-tracked overlays and convert matched evidence into bounded action plans.

V1 has a controlled write path for playbooks:

- create a playbook from the operator UI
- store it in `advisory_hypotheses`
- scan persisted news and announcements
- show latest matches
- persist matches in `advisory_hypothesis_matches`
- generate action plans in `advisory_playbook_action_plans`
- show examples of matched headlines/announcements
- show whether the playbook is draft, active review, trusted overlay, or retired
- use Codex to decide what else to check, urgency, safe action boundary, and whether a review-only trusted overlay is allowed
- keep suggested decisions as operator review/risk-overlay candidates, not direct trades
- import versioned playbooks from `config/hypotheses.yaml`
- preview the normalized/generated playbook payload before saving from the UI
- edit existing playbooks and explicitly move them between active review and trusted overlay from the UI
- run and display reliability checks for historical coverage and operator evidence
- show point-in-time forward-return summaries from Dhan OHLCV in reliability checks
- show excess-return evidence versus NIFTY in reliability checks
- show sector excess-return evidence using `master_sharpely_equity.sector_code`
- bridge trusted playbook action plans into action consolidation as `review_only` risk-overlay/manual-review candidates

### Regime Review

Allows the operator to review dynamic macro/news regime overlays before any rule is implemented.

V1 behavior:

- proposals are generated by `python -m advisory.regime_overlay`
- proposals are shown on `/regime-overlays`
- operator decisions are `approve_for_testing`, `promote_to_review_rule`, `needs_more_evidence`, or `reject`
- decisions write audit rows to `advisory_regime_overlay_decisions`
- no decision directly changes action policy, sizing, portfolio state, or broker execution
- `promote_to_review_rule` means the proposal is ready for a separate reviewed config/rule implementation, not live production use

### Research And Calibration

Shows research and calibration evidence without changing live strategy thresholds automatically:

- event-policy realized-return evidence
- technical threshold calibration summaries
- LLM-assisted threshold promotion reviews
- operator approval/rejection audit rows
- optional research run records where present

### Data Health

Shows:

- ingestion freshness by source
- failed OCR/summarization/parsing/evaluation rows
- Dhan OHLCV freshness
- playbook/action-plan freshness
- optional event-model label coverage for research-only runs
- cron last-run status
- Redis/Postgres connectivity status

## Backend

Use the FastAPI operator service:

Initial implementation:

```sh
python -m advisory.api.app --host 127.0.0.1 --port 8765
```

The API serves from `advisory_operator_snapshots` first and falls back to the live payload builder when no fresh snapshot is available. The Nuxt landing page uses compact `/api/home` payloads to avoid repeatedly loading full dashboard sections.

Current endpoints:

- `GET /api/health`
- `GET /api/health/details`
- `GET /api/home`
- `GET /api/summary`
- `GET /api/actions`
- `GET /api/portfolio`
- `GET /api/operator-portfolio/recommendations`
- `GET /api/operator-portfolio`
- `POST /api/operator-portfolio/action`
- `POST /api/operator-portfolio/reset`
- `GET /api/watchlist`
- `GET /api/market-context`
- `GET /api/technical-calibration`
- `POST /api/technical-calibration/promotion-review`
- `GET /api/technical-calibration/promotion-reviews`
- `POST /api/technical-calibration/promotion-review/decision`
- `GET /api/events`

List-style operator endpoints should be compact by default when they are used for normal UI reads. `/api/actions` and `/api/portfolio` default to compact rows so the Action Queue, Portfolio views, and latency probe do not pull bulky raw evidence; use `?compact=false` only for short debugging, and prefer `/api/actions/detail` or `/api/portfolio/{symbol}/detail` for full row evidence. `/api/home` omits duplicated action/today cards by default because the UI loads them from `/api/actions` and `/api/portfolio`; use `include_action_cards=true` only for legacy/debug reads. `/api/portfolio` supports `bucket=today_recommendations|current_recommendations|exited_recommendations|portfolio|lifecycle` so normal UI reads fetch only the selected tab and keep the other sections empty while preserving pagination/meta totals. The Action Queue list uses persisted decision-time feature freshness by default; current live freshness recomputation is explicit through `refresh_feature_freshness=true`.

Watchlist reads follow the same list contract. `/api/watchlist` accepts `section=watch_recommendations|watchlist|ts_watch_recommendations|ts_forecast_watch|ts_forecast_eval_summary|ts_forecast_paper_summary` plus `limit` and `compact`; normal UI reads should request one section at a time. Compact TS forecast watch rows omit bulky swing/position window internals and retain summary fields; use `compact=false` only for targeted debugging.
- `GET /api/event-policy`
- `GET /api/event-policy/evaluation`
- `GET /api/events/{unique_id}/trace`
- `GET /api/events/{unique_id}/trace/summary`
- `GET /api/symbols/{symbol}/trace`
- `GET /api/symbols/{symbol}/trace/summary`
- `GET /api/hypotheses`
- `POST /api/hypotheses`
- `POST /api/hypotheses/preview`
- `POST /api/hypotheses/{hypothesis_id}`
- `POST /api/hypotheses/{hypothesis_id}/promotion-audit`
- `POST /api/hypotheses/run`
- `GET /api/data-health`

Later:

- Server-Sent Events or WebSocket updates from Redis pub-sub.
- operations endpoints for read-only smoke checks, event-model promotion checks, S3 artifact inspection, bounded cron log tails, fallback telemetry, and reviewed config-diff generation.

## Data Contracts

The app should not compute investment logic.

It should consume:

- `advisory_action_recommendations`
- `advisory_portfolio_orders`
- `advisory_position_lifecycle`
- `advisory_rebalance_actions`
- `advisory_watchlist`
- `advisory_live_watch_alerts`
- `advisory_event_evaluations`
- `advisory_event_risks`
- `advisory_event_policy_actions`
- `advisory_event_policy_eval_summary`
- `advisory_news_events`
- `advisory_watch_events`
- `announcement_pipeline_documents`
- `advisory_ts_forecasts_daily`
- `advisory_ts_forecast_watchlist`
- `advisory_ts_forecast_eval_summary`
- `advisory_research_runs`
- future trace/hypothesis tables

## Build Order

1. Done: add operator API using DB snapshots first and the live payload builder as fallback.
2. Done: scaffold Nuxt app.
3. Done: build Overview page with action queue, today's recommendations, TS watch, health summary, and symbol trace loading.
4. Done: build Event Inbox with event trace loading and event-policy action review.
5. Done: add normalized event and symbol trace summary APIs so the UI does not need raw DB JSON.
6. Done: backend tracing now covers announcement ingest, event evaluation, adversarial review, lifecycle, rebalance, and action consolidation.
7. Done: add symbol trace API for action/lifecycle consolidation traces.
8. Started: add readable trace timeline cards in Overview, Event Inbox, and the dedicated Decision Trace page.
9. Started: add domain-specific trace cards for event evaluation, adversarial review, investor playbook overlays, technical state, risk sizing, macro context, exchange-event context, portfolio allocation, lifecycle/exit policy, action consolidation, and execution eligibility.
10. Done: add execution-order planning, live-safety, submission, and reconciliation traces so skipped and non-executable actions are visible.
11. Done: add trace filters by domain, status, problems, execution blockers, action changes, and event-driven changes.
12. Done: persist trace filter state in the URL and add deep links to specific trace decisions.
13. Done: build Investor Playbooks creation, scan, action-plan flow, and production-safe action-consolidation bridge.
14. Done: expose event-policy action counts, operator notes, wait-for events, and questions in the Event Inbox.
15. Done: expose research-only event-policy realized-return summary in the Event Inbox.
16. Done: add UI-first operations workbench foundations, including Health, Operations, Manual Review, Wait Signals, Identity Issues, Signal Quality, Prompt Registry, and Research Evidence pages.
17. Done: add Action Queue and Symbol Detail data-input freshness visibility.
18. Done: persist action decision-time feature freshness and show it in Action Queue/Symbol Detail.
19. Done: enforce feature dependencies in rules, risk, portfolio, lifecycle, and actions, and surface blocked stage gates through Operator Health.
20. Done: add richer per-symbol UI drill-down for current and decision-time stage gate effects.
21. Add live update stream.

## UI-First Operations Workbench

Remaining gaps before the project can be managed almost entirely from UI:

- Feature dependency enforcement: show and enforce which stage was downgraded or blocked by stale/missing required inputs.
- Broader decision-time freshness: extend persisted snapshots beyond consolidated action rows where needed, especially non-action Manual Review sources.
- Ingestion state: standardized downloader/parser run rows, source-specific failure classes, and failed-symbol/source-skip summaries are visible in Health; keep expanding source-specific semantics as new ambiguous failures appear.
- Research/config operations: S3 artifact inspection, event-model research safety controls, and recent research-ledger review are now read-only in Operations; Technical Calibration, Signal Quality, and Event Policy reviewed diffs have audit-only application recording; TS forecast review/application state is visible on the home TS workflow panel. Remaining work is advanced research-ledger reconciliation.
- Data lineage: keep improving raw event -> OCR/summary -> tensor -> policy/review -> action -> lifecycle/execution plan visibility without raw JSON.
- Retention/performance: keep summary-first/paginated endpoints and add hot/cold retention for old trace and intraday rows.

## Frontend Commands

From `apps/operator-web`:

```sh
npm install
npm run dev
```

Set the API base when needed:

```sh
NUXT_PUBLIC_API_BASE=http://127.0.0.1:8765 npm run dev
```
