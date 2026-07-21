# systrader

A systematic trading framework for Indian markets in Go, built strictly on the
architecture of Robert Carver's *Systematic Trading*: continuous
volatility-standardized forecasts → handcrafted combination → volatility
targeting → position sizing → portfolio assembly → costs & speed limits.

**Read `TRADING_BIBLE.md` before touching anything.** It is law here; the
condensed version is installed as the `trading-bible` Claude skill and loads in
every session.

## Layout

```
TRADING_BIBLE.md          The laws (Carver distilled) — non-negotiable
docs/open_questions.md    Questions you must answer; edit inline
docs/instruments_india.md Indian instrument analysis & two-sleeve design
docs/rule_ideas.md        Phase-2 rules (breakout, TimesFM…) + innovations
research/LEDGER.md        Append-only record of every experiment (M-counter)

internal/core             Series math: EWMA, EWMA-std, price-unit volatility
internal/data             Instrument metadata, cost model, CSV loading
internal/rules            Rule interface + EWMAC + carry (scaled, capped ±20)
internal/combine          Forecast weights + FDM (≤2.5) → combined forecast
internal/sizing           Vol targeting: cash vol target, vol scalar, subsystem
internal/portfolio        Instrument weights, IDM (≤2.5), rounding, inertia,
                          four-block test
internal/backtest         Daily engine (compounding, costs), metrics
                          (SR/skew/DD/turnover/cost-drag), Bonferroni stats
internal/parallel         Generic worker-pool Map

cmd/backtest              Full pipeline on synthetic data (demo/harness check)
cmd/run                   Daily production runner → prints the order sheet
```

## Quickstart

```bash
go test ./...        # includes the no-look-ahead property test
go run ./cmd/backtest   # synthetic demo (numbers meaningless by design)
```

When real data arrives: drop CSVs (date,open,high,low,close,volume) in
`data/`, write `config.json` (schema at top of `cmd/run/main.go`), then:

```bash
go run ./cmd/run -config config.json -data ./data
```

## Design decisions already taken (challenge via open_questions.md)

- **Zero external dependencies in v1.** EWMA/vol/EWMAC/carry are ~150 lines we
  must control and test (look-ahead safety beats library convenience).
  `cinar/indicator/v2` joins when Phase-2 technicals do.
- Reference rule set: EWMAC 16/64 (21%), 32/128 (8%), 64/256 (21%), carry
  (50%); FDM 1.31 — Carver's Ch-15 configuration, inherited not fitted.
- Engine enforces the laws mechanically: vol target ≤ 50%, FDM/IDM ≤ 2.5,
  forecasts capped ±20, inertia 10%, cost drag printed against the 0.13 SR
  speed limit, t-stat judged against the ledger's Bonferroni bar.

## Status

- [x] Framework core + tests + synthetic demo
- [ ] Real OHLCV ingestion (blocked on open questions B5–B7)
- [ ] Futures stitching (Panama) + carry series construction
- [ ] Matched-control baseline harness; bootstrap weight estimation
- [ ] Handcrafting helper (correlation grouping → weights + FDM/IDM)
- [ ] Instrument universe finalization (blocked on capital, A1)
- [ ] Paper-trade mode; later: broker API execution
