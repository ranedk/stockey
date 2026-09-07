package rules

import "fmt"

// Forecast scalars for the rules this project implements itself.
//
// Law 8's convention is that a rule's forecast averages 10 in absolute value.
// Carver fits that constant on the FORECAST DISTRIBUTION — the average |raw|
// over a long pooled sample — never on returns, which is what keeps it out of
// the M-accounting: no performance number is consulted, so no hypothesis is
// tested and no sample is consumed. EWMAC and carry use his published table
// (rules.go); the rules below are ours, so their constants are measured the
// same way on our own universe by:
//
//	rulelab scalars -cache data/cache/bars_daily.bin
//
// Measured 2026-09-07 on the NSE adjusted-EQ cache (3,089 symbols,
// 2013-07-01..2026-06-30, Rs 1cr/day median-turnover floor at the observation,
// close prices, PriceUnitVol span 36). Frozen here on purpose: a scalar that
// silently re-fits per run makes two backtests incomparable.
// The EWMAC rows of that same run are the procedure's control: measured on
// NSE equities they come out 4.43 / 2.97 / 2.01 for fast 16 / 32 / 64, against
// Carver's published 3.75 / 2.65 / 1.87 — 7-18% higher, i.e. our single stocks
// produce a slightly SMALLER trend signal per unit of vol than his futures
// portfolio does. Two consequences, both deliberate:
//
//   - EWMAC and carry keep the book's constants (rules.go). They are inherited
//     rules admitted on the book's out-of-sample evidence, and that evidence is
//     of the whole rule including its scaling; re-fitting the constant on our
//     data would make them ours, not his.
//   - Which means EWMAC's forecast averages ~8.5 on this universe while the
//     rules below average 10, so they run ~12% hotter for the same handcrafted
//     weight. Visible in `rulelab corr`'s mean|fcst| column, not hidden. If a
//     combination ever proves sensitive to it, the fix is to move EWMAC onto
//     measured scalars too (one line here) — never to fudge these to match.
//
// Independent corroboration that the procedure is sound: breakout measures
// 37.5-39.6 across lookbacks, against the 40 Carver builds into the rule's
// definition on entirely different instruments.
var (
	breakoutScalars = map[int]float64{40: 37.5, 80: 38.6, 160: 39.2, 320: 39.6}
	accelScalars    = map[int]float64{16: 6.89, 32: 4.68, 64: 3.05}
	meanrevScalars  = map[int]float64{512: 19.1, 1280: 19.4}
)

func lookupScalar(name string, table map[int]float64, key int) float64 {
	if s, ok := table[key]; ok {
		return s
	}
	panic(fmt.Sprintf("rules: no frozen forecast scalar for %q — measure it with `rulelab scalars` and add it to scalars.go rather than guessing", name))
}

// Library is the standard variation set: every rule this project runs, in the
// order reports print them. Variations are chosen for spacing (each roughly
// doubles the horizon of the one before), not swept — a sweep would be a
// selection trial and would inflate M, which is exactly what the ledger
// exists to prevent.
func Library() []Rule {
	return []Rule{
		EWMAC{Fast: 16}, EWMAC{Fast: 32}, EWMAC{Fast: 64},
		Breakout{N: 40}, Breakout{N: 80}, Breakout{N: 160}, Breakout{N: 320},
		Acceleration{Fast: 16}, Acceleration{Fast: 32}, Acceleration{Fast: 64},
		MeanReversion{Window: 512}, MeanReversion{Window: 1280},
	}
}
