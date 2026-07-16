# Stockey Priority Todo

Updated: `2026-07-13`

Single active planning document. Priority-ordered backlog + current-state summary.
This rewrite supersedes the 2026-06-23 audit backlog: that arc (correctness, migrations,
OOM, data-layer robustness) is largely shipped, and the project has moved into the
**discovery-engine / alpha** phase. The governing design is `docs/specs/discovery_engine.md`
(read it first) and `docs/specs/actor_critic_closed_loop.md`.

## How To Use This File (LLM Task Contract)

Each task is a self-contained card: **Why** (problem + evidence), **Files** (the only files to
open; use `rg`, line numbers drift), **Do** (steps), **Guardrail** (authority/point-in-time
boundary), **Validate** (smallest check + acceptance), **Confidence** (`verified` /
`verify-first`).

Global rules (see `CLAUDE.md` + `docs/specs/discovery_engine.md`, esp. §8 — the 2026-07-13 review):
- **Descriptive is well-powered; prescriptive is not.** The cross-section is broad (~2,700 names/day)
  so *measuring* whether a sub-score predicts is sound; but re-fitting weights to claim next-period
  improvement rests on only **~13 independent time blocks** (275 days ÷ 20d windows). Cross-sectional
  breadth is NOT time-series power. Measure freely; re-weight almost never, and only if the adapted
  weights beat frozen ones OOS (kill-switch, spec §8.1).
- **Adaptive tracking, not convergent optimization.** Data is fixed (~13 months). Favor simple,
  low-dimensional, robust methods (pooled cross-section, rank-IC, walk-forward); report IC via
  block-bootstrap CIs, not point estimates (overlapping windows overstate significance).
- **Robustness is the moat.** No discovered relationship earns a knob change without walk-forward
  + multi-definition robustness + enough matured labels + false-discovery control.
- **Priors, not proofs.** Take market truths as ~90% priors; do not re-derive them; size positions
  so the ~10% leaks are absorbed as portfolio risk — and build the risk math that "absorbed" implies.
- **Costs and the north-star are first-class.** Score every edge under a name-specific cost model
  (not a flat 25bps), and against the one honest scorecard: net return vs buy-and-hold NIFTY after
  costs. IC-drift is a warning light, never a portfolio driver (spec §8).
- Review-only everywhere; `broker_execution_allowed=false`; master flags OFF; append-only
  migrations; typed evidence/provenance on every decision; point-in-time (no look-ahead).
- Narrow tests per behavior; env/docs/cron-preflight/migration-drift audits green per commit.

---

## Current State (shipped this arc)

- **Dynamic screener fabric**: regime admission policy, cross-sectional RS ranking, hypothesis
  strategy instances (universe/alignment/retirement), theme screeners, watch retention with
  recorded exits, watch tiering.
- **Coverage lanes**: market-action scan (new-high breakout), volume-surge scan (base breakout),
  momentum-trend scan (multi-day grind) — each a graded `dynamic_source`.
- **Momentum-continuation entry archetype**: archetype-aware technical engine (momentum replaces
  `base_too_deep` with a parabolic guard + orderly-trend structure) + `MOMENTUM_CONTINUATION_V1`
  sleeve. The engine can finally buy a runner (SUVEN: REJECT → NEAR_PIVOT).
- **Measurement substrate**: regret ledger (1,717 matured labels), missed-movers analyser,
  outcome labeling, walk-forward multi-archetype backtest (`archetype_backtest.py`).
- **Conditional allocation Phase A**: momentum sleeve cap — currently NEUTRALIZED (the regime
  tilt was OOS-confirmed backwards; `MOMENTUM_SLEEVE_TILT_ENABLED=false`).
- **T0 invalidation gate SHIPPED (2026-07-13)**: name-specific cost model (`advisory/cost_model.py`:
  statutory + turnover-spread + size-impact; median pick ~45bps vs the old flat 25), delist-honest
  backtest (`archetype_backtest.py` now counts matured-but-gone picks instead of dropping them —
  324 in the full run), and the north-star scorecard (`advisory/north_star.py`). **Finding: the raw
  momentum+breakout lane, equal-weight, returned −9.24% net of cost over 13 months vs NIFTY −3.12%
  — it TRAILS the index by ~6pts** (one −10% window, Sep 2025, dominates; 13 rebalances = wide error
  bars per §8.1). Caveat: this is the *unfiltered lane population*, not the gated recommendation set.
- **Gated north-star SHIPPED (2026-07-13, `north_star --source gated`)**: two findings. (1) STRUCTURAL:
  the strict gated BUY set is **EMPTY** — zero PASS_NOW+entry-confirmed, zero BUY action codes, zero
  broker-executable rows. The live funnel has recorded no buys, so there is literally nothing it
  *would trade* to score (the §8.1/§8.5 "nothing to learn from" made concrete). (2) PROXY: scoring
  the loosest proxy — PASS_NOW flags. **CORRECTED 2026-07-13 (funnel_invariants found the pollution):**
  the first pass (n=320, "+0.6/+1.3/+4.3% excess, day-t up to 2.70, encouraging") was POLLUTED — 47% of
  PASS_NOW rows are `research_only` RESEARCH_TRAINING label-harvesting rows (0.58 bar), not
  recommendations. Excluding them (`north_star` now filters RESEARCH_TRAINING; honest set n=186 over 18
  days): **5d −0.94%, 10d −0.35%, 20d +1.06% excess (day-t −0.48/0.45/0.68)** — the "gate beats NIFTY"
  finding EVAPORATES; the honest recommendation set is ~neutral vs NIFTY. The +excess was research-row
  artifact. (Exactly why the correctness net matters — see below.)
- **WINNER-BACKWARD finding (2026-07-13) — the funnel fishes in the wrong pond.** Stopping the
  circular funnel-output analysis and measuring against REALITY (actual +40%/60d moves in liquid
  names, point-in-time features): the states our admission layer targets have NEGATIVE-to-zero
  expected 60d return — *at-52w-high* mean −1.4%, *hot momentum (+30% already)* mean −3.2% with a
  16.5% blow-up rate, *clean uptrend* mean 0.0%. The money was in the population the funnel EXCLUDES
  by construction: *deep-below-52w-high* mean +9.7%, and *beaten-down + turning on RS* mean +12.4%
  with the LOWEST blow-up rate (4.1%). Survivorship-stress-tested (fill delisted at −60%): +9.7%→
  +7.8%, absolute edge survives. **BUT the kill-test (T0.5) then KILLED the tradeable version**: on
  benchmark-EXCESS + walk-forward the reversal edge is INCONSISTENT (one window) and its beaten-down
  absolute "+9.7%" was recovery-BETA, not repeatable alpha. On excess terms the *breakout* foil was
  the more robust signal (consistent across windows+regimes) despite poor absolute return — the funnel
  picks defensive RS names, so its weak absolute payoff is a market-TIMING problem, not stock-
  selection. Net lesson: the market CYCLE dominates the archetype (feeds T0.75). Detail: spec §9.
- **Funnel correctness net SHIPPED (Part A, 2026-07-13)** — the answer to "be sure the CODE is right,
  not just the data" (the scale bug was found by luck). NEW `advisory/score_scales.py` (single source
  of truth for 0-1 vs 0-100 score fields); NEW `scripts/funnel_invariants.py` audit — an impossible-gate
  /dead-branch detector that statically catches the 0-1-vs-0-100 class + real-data range/cross-field
  checks (in the CLAUDE.md validation checklist, `--strict`); golden-path reachability tests (ideal
  candidate reaches BUY_TRIGGERED→PASS_NOW→technical_entry_confirmed; garbage does not; monotonic; and
  the funnel ceiling is PASS_NOW, NOT a BUY); the confirmed `technical_score` 0-1-vs-0-100 bug fixed via
  `to_100` in `company_memory_review` (capped review-only WATCH) + `action_recommender`; and a new
  `confirmed_entry_exists_but_no_promotion_bridge` no-BUY cause in `recommendation_diagnostics`. Root
  cause of no-BUYs proven ARCHITECTURAL: `action_recommender` emits BUY only from approved portfolio
  rows (:6512) — no candidate→approved→BUY promotion bridge ([P-LLM-AUTH] unbuilt). **The audit already
  surfaced a real warning to chase: 99 PASS_NOW rows are neither BUY_TRIGGERED nor override-sourced**
  (a provenance gap). Part B (the paper decision loop) is the next pass on this verified ground.
