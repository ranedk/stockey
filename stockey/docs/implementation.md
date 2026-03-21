# What to borrow

* **Borrow** broad fundamental screening from **Screener**. It supports custom screens, alerts, CSV export, company announcements, Excel export workflows, and now has AI over official company documents. ([Screener.][1])
* **Borrow** technical shortlist generation from **ScanX**. It offers 50+ readymade screeners, 200+ custom filters, sectoral research, and technical screens for breakouts, RSI, support/resistance, crossovers, momentum and squeeze/range setups; its live scanner is tied to Dhan login for real-time access. ([Dhan][2])
* Use **one broker API** as your price source of truth. **Dhan** says its daily historical data starts from a stock’s inception, intraday goes back 5 years, and daily historical data is adjusted for bonuses and splits. **Zerodha**’s paid Kite Connect includes historical and live market data for ₹500/month, while the free personal tier does not include market data; Zerodha also adjusts historical OHLC for corporate actions. ([Dhan][3])
* Use **official** sources for disclosures and hard-risk flags: **NSE corporate filings/announcements**, **NSE pledged-data pages**, and **SEBI ASM/GSM surveillance** lists. ([NSE India][4])

So the stack should become:

1. **External shortlist layer**: Screener + ScanX
2. **Your validation layer**: Dhan/Zerodha OHLC + a few local indicators
3. **Your edge layer**: NSE/BSE filing fetch, dedup, OCR only if needed, LLM event parser, regime overrides, final ranking

## 1) Horizon-specific rulesets

First, don’t treat short, mid and long term as one pipeline with different weights. They need different vetoes.

### A. Long term: 6 to 24 months

This is **business quality first, entry quality second**.

**Universe**

* Large cap + mid cap as default
* Small cap only if balance sheet and cash conversion are clean
* SME usually **off by default**
* SME gets enabled only in **clear bull / risk-on** regime and only as a small satellite book

**Step-by-step**

1. Start with Screener-style fundamental screens: sales growth, PAT growth, ROCE/ROE, debt, cash flow quality, dilution, pledge, auditor/governance clues.
2. Remove all governance poison immediately: rising pledge, auditor resignation/qualification, serial dilution, receivables blowing up vs sales, cash flow mismatch.
3. Prefer businesses where the next 2–8 quarters are understandable: capacity addition, order book conversion, deleveraging, margin improvement, market-share gain.
4. Use technicals only for **entry timing**: base, retest, not wildly extended.
5. Use event layer to answer: “Is the thesis strengthening or breaking?”
6. Size larger only in liquid names with clean governance.

**Long-term regime rules**

* In **bull** regime: allow select small caps; still keep SME small.
* In **neutral** regime: large/mid dominate.
* In **bear / shock** regime: no fresh SME buys; deep cyclicals need stronger balance sheets; cash-generators and leaders get preference.

**What matters most**

* Earnings quality
* Balance sheet
* Promoter behaviour
* Industry tailwind
* Execution visibility

**What matters less**

* One-day breakout
* Short-term RSI
* Social-media/news excitement

### B. Mid term: 1 to 6 months

This is where your original pipeline fits best.

**Universe**

* Large, mid, small
* SME only if: strong regime + real liquidity + real event catalyst + actual setup

**Step-by-step**

1. Use Screener for broad structural quality.
2. Use ScanX for technical state: compression, near breakout, RS, momentum, volume pattern.
3. Pull your own OHLC only for survivors and recompute a few local checks: ATR compression, 20/50/200DMA structure, distance from highs, volume expansion.
4. Fetch latest 30–90 day announcements/news for top 50–150 names.
5. Let LLM produce structured event facts only.
6. Apply hard overrides:

   * ASM/GSM or surveillance = usually reject
   * severe governance flag = reject
   * too extended = demote
7. Rank by **fundamental + technical + event + tradeability**

**Mid-term regime rules**

