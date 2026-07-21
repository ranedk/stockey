// Package portfolio assembles subsystem positions into final portfolio
// positions (Bible Law 12): × instrument weight × IDM (≤ 2.5), round to whole
// blocks LAST, then apply position inertia (no trade unless the target moved
// more than 10% away from the current position).
package portfolio

import "math"

const MaxIDM = 2.5

// Target scales a subsystem position to the portfolio level.
func Target(subsystem, instrumentWeight, idm float64) float64 {
	if idm > MaxIDM {
		idm = MaxIDM
	}
	if idm < 1 {
		idm = 1
	}
	return subsystem * instrumentWeight * idm
}

// ApplyInertia decides the new held position given the current one and the
// unrounded target. block is the minimum increment (normally 1).
func ApplyInertia(current, target, block float64) float64 {
	if math.IsNaN(target) {
		return current // no information → hold (never trade on NaN)
	}
	if block <= 0 {
		block = 1
	}
	rounded := math.Round(target/block) * block
	if target == 0 {
		return 0 // explicit flat signal always honored
	}
	// Trade only if current position drifted >10% of target away from it.
	if math.Abs(current-target) > 0.1*math.Abs(target) {
		return rounded
	}
	return current
}

// MaxPosition returns the largest position the system can ever request
// (forecast at the ±20 cap) — the input to the four-block test (Law 14).
func MaxPosition(volScalar, instrumentWeight, idm float64) float64 {
	return 2 * volScalar * instrumentWeight * math.Min(idm, MaxIDM)
}

// PassesFourBlockTest reports whether an instrument is viable at this capital
// and weight (max possible position ≥ 4 blocks).
func PassesFourBlockTest(volScalar, instrumentWeight, idm, block float64) bool {
	if block <= 0 {
		block = 1
	}
	return MaxPosition(volScalar, instrumentWeight, idm) >= 4*block
}
