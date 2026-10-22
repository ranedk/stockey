# Workspace PRD — what to build next, and why

Written 2026-09-23 from the session that closed the combination-policy question
(LEDGER row 41), built the two-trait grids (rows 42-43) and priced the speed
limit. Revised the same day to cover universe expansion, signal coverage, and
one live bug found while measuring it. Cross-repo on purpose: the work splits
across all three repos, and several items only make sense read together.

**How to use this.** Pick one item; it becomes that session's task. Anything
touching market data still follows `systrader/docs/RESEARCH_PROTOCOL.md` —
pre-register, one LEDGER row, forward paper trading as the gate. Items are
ordered within each section by what I'd do first, not by size.

**Status key:** `[ ]` open · `[~]` partly done · `[x]` done · `[-]` deliberately
not doing (with the reason, so it is not re-litigated).

**Where the work lands.** Three projects in one git repo (since 2026-09-26), and this file at the workspace root
above them, because most items cross at least two:

- `stockey/` — data platform + the fundamentals carve-out. Sections B, C.
- `systrade/` — research, signals, backtests, execution. Sections A, E.
- `screener/` — the Nuxt frontend both of the others publish through. Anything
  in B or C that needs to be *seen* lands here too.

This file is the index of open work. If an item is open only in a repo's README
or PRD and not here, that is a bug in this file.

---

## What the data already settled

Inputs to everything below, not opinions. Each was measured, not assumed:

- **Costs beat speed.** A book held 5 days costs 17.8%/yr to run at Rs 20,000
  a position with auction fills; 10 days costs 8.9%; 21 days costs 4.2%
  (`costs speed`). Short-interval TA is priced out before any signal is tested.
- **Past a month, slowing down buys no alpha.** Net selection edge per year is
  flat — 11.9% at 20 days, 11.7% at 42, 11.3% at 63 (LEDGER row 43). Monthly →
  quarterly saves ~0.8%/yr in cost and gives up ~0.6%/yr in edge. **A wash —
  unless tax is counted, and nothing here counts it.**
- **It is volatility, not size.** Within every liquidity third the edge rises
  with volatility; within every volatility third liquidity barely moves it
  (row 42). The most liquid, calmest names are a dead zone for this rule.
- **Three of the eight documented anomaly families are live** (momentum, low
  volatility, their combination), **three are rejected with evidence**
  (trend/regime overlay row 20, 52-week-high breakouts rows 38/40, short-term
  mean reversion rows 11-13 and 30), and **three are untested** — quality,
  value, event drift. All three untested ones are fundamental: stockey's side.
- **stockey cannot backtest them.** Fundamental history is ~6 weeks deep (742
  `fundamentals_l2_state` rows, from 2026-08). Quality and value can only run
  FORWARD from here.
- **The L1 universe is 194 names** (2026-09-23), capped at Rs 5,000 cr market
  cap with a Rs 10 lakh/day liquidity floor — a hundredth of the Rs 10 crore
  floor every systrader track uses.
- **The confluence score is running on partial data.** On the latest run its
  five axes scored 0%, 60%, 53%, 47% and 55% of the 137 names. The 0% is a
  bug, not thin data — see C1.

---

## A. Friction: the gap that changes decisions already made

### A1. Put tax in the cost model
- [x] **Done 2026-09-24 (systrade d2bf91b).** At the live tracks' ~2/3 churn and 16%/yr
      before costs, quarterly keeps +1.69 points a year over monthly after costs AND tax
      (+1.94 costs, -0.25 tax): at that churn neither clock realises anything long-term,
      so the 12.5% rate is out of reach. At 20% churn tax flips to +0.49 for quarterly
      (23% vs 74% of gains long-term). The tax lever is churn -- A2 -- not the clock.
- **Was:** extend `internal/costs` and `costs speed` with Indian equity
      tax — 20% short-term, 12.5% long-term above Rs 1.25L — and report
      after-tax cost per holding period beside the pre-tax table.
- **Why:** the one item on the operator's list this workspace models nowhere,
  and larger than the cost saving row 43 measured. A monthly book realises
  nearly everything short-term; on a ~16%/yr book the gap between 20% and
  12.5% is worth more than a point a year.
- **Where:** `systrade/internal/costs`, `cmd/costs`.
- **Done when:** the speed-limit table has an after-tax column and the
  monthly-vs-quarterly comparison is one number.
- **Size:** small. Arithmetic, no market data, no LEDGER row.

### A2. Rank-buffer exits
- [x] **Built 2026-09-28** (systrade `internal/paper` Spec.KeepMultiple, `paper buffer`, LEDGER 49).
      In-sample 2013-2021: turnover halves at K=2 everywhere; the two TREND tracks gain ~2-3 points a
      year after tax (the buffer also lifts gross -- winners were sold early); momentum, low-vol and the
      combination are flat after tax. K=3 is no better. Frozen tracks untouched. Feeds A3.
