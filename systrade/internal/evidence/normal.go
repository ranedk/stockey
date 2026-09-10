package evidence

import "math"

// NormCDF is the standard normal distribution function.
func NormCDF(x float64) float64 { return 0.5 * math.Erfc(-x/math.Sqrt2) }

// NormInv is the inverse standard normal CDF (Acklam's approximation,
// |ε| < 1.15e-9 — far more precision than any trading statistic deserves).
func NormInv(p float64) float64 {
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

func mean(x []float64) float64 {
	var s float64
	for _, v := range x {
		s += v
	}
	return s / float64(len(x))
}

// stddev is the sample standard deviation (n-1).
func stddev(x []float64) float64 {
	if len(x) < 2 {
		return 0
	}
	m := mean(x)
	var ss float64
	for _, v := range x {
		ss += (v - m) * (v - m)
	}
	return math.Sqrt(ss / float64(len(x)-1))
}

// quantile reads the q-th quantile of an already SORTED slice, interpolating
// linearly between order statistics.
func quantile(sorted []float64, q float64) float64 {
	if len(sorted) == 0 {
		return math.NaN()
	}
	pos := q * float64(len(sorted)-1)
	lo := int(math.Floor(pos))
	if lo >= len(sorted)-1 {
		return sorted[len(sorted)-1]
	}
	f := pos - float64(lo)
	return sorted[lo]*(1-f) + sorted[lo+1]*f
}
