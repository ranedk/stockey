# Analysis Agent Board

Date: 2026-06-07

Purpose: short-lived coordination board for the `analysis.md` development loop. This is not a backlog replacement. `analysis.md` remains the source of truth; this board tracks the current multi-agent cycle and handoffs.

## Relay Model

- Coordinator agent reads `analysis.md` and proposes the dependency-ordered queue.
- Builder agent gets one bounded slice at a time.
- Reviewer agent audits the builder slice.
- Main integrator applies/fixes changes in the real workspace, runs validation, updates `analysis.md`, and starts the next cycle.

Agents do not rely on direct peer-to-peer chat. The main integrator relays relevant outputs between them.

## Manual Runner

Run one bounded development cycle:

```sh
ANALYSIS_AGENT_MAX_CYCLES=1 ./all_analysis_codex.sh
```

Run multiple cycles only when you are watching:

```sh
ANALYSIS_AGENT_MAX_CYCLES=3 ./all_analysis_codex.sh
```

Logs and prompts are written to `logs/analysis_agents/`. After every cycle, review this board, `analysis.md`, `git diff`, and the validation output before accepting the changes.

Supervisor guardrails:

- The wrapper enforces a default hard cap of `3` cycles unless `--allow-large-runs` is passed.
- Every completed cycle is followed by local deterministic checks: diff hygiene, Python compile, full backend regression for backend/Python changes, and Nuxt typecheck/tests for frontend changes.
- Root generated NSE CSV artifacts are denied before and after cycles unless ignored/moved.
- The Codex worker must return an explicit `CYCLE_STATUS` marker.
- Planner/Builder/Reviewer/Integrator phases are required in the worker output; real subagents are optional sidecars, not a substitute for local gates.

## Active Cycle

Status: `integrated`

Previous slice: `Rebalance SELL over risk-off market-gated portfolio BUY_MORE plus WATCH reason-contract source-precedence coverage`
Current slice: `Adversarial-veto Manual Review over risk-off market-gated portfolio BUY_MORE plus WATCH reason-contract source-precedence coverage`

Acceptance criteria:

- A focused backend regression proves adversarial-veto event-policy `MANUAL_REVIEW` wins over a same-symbol risk-off market-gated portfolio `BUY_MORE` and watchlist `WATCH`.
- The adversarial-veto winner remains review-only with no transaction type, explicit event veto evidence, and no broker execution authority.
- Source-precedence evidence lists both losing candidates and preserves the losing portfolio add-on's market-gate context, including original `BUY_MORE`.
- No broker execution behavior changes.
- No cleanup command is executed and no DB rows are marked superseded.

## Queue

1. Typed wait-signal conditions and matcher contracts.
2. Wait-signal match escalation to original manual item/action candidate. Integrated through Manual Review follow-up, original-item suppression, and durable review-only action candidates.
3. Complete manual-review decision tests for every dropdown option. Integrated.
4. Full action/reason contract tests. Wait-signal, same-symbol source-precedence, duplicate BUY screener, MANUAL_REVIEW-over-WATCH, portfolio BUY-over-WATCH, lifecycle HOLD-over-WATCH, market-gated portfolio BUY and BUY_MORE MANUAL_REVIEW-over-WATCH, cautious-market portfolio BUY_MORE-over-WATCH sizing reduction, event-policy Manual Review over market-adjusted portfolio BUY_MORE plus WATCH, adversarial-veto Manual Review over market-gated portfolio BUY_MORE plus WATCH, rebalance SELL over market-gated portfolio BUY_MORE plus WATCH, portfolio BUY_MORE-over-portfolio BUY, portfolio BUY_MORE-over-WATCH, portfolio SELL-over-portfolio BUY, SELL-over-WATCH, generic MANUAL_REVIEW-over-portfolio BUY, PARTIAL_SELL-over-portfolio BUY, PARTIAL_SELL-over-WATCH, TIGHTEN_STOP-over-portfolio BUY, TIGHTEN_STOP-over-WATCH, BUY/SELL/MANUAL_REVIEW/WATCH collision, event-policy/playbook/lifecycle matrix, lifecycle exit over review overlays, event review over lifecycle hold, and equal-priority freshness tie-break contract sub-slices integrated; broader action-source permutations remain open.
5. Superseded-error cleanup. Durable dry-run/apply marking helper and scheduled/audited dry-run visibility are integrated; operator bulk close/apply workflow remains.
6. “Why not approved” explanations in Action Queue. Integrated in cycle 2.
7. Current blockers Health card. Integrated in cycle 3.
8. Durable skipped-symbol/identity issue queue and UI. Integrated in cycle 4.
9. Portfolio lifecycle state tests. Integrated in cycle 5.
10. Execution dry-run approval and reconciliation gate. Integrated in cycle 6.
11. Manual-review wait-signal closure and escalation coverage. Integrated in cycle 16.
12. Conflict-rule ranking regression. Integrated in cycle 17.
13. Conflict-rule enable/disable UI. Integrated in cycle 18.
14. Stale API/code version indicator. Integrated in cycle 19.
15. Conflict-rule explanation edit workflow. Integrated in cycle 20.
16. Conflict-rule manual-resolution promotion workflow. Integrated in cycle 21.
17. Health degradation lifecycle grouping. Integrated in cycle 22.
18. Manual Review item-impact and decision-boundary visibility. Integrated in cycle 23.
19. Full mocked operator state-machine journey coverage. Integrated in cycle 24.
20. Promoted conflict-rule ranking support. Integrated in cycle 25.
21. Execution safety contract visibility in Action Queue. Integrated in cycle 26.
22. Mocked broker account/reconciliation coverage. Integrated in cycle 27.
23. Persisted reconciliation workflow coverage. Integrated in cycle 28.
24. Conflict-rule exact-match condition editing. Integrated in cycle 29.
25. Operator API schema metadata for critical endpoints. Integrated in cycle 30.
26. FastAPI/Pydantic typed response models for critical Operator API endpoints. Integrated in cycle 31.
27. Route-level response smoke tests for critical Operator API endpoints. Integrated in cycle 2/300.
28. Symbol-trace route response/error-path smoke coverage. Integrated in cycle 3/300.
29. Event detail/trace route response/error-path smoke coverage. Integrated in cycle 4/300.
30. Adversarial-review veto precedence regression. Integrated in cycle 5/300.
31. Adversarial-review reason in Action Queue compact rows/UI. Integrated in cycle 6/300.
32. Market-gate reason in Action Queue compact rows/UI. Integrated in cycle 7/300.
33. Manual decision and wait-signal trace links. Integrated in cycle 8/300.
34. Human-readable compact action reasons. Integrated in cycle 9/300.
35. Stale snapshot warnings across operator API/pages. Integrated in cycle 10/300.
36. Operator-visible watcher output effects. Integrated in cycle 11/300.
37. Document advisory/watch/signal/manual-review boundaries. Integrated in cycle 12/300.
38. Document Manual Review operator decision effects. Integrated in cycle 13/300.
39. Health superseded cleanup preview visibility. Integrated in cycle 14/300.
40. Superseded cleanup audit scheduling and Operations dry-run. Integrated in cycle 15/300.
41. Compact Action Queue raw broker execution-safety contract coverage. Integrated in cycle 16/300.
42. Incomplete reason-contract Manual Review boundary evidence. Integrated in cycle 17/300.
43. Watcher skipped-cycle visibility. Integrated in cycle 18/300.
44. External watcher lock-skipped visibility. Integrated in cycle 19/300.
45. BUY/SELL/manual/watch reason-contract collision. Integrated in cycle 20/300.
46. News/announcement watcher catch-up bounds. Integrated in cycle 21/300.
47. Watcher signal-refresh versus full-advisory trigger boundaries. Integrated in cycle 22/300.
48. Symbol detail stale snapshot warning visibility. Integrated in cycle 23/300.
49. Standalone identity issues read-only page. Integrated in cycle 24/300.
50. Source-specific freshness warnings on non-snapshot operator pages. Integrated in cycle 25/300.
51. Global live-trading disabled visibility. Integrated in cycle 26/300.
52. Operator safety component smoke coverage. Integrated in cycle 27/300.
53. Page-level operator safety smoke coverage. Integrated in cycle 28/300.
54. Symbol/Health page safety smoke coverage. Integrated in cycle 29/300.
55. Promoted exact-match reason-contract attribution. Integrated in cycle 30/300.
56. Lifecycle rebalance Manual Review reason-contract boundary evidence. Integrated in cycle 31/300.
57. Event-policy/playbook Manual Review reason-contract boundary evidence. Integrated in cycle 32/300.
58. Lifecycle/rebalance Manual Review reason-contract boundary permutations. Integrated in cycle 33/300.
59. Market-gated portfolio Manual Review reason-contract boundary. Integrated in cycle 34/300.
60. Portfolio BUY-over-WATCH reason-contract source-precedence coverage. Integrated in cycle 35/300.
61. Generic Manual Review-over-portfolio BUY reason-contract boundary coverage. Integrated in cycle 36/300.
62. PARTIAL_SELL-over-portfolio BUY reason-contract source-precedence coverage. Integrated in cycle 37/300.
63. BUY_MORE-over-portfolio BUY reason-contract source-precedence coverage. Integrated in cycle 38/300.
64. TIGHTEN_STOP-over-portfolio BUY reason-contract source-precedence coverage. Integrated in cycle 39/300.
65. Portfolio SELL-over-portfolio BUY reason-contract source-precedence coverage. Integrated in cycle 40/300.
66. SELL-over-WATCH reason-contract source-precedence coverage. Integrated in cycle 41/300.
67. PARTIAL_SELL-over-WATCH reason-contract source-precedence coverage. Integrated in cycle 42/300.
68. TIGHTEN_STOP-over-WATCH reason-contract source-precedence coverage. Integrated in cycle 43/300.
69. BUY_MORE-over-WATCH reason-contract source-precedence coverage. Integrated in cycle 44/300.
70. Read-only Operations API typed response and smoke coverage. Integrated in cycle 45/300.
71. Read-only technical calibration API typed response and smoke coverage. Integrated in cycle 46/300.
72. Read-only event-model research API typed response and smoke coverage. Integrated in cycle 47/300.
73. Read-only hypotheses API typed response and smoke coverage. Integrated in cycle 48/300.
74. Read-only event-policy/evaluation API typed response and smoke coverage. Integrated in cycle 49/300.
75. Read-only home/portfolio/events API typed response and smoke coverage. Integrated in cycle 50/300.
76. Read-only summary/watchlist/market-context API typed response and smoke coverage. Integrated in cycle 51/300.
77. Read-only signal-refresh API typed response and smoke coverage. Integrated in cycle 52/300.
78. Read-only data-health API typed response and smoke coverage. Integrated in cycle 53/300.
79. Read-only action/portfolio detail API typed response and smoke coverage. Integrated in cycle 54/300.
80. Read-only runtime API typed response and smoke coverage. Integrated in cycle 55/300.
81. Read-only legacy health API typed response and smoke coverage. Integrated in cycle 56/300.
82. Read-only trace/detail API schema metadata route coverage. Integrated in cycle 57/300.
83. Frontend trace/detail API schema metadata type alignment. Integrated in cycle 58/300.
84. Frontend read-only operations/research API schema metadata type alignment. Integrated in cycle 59/300.
85. Frontend action/portfolio detail API schema metadata type alignment. Integrated in cycle 60/300.
86. Frontend wait-signal match API schema metadata type alignment. Integrated in cycle 61/300.
87. Frontend action-conflict rule API schema metadata type alignment. Integrated in cycle 62/300.
88. Lifecycle HOLD-over-WATCH reason-contract source-precedence coverage. Integrated in cycle 63/300.
89. Analysis-agent supervisor hardening. Integrated manually after reviewing multi-cycle drift: hard cycle cap, required status marker, post-cycle local gates, artifact denylist, and documented subagent/reviewer expectations.
90. Market-gated portfolio Manual Review-over-WATCH reason-contract source-precedence coverage. Integrated in cycle 1/3.
91. Market-gated portfolio BUY_MORE Manual Review-over-WATCH reason-contract source-precedence coverage. Integrated in cycle 2/3.
92. Cautious-market portfolio BUY_MORE-over-WATCH sizing-reduction reason-contract source-precedence coverage. Integrated in cycle 3/3.
93. Event-policy Manual Review over market-adjusted portfolio BUY_MORE plus WATCH reason-contract source-precedence coverage. Integrated in cycle 1/3.
94. Rebalance SELL over risk-off market-gated portfolio BUY_MORE plus WATCH reason-contract source-precedence coverage. Integrated in cycle 2/3.
95. Adversarial-veto Manual Review over risk-off market-gated portfolio BUY_MORE plus WATCH reason-contract source-precedence coverage. Integrated in cycle 3/3.