- **Paper loop OPERATORIZED into a REVIEW-ONLY daily advisory (2026-07-14)** — the working edge, finally
  surfaced. The paper loop's forward scorer SKIPS the latest (unmatured) date, so today's live picks were
  never shown. NEW `advisory/paper_advisory.py` (+ table `advisory_daily_advisory`, migration, CLI) builds
  the operator's daily advisory: today's RS>=80 picks, each vol-target sized × the DATA-SELECTED regime
  floor (breadth @0.70 → currently full deployment since breadth 0.70 is healthy), ATR stop, name-specific
  cost, + market context (breadth, deployment%, cash%). REVIEW-ONLY by contract: portfolio_authority=none,
  broker_execution_allowed=false, full_advisory_required=true — no broker, no action queue. CRON-WIRED
  (2026-07-14): the coherent daily chain price_adjustment(38) → market_breadth(39) → paper_decision_loop(40)
  → regime_shadow_ledger(42, floor config) → paper_advisory(43); NEW wrappers all_market_breadth.sh /
  all_regime_shadow_ledger.sh / all_paper_advisory.sh; crontab regenerated (28→31 jobs), cron_preflight +
  migration-drift clean. Test: sizing (5% cap / high-ATR down-size) × exposure, ATR stop, review-only
  authority stamped on every row. So the one measured edge now produces a daily operator decision.
  **OPERATOR DASHBOARD (2026-07-14):** `paper_advisory` also renders a self-contained HTML dashboard on
  each run (`reports/daily_advisory.html`, env `PAPER_ADVISORY_HTML_PATH`; gitignored) — today's picks with
  size/stop/cost, market-health chips (breadth, deployment, the ×exposure dial), and a TRACK-RECORD panel
  from matured paper-loop trades (currently +8.0% avg excess/rebalance, 79% of dates beat NIFTY, 63 matured
  dates) with an up/down sparkline. Theme-aware, review-only badge. So the operator sees today's list next
  to how recent picks actually did. Test: renders picks/track/authority + empty-track graceful + standalone wrap.
- **ALWAYS-ON PAPER BOOK SHIPPED (2026-07-14) — the exit side; closes the entry-only gap.** The advisory only
  emitted BUYs, so a paper-trader would never get a SELL. NEW `advisory/paper_book.py` (+ table
  `advisory_paper_book`, migration; `advisory/paper_book_report.py` dashboard) assumes EVERY daily RS BUY was
  taken and manages each to EXIT: pure `decide_action` → HOLD / TRIM / EXIT with reason — stop hit, 20-day
  time cap, momentum fade (RS<`PAPER_BOOK_TRIM_RS`=55), or data-gap/delist (no price for `MAX_MISSING_DAYS`=3,
  which deliberately SURFACES the always-trading edge cases). Dedup (no pyramiding a held symbol); marks/returns
  on adj_close; realized + unrealized P&L and excess vs NIFTY over each name's actual window; CA-mid-hold flag.
  `run_book` does daily-incremental or `--backfill-from` to populate. **Backfilled 2026-05-15→07-10: 30 open /
  51 closed; textbook trend payoff — time_cap 41 exits +13.4% (excess +11.7%), stop_hit 10 exits −14.6%;
  blended realized +7.9% (excess +6.7%, 65% win).** Operator book dashboard (`reports/daily_book.html`, env
  `PAPER_BOOK_HTML_PATH`, gitignored): today's actions (exit/trim/buy with P&L), the open book with a per-name
  signal + stop-distance + flags, realized track-record panel + sparkline. CRON-WIRED at 44 21 (after the
  advisory); `all_paper_book.sh`; crontab regenerated (31→32 jobs), cron_preflight + migration-drift clean.
  Review-only (portfolio_authority=none, no broker). Test: decide_action rule precedence (stop>time>trim, data-
  gap tolerance, RS-unknown). So the system now gives entries AND exits — an always-trading paper model.
