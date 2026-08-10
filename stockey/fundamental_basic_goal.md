# Fundamental Signal System — Build Spec

**Scope:** Indian smallcap/microcap. Horizon 12–30 months. Not a trading system.
**Design principle:** Screener gives *state*. Feeds give *events*. Events only matter when they land on primed state.

---

## 1. What to build

### L1 — Universe (quarterly refresh)

Purpose: define what you will ever look at. Cheap, run first, removes the dominant loss mode.

| Filter | Rule |
|---|---|
| Liquidity floor | 20-day median traded value ≥ threshold set from max position size (hard gate, not a preference) |
| Neglect | Mcap band + institutional holding % + coverage proxy |
| Cash conversion | Rolling 3yr CFO/EBITDA — flag < 0.6 |
| Receivables | Debtor days trend over 8 quarters — flag rising |
| Auditor | Any change in last 3 years — flag |
| Contingent liabilities | As % of net worth — flag above threshold |
| Related party | RPT as % of revenue — flag above threshold |

Output: ~500–700 names. Flags are exclusions, not scores.

### L2 — Watch state (quarterly + on-filing)

Per-company state vector. **Generates no trades.** Answers: if an event hits this name, does it matter?

- `debt_trajectory` — net debt Δ over 4 quarters, interest coverage, debt/EBITDA
- `cwip_ratio` — CWIP/gross block, and its QoQ delta (conversion is the trigger, not the level)
- `pledge_pct` — promoter pledge and direction
- `promoter_stake` — direction over 4 quarters
- `sector_cycle_phase` — computed by aggregating gross-block growth across all listed players in the sector vs demand proxy; sector-level, not company-level
- `valuation_percentile` — EV/EBITDA vs own history and sector

### L3 — Triggers (daily batch, one job, ~8am)

Build these four only.

1. Rating actions (all four agencies)
2. SEBI PIT insider trades, SAST, pledge change disclosures
3. Quarterly shareholding pattern deltas — first institutional entry
4. Results calendar + reported numbers

Alert fires **only** on trigger × primed L2 state. Never on trigger alone.

### L4 — Thesis register (per position)

Mandatory at entry, before sizing:

- Falsifiable **fundamental** prediction with a date. Not a price target.
  *e.g. "Net debt < ₹X by Q3 FY27", "utilisation > 85% within 4 quarters", "CWIP → gross block by Q2, revenue +30% within 2 quarters"*
- Invalidation criteria, pre-committed
- Origin tag: `systematic_screen` | `ad_hoc`
- Signal definition version used

### L5 — Portfolio construction

- 12–20 concurrent positions. Below ~10, a 20% hit rate gives a material chance of holding zero winners over a cycle.
- Size off ADV, not conviction.
- Exit on thesis invalidation, not price. Optional wide stop only.
- Optional entry gate: 200-DMA reclaim on an already-qualified name. Never disqualifies permanently.

---

## 2. Explicitly NOT building

| Item | Why deferred |
|---|---|
| General BSE/NSE announcement classifier | High volume, high maintenance, low marginal signal over the four feeds |
| Concall transcript NLP | Cost/benefit fails at this horizon |
| HS-code → company mapping table | Highest moat, but months of manual work. Build *after* the strategy shows evidence of working, not to find out whether it does |
| Anything sub-daily | No edge available on latency at an 18-month horizon |

---

## 3. Sources

### 3.1 Overview

| Data | Source | Refresh | Effort |
|---|---|---|---|
| Financials, ratios, historical series | screener.in (query language + custom screens) | Quarterly | Low |
| Shareholding, pledge | screener.in + exchange filings | Quarterly | Low |
| Rating actions — *detection* | Exchange announcement feed (LODR Reg 30 / NSE "SDD – Credit Rating") | Daily | Low |
| Rating actions — *enrichment* | Agency site: rationale, debt quantum, reasoning | Daily | Low |
| Insider trades (PIT), SAST | BSE primary + NSE secondary | Daily | Low |
| Results calendar | Exchange | Daily | Low |
| Corporate actions (demergers) | Exchange corp-action feed | Daily | Low |
| Sector capacity aggregate | Computed from L1 universe gross block + industry bodies | Quarterly | Medium |
| HS-code import volumes/unit values | Ministry of Commerce trade data | Monthly | Medium — *deferred* |

### 3.2 Exchange strategy — BSE primary, NSE redundant

**BSE is the primary feed, not NSE.** A meaningful share of the microcap universe is BSE-only listed, and BSE has no equivalent of NSE's cookie/session gate.

BSE pages (all Angular-driven — an `api.bseindia.com` JSON endpoint sits behind each; capture from the network tab):

| Feed | Page |
|---|---|
| Announcements (has "Insider Trading / SAST" category filter) | `bseindia.com/corporates/ann.html` |
| PIT disclosures | `bseindia.com/corporates/Insider_Trading_new` |
| SAST | `bseindia.com/corporates/Sast.html` |
| SAST — system-driven (depository-sourced) | `bseindia.com/corporates/Regulation_29.aspx` |

**Own crawler, but take the endpoint map and response shapes from `BseIndiaApi`** (`pip install bse`, github.com/BennyThadikaran/BseIndiaApi). Reference material only — do not take a runtime dependency:

- `src/samples/` — committed JSON responses per method. Use these as parser fixtures and to define your schema without live calls.
- `src/examples/get_all_announcements.py` — the announcements pagination contract.
- `getScripCode()` — symbol → BSE scrip code mapping logic.

