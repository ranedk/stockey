# Portfolio ruleset — mechanical evaluator with an LLM adjudicator

Written 2026-09-04. Supersedes nothing; extends `FUNDAMENTAL_SCREENER_PRD.md` §12
(confluence scoring) into an actual portfolio, and depends on the boundary revision
recorded in `CLAUDE.md` ("Boundary with systrader", 2026-09-04).

## What this is, and what changed

stockey now builds and runs a fundamental swing/long-term portfolio. The operator's
framing: *"a way for the LLM to create logical rules to create a portfolio, then invest
real money into it and if the logic is directionally fine, make few losses."*

That is a real reversal of §1/§3.3's "no LLM makes a capital decision", and the reversal
is deliberate. But it is narrower than it first looks, and the narrowing is the whole
design:

- **An LLM AUTHORS the rules.** They are committed, versioned, human-readable, and
  execute deterministically. This is not what §12 argued against.
- **An LLM ADJUDICATES each action the rules propose**, with asymmetric powers (below).
- **An LLM never SELECTS a position.** It cannot add a name the ruleset did not pass.
  The ruleset is the upper bound on what enters the portfolio; the adjudicator can only
  narrow it.

§12's objection stands and is respected: *"a well-argued LLM thesis is harder to audit
than a single-signal reaction, not easier, because it sounds more trustworthy without
being any more falsifiable."* A per-position LLM buy call is unauditable. A versioned
rule plus a recorded, reasoned veto is auditable — you can read the rule, read the veto,
and later score both.

## The measurement problem, stated honestly

The operator's own doubt: *"I have serious doubts that we will be able to get clear
indications of what combination of signals work and what doesnt."*

That is probably right, and it is the reason for the architecture rather than an
argument against it. If attribution is hard with a FIXED rule, it is impossible when the
decision process itself drifts between decisions — a model update or a prompt change
silently makes position #21 a sample of a different strategy than position #1. Weak
measurement demands a MORE stable decision process, not a looser one.

Hence: every position records `ruleset_version`, and every adjudication is recorded with
its reasoning. The sample accumulates against fixed objects.

A true historical backtest is not attempted, and is not merely being skipped for
convenience: the confluence axes read point-in-time L2 state this repo has no history
for, so any "backtest" would be reconstructed with hindsight and would flatter itself.
Forward-recording is slower and honest. `hit_rate_by_confluence_count` already refuses
to show a number below 5 resolved samples for exactly this reason.

## v1 ruleset

Deliberately two conditions. Not because two is magic, but because we have no evidence
that a third earns its place, and each added condition shrinks the candidate pool fast.

```
ENTER when:
    evaluable_count     >= 1          (at least one axis could actually be judged)
AND contradicting_count == 0          (and none of the judged axes argues against)
AND weinstein_stage     == 2          (price is in an advancing base breakout)
```

**The `evaluable_count >= 1` guard was NOT in the first draft of this rule, and running
the draft against live data is what caught it.** `contradicting_count == 0` is trivially
satisfied when NOTHING could be evaluated: 4 of the 43 "clean" watchlist names had
`0 for / 0 evaluable`, and two of them (BLKASHYAP, GANDHITUBE) sailed into the candidate
list on an empty scorecard. That is absence of evidence being read as evidence of
absence -- the rule was silently strongest exactly where it knew least.

Whether the floor should be higher than 1 is a genuine open parameter, deliberately left
at the minimum that makes the rule *meaningful* rather than at a number that feels
prudent. Raising it to 2 re-introduces the "more evidence of the same kind" reasoning
this ruleset argues against elsewhere, so it should be a decision the RECORD makes, not
this document.

Measured against the live watchlist on 2026-09-04:

```
108  active watchlist
 43  contradicting_count == 0
 24  ...AND confluence_count >= 2      (the operator's first proposal)
  8  ...AND Stage 2                    (the operator's first proposal, from the 24)

v1 as specified below:
 43  contradicting_count == 0
 39  ...AND evaluable_count >= 1       (4 dropped: nothing was evaluable at all)
 10  ...AND Stage 2                    <- v1 candidates today
```