- **Was:** hold a name until it falls out of the top 2N, not the top N.
      Implement in `internal/sleeve` and `internal/paper` so research and the
      paper tracks can both run it.
- **Why:** the cheapest turnover reduction available, and it compounds with
  A1 — lower turnover moves gains from short-term to long-term.
- **Done when:** the buffer is a spec field, tested on synthetic data, and its
  effect on turnover is measured on the five live tracks' books.
- **Size:** medium.

### A3. Re-price the live tracks' clock
- [x] **Done 2026-09-29.** Monthly vs quarterly with/without the buffer measured on the trend tracks:
      the clock effect is inconsistent (quintile quarterly better, speed-blend quarterly worse); the
      buffer is the consistent gain. New forward track `trend-speed-blend-buffered` (K=2, monthly)
      frozen, starts 2026-09-30, LEDGER 50; judged vs trend-speed-blend from 2027-09-30.
- **Was:** with A1 and A2 built, compare monthly vs quarterly rebalancing
      with and without the buffer, after costs AND tax, on the five frozen
      tracks.
- **Why:** row 43 says the alpha is a wash; A1 says tax probably is not.
- **Careful:** changing a frozen track's clock is a NEW spec and a NEW 12-month
  clock (Law 19). A survivor starts a sixth forward record; it does not edit
  the five.
- **Size:** medium. **Unblocked 2026-09-28** (A1 + A2 done; A2 says: test K=2 on the two trend tracks only).

---

## B. Universe: widen it, and know what each filter costs

The L1 screen today:

| filter | current value | what it does |
|---|---|---|
| market cap | Rs 100 cr – **5,000 cr** | excludes every mid and large cap |
| liquidity | volume × price > **Rs 10 lakh/day** | 1/100th of systrader's floor |
| FII + DII holding | **< 20%** | excludes anything institutions own |
| shareholders | **< 50,000** | excludes anything widely held |
| cash conversion | ≥ 0.6 over two years | quality |
| debtor days | ≤ 3 years ago | quality |
| contingent liabilities | < 25% of net worth | quality |

The last three are quality filters. The first four are a *universe* choice, and
two of them are **neglect** filters: a high-ROCE large cap is thrown out for
being popular, not for being weak.

