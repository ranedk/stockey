# Research platform: fundamental + technical research, screens, paper portfolios

Written 2026-11-13; reviewed the same day (gaps fixed, task list added: section 10).
Status: agreed scope; only the XBRL quarterly store is built. Replaces `SAAS_PRD.md` as the plan
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

**Proposed in the review and accepted when work started (2026-11-13):**

| Topic | Proposal |
|---|---|
| Timing of data | End-of-day product. No live or intraday prices in any stage before 5 (Dhan bars are a personal account: internal only). |
| Financials shown | Figures **as filed**, with the time they became public; a revision is a new version. This is what makes "as of" screens honest. It will differ from screener.in where they show restated history. |
| Consolidated or standalone | Consolidated where the company files it (1,895 of 2,344), else standalone; the page says which and lets the user switch. |
| Where code lives | Serving-table builders in `stockey/serving/`; public API in a new `api/` folder at the workspace root with its own database schema (users, screens, portfolios, alerts); web app rebuilt inside `screener/`, **replacing the old site completely** (operator, 2026-11-13). The old pages that are still used (paper tracks, machine portfolio, data health, themes, stages) are rebuilt as sections of the new app; the old build stays deployed until the new one ships. |
| Technical numbers | Computed only by systrader (stage, relative strength, averages), written to `systrader_*` tables; the serving build copies them. stockey grows no technical code (boundary unchanged). |

## 0.1 Not doing

- No order placing or broker link; paper only.
- No live quotes, no intraday charts (until stage 5 at the earliest).
- No mobile app.
- No tips: outside the partner's published portfolios, the product states facts and test
  results, never "buy".
- No user-written SQL or code; screens go through the screen language only.

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
| Financials | own XBRL results 2020+ for 2,344 NSE companies (87,700 filings, 98% match vs screener.in): profit and loss every quarter, balance sheet and cash flow every half year. Fetched by hand, not on a schedule | daily fetch of new results; clean quarterly/annual/TTM series; ratios; segments (not mapped yet); companies with no XBRL (SME, BSE-only); history before 2020 is not planned |
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
- **How a screen runs.** Two kinds of serving table, joined by the screen compiler:
  a wide daily table for price and technical metrics (one row per company per day), and
  interval rows for slow data (financials, ownership, facts: value, known-from time,
  known-until time). "As of date D" means the daily row for D and the interval rows known
  on D. The screen language compiles to SQL over catalogue fields only. Targets: a screen on
  today's data under 2 seconds, on a past date under 5.
- **Company identity.** One stable company id (ISIN-based, from `dim_security`) with symbol
  and name history, so renames, BSE-only names and relistings do not break pages or screens.
- **Paper portfolio marking.** Orders fill at the next session's open with the cost model;
  marks at the adjusted close; dividends credited; splits and bonuses through the adjustment
  factors. One rule set for every portfolio, written down before the engine is built.
