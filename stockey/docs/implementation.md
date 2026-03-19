# Stock Investment Analysis Pipeline — Implementation Spec

## 1) Goal

Build a daily/periodic pipeline that narrows a broad stock universe into a ranked actionable list by combining:

1. hard filters for junk removal,
2. fundamental and structural scoring,
3. technical setup detection,
4. selective event/news/announcement understanding using LLMs only on a small candidate set,
5. final ranking and optional portfolio/trade signals.

This is **not** a pure prediction system. It is a **multi-stage decision pipeline** where cheap deterministic filters run first, and expensive ML/LLM analysis runs last on a reduced universe.

---

## 2) High-level design principles

### 2.1 Stage-gated architecture

Do not run all models on all stocks.

Pipeline should be:

* **Stage A:** Universe definition
* **Stage B:** Hard exclusion filters
* **Stage C:** Fundamental + structural scoring
* **Stage D:** Technical setup scoring
* **Stage E:** Candidate selection for event analysis
* **Stage F:** LLM/event/news scoring
* **Stage G:** Final ranking + outputs

### 2.2 Cheap before expensive

Order of cost:

1. SQL/stat rules
2. feature engineering
3. simple statistical/ML models
4. LLM-based extraction/summarization

### 2.3 Separate “facts” from “opinions”

Store:

* raw source data,
* engineered features,
* model outputs,
* LLM-extracted structured event facts,
* final scores.

Do not mix them in one table.

### 2.4 Backtestable where possible

LLM/event layer will be hardest to backtest reliably. Therefore:

* stages A–D must be fully backtestable,
* stage E/F should produce structured outputs that can later be evaluated,
* keep raw event timestamps and extracted labels.

---

# 3) Scope

## 3.1 Supported instruments

Initial v1:

* NSE/BSE listed equities
* optional split between:

  * large cap
  * mid cap
  * small cap
  * SME/microcap as separate regime

Do **not** mix SME and liquid large cap names under one identical rule-set.

## 3.2 Time horizons

Support at least two analysis horizons:

1. **Swing / positional:** 2 weeks to 6 months
2. **Investment / medium term:** 3 to 18 months

Technical features may differ by horizon, but pipeline can share most stages.

---

# 4) Inputs / data sources

## 4.1 Price and volume data

Per symbol, daily OHLCV:

* open
* high
* low
* close
* adjusted close if possible
* volume
* delivery volume / traded quantity if available
* number of trades if available

## 4.2 Market structure / tradeability data

* average daily traded value
* bid-ask spread if available
* impact cost if available
* upper/lower circuit history
* ASM/GSM / surveillance flags
* pledged share data if available
* bulk/block deals if available

## 4.3 Fundamentals

Quarterly + annual:

* revenue / sales
* EBITDA / operating profit
* PAT / net profit
* EPS
* margins
* debt
* cash
* interest coverage
* CFO
* FCF if possible
* ROE / ROCE
* receivables / inventory / working capital indicators
* share capital changes / dilution
* promoter holding
* promoter pledge
* FII/DII/public holding
* auditor changes / qualified opinions if available

## 4.4 Events / documents

For shortlisted names only:

* latest company announcements
* corporate filings
* earnings call notes/transcripts if available
* recent company-specific news
* sector news
* macro indicators linked to sector
* regulatory actions / orders if relevant

## 4.5 Macro / sector data

* sector indices
* commodity prices where relevant
* FX if sector-sensitive
* rates / bond yields
* CPI/WPI
* government policy announcements
* import/export / industry metrics if relevant

---

# 5) Pipeline stages

---

## Stage A — Universe Definition

### Objective

Create the daily eligible stock universe before screening.

### Inputs

* exchange master
* listing status
* symbol mapping
* corporate action adjusted price history
* sector/industry mapping

### Rules

Exclude:

* suspended names
* recently listed names with insufficient history
* symbols with missing core data
* shell/inactive/illiquid names

### Config

* minimum price history: 252 trading days preferred
* minimum fundamental history: at least 6 quarters preferred
* allow separate configuration for IPO bucket

### Output

`universe_candidates(date, symbol, regime, eligibility_status, reason_codes[])`

---

## Stage B — Hard Junk Filters

### Objective

Remove names that are not worth deeper analysis.

### Suggested rules

#### B1. Liquidity filter

Reject if below thresholds such as:

* median daily traded value < X
* median daily volume < Y
* too many zero-volume or near-zero-volume days
* excessive spread / impact cost if available

Use rolling windows:

* 20D
* 60D
* 120D

