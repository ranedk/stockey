# Strategy spec — momentum + low-volatility combination (NSE cash equity)

**Status: FROZEN 2026-09-12.** The forward record starts with the first
trading day on or after 2026-09-12, conditional on its kill screen
(`research/preregistrations/2026-09-12_lowvol_and_combination_screens.md`).
A fifth parallel record; the two tracks it is built from keep running on their
own.

## 1. What is traded

One book of nine variants: the momentum lookback blend's four
(`docs/strategies/2026-09-12_momentum_lookback_blend.md`) and the
low-volatility blend's five (`docs/strategies/2026-09-12_low_volatility_blend.md`),
each variant's own top quintile, held **16 / 16 / 8 / 8** (6, 9, 12,
12-minus-1 month momentum) and **6 / 6 / 6 / 17 / 17** (3, 6, 12-month
volatility, beta, residual volatility) — 48% momentum, 52% low risk. Same
universe, clock, costs and controls as every track; monthly; no leverage.

## 2. Why — the first combination with two things worth combining

The queue's combination item waited because every rule tested had failed its
controls. Momentum (rows 18, 30) and low volatility (row 32) are the first two
that survived. They are also complementary by construction and by evidence:
row 29 found momentum weakest exactly among the lowest-volatility names, and
the two branches' excess returns correlate **−0.12** over the exploration years.

## 3. Weights — the handcrafted combination policy (Law 6)

A two-branch tree, each branch at its own track's frozen weights: Table 8 row
2 splits two branches in halves; Table 12 column A on their costs (momentum
0.088, low volatility 0.067 SR/yr) tilts that to 49/51; whole percent over the
nine members (`research/reports/2026-09-12_combination_construction.txt`).
Carver's bootstrap, read after, would lean 67/33 toward momentum — it is the
one input that sees returns, and it cannot move the weights.

This is the handcrafted policy of the combination item; Hedge and ML
combinations remain untested alternatives.

## 4. Evaluation (binding)

Judged at matched risk, as the low-volatility track: no judgement before 12
months and 12 rebalances; success is a positive mean monthly difference
against BOTH controls; kill is a negative one against equal-weight over 18
months or realised costs above 100 bps. The forward comparison that matters
most is against its own parents — does the combination beat each alone at
matched risk? — read once, at the joint 12-month date, deciding nothing on
its own.

In the screener's cross-strategy view it has no columns of its own
(`paper.Spec.Composite`): qualifying for it means qualifying for one of its
parents' variants.
