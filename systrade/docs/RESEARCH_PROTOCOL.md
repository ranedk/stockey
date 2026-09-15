# Research protocol — two tracks

**Status: ADOPTED 2026-09-08 by the operator.** It amends how Laws 2 and 4 of
`TRADING_BIBLE.md` are applied, and those laws now carry the amendment inline.
Where this file and the bible disagree on research process, this file governs;
on everything else — stories, matched controls, blending, sizing, no
discretionary override — the bible is unchanged and absolute.

## Why the previous process was guaranteed to fail

Six trials (LEDGER rows 11-16), six rejections, and not one of them ever asked
*where* a rule worked. Every trial was: pre-register, run, read a single
number, reject. Three things made that a machine for producing rejections
regardless of merit:

1. **The Bonferroni bar was being applied across unrelated families.** M = 1114
   includes 855 imported cron *monitoring* runs. Requiring t = 4.08 of a new
   idea because a factor-IC monitor ran nightly for two months is arithmetic
   punishment, not discipline.
2. **One-shot holdouts are a spending model with no income.** Thirteen years of
   data, burned once per family, is about five shots ever. We were rationing
   evidence rather than generating it.
3. **There was no exploration at all.** Nothing in the toolchain could answer
   "which slice of the universe did this work in?" — so every hypothesis had to
   arrive from outside, fully formed, and be killed by one number.

Meanwhile the workspace's own history says the opposite of what we kept
testing: everything that ever worked here was CONDITIONING (row 3's drawdown
control, row 5's trend/breadth deployment floor) and everything that failed was
SELECTION (rows 4, 12, 13, 15).

## Track 1 — Exploration

**Purpose:** find a mechanism worth writing down. **Output:** a hypothesis with
a story, never a result.

- Slice freely. Look at everything. `cmd/slice` cuts a rule's edge by
  liquidity, size, own volatility, price, sector, own trend state, market
  breadth and year, each bucket measured against ITSELF on the same day.
- **No significance claims, ever.** No p-values on a winning bucket, no t-stats
  reported, no ledger row that says a bucket "works". The best of sixty buckets
  is what noise looks like; that is why the tool prints the bucket count in its
  header and refuses to rank them.
- Read the SHAPE, not the winner: monotone across a dimension, sign-flips
  between regimes, consistency across halves and years. A number that appears
  in one half and not the other is a dead effect, not a discovery (row 13).
- Exploration IS logged in the ledger with `trials=0` and the word EXPLORATION,
  so nobody later mistakes mined data for virgin data. The ledger exists so we
  cannot pretend we did not look.

## Track 2 — Confirmation

**Purpose:** decide. **Input:** one pre-registered hypothesis with a mechanism.

- **Purged walk-forward, not a single split.** Fit on an expanding window,
  score on the next block, purge the overlap of the forward-return horizon so
  no observation appears on both sides. This measures stability and can be run
  more than once without lying, which one-shot holdouts cannot.
- **FDR within the family, plus deflated Sharpe.** Correct for the search that
  actually happened — the variations tried in THIS family — and report the
  deflated Sharpe against the number of configurations examined. Do not apply
  a workspace-wide Bonferroni across unrelated research families. The
  arithmetic lives in `internal/evidence` (`PairedEdge` for each member's p
  against its control, `Judge` for FDR + deflated Sharpe over the family) and
  the purged folds in `research.PurgedWalkForward`.
- **Forward paper trading is the real gate**, and Law 4 already says so
  ("cheapest honest data: paper-trade the frozen rule forward"). It is the only
  evidence budget that regenerates: every month of live paper data is new,
  uncontaminated, and impossible to mine. Nothing goes live without it.
- The rule is frozen before track 2 starts. A parameter changed after seeing a
  confirmation result sends the whole thing back to track 1.

## What does NOT change

Law 1 (story first), Law 3 (edge is always versus a matched control), Law 5
(blend, never select the best variation), Law 7 (discount every backtest),
Law 19 (no discretionary override of a live decision). Slicing is allowed;
believing an unconfirmed slice is not.

## Amendment 2026-09-13 (operator): the go-live gate is operational stability

The operator has decided that capital may move to a paper-tracked strategy
once the SYSTEM has run cleanly, without waiting for the specs' 12-month
judgement. The backtests' drawdowns and their limits are accepted as known;
performance is no longer a go-live criterion. "Forward paper trading is the
real gate" above now means this gate, not a performance reading.

**Stability gate** — all of these, recorded with dates in the README before
any order is sent:

- **S1:** 20 consecutive trading days of unattended scheduled runs — the
  stockey sync, `paper run` for every forward track, the API — each exiting
  cleanly, with no manual re-run or hand fix.
- **S2:** every order sheet in that window priced off the previous trading
  day's close, with no missing price and no zero or non-numeric share count
  on the Rs 1 crore account.
- **S3:** at least one scheduled rebalance inside the window processed end to
  end: the account book's trades on the day match the order sheet produced
  the evening before.
- **S4:** the account's realised charges below every spec's 100-bps kill level.
- **S5 (before the first real order):** the Dhan execution path, run with
  `LIVE_ORDERS=false`, produces exactly the order sheet for one rebalance.

**What does not change.** Every spec's kill criteria still apply and are
applied mechanically (Law 19) — from go-live they stop real capital, not
just a paper record. The 12-month evaluations still run, and now inform
scaling and the weights of the multi-strategy portfolio instead of the
go-live decision. Frozen parameters stay frozen; a change is still a new
spec and a new clock.

## Amendment 2026-09-15 (operator): no cumulative bar

The workspace-wide Bonferroni bar is dropped, even as "context": a threshold that
rises with every idea ever tried taxes exploration for its own history, and the
gate before capital is the stability gate above, not a backtest threshold.
Ledger rows are still written for every experiment (the audit trail) and M is
still counted, as a record only. A family of variants tested together is still
judged with FDR and a deflated Sharpe over that family's own trials — the
correction that stops the luckiest of three looking like a discovery, and one
that does not grow with later research. Kill screens and pre-registrations
are unchanged.
