# SaaS plan: screener, research and alerts over our data

Written 2026-11-10. **Superseded as the plan by `PLATFORM_PRD.md` (2026-11-13)**; kept for the blocker notes (section 2). Spans all three projects; the scale-out
work it builds on is `stockey/docs/SCALE_OUT_PRD.md`.

**Goal.** A paid web service where people build complicated screens, research companies
(filings, boards, founders, orders, capital raises, themes), test ideas honestly, and get
notified when something they care about happens. Everything we have built becomes a
research and notify tool.

**Picks (decided 2026-11-10).** A SEBI-registered wealth advisor has agreed to partner, so our
picks and portfolios can be published under their registration. Their compliance rules (what
may be claimed, how past performance is shown, disclaimers, records) apply to everything with
a buy/sell tone; the screener, research and alerts stay tool features. A separate brand comes
later; until then we build on what we have.

## 1. What we have to offer

| Area | What exists | Where |
|---|---|---|
| Prices | NSE daily 2013+ (adjusted), delivery %, circuit hits, BSE daily, indices, F&O daily 2016+, 1-min Dhan bars ~5 years | stockey |
| Ownership and deals | bulk/block deals, short selling, promoter/insider (PIT/SAST) disclosures, pledges, surveillance flags | stockey |
| Fundamentals | daily snapshot (ROE/ROCE, D/E, cash flow, valuation), 13 quarters of results, NSE XBRL results 2019+ | stockey (mostly screener.in) |
| Filings | announcements + PDFs + OCR; records with summaries, typed verified facts (orders, preferential issues, stake changes, board changes, ratings ...) and searchable passages | stockey `infra/` |
| Events and text | ET news tagged by company, SEBI orders, credit-rating actions, annual-report text, company profiles | stockey |
| Scores and screens | L1 universe, L2 state, L3 alerts (10 rule triggers + LLM triage), confluence score (5 axes), story score, results reading, sector capital cycle, investor tiers | stockey `fundamentals/` |
| Themes | 26 themes: members with revenue share, strength ranks (policy, capacity, fundamentals, breadth), late flag | stockey `fundamentals/themes/` |
| Price structure | Weinstein stages, relative strength, industry and theme rotation, trend/breakout tiers, liquidity | systrade `internal/stage`, `rotation` |
| Testing engine | daily backtester with costs, matched controls (beta, shuffle, random), walk-forward, deflated Sharpe, FDR, slicer (where an edge lives), capacity at real rupees | systrade `internal/` |
| Research record | 89 ledger rows: what worked and, mostly, what did not (order wins, L1 bids, breakouts, rotation, option selling ...) | systrade `research/LEDGER.md` |
| Portfolios | machine fundamental portfolio, ~15 paper tracks incl. investor-style replicas, E1 options track | stockey, systrade |
| Alerts | SES email (portfolio action mail, ops alerts) | stockey |
| Front end | Nuxt 4 site with 13 pages behind one basic-auth password | screener |
| Scale | orchestrator, Hetzner crawl fleet (max 8), RunPod OCR (max 3) | stockey `infra/` |

**The honest-testing angle is the product's edge.** Indian screener sites show screens and
charts; none tell you that "order wins" or "52-week breakouts" lost money after costs against a
matched control. We have the engine and the record to do that.

## 2. Settle before anyone pays (blockers)

