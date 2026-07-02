# Advisory Architecture

This document describes the current architecture of the live investment advisory system.

It is not a future-state PRD. Use [`todo.md`](../todo.md) for the active roadmap.

## Design intent

The project is built around four layers:

1. extraction
2. prediction
3. policy
4. execution support

The main design constraint is that LLMs help with extraction, event interpretation, playbook action planning, and review, but they do not get final trading authority. Production decisions are primarily driven by validated investor playbooks, deterministic event policies, macro/regime gates, technical entry/exit timing, and portfolio risk controls. ML training remains optional research, not the default path to signal.

## Runtime flows

The repository has three primary runtime modes:

1. data refresh
   - `./all_downloaders.sh`
   - `./all_parsers.sh`
   - `./complete_data.sh`
2. optional model research and training
   - `./all_ml.sh`
3. batch advisory and continuous watch
   - `./all_advisory.sh`
   - `./all_watchers.sh --loop`
4. operator visibility
   - `./all_frontend.sh`
   - `python -m advisory.api.app --host 127.0.0.1 --port 8085`

## Layer 1: Extraction

### Raw data sources

The active extraction layer pulls from:

- Dhan
  - security master
  - daily OHLCV
  - intraday OHLCV
- Sharpely
  - financial statements
  - shareholding
  - historical market cap
  - peers and stock metadata
- macro sources
  - FRED and ISM
  - RBI
  - CPI, WPI, FPI
- exchange and market news
  - official exchange announcements
  - Economic Times RSS
- Screener.in
  - recurring production screeners
  - ad hoc research queries

### Normalized advisory inputs

The advisory stack then builds:

- `advisory_screener_constituents`
- `advisory_market_context_universe_daily`
- `advisory_market_context_summary_daily`
- `advisory_macro_daily`
- `advisory_macro_features_daily`
- `advisory_exchange_events`
- `advisory_exchange_features_daily`
- `advisory_fundamentals_daily`
- `advisory_technical_daily`
- `advisory_intraday_features_daily`
- `advisory_market_regime`

### Event extraction

Once a name is on the watch path:

- announcement and news rows are matched to watched symbols
- the LLM extracts a structured event tensor
- the tensor is persisted in `advisory_event_evaluations`

The event tensor includes fields such as:

- `event_class`
- `direction`
- `surprise`
- `novelty`
- `contradiction`
- `expected_decay_days`
- `source_reliability`
- affected peers and sectors

## Layer 2: Prediction

### Rule engine

The rule engine is still the primary prediction path today. It combines:

- screener membership
- regime and overlay
- technicals
- fundamentals
- optional intraday confirmation

It outputs scored candidate states such as:

- `PASS_NOW`
- `WATCH_BREAKOUT`
- `WATCH_PULLBACK`
- `WATCH_EVENT`
- `ABSTAIN`
- `REJECT`

### Event meta-model

The project also has a research-stage event meta-model. It is not the primary production signal path:

- training prep: `advisory.event_model_data_prep`
- training and scoring: `advisory.event_meta_model`

Current model inputs include:

- structured event-tensor features
- reviewer features
- anchor-day intraday response features

Current label target:

- sign-adjusted forward daily return

The model path is intentionally gated and disabled from the default cron loop. It only trains when explicitly run and when label coverage is sufficient. Event/news action in the live system should come from structured event interpretation, validated playbooks, macro/regime context, and technical timing.

### Experimental time-series forecasts

The project now has a separate OHLCV forecast-feature path:

- feature builder: `advisory.ts_forecast_features`
- destination table: `advisory_ts_forecasts_daily`
- evaluator: `advisory.ts_forecast_evaluator`
- workflow: `advisory.ts_forecast_workflow`
- evaluation tables: `advisory_ts_forecast_evaluations`, `advisory_ts_forecast_eval_summary`
- experimental watch table: `advisory_ts_forecast_watchlist`
- current adapter: `naive_momentum_v1`
- intended future adapters: TimesFM, Chronos, Moirai, or similar time-series foundation models

This layer consumes `dhan_ohlcv_daily` and emits expected return, forecast price, upside/downside quantiles, probability of positive return, volatility, momentum context, signal quality, and an `EXPERIMENTAL_*` hint.

The workflow can also use `config/ts_forecast_screeners.yaml` to fetch a fresh ad hoc Screener.in universe, refresh Dhan daily OHLCV for those symbols, and persist experimental positive names to `advisory_ts_forecast_watchlist`.

TimesFM dependencies are installed by default through `python builder.py`. Use `python builder.py --skip-timesfm-install` or `STOCKEY_SKIP_TIMESFM_INSTALL=true python builder.py` only for lightweight environments where the TS forecast workflow is not needed.