- **ADVISORY WIRED INTO THE OPERATOR FRONTEND (2026-07-15).** The recommendations were only in standalone
  HTML, so the operator (looking at operator-web) saw the funnel's ~no-BUYs. NEW read-only endpoint
  `GET /api/advisory/daily` (`build_daily_advisory_payload`, reads advisory_daily_advisory + advisory_paper_book
  persisted tables, computes the book view in pandas; review-only operator_boundary) + NEW page
  `apps/operator-web/pages/advisory.vue` (top-nav "Advisory"): today's actions (exit/trim/buy with P&L),
  today's picks (size/stop/cost), the open book with a per-name HOLD/TRIM/EXIT StatusPill + ⚑ flags, and the
  realized-vs-NIFTY tiles. Matches existing conventions (useOperatorApi/useAsyncData, MetricTile/StatusPill/
  SymbolLink, Tailwind palette). `npm run typecheck` green; payload contract test asserts review-only/no-broker.
  **DAILY-HOME + FRESHNESS pass (2026-07-15):** payload gains a `freshness` block (asof vs latest market day,
  is_current, generated_at, flagged) and a `chain` health list (breadth→advisory→book last-date/rows/current).
  Advisory page now shows a Current/Behind pill, a stale-data banner + flagged-position warning (no silent
  fallback), a 3-step pipeline-health strip (maintenance at a glance) linking to Operations, a track-record
  sparkline vs NIFTY, and explicit empty states. Restart banner reworded/de-alarmed (amber, "API running
  older code — restart to serve current data" + why) — the reports/ staleness false-positive is separately
  fixed. typecheck green; contract test extended for freshness/chain.
  **BROAD CONSISTENCY SWEEP (2026-07-15):** audited all 20 operator-web pages. Fixed the real gaps — 2 pages
  silently swallowed load errors (`hypotheses.vue`, `technical-calibration.vue`: added `error`/`pending` +
  ApiErrorBanner + loading), added a `prompts.vue` empty state, and unified error UX by swapping 7 hand-rolled
  rust blocks (health-hub/scorecard/workbench/positions/llm-decisions/recommendations-unified/watchlist) that
  discarded the real error → `ApiErrorBanner` (now shows the actual API message, a maintenance win). Result:
  ApiErrorBanner adoption 8→18 pages, zero hand-rolled error blocks, every data page surfaces errors. Every
  `<table>` was already `overflow-x-auto` (no mobile gaps). typecheck green; diff clean.
- **FIXED (2026-07-15) operator-API false "restart needed" warning.** `_latest_source_mtime` (advisory/api/app.py)
  scanned `.html` under the repo, so the `reports/*.html` dashboards (rewritten every cron run) were always
  newer than the API process → the "Running API process is older than source files" banner reappeared after
  every restart. Added `reports` to the ignored dirs (like `logs`/`data`/`live_dashboard`); regression test.
- **Coherent paper decision loop SHIPPED (Part B, 2026-07-13)** — the clean forward-tracked path that
  finally generates real outcomes, built on the verified substrate. NEW `advisory/paper_decision_loop.py`
  (+ table `advisory_paper_decision_loop`, migration): BYPASSES the funnel (no promotion bridge needed) —
  SELECT on point-in-time RS computed from OHLCV (the persisted RS panel keeps only ~4 days; RS blends
  whichever of 63/126/252d horizons exist), SIZE via `portfolio_risk.size_position` × crash-floor, COST
  via `cost_model`, SCORE per-trade vs NIFTY (overlap-collapsed, §8.3). Research-only authority (writes one
  table, never the action queue/broker; excludes research rows by construction). **Honest scorecard
  (corrected 2026-07-13 after THREE data-bug fixes below): RS≥80 top-20 hold-20d over ~13mo → net +6.98%,
  NIFTY −0.93%, excess +7.91% (bracket +5.28% to +7.91%), day-level t=3.67, 5% unscored.** Corrected
  regime arc: Dec +17 / Jan −7.5 (the one real momentum-crush month) / Feb +2.3 / Mar +4 / Apr +15 /
  May +12 / Jun −0.7. Stronger and MORE consistent than earlier contaminated reads. STILL not a proven
  all-weather edge: one ~13mo (recovery-ish) regime, t overlap-inflated (§8.3), momentum's tail showed
  in Jan. **Three data/methodology bugs found & fixed (each moved the scorecard):** (a) delist logic
  fabricated −50% for any missing forward price → now `unscored_data_ends` (excluded+bracketed, never a
  fake loss); (b) **EQ→BE series migration** — NSE T2T's high-momentum names when they run up, so an
  EQ-only query lost them → panel now `series IN ('EQ','BE')`; (c) **unadjusted-price corporate actions**
  — splits/bonuses (e.g. SILVER1's 10:1 → a fake −90%) counted as real losses; now a single-day
  circuit-breach guard flags them `unscored_corporate_action`. My earlier "Jan-Feb brutal tail" was
  substantially (c)'s silver-ETF split artifacts, not real momentum losses. **CRON-WIRED (2026-07-13):** `all_paper_decision_loop.sh` (runs the
  module `--persist` via run_with_markers) + crontab entry (40 21 weekdays, post-close, lock+log);
  generated crontab regenerated; cron_preflight/docs-audit/migration-drift clean. So forward outcomes now
  accrue daily. Deferred (minor): research-ledger `start/finish_research_run` wrapper.
- **Silent-data-bug HUNT round 2 (2026-07-13)** — prioritized hunt for the "code runs fine but numbers
  are wrong from a hidden data assumption" class. FOUND & FIXED: (1) unadjusted corporate actions —
  splits/bonuses (SILVER1 10:1 → fake −90%) counted as real returns; single-day circuit-breach guard
  added to `paper_decision_loop` + `north_star`; (2) EQ→BE series migration (285 symbols) → `series IN
  ('EQ','BE')` in both; (3) delist −50% fabrication → honest `unscored_*` (excluded+bracketed);
  (4) benchmark-window misalignment (14% of picks had stock 20-row-exit ≠ NIFTY 20-row-exit) → NIFTY now
  benchmarked over the stock's ACTUAL window. NEGATIVE results (increase trust): NO lookahead in feature
  code (all LEAD is in outcomes), NO stale-latest bug (tables fresh), paper-loop returns match a
  differential recompute exactly. NEW durable audit `scripts/price_data_sanity.py` institutionalizes the
  hunt (CA steps, EQ→BE, cross-source mismatch, adjusted-table coverage, benchmark gaps). Paper-loop
  final honest scorecard after all fixes: **net +6.98%, excess +8.09% vs NIFTY, day-t 3.81** (bracket
  +5.5–8.1%) — still one regime / overlap-inflated / momentum-beta. Open data-health (fix at source):
  NIFTY index missing 5 days incl. 4 in the March crash week; `nseindia_ohlcv_adjusted` covers only 2
  symbols (unpopulated); `technical_features` bhavcopy gap-fill can mix adjusted-dhan + unadjusted-nse
  for ~26 split names; `momentum_backtest` (older/superseded) still needs the guards. `archetype_backtest`
  NOW FIXED (EQ+BE + CA guard; delisted 324→36) — and the "momentum-in-strong-markets is backwards" edge
  SURVIVED the cleaning (still CONSISTENT under above_50dma), confirming it was not a data artifact.
- **Data-layer robustness**: bhavcopy zero-byte guard + morning catch-up, benchmark freshness
  gate (stale NIFTY zeroed rs_vs_benchmark market-wide), Sharpely cache-poisoning fix + encrypted
  v2 endpoint reverse-engineered + fast-source fundamentals, technical-feature bhavcopy fallback,
  data-readiness gate on all long jobs, daily log rotation.
- **PIPELINE RESILIENCE HARDENING (2026-07-16).** 2-day health check found the advisory chain + paper-book
  lifecycle all WORKING (exits/time-caps/stops firing, self-consistent), but market data was stuck at 07-14:
  07-15 (a trading day, dhan had it) never ingested. Root cause: `data.download_runner.run_all_downloads`
  used `continue_on_error=False` → the FIRST failing source `break`s and ABORTS the pipeline, and
  `data.mospi.cpi` (SSL flake) + `sharpely_data` are ordered BEFORE `bhavcopy_downloader`/`indices_downloader`
  → a flaky external macro source skipped the market downloaders AND flipped complete_data to exit 1 daily.
  FIX: criticality-aware runner — `CRITICAL_PURPOSES = {market_wide, benchmark_sync, dhan_ohlcv_precheck}`;
  now runs EVERY step (never aborts) and `_overall_download_status` reports `failed` only on a CRITICAL
  failure, `warning` (exit 0, still visible) for non-critical. Plus `bhavcopy_downloader` gained an in-run
  empty-retry wrapper (`download_bhavcopy_with_retries`, parity with the indices fix). Tests: criticality
  matrix + never-aborts + bhavcopy retry; updated 2 tests that encoded the old stop-on-first-failure.
  **07-15 RE-FETCHED** (the failure was transient): bhavcopy+indices re-downloaded → parsed → adjusted →
  advisory chain advanced to 07-15 (breadth 0.681, 20 picks; book 30 open/64 closed, +4 buys/+4 time-cap
  exits). Advisory is CURRENT again. So a flaky CPI/macro source can no longer stall the market feed.

Key learned facts (do not relitigate): entry-confirmation gate is VALIDATED (regret ledger:
−2.86% forward excess on what it blocks); costly gates are admission/coverage; the "buy strength
when weak" edge is fragile/definition-sensitive; regime is fragile — IC-drift is a *diagnostic*
(warning light), NOT an auto-driver (spec §8.2). 2026-07-13 review facts: ~13 independent time
blocks cap all prescriptive re-fitting (§8.1); momentum picks median turnover ₹41.5cr / p25 ₹19cr
so flat 25bps is optimistic (§8.4); data holds 171/2,166 non-survivors so survivorship bias is
second-order (§8.4); there is no portfolio-level risk math yet and no net-vs-NIFTY north-star.

---

## Build Queue

### T0.5 — Validate "beaten-down + RS-turning" archetype  `[DONE 2026-07-13 — VERDICT: NOT PROMOTED]`

> **Kill-test ran; the archetype FAILED it (spec §9).** 40d benchmark-excess walk-forward was
> INCONSISTENT (W1 −6.2% / W2 +10.5% / W3 +2.0% — one window), and the regime attribution
> self-contradicts (edge in a mostly-rising window, credited to weak-day decisions; ~13 blocks can't
> disentangle). NO reversal lane built — it was the momentum prior inverted, and the harness caught
> it. Key reconciliation: the beaten-down absolute "+9.7%" was recovery-BETA (one episode), not
> repeatable alpha; on excess terms the breakout foil was the more robust (consistent) signal despite
> poor absolute return. Real takeaway → the market CYCLE dominates the archetype (feeds T0.75).
> Scratchpad harness kept; promote to `advisory/reversal_archetype.py` only if it re-validates on more
> data. Below is the as-run kill-test spec.

- **Why.** The winner-backward finding (Current State) said the funnel's admission philosophy looked
  pointed the wrong way for this regime by *absolute* return: the highest expected-return, lowest-
  blow-up state was *beaten-down (deep below 52w high) + turning up on relative strength* (+12.4% mean
  60d, 4.1% blow-up). Before believing it — the mirror-image temptation of the momentum prior that
  already burned us — try to KILL it. See `docs/specs/discovery_engine.md` §9.
- **Files.** exploratory validation first (scratchpad script over `nseindia_ohlcv` + NIFTY); reuse
  `advisory/archetype_backtest.py` walk-forward/regime/delist-honest patterns + `advisory/cost_model.py`.
  Only promote to a real module (`advisory/reversal_archetype.py` + an `ARCHETYPE_FILTERS` entry) IF
  it survives the kill-test.
- **Do (the kill-test, in order — any failure demotes it to `regime_conditional` not `promote`).**
  1. **Benchmark-EXCESS, not absolute.** Beaten-down = high beta; a +tape lifts it for free. Measure
     forward 20/40/60d return MINUS NIFTY. Delist-honest (fill delisted at −60%, count them).
  2. **Walk-forward sign-consistency** across ≥3 sequential non-overlapping windows (magnitude is
     overlap-inflated; sign-consistency is the robust test).
  3. **THE decisive test — regime split.** Tag each decision date by market state (NIFTY above/below
     50DMA). If the excess exists only when the market is rising and vanishes/inverts when it is
     weak, it is recovery-BETA, not alpha → do NOT promote as unconditional; at most a
     regime-conditional lane. If it holds (even weaker) in the weak-market sub-window, it is real.
  4. Foil: run the *breakout-at-high* archetype through the same harness as the negative control
     (expect it to look bad, per the finding).
  5. Breadth/tradability: enough liquid names/day to matter; median turnover of the admitted set.
- **Guardrail.** Research/report-only. Do NOT wire a new lane into the live funnel until it survives
  walk-forward + the regime split with benchmark-excess. No hardcoding "mean-reversion works" — that
  is the momentum mistake inverted. Point-in-time; review-only.
- **Validate.** Verdict is explicit: `promote` (survives regime split) / `regime_conditional` (only in
  favorable) / `beta_not_alpha` / `needs_more_data`. Unit test the excess + delist-fill + regime-tag
  math on a synthetic panel once/if promoted to a module.
- **Confidence.** `verify-first` (the regime split is a genuine coin-flip; expect it may only survive
  conditionally on this one recovery tape).

### T0.75 — The cycle question (#2)  `[OPENED & RESOLVED 2026-07-13 — robustness, not timing]`

> **Resolved (spec §10).** Empirical fact: NIFTY net −2.1% over the sample, worst DD −15.2%, **0**
> 200DMA regime transitions — the whole "cycle" is ONE event (choppy grind → March-2026 −15% crash →
> weak recovery). So cycle-TIMING is unlearnable (N=1 transition; a regime→archetype switcher would
> memorize one anecdote) → §2/§5's demotion of fine-grained regime conditioning STANDS. A pre-committed
> 50DMA cash floor on NIFTY cut maxDD −15.2%→−8.7% and vol 13%→6% but COST return (−2.1%→−4.4%,
> whipsaw in chop) → a coarse floor is legitimate CAPITAL PROTECTION, never alpha. **Answer: go
> cycle-ROBUST on 3 legs — (1) lean on the one cross-regime-consistent signal (RS/breakout on
> benchmark-excess), (2) risk control does the heavy lifting → elevates T2, (3) a coarse conservative
> crash floor.** The funnel's weakness is marginal selection edge in this data, not a re-philosophizable
> error; leverage is not-losing (T2) + the robust signal, discovery engine stays slow research (§8.5).
> (Operator: "do #1, pick #2 later" then "open the cycle question now", 2026-07-13.)

### T0 — Invalidation gate: costs, capacity, survivorship, north-star  `[SHIPPED 2026-07-13]`

> **Result recorded in Current State above** (both the raw lane AND the gated set are now scored).
> The raw lane trails NIFTY; the gated set (PASS_NOW proxy) beats it but unproven; the strict gated
> BUY set is empty. Remaining follow-up: calibrate the cost coefficients (`COST_*`) against real
> fills, and re-score the gated set as more months accrue (the current window is one ~2.5mo regime).
> Everything below is the as-built spec.


- **Why.** Cheap to build, and it can kill or reshape everything downstream. Grounded facts (spec
  §8.4): momentum picks median turnover ₹41.5cr / p25 ₹19cr → flat 25bps is optimistic and the
  ~2-3%/10d edge may be a third-to-half eaten by real cost; the data holds 171/2,166 non-survivors
  (survivorship second-order but the backtest silently drops picks that delist mid-window); and there
  is **no net-vs-NIFTY scorecard** — the one metric that says whether any of this beats owning the
  index. Do this before pointing the heavy machinery anywhere.
- **Files.** `advisory/archetype_backtest.py` (cost model + delist-drop accounting); NEW
  `advisory/cost_model.py` (name-specific spread + size-scaled impact from turnover); NEW north-star
  reporter (reuse backtest forward-return SQL; benchmark = NIFTY).
- **Do.**
  1. **Name-specific cost model**: per-name round-trip cost from avg-daily turnover + price band
     (spread proxy) + a size-scaled impact term for an assumed position size; replace the flat 25bps
     everywhere the backtest scores an edge. Env for the assumed size.
  2. **Survivorship honesty**: when a pick delists/suspends inside the forward window, do NOT silently
     drop it — count and label it (worst-case fill or flat), and report how many picks it affects.
  3. **North-star scorecard**: net portfolio return vs buy-and-hold NIFTY, after the name-specific
     costs, over the full history and per walk-forward window. This is the top-line number.
  4. Re-run the momentum/archetype edges under (1)+(2); report how much edge survives realistic cost.
- **Guardrail.** Research/report-only; no authority, no knob changes. Point-in-time. If the edge does
  not survive realistic cost, that is a finding to record, not a threshold to loosen.
- **Validate.** Unit tests: cost rises as turnover falls / size rises; delist-drop is counted not
  silently dropped; north-star matches a hand-checked toy portfolio. Live: edge-after-cost table +
  net-vs-NIFTY per window. `pytest -q tests/test_advisory_regression.py -k cost_model`.
- **Confidence.** `verify-first` (cost/impact calibration for Indian mid/small-caps needs a sanity check).

### T1 — Sub-score IC *validation* (descriptive; prescriptive reweighting DEFERRED)  `[SHIPPED 2026-07-14]`

> **Result: no sub-score has robust cross-regime selection alpha.** NEW `advisory/subscore_ic.py`
> (+ table `advisory_subscore_ic`, migration `20260714_advisory_subscore_ic`, CLI
> `python -m advisory.subscore_ic`): regenerates the REAL persisted features over full history with the
> REAL builder (`build_technical_features(rebuild=True)`, in-memory) and scores them with the REAL engine
> functions, so the IC is of the ACTUAL sub-scores, not proxies. 1,708 liquid names, ~52k name-days, 55
> dates, forward benchmark-EXCESS on adj_close (delist-honest, ambiguous-dropped), daily cross-sectional
> rank-IC + block-bootstrap CIs, plus the two robustness gates that decide the verdict: the REGIME split
> (NIFTY>50DMA vs weak tape) and walk-forward sign-consistency. **Findings (20d, consistent 5/10/20d):**
> (1) EVERY positive-IC sub-score is favorable-regime-only and collapses to ~0 or inverts in a weak tape
> — `trend` fav +0.099/unf +0.013, `structure_momentum` fav +0.096/unf +0.001, `rs` fav +0.072/unf
> −0.029, `structure_base` fav +0.074/unf −0.031 → verdict `benchmark_beta_not_alpha` for the two
> significant ones. The scorer's apparent edge is a rising-tape (beta/timing) phenomenon, NOT all-weather
> selection — an INDEPENDENT confirmation, from the IC side, of the winner-backward + T0.75 cycle finding
> (the market cycle dominates the archetype). (2) **`participation_base` is robustly INVERTED** — sig
> negative IC (−0.015/−0.025/−0.027), negative in BOTH regimes → verdict `do_not_relax` (the base
> distribution-day/breakout-volume score ranks the WRONG way; a concrete fixable defect). (3) the engine
> `rs` sub-score (rs_vs_benchmark/sector) has NO robust edge and inverts in weak tape — distinct from the
> standalone `rs_percentile` (63/126/252d cross-sectional rank) that drives the paper loop's +7-8% excess,
> so the funnel's "RS" != the RS that works. (4) `tradability` has no return content (it is a liquidity
> gate). NOTHING earns `candidate`. Caveats: small IC magnitudes / marginal CIs / ~13 blocks (the robust
> result is the uniform fav>>unf ASYMMETRY, not any single IC); 252d-window features mature late in the
> sample. Tests: rank-IC recovery / block-bootstrap / regime-split / walk-forward / verdict-mapping /
> delist+ambiguous on synthetic panels. Strategic implication: entry-selection alpha is weak → the
> leverage is not-losing (T2 risk layer), reinforcing the T0.75 resolution. **Prescriptive reweighting
> stays DEFERRED** (report/verdict only; no scorer/threshold/authority change). Two defects recorded in
> Deferred below. Below is the as-built spec.

