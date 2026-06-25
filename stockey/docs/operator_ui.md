# Operator UI -- one symbol, one place, one why

This is the implemented operator console after the "UI redesign" arc (see
`docs/ui_simplification_plan.md` for the review that motivated it, and
`.claude/plans/lets-plan-this-development-async-aurora.md` for the work-item plan). It replaces an
18-page flat navigation where one symbol's state was spread across ~6 pages and a single list endpoint
shipped 287 KB for 25 rows. Everything here is **review-only**: no view submits a broker order
(`broker_execution_allowed=false` throughout).

## The core idea: one state per symbol

A server-side resolver (`resolve_symbol_states` in `advisory/api/app.py`) collapses three sources --
the deterministic action queue (`advisory_action_recommendations`), the LLM decisions
(`advisory_llm_decisions`), and operator-tracked holdings (`advisory_operator_holdings`) -- into
exactly **one** state per symbol, so a symbol never appears in two lists:

| State | Meaning |
|---|---|
| `RECOMMENDATION` | a fresh actionable signal the operator has not actioned (deterministic action + LLM decision merged into one row, with agree/conflict) |
| `HOLDING` | operator marked it "taken" -> a tracked position (entry price + date) |
| `EXITED` | a holding the operator closed |
| `NONE` | no current signal |

Precedence is `HOLDING > RECOMMENDATION > EXITED > NONE`. The manual transitions are **Take**
(RECOMMENDATION -> HOLDING), **Dismiss** (clears it from the queue, client-side), and **Exit**
(HOLDING -> EXITED). Taking a recommendation moves it out of the queue and into Positions in one place
-- the dedup invariant.

Holdings are **tracked monitoring, not paper trading**: entry price/date are recorded and the move
since entry is shown for context, but there is **no simulated P&L** (paper trading was dropped, operator
decision 2026-06-23). The realized-outcome labeler can mature a holding's benchmark-excess the same way
it does an LLM decision.

## Navigation (6 primary + a Research menu)

1. **Workbench** (`/workbench`) -- "what needs me now": top recommendations + tracked holdings + health
   tiles, one screen.
2. **Recommendations** (`/recommendations-unified`) -- the full unified per-symbol queue; row actions
   Take / Dismiss; click a symbol for the "why".
3. **Positions** (`/positions`) -- tracked holdings (entry / current / since-entry move), open|exited
   filter, Exit action.
4. **Decisions** (`/llm-decisions`) -- the LLM decision journal.
5. **Health** (`/health-hub`) -- one diagnostics hub.
6. **Research** (dropdown) -- low-frequency tools: Playbooks, Screeners, Signal Quality, Regime, Prompt
   Registry, Technical Calibration, Conflict Rules, Identity Issues, Operations.

Root (`/`) redirects to the Workbench. The retired pages (old Overview/Recommendations, Event Inbox,
Manual Review, Operator Journey, Execution Approvals, Decision Trace, Wait Signals, Paper Portfolio,
old Data Health) were removed -- their routes now 404. The per-symbol detail page (`/symbols/{symbol}`)
remains, reachable from symbol links.

The **"why"** is a drill-in panel (`components/DecisionWhy.vue`), reachable by clicking any symbol
anywhere. It is the *only* place the heavy evidence tree loads.

## Endpoints

| Route | Payload builder | Shape |
|---|---|---|
| `GET /api/recommendations-unified` | `build_recommendations_unified_payload` | lean per-symbol RECOMMENDATION rows + GROUP-BY summary (by_action, agree, conflict); paginated |
| `GET /api/positions` | `build_positions_payload` | lean holdings (default open) + latest price + since-entry change |
| `POST /api/positions/take` | `build_position_take_payload` | mark taken -> `advisory_operator_holdings` |
| `POST /api/positions/exit` | `build_position_exit_payload` | close an open holding |
| `GET /api/symbol/{symbol}/why` | `build_symbol_why_payload` | the heavy detail: evidence verdicts (`load_evidence_packet`) + grounding (`validate_decision_grounding`) + deterministic reason + LLM decision. On-demand only |
| `GET /api/workbench` | `build_workbench_payload` | one bounded call: top recommendations + holdings + health tiles |
| `GET /api/health-hub` | `build_health_hub_payload` | data freshness/sync + cron + API errors + LLM systematic-error monitor; each source bounded, degraded sources under `skipped`; 15s cached |

### Performance contract (every list endpoint)

- Lean lists: small projection, **no** evidence/contract JSON blobs; the heavy "why" is on demand.
- Server-bounded + paginated: `{total, returned, offset, limit, next_offset}` via `_bounded_limit`.
- Batch, never N+1: one query per source keyed by symbol.
- Summaries via `GROUP BY` on the server.
- Targets: list < 2 s, detail < 1 s. The frontend timeout ceiling is `NUXT_PUBLIC_API_TIMEOUT_MS`
  (default 25000) -- a safety net, not the goal. Track with `scripts/api_performance_report.py`.

Measured live: recommendations ~27 ms, positions ~21 ms, workbench ~2.2 s (cold), health-hub 1.1 s
cold / 4.5 ms warm, why ~1.9 s (the single heavy call).

## Data model

`advisory_operator_holdings` (append-only migration `20260625_advisory_operator_holdings_base`, via
`advisory/operator_holdings.py`): `symbol, action, entry_price, entry_date, source, status (open|
exited), exit_price, exit_date, note, created_at, updated_at`, `UNIQUE(symbol, entry_date)`. Write
surface: `record_take` / `record_exit` / `load_holdings`. Review-only fields only -- no quantity, no
broker fields, no P&L.

## Guardrails

- Additive rollout: new pages shipped alongside the old ones; legacy pages remain until proven
  redundant in real use (the old `/api/actions` heavy shape is intentionally still intact).
- Append-only migrations only.
- Everything stays review-only; holdings are operator-marked tracking, not orders.