**Why `contradicting_count == 0` rather than `confluence_count >= N`.** These buy
different things. More supportive axes is more evidence *of the same kind*, all read off
the same balance sheet; absence of a contradicting axis is the absence of a specific,
named argument against. We have no evidence a 3rd supportive axis predicts better than a
2nd. We have a structural reason to think a contradicting one matters.

**Why Stage 2 is the higher-value half.** It is the only condition here that is
*independent* of the fundamental axes — a price read, from systrader, updated daily. A
conjunction of five correlated fundamental axes adds far less than one fundamental read
plus one price read. Independence is what makes an AND worth anything.

**Why stage comes from systrader.** Per the revised boundary, stockey does not compute
price signals. It reads `GET /api/stage` (systrader `cmd/api`). A stage read that is
missing or stale is a FAIL-CLOSED condition: no stage, no entry. 6 of the 24 currently
have no stage read.

## Exits — and the one asymmetry that matters

Entry and exit are not mirror images, and treating them as such is the main design error
this section exists to avoid.

On entry, the adjudicator can only reject. The worst case is a trade not taken —
bounded, and the ruleset remains the ceiling. On exit, a veto means *staying in a
position the ruleset says to leave*: not a subset of the rule but an extension of
exposure, with unbounded downside. It is the classic way a systematic process degrades,
and it is what `systrader/TRADING_BIBLE.md`'s Law 19 (no discretionary override during
drawdowns) exists to prevent. stockey is not governed by that bible, but the reasoning
transfers exactly.

So exits are split by kind:

| trigger | adjudicator may veto? | rationale |
|---|---|---|
| **stop-loss / risk** | **NEVER** — unconditional | The one exit that must not be reasoned with. A stop is a statement about capital, not about the thesis, and every argument for holding through it is available at every price. |
| **thesis invalidation** | Defer, not cancel | The named invalidation criterion may have tripped on noise. The adjudicator may hold for ONE more evaluation cycle and must re-justify next run. |
| **target date reached** | Yes | Staleness, not danger. |

"Defer, not cancel" is load-bearing: a veto that silently persists is indistinguishable
from having no exit rule. Each deferral is recorded, capped, and re-argued.

## Architecture

```
  nightly:  ruleset v1 (mechanical, deterministic)
                 |
       candidates + full signal detail
                 |
            ENTRY ADJUDICATOR (LLM)  -- may REJECT only
                 |
      thesis written by the adjudicator (prediction, target date, invalidation,
        and a machine-checkable metric wherever the claim reduces to one)
      L5 sizing (ADV-capped equal weight) -> position opened
                 |
  nightly:  EXIT EVALUATOR (mechanical: stop / invalidation / target)
                 |
            EXIT ADJUDICATOR (LLM)  -- veto per the table above
                 |
  nightly:  FORECAST RESOLUTION at target date -- mechanical where a metric exists,
            judged otherwise, method recorded either way. No human step.
```

Every row records `ruleset_version`, the adjudicator's decision, and its reasoning.

## Stops — sized, not chosen

The operator's instruction was *"if we dont have a strong stop-loss basis, we can put it
at a 5-15% or so"*. That is held as a **band**, not a number, because a flat percentage
means opposite things across this universe. Measured on the 2026-09-04 candidates,
HESTERBIO's ordinary 10-day noise is ±12.2% (3.87% daily vol) while SOFTTECH's is ±5.8%.
A flat 5% stop on the first is hit by noise within days; a flat 15% on the second is
~2.6σ and would essentially never trigger.

So the stop is `1.5 × the 10-day 1σ move`, clamped into 5–15%. Every position then carries
roughly the same probability of a noise-triggered exit, which is what a stop is for.
Today's book runs 8.7% (SOFTTECH) to 15.0% (HESTERBIO).