- **Rewrites expected**: screener.in-sourced fields replaced by our XBRL financials; the
  screen engine (new); the portfolio engine unified (stockey's machine portfolio and
  systrader's paper tracks share one model); front end.

## 5. Release stages

| Stage | Scope | Done when |
|---|---|---|
| **0. Foundation** | clean quarterly/annual/TTM series and ratios from XBRL, fetched daily; company identity; metric catalogue; serving tables (point-in-time, licence tags); public API skeleton; email/password accounts and invites; app shell (navigation, tables, charts, responsive); a basic company page (price chart, financials, ratios, links to filings) | an invited user can sign in and open the basic page of any company; point-in-time tests pass |
| **1a. Closed beta: look up and screen** (invite, free, everything shown) | full company page (filing timeline and facts, ownership, deals, themes, peers, news); mixed screener (builder + text, as-of dates, saved screens, export); filings records for the 2-year universe; status page | 10-20 invited users; a screen's result on a past date matches a hand check |
| **1b. Closed beta: track and ask** | paper portfolios (rule-based and manual, frozen versions, daily marks); email alerts; Ask (one company, needs the Anthropic API account); market dashboard; usage tracking and a feedback button | 20-50 invited users weekly; portfolios match the engine's numbers |
| **2. Research and testing** | screen backtests with controls, slicer, trial counter; cross-company questions; research library; themes, people, macro pages; MF data | backtests reproduce known ledger rows |
| **3. Advisors** | model portfolios and client links, recommendation record, reports, team workspaces, our published portfolios under the partner | partner signs off on compliance |
| **4. Open beta / paid** | licences settled (fields switched by tag), plans and billing (Razorpay), limits, terms and privacy, API keys, Telegram/WhatsApp/webhooks, commodities and currency | first paying users |
| **5. Scale** | full filings history, more news sources, BigQuery-backed research views, options strategies in portfolios | -- |

Stages 2 and 3 can overlap once stage 1b is stable. Stage 4 also needs every served score
(story, confluence, themes, universe layers) rebuilt on XBRL numbers: today they read screener.in.

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
- Carried over from `SAAS_PRD.md` section 2, still open:
  - screener.in data cannot be served; anything derived from it (story score, themes,
    universe layers) is closed-beta only until rebuilt on XBRL.
  - Sharpely sector labels cannot be served; the exchange's four-level industry labels
    (`fundamentals_industry_classification`) replace them.
  - The pipelines that read filings run on a personal Claude subscription (and one OpenAI
    step). Output sold to the public must come from the Anthropic API under commercial terms;
    this applies to the records build, not only to user questions.
  - Privacy law (DPDP Act): consent, deletion on request, where user data is hosted. People
    pages state public facts with sources only.
- Coverage: 2,344 companies have XBRL financials; SME and BSE-only companies may have none.
  The company page must say "no financials filed in XBRL" rather than show blanks.
- Five years of financials only (2020 on): long-history ratios (10-year growth) are not
  possible; say so in the catalogue.

## 8. Running it (applies from stage 0)

- **Security**: passwords hashed (argon2), login rate limit, sessions that can be revoked,
  secrets out of the repo (gitleaks over history before any outside user), HTTPS only, API
  rate limits per user.
- **User data**: nightly backup of the app schema, restore tested once; account deletion works.
- **Monitoring**: error log with alerts to the operator; data freshness on the status page
  (reuses the completeness gate); a failed nightly serving build keeps yesterday's tables and
  says so on the page.
- **Usage**: page views, screens run, questions asked per user (needed for the "weekly users"
  measure and for plan limits later).
- **LLM spend**: budget per user per day, cached answers, a hard monthly cap.

## 9. Open items (operator)

1. Partner details (name, registration number, compliance rules, disclaimer text). Needed by stage 3.
2. Legal opinion: the tool/advice line, and how a user's own backtest may be shown. Needed by stage 2.
3. Anthropic API account and question budgets. Needed by stage 1b (Ask) and for the records build.
4. Licences: NSE display/data, MCX, any news source. Needed by stage 4.
5. Hosting choice (section 6) and where user data sits. Needed before the first outside user.
6. Domain and brand (later).

## 10. Tasks

Stage 0 and 1a in detail; later stages get their list when the one before is close to done.
`TODO.md` section J tracks status.

### Stage 0: foundation

Financials (stockey)
- F1. Coverage report: universe companies with no XBRL financials, and why (SME, BSE-only,
  new listing, fetch failed). Fix what can be fixed; list the rest.
- F2. Daily fetch: new result filings picked up on a schedule (cron, through the fleet), so
  numbers arrive the day they are filed. Today it is run by hand.
- F3. Clean series per company and basis: quarters (Q4 = year - nine months; cash-flow
  quarters from year-to-date), annual, TTM. Each value keeps known-from time; a revision
  adds a version.
- F4. Ratios: margins, growth (YoY, QoQ, 3-year), ROE, ROCE, debt/equity, interest cover,
  working-capital days, cash conversion; valuation from market-cap history (PE, PB,
  EV/EBITDA, price/sales). Separate short lists for banks, NBFCs and insurers.
- F5. Check F3-F4 against screener.in on the 50 pilot companies (annual and ratios, 5%).
- F6. Segments: map segment revenue and result.
- F7. Industry labels: confirm the exchange four-level labels cover the whole universe.

Serving (stockey/serving)
- S1. Company identity table: stable id, ISIN, symbol and name history, exchange, industry.
- S2. Metric catalogue: table + first ~80 metrics (name, group, unit, frequency, source,
  licence tag, point-in-time rule, formula in words).
- S3. Serving tables: daily prices (NSE end-of-day, adjusted), daily technicals (from
  systrader tables), financial interval rows; nightly build after price adjustment; freshness table.
- S4. Point-in-time tests on known cases: a revised result, a result filed after the close,
  a split, a renamed company.

App (api/, screener/)
- A1. Design note first (one page each): screen-table layout, API layout, component and
  chart library choice.
- A2. API skeleton: accounts (email + password, verify, reset through SES), invites,
  sessions, rate limits, app schema, tests.
- A3. App shell: navigation, table, chart, empty states, settings, login pages; phone width works.
- A4. Basic company page: price chart, quarterly/annual/TTM tables, ratios, filing links,
  basis switch.
- A5. Admin: invites, users, data freshness.
- A6. Section 8 basics: gitleaks, app-schema backup, error alerts, usage log.

### Stage 1a: look up and screen

- B1. Filings records for the 2-year universe (about USD 1k), with records and embeddings
  wired into the crawl (`TODO.md` section I).
- B2. Interval serving tables for ownership, deals, filing facts, news, themes.
- B3. Full company page: timeline, ownership, deals, themes, peers, news.
- B4. Screen language (JSON) and compiler to SQL; all six condition kinds; as-of dates;
  tests against hand-checked results.
- B5. Screener UI: builder, text form, results table with columns/sort/export, saved screens.
- B6. Status page; invite the first users.

### Stage 1b: track and ask (outline)

Portfolio model note (one model for stockey's machine portfolio and systrader's paper
tracks) -> portfolio engine and daily marks -> portfolio pages -> email alerts -> Ask on the
Anthropic API with budgets -> market dashboard.
