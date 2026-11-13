# Research platform: fundamental + technical research, screens, paper portfolios

Written 2026-11-13. Status: agreed scope, nothing built. Replaces `SAAS_PRD.md` as the plan
(that file keeps the blocker notes: SEBI line, data licences). Builds on
`stockey/docs/SCALE_OUT_PRD.md` (crawl/OCR fleet, records) and
`stockey/docs/XBRL_FUNDAMENTALS.md` (our own financials).

**Goal.** One web platform where individual traders, research houses and wealth advisors
research Indian companies and markets, build screens that mix fundamental and technical data,
test ideas honestly, and run paper portfolios on any mix of rules. Everything we have built
(stockey data and fundamentals, systrader's engine and trading bible, the filings records,
macro, news) goes into it.

## 0. Decisions (operator, 2026-11-13)

| Topic | Decision |
|---|---|
| Picks and our portfolios | Published under the partner's SEBI registration (partner details to come). Their compliance rules apply to anything with a buy/sell tone. |
| Data licences | Closed beta shows everything to invited users; licences (NSE display, MCX, others) decided before the open beta. Every field still carries a licence tag from day one so a later decision is a switch, not a rewrite. |
| Mobile | No app. Every page responsive (phone width works, no sideways scroll). |
| Screener | One screener that mixes fundamental, technical, event, ownership and macro conditions. |
| Look | A SaaS product: proper app shell, navigation, tables, charts, empty states, settings. Not the current hand-built pages. |
| Login | Email + password to start (verified email, reset by email). Google sign-in and teams later. |
| Databases | Postgres + TimescaleDB is the main store (local first, then a hosted box); BigQuery for heavy analysis of append-only data; SQLite only as a local batching buffer before loads. |
| Brand | Separate brand later; build under the current name. |

## 1. Who it is for

| Segment | What they come for | Pays |
|---|---|---|
| Individual traders | mixed screens, alerts, honest backtests, paper portfolios | low (Rs 300-1,000/month), many users |
| Research houses | cross-company filing research, financials, API/exports, team space | high, few users |
| Wealth advisors | model portfolios for clients, client reports, recommendation records for SEBI | medium-high |

## 2. Principles (from the trading bible and our research record)

1. **Point in time.** Every number carries when it became public (filing broadcast time,
   price close, release date). Screens, backtests and portfolios only see what was public then.
   One look-ahead leak destroys trust.
2. **Show the source.** Every fact links to the filing and quote it came from; every metric
   says how it is computed.
3. **Honest testing.** Costs, a matched random control, walk-forward, a trial counter. We show
   when an idea does not work (our own ledger: most do not).
4. **Frozen rules.** A paper portfolio's rules cannot be edited once it starts; a change is a
   new version with its own track record (bible Law 19).
5. **Machine-run where we publish.** Our own portfolios are rule-run; an LLM may only veto,
   never pick (portfolio ruleset PRD).

## 3. Features

### 3.1 Data

| Area | Have now | Add |
|---|---|---|
| Equities NSE/BSE incl. SME | daily OHLCV 2013+, adjusted, delivery, circuits; 1-min bars ~5y (Dhan, internal) | BSE-only names fully; corporate-action audit view |
| Indices | NSE indices daily | sector/thematic index constituents over time |
| F&O | daily bhavcopy 2016+, rolling options | open interest views, option chain history |
| Financials | own XBRL results 2020+, 87,700 filings, 98% match vs screener.in | annual balance sheet/cash flow pages, segments, restated series, ratios, TTM |
| Filings | records: summary, verified typed facts, searchable passages (pilot companies) | whole universe (2 years first, ~USD 1k), BSE-only filers |
| Ownership and deals | bulk/block, insider/SAST, pledges, surveillance flags | shareholding pattern history, MF and FPI holdings per stock |
| News | ET news tagged by company | more sources, de-duplication, event tagging into the same fact types |
| Macro | RBI rates, G-sec (FBIL), currency reference rates | CPI/WPI, IIP, GDP, GST, bank credit, FX reserves, FII/DII flows, release calendar |
| Mutual funds | -- | AMFI NAVs, monthly portfolios, AUM and flows, category returns, overlap |
| Commodities | -- | MCX (licence), global benchmarks |
| Currency | RBI/FBIL reference rates | NSE currency futures |

Every dataset: source, licence tag, update time, point-in-time column, freshness shown on a
status page.

### 3.2 Company and market research

- **Company page**: price chart with stage and relative strength, financials (quarterly,
  annual, TTM, ratios), filing timeline with summaries and facts, ownership, deals, themes,
  peers, news. Every number links to its source.
- **Ask**: questions about a company answered from its filings with citations (exists:
  `infra/research.py`). Across companies: "all preferential issues above market price in two
  years" from the typed facts.