#### B2. Circuit / manipulation proxy filter

Reject or penalize if:

* high fraction of days locked in upper/lower circuit over last 60/120 days
* repeated gap-and-freeze behavior
* very low float with extreme one-sided moves
* price jumps unsupported by volume quality

#### B3. Surveillance filter

Reject or heavily penalize if currently under:

* ASM
* GSM
* trade-to-trade restrictions
* other surveillance-heavy classifications

#### B4. Governance proxy filter

Reject or penalize based on proxies:

* high promoter pledge
* repeated preferential allotments / warrants with dilution
* auditor resignation / adverse comments
* delayed filings
* related-party flags if available
* frequent equity dilution
* extreme receivable growth vs sales growth
* cash flow mismatch over time

#### B5. Weak fundamental floor

Reject if persistent weakness:

* falling sales and profits across long periods
* negative operating cash flow over multiple periods
* unsustainable leverage
* interest coverage too low
* net worth erosion / distress indicators

### Output

`hard_filter_results(date, symbol, pass_flag, filter_name, metric_value, threshold, action)`

Also create aggregate:
`hard_filter_summary(date, symbol, pass_all, exclusion_reasons[])`

---

## Stage C — Fundamental + Structural Shortlist

### Objective

Score business quality and ownership/structure quality for stocks that survive Stage B.

### Philosophy

Do not aim for perfect “value investing” quality. Aim for **investable growth / improving business quality**.

### C1. Feature groups

#### Growth features

* sales growth YoY and QoQ
* PAT growth YoY and QoQ
* EBITDA growth
* EPS growth
* 2Y / 3Y CAGR where available

#### Quality features

* EBITDA margin trend
* PAT margin trend
* ROE / ROCE
* CFO/PAT ratio
* FCF consistency
* asset turns / capital efficiency if available

#### Balance sheet features

* debt/equity
* net debt/EBITDA
* interest coverage
* working capital stress indicators

#### Dilution / capital structure

* change in share count
* warrants / convertibles / preferential issues
* promoter holding trend
* promoter pledge trend

#### Ownership / float structure

* promoter holding band
* public float
* FII/DII trend if relevant
* concentration proxies if available

#### Stability / consistency

* earnings variance
* margin variance
* accruals quality proxies
* frequency of negative surprises if available

### C2. Scoring method

For each feature, compute:

* raw value
* normalized score within appropriate peer set:

  * sector
  * market cap bucket
  * regime bucket

Recommended normalization:

* percentile rank
* winsorization for outliers
* z-score only if distribution is stable

### C3. Composite score

Create:

* `growth_score`
* `quality_score`
* `balance_sheet_score`
* `dilution_score`
* `ownership_score`
* `consistency_score`

Then:
`fundamental_structural_score = weighted_sum(...)`

Recommended initial weights:

* growth: 25%
* quality: 20%
* balance sheet: 20%
* dilution/capital discipline: 15%
* ownership/float: 10%
* consistency: 10%

Weights should be config-driven.

### C4. Shortlist rule

Keep top X percentile or top N per sector/market-cap bucket.

Example:

* top 20–30% overall after exclusions
* enforce sector diversification to avoid one-theme crowding

### Output

`fundamental_scores(date, symbol, feature_json, component_scores, total_score, rank)`

---

## Stage D — Technical Breakout Watchlist

### Objective

Find names from the fundamental shortlist that are close to actionable technical setups.

### D1. Required technical concepts

#### Compression

Detect price contraction / volatility contraction:

Possible features:

* ATR compression
* Bollinger Band width contraction
* decreasing true range percentile
* narrow range days (NR7/NR10 style)
* range contraction across multi-week periods
* squeeze indicators

#### Relative strength

Measure outperformance vs:

* benchmark index
* sector index
* custom peer basket

Features:

* RS over 20/60/120D
* RS slope
* price above rising moving averages
* stock near highs while benchmark is weaker

#### Volume expansion readiness

Features:

* dry-up before breakout
* breakout day volume spike
* delivery volume confirmation if available
* accumulation distribution proxies

#### Breakout proximity

Features:

* distance to 20D/50D/52W high
* pivot pattern detection
* base duration
* resistance touches
* breakout level proximity without excessive extension

### D2. Technical pattern families to support

Not all need be rule-based initially, but at least support:

* volatility contraction pattern
* flat base
* ascending base
* range breakout
* cup/handle-like approximations
* moving-average consolidation and expansion
* momentum continuation after first base

### D3. Technical scoring

Create components:

