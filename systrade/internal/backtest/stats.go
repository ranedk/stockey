package backtest

import "math"

// BonferroniBar returns the one-sided t-stat a result must clear to be
// significant at the 5%-family level after M variations have been tried on
// the same data (Bible Law 2): z such that P(Z > z) = 0.025/M.
// M=1→1.96, 10→2.81, 100→3.48, 1000→4.06.
func BonferroniBar(m int) float64 {
	if m < 1 {
		m = 1
	}
	p := 0.025 / float64(m)
	return normInv(1 - p)
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

// normInv is the inverse standard normal CDF (Acklam's approximation,
// |ε| < 1.15e-9 — far more precision than any trading statistic deserves).
func normInv(p float64) float64 {
	if p <= 0 || p >= 1 {
		return math.NaN()
	}
	a := [6]float64{-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
		1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00}
	b := [5]float64{-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
		6.680131188771972e+01, -1.328068155288572e+01}
	c := [6]float64{-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
		-2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00}
	d := [4]float64{7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
		3.754408661907416e+00}
	const pl = 0.02425
	switch {
	case p < pl:
		q := math.Sqrt(-2 * math.Log(p))
		return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q + c[5]) /
			((((d[0]*q+d[1])*q+d[2])*q+d[3])*q + 1)
	case p <= 1-pl:
		q := p - 0.5
		r := q * q
		return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r + a[5]) * q /
			(((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r + 1)
	default:
		q := math.Sqrt(-2 * math.Log(1-p))
		return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q + c[5]) /
			((((d[0]*q+d[1])*q+d[2])*q+d[3])*q + 1)
	}
}
