# Operator UI -- Review & Simplification Plan

> **Status (2026-06-25):** the consolidation in this plan is now implemented -- unified per-symbol
> state, the on-demand "why", lean/cached endpoints, and a 6-item navigation. See
> [`docs/operator_ui.md`](operator_ui.md) for the shipped console. The remaining deferred steps are
> slimming the legacy `/api/actions` heavy shape and deleting the legacy pages, both kept until proven
> redundant in real use (additive guardrail).

A grounded review of the Nuxt operator UI (`apps/operator-web`, 18 pages) and its FastAPI backend
(`advisory/api/app.py`, ~73 endpoints), plus a plan to make it easier to understand and fast to load.
The guiding rule: **a UI that has every fact but takes tens of minutes to load is worse than a lean
one** -- so every change here is judged on clutter AND load time.

## The problem in one line

The same workflow (an action's full state) is spread across 6 pages, and a handful of endpoints do
per-row work over large tables, risking multi-minute loads. The fix is *consolidation* (fewer,
purpose-built pages) plus *bounded, batched, cached* endpoints.

## Top clutter problems (hardest to understand)

1. **The action queue is dispersed across ~6 places** -- `index` (3 lists), `/actions`,
   `/operator-journey`, `/decision-trace`, `/execution-approvals`, `/manual-review`. An operator must
   visit several pages to understand one action's state, with no clear source of truth.
2. **Portfolio is fragmented into 5 buckets** (today / current / exited / portfolio / lifecycle) with
   no unified "what do I hold right now?" view.
3. **Execution approval is scattered** across `/actions` (status), `/execution-approvals` (queue), and
   `/manual-review` (evidence) -- two different schemas (`final_state_trust` vs
   `execution_safety_contract`).
4. **"Why is this manual_review?" is buried** -- the deciding event-policy / conflict rule lives in
   `/events` + `/action-conflict-rules`, not on the action row.
5. **Feature-freshness is scattered** across action rows, a per-symbol endpoint, and `/health`, with
   inconsistent schemas and no "which symbols are blocked on data?" aggregate.

## Top performance hotspots (biggest load-time risks)

| Endpoint | Problem | Worst case | Fix |
|---|---|---|---|
| `GET /api/actions` | per-row enrichment (company-memory × symbol, feature-gate × row, trust × row) | 100 rows -> ~300 DB calls, 5-30s | batch enrichment by symbol; cache gate effects; precompute trust |
| `GET /api/operator-journey` | 5 stages × per-stage joins, no pagination | 50+ queries/symbol, 2-10s | materialise stage summaries; one query per symbol+date |
| `GET /api/symbols/{symbol}/trace` | no server-side max limit (client can ask 10000) | full-table scan, 50KB+ payload | enforce max limit (e.g. 500); paginate |
| `GET /api/health/details` | freshness + calibration + signal-quality + event-policy scanned without LIMIT | 10-30s under live load | explicit LIMITs; materialise; cache ~60s |
| `GET /api/home` | falls back to the ENTIRE operator snapshot if sections missing | 10-100MB JSON, timeout | incremental section loading; never fall back to full payload |

## Design principles for the new/refit UI

- **One job per page.** A page answers one question (what should I act on? what do I hold? what did
  the AI decide?). Cross-links instead of cramming.
- **Lean lists, detail on demand.** List endpoints return a small projection (no large JSON blobs);
  the heavy evidence/trace is a separate per-row detail call. (The new decisions endpoint follows this.)
- **Server-side bounded + paginated.** Every list endpoint enforces a max limit and returns
  `{total, returned, offset, limit, next_offset}`. No client-controlled unbounded queries.
- **Batch, don't N+1.** Enrich a page of rows with one query per data source, keyed by symbol -- never
  one query per row.
- **Summarise on the server.** Counts/aggregates come from a `GROUP BY`, not by shipping all rows to
  the client.
- **A performance budget.** Target: every list view < ~2s, every detail view < ~1s, no endpoint scans
  an unbounded large table. Track regressions with `scripts/api_performance_report.py`.

## The new "Daily Decisions" view (stitches in the decision runner)

The daily decision runner (`advisory/llm_decision_runner.py`) writes review-only decisions to
`advisory_llm_decisions`. The UI home for them, built to the principles above as the TEMPLATE the rest
of the UI should follow:

- **Backend (built):** `GET /api/llm-decisions` (`build_llm_decisions_payload`) -- bounded page,
  indexed `decided_at DESC`, a LEAN projection (symbol / action / decision_mode / conviction /
  grounded / sufficiency_path / llm_status -- NO evidence/contract/sizing blobs), plus a cheap
  `GROUP BY` summary. Fast on the small review-only table. Every row is `broker_execution_allowed=False`.
- **Frontend (to build):** a `llm-decisions.vue` page -- a paginated table (date / symbol / action /
  mode / conviction / grounded?), filterable by symbol and date, with a row click opening a detail
  panel that fetches the full evidence/reasons on demand. As outcomes mature, add the monitor's verdict
  (realised benchmark-excess, systematic-error flags) as columns.

This page doubles as the proof-of-pattern: lean list + detail-on-demand + bounded + summarised.

## Roadmap (phased -- highest value first)

**Phase 1 -- the fast wins + the decisions view (the template):**
1. Ship `/api/llm-decisions` (done) + the `llm-decisions.vue` page (lean/paginated/detail-on-demand).
2. Batch `build_actions_payload` enrichment (the #1 load risk) -> ~10x faster.
3. Enforce server-side max limits on the trace endpoints; cache `/api/health` for ~60s.

**Phase 2 -- consolidate the core workflow:**
4. One **Actions** page (toggles for top/regular/alerts) as the single source of truth, with the
   "blocked by / decided by" reason on each row (folds in event-policy + conflict context).
5. One **Portfolio** page defaulting to current open positions, with a bucket grouping option.
6. Fold the execution-approval status into the Actions/Portfolio flow (one schema, one place).

**Phase 3 -- diagnostics:**
7. Materialise the operator-journey stage summaries.
8. A single "what's blocked on data freshness?" aggregate in `/health`.

## How to measure

```sh
python scripts/api_performance_report.py --limit 20     # rank slow endpoints
```
Use this before/after each Phase-1 change; nothing should regress past the budget above.
