# Strategy spec — investor-paradigm replicas (momentum side)

**Status: FROZEN 2026-10-23.** The forward records start 2026-10-26. LEDGER row 83. The
operator asked for portfolios built from the stock-selection methods that Indian managers
publish (`../twitter_handles.md`), shown on the screener's /paradigms page with the paradigm,
the holdings and the returns. The five fundamental methods are stockey forward tracks
(`stockey/fundamentals/screens/forward_tracks.py`); the two price-only methods are here.

**These are OUR rules built from what each manager has published — not their portfolios,
not their returns.** Wherever a step is private, the value used is ours and is named below.

## 1. Common to all five

Universe: NSE cash equity with 60-day median traded value of Rs 1 crore or more, ETFs,
fund units, REITs and InvITs left out (`ExcludeFunds`), then the traded-value RANK band named
per track (`BandLo..BandHi`) — the stand-in for the index each manager names, because index
membership has no history here and market caps only from 2024. Decide at the close, fill at
the next open, 50 bps round trip. Controls: equal weight of the same band, and a random book
of the same size. Each track also runs a reference replay from 2016-01-04 (`-reference`),
recomputed nightly, which the page shows as the backtest.

## 2. The tracks

| Track | Method | Universe band | Score | Holding rule | Clock |
|---|---|---|---|---|---|
| `paradigm-capitalmind-momentum` | Capitalmind Adaptive Momentum | 1-500 | 3/6/12-month return each over its own volatility, averaged (`cm-composite`) | only positive score, above 200-day average, not parabolic; top 30 at 1/30, empty slots cash; max 5 per industry; book to cash while the universe's equal-weight index is below its 200-day average | 21 days |
| `paradigm-wi-mi20` | WeekendInvesting Mi 20 | 101-500 | mean of 6- and 12-month return (`wi-rs`) | top 20, kept until outside the top 40, weights drift | 5 days |
| `paradigm-wi-top10` | Mi India Top 10 | 1-50 | `wi-rs` | top 10, kept until outside the top 20, weights drift | 5 days |
| `paradigm-wi-nnf10` | Mi NNF 10 | 51-100 | `wi-rs` | top 10, kept until outside the top 20, weights drift | 5 days |
| `paradigm-wi-mi30` | Mi 30 (absolute) | 1-500 | `wi-rs` | only positive score and above 200-day average; up to 33 at 1/33, empty slots cash; kept until outside the top 99; weights drift | 5 days |

Ours, not theirs: the equal 3/6/12 lookback weights; the parabolic thresholds (+50% in 20
sessions, or two moves of 9.5%+ in 10); the market-trend rule standing in for Capitalmind's
breadth rule; the 5-per-industry cap; WeekendInvesting's ranking (the 6/12-month average is
the approximation his published notes suggest), exit band (2N) and absolute threshold (> 0).

## 3. The replays (2016-01-04 .. 2026-10-23, in-sample, seen before freezing)

| Track | %/yr | worst drop | costs/yr | same-band equal weight | random book |
|---|---|---|---|---|---|
| Capitalmind replica | 19.7 | −30.7% | 2.2% | 11.7 (−61%) | 11.1 |
| Mi 20 replica | 25.7 | −60.9% | 1.8% | 12.4 (−63%) | 16.4 |
| Mi 30 replica | 23.1 | −53.3% | 1.3% | 11.9 (−61%) | 15.0 |
| NNF 10 replica | 18.0 | −53.4% | 2.6% | 8.7 (−57%) | 6.8 |
| India Top 10 replica | 5.9 | −45.8% | 1.2% | 6.9 (−58%) | 7.1 |

With our thresholds the absolute and cash rules rarely bite: even at the March 2020 low, 33 of
the top 500 still had positive momentum above their 200-day average. Capitalmind's replica
gets its small drawdown from the market-trend rule. Not tuned after seeing this.

## 4. Evaluation

A record, not a test: no pass/kill rule. The page shows each replica's replay and forward
return against its own equal-weight band. If one is to be traded, it gets its own
pre-registration first.