* **Bull**: breakout/continuation setups work; allow select small caps and exceptional SMEs.
* **Neutral**: prefer leaders near highs, not laggards.
* **Bear**: focus on relative-strength names only; avoid low-float breakouts; mostly large/mid.
* **Shock**: cut SME entirely; reduce cyclical exposure unless the stock is a direct beneficiary.

### C. Short term: 2 days to 6 weeks

This is **market regime first, stock second**.

**Universe**

* Highly liquid large caps and liquid mid caps
* SME should be a **separate speculative book**, not mixed into the main pipeline

**Step-by-step**

1. Check market state first: index trend, breadth, volatility, sector leadership.
2. Run ScanX/live scanners for liquid technical setups only.
3. Confirm with your own OHLC and volume.
4. Avoid holding through binary events unless that event itself is the trade thesis.
5. Use very strict invalidation.
6. If regime is messy, trade less. Cash is a valid position.

**Short-term regime rules**

* In **risk-off**, most breakout systems degrade.
* In **shock**, either don’t trade or only trade very liquid direct beneficiaries.
* Never let SME names masquerade as “short-term investments.” They are often liquidity traps.

---

## 2) Regime engine you should hard-code

This part should be simple and cheap.

Use 5 regime flags:

1. **Index trend**: Nifty / Nifty 500 above or below medium and long trend
2. **Breadth**: participation, not just index level
3. **Volatility**: India VIX or realized vol
4. **Macro shock**: oil, INR, yields
5. **Policy/geopolitical shock flag**: tariff escalation, war/conflict, sanctions, severe regulation

You do not need a PhD model for this. A coarse regime engine is enough.

Why this matters: WTO’s March 2026 outlook says tariffs and uncertainty shaped trade patterns, and prolonged conflict can keep transport and fuel costs elevated while hurting net energy-importing regions more. IMF said its World Uncertainty Index had doubled since January 2025, and BIS notes uncertainty weakens business investment. That is enough justification to raise the hurdle rate and shrink your universe during shock periods. ([World Trade Organization][5])

---

## 3) How to handle war/conflict and tariff news

Do **not** convert headline news directly into buy signals.

Use this 6-question filter for each affected stock:

1. Is the company a **direct** beneficiary/victim, or is this just a story?
2. Does it have **pricing power**?
3. Is it **import-dependent** for key raw materials/components?
4. Is it **export-dependent** into the affected geography?
5. Can it actually **execute** if demand shifts to it?
6. Is the chart confirming the thesis, or are you forcing a macro narrative onto a bad stock?

### Tariff priors

* Tariff headlines help only if the company has **domestic capacity**, acceptable margins, and low dependence on imported inputs.
* “China+1” style stories are useless without customer approvals, capacity, working capital and execution proof.
* Tariff beneficiaries without balance-sheet strength are usually traps.

### War/conflict priors

* First-order transmission is usually through **oil, gas, shipping, fertilizer, FX, rates**.
* Energy/import-sensitive sectors get penalized first.
* Domestic-defense, energy, logistics or substitute-manufacturing stories should only be trusted if earnings/order flow supports them.
* In shock periods, lower-cap names deserve an automatic penalty even if the story sounds right.

---

## 4) Operational priors to hard-code

These are not “truth.” They are **default priors** until your own data disproves them.

### Market-structure priors

* SME and microcaps are the **first bucket to disable** in risk-off.
* Breakouts work better with broad participation; lone-stock breakouts in weak breadth fail more.
* Governance red flags override cheap valuation.
* Price action without liquidity is noise.
* Event positives do not override ASM/GSM/surveillance. ([Securities and Exchange Board of India][6])

### Business-quality priors

* **New order win** matters only if it is material relative to revenue/order book and the company can fund execution.
* **Capex announcement** is positive only if utilization, balance sheet and demand visibility exist.
* **Deleveraging + margin improvement** is a stronger signal than revenue growth alone in many cyclicals.
* **Receivables up sharply after “growth”** is suspicious.
* **Promoter pledge rising** is a major negative unless clearly explained and temporary. NSE explicitly provides pledged-data disclosures, so this should be machine-checked. ([NSE India][7])

