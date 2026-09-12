# Pre-registration — the low-volatility anomaly across time windows

Written 2026-09-12, before any return of any low-volatility or low-beta book
has been computed in this workspace. What has been looked at: the traits'
coverage, persistence and overlap WITHOUT returns (LEDGER rows 27-28), and
momentum's edge inside own-volatility buckets (row 29) — not a low-risk book.

## 1. The claim, and the mechanism

The published anomaly: low-risk stocks earn about as much as high-risk ones,
so their risk-adjusted returns are higher (Ang, Hodrick, Xing and Zhang 2006;
Baker, Bradley and Wurgler 2011; Frazzini and Pedersen 2014; NSE runs Nifty
Low Volatility indices on it). The story: investors who cannot or will not use
leverage, and managers benchmarked to an index, get market-beating risk by
buying high-beta stocks instead; lottery-seekers do the same. That bids up
high-risk stocks and leaves low-risk ones cheap. A long-only cash book cannot
lever the low-risk side, so what it can harvest is a DEFENSIVE book: similar
return, less risk, smaller drawdowns.

## 2. The statistic that decides — risk-adjusted, not raw

The claim is about return per unit of risk, so the primary statistic is the
**edge at matched risk** (LEDGER row 24's standard, `evidence.ScaleToRisk`):
each low-risk book's daily returns are scaled to the realised volatility of
the control it is compared with, then compared month by month. The scaling is
a reading device, fixed with hindsight over the sample; a cash book cannot
actually lever. The raw edge, volatility and maximum drawdown are reported
beside it.

## 3. The grid (fixed)

The book holds the LOWEST fifth of the eligible universe by:

| name | measure |
|---|---|
| vol1 | standard deviation of daily returns, last 21 trading days |
| vol3 | the same, last 63 |
| vol6 | the same, last 126 |
| vol12 | the same, last 252 |
| beta12 | 252-day beta to the equal-weight eligible universe (point in time) |
| idio12 | 252-day residual volatility after that beta |

Each is held **1, 3 and 6 months**, as staggered books a month apart
(Jegadeesh-Titman), averaged. **Family: 6 × 3 = 18 cells.** A measure needs
at least 80% of its window's daily returns (at least 200 of 252 for beta and
residual volatility, as in `internal/traits`); daily moves beyond ±50% are
data errors, as there.

## 4. Setup (fixed)

- NSE adjusted EQ, Rs 10 crore 60-bar median traded value; decisions
  2013-07-01 → 2021-12-31, the exploration years (2022+ unread).
- Equal weight within the book, decide at the close, fill at the next open;
  50 bps round trip, 100 bps as sensitivity.
- `internal/sleeve` equal-weight and stable-shuffle controls (seeds 1-5, now
  reproducible — LEDGER row 31).
- Monthly paired differences; one-sided stationary block bootstrap (mean block
  5 months, 10,000 resamples, seed 1); FDR by Benjamini-Hochberg at q = 0.10
  within the 18; deflated Sharpe on the matched-risk excess over the tougher
  control, trials = 18.

## 5. Verdict rule (stated before any result)

- **Plateau:** at least 12 of 18 cells beat BOTH controls at matched risk and
  survive FDR, not confined to one row or one column.
- **Spike:** fewer than 6, or all in one row or column.
- In between otherwise.
- Reported, deciding nothing: whether the raw return keeps up with the
  equal-weight book (the "similar return" half of the claim), and the
  drawdown each book saves or costs.

## 6. What it can and cannot decide

Exploration years only: it can kill the anomaly here, never confirm it. A
plateau would make low volatility a candidate second strategy — and, because
row 29 found momentum weakest among the lowest-volatility stocks, a candidate
partner for momentum in the queue's combination question — to be confirmed
forward, never on these years. A spike or a kill ends it.

## 7. Ledger

One row, trials = 18.
