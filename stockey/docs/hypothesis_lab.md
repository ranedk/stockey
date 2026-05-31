# Investor Playbook Lab

## Objective

Capture investor playbooks as versioned reliability overlays and convert matches into cautious operator action plans instead of hardcoding them directly into trading rules.

The system should:

- match playbooks against news, announcements, macro events, and policy text
- store every match point-in-time
- use an LLM action planner to decide what else to check and how to act safely
- show results in the operator app
- allow only explicitly trusted playbooks to influence review-only overlays or risk controls

## Example

Playbook:

If the Prime Minister, finance minister, RBI governor, or another top authority calls for austerity, fiscal tightening, or spending cuts, the broad market may fall. The system should consider going cash, reducing exposure, or researching short exposure.

This should not directly liquidate the portfolio. It should generate an action plan that checks contradictory evidence, price reaction, current exposure, regime context, and operator approval before any action.

## Hypothesis Schema

Suggested `config/hypotheses.yaml` fields:

```yaml
hypotheses:
  - hypothesis_id: MARKET_AUSTERITY_RISK_V1
    title: Top authority austerity risk
    description: Top authority calls for austerity or fiscal tightening may create market downside pressure.
    source: investor_interview
    status: active_review
    trigger_scope: market
    trigger_patterns:
      keywords:
        include:
          - austerity
          - fiscal tightening
          - spending cuts
          - expenditure rationalisation
        exclude:
          - company internal cost cutting
      authority_roles:
        - prime_minister
        - finance_minister
        - rbi_governor
      source_reliability_min: medium
      required_evidence_count: 1
    expected_effect:
      action_bias: reduce_exposure
      market_direction: negative
      volatility: higher
    holding_window:
      horizons_days: [1, 3, 5, 10, 20]
    validation_protocol:
      method: point_in_time_forward_return
      benchmark: NIFTY
      costs_bps: 25
```

## Current V1 Implementation

The first implementation keeps the historical table/API names for compatibility, but the operator-facing concept is now an investor playbook.

- Backend engine: `advisory/hypothesis_engine.py`
- Versioned config: `config/hypotheses.yaml`
- API endpoints:
  - `GET /api/hypotheses`
  - `POST /api/hypotheses`
  - `POST /api/hypotheses/preview`
  - `POST /api/hypotheses/{hypothesis_id}`
  - `POST /api/hypotheses/run`
- Frontend page: `apps/operator-web/pages/hypotheses.vue`
- Action-plan table: `advisory_playbook_action_plans`
- Sources scanned in V1:
  - `advisory_news_events`
  - `advisory_watch_events`
  - `announcement_pipeline_documents`

V1 matches trigger keywords/phrases against persisted event text and creates a conservative suggested action such as `MANUAL_REVIEW`, `REDUCE_EXPOSURE_REVIEW`, or `SHORT_RESEARCH_ONLY`.

Import versioned playbooks:

```sh
python -m advisory.hypothesis_engine --import-config config/hypotheses.yaml
```

Preview without writing:

```sh
python -m advisory.hypothesis_engine --import-config config/hypotheses.yaml --dry-run
```

The Operator Playbooks page also has a normalized preview before save. Use it to confirm the generated trigger patterns, expected effect, decision policy, and DB row shape before writing a playbook. Existing playbooks can be loaded into the form for edits. Moving a playbook to `trusted_overlay` requires explicit confirmation because trusted overlays can influence consolidated actions as review-only overlays.

Reliability checks use the legacy backend audit endpoint/table names:

- `POST /api/hypotheses/{hypothesis_id}/promotion-audit` creates an audit row in `advisory_playbook_promotion_audits`.
- The audit records match count, source coverage, symbol coverage, sample evidence, point-in-time forward returns from `dhan_ohlcv_daily`, excess returns versus NIFTY, and sector excess returns from `master_sharpely_equity.sector_code`.
- Forward returns use the first trading day after the evidence timestamp as the anchor and the configured holding-window horizons when available.
- Sector excess returns intentionally avoid a brittle manual sector-index map. They use the broad Sharpely master sector code because the live `sharpely_stock_meta` and peer snapshots are currently not reliable enough for audit-grade labels, and fail loudly when stock-level sector coverage is unavailable.
- A reliability check returns `sufficient_history` or `insufficient_history`; it is evidence for operator judgement, not a statistical validation gate.
- The Operator UI can run the check and shows the latest reliability status on each playbook card.

Run a scan and let Codex create bounded action plans:

```sh
python -m advisory.hypothesis_engine --run-scan
```

For each match, the action planner creates:

- action type
- urgency
- confidence
- trusted overlay flag
- operator summary
- decision reason
- checks to run before action
- risk controls and safe boundaries
- top-50% market-context snapshot and any market-context adjustment

The default planner uses Codex through `utils.codex_cli`. If Codex is unavailable or disabled, the deterministic fallback still creates a conservative action plan and records the LLM status.

The planner receives the latest `advisory_market_context_summary_daily` and matching symbol row from `advisory_market_context_universe_daily`. In weak breadth or risk-off regimes, positive playbook actions are downgraded to `BUY_WATCH`/manual review, and risk-reduction playbooks get higher urgency. Market context is evidence and a safety gate; it is not a standalone buy/sell rule.

It does not auto-trade or auto-liquidate. `active_review` playbooks can be scanned and action-planned for review. Only `trusted_overlay` playbooks are allowed to become `advisory_action_recommendations` candidates, and even then only as `review_only` risk overlays such as `MANUAL_REVIEW` or `WATCH`; they do not create broker-executable orders directly.

## Tables

Current and planned tables:

- `advisory_hypotheses`
- `advisory_hypothesis_matches`
- `advisory_playbook_action_plans`
- `advisory_playbook_promotion_audits`
- `advisory_action_recommendations` for the final non-executable playbook overlay when the playbook is trusted
- `advisory_hypothesis_evaluations`
- `advisory_research_runs` with `run_type=hypothesis_eval`

`advisory_hypothesis_matches` should include:

- hypothesis id and version
- matched source id
- source type
- symbol or market scope
- matched timestamp
- evidence snippets
- confidence
- matched rule details
- point-in-time context

`advisory_hypothesis_evaluations` should include:

- hypothesis id and version
- evaluation date range
- horizon
- forward return
- benchmark return
- drawdown
- go-cash avoided loss/gain
- short-side return if researched
- false positive estimate
- sample size
- validation status

## Evaluation Rules

Use point-in-time discipline:

- only use news/announcement text available at the event timestamp
- apply publication lag for macro data
- compare against passive benchmark and current advisory action
- include costs and slippage
- record every tested config in the research ledger

Do not:

- tune keywords repeatedly without logging
- use future market moves to define the trigger
- promote sparse hypotheses without enough samples
- allow shorting in production without separate risk controls

## Production Promotion

Validated hypotheses can become:

- market overlay
- sector overlay
- risk cap adjustment
- manual-review trigger
- event-model feature
- adversarial-review rule

They should not directly become unconstrained buy/sell rules.