**The adjudicator does not choose the stop level**, and this is deliberate rather than an
oversight: if it could, it could neuter the one unvetoable rule by always choosing the
widest allowed value — the ban on vetoing a stop would survive in letter and not in
spirit. The same reasoning clamps the target date (below).

## The forecast lifecycle (no human anywhere in it)

Every accepted position records a `prediction_text`, `target_date` and
`invalidation_criteria` written by the entry adjudicator. The prediction must be a
FUNDAMENTAL claim, never a price target.

These live on `fundamentals_portfolio_position`. The human forecast register
(`fundamentals_l4_thesis`, `l4_thesis.py`, and the `POST /api/portfolio` create/resolve
endpoints) was **deleted 2026-09-04** at the operator's instruction: *"remove the human
forecast completely. Let there only be machine created portfolio and forecast."* It held
zero rows — no human had ever written one — so nothing was lost.

**Deleting the human resolver is what forced `portfolio_resolution.py` to exist.** The
one human act in the old design was resolution: a person decided whether a prediction came
true and, when it did not, whether it was `thesis_wrong` or `thesis_right_market_hasnt_paid`.
Remove the human and put nothing in their place and the register never resolves — and it
does not break loudly, it reports "0 resolved" forever.

### Resolution, and the self-grading problem

A model that both writes and grades its own forecasts will report a flattering hit rate,
and that number is *worse than no number* because it looks like evidence. Two things are
done about it:

1. **The adjudicator must reduce its prediction to a machine-checkable comparison** against
   a real numeric `fundamentals_l2_state` column, wherever it honestly reduces. Those
   resolve mechanically and the model's prose is never consulted. The column menu is read
   live from `information_schema`, and an invented name is discarded rather than stored —
   an invented metric is worse than none, because it looks checkable and silently never
   resolves. On the first v3 run, 8 of 8 accepted names produced a valid metric and none
   invented a column.
2. **Where it does not reduce, a judged read happens** — but `resolution_method` records
   which path was taken and every hit rate is reported SPLIT by it.

If the judged hit rate runs well above the mechanical one, that gap is the self-grading
bias, made visible rather than argued about. It is a measurement, not a guarantee, and it
is the honest limit of this design.

An unresolvable forecast stays OPEN. Writing `false` on a failed grading manufactures a
miss out of missing data. (Observed live: asked to grade forecasts whose target dates had
not passed, the resolver declined all eleven rather than guessing.)

### Failure attribution, now derived rather than judged

`thesis_wrong` vs `thesis_right_market_hasnt_paid` — the source spec notes these "look
identical in P&L and demand opposite corrections". It was the human's call. It is now
derived from the two outcomes already recorded: the forecast result and the price result.
A wrong forecast that happened to make money is still `thesis_wrong`; recording it as a
success teaches the process the opposite of the truth.

**Forecast resolution is independent of position closure.** A name stopped out in month
two still has a forecast that resolves at its target date. Collapsing the two would
destroy the only distinction that matters for correction.

`target_date` is **clamped to 60–365 days** for the same reason the stop level is not the
adjudicator's to pick. An unclamped model can push the date far enough out that the
`target_date` exit can never fire — a rule neutered while formally obeyed. On the
2026-09-04 book the clamp did not bind on any of the ten; it is a guard, not a correction.

Of the three, only `target_date` is a live mechanical trigger. `invalidation_criteria` is
recorded as the audit object that makes a position's reasoning checkable in six months;
it is deliberately not machine-evaluated in v1, because a criterion the LLM both writes
and judges is not a constraint on the LLM.

## Exit triggers as implemented

`portfolio_exit.py`, in strict priority order — the order matters, because a position can
satisfy several at once and only the first is reported. If invalidation were reported
ahead of a breached stop, the exit would become deferrable, routing a stop through the
adjudicator by the back door.

| # | trigger | condition | adjudicated? |
|---|---|---|---|
| 1 | `stop_loss` | adjusted close ≤ `entry × (1 − stop_pct/100)` | never |
| 2 | `thesis_invalidation` | a contradicting axis appeared, or the stage read left 2 | defer ×1 |
| 3 | `target_date` | the committed target date passed unresolved | defer ×1 |

