# Investment Advisory TODO

This file tracks the next advisory redesign as of 2026-03-26.

Primary operator path:

- `./all_advisory.sh`
- `python -m advisory.master_pipeline`

Use `advisory.pipeline` only for component-level advisory runs and debugging.

The current system already has:

- screener normalization into `advisory_screener_constituents`
- macro snapshot and base regime classification
- technical and fundamental snapshots
- rule engine, watchlist, announcement watch, and ET RSS watch
- LLM event evaluation
- risk, portfolio, lifecycle, and trace/dashboard tools
- authenticated ad hoc Screener.in raw query support with persisted company-level results

The next weakness is not missing infrastructure. It is research discipline and model validation:

- too much logic is still configuration- and threshold-driven without strong validation controls
- point-in-time discipline needs to be treated as a first-class constraint, not a cleanup step
- there is no formal abstain layer, so the system can still over-opine
- LLM work is useful, but it still risks drifting from extraction into decision-making
- intraday features exist, but they are not yet part of a properly validated prediction layer
- there is no research ledger that records what was tried and what failed

## Objective

Refocus the advisory system into four clean layers:

1. extraction
2. prediction
3. policy
4. execution

Use LLMs mainly in extraction and adversarial review. Keep the alpha engine mostly tabular and statistically validated.

Concretely:

- use announcements and news to build structured event tensors
- use regime models and change-point detectors to describe market state
- use XGBoost or similar tabular models for ranking or edge prediction
- use a separate policy layer for abstain, sizing, caps, and execution safety
- keep ad hoc Screener.in queries as research tools, not autonomous production mutation

## Scope

In scope:

- research ledger and leakage-resistant evaluation
- structured event extraction from text
- abstain / do-nothing class and turnover control
- tabular prediction and ranking
- regime stack with shock detection
- policy separation from prediction and execution
- event memory and decay modeling

Out of scope:

- new vendors
- broker/feed expansion
- LLM-led discretionary trading
- free-form multi-agent debate systems
- execution authority for LLMs
- backtest theater without leakage control

## Current Reality

Already implemented from the previous redesign:

- ranked setup scoring
- reduced hard rejects
- watch states and entry-zone metadata
- event taxonomy and state-transition hints
- aggregated event effect handling in watch, risk, and portfolio
- improved symbol/setup trace visibility

Main gaps now:

- there is no research ledger or false-discovery control
- abstention is still too informal
- structured event extraction is richer now, but it is not yet fed into a tabular prediction layer
- regime handling should evolve from simple labels into a stack of shock detection plus persistent regime estimates
- prediction and policy are still too entangled in the rule logic
- the adversarial review layer is deterministic today and should later become a small tabular reviewer or meta-model

Recently implemented:

- `advisory.news_theme_engine` recommends Screener.in query text from active news themes
- theme-linked Screener.in URLs can be registered back into the project
- `EVENT_OPPORTUNITY_V1` can consume registered theme screeners
- `data.screenerin.ad_hoc_query` can run authenticated one-off Screener.in raw queries and persist company-level rows
- recurring downloader flow no longer seeds default Screener.in screeners automatically; registered screeners are now the production-only recurring path
- the old default-screener bootstrap path and the older fallback theme config have been removed
- `advisory.intraday_features` can now pull missing Dhan intraday bars on demand and persist daily intraday pattern features for advisory use
- the intraday layer now supports multi-interval storage/builds (`1`, `5`, `15`, `25`, `60` minutes); model scoring is still a follow-on phase
- `advisory.research_ledger` can now record experiment configs, as-of dates, validation protocol, and result metrics from `advisory.pipeline` and `advisory.master_pipeline`
- the advisory stack now has a formal abstain layer via `candidate_state=ABSTAIN` and `allocation_status=abstained`
- `advisory.llm_event_evaluator` now emits a richer event tensor with direction, surprise, novelty, contradiction, expected decay, source reliability, and affected peers/sectors while keeping `event_class` and state transitions stable for policy
- `advisory.adversarial_review` now scores those event tensors into `clear`, `penalize`, `review_manual`, or `veto` before risk sizing
- `advisory.event_meta_model` now provides an explicit train/score scaffold for an XGBoost event meta-model using sign-adjusted forward-return labels from `dhan_ohlcv_daily`

Current model-training blocker:

- there are still too few matured labeled event rows for a statistically useful first fit
- immediate priority is more historical event evaluations plus fresh daily OHLCV for those symbols, not more model complexity

## Additional Objective: Intraday Pattern Layer

Build a controlled intraday confirmation layer where:

- daily regime/setup logic remains the base
- intraday history is pulled only when the current screener universe needs it
- the project stores that history and derived daily pattern features
- setups can optionally use those intraday features through `intraday_rules`
- later model work such as XGBoost uses the persisted feature history instead of raw bars directly

This should be:

- on-demand
- persistent
- feature-first
- compatible with the existing rule engine

It should not be:

- a full intraday trading engine
- an always-sync-5-years-for-all-symbols job
- a model-first path without durable feature history

## Additional Objective: News-Led Opportunity Discovery

