package handcraft

import "math"

// Certainty selects the Table 12 column: how well the Sharpe-ratio difference
// is known.
type Certainty int

const (
	// Certain is column A: the difference is known precisely, as costs are.
	Certain Certainty = iota
	// LongHistory is column B: estimated from more than ten years of data.
	LongHistory
	// ShortHistory is column C: under ten years — the factor is always 1,
	// because Sharpe differences that short are almost never significant.
	ShortHistory
)

// Table 12 (p. 86): the factor to multiply a handcrafted weight by, given an
// asset's Sharpe ratio minus its group's average.
var (
	table12Diff = []float64{-0.50, -0.40, -0.30, -0.25, -0.20, -0.15, -0.10, -0.05, 0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50}
	table12A    = []float64{0.32, 0.42, 0.55, 0.60, 0.66, 0.77, 0.85, 0.94, 1.00, 1.11, 1.19, 1.30, 1.37, 1.48, 1.56, 1.72, 1.83}
	table12B    = []float64{0.65, 0.75, 0.83, 0.85, 0.88, 0.92, 0.95, 0.98, 1.00, 1.03, 1.06, 1.09, 1.13, 1.15, 1.17, 1.25, 1.35}
)

// SharpeFactor reads Table 12. Between rows it interpolates linearly — the
// book does not say, and the alternative (snapping to the nearest row) would
// make a 0.074 and a 0.076 difference land a whole row apart. Outside ±0.5
// it holds the end value: the book gives nothing beyond, and a difference that
// large is more likely a bug than a fact.
func SharpeFactor(diff float64, c Certainty) float64 {
	var col []float64
	switch c {
	case Certain:
		col = table12A
	case LongHistory:
		col = table12B
	default:
		return 1
	}
	if diff <= table12Diff[0] {
		return col[0]
	}
	last := len(table12Diff) - 1
	if diff >= table12Diff[last] {
		return col[last]
	}
	for i := 1; i <= last; i++ {
		if diff <= table12Diff[i] {
			f := (diff - table12Diff[i-1]) / (table12Diff[i] - table12Diff[i-1])
			return col[i-1] + f*(col[i]-col[i-1])
		}
	}
	return 1
}

// AdjustForSharpe follows the book's steps (p. 87) for one group: each
// member's Sharpe ratio against the group's simple average, the Table 12
// factor for that difference, multiply, renormalise to the group's total.
func AdjustForSharpe(w, sr []float64, c Certainty) []float64 {
	var avg, total float64
	for i := range sr {
		avg += sr[i]
		total += w[i]
	}
	avg /= float64(len(sr))
	out := make([]float64, len(w))
	var sum float64
	for i := range w {
		out[i] = w[i] * SharpeFactor(sr[i]-avg, c)
		sum += out[i]
	}
	if sum <= 0 || math.IsNaN(sum) {
		return append([]float64(nil), w...)
	}
	for i := range out {
		out[i] *= total / sum
	}
	return out
}
