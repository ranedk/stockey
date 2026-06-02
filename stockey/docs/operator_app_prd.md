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
- adversarially reviewed
- routed
- action updated

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
- technical threshold calibration summaries with copyable config JSON
- LLM-assisted technical threshold promotion reviews with manual-only pending patch payloads
- manual approval/rejection audit state for threshold-review patches
- regime and macro context
- exchange-event context
- news/announcement context
- event model output
- event-policy action (`BUY_WATCH`, `MANUAL_REVIEW`, `REDUCE_EXPOSURE_REVIEW`, `NO_ACTION`) with policy class, checks, LLM operator notes, wait-for events, and operator questions
- adversarial review
- risk sizing
- lifecycle and exit policy
- execution eligibility

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
- `GET /api/watchlist`
- `GET /api/market-context`
- `GET /api/technical-calibration`
- `POST /api/technical-calibration/promotion-review`
- `GET /api/technical-calibration/promotion-reviews`
- `POST /api/technical-calibration/promotion-review/decision`
- `GET /api/events`
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
16. Add live update stream.

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
