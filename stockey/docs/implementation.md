# Advisory Architecture

This document describes the current architecture of the live investment advisory system.

It is not a future-state PRD. Use [`todo.md`](../todo.md) for the active roadmap.

## Design intent

The project is built around four layers:

1. extraction
2. prediction
3. policy
4. execution support

The main design constraint is that LLMs help with extraction and review, but they do not get final trading authority.

## Runtime flows

The repository has three primary runtime modes:

1. batch advisory
   - `./all_full_advisory.sh`
   - `./all_advisory.sh`
2. model research and training
   - `./all_model_training.sh`
3. continuous watch
   - `./all_continuous_watch.sh --loop`

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

The project also has a research-stage event meta-model:

- training prep: `advisory.event_model_data_prep`
- training and scoring: `advisory.event_meta_model`

Current model inputs include:

- structured event-tensor features
- reviewer features
- anchor-day intraday response features

Current label target:

- sign-adjusted forward daily return

The model path is intentionally gated. It only trains when label coverage is sufficient.

## Layer 3: Policy

### Adversarial review

Before risk sizing, the system runs deterministic event review in `advisory.adversarial_review`.

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
- `advisory.execution_engine`
- traces and dashboards

This layer is for:

- lifecycle state
- rebalance suggestions
- execution planning
- operator visibility

## Continuous watch architecture

The live watch loop is separate from the batch advisory run.

It is built from:

- `advisory.sync_state`
- `advisory.continuous_watch`
- `advisory.event_router`
- `advisory.live_dashboard`

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
5. rewrites a static HTML and JSON dashboard

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
- gated model training based on label coverage
- separation of extraction, prediction, policy, and execution

The project is intentionally moving toward:

- stronger false-discovery control
- better regime modeling
- better event persistence modeling
- more tabular prediction and less threshold sprawl

## Current gaps

The main remaining gaps are:

1. insufficient matured event labels for a strong first event-model fit
2. regime stack is still simpler than the intended long-run design
3. too much prediction still lives inside hand-tuned rule logic
4. continuous-watch routing can still be tightened further

Use [`todo.md`](../todo.md) for the current roadmap on these items.
