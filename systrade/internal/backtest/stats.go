package backtest

import (
	"math"

	"github.com/ranedk/systrader/internal/evidence"
)

// BonferroniBar returns the one-sided t-stat a result must clear to be
// significant at the 5%-family level after M variations have been tried on
// the same data (Bible Law 2): z such that P(Z > z) = 0.025/M.
// M=1→1.96, 10→2.81, 100→3.48, 1000→4.06.
//
// Since the 2026-09-08 amendment this is the workspace-wide bar, reported for
// context only; the decision is FDR within the family plus a deflated Sharpe
// (internal/evidence).
func BonferroniBar(m int) float64 {
	if m < 1 {
		m = 1
	}
	p := 0.025 / float64(m)
	return evidence.NormInv(1 - p)
}

// RequiredN estimates how many observations (trades/days) are needed to
// detect an edge of size `edgePerObs` with noise `sdPerObs` at t-bar `bar`:
// N ≈ (bar × sd / edge)². Bible Law 4 companion: know this BEFORE testing.
func RequiredN(edgePerObs, sdPerObs, bar float64) float64 {
	if edgePerObs <= 0 {
		return math.Inf(1)
	}
	r := bar * sdPerObs / edgePerObs
	return r * r
}
