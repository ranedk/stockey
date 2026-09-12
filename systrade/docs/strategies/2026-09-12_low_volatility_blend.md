# Strategy spec — low-volatility blend (NSE cash equity)

**Status: FROZEN 2026-09-12.** The forward record starts with the first
trading day on or after 2026-09-12, on one condition: its kill screen,
pre-registered in `research/preregistrations/2026-09-12_lowvol_and_combination_screens.md`,
must not kill it. A fourth parallel record; it replaces nothing.

## 1. What is traded

- **Universe:** NSE cash equities with a 60-bar median traded value of at
  least Rs 10 crore, as every other track.
- **Signals** (`traits.LowRiskScore`, the one definition research and paper
  share): standard deviation of daily returns over 3, 6 and 12 months; 1-year
  beta to the equal-weight eligible universe; 1-year residual volatility.
- **Book:** a blend of BOOKS — each measure's LOWEST-risk quintile, equal
  weight within it — held **11 / 11 / 11 / 33 / 34** (3m, 6m, 12m volatility,
  beta, residual volatility). A stock in several is held once at the summed
  weight.
- **Rebalance:** every 21 trading days; decide at the close, fill at the next
  open; 50 bps round trip; fully invested, no leverage, no stop.
- **Controls:** equal-weight universe and a random ranking, same seed as every
  track.

## 2. Why these five, at these weights — no performance input

LEDGER row 32 found low volatility a PLATEAU: 17 of 18 cells survived at
matched risk. The members are the measures whose ONE-MONTH-hold cells
survived — 1-month volatility held one month did not. Weights from the
handcrafting tree on how the books co-move and what they cost
(`research/reports/2026-09-12_lowvol_blend_construction.txt`): the three
volatility windows correlate 0.94-0.97 and form one group; that group, beta
and residual volatility all round to Table 8's 0.9 — thirds; thirds again
inside the volatility group; Table 12 column A on costs (3-month volatility
trades most) moves it to 11/11/11/33/34.

## 3. What to expect of it

It is a defensive book. Research found every low-risk book ran about
two-thirds of the universe's volatility and roughly half its drawdown while
keeping up on raw return. Its edge was measured at matched risk — a reading
device, since a cash book cannot lever — so the forward record is judged the
same way: on return per unit of risk against both controls, and on raw return
reported beside it. No in-sample reference curve: 2022+ stays unread for this
family.

## 4. Pre-registered evaluation (binding)

As trend-quintile's §5, with the comparison made at matched risk: no judgement
before 12 months and 12 rebalances; success is a positive mean monthly
difference against BOTH controls at matched risk; kill is a negative one
against equal-weight over 18 months, or realised round-trip costs above 100
bps; frozen throughout.