- **Why.** The scoring function is the heart of every decision and is entirely hand-weighted, and we
  have never measured whether a high sub-score predicts a higher forward return. The *descriptive*
  measurement is well-powered cross-sectionally and is the genuine payoff; it also hands us the
  buy-bar knee. The *prescriptive* auto-reweighting is deferred — it rests on only ~13 independent
  time blocks (§8.1). See `docs/specs/discovery_engine.md` §3 + §8.1.
- **Files.** NEW `advisory/subscore_ic.py`; `advisory/technical_engine.py` (the sub-score
  functions + `DEFAULT_THRESHOLDS`/weights); reuse `advisory/archetype_backtest.py` harness
  patterns (walk-forward, forward-return SQL, regime tagging) — now cost-aware after T0.
- **Do.**
  1. For each sampled historical date, compute each sub-score (trend/structure/participation/RS/
     tradability) per name (point-in-time) and its forward 5/10/20d benchmark-excess.
  2. Metric = **rank-IC** (Spearman of sub-score vs realized forward excess), computed on the
     **pooled cross-section** per archetype (NOT per-stock). Report each sub-score's IC with
     **block-bootstrap confidence intervals** (overlapping windows overstate naive significance),
     shrunk hard.
  3. Buy-bar knee: report, per archetype, the score threshold where forward expectancy peaks
     (research-only; do not auto-apply the bar).
  4. **Prescriptive reweighting is DEFERRED.** Output the IC ranking as a report; a weight change is
     at most a *single human-reviewed* reweighting, gated on the adapted weights beating frozen
     hand-set weights OOS. NO daily auto-reweighter, NO `SUBSCORE_IC_UPDATE_STEP` live loop yet.
  5. **IC-drift = warning light only.** Track aggregate IC vs trailing baseline and *surface* a
     collapse as a diagnostic alert; it must NOT auto-drive de-risking (§8.2). Per-stock residual =
     secondary anomaly flag only.