## Latest Agent Notes

- Coordinator: selected typed wait-signal contracts as first dependency.
- Builder: implemented typed wait-signal contracts and matcher routing.
- Reviewer: found symbol-scoped market-wide matching, generic default-keyword matching, global match lookup, source-table drift, and price-summary readability issues.
- Integrator: fixed reviewer findings in the main workspace.
- Integrator: validated typed conditions and conservative signal-refresh escalation in the main workspace and updated `analysis.md`.
- Integrator: added API-level coverage for every Manual Review dropdown decision.
- Builder/Integrator: matched manual-review wait signals now carry explicit original-review provenance in evidence, Wait Signals API/UI rows, and Manual Review follow-up items.
- Integrator: suppressed recovered/superseded event-processing and announcement-document failures from active Manual Review/Health without mutating data.
- Integrator: compact Action Queue rows now preserve manual-revision explanation fields, and the UI shows deterministic “why not approved/manual review” checks alongside reason contracts.
- Integrator: added a read-only current-blockers summary to operator health and rendered it on the Data Health page.
- Integrator: added durable `advisory_identity_issues` recording for missing Dhan stock security IDs and surfaced open identity issues through Manual Review.
- Integrator: added lifecycle context fields for review-only states and regression coverage for no-backdated paper entries.
- Integrator: added backend execution safety contracts for order previews, approval/reconciliation live gates, and stale/manual-closed action-row blocking.
- Integrator: added `advisory/superseded_failures.py` with dry-run discovery and explicit apply-mode durable markers for superseded event-processing failures and recovered announcement document errors.
- Integrator: added Manual Review runtime state handling so matched manual-review wait signals reopen as follow-up items and suppress stale original waiting items.
- Integrator: added durable review-only action candidates for matched Manual Review wait signals, preserving original manual-review item ids and match evidence in action context.
- Integrator: added explicit `wait_signal` reason-contract evidence for matched Manual Review wait-signal action candidates and focused regression coverage that the candidate remains review-only with a complete contract.
- Integrator: added same-symbol source-precedence evidence to recommendation reason contracts, including winning source, losing candidates, and deterministic precedence rationale.
- Integrator: added focused reason-contract regression coverage for duplicate BUY screener collapse and MANUAL_REVIEW-over-WATCH same-symbol precedence.
- Integrator: accepted parsed playbook `checks` as reason-contract review evidence and added matrix coverage for playbook winning over event-policy and lifecycle same-symbol candidates.
- Integrator: compact Action Queue payloads now retain conflict-resolution evidence, and Reason Contract UI renders source-precedence summaries plus losing candidates.
- Integrator: added remaining event-policy/playbook/lifecycle reason-contract permutation coverage for lifecycle exits beating review overlays, event reviews beating lifecycle holds, and equal-priority freshness tie-breaks.
- Integrator: suppressed matched Manual Review wait-signal follow-up rows when the original operator item has since been closed, while preserving reopened follow-up behavior for active `watch_for_event` decisions.
- Integrator: added focused regressions for closed-original suppression and review-only Manual Review wait-signal signal-refresh escalation.
- Integrator: added focused regression coverage proving enabled conflict rules can override misleading raw candidate priority during ranking, and that disabled rules fall back to ordinary ranking.
- Integrator: added a narrow operator API and Conflict Rules page controls to enable or disable existing deterministic conflict rules without rewriting historical action rows or changing broker execution.
- Integrator: added `/api/runtime` git/process/source metadata plus a global operator header warning when the running API process is older than source files on disk.
- Integrator: added a narrow conflict-rule explanation edit workflow, with API validation, UI save/reset controls, and bootstrap preservation for operator-edited rule text.
- Integrator: added a disabled-by-default manual conflict promotion workflow; unresolved conflicts can become exact-match rule candidates, and enabled promoted rules are consumed by the conflict resolver.
- Integrator: added read-only Health lifecycle grouping for active, recovered, and superseded-ready degradation rows, backed by the existing superseded-failure dry-run preview and rendered in the Health UI.
- Integrator: added Manual Review item-impact badges/panels and selected-decision boundary panels so operators can distinguish closing, annotating, wait-signal, investment, operational, research, and execution-blocking states before save.
- Integrator: added a mocked end-to-end operator state-machine regression covering Manual Review `watch_for_event` decision recording, typed wait-signal creation, event matching, reopened Manual Review follow-up payload, and review-only action-candidate linkage.
- Integrator: added candidate-ranking support for enabled promoted exact-match conflict rules, including same-symbol peer matching, rule id/reason attribution in raw context, and regression coverage for nonmatching source fallback.
- Integrator: surfaced dry-run execution safety contracts through operator snapshot/API compact action rows and rendered a read-only approval/reconciliation gate in Action Queue without adding approval or broker submission behavior.
- Integrator: added mocked broker account/reconciliation coverage for nested cash payload parsing, holdings/positions aggregation, broker order status mapping, and fill extraction; nested `data` dict payloads now flatten safely.
- Integrator: added persisted reconciliation safety-contract annotation, so mocked broker order reconciliation writes `broker_reconciliation_status=reconciled` into both `safety_checks_json` and raw broker JSON while preserving live-submission disabled state.
- Integrator: added validated `action_pair_exact` condition editing for promoted/manual-resolution conflict rules in the operator API/UI, with unsupported condition shapes rejected and no historical action-row rewrites.
- Integrator: added `api_schema` response metadata for critical Operator API payloads and frontend payload types, with focused regression coverage that broker execution remains explicitly disabled in the contract.
- Integrator: registered permissive FastAPI/Pydantic response models for critical Operator API routes and added OpenAPI regression coverage for actions, health details, manual review queue/decision, wait signals/match, and conflict-rule read/write responses.
- Integrator: added route-level FastAPI smoke coverage for critical Operator API endpoints with mocked builders and added `api_schema` broker-disabled metadata to `/api/wait-signals/match`.
- Integrator: added permissive response models for symbol trace and symbol trace summary routes plus TestClient smoke coverage for success, validation, and guarded 400/500 error paths.
- Integrator: added permissive response models for event detail, event trace, and event trace summary routes plus TestClient smoke coverage for success and guarded 400/500 error paths.
- Integrator: added explicit adversarial-veto conflict precedence for event-policy manual-review rows, carried veto/review-reason evidence into reason contracts, and pinned veto over BUY/WATCH ranking with focused regression coverage.
- Integrator: compact Action Queue rows now preserve adversarial-review event evidence, and the Reason Contract UI renders veto status, review action/status, and review reason without requiring trace/detail views.
- Integrator: compact Action Queue rows now preserve market-gate evidence for positive actions downgraded to Manual Review, and the Reason Contract UI renders a dedicated market-gate block with original action, gate reason, regime, breadth, and risk-off score.
- Integrator: symbol/event traces now include read-only manual-review decision -> wait-signal -> match links, and Trace Timeline renders the relation with manual item, signal id, match evidence, and source keys.
- Integrator: compact Action Queue/API display payloads now humanize internal reason/status codes and missing-field labels before UI display, while preserving machine action codes and execution safety contracts.
- Integrator: snapshot-backed Operator API payloads now include nullable `snapshot_warning` metadata for stale cached operator snapshots, and Dashboard, Events, and Health render a shared snapshot warning banner.
- Integrator: signal-refresh rows now persist operator-visible watcher effect labels for `action_changed`, `wait_match_created`, and `evidence_only`; compact API rows retain the signal/effect fields, and the home page renders the effect summary.
- Integrator: documented the operator boundary between full advisory, watchers, fast signal refresh, wait signals, and Manual Review in `docs/operators_manual.md`, including reads, writes, allowed changes, and explicit non-authority over broker execution.
- Integrator: documented every Manual Review dropdown decision in `docs/operators_manual.md`, including runtime state, active-queue effect, side effects, matched wait-signal reopen/suppression behavior, and explicit non-authority over portfolio/action/config/broker mutation.
- Integrator: added read-only Health superseded cleanup preview samples and dry-run/apply command visibility, plus operator docs that apply mode requires explicit operator intent and does not affect broker/portfolio/action/config state.
- Integrator: added `all_superseded_cleanup_audit.sh`, scheduled it in the cron template/generated crontab as preview-only, and exposed the same dry-run through the audited Operations command registry without adding apply behavior.
- Integrator: skipped the superseded cleanup operator apply workflow for cycle 16 because it would add a DB cleanup write path without explicit operator approval for that class of operation.
- Integrator: added focused compact Action Queue regression coverage proving execution-safety contracts remain visible when persisted only in `raw_broker_json`; broker execution behavior and cleanup behavior remain unchanged.
- Integrator: skipped the superseded cleanup operator bulk-close/apply workflow for cycle 17 because this run explicitly disallows destructive DB cleanup and unclear cleanup write paths.
- Integrator: added explicit Manual Review boundary evidence for incomplete reason-contract downgrades, including original blocked action, missing fields, operator question, review-only effect, and `broker_execution_allowed=false`, with focused regression coverage.
- Integrator: added per-source watcher skipped-cycle events for not-due OHLCV, announcement, and news cycles, with focused regression coverage proving skipped cycles are operator-visible without changing cursor/success state.
- Integrator: added shell-level watcher lock-skip publishing for OHLCV, announcements, news, router, wait signals, operator snapshot, and trace summary store, without writing sync-state or running downstream steps.
- Integrator: added focused reason-contract regression coverage for a same-symbol BUY, SELL, MANUAL_REVIEW, and WATCH collision, proving the SELL winner keeps lifecycle/conflict-rule evidence and lists all losing action sources.
- Integrator: added bounded event-cursor catch-up handling for news and announcement watcher cycles, including persisted requested-from/requested-to metadata, `catchup_truncated` flags, and regression coverage for stale cursors.
- Integrator: documented watcher trigger boundaries for intraday OHLCV, news/announcements, context observations, material context triggers, wait-signal matches, skipped/failed/truncated watcher cycles, and the post-close full advisory path.
- Integrator: symbol detail now reuses the shared SnapshotWarning banner with snapshot metadata from actions, portfolio, or events payloads, so stale cached operator snapshots are visible while inspecting a single symbol.
- Integrator: added read-only `/api/identity-issues` payload and a first-class Identity Issues operator page for open Dhan skipped-symbol/security mapping failures, including repair hints, attempted exchanges/fallbacks, Manual Review linkage, and explicit no-broker/no-mutation boundaries.
- Integrator: added read-only source freshness warnings to Manual Review, Wait Signals, and Identity Issues payloads/UI, covering stale visible rows, missing timestamps, and skipped source queries without changing broker, cleanup, identity, or action state.
- Integrator: `/api/runtime` now exposes the global `STOCKEY_LIVE_TRADING_ENABLED` state, defaulting to disabled, and the operator header renders that read-only live-trading safety status without adding approval or broker-submission controls.
- Integrator: added Vitest component mount coverage for shared operator-safety UI components: stale snapshot warnings, source freshness warnings, Reason Contract safety evidence, and Trace Timeline manual-review wait links.
- Integrator: added Vitest page-level mount coverage for Manual Review and Action Queue safety flows, using mocked read-only API payloads to assert source warnings, decision boundaries, matched wait follow-up context, execution approval/reconciliation gates, market-gate evidence, conflict evidence, and manual-review explanations remain visible.
- Integrator: added Vitest page-level mount coverage for Health and Symbol Detail safety flows, using mocked read-only API payloads to assert stale snapshot warnings, current blockers, superseded cleanup preview boundaries, degradation rows, participation read context, market-gate reason-contract evidence, and symbol event context remain visible.
- Integrator: added focused backend regression coverage proving promoted exact-match conflict rules that affect ranking preserve rule id/reason/score in the reason contract along with the losing same-symbol portfolio BUY candidate.
- Integrator: lifecycle/rebalance Manual Review action-consolidation rows now infer explicit review-only/no-broker Manual Review reason-contract evidence, with focused regression coverage for `review_target`.
- Integrator: event-policy and playbook Manual Review action-consolidation rows now infer source-specific review-only/no-broker Manual Review reason-contract boundaries, with focused regression coverage for event and playbook review sources.
- Integrator: lifecycle/rebalance Manual Review reason-contract boundaries now distinguish `review_manual`, `review_stale`, `review_horizon`, and `review_target`, while preserving review-only/no-broker evidence and lifecycle context.
- Integrator: market-gated portfolio BUY downgrades now carry explicit `market_context_positive_action_review_required` Manual Review reason-contract evidence, including blocked original action and `broker_execution_allowed=false`.
- Integrator: added focused backend regression coverage proving a market-gated portfolio BUY downgraded to Manual Review wins over a same-symbol watchlist WATCH while preserving review-only/no-broker boundary evidence and the losing WATCH candidate in source-precedence evidence.
- Integrator: added focused backend regression coverage proving a complete broker-capable portfolio BUY that wins over a same-symbol watchlist WATCH preserves screener/technical/risk evidence, remains broker-capable, and records the losing WATCH candidate in source-precedence reason-contract evidence.
- Integrator: added focused backend regression coverage proving a generic review-only Manual Review candidate that wins over a same-symbol broker-capable portfolio BUY preserves explicit no-broker Manual Review boundary evidence and records the losing BUY candidate in source-precedence evidence.
- Integrator: added focused backend regression coverage proving a rebalance PARTIAL_SELL trim that wins over a same-symbol broker-capable portfolio BUY remains a broker-capable sell-side risk action, preserves lifecycle/risk evidence, and records exit-precedence plus the losing BUY candidate in source-precedence reason-contract evidence.
- Integrator: added focused backend regression coverage proving a broker-capable portfolio BUY_MORE add-on that wins over a same-symbol broker-capable portfolio BUY remains a buy-side add-on action, preserves screener/technical/lifecycle/risk evidence, and records the losing BUY candidate in source-precedence reason-contract evidence.
- Integrator: added focused backend regression coverage proving a rebalance TIGHTEN_STOP that wins over a same-symbol broker-capable portfolio BUY remains review-only with no transaction type, preserves lifecycle/risk evidence, and records exit-precedence plus the losing BUY candidate in source-precedence reason-contract evidence.
- Integrator: added focused backend regression coverage proving a portfolio SELL that wins over a same-symbol portfolio BUY remains broker-capable with sell-side transaction type, preserves technical/lifecycle/risk evidence, and records exit-precedence plus the losing BUY candidate in source-precedence reason-contract evidence.
- Integrator: added focused backend regression coverage proving a rebalance SELL that wins over a same-symbol watchlist WATCH remains broker-capable with sell-side transaction type, preserves technical/lifecycle/risk evidence, and records exit-precedence plus the losing WATCH candidate in source-precedence reason-contract evidence.
- Integrator: added focused backend regression coverage proving a rebalance PARTIAL_SELL that wins over a same-symbol watchlist WATCH remains broker-capable with sell-side transaction type, preserves technical/lifecycle/risk evidence, and records exit-precedence plus the losing WATCH candidate in source-precedence reason-contract evidence.
- Integrator: added focused backend regression coverage proving a rebalance TIGHTEN_STOP that wins over a same-symbol watchlist WATCH remains review-only with no transaction type, preserves technical/lifecycle/risk evidence, and records exit-precedence plus the losing WATCH candidate in source-precedence reason-contract evidence.
- Integrator: added focused backend regression coverage proving a portfolio BUY_MORE add-on that wins over a same-symbol watchlist WATCH remains broker-capable with buy-side transaction type, preserves screener/technical/lifecycle/risk evidence, and records the losing WATCH candidate in source-precedence reason-contract evidence.
- Integrator: added permissive FastAPI/Pydantic response models and `api_schema` broker-disabled metadata for read-only Operations routes covering smoke checks, cron logs, command registry, and API errors, plus mocked TestClient smoke coverage that does not run commands.
- Integrator: added permissive FastAPI/Pydantic response models and `api_schema` broker-disabled metadata for read-only technical calibration GET routes covering latest calibration summary/configs and promotion-review listing, plus mocked TestClient smoke coverage that does not generate or decide promotion reviews.
- Integrator: added permissive FastAPI/Pydantic response models and `api_schema` broker-disabled metadata for read-only event-model research GET routes covering promotion-check and artifact manifest payloads, plus mocked TestClient smoke coverage that does not promote models, write S3 artifacts, run commands, or touch broker paths.
- Integrator: added a permissive FastAPI/Pydantic response model and `api_schema` broker-disabled metadata for the read-only hypotheses GET route, plus mocked TestClient smoke coverage that does not create/update hypotheses, run scans, write promotion audits, run commands, or touch broker paths.
- Integrator: added permissive FastAPI/Pydantic response models and `api_schema` broker-disabled metadata for read-only event-policy/evaluation GET routes, plus mocked TestClient smoke coverage that does not run evaluators, execute commands, mutate policy rows, or touch broker paths.
- Integrator: added permissive FastAPI/Pydantic response models and `api_schema` broker-disabled metadata for read-only home, portfolio, and events GET routes, plus mocked TestClient smoke coverage that does not run commands, mutate cleanup state, or touch broker paths.
- Integrator: added permissive FastAPI/Pydantic response models and `api_schema` broker-disabled metadata for read-only summary, watchlist, and market-context GET routes, plus mocked TestClient smoke coverage that does not run commands, mutate cleanup state, or touch broker paths.
- Integrator: added a permissive FastAPI/Pydantic response model and `api_schema` broker-disabled metadata for the read-only signal-refresh GET route, plus mocked TestClient smoke coverage that does not run refreshes, commands, cleanup state, or broker paths.
- Integrator: added a permissive FastAPI/Pydantic response model and `api_schema` broker-disabled metadata for the read-only data-health GET route, plus mocked TestClient smoke coverage that does not run commands, cleanup state, or broker paths.
- Integrator: added a shared permissive FastAPI/Pydantic detail response model and `api_schema` broker-disabled metadata for read-only action detail and portfolio detail GET routes, plus mocked TestClient smoke coverage that does not run commands, cleanup state, or broker paths.
- Integrator: added a permissive FastAPI/Pydantic response model and `api_schema` broker-disabled metadata for the read-only runtime GET route, plus mocked TestClient smoke coverage that does not run commands, credentials, cleanup state, DB writes, or broker paths.
- Integrator: added a permissive FastAPI/Pydantic response model and `api_schema` broker-disabled metadata for the read-only legacy health GET route, plus mocked TestClient smoke coverage that does not run commands, credentials, cleanup state, DB writes, or broker paths.
- Integrator: added route-level `api_schema` read-only/broker-disabled enrichment for event detail, event trace, event trace summary, symbol trace, and symbol trace summary success payloads, preserving guarded error paths and avoiding command, cleanup, credential, DB write, and broker paths.
- Integrator: aligned Nuxt frontend payload types for event detail, event trace, symbol trace, and trace summaries so the client preserves trace/detail `api_schema` read-only/broker-disabled metadata without touching backend, broker, cleanup, credentials, or DB write paths.
- Integrator: aligned Nuxt frontend payload types for Operations, event-model research, technical calibration, technical promotion-review listing, and event-policy/evaluation so the client preserves read-only/broker-disabled `api_schema` metadata without touching backend, broker, cleanup, credentials, command execution, or DB write paths.
- Integrator: aligned Nuxt frontend payload types for action detail and portfolio detail so the client preserves read-only/broker-disabled `api_schema` metadata without touching backend, broker, cleanup, credentials, command execution, or DB write paths.
- Integrator: aligned Nuxt frontend payload types for wait-signal match results so the client preserves `api_schema` broker-disabled metadata without touching backend, broker, cleanup, credentials, command execution, or DB write paths.
- Integrator: aligned Nuxt frontend payload types for action-conflict rule read/write results so the client preserves `api_schema` broker-disabled metadata without touching backend, broker, cleanup, credentials, command execution, ranking behavior, or DB write paths.
- Integrator: added focused backend regression coverage proving a lifecycle HOLD that wins over a same-symbol watchlist WATCH remains review-only, preserves lifecycle/risk/conflict evidence, and records the losing WATCH candidate in source-precedence reason-contract evidence.
- Integrator: changed risk-off market gating for positive portfolio BUY_MORE add-ons to downgrade to review-only Manual Review instead of HOLD, preserving blocked original action `BUY_MORE`, `broker_execution_allowed=false`, macro gate evidence, and the losing same-symbol WATCH candidate in source-precedence reason-contract evidence.
- Integrator: added cautious-market BUY_MORE-over-WATCH regression coverage, preserving broker-capable add-on execution while reduced allocation/fraction/score, macro sizing evidence, and losing WATCH source-precedence evidence remain visible in the reason contract.
- Integrator: added event-policy Manual Review-over-market-adjusted BUY_MORE plus WATCH regression coverage, and source-precedence losing-candidate evidence now preserves market-adjustment context for the losing portfolio add-on.
- Integrator: added rebalance SELL-over-risk-off market-gated BUY_MORE plus WATCH regression coverage, proving exit precedence remains broker-capable and losing source-precedence evidence preserves the blocked original add-on context.
- Planner: confirmed the board's stale current rebalance slice was already integrated and recommended choosing another bounded action-source reason-contract permutation or read-only Operator API contract gap.
- Integrator: added adversarial-veto Manual Review-over-risk-off market-gated BUY_MORE plus WATCH regression coverage, proving veto precedence remains review-only and losing source-precedence evidence preserves blocked original add-on context.
- Next cycle after this: pick another bounded action-source reason-contract permutation, such as playbook exposure-review overlays against market-gated portfolio candidates, or a read-only Operator API contract gap; skip write routes that execute commands, mutate cleanup state, require credentials, or alter live broker behavior.