1. **SEBI rules on research and advice.** Selling stock recommendations or research reports for
   a fee needs SEBI Research Analyst registration (or Investment Adviser for personal advice).
   Showing past returns of strategies to the public is also restricted, and SEBI has been
   tightening rules on performance claims and on platforms linked with unregistered
   "finfluencers". A **tool** -- the user writes the screen, picks the alert, asks the question,
   and the answer states sourced facts -- is how screener.in, Trendlyne and Tickertape operate.
   Our own picks, portfolio entries/exits, watchlist "alerts" with a buy tone, and paper-track
   returns cross into recommendations. **Plan: launch as a tool; take a lawyer's opinion on the
   exact line (and on showing backtest results of a user's own screen) before launch; keep
   our portfolios private or behind an RA registration later.**
2. **Data we may not resell.**
   - **screener.in** (scraped with a login) feeds the fundamentals snapshot, quarterly results,
     L1/L2, profiles, and through them the story score, themes and forward tracks. It cannot be
     served. **Replace it with our own fundamentals built from the XBRL filings on NSE/BSE**
     (we already parse XBRL results from 2019) -- the largest single piece of work here. The
     alternative is a paid licence from a data vendor (CMOTS, Accord, ACE Equity).
   - **Dhan** bars come through a personal broker account: internal use only. Public charts
     use NSE end-of-day data.
   - **Sharpely** sectors: replace with NSE's own industry classification (four levels).
   - **NSE/BSE** themselves: showing end-of-day prices and derived numbers publicly may need a
     data licence from NSE (NSE Data & Analytics); read the terms, ask them.
   - Rating-agency text and ET articles: show our summary and the link, not their text.
   - Each served field gets a `licence` tag in the metric catalogue (section 4); the public
     API serves only fields marked clear.
3. **Models for the public.** Claude runs through a personal CLI subscription and cannot serve
   other people. The service needs the Anthropic API with its own key, a budget per user and
   per day, and a queue. The OpenAI extraction step needs the same treatment or a move to Claude.
4. **People and privacy.** Board and founder pages state public facts with their source
   (filings, regulator orders, reported news), no private details. Indian DPDP Act for our own
   users' data: consent, deletion, an India-hosted DB.

## 3. Product

### 3.1 Screener (the core)
- **Metric catalogue**: one place that names every field a screen can use, its meaning, unit,
  history start, update time and licence. Families: price and volume, trend and stage, RS and
  rotation, liquidity, fundamentals (from XBRL), growth, quality, valuation, ownership
  (promoter %, change, pledge), deals, events from filings (e.g. "orders won in the last 90
  days as % of revenue", "promoter bought", "director resigned", "rating downgrade"), themes
  (member, share %), surveillance flags, scores.
- **Screen language**: a small JSON filter language (conditions, AND/OR groups, ranks within
  sector, "changed by", "crossed", lookbacks). Compiled to SQL over whitelisted fields only;
  users never send SQL.
- **Builder UI** plus **"describe it in words"**: Claude turns a sentence into the same JSON,
  shown to the user to edit before running. The JSON is the truth, not the prose.
- **Point-in-time history** for every metric, so a screen can be run "as of" any past date
  without look-ahead (stockey's `fundamentals/pit.py` discipline).
- Saved screens, results table with chosen columns, CSV export (row limits per plan).

### 3.2 Test a screen (honest backtest)
- Rebalance the screen monthly/weekly; hold equal weight; costs included.
- Always shown beside **matched controls**: the same number of random stocks from the same
  universe, and the equal-weight universe. Show the slicer view (where it worked: size,
  sector, year) and the caveats (how many screens this user has tried -- multiple testing).
- Runs as orchestrator tasks with a per-user budget; Go engine from systrade behind a small
  job wrapper (the sleeve backtester takes a list of stocks per date).
- Subject to the legal opinion in 2.1 on how results may be shown.

### 3.3 Research
- **Company page**: price and stage, fundamentals, a timeline of filing facts (orders,
  capital raises, stake changes, board and auditor changes, ratings, litigation), themes,
  news, SEBI orders, every item linked to its filing.
- **Ask a question** about a company (`infra/research.py`): sourced answer, every claim cited.
- **Ask across companies**: Claude turns the question into a query over the facts table
  ("preferential allotments to promoters above Rs 50 crore in 2025"), runs it, explains
  the result with links.
- **People**: directors and founders across companies, their appointments and exits,
  regulator orders naming them (public records only).
- **Themes, rotation and sector cycle** pages as they exist today, rebuilt on licensed data.
- **Research library**: our studies written up for readers, including the failures, as
  education ("we tested trading order-win news; here is why it does not pay"), no
  recommendations.

### 3.4 Notify
- Triggers: a stock enters or leaves a saved screen; a new filing fact of chosen types for a
  watchlist (order above X% of revenue, preferential issue, promoter selling, pledge up,
  director or auditor resigns, rating downgrade, SEBI order); price or stage change; results
  out with a summary.
- Channels: email (SES, already used), Telegram bot, web push. Instant or daily digest.
- Engine: every new fact, screen run or price day emits an event; a matcher compares events
  with users' rules and queues messages with de-duplication and per-user limits.
- Messages state what happened and link the filing; no "buy/sell" wording.

## 4. Architecture

```
 internal pipelines (stockey, systrade, infra)   -- unchanged, write the internal DB
                    |
          serving build (nightly + on new facts)  -- licensed fields only, point-in-time
                    v
   serving DB (separate box, replica)  <---  public API (FastAPI, auth, quotas)  <---  web app (Nuxt)
                    |                                  |
             screen engine (SQL)          job queue (orchestrator): screen backtests,
                                          questions (Anthropic API), alert fan-out
```

- **Separate the public from the pipelines.** Today one server runs the database, every cron
  job and the site. Public traffic gets its own server with a replica of a curated **serving
  schema**; the internal DB and APIs are never reachable from outside (both internal APIs say
  "not for public exposure").
- **Serving schema**: a wide daily metric table per company (point-in-time), the facts and
  records tables, themes, stages, the metric catalogue. A few thousand companies x a few
  hundred metrics a day is small; Postgres is enough.
- **Public API**: new service, not the existing fundamentals API. Users, plans, API keys,
  quotas, request logs.
- **Accounts and billing**: email + Google sign-in through a managed auth provider; Razorpay
  subscriptions (cards, UPI). User data in its own database.
- **Web app**: the existing Nuxt site split into a public part (landing, research library,
  sample company pages) and the signed-in app. Needs a chart library and a component library
  (today everything is hand-written SVG).
- **Costs per user**: Claude questions and backtests are budgeted per plan per month; the
  orchestrator's rate and spend caps stay.

## 5. Build order

| Phase | What | Done when |
|---|---|---|
| 0. Decide | legal opinion (2.1), data plan (2.2), Anthropic API account, name/domain | written answers |
| 1. Data base | XBRL fundamentals to replace screener.in; NSE industry classes; metric catalogue with licence tags; serving schema + replica; crawl all ~2,000 companies' 2-year filings into records | every catalogue field filled for the universe, point-in-time |
| 2. Private beta (free, invite) | accounts; screener (builder + words); company pages; filing-fact timeline; alerts by email/Telegram | 20-50 invited users using it weekly |
| 3. Research | questions per company and across companies (Anthropic API); people pages; research library | answers cite sources, cost per question known |
| 4. Testing | screen backtests with controls and slicer | matches systrade's own results on our known rows |
| 5. Paid launch | plans, Razorpay, limits, terms, privacy policy, support | first paying users |

Phases 2-4 can overlap once phase 1's serving schema exists.

## 6. Plans (first guess, to test in beta)

| Plan | For | Limits |
|---|---|---|
| Free | trying it | 3 saved screens, daily digest alerts, 5 questions/month, no backtests |
| Pro (~Rs 500-1,000/month) | active investors | unlimited screens, instant alerts, 100 questions, 20 backtests |
| Research (~Rs 2,500-4,000/month) | serious / small funds | cross-company questions, more backtests, API access, exports |

## 7. Decisions for the operator

1. ~~Regulatory route~~ -- decided: partner with a registered wealth advisor; picks published
   under their registration and compliance.
2. ~~Fundamentals source~~ -- decided: build our own from company filings.
3. **NSE data licence**: ask NSE now; the answer may change what the free plan can show.
4. ~~Brand~~ -- decided: a separate brand later; build on the current site for now.

## 8. Risks

- Regulation moves (SEBI has been tightening on performance claims and unregistered advice).
- Data terms: one cease-and-desist on a scraped source can blank a feature; hence licence tags.
- LLM cost per user; mitigated by plan budgets and caching answers per company per day.
- Running a public service and the research pipelines on one machine; hence the separate box.
- Look-ahead in backtests if the serving data is not point-in-time; one leak destroys trust.