- **Guardrail.** Research/report-only. Do NOT auto-apply weights to live scoring. Point-in-time: only
  pre-decision data in the measurement; forward windows are the outcome. Deterministic risk limits
  never touched. IC-drift never drives portfolio action.
- **Validate.** Unit tests: rank-IC math on a synthetic panel (known ordering); block-bootstrap CI
  widens on autocorrelated input; pooled-vs-per-stock (per-stock refused/insufficient); drift alarm
  fires (alert only) on a synthetic IC collapse. Live: each sub-score's IC + CIs over history + the
  buy-bar knee per archetype. `pytest -q tests/test_advisory_regression.py -k subscore_ic`.
- **Confidence.** `verified` (descriptive design grounded in the shipped harness; sub-score functions
  confirmed in `technical_engine.py`).

### T2 — Portfolio & risk layer (the asserted-but-unbuilt "absorb the leaks")  `[SHIPPED 2026-07-13; live hook + shadow ledger 2026-07-14]`

> **Live hook + shadow ledger SHIPPED (2026-07-14), on the T1 evidence that entry-selection alpha is
> weak (rising-tape beta) → the leverage is not-losing.** Two pieces, both advisory/research-only, zero
> execution change: (1) `advisory/portfolio_engine.py` now populates two ADVISORY columns per planned
> position — `advisory_sized_weight_pct` (vol-target `size_position` × the resolved 3-state regime
> exposure) and `advisory_regime_exposure` (the multiplier) — next to `invest_score_pct`, via a separate
> migration `20260714_advisory_portfolio_advisory_sizing` so the base-schema checksum is untouched. It
> NEVER touches `approved_allocation_inr`, `position_state`, or the state-transition contract (execution
> stays capital-cap-ladder + `broker_execution_allowed=false`). Regime exposure = new pre-committed
> `portfolio_risk.regime_exposure_multiplier` (risk_on 1.0 / neutral 0.70 / risk_off 0.30; fail-open to
> full). Low live signal NOW (the funnel produces ~no BUYs) — it is the plumbing so the sized-weight is
> there when buys appear. (2) `advisory/regime_shadow_ledger.py` (+ table `advisory_regime_shadow_ledger`,
> migration, CLI) — THE evidence engine: re-books the paper loop's RS pick stream (`advisory_paper_
> decision_loop`, which persists vol-target `weight_pct` AND `floor_multiplier` separately) under three
> sizing policies as pure analysis, vs buy-and-hold NIFTY, net of cost. **Result (1,320 evaluated trades,
> 63 dates, 20d hold):** equal-weight mean +7.19%/hold (vol 10.96, non-overlap maxDD −22.5%); vol-target-
> no-floor +7.30% (maxDD −21.7% — sizing alone adds ~nothing, picks are ATR-homogeneous); **regime_sized
> (vol-target × crash floor) +4.36% (vol 7.19, maxDD −10.7%, avg deployment 53.8%)**; NIFTY −0.85%.
> Verdict `floor_reduces_drawdown_costs_return`: **the floor roughly HALVES drawdown (−22%→−11%) and cuts
> vol (11%→7%) but costs ~40% of the return** — it does NOT improve average risk-adjusted return (ret/vol
> 0.61 vs 0.68), it is DRAWDOWN INSURANCE (exactly the §10 "insurance, not alpha", now measured on the
> real book not just NIFTY). Whether the −22→−11 DD cut is worth ~3pts/hold of return is an operator
> risk-preference call (for a personal account, usually yes). Caveats: non-overlap curve is only 10
> points (thin), one regime (the March crash dominates the DD), overlap-inflated per §8.3. Tests: book
> math per policy / overlap + non-overlap maxDD / verdict; regime multiplier; advisory sized-weight
> report-only (None when ATR absent, never fabricates). Deferred: wire the LIVE portfolio_engine book
> (when it has positions) into the ledger as a policy alongside the paper stream. Below is the
> as-built spec.

> **DATA-SELECTED floor (2026-07-14) — the operator sets an OBJECTIVE, not a threshold** ("I don't want to
> take decisions as long as data takes it"). Honest boundary first: on this one recovery-tape sample,
> maximising terminal WEALTH actually favours LIGHT/NO floor (no-floor compounded +49.8% vs breadth-floor
> +40.9% — the one crash recovered, so full deployment won on growth; the floor only cut drawdown). So
> "how much to insure against a crash the 13mo didn't contain" is the one irreducible preference. Resolved
> by pre-committing ONE standard objective: **maximise compounded growth SUBJECT TO a coarse ruin-guard
> (a wide max-drawdown cap, default −20%, env `SHADOW_LEDGER_RUIN_GUARD_MAXDD`) — "grow the account, never
> risk ruin"**, not a return/DD tradeoff dial. NEW `select_floor_config` / `choose_config` in
> `regime_shadow_ledger` (+ table `advisory_regime_floor_config`, migration) sweeps 13 configs
> (none / nifty×exposure / breadth×threshold×exposure), scores each by non-overlap growth + maxDD, and
> the objective picks: the −20% guard knocks out no-floor (−21.7% DD), and among survivors the data
> chooses a **LIGHT breadth floor @ >=0.50, exposure 0.70** (growth +46.7%, maxDD −15.6%, 82% deployed,
> 12/13 pass, robust) — a gentle trim, NOT the aggressive 0.30 floor. As worse tails accrue the SAME
> objective tightens it automatically; if nothing clears the guard it flips to min-drawdown (most
> protective). Descriptive/report-only; changes no live sizing. Tests: choose_config objective (loose
> guard→max growth, −20→light floor, tight→min-DD binds) + book_from_floor_map rescale.

> **BREADTH market check SHIPPED (2026-07-14) — the richer regime lever (operator: "NIFTY is too simple").**
> NIFTY is cap-weighted, so a few megacaps mask the market most stocks live in. NEW `advisory/market_breadth.py`
> (+ table `advisory_market_breadth_daily`, migration, CLI) computes % of the liquid universe above its own
> 50DMA/200DMA over history from adj_close. **Divergence: NIFTY>50DMA and breadth>=50% AGREE only 67% of
> days** — 54 narrow-rally days (NIFTY up, market narrow) + 37 hidden-strength days. NEW
> `portfolio_risk.breadth_floor_multiplier` (sibling of crash_floor_multiplier, point-in-time shift(1),
> pre-committed 0.50 line). **A breadth floor DOMINATES the NIFTY-50DMA floor on the paper book — robustly
> across every threshold 0.40-0.60 (not a fitted cut):** at 0.50, regime_sized_breadth returns +5.17%/hold
> vs the NIFTY floor's +4.36%, drawdown −6.9% vs −10.65%, ret/vol 0.738 vs 0.606 — same-or-better DD, more
> return, best risk-adjusted, because breadth stays deployed on hidden-strength days and de-risks on narrow
> rallies. Wired as the `regime_sized_breadth` policy in the shadow ledger (verdict
> `breadth_floor_dominates_nifty`); NIFTY floor kept for comparison (operator: alongside, breadth preferred).
> Threshold-FREE (breadth vs own MA) was worse → it is specifically the ~50% LEVEL that is the lever. Tests:
> compute_breadth counts-above-own-DMA + liquidity filter; breadth_floor_multiplier uses-yesterday. Descriptive
> measurement, no fitted knob. **T1 CONFIRM (negative, honest): breadth did NOT sharpen the sub-score SELECTION
> IC split — NIFTY-50DMA is the equal-or-better conditioner there** (fav−unf 20d gaps WIDER under NIFTY:
> trend .086 vs .076, rs .101 vs .058, structure_base .105 vs .001, structure_momentum .095 vs .065; the
> "breadth-sharper" rows are all ~0/noise). So breadth and NIFTY do DIFFERENT jobs: breadth is the better
> RISK/DEPLOYMENT lever (the floor — how much to be invested), NIFTY-50DMA is the better SELECTION-timing
> conditioner (when stock-picking works). Correctly scoped: breadth wired only into the floor
> (`portfolio_risk`/ledger); `subscore_ic`'s regime split stays on NIFTY (now validated as the right choice).
> NOT a universal NIFTY replacement.

> **Shipped as an advisory/report layer** (`advisory/portfolio_risk.py`, `python -m advisory.portfolio_risk`):
> (1) volatility-targeted `size_position` — risks a fixed % of capital to a 2.5*ATR stop, clamped to
> the hard `LLM_DECISION_MAX_POSITION_PCT` cap (volatile names auto-size down: 12% ATR -> 2.5% weight;
> calm names bind on the 5% cap); (2) `book_metrics` — portfolio vol, effective-bets,
> diversification-ratio, avg pairwise corr (catches "one bet wearing many tickers" that per-name caps
> miss); (3) `crash_floor_multiplier` — pre-committed NIFTY<50DMA exposure cut (report: maxDD
> -15%->-10%, vol 12%->8%, but costs return in chop — insurance, NOT alpha); plus `portfolio_heat`
> (aggregate open risk vs `MAX_HEAT_PCT`) and `sector_exposure`. Tests + env + §11. Deferred follow-up:
> the LIVE advisory hook into `portfolio_engine.py` (populate an advisory sized-weight field; do NOT
> change execution) — held back deliberately since it touches live sizing (CLAUDE.md: don't mutate
> broker behavior unprompted). Below is the as-built spec.


- **Why.** DOUBLY confirmed by the momentum→reversal→cycle arc (spec §8.3 + §10): archetype selection
  is fragile and cycle-timing is unlearnable (N=1 transition), so the real leverage is NOT-LOSING —
  position sizing, cost-awareness, and not being over-deployed into the next March-style crash. The
  spec says "size positions so the ~10% leaks are absorbed as portfolio risk" but there is no risk
  math: no correlation structure, no strategy drawdown estimate, no sizing model, no crash floor. For
  a personal account this dominates returns more than any entry-score decimal. Add a fourth item:
  the coarse capital-protection floor (§10) — pre-committed, conservative, sold as risk not alpha
  (on NIFTY it cut maxDD −15%→−9% / vol 13%→6% but cost return in chop; size it accordingly).
- **Files.** NEW `advisory/portfolio_risk.py`; `advisory/portfolio_engine.py` (sizing hook);
  reuse `advisory/archetype_backtest.py` for drawdown/vol measurement; T0's cost model.
- **Do.**
  1. Position sizing from volatility/ATR + a per-name risk budget; a hard cap so no single name is
     catastrophic (ties to the existing deterministic position/exposure limits).
  2. Correlation-aware exposure: estimate co-movement across held/candidate names (sector + return
     correlation) so the book is not one bet wearing many tickers.
  3. Strategy-level drawdown + volatility report over history (net of T0 costs), and a coarse
     risk-off safety floor (spec §2) as a documented capital-protection rule.
- **Guardrail.** Advisory sizing/report; the deterministic risk/stop/exposure gates remain the hard
  floor and are never loosened by this layer — it can only be *more* conservative. Review-only rows.
- **Validate.** Unit tests: sizing shrinks with volatility and respects the hard cap; correlation
  estimate on a synthetic 2-name panel; drawdown math on a toy equity curve. Live: portfolio vol +
  max-drawdown report net of cost. `pytest -q tests/test_advisory_regression.py -k portfolio_risk`.
- **Confidence.** `verify-first` (sizing/correlation design + portfolio_engine integration points to confirm).

### T3 — Exit-as-scored-decision (design pass, then build)

- **Why.** Exits are the unmeasured half of the P&L. Frame: symmetric to entry — a reversal score
  from features that predict a forward drawdown, validated by the same harness, with an ATR/gap
  disaster-stop and time cap underneath. See `docs/specs/discovery_engine.md` §4.
- **Files.** design note first; then NEW `advisory/exit_engine.py` + reversal features in
  `advisory/technical_features.py`; `advisory/portfolio_engine.py` / lifecycle for exit rows.
- **Do.**
  1. **Design pass (do this first, no code):** short literature-informed note — reversal feature set
     (distribution-day clusters, break of rising DMA20/50, RS deterioration, momentum divergence,
     up-day volume dry-up), the primary=scored-reversal + floor=ATR/gap-stop + time-cap structure,
     and the metric (does the reversal score predict forward drawdown?).
  2. Build the reversal features + a `score_reversal` validated by the archetype-backtest harness
     (exit-side sibling: measure forward *drawdown* after the reversal score crosses a threshold).
  3. Exit decision = scored reversal crossing a discovered threshold, with the ATR/gap stop as the
     hard floor and a max-holding time cap. Review-only rows; no broker execution.
- **Guardrail.** Review-only; `broker_execution_allowed=false`. Exit thresholds discovered/validated,
  not hand-forced. The ATR/gap stop is a capital-protection floor (allowed as a simple rule).
- **Validate.** Reversal-score predicts forward drawdown on history (backtest); unit tests on the
  reversal features + threshold logic + the disaster-stop floor.
- **Confidence.** `verify-first` (needs the design pass; exit lifecycle integration points to confirm).

### T4 — Daily discovery run + trust gate  `[DEFERRED — see §8.1]`

- **Why.** Wrap the descriptive engine into the self-correcting loop the operator asked for: a daily
  "what can we do better" run + knobs that self-adjust within guardrails. See
  `docs/specs/discovery_engine.md` §6 and `actor_critic_closed_loop.md` (Stage 1→3).
- **DEFERRED (2026-07-13 review).** A daily multi-knob auto-tuner on ~13 independent time blocks
  manufactures false winners faster than false-discovery control can catch them (§8.1). Build only
  after more data exists OR T1's descriptive pass proves the signal-to-noise supports self-tuning,
  AND T2's portfolio floor is in place. Until then the "daily run" is a **read-only report**, not an
  applier; IC-drift stays a warning light (§8.2).
- **Files.** NEW `advisory/discovery_run.py`; a governed-parameter store (from the actor/critic
  spec §5) so knobs are versioned/revertable; cron wrapper + entry.
- **Do.**
  1. Daily run: re-run the T1 descriptive IC pass (and T3 exit metrics) on recent data; emit a
     human-legible ranked report — which knobs, moved how much, would improve OOS expectancy; which
     findings are robust vs fragile. Read-only until the deferral condition above is met.
  2. **Trust gate (first-class):** a knob moves only after walk-forward + multi-definition robustness
     + enough matured labels + **false-discovery control** (the danger of a daily multi-knob scan —
     correct for it), then a small bounded reversible step, monitored with auto-revert.
  3. Governed-parameter store: knobs read through a resolver (env/YAML fallback) so the run can
     propose/apply/revert with provenance.
- **Guardrail.** Default-OFF master flag for any auto-apply (mirror `[P-LLM-AUTH]`). Tier-C
  (risk/stop/exposure/authority) never auto-touched. Every proposal + application carries typed
  evidence/provenance.
- **Validate.** Trust-gate unit tests (rejects a finding that fails FDR / lacks labels / regresses
  OOS; accepts a robust one); dry-run report on real data; auto-revert on a synthetic degradation.
- **Confidence.** `verify-first` (largest piece; build after T1 proves the signal-to-noise supports it).

---

## Standing Decisions (do NOT build — keep as intuition-seeded rules + monitoring)

- **Source-lane weighting — dropped.** Lanes are junk-eliminating filters, not weighted
  contributors. Keep the regret ledger + missed-movers as the directional health check; prune a
  losing lane manually.
- **Catalyst standalone predictor — not built.** ORDER_WIN / RESULTS_POSITIVE are a ~90% prior;
  they act as a conditional conviction/size enhancer ONLY when the technical pipeline also fires in
  a favorable tape. Never a standalone trigger.
- **Fine-grained regime conditioning — demoted.** Replaced by the discovered IC-drift signal (T1)
  + a coarse risk-off safety floor (a simple capital-protection rule, not an alpha bet).

---

## Deferred / Lower Priority

- **[DEFECT DIAGNOSED + reviewed default-OFF fix wired 2026-07-14] `participation_base` sub-score is INVERTED.**
  `score_participation` (base) has a significant NEGATIVE rank-IC vs forward excess (−0.015/−0.025/−0.027 at
  5/10/20d), negative in both regimes — verdict `do_not_relax`. **Diagnosed (scratchpad, real subscore_ic
  build):** the driver is the BREAKOUT-DAY VOLUME term — `breakout_day_volume_vs_20d` has fav-regime IC
  **−0.111** (high breakout-day volume = climax/EXHAUSTION → fade, not continuation); two other terms also
  look mis-signed (`distribution_days` +0.025, `pullback_dryup` +0.055, both opposite to how the score
  treats them), and `up_down_volume_ratio_20d` is an all-NaN dead term. **Fix measured on the TOTAL score
  (base archetype):** current IC +0.0375 → neutralize participation +0.0465 → invert +0.0557 (monotonic;
  only invert is nominally significant). Just removing the breakout-vol REWARD barely helped (+0.0387) — to
  capture the exhaustion insight you must PENALISE it, not merely stop rewarding. **BUT the gain is small,
  CIs overlap, favorable-regime-only (~13 blocks) → below the bar for a live DEFAULT change (§8.1).** So
  wired as a **reviewed, DEFAULT-OFF opt-in** (`TECHNICAL_NEUTRALIZE_PARTICIPATION_BASE=false`,
  `advisory/technical_engine.py`): when enabled, `score_participation` returns the cap midpoint (constant →
  zero counterproductive cross-sectional ranking, 0-100 scale preserved); default keeps current behavior,
  momentum participation untouched (it was ~neutral, not inverted). Enabling shifts the score distribution
  so the buy bar needs recalibration. Test: default-off unchanged / on→constant / momentum unaffected.
  Not yet enabled anywhere — turn on only after more matured data clears walk-forward + FDR.
- **[DIAGNOSED + plumbed + reviewed default-OFF fix wired 2026-07-14] the funnel's "RS" != the RS that works.**
  The engine `score_relative_strength` leans on 20d `rs_vs_benchmark` (IC +0.007 = noise) so the sub-score
  has no robust IC (fav +0.072 / unf −0.029). **Diagnosed (scratchpad):** the real RS signal is the
  ~3-month (63d) CROSS-SECTIONAL momentum RANK — `r63_rank` IC +0.049 (fav +0.119 / unf −0.006, the least
  regime-fragile RS variant), `rs_percentile` (63/126/252d blend) +0.042; the 12-month (252d) rank INVERTS
  (−0.053, winners fade), so 60/120 is the improved blend. **Swapping rs_percentile into the RS slot lifts
  the TOTAL score IC the most of any fix: +0.0375 → +0.0582** (fav +0.091 → +0.144). Same caveats (CIs
  overlap, favorable-regime-only, ~13 blocks) → below the live-DEFAULT bar. **Plumbed + wired (operator:
  flag + plumb):** `advisory/technical_features.py` now computes a cross-sectional `rs_percentile` (60/120d
  rank per asof-date, point-in-time) as a persisted column (migration `20260714_advisory_technical_daily_
  rs_percentile`; added to `ordered_cols` + `rule_engine.load_technical` SELECT); `score_relative_strength`
  has a reviewed DEFAULT-OFF flag `TECHNICAL_RS_USE_PERCENTILE` that, when on AND rs_percentile present,
  maps it onto the 0-15 slot, else falls back to the current buckets. Default unchanged; verified the column
  populates 0-100 cross-sectionally. Test: flag off (ignored) / on→scaled / on+absent→fallback / capped.
  Not enabled anywhere — turn on only after more matured data clears walk-forward + FDR.
- **[FIXED 2026-07-13 in the Part-A correctness net] technical_score scale mismatch (0-1 vs 0-100).**
  `rule_engine.py:1217` persists `technical_score`/`setup_score` as 0-1; consumers compared them to
  0-100 thresholds (dead branches; `conviction_score` persisted ~0). Fixed via `advisory/score_scales.py`
  `to_100()` in `company_memory_review` (conviction scaled; branch capped review-only WATCH, never BUY)
  and `action_recommender`; the 3 BUY-asserting tests moved to 0-1 + WATCH. `scripts/funnel_invariants.py`
  now guards the class (impossible-gate detector). No longer open.
- **[RESOLVED 2026-07-13] Provenance-gap warning → not a bug; corrected the audit + a real pollution
  finding.** The flagged PASS_NOW-without-trigger rows are NOT a bug: `candidate_state=PASS_NOW` is set
  by `setup_score >= the setup's OWN pass_now threshold` (per-setup: RESEARCH_TRAINING 0.58, EVENT 0.66,
  DEFENSIVE 0.68, …), independent of BUY_TRIGGERED — so a PASS_NOW row legitimately carries
  technical_state=IGNORE. (The earlier "PASS_NOW only from BUY_TRIGGERED" map was incomplete; the audit
  check encoded it → false positive, now fixed.) REAL finding it surfaced: `EVENT_MODEL_TRAINING_V1` is
  `research_only:True` and floods PASS_NOW (47%) for label harvesting; the audit now flags
  `research_only_rows_in_pass_now_population`, and `north_star` excludes them (which overturned the
  gated "+excess" finding — see Current State). **Research/live separator BUILT (2026-07-13):** typed
  `research_only BOOLEAN` column on advisory_candidates (migration `20260713_advisory_rule_outputs_
  research_only`, stamped from `setup.get('research_only')` at build, backfilled from the
  RESEARCH_TRAINING family). `north_star` filters `research_only=FALSE`; `funnel_invariants` flags
  leakage via the typed column (family fallback for pre-migration rows). `apply_ts_forecast_rescue`
  (`rule_engine.py:2204`) IS honest (stamps override). Remaining nuance (not urgent): PASS_NOW still
  overloads "aggregate-score-pass" vs "entry-confirmed" — that is by design (setup_score-driven).
