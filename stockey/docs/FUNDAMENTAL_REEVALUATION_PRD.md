# PRD: story-and-flaw scoring, event-driven re-evaluation, weighted portfolio

Status: **agreed with the operator 2026-09-29, not built.** Covers everything after the
universe (docs/UNIVERSE_PRD.md): events → scoring → watchlist → portfolio. Supersedes the
confluence score's role (fundamentals/screens/confluence_score.py) and the entry/exit
rules of docs/PORTFOLIO_RULESET_PRD.md where they conflict; every rule change ships as a
new ruleset version with its own forward record, never an edit of a running one.

## 1. Why

Measured on the live pipeline (2026-09-29):

- **Positions churn.** 11 closes after an average of 10 days held, 9 of them "thesis
  invalidation", against forecasts of 60-365 days. RANEHOLDIN was entered and exited twice
  in two weeks. Cause: an exit fires when ANY confluence axis flips to contradicting, and
  the axes flicker -- the valuation axis flips when a stock rises ~10%, closing winners
  for winning. Stops of 5-15% are tight for a 1-2 year holding.
- **Entries need almost no evidence.** Rule: at least one known axis, none contradicting,
  Weinstein stage 2. 11 of 34 positions entered on a single supporting axis.
- **Evidence is binary and thin.** A 10% results beat counts the same as 60%. The
  "fundamentals trajectory" axis reads only debt and CWIP -- not revenue growth, margins or
  profit trend, although every results filing is already extracted. Sector is unknown for
  63% of scored names, ownership for 60%.
- **Candidates come only from events.** 1,295 universe names with daily quality / value /
  growth data (TODO C4), but a company is considered only after an alert fires.
- **LLM flags are the largest source (72 of 132 active watchlist names) and unmeasured:** a
  yes/no with no direction, size or thesis, and outside the event-drift record.
- **The portfolio is equal-weight** with no conviction, risk, sector or replacement logic.

## 2. Principles

1. **Markets pay for one story and punish one flaw.** A company rarely re-rates for being
   decent at everything. Score the best dimension and the worst, not the average.
2. **Change beats level; acceleration beats change.** Margins 12% → 16%, growth 10% → 25%,
   ROCE turning up -- inflections re-rate stocks.
3. **Let the market say which stories and flaws matter** -- learned from price reactions,
   per sector, over time; not weights we guess.
4. **Anything new re-evaluates exactly what it touches** -- a filing, a peer's result, sector
   news, a price move -- quickly, without recomputing everything.
5. **Act slowly, see fast.** Scores update within the hour; portfolio trades are decided
   once a day, with hysteresis, so noise does not become turnover.
6. **Point-in-time and measured.** Every score is stored dated; every component is judged by
   a forward record before it earns weight.

## 3. Scoring: story and flaw

### 3.1 Dimensions and readings

Per company, within its universe group (lenders compared with lenders):

| Dimension | Examples of inputs |
|---|---|
| Growth | revenue and profit growth (quarter and year), from results extraction and the snapshot |
| Margins / profitability | operating and net margin, ROCE / ROE |
| Balance sheet | debt/equity, interest cover, cash flow, pledge |
| Value | earnings yield, EV/EBIT, P/B -- vs own history and sector |
| Ownership | promoter and institutional change, insider buying, bulk deals |
| Sector | capacity cycle, peer results, sector news |
| Events | recent positive vs negative filings, sized and decaying |
| Governance (flaw only) | auditor change, material related-party transactions, pledge rises |

Each dimension gets three percentile readings within the group: **level, change, and
acceleration** (change of change). A dimension's best reading is its "story strength", its
worst its "flaw depth".

### 3.2 Composite -- not an average

- **Story score** = the strongest story reading across dimensions (with a small bonus if a
  second story is also strong). A top-5% reading in one dimension makes a strong candidate
  on its own.
- **Flaw cap**: a deal-breaker reading in a dimension the market punishes (defaults: cash
  burn, leverage for its group, governance flags) caps the score regardless of stories.
- **Output per company per day:** story score, primary story (dimension + reading), flaw
  (if any), and the full dimension vector -- stored dated (`fundamentals_story_score`).

### 3.3 Which stories and flaws count -- learned from the market

Every results filing is a natural experiment: record how much each dimension's reading
changed and the price reaction over the next few sessions against the market (from
detection time, TODO C4). A rolling, per-sector model of "which dimension changes move
prices" sets which dimensions count as stories and which as deal-breakers, and how strong a
reading must be. Until enough filings accumulate, defaults: growth acceleration and margin
expansion are stories; cash burn and leverage are breakers.

### 3.4 LLM story read

For companies whose score changed materially (not all 1,300 daily): one structured read --
the one story, the one deal-breaker, direction, materiality 1-5, horizon, the key numbers.
Catches what numbers miss (a new product, a regulation). Stored, and measured forward like
every other component. Replaces the current yes/no `llm_flagged` triage.