**Fail-closed cuts the OPPOSITE way here, and this is the trap worth naming.** On entry, a
missing stage read blocks the entry — safe. On exit, treating a missing read as a failed
condition would liquidate the entire book the first time systrader's API is down: an
infrastructure outage turned into a market order. So every exit condition requires
POSITIVE evidence that it tripped; absent data is never an exit.

**Shadow positions are managed identically to real ones, adjudicated exits included.** The
paired comparison below only isolates the ENTRY veto if everything downstream of entry is
the same for both arms. Exiting shadows mechanically while exiting real positions with an
adjudicator would confound the two vetoes and answer neither question.

## What the review found (2026-09-04)

The build was reviewed against live data before any forecast had resolved. Six real bugs,
none of which a reading of the code would have shown:

1. **Forecasts would have resolved against the data that produced them.** Resolution reads
   the latest L2 row, and L2 refreshes on the screener's cadence — every L2 row on the
   first book predated its forecast by ~15 days. Worse than circular: these forecasts
   predict a *change*, so the pre-forecast snapshot marks them wrong before the company has
   reported anything. BALPHARMA returned `False` on a +9 reading the adjudicator had
   already seen and was predicting would reverse. The mechanical hit rate — trusted
   precisely because no model can influence it — would have converged on zero and looked
   like evidence. Resolution now requires the L2 row to strictly postdate the forecast, and
   skips the judged call too when it does not.
2. **Metric validation failed open.** `if allowed and name not in allowed` meant an *empty*
   allow-list — what a failed `information_schema` read returns — disabled validation
   entirely. Now fails closed.
3. **A veto was a permanent ban.** The re-entry guard read *all* open positions, and a
   vetoed shadow never closes, so one day's veto silently excluded a name forever.
4. **…but fixing that created veto-shopping.** Re-asking a sampled model the same question
   eventually yields an accept: RANEHOLDIN flipped veto→accept within an hour on an
   identical scorecard. A veto now stands until the *evidence* moves (the confluence score's
   `run_date`), which also saves the call. **These two are a matched pair — a change to
   either must preserve both.**
5. **Turned-away candidates lost their reasoning.** The adjudication happened and cost a
   call; the verdict was discarded when the book was full.
6. **Unbounded hypertable scan.** "Latest close per symbol" over
   `advisory_adjusted_ohlcv_daily` visited 689 chunks (2.4s for 100 symbols vs 0.2s
   bounded), nightly, growing with history. Now bounded to 90 days — and a position with no
   recent trade is *reported* as unpriced, because its stop cannot be checked and a stop
   nobody is checking must never be silently assumed safe.

7. **A stale stage read was trusted as current.** Entry was fail-closed on a *missing*
   stage read but had no protection against a stale one — and stale is more dangerous
   because it looks healthy. systrader's `cmd/api` is kept alive by its own cron; if that
   dies, `/api/stage` serves last week's stages forever without an error, turning a daily
   price read into a constant. This repo has been bitten by that exact shape twice
   (go-crond dead 5 days; Dhan collection dead 5 days with every freshness check green).
   Each row carries its own `as_of`, so staleness is now judged per row — 14 real reads
   were being trusted despite being stale.

Data integrity was separately verified clean: no duplicate open positions, no null
`entry_decision`, no vetoed row carrying a thesis, no `target_date` outside the clamp, no
`stop_pct` outside the band, no `metric_name` naming a non-existent column. The three
`UPDATE` paths that had never run (`_close`, `_defer`, `_write_resolution`) were exercised
against the real schema and `_close` confirmed idempotent. A follow-up pass exercised the
**exit adjudicator's first ever live call** (it had zero recorded exit decisions): a
synthetic position on a Stage-4 name triggered `thesis_invalidation`, the model was
genuinely consulted, chose to exit with a specific reason, and the position closed
correctly. The calibration UI — which had never rendered a single row — was verified
against synthetic resolutions covering both resolution methods. Both fixtures were removed
afterwards.