* `compression_score`
* `relative_strength_score`
* `volume_pattern_score`
* `breakout_proximity_score`
* `trend_health_score`

Then:
`technical_setup_score = weighted_sum(...)`

Suggested weights:

* compression: 20%
* RS: 25%
* volume: 20%
* breakout proximity: 20%
* trend health: 15%

### D4. Trigger states

Each stock should have one of:

* `not_interesting`
* `early_base`
* `developing_setup`
* `near_breakout`
* `breakout_confirmed`
* `extended_do_not_chase`

This is important. Ranking without state classification becomes messy.

### Output

`technical_scores(date, symbol, state, breakout_level, stop_reference, component_scores, total_score)`

---

## Stage E — Candidate Selection for Event/LLM Analysis

### Objective

Reduce the universe to a manageable set for event enrichment.

### Selection rule

Run event/LLM layer only on:

* top N by combined fundamental + technical score
* or stocks in `near_breakout` / `breakout_confirmed` with fundamental score above threshold
* optionally add manual watchlist / held portfolio stocks

Typical daily size:

* 50–200 names

### Output

`event_candidates(date, symbol, reason_for_selection, priority_rank)`

---

## Stage F — Event / Announcement / News Layer

### Objective

Understand whether recent developments strengthen, weaken, or invalidate the setup.

### Very important design rule

LLM should **not** directly give buy/sell recommendations.
LLM should convert unstructured text into **structured event intelligence**.

## F1. Input documents

For each selected stock, fetch within configured lookback:

### Company-specific

* exchange announcements
* investor presentations
* press releases
* earnings releases
* concall transcript if available

### News

* company news
* sector news
* policy/regulation news
* commodity/macro news tied to sector exposure

### Suggested lookbacks

* announcements: last 90–180 days, prioritize latest 10–20 documents
* company news: last 30–90 days
* sector/macro: last 30–120 days

Do not OCR and summarize 10 years for all stocks. Wasteful.

## F2. Pre-LLM heuristics

Before sending to LLM:

* deduplicate near-identical documents
* classify by doc type
* prioritize recency and materiality
* extract first pages / key sections using deterministic parsing where possible
* use OCR only when necessary
* skip routine low-signal filings where possible

## F3. LLM tasks

LLM should output structured JSON like:

```json
{
  "event_type": "capacity_expansion|order_win|pledge_change|fund_raise|regulatory_issue|earnings|mgmt_change|litigation|other",
  "event_direction": "positive|negative|mixed|neutral",
  "materiality": 0.0,
  "confidence": 0.0,
  "time_horizon": "immediate|near_term|medium_term|unclear",
  "summary": "...",
  "key_facts": ["..."],
  "risk_flags": ["..."],
  "supports_thesis": true,
  "contradicts_thesis": false
}
```

### Required extraction dimensions

For each stock, infer:

* what happened
* is it new or repetitive
* likely impact on revenue/profit/order book/cash flow/balance sheet/governance
* whether it is catalyst, noise, or risk
* whether it is already likely known/obvious
* whether there are red flags

## F4. Event categories

Engineer should support at least:

### Positive

* strong earnings surprise
* capacity expansion with visibility
* large order wins
* margin improvement drivers
* deleveraging
* promoter/FII accumulation
* favorable regulation
* sector tailwind
* turnaround evidence

### Negative

* auditor resignation
* pledge increase
* weak cash conversion
* qualified results
* dilution/fund raise at bad terms
* receivables spike
* regulatory action
* promoter selling
* customer concentration issue
* litigation
* adverse sector/macro shock

### Mixed / context-dependent

* capex announcement
* acquisition
* equity raise for growth
* government policy change
* commodity price moves

## F5. Event score

Build deterministic score from structured LLM extraction:

* `event_positivity_score`
* `event_materiality_score`
* `event_risk_score`
* `thesis_support_score`
* `macro_sector_tailwind_score`

Then derive:
`event_quality_score`

Important: negative governance/regulatory events should have asymmetric downside penalty.

### Output

Two layers:

1. raw extraction table
   `event_extractions(symbol, doc_id, doc_time, extraction_json, model_version)`

2. aggregated stock-level event score
   `event_scores(date, symbol, aggregated_event_features, total_event_score, key_flags[])`

---

## Stage G — Final Ranking

### Objective

Produce final actionable ranking.

## G1. Inputs

* hard filter pass/fail
* fundamental_structural_score
* technical_setup_score
* event_quality_score
* tradeability measures

## G2. Final score formula

Example:

`final_score = 0.35 * fundamental_structural_score + 0.30 * technical_setup_score + 0.20 * event_quality_score + 0.15 * tradeability_score`

