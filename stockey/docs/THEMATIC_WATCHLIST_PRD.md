# Thematic watchlist PRD -- real sectors, money flowing in, momentum inside

**Status: DRAFT 2026-10-26, operator decisions folded in the same day. Nothing built.**
Forward-looking: a ranked watchlist, not a tested strategy -- no backtest, no pass/kill
judgement (section 6).

**Operator decisions (2026-10-26):** government support is one of the four strength parts,
not a requirement; no judging -- build a watchlist and rank the companies; 5 themes, 10 stocks each
(2026-10-26, second answer); the four company ranks shown and counted equally.

## 1. The problem

Long-term investors pick a sector first and a company second. Our sector labels (NSE
industry, `fundamentals_industry_classification`; Sharpely) are the wrong map for that:

- **Too old.** NSE's basic-industry list has no "data centres", "solar cells", "EMS",
  "chip packaging" or "defence electronics". New businesses land in catch-alls
  (Electrical Equipment, IT Services, Other Industrial Products).
- **One label per company.** A cable maker that now earns a third of its revenue from solar
  EPC, or an IT firm building data centres, keeps its old label for years.
- **Measured by our own tests.** RS-leading NSE industries plus Stage 2 lost to plain Stage
  2 (LEDGER row 53, `industry-rotation-failed`). One open question is whether that failed
  because rotation does not work here, or because the labels were wrong. This PRD tests the
  second answer.

The proposal: our own list of **themes** (real end markets, e.g. solar cells, data
centres), each company tagged to the themes it actually earns from, a **theme strength**
score built from government support, capacity being built, results and price breadth, and a
**ranked watchlist** of the companies in the few strongest themes.

## 2. How others do it (research, 2026-10-26)

### 2.1 Index providers: revenue exposure, found by text, checked by people