It is not part of live execution. Forecasts must first be evaluated through matured forecast checks, paper portfolios, and research-ledger comparisons against naive momentum, the technical engine, and existing action recommendations.

Technical threshold calibration is also research-only. `advisory.technical_threshold_calibration` compares technical threshold grids against realized Dhan OHLCV forward returns after costs and the operator app exposes the latest summary at `/technical-calibration` with copyable config JSON. `advisory.technical_threshold_promotion` can ask Codex/LLM for promotion rationale and a pending patch payload. Operator approval/rejection decisions are recorded in `advisory_technical_threshold_promotion_decisions`, but promotion into live setup thresholds remains a manual config change.

## Layer 3: Policy

### Adversarial review

Before risk sizing, the system runs deterministic event review in `advisory.adversarial_review`.

Before final action consolidation, `advisory.event_policy` maps structured event evaluations into bounded operator actions in `advisory_event_policy_actions`. Positive material classes such as `ORDER_WIN`, `RESULTS_POSITIVE`, `GROWTH_ACCELERATION`, `MARGIN_EXPANSION`, `PROMOTER_BUYING`, `PLEDGE_DOWN`, `BUYBACK`, and `GUIDANCE_UPGRADE` can become `BUY_WATCH` only when confidence, materiality, score impact, and risk checks are clean. Negative classes such as `REGULATORY_NOTICE`, `MANAGEMENT_RESIGNATION`, `AUDITOR_GOVERNANCE`, `RESULTS_NEGATIVE`, `PLEDGE_UP`, and `PROMOTER_SELLING` become `REDUCE_EXPOSURE_REVIEW` or `MANUAL_REVIEW`. Informational classes such as dividends, analyst meets, and neutral corporate actions stay `NO_ACTION` unless other evidence overrides them. Action consolidation consumes these as review/risk overlays only.

`MANUAL_REVIEW` is intentionally kept narrow. `advisory.event_policy` downgrades low-information rows to `NO_ACTION` before they reach the operator queue. For the remaining manual-review rows, Codex/LLM can add `operator_notes_json`, including possible action, future events to wait for, and operator questions. The same refinement can downgrade a row to `NO_ACTION` if the LLM sees no realistic path to action. The operator API suppresses already-downgraded `NO_ACTION` event-policy rows and suppresses generic action-consolidation shadow rows when a detailed event-policy review exists for the same event.

Operator-facing API fields should humanize event-policy rows. Persisted enum fields remain useful for deterministic rules and audit, but UI-facing summaries should say “regulatory or tax notice” instead of `REGULATORY_NOTICE`, and should explain the action boundary, for example “review risk first; this is not an automatic sell.”

This layer can:

- `clear`
- `penalize`
- `review_manual`
- `veto`

It is intentionally one-directional. It can block or haircut weak ideas, but it cannot invent bullish conviction on its own.

### Risk and portfolio

Policy is then applied in:

- `advisory.risk_engine`
- `advisory.portfolio_engine`

Those layers own:

- abstention
- sizing
- capital caps
- overlap caps
- setup-level caps
- final portfolio ranking

Prediction and policy are intentionally separate. A good event or candidate score does not directly imply an approved allocation.

## Layer 4: Execution support

The final support layer includes:

- `advisory.position_lifecycle`
- `advisory.action_recommender`
- `advisory.execution_engine`
- `advisory.decision_trace`
- `advisory.api.app`
- the Nuxt operator frontend in `apps/operator-web`

This layer is for:

- lifecycle state
- rebalance suggestions
- one consolidated action recommendation per symbol/date
- execution planning
- operator visibility

Every clean recommendation should carry a persisted reason contract explaining why the stock was screened, why it was selected, which event/playbook/technical/macro/risk evidence mattered, and why the final consolidated action won. Recommendations with incomplete reason contracts should be downgraded to manual review rather than presented as clean investable ideas.

Feature freshness is now both visible and safety-gated at final action consolidation. `advisory.feature_freshness` classifies core action inputs as fresh, stale, missing, error, or intentionally skipped. Consolidated action rows persist the decision-time freshness snapshot in `advisory_action_recommendations.feature_freshness_json`. The Action Queue shows required-input summaries, and Symbol Detail shows both current freshness and the persisted decision-time snapshot. Explicit stage dependencies are configured for `rules`, `risk`, `portfolio`, `lifecycle`, and `actions`; `advisory.pipeline` emits each preflight result under the stage’s `feature_gate`. Blocked `rules` gates move `PASS_NOW` candidates to `WATCH_EVENT` without hiding the candidate. Blocked `risk` gates move `allocated` rows to `review_manual` with zero suggested allocation. Blocked `portfolio` gates move `approved` / `trimmed` rows to `deferred` with zero approved capital. Blocked `lifecycle` gates annotate lifecycle/rebalance reasons and context while preserving exits and risk-reduction actions. If a final `BUY` or `BUY_MORE` winner has blocked required decision-time inputs, `advisory.action_recommender` downgrades it to `MANUAL_REVIEW`, marks broker execution as disallowed, and preserves the blocked original action plus feature blockers in raw context.