### B0. Measure what each filter costs in names
- [x] **Done 2026-09-24** -- table in `stockey/docs/FUNDAMENTAL_SCREENER_PRD.md` §14.
      Baseline 196. Quality clauses bind hardest (+242 / +270 dropped); the Rs 5,000 cr
      ceiling alone is nearly slack (+12 at Rs 50,000 cr) because the shareholder cap
      (+81) already excludes large caps -- ceiling + both neglect clauses gives 441.
      Liquidity: Rs 1 cr/day -> 76 names, Rs 10 cr (systrader's floor) -> 11. B1 is unblocked.
- **Was:** run the L1 query with each filter dropped in turn and record
      the survivor count. Seven numbers.
- **Why:** B1-B3 are currently arguments. This turns them into a table. We know
  the screen returns 194; we do not know whether dropping the shareholder cap
  adds 50 names or 800.
- **Where:** `stockey/fundamentals/screens/l1_universe.py` (the same
  `run_query` plumbing, one call per variant).
- **Done when:** the counts are in the screener PRD beside the query text.
- **Size:** small. **Do this first — B1, B2 and B3 all read it.**

### B5. Rebuild the universe: Layer 1 + Layer 2 (PRD agreed 2026-09-25)
- [x] **What:** replace the L1 screen with `stockey/docs/UNIVERSE_PRD.md`: Layer 1 -- done 2026-09-25 (l1_universe v2 + Layer 2)
      (NSE main board, not on the surveillance list, >= Rs 300 cr, >= Rs 50 L traded a
      day, listed >= 1 year, pledge < 50%) tags each stock with one of five groups;
      Layer 2 excludes fundamentally bad businesses with checks per group.
- **Why:** the current screen (~196) misses most of where money was made; this
  Layer 1 held 95% of 2019+ 5x winners before their peak (LEDGER 44), ~1,350 stocks.
- **Also:** OCR becomes its own continuous job -- it must never limit the universe.
- **Done when:** PRD build steps 1-6 are done and the operator has signed off the
  counts and the 5x winners Layer 2 rejects.
- **Progress:** steps 1-2 done 2026-09-25 -- Layer 1 = 1,395 stocks
  (`stockey/fundamentals/screens/universe.py`); surveillance = GSM + ASM stage 2+ from NSE's
  daily file (new collector); pledge moved to Layer 2. Step 3 done: four groups from
  exchange industry labels (Sharpely bulk, BSE per stock overrides) + RBI's NBFC register;
  no asset-heavy/light split (operator). Step 4 done: Layer 2 passes 1,297 of 1,394
  (thresholds revised, contingent check dropped, turnaround + growth allow-rules). Next: step 5 (done 2026-09-25: l1_universe v2 = 1,295 stocks). Next: step 6 -- OCR as its own job,
  announcements / L2 crawl scaling -- BEFORE removing .pause_fundamentals.
- **Size:** large. Supersedes B1-B4 below (kept for their reasoning).

### B6. Story-and-flaw scoring, event-driven re-evaluation, weighted portfolio (PRD agreed 2026-09-29)
- [x] **What:** `stockey/docs/FUNDAMENTAL_REEVALUATION_PRD.md` -- everything after the universe: -- done 2026-10-02 (steps 3-7 built, live)
      churn fixes first (ruleset v2), then a daily dated story/flaw score per company (change and
      acceleration beside level; the market's price reactions decide what counts), results reading,
      a re-evaluation queue driven by filings / peer filings / ET news / prices / macro, a structured
      LLM story read, the watchlist on the score, and a conviction x risk weighted portfolio.
- **Built so far:** Economic Times RSS collector, hourly (`fundamentals/collectors/et_news.py`).
- **Step 1 done 2026-09-29:** ruleset v2 (2-axis entry, wider stops, exits only on hard
  contradictions / 3-run soft ones / stage 4, 20-session minimum hold, valuation never exits).
- **Step 2 done 2026-09-29:** daily dated story/flaw score (`fundamentals/screens/story_score.py`,
  table `fundamentals_story_score`); a record until step 6 puts the watchlist on it.
- **Step 3 done 2026-09-29:** results reading from screener.in's quarterly table (stored by
  the L2 crawl), growth and margin against each company's own trend, one-offs stripped; story
  score v2 reads it.
- **Step 4 built 2026-09-29:** every 30 minutes, today's filings and tagged ET news re-score
  the touched companies; material score changes recorded (`all_fundamentals_reeval.sh`, in the
  crontab template -- live at go-live with the crontab regeneration).
- **Step 5 built 2026-09-29:** LLM story read on material score changes and substantive filings;
  alerts `story_read_positive/negative` for the watchlist; triage retired from the nightly run;
  measured forward (LEDGER row 51).
- **Step 6 built 2026-09-29:** watchlist on the story score (band or event-above-median, never
  with a flaw; flawed / faded statuses). Confluence stays only as ruleset v2's entry filter until v3.
- **Step 7 built 2026-09-30:** ruleset v3 -- weighted by story score / volatility with caps,
  replacement, trailing stop, thesis-broken / story-fading exits, valuation trim, tax guard,
  counterfactuals (LEDGER row 52). Replaces v2 for new entries.
- **Next:** go-live (OCR catch-up, crontab regeneration, unpause); then step 8, price-reaction
  learning, once filings accumulate. Cleanup: drop the nightly confluence step once nothing reads it.

### B1. Name the anomaly the screen is for
- [x] **Folded into B5 (2026-09-25).** **What:** decide, and record in `stockey/docs/FUNDAMENTAL_SCREENER_PRD.md`,
      whether the screen hunts **neglect** or **quality**.
- **Why:** today it hunts neglect and calls it quality. Those are two different
  documented anomalies with different universes, and the neglect filters are
  almost certainly what caps the universe at 194. Everything in C inherits the
  answer.
- **Done when:** the PRD says which, in a paragraph, with the filters that
  follow.
- **Size:** small, and it gates B2/B3. **Blocked on B0.**

### B2. Raise the liquidity floor
- [x] **Folded into B5 (2026-09-25).** **What:** measure how many names survive at Rs 1 cr / Rs 5 cr / Rs 10 cr
      of median daily traded value, then set a floor.
- **Why:** this is the one item here that *shrinks* the universe, and it should
  still be done. Row 35 measured the spread at 16.5 bps in the Rs 1-10 cr tier
  against 3.7 bps at Rs 100-500 cr, and the operator's own source is blunt: in
  small caps impact can exceed every explicit cost, and a backtest there assumes
  fills you would never get. A wider universe of untradeable names is not wider.
- **Size:** small. **Blocked on B0.**

### B3. Widen the market-cap band
- [x] **Folded into B5 (2026-09-25).** **What:** raise the Rs 5,000 cr ceiling, and decide whether the neglect
      filters survive B1.
- **Why:** the biggest single expansion lever. It also creates overlap with
  systrader's Rs 10 cr-ADV universe, which is where momentum and low volatility
  already run — and that overlap is what D1 needs.
- **Careful:** bump `L1_QUERY_VERSION`, never edit the query in place. The
  version is what makes an old universe reconstructable.
- **Size:** small. **Blocked on B1.**

### B4. Make the pipelines cover the wider universe
- [x] **Folded into B5 (2026-09-25).** **What:** L3 crawls, price coverage and adjustment are all scoped to the
      L1 universe by design. Widening L1 widens all of them. Check and extend:
      BSE-only price coverage (43 active L1 names have no NSE listing today —
      that count grows), `price_adjustment` coverage, the technicals refresh's
      per-ticker history load, L2 crawl state, and L3 event crawl scope.
- **Why:** an expanded universe whose new names have no price history, no
  events and no L2 state is expansion on paper only.
- **Done when:** for the new universe, every name has price history, an L2 row
  and event coverage — or is listed with the reason it does not.
- **Size:** medium. **Blocked on B3, and the real work of expanding.**

---

## C. Signals: fix what is dead, then add what the data already supports

### C1. The fundamentals-trajectory axis is dead — fix it
- [x] **Done 2026-09-23 (score v2).** Axis reads L2's real vocabulary
      (`*_increase` → False, `*_decline` → True, `flat`/`reversal` →
      None); now scores 72 of 137 (29 improving, 43 worsening). All five
      readers tie-break on `score_version DESC`; v1 rows kept as history. Entry
      thresholds are counts of contradicting/evaluable axes, not "out of five",
      so none needed retuning — but eligible names fell 39 → 30, and one open
      *shadow* position (ASHIANA, debt `decelerating_increase`) now carries a
      contradicting axis and will exit as `thesis_invalidation` on the next run.
      A regression test feeds `compute_trend_direction`'s own output to the axis.
- **What (original):** `compute_fundamentals_trajectory_axis` tests
      `net_debt_trend_direction` and `cwip_ratio_trend_direction` for the
      values `"increasing"` / `"decreasing"`. Those two fields are written by
      `l2_state.py`'s *numeric-series* vocabulary, which emits
      `accelerating_increase`, `decelerating_increase`, `steady_increase`,
      the three declining mirrors, `flat` and `reversal` — and **never**
      `increasing` or `decreasing`. The axis can only ever return `None`.
- **Measured:** 0 of 137 names scored, on every run checked back to 2026-09-15.
  The confluence score has therefore always been a count out of **four** axes,
  not five, and any threshold tuned on it was tuned on four.
- **Why it matters:** the confluence score feeds the portfolio ruleset and the
  LLM adjudicator. This is a live machine-run decision path.
- **Careful:** fixing it changes every name's score, so it changes live
  portfolio behaviour. Bump the score version rather than silently
  reinterpreting stored rows, and re-check any threshold that reads it.
- **Where:** `stockey/fundamentals/screens/confluence_score.py`.
- **Size:** small fix, real consequence. **Do this first in section C.**

### C2. Two more axes are running on near-empty inputs
- [x] **Done 2026-09-23 (data audit).** Pledge: fetched market-wide daily but stored only
      for the 0-6 companies due a 75-day crawl -- now `fundamentals_l2_market_snapshot`
      (every L1 company, every run, failed fetch = NULL not 0.0). Institutional: NULL
      when FII and DII rows are both absent from a parsed table -- now 0. Full record
      in `stockey/docs/DATA_AUDIT_2026-09-23.md`.
- **What (original):** `pledge_pct_trend_direction` is null in **734 of 742** L2 rows
      and `institutional_stake_direction` in **426 of 742** (57%). Both feed
      the ownership axis. Find out whether the source data is missing, the
      parse is failing, or the field genuinely cannot be computed for most
      companies — and say which, in the code.
- **Why:** an axis that silently degrades to `None` for half the universe is
  not a signal, it is a coin that lands on its edge. Distinguishing "no data"
  from "no trend" is the whole job here.
- **Size:** medium.

### C3. Make axis coverage a standing check
- [x] **Done 2026-09-23.** Each run puts `axis_coverage` in its run state and
      emails via `send_ops_alert` on two classes: `dead` (0 of N) and `dropped`
      (< half the previous same-version run, prior ≥ 10%). First v2 run: 53 / 60
      / 53 / 49 / 56%, no alarm.
- **What (original):** every confluence run records what share of names each axis
      actually scored, and alerts when an axis drops to zero or falls sharply.
- **Why:** C1 sat dead for at least a week in a live decision path and nothing
  noticed. The fix is not vigilance, it is a check.
- **Careful:** the workspace rule from prior incidents — fix a noisy monitor by
  classification, never by widening the filter.
- **Size:** small. **Do alongside C1.**

### C4. Point-in-time snapshots, starting now
- [x] **Done 2026-09-27/28.** Daily dated snapshot of every company's screener.in fundamentals
      (`fundamentals_snapshot_daily`, Layer 2 reads it), `fundamentals_events.detected_at`,
      text->DATE fixes, and `fundamentals/pit.py` (`known_as_of`, `snapshot_panel`). Missed
      weekdays are reported. History starts 2026-09-27.
- **Was:** snapshot every fundamental input the screens read, dated, on
      every refresh — not just current state.
- **Why:** the highest-value cheap item on this list. Quality and value cannot
  be backtested today because there is no history; in two years that is either
  still true or solved, and today is when that is decided.
  `fundamentals_l2_state` is append-only and versioned already, which is the
  right shape — the gap is coverage and never overwriting.
- **Done when:** a query can answer "what did we know about company X on date
  D" for every field a C6-C8 rule would read.
- **Size:** medium. **Before or alongside C6-C8, never after.**

### C5. Two signals the data already supports but nothing reads
- [x] **Built 2026-09-28 as DESCRIPTIVE state, not scored axes** (the boundary: stockey does not
      author price signals). `technicals.py` stores delivery % (20d vs the stock's own 1y median)
      and upper/lower band hits (20d/60d) for every universe/watchlist stock; the watchlist
      page shows them; `event_drift` stores them on each record at entry, so whether they matter
      is measured forward by slicing the C7 record. **Data finding:** NSE's band-hit file is
      INVERTED for stocks with futures (60 days: F&O 'H' rows median close -2.5%, 18% up days;
      non-F&O ~90% correct), so band hits are None for F&O stocks. **Re-checked 2026-09-28:** the
      inversion starts August 2026 (2013-2025 labels are right), so systrader rows 28/29/39 stand;
      `store.CircuitHits` drops futures stocks' hits from 2026-08-01 (LEDGER note under row 28).
- **Was:** add axes for **delivery %** (`nseindia_mto`, 6.07M rows, 2013+)
      and **price-band hits** (`nseindia_circuit_hit`, 758k rows, 2013+).
      Delivery % is conviction — stock actually taken, not churned intraday.
      Band hits are stress and crowding.
- **Why:** both were revived for systrader's trait library (rows 27-28) and
  came back **distinct** from the other traits, so they carry information the
  current five axes do not. They exist, they are deep, and stockey reads
  neither.
- **Careful:** these are price-derived, so keep them on the stockey side of the
  boundary as *descriptive state*, the way `technicals.py` already handles
  momentum and mean-reversion — stockey does not author TA signals.
- **Size:** medium.

### C6. Quality, as a forward record
- [x] **Built 2026-09-28** (`fundamentals/screens/forward_tracks.py`, quality v1 frozen, LEDGER 46);
      the record starts 2026-10-01 and is judged after >= 12 months.
- **Was:** define quality from what stockey already collects (ROCE/ROE,
      debt, earnings stability), rank the universe, hold the top slice, record
      it forward like a paper track.
- **Why:** untested here, strong evidence elsewhere, and the classic partner
  for momentum — momentum crashes tend to happen while quality holds up.
- **Honest limit:** no backtest is possible (C4). This starts a record; it does
  not produce a result for a year.
- **Size:** medium. **Blocked on B1 + C4.**

### C7. Event drift
- [x] **Built 2026-09-28** (`fundamentals/screens/event_drift.py`, 9 trigger types, LEDGER 48);
      forward record from 2026-09-28, judged as one family with FDR.
- **Was:** a rule on events stockey already ingests — results beats,
      insider/promoter buying, bulk-deal buys, capital raises, rating actions —
      bought after the event and held weeks.
- **Why:** the family stockey is *best* placed for. The trigger vocabulary is
  already rich (14 types in `l3_triggers.py`), the feeds run, and the family
  needs clean point-in-time event data more than it needs long history.
- **Careful:** the event's **detection time** must be stored, not just its
  date. A drift study on hindsight timestamps is fiction.
- **Size:** medium. **Blocked on C4.**

### C8. Value
- [x] **Built 2026-09-28** (value v1 within quality's better half, LEDGER 47); starts 2026-10-01.
- **Was:** cheapest by earnings yield or EV/EBIT, screened for quality.
- **Why:** documented and untested here — but the operator's own source calls
  its India record mixed, so it is third of the three.
- **Size:** medium. **Blocked on B1 + C4.**

---

### C9. Open items from the 2026-09-23 data audit
All closed 2026-09-24; detail in `stockey/docs/DATA_AUDIT_2026-09-23.md` (G1-G12).
- [x] **Paired comparison is one-sided** -- rejects get a mechanical horizon; arms compared on price.
- [x] **Stage for BSE-only names** -- systrader's stage API serves `BSE:<scrip>` keys and
      all NSE series (systrade 9b15b79); 89 -> 126 of 138 active names staged.
- [x] **Sector phase method** -- per-company gross block vs annual sales, median gap; confluence v4.
- [x] **Negative-only watchlist names** -- no longer added; exit as `no_thesis`.
- [x] **Extraction schemas lack enums** -- enums added, no re-extraction needed.
- [x] **584 failed BSE insider-notice PDFs** -- the 152 SAST disclosures re-admitted; the
      trading-window notices stay out (no holding information).
- [~] **Watch: completeness gate timing** -- first clean night 2026-09-24; move the gate
      after 12:00 IST only if nights fail at 08:00 but pass by noon.

### C11. Junk weekend rows in `nseindia_indices` (found 2026-10-20)

- [ ] 2023-06-04 (Sun) and 2023-11-04 (Sat) hold closes 5-8% off the surrounding sessions in
  Nifty 50, Nifty Bank and India VIX (Nifty 17,599 / 17,722 between ~18,500 and ~19,200
  closes). The other weekend rows are real special sessions (Muhurat, Budget Saturdays, the
  2024 DR drills). Delete the two at source and find which collector wrote them; anything
  computing index returns or volatility across those dates is wrong until then (LEDGER 61-62
  filtered them by hand).

### C10. Trigger study: which filings and results come around momentum entries that keep running -- CLOSED

Operator idea, 2026-10-08 (`stockey/docs/TRIGGER_STUDY.md`, LEDGER rows 56-57).
- [x] Full run: 561 entries 2016-2022, 403 stocks, ~18,400 filings + 2,800 attachments tagged
      blind. No filing type separates winners from failures (best q 0.64), in any sector.
- [x] Results confirmation (row 57): in-sample PASS (+4.5 pp / 13 weeks, p 0.001), but the
      once-only 2023-2024 check FAILED (-0.6 pp, p 0.66). No paper track.
- Left behind and still useful: NSE XBRL results collector (`trigger_study/nse_results.py`,
  Sep 2019 - Dec 2024), the blind Claude-CLI tagger + research OCR queue, the supervisor.

## D. The boundary question

### D1. Momentum + quality — decide where the blend lives
- [x] **Decided 2026-10-19 (operator):** systrader may READ stockey's forward-track holdings
      (`fundamentals_forward_track_holding`, synced in full nightly; DATA_CONTRACT both copies).
      Blend pre-registered and running: `momentum-quality-combination`, 50/50, from 2026-10-20
      (LEDGER 60), judged from 2027-10-20.
- **Was:** momentum runs in systrader, quality would run in stockey, and
      `DATA_CONTRACT.md` says stockey writes no fundamentals into systrader's
      reach. To blend them, one of two things must change: a fundamentals table
      becomes readable by systrader, or the blend is assembled in stockey from
      systrader's published momentum output.
- **Why:** the only item here that cannot be built without amending the
  contract — and B3's wider universe is what makes the overlap real.
- **Careful:** the contract has a byte-identical mirror in both repos. Edit both
  or neither.
- **Size:** small decision, medium consequence. **Blocked on B3 + C6.**

---

## E. Open elsewhere, pulled in so nothing is dropped

Everything here was open in a repo README or PRD before this file existed.

**systrader**

- [~] **Stability gate** — 20 clean trading days, then capital may move. Do not
  disturb the schedulers. **Checked 2026-10-20:** S1's count restarted 2026-10-05
  (the paper job failed 2026-09-28..10-01 on "no whole-share change", plus a
  manual run 10-02); 11 clean days by 10-19, day 20 about 2026-11-02. S3 (account
  trades = the evening-before sheet) failed by construction -- only 30-60% of
  trades matched; fixed 2026-10-20 (the account fills the sheet; the sheet reads
  the latest close's signals). S3 still needs one scheduled rebalance after the
  fix, checked with `paper run -strategy <s> -as-of <date>`. S4, S5 not yet
  recorded.
- [ ] **Multi-strategy live portfolio** — one account, several tracks,
  risk-weighted by handcrafting, positions netted across tracks. Better done
  after A3, which may change what the tracks look like.
- [ ] **Live execution** — `dhan orders -strategy <one> -live` is built and has
  never sent an order. Needs this server's static IP whitelisted with Dhan
  (order APIs require it), a valid token at run time, and the E-stability gate.
- [ ] **Instrument universe finalization** — blocked on capital (open question
  A1 in `systrade/docs/open_questions.md`).
- [ ] **First paper evaluations** — trend-quintile from 2027-09-08,
  trend-speed-blend from 2027-09-10. Not actionable before then.

**stockey**

- [x] **A large uncommitted change set was committed blind as `WIP`** (b4b3230, -- resolved: both tests pass (checked 2026-10-19)
  2026-09-23 16:11 — one minute after this file was written). It left two
  failing tests on HEAD: `test_entry_sizing_is_frozen_on_the_position` (stub
  arity vs `portfolio_runner.py:160`) and `test_morning_catch_up_runs_on_saturday`.
  Still worth splitting/rewording before anything is pushed. Original note: 46
  files, ~4,900 insertions and ~1,400 deletions as of 2026-09-23: doc rewrites
  (`CLAUDE.md`, `DATA_INVENTORY.md`), collectors, `fundamentals/` screens,
  ~3,250 lines of test changes, ops scripts, and the deletion of
  `fundamentals/screens/l4_thesis.py` (which is still in HEAD). The last commit
  is from the same day, so this is in-flight work a session did not finish, not
  drift. **Nobody should commit it blind.** Read it, split it into commits that
  make sense, or discard deliberately — but do not leave four thousand lines of
  unsaved work in the tree.
- [ ] **L4 thesis register / quarterly forecast scoring** — step 8 of the
  screener PRD's build order. Partly superseded by the machine-run portfolio
  (the human forecast register was deleted 2026-09-04 and must not return);
  what remains of step 8 under that decision needs saying out loud.

**screener**

- [ ] **Surface whatever B and C add** — a wider universe, repaired confluence
  axes and any new axis all need to be visible on the watchlist page, or they
  are changes nobody can see.

---

## G. Nifty intraday + F&O (opened 2026-10-20)

Operator, 2026-10-20: look beyond long-only daily stock books -- Nifty, intraday and F&O,
TA or event-based. Quick checks on data in hand (LEDGER 61-63): option sellers ARE paid on
Nifty (VIX above next-month realised vol in 79% of windows, +2.5 pts) but a naked monthly
straddle only breaks even; trend on Nifty/Bank Nifty futures and first-half-hour momentum
are dead.

- [~] **Data** (stockey, backfills launched 2026-10-20 ~15:10 UTC, resumable -- re-run the
  same command to continue or retry):
  - `dhan_option_rolling`: Nifty options, 1-min, 5 years, WEEK1 -> MONTH1 -> WEEK2, ATM+-10,
    CE/PE (`python -m data.dhanlive.option_rolling --underlying NIFTY --years 5 --series WEEK1
    --workers 6`; log `stockey/logs/option_rolling_backfill.log`). ~18 h in all.
  - `nseindia_fo_bhavcopy`: index futures + options EOD, 2016+ (`python -m
    data.nseindia.fo_bhavcopy --from 2016-01-01 --to <date>`; log
    `stockey/logs/fo_bhavcopy_backfill.log`). ~15 h through the NSE gate.
  - Both readable from systrade through `postgres_fdw`. Neither is scheduled yet: add a
    nightly keep-current step once the backfill ends, then turn on compression for
    `dhan_option_rolling` (not before -- compressing mid-backfill is the 2026-08-23 WAL storm).
  - Nifty's own 1-min history comes from `dhan_option_rolling.spot`; a separate index
    backfill into the compressed `dhan_ohlcv_intraday` was skipped for the same WAL reason.
- [x] **Option-selling test** (LEDGER 64, 2026-10-20): weekly iron fly / condor / expiry-day
  fly, held to expiry, real costs. **All three FAILED in-sample** (2021-10..2024-10: −2.8%,
  −2.1%, −0.05% a week on capital at risk). Check period not run, not burned.
### G-goal: "trade less, exit sooner" (operator, 2026-10-21)

Lesson from row 64: working F&O traders skip weeks (event weeks, quiet premium) and manage
trades (take profit early, cut losses) instead of trading every week and holding to expiry.
Already measured on the STOCK books and rejected there (stops row 21-22, timing overlays row
20; only stockey's trend/breadth floor helped, row 5) -- so the goal is F&O-first. Guard:
every skip/exit rule is a dial; pick few, from a reason, written down before looking, and
judge on the unused 2024-11..2026-10 check period.

1. [x] **Event study** (LEDGER 65, 2026-10-21, data to 2024-10 only): election days move ~6x
   what VIX allows, Budget ~2.4x, RBI and expiry days are normal-to-quiet. But the main
   finding is elsewhere: weekly straddles are priced ~15% UNDER VIX and paid out 0.98 of
   their premium -- event-free weeks broke even before costs too. Skipping events cannot
   rescue weekly selling. Next question: are MONTHLY options priced like VIX (row 61's
   gap)? Waits for the MONTH1 download.
   Follow-up (LEDGER 66): sell-when-rich / buy-when-cheap against recent movement shows no
   sort on weekly options (rank correlation +0.01..+0.14, the wrong sign).
   Monthly (LEDGER 67): YES, and front-loaded -- a full-month straddle sold after the roll
   paid out 0.89 of its premium (66% of months won, gross); the last 10 / 5 sessions paid
   out 1.09 / 1.28. Selling near expiry is the losing part.
2. [x] **Managed MONTHLY option selling** -- LEDGER 68, 2026-10-21: **all four FAILED in-sample**
   (2016-2024: hold −7.5%, exit early −3.9% a month on risk; take-profit and stop never
   triggered). Early exit roughly broke even before costs; costs ~14 pts a month. The
   premium is in the tails, which the wings buy back. Check period not run.
   Original plan: (re-aimed by rows 65-67) -- new pre-registered
   family, 3-4 versions: sell the monthly ATM at the roll with wings, exit early (before the
   last ~10 sessions) vs hold to expiry, take profit at half the premium; skip election
   months. ("Sell when VIX is rich vs recent movement" dropped: row 66 found no sort.)
   In-sample 2021-10..2024-10, then the once-only check.
3. [~] **"20 trades a month" Nifty model** -- operator 2026-10-21: the contacts SELL Nifty (some
   Bank Nifty) options, enter/exit on patterns with a stop, stay out around elections, global
   events and trend reversals. LEDGER 70 (exploration): no pattern sorts cleanly. The
   apparent +4%/day from selling a fresh ATM straddle daily was SURVIVORSHIP (big-move days
   fell outside Dhan's +-10 strikes and were skipped); corrected +2.2% gross, ~0 after costs.
   Bank Nifty options not downloaded yet. Rows 61-70 together: Nifty options are about fairly
   priced; Nifty option work paused here (2026-10-21) pending new evidence.
   Reopened 2026-10-22 (operator): Dhan's marketplace (LEDGER 72) -- plain straddles/strangles
   ~0 live after costs, one-day credit spreads +66%/yr on margin with no-skill direction. Row 73
   pre-registered: A intraday stopped straddle, B one-day iron fly. Next: run row 73 in-sample.
   Original plan: -- trade only on days meeting a clear setup, fixed
   exit. Best with the operator's contact's rough method (what they wait for, when they
   exit, when they stay out).
4. [x] **Stock books: one targeted check** -- LEDGER 69: row 20's floor vs matched constant on 5
   more books: 4 fail, **stage2-rs-leaders passes** (14.2%/−37.5% vs 13.4%/−43.8%). A forward
   pair (LEDGER 71) was set up and WITHDRAWN 2026-10-22 before it started: returns are the
   only aim for now, and the floor costs return (14.2% vs 19.2%/yr). Revisit if drawdown starts to matter. Original plan: -- row 5's trend/breadth floor on the current paper
   books, one pre-registered test, not a redo.
- [ ] **Live intraday plumbing** -- live feed, intraday scheduler, kill switch -- only for
  what passes forward paper trading.

## F. Closed — do not re-open without new evidence

- [-] **Short-interval / swing TA (2-10 day holds).** Twice rejected on
  evidence (rows 11-13, 20-22, 30) and now priced out independently: 18-89%/yr
  in costs before any signal. Reopening needs a signal whose *gross* edge clears
  that table, stated in advance.
- [-] **Trend / 200DMA regime overlay.** Row 20: every exposure overlay lost to
  simply holding that much constantly, on both return and drawdown.
- [-] **52-week-high breakouts with trailing stops.** Rows 38 and 40 killed the
  breakouts (−6.4%/yr); rows 21-22 killed the stops — forward returns are flat
  to *rising* with drawdown depth, so no exit level exists.
- [-] **Adaptive combination policies (Hedge / ML).** Row 41: none of eight beat
  the handcrafted 48/52 split.
- [-] **Market cap as a slicing trait.** Rows 27-28: 8.8% coverage, no
  full-history source. Liquidity is the size axis.
- [-] **Worrying about the crawl/LLM bill while choosing the universe.**
  Operator, 2026-09-23: get it right first. Revisit only if a run actually
  fails on cost or rate limits.

### A4. Industry rotation, RS and early Stage 2 (systrader, 2026-10-01)
- [x] **Rotation view** replaces the screener's /stages page (`internal/rotation`, `cmd/rotation snapshot`
      daily, `/api/rotation`; stockey `/api/story-scores` for the fundamental filter column).
- [x] **Pre-registered test FAILED** (LEDGER row 53): restricting Stage 2 to RS-leading industries made
      the book calmer (maxDD −30% vs −47%) but poorer than plain Stage 2 (−0.69%/month, p 0.96). No paper track.
- [x] **stage2-rs-leaders forward paper track** (LEDGER row 54, from 2026-10-01): row 53's plain Stage 2
      reference book, frozen. In-sample 22.1%/yr seen first, so only the forward record counts; judged
      from 2027-10-01 against both controls.
- [x] **stage2-rs-leaders-clean** (row 55): the same book with stockey's fundamental filter (no flaw, story
      score >= median); stockey publishes `fundamentals_story_filter_daily`, synced nightly. Paired with row 54.