## Measuring the adjudicator, not just the ruleset

The adjudicator is itself a hypothesis and must be scored, or it becomes an unfalsifiable
layer that absorbs every result. **Rejected candidates are recorded and shadow-tracked**
as if they had been taken.

That gives a paired comparison — taken-vs-rejected under the same ruleset, same period —
which is far more informative on a small sample than any absolute hit rate, and it
directly attacks the operator's measurement doubt. After enough resolutions it answers a
sharp question: *is the veto adding value or destroying it?* If rejected names outperform
taken ones, the adjudicator is hurting and should be reduced to advisory.

Shadow positions are marked and never counted as real P&L.

## Rollout

1. **Record-only.** Rules + adjudicator run nightly, write positions to a shadow table,
   no money. Purely a shakedown: does the evaluator produce a portfolio a human
   recognises as sensible? Not a backtest and does not pretend to be.
2. **Sizing is decided and recorded, but not yet committed** (operator, 2026-09-04):
   a flat **₹1,00,000 per position, up to 100 names** — so the book can deploy at most
   ₹1 crore. The operator's instruction was to *"watch the portfolio for some time before
   we start doing --live"*, so this size is recorded on every position and no money moves.

   **Flat, not an equal-weight split of a sleeve.** Under a split, every new name shrinks
   every existing one, so a position's size depends on how many *other* companies happened
   to qualify that day. A name's allocation moving because an unrelated company passed the
   screen is not a decision anyone made. Flat allocation makes each position independent.

   **`MAX_POSITIONS` is a capital constraint, not a diversification heuristic**, and that
   distinction decides how it is enforced: a full book stops entering and *names* the
   candidates it turned away. It never silently truncates the candidate list, which would
   make entry depend on iteration order. "The book was full" and "the rule found nothing"
   look identical in a position count and mean opposite things.

   The source spec's 12–20 figure survives only as a floor-side warning: below ~10 names a
   20% hit rate has a material chance of holding zero winners over a cycle
   (`fundamental_basic_goal.md` §L5).

   The ADV liquidity cap (10% of average daily traded value) still applies and can only
   pull a size DOWN, never raise it — a very liquid name does not earn a bigger position,
   which would turn a risk control into a conviction signal. Measured on the 2026-09-04
   book, all 8 accepted names clear ₹1L comfortably (BALPHARMA is closest, at ~62% of its
   ADV cap), so liquidity is not currently binding.

   Sizing is **frozen at entry**. Recomputing it on read would restate history every time
   ADV moved, and the question a review asks is "what did we commit", not "what would we
   commit today".

3. **Review by ruleset version** once ~20 positions resolve.

## Open questions

- **Does stockey inherit systrader's LEDGER M-accounting and holdout discipline?** If
  rulesets are iterated against the same data repeatedly, multiple testing applies here
  as much as there. Either adopt it or consciously accept that stockey's rules are not
  held to that bar. Drifting into the second by not deciding is the bad outcome.
- **Does the stop belong on systrader's side of the boundary?** It is sized from a price
  series (below), which is systrader's domain by method. It stays here for now because
  the volatility is used only to scale a risk parameter for a name already chosen — it
  never selects or rejects a candidate, so it is not a signal. If stops later become
  entry- or exit-*selective*, that argument stops working and this moves.
- **Should `evaluable_count`'s floor be higher than 1?** Left at the minimum that makes
  the rule meaningful rather than at a number that feels prudent. A decision for the
  record to make, not this document.
- **Does stockey inherit systrader's LEDGER M-accounting?** (unchanged, see above)
- **Rebalancing when the book is full.** v1 stops entering and records the names it turned
  away. It does NOT evict a weaker existing position to make room, because that needs a
  comparable strength score across positions and the whole premise here is that we do not
  have one. The turned-away log is the evidence that will say whether this matters.


## Ruleset v2 (2026-09-29) -- stop the churn