Saves reverse-engineering; the library's own coverage decisions are not binding on you.

**Caveat on `Regulation_29.aspx`:** BSE restarted the 2% / 5% change calculations from **1 March 2022** as base. Pre-that-date history requires the directly-filed promoter disclosures, not the system-driven feed. Only matters if backfilling.

**NSE — keep as second source, change the access mode.** Flakiness is the session gate on `www.nseindia.com/api/*`, not the data.

- Prefer `nsearchives.nseindia.com` — plain static file server, no cookie gate. Anything reachable as a dated archive file or via the page's **Download (.csv)** control takes this path.
- `nsearchives.nseindia.com/content/equities/EQUITY_L.csv` — full listed universe with **ISIN**. Foundation of the symbol map.
- For API-only feeds: warm a session against the homepage, persist cookies, jitter, retry. One daily batch with 3 attempts over ~20 min makes reliability a non-issue.

### 3.3 Rating agencies

Exchange feed detects the *event*; the agency site supplies the *content*. Design accordingly — **detection must be reliable, enrichment may fail silently and retry tomorrow.** This removes the need for seven robust agency crawlers.

**India Ratings** (JS-rendered; `/raclisting/rac/all` returns an empty shell — use the XHR directly):

```
Listing: https://www.indiaratings.co.in/pressReleases/GetListing_RAC
         ?type=rac&year=&pageNo=<n>&searchText=
Detail:  https://www.indiaratings.co.in/pressrelease/<urlKey>/<issuerName_slug>
```

Paginate the listing; store `urlKey` as the stable detail identifier and construct the detail URL from it. Expect the same shell-plus-XHR pattern on CRISIL, ICRA, CARE, Acuité — find each listing endpoint the same way rather than parsing DOM.

Agencies to cover: CRISIL, ICRA, CARE, India Ratings, Acuité *(+ Brickwork, Infomerics if microcap coverage warrants)*.

---

## 4. Schema requirements (retrofitting these is painful)

- **ISIN is the primary key, never symbol.** BSE scrip code and NSE symbol are different keys for the same company. Source ISIN from `EQUITY_L.csv` and BSE responses; maintain a `security_master` mapping `isin → {nse_symbol, bse_scrip_code, name}`.
- **One normalised `events` table, `source` as a column.** Every disclosure lands on both exchanges — treat that as redundancy, not duplication. Dedupe on `(isin, filing_type, disclosure_date, quantity)`; keep the **earliest** `announcement_timestamp` across sources. Either exchange being down for a day then costs nothing.
- Store **announcement timestamp separately from exchange filing date**.
- Rating events carry `detection_source` (exchange) and `enrichment_status` (pending / fetched / failed) separately, so a failed agency crawl never suppresses the event.
- **Version every signal definition.** A backtest must reproduce what a rule was on a past date, not what it is now.
- Append-only state history — never overwrite a quarter's L2 vector.
- Log every L1 rejection with reason. You cannot backtest what you never recorded.

---

## 5. Outcomes to measure

### Primary — forecast accuracy, not returns

Terminal returns give ~6–8 independent observations per year against a heavily right-skewed distribution. Statistically unfalsifiable within a decade. **Do not attempt to measure signal alpha directly.**

Instead, score the L4 fundamental predictions quarterly, independent of price:

| Metric | Definition | Frequency |
|---|---|---|
| Forecast hit rate | % of dated fundamental predictions that resolved true | Quarterly |
| Forecast calibration | Predicted vs actual magnitude, by signal type | Quarterly |
| Failure attribution | `thesis_wrong` vs `thesis_right_market_hasnt_paid` — these look identical in P&L and demand opposite corrections | Per resolution |
| Time-to-confirmation | Quarters from entry to first prediction resolving true | Per position |

Target: 60–100 scored forecasts over two years vs ~15 terminal outcomes.

### Secondary

- Return distribution by `origin_tag` (systematic vs ad hoc) — directional in ~12 months, never significant
- L1 flag efficacy — did flagged-and-excluded names underperform?
- Recall check: quarterly, manually review 10 random L2-primed names that fired no alert. Would you have wanted them?
- Realised slippage vs ADV assumption at entry and exit

### Not to be measured

Signal-level Sharpe, IR, or hit rate at the individual-signal level. Sample size makes these noise that will read as evidence.

---

## 6. Build order

0. **Security master** — `EQUITY_L.csv` + BSE scrip codes, keyed on ISIN. Half a day, and everything else joins through it.
1. **Deleveraging screen** — highest signal-to-effort, ~90% screener-derivable, arithmetic mechanism. Validates the pipeline end to end.
2. **L1 fraud/quality filter** — every later signal is worthless without it.
3. **L2 state store + versioning + append-only history.**
4. **Four L3 feeds** — BSE crawler first, NSE archive second, then rating detection + enrichment.
5. **L4 thesis register + quarterly forecast scoring.**
6. Sector capital-cycle aggregation.
7. *(Conditional)* HS-code mapping table.

---

## 7. Standing constraints

- Expect to be early by 12–18 months and to sit through 40% drawdowns on correct theses.
- Most turnaround and capital-cycle theses fail. The distribution is carried by a handful of outcomes.
- Position sizing and pre-committed invalidation matter more to the outcome than signal quality.
- The pipeline is a **coverage and record-keeping tool**, not an alpha source. Judge it on recall and falsifiability.

---

*Not financial advice. Small/microcap valuations have compressed the margin of safety these approaches depend on.*