This is a starting point only.

Alternative:
Use different weights by regime:

* investment mode: fundamentals heavier
* swing mode: technicals heavier

## G3. Hard overrides

Even with high score, suppress/rerank if:

* governance red flag active
* surveillance active
* too illiquid
* too extended technically
* event contradiction severe

## G4. Final outputs

Each stock should have:

* final rank
* state
* thesis summary
* key reasons
* top risks
* next trigger level
* invalidation level
* confidence band

### Output schema

`final_rankings(date, symbol, final_score, rank, state, thesis_json, risks_json, trigger_level, invalidation_level)`

---

# 6) ML architecture

## 6.1 What should be rule-based first

Start rule-based for:

* hard filters
* most fundamental scores
* most technical scores
* initial event aggregation weights

Reason: easier to debug and backtest.

## 6.2 Where ML can help

Use ML only where it adds real edge.

### Good ML use cases

* learn final score weighting from history
* classify breakout success probability
* regime detection
* anomaly detection in fundamentals
* event impact calibration
* ranking model using engineered features

### Candidate ML models

* XGBoost / LightGBM for ranking/classification
* logistic regression baseline
* survival/hazard models for breakout follow-through
* HMM / regime models for market conditions
* clustering by stock behavior / sector regime

### Avoid early overengineering

Do **not** start with:

* massive end-to-end deep models
* LLM deciding trading strategy directly
* raw text + price multimodal model in v1

---

# 7) Labels / target design

This matters. Without labels the ML engineer will build nonsense.

## 7.1 Suggested target types

### For breakout pipeline

Binary / probabilistic labels:

* did stock break out above identified pivot within next X days?
* after breakout, did it deliver Y% move before hitting Z% stop?
* was breakout sustained after 5/10/20 trading days?

### For ranking pipeline

Forward return buckets:

* 20D forward return
* 60D forward return
* 120D forward return

But returns alone are weak. Better:

* risk-adjusted forward return
* alpha vs benchmark
* max adverse excursion
* max favorable excursion

### Example label

For a stock on date T:

* entry trigger = breakout level
* success = reaches +15% before -7% stop within 60 trading days

This is more useful than raw return.

---

# 8) Backtesting framework

## Must-have rules

* no lookahead leakage
* use point-in-time data only
* corporate actions adjusted
* event timestamps respected
* fundamentals lagged realistically after release dates
* announcements/news available only after actual publication time

## Backtest outputs

* hit rate
* average gain/loss
* expectancy
* max drawdown
* Sharpe / Sortino if portfolio-based
* turnover
* sector concentration
* liquidity stress
* slippage sensitivity

## Slice analysis

Evaluate by:

* market regime
* cap bucket
* sector
* bull / bear / sideways
* liquid vs less liquid
* with vs without event layer

This will tell whether the LLM layer is adding real value or just sounding intelligent.

---

# 9) Data model / storage suggestion

## Core tables

### Security master

* symbol
* isin
* exchange
* sector
* industry
* listing dates
* regime tags

### Market data tables

* daily_price_volume
* delivery_data
* tradeability_metrics
* surveillance_flags

### Fundamentals tables

* quarterly_financials
* annual_financials
* shareholding_pattern
* corporate_actions
* governance_flags

### Derived features

* feature_store_daily
* feature_store_quarterly

### Stage outputs

* universe_candidates
* hard_filter_results
* fundamental_scores
* technical_scores
* event_candidates
* event_extractions
* event_scores
* final_rankings

### Audit / reproducibility

* model_registry
* prompt_registry
* feature_version_registry
* run_logs

---

# 10) Orchestration

## Recommended jobs

### Daily EOD

* ingest price/volume
* update tradeability metrics
* update technical features
* rerun universe/hard filters
* rerun technical ranking
* generate event candidate list
* fetch latest announcements/news for candidates
* run event extraction
* publish final rankings

### Weekly

* recompute deeper structural features
* governance proxy refresh
* sector relative rankings
* retrain/recalibrate models if required

### Quarterly

* update fundamental data
* recompute fundamental scores
* review thresholds and score drift

---

# 11) LLM subsystem requirements

## 11.1 Do not store only summaries

Store:

* raw text
* cleaned text
* extracted structured facts
* short summary
* model version
* prompt version

## 11.2 Prompting design

Use fixed templates by doc type:

* earnings result
* exchange filing
* press release
* regulatory order
* news article

## 11.3 Quality controls

Add deterministic checks:

* does extracted number actually exist in text?
* is polarity inconsistent with extracted facts?
* is announcement stale?
* are multiple docs repeating same event?

## 11.4 Cost control

Prioritize docs by materiality score before LLM.
Examples of high priority:

* results
* order wins
* fund raise
* promoter pledge changes
* auditor changes
* regulatory notices

Skip low-value repetitive notices.

---

# 12) Explainability requirements

For each ranked stock, output human-readable explanation:

## Example

* passed liquidity and governance filters
* strong 3-quarter sales/profit growth
* improving EBITDA margin and low dilution
* in 8-week compression near 52-week high
* RS in top decile vs sector
* latest announcement indicates capacity expansion / order win
* no major recent governance red flags

Also show:

* top 3 positives
* top 3 risks
* exact disqualifiers if excluded

This is necessary for trust and debugging.

---

# 13) Risk controls / exclusions

Engineer should support explicit veto flags:

* active surveillance / GSM / ASM
* high pledge
* auditor resignation
* suspected manipulation pattern
* too illiquid for portfolio size
* technically overextended after breakout
* event score sharply negative

This should override raw score.

---

# 14) Portfolio construction hooks

Even if not trading immediately, final ranking should expose fields needed later:

* position sizing bucket
* liquidity bucket
* sector bucket
* conviction score
* risk score
* suggested entry zone
* invalidation / stop zone
* holding horizon bucket

Portfolio engine can be built later on top.

---

# 15) Evaluation roadmap

## Phase 1

Rule-based pipeline only:

* stages A–D + final ranking
* no LLM
* backtest and validate

## Phase 2

Selective event layer:

* top 50–200 names only
* structured LLM extraction
* compare ranking quality with and without events

## Phase 3

ML ranking calibration:

* train ranking model on engineered features + event scores
* compare against weighted-rule baseline

## Phase 4

Portfolio / execution simulation

---

# 16) Non-goals for v1

Do not do these initially:

* full 10-year OCR parsing for all stocks
* end-to-end autonomous “AI trader”
* tick-level execution intelligence
* one universal model for all cap buckets and regimes
* purely news-driven stock picking
* direct LLM buy/sell call generation

---

# 17) Recommended implementation details

## Stack

* Python
* Postgres for structured storage
* object storage for raw docs/text
* feature pipeline with Pandas/Polars
* orchestration via cron/Airflow/Prefect
* XGBoost/LightGBM for ranking experiments
* LLM service as separate module, not embedded everywhere

## Module split

### `data_ingestion/`

* prices
* fundamentals
* announcements
* news
* macro

### `features/`

* liquidity
* governance proxies
* fundamentals
* technicals
* sector-relative
* macro joins

### `screening/`

* universe rules
* hard filters
* shortlist generation

### `event_layer/`

* doc fetch
* cleaning
* dedup
* LLM extraction
* event aggregation

### `ranking/`

* score aggregation
* ML model inference
* explanations

### `backtest/`

* point-in-time reconstruction
* label generation
* evaluation

### `serving/`

* daily ranked outputs
* dashboards
* alerts

---

# 18) Deliverables for ML engineer

Engineer should produce:

1. feature spec document
2. data contracts for each table
3. scoring engine with config-driven thresholds/weights
4. daily pipeline DAG
5. event extraction schema and prompts
6. baseline backtest notebook/report
7. final ranking output format
8. monitoring for missing data / stale data / score drift

---

# 19) Minimal output contract per stock

```json
{
  "date": "2026-03-19",
  "symbol": "ABC",
  "universe_pass": true,
  "hard_filter_pass": true,
  "fundamental_structural_score": 78.4,
  "technical_setup_score": 82.1,
  "event_quality_score": 64.0,
  "tradeability_score": 71.5,
  "final_score": 75.8,
  "state": "near_breakout",
  "breakout_level": 1245.0,
  "invalidation_level": 1168.0,
  "top_reasons": [
    "Strong sales and PAT growth",
    "Margin expansion over 3 quarters",
    "8-week compression with high relative strength"
  ],
  "top_risks": [
    "Recent dilution last year",
    "Sector news mixed",
    "Breakout not yet confirmed"
  ],
  "red_flags": [],
  "explanation": "Passed tradeability and governance filters, strong improving fundamentals, technically near actionable breakout, event layer moderately supportive."
}
```

# Final words

The main thing to enforce in the spec is this:

* **LLM is a late-stage structured event parser, not the brain of the system.**
* **Rules + engineered features should do most of the heavy lifting.**
* **Backtest A–D hard before trusting F.**