The operator trace UI now consumes normalized trace summaries from `advisory.api.app`. It renders event evaluation, adversarial review, investor playbook overlays, technical state, risk sizing, macro context, exchange-event context, portfolio allocation, lifecycle/exit policy, action consolidation, execution planning, execution safety, submission, and reconciliation as readable cards, while keeping raw payload details expandable for debugging.

The trace UI also supports client-side search, domain/status filters, and quick filters for problems, execution blockers, action changes, and event-driven changes. On the dedicated Decision Trace page, filter state is persisted in URL query params and processing stages, decision rows, and individual steps have copyable deep links.

The market-context stage writes `advisory_market_context_universe_daily` and `advisory_market_context_summary_daily`. It ranks the investable technical universe by market cap where available plus traded value, keeps the top 50%, and summarizes breadth, technical leadership, sector clusters, exchange events, news/announcement activity, and current regime. This is context for interpretation and gating, not a direct buy/sell signal.

Event evaluation payloads now include this broad-market snapshot as `broad_market_context`. Investor playbook action plans also persist `market_context_json` and `market_context_adjustment_json`; positive playbook actions are downgraded to manual watch/review when breadth or regime context is weak, while risk-reduction playbooks are reinforced. This still does not create broker-executable trades.

The continuous watch loop also uses the latest top-context universe as a lower-priority intake source for news and announcements. Active watchlist and open-position symbols win if a symbol appears in multiple sources. Top-context news/announcement rows are stored as `context_observed` unless a deterministic materiality filter sees high-impact terms such as order wins, results, rating actions, regulatory events, promoter/stake changes, buybacks, mergers, or governance issues. Only `triggered` event rows are routed into symbol refresh/evaluation.

## Continuous watch architecture

The live watch loop is separate from the batch advisory run.

It is built from:

- `advisory.sync_state`
- `advisory.continuous_watch`
- `advisory.event_router`
- `advisory.decision_trace`
- `advisory.api.app`
- the Nuxt operator frontend

The design is:

- incremental
- watchlist-scoped
- fail-soft
- cheap enough to run every few minutes

It does not:

- watch the entire market tick-by-tick
- rerun the full advisory stack on every update
- put the LLM in the raw polling loop

Instead it:

1. polls recent OHLCV only for active watchlist symbols
2. polls announcements and ET/news incrementally
3. raises live alerts such as `ENTRY_ZONE_HIT` and `INVALIDATION_HIT`
4. routes the highest-priority symbols into targeted reevaluation
5. records trace/action state for the operator API and Nuxt frontend

The old static `live_dashboard/` path is deprecated. The API still reuses parts of the historical payload builder for compatibility, but the operator target is now the Nuxt app.

## Screener model

The project now separates:

- production screeners
  - recurring
  - registered
  - synced into historical snapshots
- research queries
  - ad hoc
  - exploratory
  - only promoted into production when they survive validation

This keeps the production advisory universe deterministic while still allowing fast idea generation.

## Snapshot policy

The snapshot policy is intentionally simple:

- the screener date is the anchor date
- regime and overlay use the latest available row on or before that date
- technicals, fundamentals, and intraday features use the latest available row on or before that date
- setup freshness policies decide how stale each input is allowed to be

The system no longer forces the entire run back to one oldest common date across all datasets.

## Research discipline

The main research guardrails are:

- research ledger logging in `advisory_research_runs`
- point-in-time dataset construction
- explicit abstain state
- model training is optional research, not required for production event/playbook action
- separation of extraction, prediction, policy, and execution

The project is intentionally moving toward:

- stronger false-discovery control
- better regime modeling
- better event persistence modeling
- cleaner deterministic event/playbook policies before adding more tabular prediction

## Current gaps

The main remaining gaps are:

1. every recommendation needs a mandatory logical reason contract
2. top-50% market context universe and summaries need to be added
3. production investor playbook action plans need to be bridged into action consolidation as review/risk-overlay candidates
4. event-class policies need stronger deterministic mapping from known event types to watch/buy/review/reduce actions
5. macro/regime state needs to be a first-class gate for event-driven candidates
6. technical entry/exit timing needs tighter integration with event-driven candidate creation
7. operator trace UI needs broader coverage for any remaining low-value raw JSON payloads
8. continuous-watch routing can still be tightened further

Use [`todo.md`](../todo.md) for the current roadmap on these items.