From docs/FUNDAMENTAL_REEVALUATION_PRD.md section 5.1, after v1 averaged 10-day holds (9 of 11
closes "thesis invalidation") against 60-365 day forecasts. New entries are v2; positions
opened under v1 keep v1's exit rules until they close, so v1's record completes as recorded.

- **Entry:** confluence_count >= 2 (was: evaluable >= 1), no contradicting axis, stage 2.
- **Stop:** 2.5x the 10-day one-sigma move, clamped to 10-25% (was 1.5x, 5-15%).
- **Exit on thesis only for:** a HARD contradiction since entry (results_decline,
  rating_downgrade, pledge_increase, auditor_change, insider_sell_surprise) -- immediately;
  a SOFT contradiction (any axis except valuation) on each of the last 3 scoring runs, after
  20 sessions held; or Weinstein stage 4 (declining), after 20 sessions. Valuation is never
  an exit reason. Stop-loss and target date unchanged; the adjudicator may still defer once.
- **Candidates (2026-09-29, before v2's first live run):** the active watchlist, which is now
  chosen on the story score (docs/FUNDAMENTAL_REEVALUATION_PRD.md step 6) instead of "any
  positive alert ever". v2's entry filter above is unchanged and still reads confluence; the
  confluence count is kept for exactly that and retires with v2 when ruleset v3 (on the story
  score) replaces it. v2's record therefore starts at go-live on the new candidate list.

## Ruleset v3 (2026-09-30) -- the portfolio on the story score

Replaces v2 for new entries from go-live (operator decision 2026-09-30: v3 replaces v2 rather
than running beside it; v2 never opened a position). v1 positions keep v1's exits until they
close. Code: `fundamentals/screens/portfolio_v3.py`, dispatched from portfolio_ruleset /
portfolio_runner / portfolio_exit by each candidate's or position's `ruleset_version`.
Pre-registered as systrader LEDGER row 52. Full rules: docs/FUNDAMENTAL_REEVALUATION_PRD.md
5.2-5.3; as built:

- **Entry:** active watchlist name, live story score in the band (enter 80, stay to 65), no
  flaw, Weinstein stage 2. Not at or above 2x its own valuation history (amendment 2026-10-02: v3 bought
  IDEAFORGE at 8.6x and would have trimmed it the same night). The adjudicator may veto (prompt v4 shows it the story score,
  primary story and the latest story read instead of confluence axes). Candidates outside the
  book's best 25 by score are not adjudicated (`outside_top_by_score`).
- **Weight:** story score / daily volatility, normalised so an average name is 1/25 of the
  bucket, clipped to 2-8%, sector <= 25%, then the 10%-of-ADV cap. Recorded as
  `target_weight` on the position; weights never sum past 100%, the rest is cash.
- **Replacement when full:** the weakest holding, only if the candidate scores 15+ points
  higher, the holding is 20+ sessions old and not waiting on the tax guard; closed as
  `replaced` with a counterfactual row.
- **Exits:** `stop_loss` / `trailing_stop` (trail 3x the 20-session move, 15-35%, ratchets via
  `trail_high`, never below entry after a 2x-stop gain) and `thesis_broken` (flaw, or hard
  negative alert since entry) are unconditional; `story_fading` (daily score below 65 on 3 runs)
  and `target_date` may be deferred once; a valuation trim (>= 2x own history, story not
  strengthening) cuts a third once, after at least 20 sessions held (amendment 2026-10-02). Non-urgent exits and trims in profit wait inside the 30 days
  before the first anniversary (tax guard).
- **Records:** `fundamentals_portfolio_counterfactual` -- 20/40/60-session returns after every
  exit and trim, as if held; `fundamentals_portfolio_adjustment` -- trims and rebalance signals
  (a position 50% away from its target value, at most one per 20 days). The book is
  record-only, so trims and rebalances are records; `position_size_rs` stays the entry size.
- **Confluence** is no longer read by the active ruleset. It is still computed nightly (the
  frontend shows it and v1/v2 code paths read it); removing the step is a separate cleanup.