### 3.5 Results reading (feeds Growth / Margins)

Instead of ±10% / +15% YoY on the better of revenue or profit: growth against the company's
own trailing trend (accelerating / slowing), margin change, exceptional items stripped out.
The extracted fields already exist (results schema v3).

## 4. Event-driven re-evaluation

### 4.1 What triggers it, and for whom

| Trigger | Dimensions touched | Companies re-scored |
|---|---|---|
| Company's own filing (results, rating, insider, capital raise, order, management, litigation) | by type: results → growth, margins, value; rating → balance sheet; insider → ownership | that company |
| Peer filing (results, capex, order, guidance) | sector (read-across) | same-sector companies whose story or flaw is sector |
| Sector aggregate change (capacity cycle, median margin / growth trend) | sector | same |
| Economic Times news (hourly, `fundamentals_news_item`) tagged to a sector or company | sector, or the tagged dimension | the tagged company; sector-sensitive companies in the sector |
| Daily market data (price, bulk deals, pledge) | value, ownership | all (cheap) |
| Quarterly shareholding | ownership | that company |
| Rates / macro (RBI, FBIL) | balance sheet (funding cost) | lenders, rate-sensitive, and those whose flaw is leverage |

Rule: a trigger re-scores a dimension for a company only where that dimension is the
company's story or flaw (or it is the company's own filing); everything else gets the cheap
daily refresh. The same holds for every dimension, not only sector.

### 4.2 How it runs

- **Queue:** `fundamentals_reeval_queue` (company, dimension, cause, enqueued_at). Written
  by every collector, the news tagger and the daily market job.
- **Scorer job every 30 minutes** (like OCR): recomputes only queued dimensions, writes a
  dated score row, compares with the previous one.
- **Material change → event:** a story strengthening or fading, a flaw appearing, or a
  company entering / leaving the watchlist band -- with a hysteresis margin so small moves
  do not flicker. Materiality thresholds start as defaults and are learned (3.3).
- **Intraday filings:** the market-wide BSE announcements fetch (already one day per call)
  runs every 30 minutes for today; OCR and extraction are already continuous. Target: a
  filing scored within ~1 hour.
- **News tagging:** each news item is matched to companies (names / tickers) and sectors;
  the LLM classifies only items that match a universe company or carry a sector hint.
- **LLM cost control:** model calls only for material filings and matched news.

### 4.3 What a change leads to

- **Watchlist:** updated immediately -- enter on a strong story without a disqualifying flaw
  (or an event with an above-median score); flag or exit when the story fades or a flaw
  appears.
- **Portfolio:** decided once a day at the close from the day's score changes (section 5).

## 5. Portfolio

### 5.1 Stop the churn (first -- ruleset v2)

- **Exit only on hard contradictions:** results decline, rating downgrade, pledge increase,
  auditor change, insider selling surprise -- or a flaw appearing (4.3). A soft axis must
  contradict on 3 consecutive runs before it counts.
- **Valuation is never an exit reason** -- an entry filter only.
- **Minimum hold 20 sessions**, except a stop or a hard event.
- **Trend exit only on stage 4 (declining)**, not merely "no longer stage 2".
- **Wider stops for the long-term bucket.**
- **Entry needs at least 2 supporting axes** (PORTFOLIO_RULESET_PRD open item), until the
  story score replaces the axes.

### 5.2 Weighted construction (ruleset v3, on the story score)

- **Weight ∝ conviction (story score) × risk scaling (1 / volatility)**, normalised.
- **Caps:** ~2-8% per name, sector ≤ ~25%, and the existing 10%-of-ADV liquidity cap.
- **Replacement when full:** a candidate replaces the weakest holding only if its score is
  higher by a margin (hysteresis).
- **Rebalance bands:** trade only when a position is well away from its target weight.
- **The LLM adjudicator keeps its role** (may only veto / defer), and the paired accepted vs
  vetoed comparison continues.

### 5.3 Exits (ruleset v3, with 5.2)

Exit checks run daily. Through v2 the stop LEVEL is set once at entry (a fixed percentage
below the entry price) and never moves. v3 separates the reasons to sell, each with its own
rule (agreed with the operator 2026-09-29):