### Macro/sector priors

* Oil up: good for upstream/energy-linked beneficiaries, bad for many importers and energy users.
* INR weakness: usually helps exporters with domestic costs, hurts import-heavy sectors unless they can pass cost through.
* Falling rates or easing liquidity: better for financials, housing-linked names, discretionary cyclicals.
* Tariff protection helps only if local substitutes actually exist.
* Commodity consumers need pricing power; commodity producers need cycle discipline.

### Geography/location priors

* Single-plant or single-port dependence raises operational risk.
* State-policy dependence matters for EPC, infra, tourism, hospitals, liquor, mining, building materials.
* Monsoon/rural-income dependence matters for tractors, agrochem, rural financiers, some FMCG and two-wheelers.
* Export geography concentration matters more than people admit.

---

## 5) What to backtest and what not to waste time on

**Backtest**

* Your own OHLC-based rules
* Your own regime filters
* Event reaction templates after structured parsing
* Final score weights
* Entry/invalidation logic

**Do not waste time trying to backtest**

* Third-party screener logic you don’t own
* Uncaptured historical outputs from Screener/ScanX
* “Narrative priors” as if they were clean labels

But do this from day 1:

* save **daily CSV snapshots** of every Screener/ScanX shortlist
* save broker OHLC snapshot
* save regime flags
* save event parser outputs

That gives you a forward test archive even if you cannot reconstruct the past.

---

## 6) Minimal-code version I would actually build

1. **Screener**

   * 3–5 saved screens for long-term quality, mid-term improvers, balance-sheet clean small caps
   * export CSV daily
2. **ScanX**

   * 3–5 saved technical screens: squeeze, near-high, breakout, RS, volume
   * export/capture symbols daily
3. **Broker API**

   * choose **one**: Dhan or Zerodha
   * pull OHLC only for shortlisted symbols
   * compute just 8–12 local indicators
4. **Official feeds**

   * NSE announcements
   * NSE pledged data
   * SEBI ASM/GSM
5. **Your event layer**

   * fetch top docs for top 50–100 names
   * structured extraction only
6. **Rule engine**

   * horizon-specific weights
   * regime penalties
   * SME enable/disable switch
7. **Final output**

   * ranked list + state + thesis + top risks + veto flags

That is small enough to ship.

## 7) Final recommendation

The correct redesign is:

* **Do not** build full fundamentals + full technical indicator infra + full historical document warehouse first.
* **Do** use Screener/ScanX for upstream filtering, a broker API for clean adjusted OHLC, and official exchange/regulator pages for disclosures and risk flags.
* Keep your proprietary work focused on:

  * regime logic
  * structured announcement/news parsing
  * hard overrides
  * final decision memo

And one correction: don’t call your priors “ground truth.” They’re **priors**. Ground truth is what survives logging and future validation.

If you want, next I’ll turn this into a concrete **v2 implementation spec** with exact modules, tables, daily jobs, and horizon-specific rule configs.

[1]: https://www.screener.in/features/ "Features - Screener"
[2]: https://dhan.co/scanx-stock-screener/ "ScanX Stock Screener: 50+ Screeners for Stock & Live Market Insights"
[3]: https://dhan.co/support/platforms/dhanhq-api/what-timeframe-data-is-available-through-dhan-s-historical-data-apis/ "What timeframe data is available through Dhan’s Historical Data APIs? | Dhan Support"
[4]: https://www.nseindia.com/companies-listing/corporate-filings-application?utm_source=chatgpt.com "Corporate Filings- Equity, Debt, MF, SME"
[5]: https://www.wto.org/english/res_e/booksp_e/gtos0326_e.pdf "Global Trade Outlook and Statistics - March 2026"
[6]: https://www.sebi.gov.in/curation/surviellance.html "Securities and Exchange Board of India"
[7]: https://www.nseindia.com/companies-listing/corporate-filings-pledged-data?utm_source=chatgpt.com "Corporate Filings Pledged Data - Equity, SME"
