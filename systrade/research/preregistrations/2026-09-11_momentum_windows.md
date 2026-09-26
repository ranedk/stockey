# Pre-registration — cross-sectional momentum across time windows

Written 2026-09-11, before any return of any window below has been computed.
The grid was taken from the published literature and approved by the operator
the same day; nothing in it was chosen by looking at our data.

## 1. The question

Every momentum test in this workspace so far used EWMAC speeds whose lookbacks
are roughly one to three months, held for 10-40 trading days. The published
momentum literature works at longer windows — lookbacks of 3 to 12 months,
holding periods of 1 to 12 months — and Indian studies find it strongest at
6-12 month lookbacks. The question is not "which window is best" (picking it
would be Law 5's pick-the-winner) but **"is the effect a plateau across
windows, or a spike at one?"** A plateau is evidence the premium is real and
robust to arbitrary choices; a spike is what luck looks like.

## 2. The grid (fixed)

**Signal** — trailing total return on adjusted closes, point in time,
"M" = 21 trading days:

| name | formation window |
|---|---|
| mom3 | t−3M → t |
| mom6 | t−6M → t |
| mom9 | t−9M → t |
| mom12 | t−12M → t |
| mom12_1 | t−12M → t−1M (skip the latest month, the modern standard) |
| mom12_7 | t−12M → t−7M (intermediate momentum, Novy-Marx) |

**Holding period** K = 1, 2, 3, 6, 12 months. A K-month hold is run as K
**staggered books** (Jegadeesh-Titman): K copies of the same book, each
rebalancing every K months, started one month apart, and averaged — so no
result depends on which day the rebalances happen to fall on.

**Momentum family: 6 signals × 5 holding periods = 30 cells.**

**Known-sign controls** (a separate family of 10, reported, not part of the
momentum verdict): short-term reversal `rev1` (the LOSERS of t−1M → t) and
long-term reversal `rev36` (the losers of t−36M → t−12M, De Bondt-Thaler),
each at the same five holding periods. They are there to check the machinery:
the literature says both should have the opposite sign to momentum's
recent-winner tilt.

## 3. Setup (fixed)

- NSE adjusted EQ from the survivorship-honest bar cache; eligible if the
  60-bar median traded value is at least **Rs 10 crore** (the tradable
  universe the paper tracks use).
- Each cell: long the top quintile of the eligible universe by its signal,
  equal weight, decide at the close, fill at the next open.
- `internal/sleeve`, with its equal-weight (beta) and stable-shuffle
  (turnover-matched selection) controls, shuffle seeds 1-5; 50 bps round trip,
  and 100 bps reported as sensitivity.
- **Exploration years only: decisions 2013-07-01 → 2021-12-31.** 2022-01 →
  2026-06 stays unread for confirmation. Windows needing history before 2013-07
  simply start later (mom12 in mid-2014, rev36 in mid-2016).
- Statistics per `internal/evidence`: monthly paired differences against each
  control, one-sided stationary block bootstrap (mean block 5 months, 10,000
  resamples, seed 1); FDR by Benjamini-Hochberg at q = 0.10 within the 30-cell
  family; deflated Sharpe on the excess over the tougher control, trials = 30.

## 4. What counts as a plateau (stated before any result)

- **Plateau:** at least 20 of the 30 cells beat BOTH controls at 50 bps and
  survive FDR, AND no single signal or holding period accounts for all of the
  survivors.
- **Spike:** fewer than 10 of 30 survive, or the survivors cluster in one row
  or one column of the grid.
- In between is reported as in between.

## 5. What this can and cannot decide

It runs on years that rows 17-18 and 29 have already explored for momentum, so
like row 18 **it can kill a window family but cannot confirm one**. If there is
a plateau, the consequence is a pre-registered confirmation on 2022-2026 of the
BLEND of surviving windows (Law 5), never of the single best cell. If there is
a spike, the spike is not pursued.

## 6. Ledger

One row, trials = 30 for the momentum family; the reversal controls declared
separately as trials = 10.
