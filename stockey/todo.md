# Investment Advisory Roadmap

Updated: `2026-04-10`

This file is the current roadmap for the live advisory stack. It is not a historical design log.

## Primary operator paths

- Downloaders only: `./all_downloaders.sh`
- Parsers only: `./all_parsers.sh`
- Download + parse: `./complete_data.sh`
- Advisory only: `./all_advisory.sh`
- Fast advisory refresh: `./all_advisory.sh --fast`
- Model prep and training: `./all_ml.sh`
- Continuous monitoring: `./all_watchers.sh --loop`

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
- `all_ml.sh`

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

### 5. Add thesis buckets, horizon policy, and exit-event policy

Problem:

- `advisory_portfolio_orders` currently tells us allocation, priority, and invalidation, but not the intended holding contract.
- Some ideas are target-driven, some are explicitly time-window trades, and some should stay live only while the data continues to support them.
- The dashboard should show not just what was selected, but why it belongs in a given portfolio bucket and what would exit it.

Target:

- divide portfolio ideas into three thesis buckets:
  - `TARGET`
  - `TIME_HORIZON`
  - `DATA_DEPENDENT`
- add explicit exit-event rules that override target/time bucket behavior
- surface bucket rationale, screener provenance, and exit conditions in the dashboard and lifecycle views

Policy model:

- target bucket:
  - valuation or rerating driven
  - fields: `target_price`, `target_basis`, `target_confidence`, `target_review_date`
- time-horizon bucket:
  - defined event/trade window
  - fields: `expected_horizon_days`, `horizon_type`, `horizon_end_date`, `horizon_basis`
- data-dependent bucket:
  - thesis remains live while evidence remains supportive
  - fields: `continue_while`, `key_monitor_fields`, `recheck_frequency`
- exit-event layer:
  - invalidation hit
  - stop hit
  - thesis contradiction
  - adverse insider / exchange-event cluster
  - regime deterioration
  - liquidity breakdown

Implementation slices:

1. Add bucket and exit-policy columns to `advisory_portfolio_orders`.

### 5A. Add consolidated action recommendations

Problem:

- The system now has portfolio rows, lifecycle rows, rebalance rows, and execution rows.
- Even though portfolio/lifecycle snapshots are mostly deduped to one row per symbol, operator intent is still spread across multiple tables.
- The execution engine still reasons from separate portfolio and rebalance paths instead of one explicit action contract.
- The dashboard can show multiple adjacent concepts for the same stock, which is noisy when the operator just needs the single current action.

Target:

- create one point-in-time table, `advisory_action_recommendations`
- enforce one winning action per `asof_date + symbol`
- make this the single contract for:
  - operator dashboard
  - trading API handoff
  - execution dry-runs

Canonical action set:

- `BUY`
- `BUY_MORE`
- `PARTIAL_SELL`
- `SELL`
- `TIGHTEN_STOP`
- `MANUAL_REVIEW`
- `HOLD`
- `WATCH`

Priority model:

- `SELL` outranks everything
- `PARTIAL_SELL` outranks `BUY_MORE`
- `MANUAL_REVIEW` outranks `BUY` and `WATCH`
- `TIGHTEN_STOP` is lower than liquidation actions but higher than passive `HOLD`
- `BUY_MORE` outranks `BUY`
- `BUY` outranks `WATCH`

Source mapping:

- `advisory_rebalance_actions`
  - `exit_*` -> `SELL`
  - `trim_winner` -> `PARTIAL_SELL`
  - `add_on_pullback` -> `BUY_MORE`
  - `tighten_stop` -> `TIGHTEN_STOP`
  - `review_*` / `review_manual` -> `MANUAL_REVIEW`
- `advisory_portfolio_orders`
  - approved live recommendation -> `BUY`
- `advisory_position_lifecycle`
  - open with no stronger action -> `HOLD`
- `advisory_watchlist`
  - active non-abstain watch row -> `WATCH`

Execution integration:

- `advisory.execution_engine` should plan only from `advisory_action_recommendations`
- only broker-submittable actions should become order intents:
  - `BUY`
  - `BUY_MORE`
  - `PARTIAL_SELL`
  - `SELL`
