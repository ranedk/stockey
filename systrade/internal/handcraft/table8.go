package handcraft

import (
	"fmt"
	"math"
)

// Table 8 (p. 79): weights for a group of three whose correlations are not
// all the same. Correlations are between A-B, A-C and B-C; weights are for A,
// B and C. The book numbers these rows 5-11; rows 1-4 are the rules for one
// asset, two assets, identical correlations, and "four or more: split
// further", which Weights applies directly.
var table8 = []struct {
	row        int
	ab, ac, bc float64
	w          [3]float64
}{
	{5, 0.0, 0.5, 0.0, [3]float64{0.30, 0.40, 0.30}},
	{6, 0.0, 0.9, 0.0, [3]float64{0.27, 0.46, 0.27}},
	{7, 0.5, 0.0, 0.5, [3]float64{0.37, 0.26, 0.37}},
	{8, 0.0, 0.5, 0.9, [3]float64{0.45, 0.45, 0.10}},
	{9, 0.9, 0.0, 0.9, [3]float64{0.39, 0.22, 0.39}},
	{10, 0.5, 0.9, 0.5, [3]float64{0.29, 0.42, 0.29}},
	{11, 0.9, 0.5, 0.9, [3]float64{0.42, 0.16, 0.42}},
}

// RoundCorrelation maps a correlation onto Table 8's grid of 0, 0.5 and 0.9.
// Negative values are floored at zero first (footnote 64: an asset with a
// negative correlation would get an unreasonably extreme allocation).
//
// The book says "round to the closest relevant number" and does not settle a
// tie. Its own practice does: chapter 8 takes EWMAC 16/64 at 0.70 — exactly
// between 0.5 and 0.9 — and uses row 11, i.e. 0.5. Ties therefore round DOWN,
// here and at 0.25.
func RoundCorrelation(c float64) (float64, error) {
	switch {
	case math.IsNaN(c):
		return 0, fmt.Errorf("handcraft: unknown correlation — estimate it before grouping, never guess")
	case c <= 0.25:
		return 0, nil
	case c <= 0.70:
		return 0.5, nil
	default:
		return 0.9, nil
	}
}

// TripletWeights returns the Table 8 weights for three members with the given
// pairwise correlations, and the book row they came from. Every pattern of
// the rounded grid is a row of the table or a relabelling of one, so this
// never fails on a valid correlation.
func TripletWeights(ab, ac, bc float64) (w [3]float64, row int, err error) {
	var r [3]float64
	for i, c := range []float64{ab, ac, bc} {
		if r[i], err = RoundCorrelation(c); err != nil {
			return w, 0, err
		}
	}
	if r[0] == r[1] && r[1] == r[2] {
		return [3]float64{1.0 / 3, 1.0 / 3, 1.0 / 3}, 3, nil
	}
	var m [3][3]float64
	m[0][1], m[0][2], m[1][2] = r[0], r[1], r[2]
	m[1][0], m[2][0], m[2][1] = r[0], r[1], r[2]
	perms := [6][3]int{{0, 1, 2}, {0, 2, 1}, {1, 0, 2}, {1, 2, 0}, {2, 0, 1}, {2, 1, 0}}
	for _, t := range table8 {
		for _, p := range perms {
			// p[k] is which of our members plays the table's A, B, C.
			if m[p[0]][p[1]] == t.ab && m[p[0]][p[2]] == t.ac && m[p[1]][p[2]] == t.bc {
				for k := 0; k < 3; k++ {
					w[p[k]] = t.w[k]
				}
				return w, t.row, nil
			}
		}
	}
	return w, 0, fmt.Errorf("handcraft: correlations %.2f/%.2f/%.2f match no Table 8 row — the table is incomplete or the rounding is wrong", ab, ac, bc)
}
