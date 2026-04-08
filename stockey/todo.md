# Investment Advisory Roadmap

Updated: `2026-04-08`

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
- macro snapshots, base regime classification, and a lightweight news overlay
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
4. Too much prediction still lives in hand-tuned rule logic.
5. Continuous watch can be tightened further with cooldowns and duplicate suppression.

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

### 3. Shift more prediction into tabular models

Target direction:

- keep LLMs in extraction and adversarial review
- move more ranking logic into tabular predictors
- use rule logic more for guardrails and policy than for raw alpha

Likely next slices:

- learned reviewer or meta-reviewer
- richer event-memory and decay features
- more model-driven setup ranking

### 4. Strengthen event memory

Target direction:

- store event persistence and decay by event type, sector, and regime
- separate surprise, credibility, and persistence instead of using one flat event score

This should support:

- better forward-return labels
- better reviewer features
- less prompt-driven heuristics

### 5. Tighten the continuous watch loop

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

### 6. Keep research discipline strict

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