- review-only actions must remain visible but non-submittable

Dashboard integration:

- show one current `Action Recommendation` per stock
- explain:
  - action code
  - action priority
  - winning reason
  - winning source table
  - action fraction if any
  - execution mode

Implementation slices:

1. Add `advisory/action_recommender.py`
2. Persist `advisory_action_recommendations`
3. Wire `actions` stage into `advisory.pipeline`
4. Make `advisory.execution_engine` read the consolidated action table
5. Make the dashboard/operator view prefer the consolidated action over raw rebalance rows

Do not:

- let multiple active actions survive for the same symbol on the same advisory date
- allow both a plain `BUY` and a `BUY_MORE` execution plan for the same symbol in the same run
- hide manual-review conflicts; surface them explicitly as the winning action

### 6. Build a proper swing technical buy/exit engine

Problem:

- Current technical usage is useful but still too mixed between generic rule checks, intraday confirmation, and event/risk overlays.
- We need one explicit swing technical engine for Indian equities that answers:
  - is the stock in a tradable uptrend?
  - is it forming a constructive structure?
  - is there accumulation and leadership?
  - is it executable with sensible risk?
  - has the setup triggered?
  - has the technical thesis broken?
- This is not an intraday trading engine.

Target:

- create a dedicated technical state engine that outputs daily:
  - pre-entry:
    - `REJECT`
    - `IGNORE`
    - `WATCHLIST`
    - `NEAR_PIVOT`
    - `READY`
    - `BUY_TRIGGERED`
  - post-entry:
    - `HOLD`
    - `ADD_ON_PULLBACK`
    - `PARTIAL_EXIT`
    - `FULL_EXIT`
    - `EMERGENCY_EXIT`
- make the engine explicitly prefer:
  - liquid Indian equities
  - structurally strong names
  - breakout and trend-continuation setups
  - clean execution and realistic stop placement

Design rules:

- do not build this around standalone RSI / MACD / stochastic signals
- do not turn Elliott Wave, candlestick patterns, or Fibonacci into primary buy logic
- use indicators as tools, not as the edge
- keep the engine swing-oriented and based on trend, structure, participation, leadership, and tradability

Hard pre-score filters:

- reject before scoring when any of these fail:
  - minimum average daily traded value
  - minimum price
  - minimum median volume
  - maximum spread percent
  - maximum recent gap frequency
  - excessive recent circuit behavior
  - obvious event hazard for short swing holds
  - highly erratic chart with no clean structure
- use config-driven thresholds, not hardcoded literals inside scoring code

Technical score model:

- total score: `100`
- trend regime score: `25`
  - price vs 20 / 50 / 150 DMA
  - slope of moving averages
  - higher-high / higher-low behavior
  - distance from 52-week high
  - 1m / 3m / 6m trend persistence
- structure quality score: `30`
  - flat base / tight range / VCP-like contraction / ascending base / breakout shelf / constructive pullback
  - base duration and depth
  - volatility contraction
  - support respect
  - pivot clarity
  - overhead supply estimate
- participation / volume score: `20`
  - breakout-day volume vs 20-day average
  - accumulation vs distribution balance
  - dry-up on pullbacks and contractions
  - retest behavior
- relative strength score: `15`
  - 1m / 3m / 6m performance vs benchmark
  - sector-relative performance
  - RS improvement before breakout
  - resilience during market weakness
- tradability / risk score: `10`
  - ATR percent
  - spread percent
  - average traded value
  - gap risk
  - circuit frequency
  - realistic stop distance

Suggested V1 thresholds:

- minimum gate before `READY` / `BUY_TRIGGERED`:
  - trend `>= 15/25`
  - structure `>= 18/30`
  - participation `>= 10/20`
  - relative strength `>= 8/15`
  - tradability `>= 6/10`
- state guidance:
  - total `< 55`: `IGNORE` or `REJECT`
  - total `55-69`: `WATCHLIST`
  - total `70-77`: `READY`
  - total `>= 78`: trigger-eligible, but only `BUY_TRIGGERED` after valid entry confirmation

