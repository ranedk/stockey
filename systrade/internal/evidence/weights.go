package evidence

import (
	"fmt"
	"math"
	"sort"
)

// Law 6 says forecast and instrument weights are HANDCRAFTED: group by
// correlation, equal weight within groups, multiply down the tree. Carver's
// alternative (Systematic Trading ch. 4) is bootstrapping: optimise on many
// resamples and average the answers, which smooths away the extreme weights a
// single optimisation produces. It lives here as the CROSS-CHECK on
// handcrafted weights, never as their replacement.
//
// Means are held equal on purpose. The optimiser sees only the resampled
// correlation matrix and treats every subsystem as having the same Sharpe
// ratio and the same volatility — they are all vol-normalised to one target.
// That is Law 6's own position (Sharpe adjustments are zero with under ten
// years of evidence), and it removes the one input an optimiser abuses most.
// With equal means and vols, the maximum-Sharpe portfolio is the long-only
// minimum-variance portfolio on the correlation matrix; the bootstrap then
// shows how much of that answer is the correlations and how much the sample.
//
// Read it this way: where bootstrapped and handcrafted weights agree, the
// handcrafted tree is right. Where they differ by more than the bootstrap's
// own interval, a correlation group was drawn wrong.

// WeightEstimate holds one weight per column.
type WeightEstimate struct {
	Full []float64 // one optimisation on the whole sample — the answer not to trust
	Mean []float64 // averaged over resamples — the bootstrapped weights
	Low  []float64 // 10th percentile across resamples
	High []float64 // 90th percentile across resamples
}

// BootstrapWeights estimates weights for aligned subsystem return columns
// (one slice per subsystem, all the same length), resampling rows jointly.
func BootstrapWeights(columns [][]float64, bs Bootstrap) (WeightEstimate, error) {
	k := len(columns)
	if k == 0 {
		return WeightEstimate{}, fmt.Errorf("evidence: no columns to weight")
	}
	n := len(columns[0])
	for i, c := range columns {
		if len(c) != n {
			return WeightEstimate{}, fmt.Errorf("evidence: column %d has %d rows, column 0 has %d — align on dates first", i, len(c), n)
		}
	}
	if err := bs.check(n); err != nil {
		return WeightEstimate{}, err
	}
	est := WeightEstimate{Full: MinVarianceWeights(Correlation(columns, nil))}
	draws := make([][]float64, k)
	err := bs.Each(n, func(_ int, idx []int) {
		w := MinVarianceWeights(Correlation(columns, idx))
		for j := range w {
			draws[j] = append(draws[j], w[j])
		}
	})
	if err != nil {
		return WeightEstimate{}, err
	}
	est.Mean, est.Low, est.High = make([]float64, k), make([]float64, k), make([]float64, k)
	for j := range draws {
		est.Mean[j] = mean(draws[j])
		sort.Float64s(draws[j])
		est.Low[j] = quantile(draws[j], 0.10)
		est.High[j] = quantile(draws[j], 0.90)
	}
	return est, nil
}

// Correlation is the correlation matrix of the columns, read at rows idx
// (nil for every row). A column with no variance in the rows read — a rule
// that sat flat through a resample — is treated as uncorrelated with the
// rest rather than poisoning the matrix with NaN.
func Correlation(columns [][]float64, idx []int) [][]float64 {
	k := len(columns)
	if idx == nil {
		idx = make([]int, len(columns[0]))
		for i := range idx {
			idx[i] = i
		}
	}
	n := float64(len(idx))
	mu := make([]float64, k)
	for j, c := range columns {
		for _, r := range idx {
			mu[j] += c[r]
		}
		mu[j] /= n
	}
	cov := make([][]float64, k)
	for a := range cov {
		cov[a] = make([]float64, k)
	}
	for _, r := range idx {
		for a := 0; a < k; a++ {
			da := columns[a][r] - mu[a]
			for b := a; b < k; b++ {
				cov[a][b] += da * (columns[b][r] - mu[b])
			}
		}
	}
	out := make([][]float64, k)
	for a := range out {
		out[a] = make([]float64, k)
		out[a][a] = 1
	}
	for a := 0; a < k; a++ {
		for b := a + 1; b < k; b++ {
			if den := math.Sqrt(cov[a][a] * cov[b][b]); den > 0 {
				out[a][b] = cov[a][b] / den
				out[b][a] = out[a][b]
			}
		}
	}
	return out
}

// MinVarianceWeights solves min w'Cw subject to w >= 0, Σw = 1 by projected
// gradient descent onto the simplex. The subsystem counts here are a dozen at
// most, so a method that is obviously correct beats a fast one.
func MinVarianceWeights(c [][]float64) []float64 {
	k := len(c)
	w := make([]float64, k)
	for i := range w {
		w[i] = 1 / float64(k)
	}
	// Gershgorin bounds the largest eigenvalue of C; 1/(2·bound) is then a
	// step the gradient of w'Cw can never overshoot with.
	var bound float64
	for i := range c {
		var s float64
		for j := range c[i] {
			s += math.Abs(c[i][j])
		}
		bound = math.Max(bound, s)
	}
	step := 1 / (2 * bound)
	next := make([]float64, k)
	for it := 0; it < 200000; it++ {
		for i := range c {
			var g float64
			for j := range c[i] {
				g += c[i][j] * w[j]
			}
			next[i] = w[i] - step*2*g
		}
		projectSimplex(next)
		var moved float64
		for i := range w {
			moved = math.Max(moved, math.Abs(next[i]-w[i]))
			w[i] = next[i]
		}
		if moved < 1e-13 {
			break
		}
	}
	return w
}

// projectSimplex replaces v with its Euclidean projection onto
// {w : w >= 0, Σw = 1} (Duchi et al. 2008).
func projectSimplex(v []float64) {
	u := append([]float64(nil), v...)
	sort.Sort(sort.Reverse(sort.Float64Slice(u)))
	var css, theta float64
	for i, x := range u {
		css += x
		if t := (css - 1) / float64(i+1); x-t > 0 {
			theta = t
		}
	}
	for i := range v {
		v[i] = math.Max(0, v[i]-theta)
	}
}