- **[DATA-SCOPE — surfaced 2026-07-13] EQ-only backtests silently drop EQ→BE series migrations.**
  `archetype_backtest.py`, `north_star.py`, `momentum_backtest.py`, and the winner-backward queries all
  filter `series='EQ'`, so any name NSE moves to the BE (Trade-to-Trade) surveillance segment loses its
  forward bars — and that hits the RS/momentum leaders hardest (they get T2T'd *because* they ran up).
  `paper_decision_loop` now uses `series IN ('EQ','BE')`; the other backtests should adopt the same
  EQ+BE continuity so their forward-return coverage stops being biased against the exact winners. Not
  urgent (research snapshots) but it modestly biased the earlier gated-north-star / archetype numbers.
- **[PERMANENT SOLUTION BUILT 2026-07-14] Corporate-action price adjustment from the price series itself.**
  Audit result: the recorded CA table is incomplete (misses ETF splits), `nseindia_ohlcv_adjusted` covers
  only 2 symbols, and DHAN is adjusted but covers only 24/78 split names (misses SILVER1/ETFs) — so NO
  external source is complete. But a split/bonus is ALWAYS a clean round-ratio single-day price step, and
  that ratio IS the factor. NEW `advisory/price_adjustment.py` `adjust_frame()` derives a complete
  cumulative back-adjustment from price steps alone (validated: SILVER1 fake −89.6% → true +4.4%);
  universe: 57/78 CA steps auto-adjusted (`split_bonus`), 21 flagged `ambiguous` (possible data errors,
  NOT adjusted — no silent rescaling). Tested. ROLLOUT REMAINING: (a) replace the exclude-only CA guards
  in `paper_decision_loop`/`north_star`/`archetype_backtest` with adjustment via this module (so CA picks
  are SCORED at their true return, not discarded); (b) a builder that populates the adjusted table for
  consumers; (c) corroborate the 21 ambiguous with CA records/dhan/volume.
- **[ROLLOUT DEPLOYED 2026-07-14] adjusted prices are now the source of truth for the live loop.** NEW
  table `advisory_adjusted_ohlcv_daily` populated by `advisory.price_adjustment` (684k rows, 128 split/
  bonus events auto-adjusted, 25 ambiguous flagged; migration `20260714_advisory_adjusted_ohlcv_daily`);
  cron-wired `all_price_adjustment.sh` at 38 21 weekdays, BEFORE the paper loop (40 21). `paper_decision_
  loop` now reads `adj_close` for returns/RS (turnover/ATR stay raw = split-invariant), scoring split
  names at their TRUE return instead of excluding them (evaluated 496→509, unscored 5%→2%; excess
  +8.09%→+7.30% t=3.57 — LOWER because the silver ETFs are now included at their real ~flat return, the
  honest number). `price_data_sanity` adjusted-table warning resolved.
- **[FOLLOW-UPS DONE 2026-07-14] adjusted prices rolled out to the research backtests + engine improved.**
  `north_star` (gated) and `archetype_backtest` now read `adj_close` for returns/RS/trailing filters
  (turnover/min-price on raw = split-invariant; `ambiguous` flag replaces the step-guard). The
  "momentum-in-strong-markets is backwards" edge STILL HOLDS on adjusted prices (above_50dma CONSISTENT)
  — robust, not an artifact. Ambiguous corroboration: 0/25 had CA records (table incomplete confirmed);
  found the round-ratio set missed large ETF splits (LICMFGOLD/IVZINGOLD 100:1) → extended `_ROUND_IMPLIED`
  to 100; rebuilt (split_bonus 128→132, ambiguous 25→21; the 21 are low-volume mid-range steps = likely
  data errors, correctly left unadjusted). REMAINING (operator/documented, not code): (a) `technical_features`
  bhavcopy gap-fill should read `adj_close`×`cum_adj_factor` for OHLC instead of mixing adjusted-dhan +
  unadjusted-bhavcopy (live feature code, narrow ~26 names — deferred to avoid live risk); (b) NIFTY index
  gap — CORRECTED: dhan DOES have the NIFTY 50 index (`ticker='NIFTY',instrument='INDEX'`, agrees with
  nseindia exactly), so `archetype_backtest._load_benchmark` now UNION-gap-fills NIFTY from dhan (all 3
  backtests import it). Dhan checked EXHAUSTIVELY: `dhan_ohlcv_daily` has only NIFTY (security_id 13,
  IDX_I) covering Mar-20; `dhan_ohlcv_intraday` has NIFTY for Mar-23 only. `_load_benchmark` now
  priority-coalesces nseindia_indices → dhan daily → dhan intraday-last-bar → filled Mar-20 + Mar-23
  (gaps 5→3). The remaining 3 real trading days (Mar 24/25, Jul 9 — verified real: stocks moved) are
  missing from ALL sources. **NSE re-fetch path**: `nseindia_indices` is populated by
  `data.nseindia.indices_parser` (parses NSE daily `ind_close_DDMMYYYY.csv` files that
  `data.download_runner --phase all` fetches). ROOT CAUSE FOUND: the original downloads for Mar 24/25/
  Jul 9 wrote **0-byte archives** (transient NSE failures), which then blocked re-fetch (downloader skips
  existing keys) and were marked processed-empty by the parser. **FIXED 2026-07-14 at source:** deleted
  the 3 empty archives (`store.delete_file`), re-downloaded via `indices_downloader --backfill --from-date/
  --to-date` (NSE served the real files), cleared the parser processed-state (`ingestion_state.clear_state`
  + Redis `indices:parsed`), re-parsed → Mar 24=22912.4, 25=23306.5, Jul 9=23962.8 now in nseindia_indices.
  `price_data_sanity` benchmark ERROR RESOLVED (status ok, 0 errors); paper-loop 26→27 decision-days,
  529 trades, excess +7.61% t=3.63. The whole NIFTY gap (all 5 days) is closed.
- Phase B conditional allocation (performance-following sleeve tilt) — only after T1's IC-drift +
  matured sleeve grades exist; the regime tilt stays neutralized until then.
- Theme-screener suggested-query auto-execution (manual Screener.in registration stays).
- Watch tiering decay / theme watch-tiering refinements.
- `sharpely_stock_meta` was cache-poisoned then rebuilt; keep an eye on the weekly refresh cadence.