## Validation Log

- `python -m py_compile advisory/action_recommender.py tests/test_advisory_regression.py` passed for adversarial-veto Manual Review over risk-off market-gated portfolio BUY_MORE plus WATCH reason-contract source-precedence coverage.
- `pytest -q tests/test_advisory_regression.py -k 'adversarial_veto_beats_market_gated_buy_more_watch'` passed with 1 test.
- `python -m py_compile advisory/action_recommender.py tests/test_advisory_regression.py` passed for rebalance SELL over risk-off market-gated portfolio BUY_MORE plus WATCH reason-contract source-precedence coverage.
- `pytest -q tests/test_advisory_regression.py -k 'rebalance and market_gated and buy_more'` passed with 1 test.
- `python -m py_compile advisory/action_recommender.py tests/test_advisory_regression.py` passed for event-policy Manual Review over market-adjusted portfolio BUY_MORE plus WATCH reason-contract source-precedence coverage.
- `pytest -q tests/test_advisory_regression.py -k 'event_policy_manual_review_beats_market_adjusted_buy_more_watch'` passed with 1 test.
- `python -m py_compile advisory/action_recommender.py tests/test_advisory_regression.py` passed for market-gated portfolio Manual Review-over-WATCH reason-contract source-precedence coverage.
- `pytest -q tests/test_advisory_regression.py -k 'market_gated_manual_review_beats_watch_with_contract or blocks_positive_broker_action_in_risk_off_market'` passed with 2 tests.
- `python -m py_compile advisory/action_recommender.py tests/test_advisory_regression.py` passed for market-gated portfolio BUY_MORE Manual Review-over-WATCH reason-contract source-precedence coverage.
- `pytest -q tests/test_advisory_regression.py -k 'market_gated_buy_more or market_gated_manual_review_beats_watch or blocks_positive_broker_action_in_risk_off_market'` passed with 3 tests.
- `python -m py_compile advisory/action_recommender.py tests/test_advisory_regression.py` passed for cautious-market portfolio BUY_MORE-over-WATCH sizing-reduction reason-contract source-precedence coverage.
- `pytest -q tests/test_advisory_regression.py -k 'cautious_buy_more or reduces_positive_action_size or market_gated_buy_more'` passed with 3 tests.
- `python -m py_compile tests/test_advisory_regression.py` passed for lifecycle HOLD-over-WATCH reason-contract source-precedence coverage.
- `pytest -q tests/test_advisory_regression.py -k 'lifecycle_hold_beating_watch'` passed with 1 test.
- `npm --prefix apps/operator-web run typecheck` passed for frontend action-conflict rule API schema metadata type alignment.
- `npm --prefix apps/operator-web run typecheck` passed for frontend wait-signal match API schema metadata type alignment.
- `npm --prefix apps/operator-web run typecheck` passed for frontend action/portfolio detail API schema metadata type alignment.
- `npm --prefix apps/operator-web run typecheck` passed for frontend read-only operations/research API schema metadata type alignment.
- `npm --prefix apps/operator-web run typecheck` passed for frontend trace/detail API schema metadata type alignment.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py` passed for read-only trace/detail API schema metadata route coverage.
- `pytest -q tests/test_advisory_regression.py -k 'operator_api_symbol_trace_routes_smoke_and_error_paths or operator_api_event_detail_trace_routes_smoke_and_error_paths or operator_api_critical_routes_publish_typed_response_models'` passed with 3 tests.
- `python -m py_compile advisory/api/app.py` passed for read-only legacy health API typed response and smoke coverage.
- `python -m py_compile tests/test_advisory_regression.py` passed for read-only legacy health API typed response and smoke coverage.
- `pytest -q tests/test_advisory_regression.py -k 'operator_api_health_read_route_smoke_with_typed_payload or operator_api_critical_routes_publish_typed_response_models'` passed with 2 tests.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py` passed for read-only runtime API typed response and smoke coverage.
- `pytest -q tests/test_advisory_regression.py -k 'runtime_read_route_smoke or critical_routes_publish_typed_response_models or operator_api_runtime_payload'` passed with 4 tests.
- `npm --prefix apps/operator-web run typecheck` passed for the runtime API payload type update.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py` passed for read-only action/portfolio detail API typed response and smoke coverage.
- `pytest -q tests/test_advisory_regression.py -k 'action_portfolio_detail_read_routes_smoke or critical_routes_publish_typed_response_models'` passed with 2 tests.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py` passed for read-only data-health API typed response and smoke coverage.
- `pytest -q tests/test_advisory_regression.py -k 'data_health_read_route_smoke or critical_routes_publish_typed_response_models or critical_payloads_include_schema_metadata'` passed with 3 tests.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py` passed for read-only signal-refresh API typed response and smoke coverage.
- `pytest -q tests/test_advisory_regression.py -k 'signal_refresh_read_route_smoke or summary_watchlist_market_context_read_routes_smoke or critical_routes_publish_typed_response_models'` passed with 3 tests.
- `npm --prefix apps/operator-web run typecheck` passed for the signal-refresh API payload type update.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py` passed for read-only summary/watchlist/market-context API typed response and smoke coverage.
- `pytest -q tests/test_advisory_regression.py -k 'summary_watchlist_market_context_read_routes_smoke or home_portfolio_events_read_routes_smoke or critical_routes_publish_typed_response_models'` passed with 3 tests.
- `npm --prefix apps/operator-web run typecheck` passed for the summary/market-context API payload type update.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py` passed for read-only technical calibration API typed response and smoke coverage.
- `pytest -q tests/test_advisory_regression.py -k 'technical_calibration_read_routes_smoke or critical_routes_publish_typed_response_models or builds_technical_calibration_payload'` passed with 3 tests.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py` passed for read-only event-model research API typed response and smoke coverage.
- `pytest -q tests/test_advisory_regression.py -k 'event_model_research_read_routes_smoke or critical_routes_publish_typed_response_models'` passed with 2 tests.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py` passed for read-only hypotheses API typed response and smoke coverage.
- `pytest -q tests/test_advisory_regression.py -k 'hypotheses_read_route_smoke or critical_routes_publish_typed_response_models or operator_api_hypothesis_payloads'` passed with 3 tests.
- `npm --prefix apps/operator-web run typecheck` passed for the hypotheses API payload type update.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py` passed for read-only event-policy/evaluation API typed response and smoke coverage.
- `pytest -q tests/test_advisory_regression.py -k 'event_policy_read_routes_smoke or critical_routes_publish_typed_response_models or builds_event_policy_payload or builds_event_policy_evaluation_payload'` passed with 4 tests.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py` passed for read-only home/portfolio/events API typed response and smoke coverage.
- `pytest -q tests/test_advisory_regression.py -k 'home_portfolio_events_read_routes_smoke or critical_routes_publish_typed_response_models'` passed with 2 tests.
- `npm --prefix apps/operator-web run typecheck` passed for the home/portfolio/events API payload type update.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py` passed for read-only Operations API typed response and smoke coverage.
- `pytest -q tests/test_advisory_regression.py -k 'operations_read_routes_smoke or critical_routes_publish_typed_response_models or lists_operator_commands'` passed with 3 tests.
- `python -m py_compile tests/test_advisory_regression.py advisory/action_recommender.py` passed for BUY_MORE-over-WATCH reason-contract source-precedence coverage.
- `pytest -q tests/test_advisory_regression.py -k 'buy_more_beating_watch or buy_more_beating_portfolio_buy or portfolio_buy_beating_watch'` passed with 3 tests.
- `python -m py_compile tests/test_advisory_regression.py advisory/action_recommender.py` passed for TIGHTEN_STOP-over-WATCH reason-contract source-precedence coverage.
- `pytest -q tests/test_advisory_regression.py -k 'tighten_stop_beating_watch or tighten_stop_beating_portfolio_buy or partial_sell_beating_watch'` passed with 3 tests.
- `python -m py_compile tests/test_advisory_regression.py advisory/action_recommender.py` passed for PARTIAL_SELL-over-WATCH reason-contract source-precedence coverage.
- `pytest -q tests/test_advisory_regression.py -k 'partial_sell_beating_watch or partial_sell_beating_portfolio_buy or sell_beating_watch'` passed with 3 tests.
- `python -m py_compile tests/test_advisory_regression.py advisory/action_recommender.py` passed for SELL-over-WATCH reason-contract source-precedence coverage.
- `pytest -q tests/test_advisory_regression.py -k 'sell_beating_watch or portfolio_sell_beating_portfolio_buy or buy_sell_manual_watch_collision'` passed with 3 tests.
- `python -m py_compile tests/test_advisory_regression.py advisory/action_recommender.py` passed for portfolio SELL-over-portfolio BUY reason-contract source-precedence coverage.
- `pytest -q tests/test_advisory_regression.py -k 'portfolio_sell_beating_portfolio_buy or partial_sell_beating_portfolio_buy or tighten_stop_beating_portfolio_buy'` passed with 3 tests.
- `python -m py_compile tests/test_advisory_regression.py advisory/action_recommender.py` passed for TIGHTEN_STOP-over-portfolio BUY reason-contract source-precedence coverage.
- `pytest -q tests/test_advisory_regression.py -k 'tighten_stop_beating_portfolio_buy or partial_sell_beating_portfolio_buy or buy_more_beating_portfolio_buy'` passed with 3 tests.
- `python -m py_compile tests/test_advisory_regression.py advisory/action_recommender.py` passed for BUY_MORE-over-portfolio BUY reason-contract source-precedence coverage.
- `pytest -q tests/test_advisory_regression.py -k 'buy_more_beating_portfolio_buy or portfolio_buy_beating_watch or partial_sell_beating_portfolio_buy'` passed with 3 tests.
- `python -m py_compile tests/test_advisory_regression.py advisory/action_recommender.py` passed for PARTIAL_SELL-over-portfolio BUY reason-contract source-precedence coverage.
- `pytest -q tests/test_advisory_regression.py -k 'partial_sell_beating_portfolio_buy or portfolio_buy_beating_watch or generic_manual_review_contract_beats_portfolio_buy'` passed with 3 tests.
- `python -m py_compile advisory/action_recommender.py tests/test_advisory_regression.py` passed for generic Manual Review-over-portfolio BUY reason-contract boundary coverage.
- `pytest -q tests/test_advisory_regression.py -k 'generic_manual_review_contract_beats_portfolio_buy or event_and_playbook_manual_review_contract_boundaries or rebalance_manual_review_contract'` passed with 3 tests.
- `python -m py_compile tests/test_advisory_regression.py advisory/action_recommender.py` passed for portfolio BUY-over-WATCH reason-contract source-precedence coverage.
- `pytest -q tests/test_advisory_regression.py -k 'portfolio_buy_beating_watch or manual_review_beating_watch or duplicate_buy_screener_collapse'` passed with 3 tests.
- `python -m py_compile advisory/action_recommender.py tests/test_advisory_regression.py` passed for market-gated portfolio Manual Review reason-contract boundary evidence.
- `pytest -q tests/test_advisory_regression.py -k 'blocks_positive_broker_action_in_risk_off_market or reason_contract'` passed with 10 tests.
- `python -m py_compile advisory/action_recommender.py tests/test_advisory_regression.py` passed for event-policy/playbook Manual Review reason-contract boundary evidence.
- `pytest -q tests/test_advisory_regression.py -k 'event_and_playbook_manual_review_contract_boundaries or rebalance_manual_review_contract or incomplete_contract_manual_review_boundary'` passed with 3 tests.
- `python -m py_compile advisory/action_recommender.py tests/test_advisory_regression.py` passed for lifecycle/rebalance Manual Review reason-contract boundary permutations.
- `pytest -q tests/test_advisory_regression.py -k 'rebalance_manual_review_contract or event_and_playbook_manual_review_contract_boundaries or incomplete_contract_manual_review_boundary'` passed with 3 tests.
- `python -m py_compile advisory/action_recommender.py tests/test_advisory_regression.py` passed for lifecycle rebalance Manual Review reason-contract boundary evidence.
- `pytest -q tests/test_advisory_regression.py -k 'rebalance_manual_review_contract or incomplete_contract_manual_review_boundary or reason_contract_downgrades_missing_buy_reason'` passed with 3 tests.
- `python -m py_compile tests/test_advisory_regression.py advisory/action_recommender.py` passed for promoted exact-match reason-contract attribution.
- `pytest -q tests/test_advisory_regression.py -k 'promoted_conflict_rule_changes_candidate_ranking or reason_contract'` passed with 10 tests.
- `npm --prefix apps/operator-web run test -- --run tests/operator-pages.test.ts` passed with 4 tests for Symbol/Health page safety smoke coverage.
- `npm --prefix apps/operator-web run typecheck` passed for Symbol/Health page safety smoke coverage.
- `npm --prefix apps/operator-web run test -- --run tests/operator-pages.test.ts` passed with 2 tests.
- `npm --prefix apps/operator-web run typecheck` passed for page-level operator safety smoke coverage.
- `npm --prefix apps/operator-web run test -- --run tests/operator-components.test.ts` passed with 4 tests.
- `npm --prefix apps/operator-web run typecheck` passed for operator component smoke coverage.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py` passed for global live-trading visibility.
- `pytest -q tests/test_advisory_regression.py -k 'operator_api_runtime_payload'` passed with 2 tests.
- `npm --prefix apps/operator-web run typecheck` passed for the runtime header safety badge.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py` passed for source-specific freshness warnings.
- `pytest -q tests/test_advisory_regression.py -k 'builds_manual_review_payload or builds_identity_issues_payload or builds_wait_signal_sections'` passed with 3 tests.
- `npm --prefix apps/operator-web run typecheck` passed for non-snapshot source warning UI.
- `python -m py_compile advisory/wait_signals.py advisory/signal_refresh.py advisory/manual_review_state.py advisory/api/app.py`
- `pytest -q tests/test_advisory_regression.py -k 'wait_signal or signal_refresh or manual_review'` passed with 22 tests.
- `npm --prefix apps/operator-web run typecheck` passed.
- `npm --prefix apps/operator-web run typecheck` passed for symbol-detail stale snapshot warning visibility.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py` passed.
- `pytest -q tests/test_advisory_regression.py -k 'identity_issues or critical_payloads_include_schema_metadata or critical_routes_publish_typed_response_models or critical_routes_smoke_with_typed_payloads'` passed with 4 tests.
- `npm --prefix apps/operator-web run typecheck` passed for standalone identity issues read-only page.
- `python -m py_compile advisory/api/app.py advisory/operator_health.py`
- `pytest -q tests/test_advisory_regression.py -k 'superseded or recovered_announcement or manual_review_suppresses or degradation_feed_includes_announcement_failures'` passed with 3 tests.
- `python -m py_compile advisory/api/app.py`
- `pytest -q tests/test_advisory_regression.py -k 'operator_api_splits_dashboard_payload'` passed with 1 test.
- `npm --prefix apps/operator-web run typecheck` passed.
- `python -m py_compile advisory/operator_health.py`
- `pytest -q tests/test_advisory_regression.py -k 'operator_health_current_blockers or operator_health_summarizes_worst_status'` passed with 2 tests.
- `npm --prefix apps/operator-web run typecheck` passed.
- `python -m py_compile advisory/identity_issues.py data/dhanlive/dhan_db.py advisory/api/app.py`
- `pytest -q tests/test_advisory_regression.py -k 'dhan_identity_records_missing_security_id_issue or operator_api_builds_manual_review_payload'` passed with 2 tests.
- `python -m py_compile advisory/position_lifecycle.py`
- `pytest -q tests/test_advisory_regression.py -k 'position_lifecycle'` passed with 8 tests.
- `python -m py_compile advisory/execution_engine.py`
- `pytest -q tests/test_advisory_regression.py -k 'execution_engine or submit_live_orders'` passed with 8 tests.
- `python -m py_compile advisory/superseded_failures.py`
- `pytest -q tests/test_advisory_regression.py -k 'superseded_failure_cleanup or manual_review_suppresses_superseded'` passed with 3 tests.
- `python -m py_compile advisory/manual_review_state.py advisory/api/app.py`
- `pytest -q tests/test_advisory_regression.py -k 'manual_review_state or matched_manual_review_wait_signal or replaces_waiting_original'` passed with 4 tests.
- `python -m py_compile advisory/action_recommender.py`
- `pytest -q tests/test_advisory_regression.py -k 'matched_manual_review_wait_signal or wait_signal_action_candidates or manual_review_wait_signal'` passed with 2 tests.
- `python -m py_compile advisory/action_recommender.py`
- `pytest -q tests/test_advisory_regression.py -k 'action_recommender_bridges_matched_manual_review_wait_signal or reason_contract'` passed with 4 tests.
- `python -m py_compile advisory/action_recommender.py`
- `pytest -q tests/test_advisory_regression.py -k 'same_symbol_source_precedence or reason_contract'` passed with 4 tests.
- `python -m py_compile advisory/action_recommender.py`
- `pytest -q tests/test_advisory_regression.py -k 'duplicate_buy_screener_collapse or manual_review_beating_watch or same_symbol_source_precedence or reason_contract'` passed with 6 tests.
- `python -m py_compile advisory/action_recommender.py`
- `pytest -q tests/test_advisory_regression.py -k 'reason_contract_matrix or event_playbook_lifecycle or reason_contract'` passed with 7 tests.
- `python -m py_compile advisory/api/app.py`
- `pytest -q tests/test_advisory_regression.py -k 'operator_api_splits_dashboard_payload'` passed with 1 test.
- `npm --prefix apps/operator-web run typecheck` passed.
- `python -m py_compile tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'reason_contract_matrix or event_playbook_lifecycle'` passed with 2 tests.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'matched_wait_followup or replaces_waiting_original or wait_signal_followup or manual_review_wait_signal or signal_refresh_keeps_manual_review_wait_signal or signal_refresh_does_not_escalate_positive_wait_signal'` passed with 6 tests.
- `python -m py_compile tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'enabled_conflict_rule_changes_candidate_ranking or decision_trace_builds_action_conflicts'` passed with 2 tests.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'action_conflict_rule_enablement or enabled_conflict_rule_changes_candidate_ranking or decision_trace_builds_action_conflicts'` passed with 3 tests.
- `npm --prefix apps/operator-web run typecheck` passed.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'operator_api_runtime_payload or operator_api_splits_dashboard_payload'` passed with 2 tests.
- `npm --prefix apps/operator-web run typecheck` passed.
- `python -m py_compile advisory/api/app.py advisory/decision_trace.py tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'action_conflict_rule_enablement or action_conflict_rule_reason or enabled_conflict_rule_changes_candidate_ranking or decision_trace_builds_action_conflicts'` passed with 4 tests.
- `npm --prefix apps/operator-web run typecheck` passed.
- `python -m py_compile advisory/decision_trace.py advisory/action_conflict_resolver.py advisory/api/app.py tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'promoted_conflict_rule or action_conflict_rule'` passed with 4 tests.
- `npm --prefix apps/operator-web run typecheck` passed.
- `python -m py_compile advisory/operator_health.py tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'operator_health_degradation_feed'` passed with 3 tests.
- `npm --prefix apps/operator-web run typecheck` passed.
- `npm --prefix apps/operator-web run typecheck` passed for Manual Review item-impact and decision-boundary visibility.
- `python -m py_compile tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'operator_journey_manual_review_wait_signal_match_reopens_review_only_candidate or manual_review_wait_signal or matched_wait_followup'` passed with 6 tests.
- `python -m py_compile advisory/action_recommender.py tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'promoted_conflict_rule or enabled_conflict_rule_changes_candidate_ranking or action_conflict_rule'` passed with 6 tests.
- `python -m py_compile advisory/live_dashboard.py advisory/api/app.py tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'operator_api_splits_dashboard_payload or execution_engine_marks_action_order_preview_as_approval_gated'` passed with 2 tests.
- `npm --prefix apps/operator-web run typecheck` passed.
- `python -m py_compile advisory/execution_engine.py tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'broker_account_budget or reconcile_live_orders or execution_engine_uses_broker_cash_cap or submit_live_orders'` passed with 6 tests.
- `python -m py_compile advisory/execution_engine.py tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'persist_reconciliation or reconcile_live_orders or broker_account_budget or submit_live_orders'` passed with 6 tests.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'action_conflict_rule_condition or action_conflict_rule_reason or action_conflict_rule_enablement or promoted_conflict_rule'` passed with 5 tests.
- `npm --prefix apps/operator-web run typecheck` passed.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'operator_api_critical_payloads_include_schema_metadata or operator_api_health_details_payload or operator_api_builds_manual_review_payload or operator_api_builds_wait_signal_sections or operator_api_updates_action_conflict_rule_condition'` passed with 5 tests.
- `npm --prefix apps/operator-web run typecheck` passed.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'operator_api_critical_payloads_include_schema_metadata or operator_api_critical_routes_publish_typed_response_models'` passed with 2 tests.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'operator_api_critical_payloads_include_schema_metadata or operator_api_critical_routes_publish_typed_response_models or operator_api_critical_routes_smoke_with_typed_payloads or operator_api_builds_wait_signal_sections'` passed with 4 tests.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'operator_api_critical_routes_publish_typed_response_models or operator_api_symbol_trace_routes_smoke_and_error_paths or operator_api_symbol_trace_payload or operator_api_trace_summary'` passed with 5 tests.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'operator_api_critical_routes_publish_typed_response_models or operator_api_event_detail_trace_routes_smoke_and_error_paths or operator_api_event_trace_payload or operator_api_trace_summary'` passed with 5 tests.
- `python -m py_compile advisory/action_recommender.py advisory/decision_trace.py tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'adversarial_veto_manual_review or adversarial_review or reason_contract_explains_manual_review_beating_watch'` passed with 7 tests.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'operator_api_splits_dashboard_payload or adversarial_veto_manual_review or reason_contract_explains_manual_review_beating_watch'` passed with 3 tests.
- `npm --prefix apps/operator-web run typecheck` passed.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'operator_api_splits_dashboard_payload or positive_broker_action_in_risk_off_market'` passed with 2 tests.
- `npm --prefix apps/operator-web run typecheck` passed.
- `python -m py_compile advisory/decision_trace.py advisory/api/app.py tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'manual_review_wait_signal_links or trace_summary_normalizes_manual_review_wait_signal_links or operator_api_symbol_trace_payload or operator_api_event_trace_payload'` passed with 4 tests.
- `npm --prefix apps/operator-web run typecheck` passed.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'operator_api_splits_dashboard_payload'` passed with 1 test.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py`
- `pytest -q tests/test_advisory_regression.py -k 'operator_api_splits_dashboard_payload'` passed with 1 test.
- `npm --prefix apps/operator-web run typecheck` passed.
- `python -m py_compile advisory/signal_refresh.py advisory/api/app.py tests/test_advisory_regression.py`
- `rg -n "Watcher trigger boundaries|intraday evidence/signal-refresh layer|document which events trigger only signal refresh" docs/operators_manual.md docs/scripts.md analysis.md docs/analysis_agent_board.md` passed.
- `git diff --check -- docs/operators_manual.md docs/scripts.md analysis.md docs/analysis_agent_board.md` passed.
- `rg -n "[ \t]+$" docs/operators_manual.md docs/scripts.md analysis.md docs/analysis_agent_board.md` found no trailing whitespace.
- `pytest -q tests/test_advisory_regression.py -k 'signal_refresh or event_router_execute_uses_signal_refresh or compact_signal_refresh'` passed with 8 tests.
- `git diff --check -- docs/operators_manual.md analysis.md docs/analysis_agent_board.md` passed.
- `rg -n "Advisory, Watcher, Wait Signal, And Manual Review Boundaries|Document advisory/watch/signal/manual-review boundaries|document exact difference" docs/operators_manual.md analysis.md docs/analysis_agent_board.md` passed.
- `rg -n "[ \t]+$" docs/operators_manual.md analysis.md docs/analysis_agent_board.md` found no trailing whitespace.
- `npm --prefix apps/operator-web run typecheck` passed.
- `git diff --check -- docs/operators_manual.md analysis.md docs/analysis_agent_board.md` passed.
- `rg -n "Manual Review decision effects|Document Manual Review operator decision effects|operator decision effects|reopened_wait_signal_matched" docs/operators_manual.md analysis.md docs/analysis_agent_board.md` passed.
- `rg -n "[ \t]+$" docs/operators_manual.md analysis.md docs/analysis_agent_board.md` found no trailing whitespace.
- `python -m py_compile advisory/operator_health.py tests/test_advisory_regression.py` passed.
- `pytest -q tests/test_advisory_regression.py -k 'operator_health_degradation_feed_groups_active_recovered_superseded or superseded_failure_cleanup'` passed with 3 tests.
- `npm --prefix apps/operator-web run typecheck` passed.
- `git diff --check -- advisory/operator_health.py apps/operator-web/pages/health.vue tests/test_advisory_regression.py docs/operators_manual.md docs/scripts.md analysis.md docs/analysis_agent_board.md` passed.
- `bash -n all_superseded_cleanup_audit.sh` passed.
- `python -m py_compile advisory/api/app.py tests/test_advisory_regression.py` passed.
- `pytest -q tests/test_advisory_regression.py -k 'operator_api_lists_operator_commands or superseded_failure_cleanup'` passed with 3 tests.
- `python -m py_compile tests/test_advisory_regression.py` passed.
- `pytest -q tests/test_advisory_regression.py -k 'raw_broker_execution_safety_contract or operator_api_splits_dashboard_payload or execution_engine_marks_action_order_preview_as_approval_gated'` passed with 3 tests.
- `python -m py_compile advisory/action_recommender.py tests/test_advisory_regression.py` passed.
- `pytest -q tests/test_advisory_regression.py -k 'incomplete_contract_manual_review_boundary or reason_contract_downgrades_missing_buy_reason'` passed with 2 tests.
- `python -m py_compile advisory/continuous_watch.py tests/test_advisory_regression.py` passed.
- `pytest -q tests/test_advisory_regression.py -k 'continuous_watch and (skipped_due_cycles or records_failed_cycle or ohlcv_from_cursor)'` passed with 4 tests.
- `python -m py_compile advisory/continuous_watch.py tests/test_advisory_regression.py` passed.
- `pytest -q tests/test_advisory_regression.py -k 'continuous_watch and (lock_skipped or skipped_due_cycles or records_failed_cycle or ohlcv_from_cursor)'` passed with 5 tests.
- `bash -n all_watchers.sh` passed.
- `git diff --check -- all_watchers.sh advisory/continuous_watch.py tests/test_advisory_regression.py analysis.md docs/analysis_agent_board.md` passed.
- `python -m py_compile tests/test_advisory_regression.py` passed.
- `pytest -q tests/test_advisory_regression.py -k 'buy_sell_manual_watch_collision or reason_contract_matrix or same_symbol_source_precedence'` passed with 4 tests.
- `python -m py_compile advisory/continuous_watch.py tests/test_advisory_regression.py` passed.
- `pytest -q tests/test_advisory_regression.py -k 'continuous_watch_event_cursor or continuous_watch_news_cycle_persists_bounded_catchup_window or continuous_watch_announcement_cycle_passes_bounded_catchup_window or continuous_watch_ohlcv_from_cursor'` passed with 6 tests.