- **Themes and sectors**: members with revenue share, strength ranks, rotation (exists:
  stockey themes, systrader rotation).
- **People**: directors, promoters, KMP changes across companies.
- **Market dashboard**: breadth, regime (trend and breadth floor), sector/theme rotation,
  flows, yield curve, macro calendar.
- **Macro pages**: each series with history, release dates, and its link to sectors.
- **Mutual fund pages**: fund holdings, changes, which funds bought a stock.
- **Notes**: private notes and tags on any company, filing or screen.

### 3.3 Screener (fundamental + technical in one)

- **Metric catalogue**: every field a screen can use, with type, unit, frequency, source,
  licence tag and point-in-time rule. Groups: price/technical, financials, valuation,
  growth, quality, ownership, events (filing facts), news, macro, our scores.
- **Condition kinds**:
  - compare: `ROCE > 18`, `price > 200-day average`
  - rank: `RS 6-month rank in universe > 80`, `PE rank within industry < 30`
  - trend over time: `sales growth > 15% for 4 quarters running`, `promoter stake rising 2 quarters`
  - event in window: `order win > 10% of revenue in last 90 days`, `no preferential issue below market in 1 year`
  - state: `Weinstein stage = 2`, `theme strength = top 5`
  - market condition: `NIFTY above 200-day average` (applies to the whole screen)
- **Universe**: exchange, index, sector/theme, liquidity floor, market-cap band (no forced
  size gate: small caps stay in).
- **Builder and text**: a visual builder and a typed form (`roce > 18 and stage = 2 and ...`)
  that are the same screen underneath (a JSON screen language).
- **Results**: table with chosen columns, sort, sparkline charts, export; "as of" any past date.
- **From a screen**: save, alert on entries/exits, backtest it, start a paper portfolio.
- **Ready screens from our theories**: quality-growth, capital cycle, story score,
  confluence, stage-2 leaders, theme leaders; each with its honest test result.

### 3.4 Honest testing

- Backtest any screen or rule set: entries, exits, sizing, rebalancing, real costs
  (brokerage, STT, slippage by liquidity), capacity at a chosen rupee size.
- Against controls: benchmark, matched random picks from the same universe, shuffled timing.
- Walk-forward and a held-back period; results per year and per market regime.
- Slicer: where an edge lives (size, sector, regime), with the false-discovery check.
- **Trial counter**: each user's tries on a family of ideas are counted and shown; the more
  tries, the higher the bar (protocol: FDR within the family).
- Engine: systrader's Go backtester as a service (exists: costs, controls, walk-forward,
  deflated Sharpe, FDR, slicer).
- **Research library**: our ledger's results written up for users (what worked, what did not,
  why).

### 3.5 Paper portfolios

- **Kinds**: rule-based (screen + entry/exit + sizing + rebalance); manual (user enters
  trades); model portfolios (advisor publishes, clients follow); replica (a known investor's
  disclosed holdings); options strategies (spreads, covered calls) on the F&O data.
- **Mixed rules**: any screener condition as entry, exit or filter; stops and time exits;
  position sizing (equal, volatility, conviction caps); the regime overlay (cut exposure when
  trend/breadth is weak; the one timing rule that held up).
- **Frozen versions**: rules fixed at start; edits create v2 with a fresh record; history kept.
- **Tracking**: daily marks, trades log, returns vs benchmark and vs random control,
  drawdowns, exposure, sector mix, attribution (what made the return), capacity.
- **Compare**: many portfolios side by side; leaderboards inside a team.
- **Our published portfolios** (under the partner): the machine fundamental portfolio, trend
  books, investor-style tracks; each with entry/exit records and the partner's disclaimers.

### 3.6 Alerts

- Triggers: filing facts (typed: order, preferential issue, pledge up, auditor change ...),
  screen entries/exits, portfolio actions, price/stage changes, macro releases, results out.
- Channels: email first; Telegram/WhatsApp and webhooks next. Digest or instant.
- Per-alert history: what fired, when, with the source.

### 3.7 For advisors and research houses

- Model portfolios with client view-only links; client reports (PDF/web) with branding.
- **Recommendation record**: every published pick timestamped with rationale and
  disclaimers, kept for the SEBI record period; exportable for audit.
- Team workspaces: shared screens, notes, portfolios; roles.
- API keys and bulk export (financials, facts, screens).

### 3.8 Platform

- Accounts (email + password), plans and limits (questions, backtests, alerts), usage meter.
- Admin: users, invites, plan changes, data freshness, job queue, LLM spend.
- Status page for data freshness and incidents.

## 4. Architecture

