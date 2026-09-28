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
- [ ] **What:** with A1 and A2 built, compare monthly vs quarterly rebalancing
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
- [ ] **What:** replace the L1 screen with `stockey/docs/UNIVERSE_PRD.md`: Layer 1
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

## D. The boundary question

### D1. Momentum + quality — decide where the blend lives
- [ ] **What:** momentum runs in systrader, quality would run in stockey, and
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

- [~] **Stability gate** — 20 clean trading days from the 2026-09-22 dry run,
  completing about 2026-10-17, then capital may move. Nothing to build; do not
  disturb the schedulers.
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

- [ ] **A large uncommitted change set was committed blind as `WIP`** (b4b3230,
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