| # | Reason | Rule | Status |
|---|---|---|---|
| 1 | Thesis broken | a flaw appears, or a hard negative event since entry: exit at once | built (v2) |
| 2 | Story fading | story score below its band on 3 consecutive runs: exit after adjudicator review | needs the story score (step 2) |
| 3 | Wrong early | initial volatility-scaled stop, 10-25% | built (v2) |
| 4 | Protect a winner | once in profit, a TRAILING stop: highest close since entry minus ~3x the stock's normal (20-day, vol-scaled) move, only ratchets up; after a gain of 2x the initial stop it never sits below break-even. Generous on purpose -- systrader found tight price stops hurt its momentum books | v3 |
| 5 | Too expensive | never a full exit: when valuation is ~2x the stock's own history (e.g. P/E vs 5-year) and the story is not strengthening, trim a third; keep the rest while the story holds | v3 |
| 6 | Something better | replace the weakest holding only when a candidate scores clearly higher (5.2) | v3 |
| 7 | Time | target date passed without the thesis confirming: adjudicator review, may defer once | built |
| 8 | Tax guard | a NON-urgent exit (2, 5, 6, 7) within ~30 days of the 1-year mark waits until the gain is long-term; hard exits (1, 3, 4) never wait | v3 |

**Counterfactual tracking (v3):** every exit and trim records what the stock did over the next
60 sessions as if held, so each rule's worth is measured (did it save money or cut winners?).
Stop variants (fixed vs trailing) can run side by side on shadow positions before one is fixed.
Exact multiples above are defaults, set from that evidence, never tuned on returns in advance.

## 6. Measurement

- Forward records by score band and by component, and by **story type** (growth inflection,
  margin expansion, turnaround, value re-rating, ...), using the forward-track and
  event-drift machinery (TODO C6-C8, C7). After ~12 months, component weights and
  story / flaw definitions are re-set from that evidence, pre-registered.
- Price-reaction study (3.3) runs continuously from 2026-09-27 (detection times exist from
  C4).
- Every ruleset version is recorded alongside the previous one; nothing is judged on less
  than its pre-registered window.

## 7. Build order

1. **5.1 churn fixes** -- ruleset v2, recorded beside v1. **Done 2026-09-29** (portfolio_ruleset / portfolio_exit, tests/test_portfolio_v2.py).
2. **Dimension readings (3.1) + story / flaw score (3.2)** with defaults, dated, daily. **Done 2026-09-29** --
   `fundamentals/screens/story_score.py`, daily pipeline step after llm_triage, table
   `fundamentals_story_score` (tests/test_story_score.py). As built:
   - Readings: growth (5y sales/profit level; quarter-YoY minus 5y as acceleration),
     profitability (5y ROCE/ROE level; now minus 5y as change), balance sheet (low D/E,
     interest cover, FCF yield), value (earnings yield, low positive EV/EBIT, cheap vs own
     history; low P/B for financials only), events (decayed net), plus categorical ownership /
     governance strengths. Margins and sector have no reading yet (step 3 / step 4).
   - Percentile within group is the midpoint (rank - 1/2)/N, so topping a small group reads
     below 100. Story = 100 x p_best^n (n = the company's reading count: net of the luck of
     many tries), and a second story closes 25% of the remaining gap -- no clamp at 100.
   - Guards: returns above 100% are one-offs and void the 5y average that contains them;
     acceleration needs quarterly sales >= Rs 50 cr.
   - Flaws cap at 40: cash burn with losses, 2 loss years (both waived for Layer 2's
     scaling-growth / turnaround admits), leverage >= group p90 with cover < 2, auditor change
     or RPT flag in 365 days, pledge >= 50% or rising in 180 days.
   - First run: 1,295 scored, median 35, 47 at >= 95, 40 capped by a flaw. A record only
     until the watchlist moves onto it (step 6). Known limit: one-off gains inside profit
     growth are not stripped until results reading (step 3).
3. **Results reading (3.5)** feeding Growth / Margins.
4. **Re-evaluation queue + scorer job (4.2)**, intraday filings, news tagging.
5. **LLM story read (3.4)**, replacing yes/no triage; into event-drift.
6. **Watchlist on the score (4.3)**; retire the confluence count.
7. **Weighted portfolio (5.2) and exits (5.3)** -- ruleset v3: trailing stop, valuation trim,
   replacement, tax guard, counterfactual tracking; story-fading exit once step 2 exists.
8. **Price-reaction learning (3.3)** once enough filings have accumulated; then **6**.

## 8. Decided / open

Decided (operator, 2026-09-29): story-and-flaw scoring instead of a weighted average;
change and acceleration beside level; learn what the market pays for from price reactions;
LLM story read; event-driven re-evaluation for every dimension; Economic Times RSS hourly
as the news source (built: `fundamentals/collectors/et_news.py`); weighted portfolio; churn
fixes first; the v3 exit framework (5.3).

Open: intraday cadence for scoring (hourly target, portfolio stays daily); exact caps and
bands in 5.2; how long the price-reaction model needs before it replaces the defaults.

Boundary: all of this is fundamental analysis inside stockey's fundamentals carve-out. The
Weinstein stage stays systrader's (read via its API); stockey still authors no price signal
-- price enters only as the market's reaction used to LEARN what fundamentals matter, and as
risk scaling in sizing.