```
 pipelines (stockey collectors, XBRL, filings records, macro, MF, news)
        |  write
        v
 Postgres + TimescaleDB  (main store: prices, financials, facts, users, portfolios, alerts)
        |                         \
        | nightly load (SQLite      \  serving tables (point-in-time, licence-tagged)
        |  batches -> load jobs)      v
        v                        API (FastAPI: auth, plans, quotas)  <--  web app (Nuxt, responsive)
 BigQuery (append-only history       |                 |
  for heavy analysis, research)      |                 +-- job queue (infra orchestrator):
                                     |                     backtests, questions (Claude API),
                          backtest service (systrader Go)  alert fan-out, report builds
```

- **Main store**: Postgres with TimescaleDB, as now. Start local; move to a hosted box when
  the beta opens (options and cost: section 6).
- **BigQuery**: append-only history (prices, financials, facts, records, marks) for heavy
  scans and research; loaded in batches (free load jobs), date-partitioned and
  symbol-clustered. Not used for live pages or app state.
- **SQLite**: local buffer that batches writes before a load; never a source of truth.
- **Serving tables**: built from the pipelines, point-in-time, licence-tagged. Public traffic
  never touches pipeline tables.
- **Services**: data pipelines stay Python; the backtest engine stays Go behind an HTTP API;
  a new public API (FastAPI) owns users, plans, quotas; the Nuxt app is rebuilt as a product
  with a component library and a chart library.
- **Rewrites expected**: screener.in-sourced fields replaced by our XBRL financials; the
  screen engine (new); the portfolio engine unified (stockey's machine portfolio and
  systrader's paper tracks share one model); front end.

## 5. Release stages

| Stage | Scope | Done when |
|---|---|---|
| **0. Foundation** | metric catalogue; serving tables (point-in-time, licence tags); public API skeleton; email/password accounts; app shell (navigation, tables, charts, responsive); XBRL annual/TTM/ratios | an invited user can sign in and open any company page |
| **1. Closed beta** (invite, free, everything shown) | company pages; mixed screener (builder + text, as-of dates); filings timeline and facts for the 2-year universe; Ask (one company); market dashboard; paper portfolios (rule-based and manual, frozen versions, daily marks); email alerts | 20-50 invited users weekly; screens and portfolios match the engine's numbers |
| **2. Research and testing** | screen backtests with controls, slicer, trial counter; cross-company questions; research library; themes, people, macro pages; MF data | backtests reproduce known ledger rows |
| **3. Advisors** | model portfolios and client links, recommendation record, reports, team workspaces, our published portfolios under the partner | partner signs off on compliance |
| **4. Open beta / paid** | licences settled (fields switched by tag), plans and billing (Razorpay), limits, terms and privacy, API keys, Telegram/WhatsApp/webhooks, commodities and currency | first paying users |
| **5. Scale** | full filings history, more news sources, BigQuery-backed research views, options strategies in portfolios | -- |

Stages 2 and 3 can overlap once stage 1 is stable.

## 6. Hosting the main database (GCP options)

Rough monthly cost for 4 vCPU / 16 GB RAM and 500 GB disk, single zone, list prices (check the
GCP calculator; Mumbai is ~10-20% above US regions):

| Option | TimescaleDB | Cost/month | Notes |
|---|---|---|---|
| Compute Engine VM, Postgres + TimescaleDB self-run | yes | ~USD 120-150 (~USD 90-110 with a 1-year commitment) | we run backups (pgbackrest to Cloud Storage) and upgrades; same setup as today |
| Cloud SQL for PostgreSQL | **no** (not a supported extension) | ~USD 250-300; ~2x with high availability | managed backups/failover; hypertables and compression would have to go |
| AlloyDB | **no** | ~USD 400+ | fast analytics engine; overkill and no Timescale |
| Hetzner dedicated (for comparison) | yes | ~EUR 50-100 | far cheaper for the same box; not GCP |

Recommendation: a Compute Engine VM with Postgres + TimescaleDB for the beta (keeps
hypertables and compression; cheapest managed-cloud route), BigQuery beside it in the same
region.

## 7. Risks

- Regulation: SEBI rules on research, past performance, and platform tie-ups; the partner's
  compliance review gates stage 3.
- Data licences: the closed beta shows unlicensed fields to invited users only; the open beta
  needs the licence decisions.
- Look-ahead: point-in-time enforced in the serving tables and tested on known cases.
- LLM cost per user: plan budgets, cached answers per company per day.
- One machine for pipelines and the public app: the hosted box separates them from stage 1.

## 8. Open items

1. Partner details (name, registration number, compliance rules, disclaimer text).
2. Licences: NSE display/data, MCX, any news source.
3. Anthropic API account and per-plan question budgets.
4. Domain and brand (later).
