# Manual Review Resolution Inventory

Updated: `2026-07-05`

This matrix supports the P1.10 LLM-resolved review epic. It inventories the current
sources that create or surface Manual Review work and classifies which ones are
safe candidates for later automated LLM resolution. It is descriptive only: it
does not change queue behavior, portfolio state, broker execution, or cleanup
state.

## Resolution Policy

Manual Review currently means "review-only input", not broker authority. Later
resolver work should preserve these boundaries:

- The resolver may map ambiguous review items to review-only outcomes such as
  `NO_ACTION`, `WATCH`, `BUY_WATCH`, `REDUCE_EXPOSURE_REVIEW`, or "keep waiting".
- Direct `BUY`/`SELL`/sizing authority belongs to the separate LLM direct-authority
  path and remains behind the default-OFF live authority flags.
- Low-confidence or contradictory items should hold as review-only/no-action or
  escalate to a stronger LLM evidence pass. They should not silently pass.
- Every external fact used later must carry provenance, fetch time, query/context,
  and identity validation before it can influence a symbol decision.

## Source Matrix

| Source family | Current source / surface | Decision needed | Evidence already loaded | External research useful? | Auto-resolution candidate | Later resolver action |
|---|---|---|---|---|---|---|
| Event-policy ambiguous rows | `advisory/event_policy.py`; consumed by `advisory/action_recommender.py` as event-policy `MANUAL_REVIEW`, `BUY_WATCH`, and `REDUCE_EXPOSURE_REVIEW` rows | Classify the event as no-action, watch pressure, de-risk pressure, or unresolved review | Event class, policy class, setup effect, materiality, confidence, actionability, source trace, operator notes, LLM review metadata | Yes, for materiality, company identity, management/filing context, and whether later market/news evidence resolves the ambiguity | High. This is the existing seed: one bounded LLM reviewer already exists for event-policy rows | Generalize first. Preserve review-only outputs and provenance; keep direct broker actions out of scope |
| Playbook / hypothesis action plans | `advisory/hypothesis_engine.py`; mapped in `advisory/action_recommender.py` via playbook action maps | Decide whether a playbook match should remain manual review, become watch/de-risk pressure, or wait for explicit conditions | Hypothesis id/status, expected effect, matched event text, score, action plan, checks, wait-signal plan | Yes, mainly to verify event semantics, symbol identity, and contradiction with current filings/news | High for event-driven playbooks with trusted identity and bounded outcomes; medium for broad sector or macro plans | Route after event-policy. Require hypothesis status, symbol-scope validation, and source-event provenance |
| Manual Review wait-signal follow-ups | `advisory/wait_signals.py`, `advisory/signal_refresh.py`, trace links in `advisory/decision_trace.py` | Decide whether matched evidence closes the original wait, keeps watching, or creates review-only action pressure | Original manual item id/source key, wait question, condition JSON, match evidence, match score/reason, observed timestamp | Sometimes. Useful when a match snippet is ambiguous or needs external corroboration | High when condition is typed and matched evidence is source-bound; medium for free-text keyword waits | Resolve only against the original item and match evidence. Never treat a match as a trade by itself |
| Action-conflict unresolved rows | `advisory/decision_trace.py`, `advisory/action_conflict_resolver.py`, Conflict Rules UI | Decide whether a repeated winner/loser combination needs a deterministic rule or should keep the current winner | Winning/losing action/source, priorities, raw reason context, existing deterministic rule matches, promoted rules | Rarely. Most conflicts are internal policy precedence, not external facts | Medium for explanation/rule-candidate drafting; low for one-off trade decisions | Prefer deterministic rule proposals with audit text. Do not rewrite historical action rows automatically |
| Market-gated positive actions | `advisory/action_recommender.py` boundary `market_context_positive_action_review_required` | Decide whether risk-off/cautious market context should keep a positive action review-only or allow watch pressure | Original positive action, market gate reason, macro regime, breadth/risk-off score, blocked original action | Maybe, but market context should come from existing market tables; external news can only annotate | Medium. Safe output is "keep review-only", "watch", or "rerun after fresh market context" | Do not override market gates into broker actions. Later resolver can explain or request refreshed market context |
| Lifecycle/rebalance review boundaries | `advisory/action_recommender.py` boundaries `review_manual`, `review_stale`, `review_horizon`, `review_target` | Decide whether lifecycle state is stale/incomplete, target/horizon needs policy review, or risk pressure should remain review-only | Position/lifecycle state, age/horizon, target/stop context, reason contract, transition blockers | Usually no for state gaps; maybe for corporate-action/news context on stale positions | Medium for stale/target interpretation; low for missing position-state repair | Separate data repair from investment judgment. Missing state should hold/refresh, not become an LLM exit |
| Incomplete reason-contract rows | `advisory/action_recommender.py` boundary `incomplete_reason_contract`; diagnostics classify as `contract_quality_blocker` | Repair missing required evidence before action authority is trusted | Missing fields, original blocked action, reason-contract status, review-only boundary | No. The source problem is internal contract completeness | Low. This is an engineering/data-quality repair, not an LLM decision | Hold as no-action/review-only and route to diagnostics or contract repair |
| Execution blockers / dry-run approval gates | `advisory/execution_engine.py`, execution approval surfaces | Decide whether an execution row has missing preconditions, stale approval/reconciliation, or blocked live gates | Safety checks JSON, missing preconditions, dry-run status, broker identity, approval/reconciliation evidence | No for broker safety. External research should not clear execution gates | Low. Resolver may summarize blockers only | Never auto-approve or submit. Keep explicit operator/live-gate workflow separate |
| Identity issues / skipped symbols | `advisory/identity_issues.py`, `/api/identity-issues`, Manual Review linkage | Decide whether a security mapping is missing, resolved, or still requires data repair | Issue key/type, symbol, requested exchange, attempted exchanges/fallbacks, error text, suggested action | Yes only for identity disambiguation, but final mapping needs source-master validation | Medium for suggesting candidate identity; low for automatic closure without validation | Later resolver may propose validated identity candidates. It must not mutate mappings or close issues silently |
| Technical threshold promotion reviews | `advisory/technical_threshold_promotion.py`, API promotion-review endpoints | Decide whether a candidate threshold patch is acceptable, overfit, or needs more evidence | Current/candidate thresholds, calibration evidence, archetype breakdown, LLM review, pending patch | No for the core decision; outcome stats are local. External research is not useful | Medium for a research/config reviewer, not Manual Review investment resolution | Keep as config-review workflow. Auto-resolution can draft recommendation, but config application remains explicit |
| Signal-quality / split promotion reviews | `advisory/signal_quality_promotion.py`, `advisory/signal_quality_split_evaluator.py`, health summaries | Decide whether a signal-quality overlay or split is stable enough to promote | Matured evidence, after-cost and benchmark-excess metrics, FDR/sector/runtime blocks, promotion review rows | No. Promotion should be evidence/statistics driven | Medium for summarization; low for automatic promotion | Keep research/config boundary. Resolver can explain but not apply strategy/config changes |
| Event-policy / TS-forecast promotion reviews | `advisory/event_policy_promotion.py`, `advisory/ts_forecast_promotion.py`, research API/health | Decide whether research artifacts or policies merit promotion | Evaluation summaries, promotion checks, review rows, benchmark/after-cost gates | No for statistical promotion; maybe for qualitative caveats only | Low to medium, depending on available mature evidence | Do not auto-promote models/policies. Generate review-only recommendation and require explicit config path |
| Operational failures and ingestion blockers | Operator Health, ingestion-file summaries, OCR/parser/API/Codex fallback rows | Decide whether the failure is current/actionable, recovered, superseded, or needs rerun/repair | Error type, source table/file, timestamps, recovery/superseded preview, fix hints | Sometimes, for public incident context or vendor docs; not for clearing local failures | Low for resolution; medium for explanation | Keep in technical lane. Resolver may suggest rerun/repair, but must not run cleanup/apply paths |

## Implementation Notes For Later Slices

- Start resolver wiring with event-policy rows, then typed Manual Review wait-signal
  follow-ups and playbook action plans. These have the clearest source evidence
  and review-only output contracts.
- Keep operational/data repair sources separate from investment-review sources.
  Identity, execution, incomplete-contract, and ingestion failures should not be
  converted into investment decisions.
- Config/research promotion reviews can use LLM summarization, but the application
  of thresholds, overlays, policies, or model promotions must stay explicit.
- The current API/docs still use "Manual Review" language for operator workflows.
  A later PRD/UI slice should rename or explain this as LLM-resolved review once
  the reusable resolver exists.