Build a controlled theme-discovery layer where:

- base regime + regular screeners still run daily
- major news themes can create additional discovery paths
- those paths emit candidate Screener.in queries in operator-friendly format
- the operator can create the screener on Screener.in, paste the link back, and resume ingestion

This should be:

- taxonomy-based
- explainable
- human-in-the-loop

It should not be:

- fully automatic headline trading
- unconstrained LLM-generated screener sprawl

## Reset Priority: Validation Over Orchestration

The next phase should de-emphasize agent orchestration and LLM-led workflow design.

Primary priorities:

- false-discovery control and a research ledger for every config tried
- point-in-time discipline everywhere
- formal abstention and turnover penalties
- LLMs as structured event extractors and adversarial reviewers
- tabular prediction with XGBoost or similar
- clean separation of extraction -> prediction -> policy -> execution

Deprioritized:

- multi-agent debate systems
- LLM-led trade selection
- adding more agent-routing logic to the production pipeline
- letting research automation mutate production screeners by itself

## Product Model: News Theme Engine

### Theme taxonomy

Introduce a stable theme layer such as:

- `PHARMA_EXPORT_SHIFT`
- `ENERGY_SUPPLY_SHOCK`
- `AGRI_SUPPLY_SHOCK_COTTON`
- `DEFENSE_TENSION`
- `POWER_POLICY_SHIFT`
- `METAL_SUPPLY_SHOCK`
- `TEXTILE_EXPORT_SHIFT`
- `CHEMICAL_INPUT_SHOCK`

Each theme should have:

- trigger keywords / classification hints
- positive sectors
- negative sectors or cost-burdened sectors where relevant
- one or more suggested Screener.in query templates
- expected holding horizon
- expiry/decay note

### Operator workflow

1. Run a news theme command
2. See active theme recommendations
3. Copy the generated Screener.in query text
4. Create the screener manually in Screener.in
5. Paste the Screener.in URL back into the project
6. Register and ingest it
7. Use it in the event-opportunity setup flow

### Acceptance

- one command can tell the operator which screener to create
- output is in Screener.in query text form, not only a theme label
- the generated screener recommendation includes a suggested slug/name
- the project can resume once the operator provides the Screener.in URL

## Delivery Plan

### Phase 1: Multi-Screener Setup Mapping

Goal:

- let setups consume multiple screeners deterministically

Tasks:

1. Update [`config/advisory_setups.yaml`](/home/rane/code/stockey/config/advisory_setups.yaml)
   - replace single-screener assumptions with:
     - `screeners`
     - `screener_mode`
     - `overlay_screeners`
     - `allowed_overlays`
     - `blocked_overlays`
2. Update [`advisory/setup_registry.py`](/home/rane/code/stockey/advisory/setup_registry.py)
   - normalize old and new setup config shapes
   - keep backward compatibility where practical
3. Update [`advisory/rule_engine.py`](/home/rane/code/stockey/advisory/rule_engine.py)
   - resolve active screeners per setup
   - support `union` and `intersection`
   - preserve source screener provenance

Done when:

- at least one setup runs against more than one screener
- trace output shows which screener pool was active
- existing setups do not break

### Phase 1B: Intraday Feature Foundation

Goal:

- use Dhan intraday data as an advisory confirmation layer without turning the stack into an intraday system

Tasks:

1. Add [`advisory/intraday_features.py`](/home/rane/code/stockey/advisory/intraday_features.py)
   - sync missing intraday history on demand for the current screener universe
   - persist derived daily intraday pattern features
2. Update [`advisory/pipeline.py`](/home/rane/code/stockey/advisory/pipeline.py)
   - add a dedicated `intraday` stage before `rules`
3. Update [`advisory/rule_engine.py`](/home/rane/code/stockey/advisory/rule_engine.py)
   - merge the latest intraday feature snapshot into candidate evaluation
   - support `intraday_rules` in setup config
4. Update [`config/advisory_setups.yaml`](/home/rane/code/stockey/config/advisory_setups.yaml)
   - add light intraday confirmation rules for event and breakout-style setups

Done when:

- the advisory run can build `advisory_intraday_features_daily`
- setup scoring can reference intraday fields without breaking existing runs
- intraday sync is on-demand instead of a blanket full-history job

Next phase:

1. Create a labeled intraday breakout dataset from `advisory_intraday_features_daily`
2. Train an `xgboost` breakout-confirmation model on persisted features rather than raw bars
3. Persist `model_name` and `model_score` back into the intraday feature table
4. Feed the calibrated model score into setup scoring as a soft input, not a hard gate

### Phase 2: Add Market News Overlay

Goal:

- classify one daily overlay from ET RSS and existing regime context

Tasks:

1. Add [`advisory/news_overlay_engine.py`](/home/rane/code/stockey/advisory/news_overlay_engine.py)
   - read recent ET RSS / matched news context
   - combine with existing regime flags
   - write:
     - `asof_date`
     - `base_regime`
     - `overlay_name`
     - `overlay_intensity`
     - `overlay_reason`
     - `source_count`