| Who | How a company gets into a theme | Notes |
|---|---|---|
| **MSCI thematic indexes** | NLP generates keyword phrases per theme; matched against each company's reported business segments. A segment whose name matches counts 100%, others are discounted by keyword density (example factor 0.45); the sum of revenue share is the **relevance score**. Index needs score >= 25%, removes companies in unrelated GICS sub-industries as false positives, caps an issuer at 5%. Keywords reviewed yearly. | [Relevance score methodology](https://www.msci.com/eqb/methodology/meth_docs/MSCI_Relevance_Score_Calculation_Methodology.pdf), [Thematic relevance 2022](https://www.msci.com/indexes/documents/methodology/2_MSCI_Thematic_Relevance_Score_Methodology_20220519.pdf), [How MSCI builds thematic indexes](https://www.msci.com/documents/1296102/43829639/How+MSCI+Builds+Thematic+Indexes.pdf/2e437bbd-05a1-ac8f-5146-b8f3bff0d6f2) |
| **MSCI, LLM version** | An LLM screens for exposure; "opposing agents" argue whether each business line belongs to the theme; **the exposure number is arithmetic on revenue shares, the model only judges association.** | [MSCI blog](https://www.msci.com/research-and-insights/blog-post/leveraging-language-models-to-capture-investment-strategies) -- the design we should copy |
| **Solactive (Global X)** | FactSet RBICS revenue classification: >= 50% of revenue from the theme; survivors ranked by how often theme keywords appear in company documents; top N. | [Solactive press release](https://www.solactive.com/wp-content/uploads/2021/09/Press-Release-Global-X-ETFs-Releases-Two-Thematic-ETFs-on-a-Single-Day-Giving-Investors-Exposure-to-Clean-Energy-2.pdf) |
| **Indxx** | Usually 50% of revenue, lowered for young themes; fills a quota with 20-50% names when too few pure plays exist. | [Indxx on methodology](https://wealthprofessional.ca/news/industry-news/why-you-should-look-under-the-hood-of-your-thematic-index/360088), [subscription index](https://www.etfstrategy.com/indxx-targets-the-subscription-economy-with-new-thematic-index-98547) |
| **NSE (Nifty India Defence)** | From Nifty Total Market: an eligible basic industry **or >= 10% of revenue from defence**; free-float weights, 20% cap. NSE thematic indices otherwise lean on its own industry list and prefer F&O stocks. | [Nifty India Defence](https://niftyindices.com/indices/equity/thematic-indices/nifty-india-defence), [factsheet](https://www.niftyindices.com/Factsheet/Factsheet_NiftyIndiaDefence.pdf), [thematic methodology](https://www.niftyindices.com/Methodology/Method_Nifty_Thematic_Indices.pdf) |

**Takeaways.** (a) Everyone serious measures **revenue share**, not a single label. (b)
Text finds candidates; a revenue number decides. (c) Thresholds range from 10% (NSE
defence) to 25% (MSCI) to 50% (Solactive/Indxx); young themes need a lower bar because
few companies are pure plays yet. (d) The theme list itself is reviewed yearly.

### 2.2 Academic work on better industry maps

- **Hoberg-Phillips text-based industries (TNIC):** industries rebuilt every year from the
  similarity of firms' 10-K product descriptions; they explain profitability, valuation and
  dividends better than SIC/NAICS. Evidence that a text map beats a fixed code list.
  [Data library](https://hobergphillips.tuck.dartmouth.edu/), [NBER w15991](https://www.nber.org/papers/w15991.pdf)
- **LLM classification:** PTEC (NAACL 2024) assigns companies to an investor's own
  multi-label taxonomy with a classification head (no invented labels, gives confidence),
  beating human labellers ([arXiv 2309.12075](https://arxiv.org/pdf/2309.12075)). THEME
  (CIKM 2025) fine-tunes embeddings so themes retrieve their stocks, then adjusts for
  returns ([EconPapers](https://econpapers.repec.org/paper/arxpapers/2508.16936.htm)).

### 2.3 Does buying themes / hot industries make money?

| Evidence | Finding | Source |
|---|---|---|
| Industry momentum (Moskowitz-Grinblatt 1999) | Industry momentum explains much of stock momentum in the US and is profitable on its own. Replications are mixed outside the US (Canada no, Europe weaker). | [JF 1999](https://ideas.repec.org/a/bla/jfinan/v54y1999i4p1249-1290.html), [AQR](https://www.aqr.com/Insights/Research/Journal-Article/Do-Industries-Explain-Momentum?aqrPDF=1) |
| Specialized/thematic ETFs (Ben-David et al., RFS 2023) | Lose about **30% risk-adjusted over their first 5 years**: they are launched on stocks already hyped and overvalued. | [NBER w28369](https://www.nber.org/system/files/working_papers/w28369/revisions/w28369.rev2.pdf), [summary](https://alphaarchitect.com/competition-for-attention-in-the-etf-space/) |
| Morningstar thematic funds | Only ~9% survived AND beat a global index over 15 years to mid-2024; funds launched 1996-2018 trailed MSCI ACWI IMI by 3.3 pp/yr in their first two years; half closed within 15 years. | [Capital Group summary](https://www.capitalgroup.com/advisor/ca/en/investments/exchange-traded-funds/resources/thematic-etfs--3-things.html), [High burnout rate](https://www.morningstar.com/etfs/high-burnout-rate-thematic-funds) |
| Capital cycle (Marathon, *Capital Returns*) | Sectors with heavy investment and new entrants do badly later; favour sectors where capital is leaving. | [Book notes](https://samvitpatel.substack.com/p/capital-returns), [MOI](https://moiglobal.com/lattice-work-capital-cycle-theory) |
| Asset growth (Cooper-Gulen-Schill 2008) | Low-asset-growth stocks beat high-asset-growth stocks by a wide margin for up to five years. | [summary](https://alphaarchitect.com/?p=1696) |
| Economic links (Cohen-Frazzini 2008) | Prices are slow to absorb news about customers; customer momentum predicts suppliers. Weaker after publication. | [Customer momentum replication](https://ar5iv.labs.arxiv.org/html/2301.11394), [FactSet](https://insight.factset.com/propagating-momentum-information-through-global-supply-chain-networks) |
| India example | HDFC Defence Fund (launched Jun 2023): ~43%/yr to Jun 2026 (one outlet's figures), but fell ~35% from its Jul 2024 peak to early 2025. | [DSIJ](https://insights.dsij.in/dsijarticledetail/rs-10-lakh-to-rs-29-lakh-in-3-years-defence-mutual-fund-rewards-investors-with-nearly-3x-returns-57983), [Angel One](https://www.angelone.in/news/mutual-funds/thematic-funds-hdfc-defence-mutual-fund-jumps-14-percent-invesco-psu-fund-gains-in-march) |

**What this means for us.**
1. **Buying a theme because it is popular loses money.** The ETF evidence is the base rate:
   by the time a theme is a fund, it is priced. So the ranking must lean on **price
   trend (momentum) and fundamentals arriving**, not on the story.
2. **Capacity building is double-edged.** Early it is the signal (orders, plants, subsidy);
   late it is the warning (capital cycle, asset growth). Solar modules are the live Indian
   case: ALMM-listed module capacity is ~228 GW (Sep 2026) against cell capacity of only
   ~32-35 GW -- a glut building in modules, a shortage in cells
   ([Energetica, Sep 2026](https://www.energetica-india.net/news/almm-update-solar-module-capacity-surpasses-228-gw-in-september-2026),
   [CEEW on ALMM](https://ceew.in/gfc/quick-reads/analysis/what-is-almm)). The same theme
   can be "early" in one layer and "late" in the next, so themes must be fine-grained.
3. **Exits matter more than entries.** Themes peak violently (defence -35%). A theme-level
   trend exit is required, as the market-trend rule was for the Capitalmind replica
   (LEDGER 83).

## 3. Goals and non-goals

**Goals**
- G1. A versioned **theme map**: a fixed list of themes, and each company's revenue share in
  each, point-in-time (dated), with the evidence quoted.
- G2. A daily **theme strength** per theme: policy, capacity, fundamentals, price breadth.
- G3. A **ranked thematic watchlist**: the top few themes, every liquid member ranked.
- G4. A screener page: themes, their strength and its parts, members with exposure and rank.

**Non-goals**
- Not a backtest-first project: the theme list is written with today's hindsight (section 6).
- No discretionary theme calls: the operator adds or removes themes only through a new
  map version, dated, never mid-version (Law 19 spirit).
- No portfolio and no performance judgement in this PRD. If a book is ever built from the
  watchlist, it gets its own pre-registration first.

## 4. Design

### 4.1 The theme list (v1, ~25 themes)

Each theme: id, one-paragraph definition (what a company must make or sell), keyword seeds,
value chain layer (upstream / midstream / end product / service), and named public data
for capacity and policy. Draft list for the operator to edit:

| Area | Themes (layers split where the economics differ) |
|---|---|
| Energy transition | solar wafers/cells; solar modules; solar EPC/IPP; wind components; battery cells/storage; pumped storage/hydro; green hydrogen/electrolysers; power T&D equipment (transformers, cables, conductors) |
| Digital infrastructure | data centres (owners, builders); data-centre equipment (cooling, power backup); telecom equipment; electronics manufacturing (EMS/ODM); chips (OSAT, design) |
| Make in India / state capex | defence platforms; defence electronics; railways (rolling stock, signalling); shipbuilding; drones; water infrastructure |
| Consumption shifts | quick commerce/logistics; travel and hospitality; organised jewellery; alcobev premiumisation |
| Health/industrials | CDMO/CRAMS; medical devices; specialty chemicals for export |

**Operator, 2026-10-26: keep all 26 as v1.** Rule for a theme to exist: at least 5 listed companies above a 10% exposure, and a named
public source for at least one of capacity or policy.

### 4.2 Tagging companies to themes (copying MSCI's LLM design)

1. **Candidates by text.** Embed each company's business description, segment names and
   recent filings; take the companies nearest each theme's keyword seeds.
2. **Association by an LLM, exposure by arithmetic.** For each candidate, the LLM reads the
   evidence and answers only: which reported segments / product lines belong to the theme,
   with a quoted sentence. The **exposure %** is computed from reported segment revenue (Ind AS
   108 segment results filed with quarterly results), or, where the company reports one
   segment, from disclosed product/revenue mix (annual report, investor presentation, order
   book disclosures). If no number exists, exposure is "unquantified" and the company is a
   watch-only member, never ranked.
3. **Two-agent check** (as MSCI's opposing agents): a second prompt argues against each tag;
   disagreements go to an exceptions list on the page, not silently resolved.
4. **Thresholds:** member at >= 20% exposure; "pure play" at >= 50%; young themes may use
   10% (as NSE defence), set per theme in the map, frozen per version.
5. **Point-in-time:** every tag has `known_from` = the filing's publish time. Re-tag on each
   new quarterly result, annual report or investor presentation. A company can enter a theme
   as its business shifts; the history is kept.

**Evidence on hand (checked 2026-10-26).** No stored business descriptions or segment tables
yet. What exists: OCR'd quarterly results PDFs for 1,321 companies (multi-segment companies
print a segment revenue table in them) and OCR'd credit-rating rationales for 555 (these
describe the business and often its revenue mix). Missing, to collect in step 2: a short
business description per listed company (screener.in "About" / "Key points", already behind
our login), and for theme candidates only (~1,000 companies) the latest annual report's
segment note or investor presentation from the BSE/NSE filings. The LLM's own knowledge may
nominate candidates but never sets an exposure.

### 4.3 Theme strength (four parts, each a rank across themes)

| Part | Measure | Our data | New data needed |
|---|---|---|---|
| **Policy** | Live schemes and their size: PLI approvals/disbursements, budget lines, duties/ALMM-type protection, tenders | ET news (hourly), BSE/NSE filings | A hand-kept policy table per theme (scheme, date, size, source), versioned |
| **Capacity** | Members' capex announcements, capital raises, gross block growth, order book growth | `capital_raise` L3 triggers, `sector_cycle.py` gross block vs sales, filings OCR | Order-book extraction from results/presentations; public capacity lists (MNRE ALMM for solar; CEA for power; MeitY/industry for data centres) |
| **Fundamentals arriving** | Members' revenue and margin growth vs their own trend (results reading), weighted by exposure | `results_reading.py`, story score | none |
| **Price breadth** | Share of members (exposure-weighted) in Stage 2 / above their 200-day average; theme index trend | systrader stage API and paper engine | a theme index series computed in systrader from the map |

**The capital-cycle brake.** A theme is marked **late** when capacity growth runs well ahead of
members' sales growth (gross block or listed capacity growing faster than revenue for 2+
quarters) **or** member margins fall two quarters running while capacity rises. Late themes
cannot be bought; holdings in them exit on the next review. This is the solar-modules case.

### 4.4 The watchlist (fewer themes, deeper)

- **Themes shown: the top 5 by strength**, excluding late ones. Themes ranked 6-8 are listed
  as "next up" with their strength only, so a theme rising into the top 5 is visible early.
  A theme leaves the list when it drops out of the top 8 or turns late (a buffer, so the list
  does not flip monthly).
- **Universe: NSE and BSE** (operator, 2026-10-26). A company listed on both is one entry;
  liquidity is its combined 60-day median traded value >= Rs 1 cr. BSE-only names use the BSE
  bhavcopy and systrader's `BSE:<scrip>` stage keys; they are marked "BSE only" on the page.
- **Management gate (hard, before ranking)** -- a company is left out, with the reason shown
  under "excluded", if in the last 5 years it or a promoter/director is named in: a SEBI, SFIO,
  ED or RBI enforcement order; a fraud or forensic-audit disclosure; an auditor resignation or
  a qualified/adverse audit opinion; a default or a rating cut to D; promoter pledge > 10%; or
  a stockey story flaw. Sources: story flaw, `auditor_change` and rating triggers, shareholding
  (pledge), filings and ET news (already collected); **new: a SEBI orders collector**
  (sebi.gov.in enforcement orders, matched to companies and promoter names) -- the one new
  data source this PRD needs for the gate.
- **Members: the top 10 per theme** by the company rank below, from members passing the
  liquidity floor and the management gate, with a quantified exposure at or above the theme's
  threshold. The rest of the members sit behind a "show all"; unquantified members appear
  greyed, unranked.
- **Company rank within a theme**, the plain mean of four ranks (equal weight), each shown on the page:
  1. **Exposure** -- share of revenue in the theme (purity).
  2. **Trend and breakout** -- systrader's trend score, adjusted for where the stock is in its
     cycle: an early Stage 2 (broke out of a Stage 1 base recently, near its 52-week high on
     rising volume) ranks above an extended one; Stage 3/4 ranks last. The point is to find
     names breaking out, not ones already far above their base.
  3. **Business** -- revenue and margin growth vs the company's own trend (`results_reading`),
     with return on capital and cash conversion as a floor for "doing good business".
  4. **Capacity / orders** -- order book or capex growth relative to sales.
  (Flawed companies never reach ranking -- see the management gate.) A company in two shown
  themes appears in both, with its exposure in each.
- **Each stock is shown with:** its Weinstein stage now (1 base / 2 rising / 3 top / 4
  falling, with weeks in stage and distance from the breakout level, from systrader); its four ranks and the combined rating (A-E by the combined
  rank's fifth within the theme's members); a short reasoning paragraph (3-5 sentences: what
  the company does in the theme and its revenue share, what the results and orders show, what
  the price trend and stage show); and a one-line conclusion. The text is written by the LLM
  **only from the stored evidence behind the four ranks**, each claim linked to its filing or
  number, and is rewritten only when that evidence changes (not nightly), so it never
  contradicts the numbers beside it.
- **Each theme is shown with:** its strength and the four parts, a short paragraph on why it
  ranks where it does (policy, capacity, results, breadth, with sources), and a one-line
  conclusion -- same rules as the stock text.
- **Refreshed daily** (ranks), theme set reviewed monthly; every change is dated so the
  history of what was on the list, and at what rank, is kept.

### 4.5 Ownership (the stockey/systrader boundary)

- **stockey** owns the theme map, tagging, policy table, capacity and fundamentals parts
  (fundamental + LLM work; `fundamentals/themes/`), and serves `GET /api/themes`.
- **systrader** owns everything price-based: theme index, breadth and the stock trend score,
  served by its API for stockey to read (same pattern as
  `stage2-rs-leaders-clean` reading stockey's flaw filter, LEDGER 55).
- **screener** gets a /themes page.

## 5. Build order

1. **Theme list v1** -- the operator reviews and edits section 4.1. (small)
2. **Tagging pipeline** on the current universe: candidates, LLM association, segment-revenue
   exposure, opposing check; output reviewed on a page before anything else uses it. (medium;
   the main cost is LLM calls, run on the existing CLI budget overnight)
3. **Policy table and capacity sources**, starting with themes that have public lists (solar
   ALMM, power, PLI sectors). (medium, partly hand-kept)
4. **Theme strength** and the late flag, daily. (small)
5. **systrader theme index, breadth and trend scores** for the map's members. (small)
6. **The ranked watchlist + /themes page.** (small)

## 6. Not judged -- but kept honest

- **No backtest and no pass/kill rule** (operator, 2026-10-26). The theme list is chosen in
  2026 with hindsight, so a replay would flatter it anyway.
- **History is kept** so it can be read later: every day's theme strengths, the shown themes
  and each company's rank are stored, dated. Anyone can later ask "how did the top-ranked
  names do?" without having to reconstruct it.
- **Map quality is checked before the list goes live:** a sample of 50 tags hand-checked by
  the operator; target >= 90% agree, else fix the tagging first.
- **First check (2026-10-27): 40 of 50 (80%) -- failed.** The operator asked for the check to
  be done against web sources (company sites, filings, rating rationales, news) rather than
  by hand; an independent web check per tag, shown with its sources on the check page. The
  10 misses: plans counted as business (Reliance batteries, Solex cells), look-alikes and
  side lines (power cables as telecom, agrochemicals as specialty chemicals, HCC water at 3%
  of orders, GOCL after selling its explosives arm, Cupid's 2% diagnostics) and two shares
  set at 100% that were ~85%. Root cause found on the way: screener.in shows Key Points to a
  free account for 20 companies a month, so ~1,640 of 1,712 profiles had only the 2-3 line
  About paragraph (the collector now detects the notice and records it). Fixes: the tagger
  now reads each company's latest annual report (overview, MD&A, segment note;
  `fundamentals/collectors/annual_report_text.py`); prompt v3 counts only current revenue,
  only a main business or named segment, and the opposing check rejects side lines; a share
  under the theme's `min_exposure` is `below_min`. On the 15 affected/control companies all
  8 wrong themes cleared and the controls stayed. The re-check uses a NEW sample (the first
  one has been seen).
- **Second check (2026-10-29, prompt v3, fresh sample seed 27): 45 of 50 (90%) -- meets the
  bar.** Misses: Vikran as railways (0.3% of revenue), Zetwerk as EMS (<= ~9%), HBL as
  battery storage (lead-acid / Ni-Cd; tagged twice because company_master holds it under
  two tickers), and Astra Microwave's share (~78%, not 100%: single-segment reports
  overstate a theme when the segment also holds space/weather work).
- **Every stock on the final watchlist gets a web check** (5 themes x 10 = 50 names), shown
  with its sources: it catches what no filing yet says, e.g. a business sold last month.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Buying the hype (the ETF base rate) | price trend and fundamentals must agree; policy alone never qualifies a theme |
| Capacity glut | the late flag (4.3) |
| LLM tags wrong or invented | exposure from reported numbers only; quoted evidence; opposing check; hand sample |
| Few pure plays, crowded small caps | liquidity floor; exposure threshold; exposure is one of the four ranks |
| Theme list drifts with the operator's views | versioned map; changes only at a new version, dated, with the reason |
| Segment data missing for single-segment companies | "unquantified" members are watch-only |

## 8. Decisions

All answered 2026-10-26: all 26 themes kept; 5 themes x 10 stocks; four ranks equal; no
judging; per-stock and per-theme reasoning with a one-line conclusion; the policy table kept
by the LLM from ET news and filings, source-linked, correctable on the page; NSE and BSE
both; a hard management gate; the stage shown per stock.

**Honest limit:** the ranking looks for companies with clean management, a growing business
and a price breaking out, inside themes with money flowing in. That describes what good
future returns have usually looked like; it does not guarantee them, and nothing here is
tested (section 6).
