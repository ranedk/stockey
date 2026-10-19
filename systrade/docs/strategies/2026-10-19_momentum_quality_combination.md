# momentum-quality-combination (frozen 2026-10-19)

**What:** half the book in the momentum lookback blend (its four variants — 6/9/12-month and
12-1 momentum, top fifth each — at that track's frozen 33/33/17/17 weights within the half),
half in stockey's frozen **quality** track (LEDGER row 46: operating companies, profitable both
of the last 2 years, positive 3-year free cash flow, ranked on 5-year ROCE, 5-year ROE and low
debt/equity; top 30, monthly, kept until outside the top 60), equal weight within the half.

**Why:** quality is the classic partner for momentum — momentum crashes tend to come while
quality holds up. TODO D1; the operator approved systrader reading stockey's forward-track
holdings on 2026-10-19 (DATA_CONTRACT updated, both copies).

**Mechanics:** `paper.MomentumQualityCombinationSpec`. The quality half holds the names the
stockey track held after its last rebalance ON OR BEFORE the decision day
(`fundamentals_forward_track_holding`, synced nightly), so no decision sees a later rebalance.
A quality name below this book's liquidity floor (the frozen spec's 60-bar median traded
value) is not held and its share goes to the quality names that are — on 2026-10-19 that is
17 of 30. Rebalanced every 21 sessions, 50 bps round trip, long-only, starts 2026-10-20.

**Judged:** from 2027-10-20 (12 months), paired monthly against the momentum lookback blend
alone: the combination is worth keeping if its max drawdown is clearly shallower at a return
not clearly worse. Forward only — the quality track has no history before 2026-10-02.
LEDGER row 60, trials = 1.
