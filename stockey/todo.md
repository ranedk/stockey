# Investment Advisory Roadmap

Updated: `2026-04-10`

This file is the current roadmap for the live advisory stack. It is not a historical design log.

## Primary operator paths

- Full production flow: `./all_full_advisory.sh`
- Advisory only: `./all_advisory.sh`
- Model prep and training: `./all_model_training.sh`
- Continuous monitoring: `./all_continuous_watch.sh --loop`

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
- event-model data prep, train, and score scaffolding
- broad research-only training universes from ad hoc Screener queries
- continuous watch, live alerting, event routing, and a static HTML dashboard

## What is working now

1. Extraction
   - ingest raw data
   - build snapshots
   - extract structured event tensors from announcements and news

2. Prediction
   - rule engine produces scored candidate states
   - event meta-model train/score path exists
   - intraday confirmation features are available

3. Policy
   - abstain layer is explicit
   - adversarial review can penalize, force manual review, or veto
   - risk and portfolio layers apply caps, sizing, and overlap controls

4. Execution support
   - lifecycle and execution-planning outputs exist
   - continuous watch can trigger symbol-level reevaluation
   - live dashboard is generated as static HTML and JSON

## Current bottlenecks

1. Event-model label coverage is still too thin for a strong first fit.
2. Historical event coverage needs more point-in-time backfill.
3. Regime logic is still simpler than the intended long-run design.
4. NSE exchange-event features are now available, but still need dashboard/trace surfacing and optional setup-level rules.
5. Too much prediction still lives in hand-tuned rule logic.
6. Continuous watch can be tightened further with cooldowns and duplicate suppression.

## Active roadmap

### 1. Make the event-model dataset trainable

Priority:

- increase historical event evaluations across more mature dates
- ensure enough later `dhan_ohlcv_daily` rows exist to form labels
- keep the first real training target on `1d`

Done already:

- `advisory.event_model_data_prep`
- `advisory.training_universe`
- `EVENT_MODEL_TRAINING_V1`
- `all_model_training.sh`

Still needed:

- more matured labeled rows
- first statistically usable training run
- validation results recorded in the research ledger

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

### 5. Shift more prediction into tabular models

Target direction:

- keep LLMs in extraction and adversarial review
- move more ranking logic into tabular predictors
- use rule logic more for guardrails and policy than for raw alpha

Likely next slices:

- learned reviewer or meta-reviewer
- richer event-memory and decay features
- more model-driven setup ranking

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