2. Keep [`advisory/regime_engine.py`](/home/rane/code/stockey/advisory/regime_engine.py) unchanged in principle
   - it remains the stable base layer

Done when:

- one overlay row is available per date
- `NONE` is valid
- dashboard and traces can display the overlay

### Phase 3: Overlay-Aware Setup Activation

Goal:

- use base regime plus overlay to decide whether setups run and which screeners they use

Tasks:

1. Extend [`advisory/rule_engine.py`](/home/rane/code/stockey/advisory/rule_engine.py)
   - apply:
     - `allowed_regimes`
     - `blocked_regimes`
     - `allowed_overlays`
     - `blocked_overlays`
   - reject with:
     - `regime_not_allowed`
     - `overlay_not_allowed`
   - add overlay-driven screener expansion/removal
2. Preserve candidate metadata:
   - `source_screener_slug`
   - `source_screener_list`
   - `base_regime`
   - `news_overlay`

Done when:

- setup activation clearly depends on both layers
- overlay can add screeners without code changes
- inclusion and rejection reasons are traceable

### Phase 4: Diagnostics and Downstream Provenance

Goal:

- show setup activation path clearly

Tasks:

1. Update [`advisory/watchlist_builder.py`](/home/rane/code/stockey/advisory/watchlist_builder.py)
   - preserve screener and overlay provenance
2. Update [`advisory/setup_trace.py`](/home/rane/code/stockey/advisory/setup_trace.py)
   - show:
     - base regime
     - active overlay
     - overlay reason
     - active screeners
     - candidate counts by screener
3. Update [`advisory/dashboard.py`](/home/rane/code/stockey/advisory/dashboard.py)
   - expose:
     - regime
     - overlay
     - active screeners
     - top rejection reason

Done when:

- one command can explain why a setup ran
- one command can explain which screener pool drove candidate generation

### Phase 5: News Theme to Screener Workflow

Goal:

- turn major news themes into operator-created Screener.in screeners

Tasks:

1. Add [`advisory/news_theme_engine.py`](/home/rane/code/stockey/advisory/news_theme_engine.py)
   - read recent market news
   - classify one or more stable opportunity themes
   - generate:
     - `theme_id`
     - `theme_reason`
     - `theme_intensity`
     - suggested Screener.in query text
     - suggested screener slug/name
2. Add a config file for theme mappings and screener query templates
   - keep templates deterministic and editable
3. Add a small operator workflow command
   - print:
     - active theme
     - rationale
     - Screener.in query text to paste
     - next step instructions asking for the Screener.in URL
4. Extend screener registry usage
   - once the operator provides the URL, register it using the existing Screener.in registry flow
5. Add an event-opportunity setup family later
   - optional next step after manual screener creation is working

Done when:

- the system can recommend a concrete Screener.in screener definition from news
- the operator can create it manually and feed the URL back into the system
- the recommendation path is separate from the normal daily regime/setup path

## Minimum Schema Changes

### New table

`advisory_market_overlay_daily`

- `asof_date`
- `base_regime`
- `overlay_name`
- `overlay_intensity`
- `overlay_reason`
- `source_count`
- `load_ts`

### `advisory_candidates`

- `source_screener_slug`
- `source_screener_list`
- `base_regime`
- `news_overlay`

### `advisory_watchlist`

- `source_screener_slug`
- `source_screener_list`
- `base_regime`
- `news_overlay`

## Operating Workflow

1. Run:

```sh
/home/rane/code/stockey/.xstockey/bin/python -m advisory.news_overlay_engine --dry-run
```

2. Run:

```sh
/home/rane/code/stockey/.xstockey/bin/python -m advisory.rule_engine --dry-run
```

3. Inspect:

```sh
/home/rane/code/stockey/.xstockey/bin/python -m advisory.setup_trace <SETUP_ID> --format text
```

4. For major event/news themes, run:

```sh
/home/rane/code/stockey/.xstockey/bin/python -m advisory.news_theme_engine --dry-run
```

5. Copy the suggested Screener.in query, create the screener manually, then continue by registering the URL:

```sh
/home/rane/code/stockey/.xstockey/bin/python -m data.screenerin.screener_registry add <SCREENER_URL>
```

6. For an authenticated ad hoc fundamental query outside the registered setup flow, run:

```sh
/home/rane/code/stockey/.xstockey/bin/python -m data.screenerin.ad_hoc_query --name "Deep Value ROCE" --query "Market capitalization > 500 AND Price to earning < 15 AND Return on capital employed > 22%"
```

This assumes Chrome remote debugging is already running. If Screener.in redirects to login, the command blocks in the terminal until login is completed. Results are stored in:

- `screenerin_ad_hoc_query_runs`
- `screenerin_ad_hoc_query_results`

4. Check:

- active regime
- active overlay
- active screeners
- candidate counts by screener
- top rejection reasons

## Blunt Rule

Do not build a new “regime” every time the market narrative changes.

Build:

- one stable base regime
- one lightweight market/news overlay
- one flexible multi-screener setup mapping layer

That is the right level of reactivity for this stack.
