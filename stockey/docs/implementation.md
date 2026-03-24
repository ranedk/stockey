# Investment Pipeline — Task and Setup Based Spec

## Goal

Build a practical stock decision pipeline with a small number of reusable tasks and a few concrete setup playbooks.

The pipeline should:

* use external screeners and broker/platform data as much as possible,
* minimize local calculations,
* watch announcements only for shortlisted stocks,
* use LLM only after a stock has already passed screener + regime + technical checks.

---

# Tasks

## Task 1 — Screener crawler

### Objective

Crawler for screener is ready and returns stocks for a given screener.

### Input

* screener name or screener URL
* optional date

### Output

* list of stocks
* screener snapshot metadata

### Notes

This task should support multiple upstream screeners later, but v1 can start with one reliable source.

---

## Task 2 — OHLCV crawler

### Objective

Crawler for OHLCV for shortlisted stocks.

### Input

* stock list from Task 1
* benchmark/index symbols
* date range

### Required data

* last 5 years of daily OHLCV
* latest 1 day of 1-minute data if available

### Output

* OHLCV stored for each stock
* daily benchmark OHLCV
* sector/index OHLCV where needed

### Notes

Use external broker/platform data as the primary source. Do not build price adjustment logic first unless forced.

---

## Task 3 — Macro crawler

### Objective

Crawler for macro parameters.

### Input

* configured macro series list

### Existing data

Use what is already available from earlier work where usable without major new engineering.

### Minimum macro list

* G-Sec yields
* RBI policy rate / repo-related data
* CPI
* WPI
* USDINR or relevant FX proxies
* sector-linked commodity prices where relevant

### Mark as new if added

If a macro variable is not already available in the current local system, mark it as `NEW_MACRO_SOURCE_REQUIRED`.

### Output

* macro snapshot table by date
* freshness status

---

## Task 4 — Regime engine

### Objective

Classify market regime using benchmark, macro, volatility, and policy/shock context.

### Output

One of:

* `BULL_RISK_ON`
* `BULL_NARROW`
* `STABLE`
* `STABLE_BUT_TARIFF_RISING`
* `RISK_OFF`
* `SHOCK`

### Notes

The exact algo can be implemented later. For now, refer to this as:

* `REGIME_ALGO_V1`

---

## Task 5 — Technical parameter calculator

### Objective

Given stocks from Task 2, compute the small set of technical parameters required by setups.

### Minimum technical parameters

* 20 DMA
* 50 DMA
* 200 DMA
* ATR 20
* ATR compression percentile
* Bollinger Band width
* distance to 20D high
* distance to 50D high
* distance to 52W high
* average traded value 20D
* average traded value 60D
* relative strength vs benchmark
* relative strength vs sector
* breakout extension %

### Output

* stock technical profile
* pass/fail flags used by setups

---

## Task 6 — Rule engine

### Objective

Apply setup-specific rules on top of screener + OHLCV + technicals + regime.

### Output

* shortlisted stocks by setup
* reject reasons
* watchlist reasons

---

## Task 7 — Announcement watcher

### Objective

Mark shortlisted stocks for announcement watching.

### Output

* watchlist of stocks
* watch status
* last checked time

### Trigger sources

* exchange announcements
* results
* pledges / governance related updates
* major company filings
* major company-specific news if added later

---

## Task 8 — Announcement parser + LLM evaluator

### Objective

When announcement is triggered, fetch it, parse it, and send it to LLM for structured evaluation.

### LLM role

LLM should not directly say buy/sell blindly.

LLM should answer:

* what happened,
* whether it is positive / negative / mixed,
* whether it is material,
* whether it strengthens the current setup,
* whether it introduces governance / balance-sheet / execution risk,
* whether the stock is worth investing now given all current data.

### Output

* structured event summary
* event verdict
* key risks
* recommendation to continue / reject / review manually

---

## Task 9 — Risk profiling and allocation

### Objective

If stock qualifies after announcement and rule checks, assign risk profile and investment amount.

### Output

* risk bucket
* conviction bucket
* suggested allocation amount X
* stop / invalidation guidance if applicable

---

# Setup Template

Each setup should follow this format.

## Generic Setup Flow

1. Check regime based on `REGIME_ALGO_V1`
2. If regime matches setup, get stocks from screener with setup-specific screener rules
3. Get OHLCV for these stocks — last 5 years daily, latest 1 day 1-minute if available
4. Find required technical parameters
5. Apply setup rule
6. Mark qualified stocks for announcement watch
7. If announcement is triggered, fetch, parse, and let LLM evaluate with access to all current stock data
8. If stock still qualifies, profile risk and invest X amount

---

# Setups

## Setup 1 — Strategy 1 — SME momentum in supportive regime

### Regime requirement

Allowed only if regime is one of:

* `BULL_RISK_ON`
* `BULL_NARROW`
* `STABLE`

Not allowed if regime is:

* `STABLE_BUT_TARIFF_RISING`
* `RISK_OFF`
* `SHOCK`

### Screener rules

Placeholder:

* `SME_MOMENTUM_SCREEN_V1`

Typical intent:

* acceptable liquidity for SME
* no obvious governance poison
* recent earnings or business momentum
* price not too far from highs
* avoid obvious junk / freeze names

### Technical parameters to use

* 20 DMA, 50 DMA
* ATR compression
* Bollinger width
* distance to 52W high
* average traded value 20D/60D
* relative strength vs SME/smallcap benchmark
* breakout extension %