Entry trigger archetypes:

- breakout entry:
  - close above valid pivot
  - meaningful breakout width
  - strong close
  - breakout volume confirmation
- breakout-retest entry:
  - breakout already happened
  - muted selling into retest
  - support hold / reversal confirmation
- trend-pullback entry:
  - controlled pullback into 20 / 50 DMA or prior support shelf
  - volume contraction
  - reversal / hold confirmation
- reclaim entry:
  - reclaim after false breakdown / failed breakout trap
  - strong close and preferably volume support

Post-entry exit framework:

- full exit:
  - pivot failure
  - decisive support break
  - structure invalidation
  - leadership collapse
  - regime deterioration with broken structure
- partial exit:
  - sharp extension away from 20 DMA / base
  - blow-off behavior
  - event-risk reduction
  - reward/risk deterioration without thesis break
- trailing exit:
  - aggressive: below recent swing support
  - balanced: below 20 DMA / pivot support
  - slower: below 50 DMA
  - optional ATR-based trail
- abnormal distribution exit:
  - clustered high-volume down days
  - repeated failed rebounds
  - deteriorating RS
- time-stop:
  - breakout triggered but no follow-through within configurable bars
- emergency exit:
  - severe gap / event shock / execution-risk breach

Conviction and sizing guidance:

- conviction bucket from technical score:
  - `LOW_CONVICTION`: `70-75`
  - `MEDIUM_CONVICTION`: `76-84`
  - `HIGH_CONVICTION`: `85+`
- do not size from score alone
- final sizing must still respect:
  - stop distance
  - liquidity
  - event-risk proximity

Context tags:

- allow only low-weight tags:
  - trend maturity early / middle / late
  - climax extension risk
  - broad market favorable / neutral / hostile
  - earnings proximity
  - sector momentum strong / weak
- these are tags, not primary buy logic

Implementation direction:

- add a dedicated module, for example `advisory/technical_engine.py`
- keep raw daily feature generation in `advisory/technical_features.py`
- do not move this into `advisory/intraday_features.py`
- intraday should stay secondary and timing-oriented, not primary swing logic

Required data/model additions:

- extend daily technical feature set with:
  - 52-week high distance
  - 20 / 50 / 150 DMA slope features
  - base duration / depth
  - range contraction measures
  - support-touch / pivot clarity metrics
  - accumulation / distribution day counts
  - breakout-volume ratios
  - gap frequency
  - circuit frequency
  - spread / tradability metrics where available
  - benchmark and sector-relative performance windows
- add explicit technical state outputs to the advisory layer

Pipeline integration points:

- `advisory.technical_features`:
  - extend feature generation for swing-structure fields
- new `advisory.technical_engine`:
  - compute technical sub-scores, technical state, entry type, invalidation candidate, and exit state
- `advisory.rule_engine`:
  - consume technical state instead of only generic technical rule fragments
  - use `WATCHLIST`, `NEAR_PIVOT`, `READY`, `BUY_TRIGGERED` directly
- `advisory.watchlist_builder`:
  - preserve technical engine state, pivot, support, and trigger archetype
- `advisory.position_lifecycle`:
  - map technical post-entry states to `HOLD`, `ADD_ON_PULLBACK`, `PARTIAL_EXIT`, `FULL_EXIT`, `EMERGENCY_EXIT`
- `advisory.risk_engine`:
  - combine conviction with liquidity and stop realism
- `advisory.live_dashboard`:
  - show technical state, trigger archetype, structure commentary, and exit reason cleanly

Implementation slices:

1. Add missing swing feature fields in `advisory.technical_features`.
2. Add config block for hard filters and technical thresholds.
3. Add `advisory.technical_engine` scoring buckets and state machine.
4. Add `BUY_TRIGGERED` trigger logic for breakout / retest / pullback / reclaim.
5. Add post-entry technical exit states and map them into lifecycle.
6. Replace scattered technical-only rule fragments in `advisory.rule_engine` with the explicit technical engine output.
7. Surface technical state, pivot, trigger type, and exit condition in dashboard and traces.
8. Add regression tests for:
   - clean breakout
   - near-pivot base
   - loose / junk structure reject
   - failed breakout full exit
   - sharp extension partial exit
   - dead-money time stop