### Apply rule

Placeholder:

* `SME_SETUP_RULE_V1`

Typical intent:

* stock is in compression or early breakout
* not overextended
* liquidity acceptable
* relative strength supportive

### Announcement watch

Watch for:

* order wins
* capacity expansion
* results
* pledge changes
* auditor/governance issues
* dilution / fund raise

### Post-announcement LLM question

* is this event a real business catalyst or just noise?
* does it improve confidence or introduce hidden risk?

### Risk profiling

* high risk bucket by default
* smaller allocation cap than large/mid caps

---

## Setup 2 — Strategy 1 — Large-cap breakout / continuation

### Regime requirement

Allowed in:

* `BULL_RISK_ON`
* `BULL_NARROW`
* `STABLE`
* selectively `STABLE_BUT_TARIFF_RISING`

Avoid fresh entries in:

* `RISK_OFF`
* `SHOCK`
  unless direct beneficiary and very liquid

### Screener rules

Placeholder:

* `LARGECAP_BREAKOUT_SCREEN_V1`

Typical intent:

* liquid large caps
* price near highs
* strong relative strength
* acceptable recent earnings profile

### Technical parameters to use

* 20/50/200 DMA
* ATR compression
* Bollinger width
* distance to 20D/50D/52W high
* RS vs Nifty / sector
* breakout extension %
* traded value

### Apply rule

Placeholder:

* `LARGECAP_BREAKOUT_RULE_V1`

Typical intent:

* clean base or continuation
* leadership within sector
* not too extended
* broad market not breaking down

### Announcement watch

Watch for:

* earnings
* guidance changes
* major orders or client wins
* sector-sensitive policy or tariff events
* management commentary changes

### Post-announcement LLM question

* does this strengthen the continuation thesis or invalidate it?

### Risk profiling

* lower risk bucket than SME
* larger max allocation than SME

---

## Setup 3 — Strategy 1 — Large-cap defensive / tariff-rising regime

### Regime requirement

Only when regime is:

* `STABLE_BUT_TARIFF_RISING`
* or `RISK_OFF` with selective defensive leadership

### Screener rules

Placeholder:

* `DEFENSIVE_TARIFF_SCREEN_V1`

Typical intent:

* liquid large caps
* pricing power
* lower external shock sensitivity
* better balance sheet
* stable earnings profile

### Technical parameters to use

* 50 DMA, 200 DMA
* RS vs benchmark
* drawdown control
* volatility state
* extension %

### Apply rule

Placeholder:

* `DEFENSIVE_TARIFF_RULE_V1`

Typical intent:

* stock is holding trend while market weakens
* not deeply broken technically
* shock narrative is not hurting business directly

### Announcement watch

Watch for:

* margin commentary
* raw material impact
* tariff commentary
* forex impact
* management guidance

### Post-announcement LLM question

* is this company exposed to tariff shock or a relative beneficiary?

### Risk profiling

* medium risk bucket
* moderate allocation

---

## Setup 4 — Strategy 1 — Mid-cap improving fundamentals + near breakout

### Regime requirement

Allowed in:

* `BULL_RISK_ON`
* `BULL_NARROW`
* `STABLE`

Restrict in:

* `STABLE_BUT_TARIFF_RISING`
* `RISK_OFF`
* `SHOCK`

### Screener rules

Placeholder:

* `MIDCAP_IMPROVER_SCREEN_V1`

Typical intent:

* improving quarterly numbers
* manageable debt
* no recent severe governance red flags
* technical setup not too late

### Technical parameters to use

* 20/50/200 DMA
* ATR compression
* Bollinger width
* distance to highs
* RS vs benchmark and sector
* breakout extension %

### Apply rule

Placeholder:

* `MIDCAP_IMPROVER_RULE_V1`

Typical intent:

* business improvement plus technical readiness
* avoid late-stage euphoric moves

### Announcement watch

Watch for:

* results
* order wins
* capex
* debt reduction
* promoter actions
* dilution risk

### Risk profiling

* medium-high risk bucket
* smaller than large cap, larger than SME only if liquidity is strong

---

# Output Contract

## Output per task

### Task 1 output

* screener name
* date
* stock list

### Task 2 output

* stock
* OHLCV availability status
* time ranges fetched

### Task 3 output

* macro parameter
* latest value
* freshness status
* `NEW_MACRO_SOURCE_REQUIRED` if unavailable

### Task 4 output

* date
* regime name
* regime notes

### Task 5 output

* stock
* technical parameter values
* technical pass/fail flags

### Task 6 output

* stock
* setup name
* rule pass/fail
* reject reasons

### Task 7 output

* stock
* watch enabled
* watch reason

### Task 8 output

* stock
* announcement summary
* event verdict
* continue/reject/manual-review

### Task 9 output

* stock
* risk bucket
* allocation X
* notes

---

# Immediate Build Order

## Phase 1

Build these first:

1. Task 1 — Screener crawler
2. Task 2 — OHLCV crawler
3. Task 3 — Macro crawler
4. Task 4 — Regime engine
5. Task 5 — Technical parameter calculator
6. Task 6 — Rule engine

## Phase 2

Then add:
7. Task 7 — Announcement watcher
8. Task 8 — Announcement parser + LLM evaluator

## Phase 3

Then add:
9. Task 9 — Risk profiling and allocation