Do not:

- let intraday confirmation become the primary swing buy engine
- make oscillator crosses the core logic
- overfit V1 with too many market-regime branches
- mix technical trigger confirmation with loose event-driven overrides in a way that hides the actual setup quality

### 4A. Rebuild exit policy, stop-loss policy, and profit-booking policy

Problem:

- current lifecycle logic is too shallow
- `stop_price` and `invalidation_price` are effectively the same number
- `trim_winner` and `tighten_stop` exist mostly as labels, not as a real management framework
- post-entry technical states exist in `advisory.technical_engine`, but lifecycle does not use them yet
- execution only handles full `exit_*` actions and does not translate partial exits into broker behavior

Target:

- separate execution stop from thesis invalidation
- route lifecycle through `technical_engine.evaluate_post_entry_state(...)`
- add real profit-booking and stop-tightening rules
- persist explicit exit-policy metadata and action semantics
- allow Dhan execution planning to handle:
  - full exits
  - partial exits
  - stop-based exits

Required semantics:

- `invalidation_price`
  - thesis-level break
  - wider, slower, structural
  - if broken, the original setup is wrong
- `stop_price`
  - execution/risk-control stop
  - tighter than invalidation where possible
  - can be tightened as the trade matures
- `partial exit`
  - reduce risk or lock profit without killing the whole position
- `full exit`
  - stop hit, invalidation hit, technical failure, emergency gap/event damage
- `time stop`
  - setup-family aware
  - should not be one flat `20 day` rule

Implementation slices:

1. Make `risk_engine.compute_invalidation(...)` return distinct `stop_price` and `invalidation_price`.
2. In `position_lifecycle`, load latest technical context and use `evaluate_post_entry_state(...)`.
3. Map post-entry technical states into:
   - `exit_emergency`
   - `exit_technical_failure`
   - `trim_winner`
   - `add_on_pullback`
   - `hold`
4. Keep stop/invalidation exits as highest-priority overrides.
5. Replace the flat stale-review rule with bucket/setup-aware time-stop logic.
6. Add action metadata for exit sizing:
   - full exit
   - partial exit fraction
   - no-execution review-only action
7. Update `execution_engine` so Dhan execution supports:
   - full `SELL`
   - partial `SELL`
   - skip non-executable review-only actions
8. Surface stop, invalidation, technical exit reason, and partial-exit plan in dashboard/lifecycle views.

Current implementation status:

- lifecycle now recomputes a deterministic management plan per open position:
  - target price
  - expected holding days
  - horizon end date
  - target review date
  - recommended stop
- target-hit positions become `trim_winner`
- time-horizon losers after horizon expiry become `exit_time_stop`
- action recommendations and execution planning understand the time-stop full-exit path
- `all_advisory.sh --fast` exists for quick refreshes without slow watch/news/repair work

Remaining:

- calibrate target multiples and horizon defaults against realized trade outcomes
- add optional low-worker parallel execution for pure DB/CPU stages only
- do not parallelize Chrome/browser-connected scraping in the same browser session

Do not:

- treat stop and invalidation as synonyms
- trigger partial exits only from arbitrary PnL thresholds without technical context
- let execution auto-submit ambiguous review actions
- let technical post-entry logic live separately from lifecycle
2. Add lifecycle fields so exit-event triggers and bucket state are visible in `advisory_position_lifecycle`.
3. Add first-pass deterministic bucket classification using setup family, holding horizon note, event context, and invalidation guidance.
4. Show bucket, bucket reason, exit-policy summary, and screener provenance in the live dashboard.
5. Later: allow richer target-price and horizon inputs from valuation/event models instead of only deterministic defaults.

Do not:

- force all ideas into target-price logic
- let bucket type replace invalidation/stop/event exits
- let the LLM invent unconstrained bucket policy without deterministic fields persisted downstream
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
