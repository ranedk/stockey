package evidence

import (
	"fmt"
	"math"
	"sort"
)

// Law 6 says forecast and instrument weights are HANDCRAFTED: group by
// correlation, equal weight within groups, multiply down the tree. Carver's
// alternative is bootstrapping, and this file implements it as the
// CROSS-CHECK on handcrafted weights, never as their replacement.
//
// It follows appendix C (p. 289): draw a sample of history, measure its
// correlations AND its members' mean returns, run the ordinary maximum-Sharpe
// optimisation on that sample alone, record the weights, repeat, average.
// Members are taken as volatility-normalised (his premise), so the inputs are
// the sample's correlation matrix and each member's Sharpe ratio on it.
// Samples are drawn as stationary blocks so runs survive; their length is the
// caller's, and appendix C's rule of thumb is 10% of the history.
//
// An earlier version held means equal and optimised correlations only, on the
// argument that Law 6 grants no Sharpe adjustment under ten years. That was
// wrong for this purpose. With equal means the optimum is minimum variance,
// which is a CORNER for any member highly correlated with two others: on the
// three trend speeds it gave the middle one 0% in every resample, an interval
// of zero width, and on Carver's own row-11 correlations it contradicts Table
// 8. The averaging only smooths anything because each sample's means differ —
// that noise is the mechanism, not a leak. What survives the averaging is a
// member that earned more across MANY samples, which is the one piece of
// performance information the method is built to let through.
//
// Read it this way: where bootstrapped and handcrafted weights agree, the
// tree is right. Where the handcrafted weight falls outside the bootstrap's
// 10-90% range, question the grouping first and the sample second.

// WeightEstimate holds one weight per column.
type WeightEstimate struct {
	Full []float64 // one optimisation on the whole sample — Carver's "single period", the answer not to trust
	Mean []float64 // averaged over samples — the bootstrapped weights
	Low  []float64 // 10th percentile across samples
	High []float64 // 90th percentile across samples
	// Uninformative counts samples in which no portfolio had a positive
	// return: they have no maximum-Sharpe answer and contribute
	// minimum-variance weights, the answer when means carry no information.
	Uninformative int
}

// BootstrapWeights runs the bootstrap over aligned return columns (one slice
// per member, all the same length), each sample sampleLen rows long (0 for
// the full length).
func BootstrapWeights(columns [][]float64, bs Bootstrap, sampleLen int) (WeightEstimate, error) {
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
	if sampleLen <= 0 || sampleLen > n {
		sampleLen = n
	}
	if sampleLen < 2 {
		return WeightEstimate{}, fmt.Errorf("evidence: a sample of %d rows has no correlation", sampleLen)
	}
	full, _ := optimise(columns, nil)
	est := WeightEstimate{Full: full}
	draws := make([][]float64, k)
	err := bs.Each(n, func(_ int, idx []int) {
		w, informative := optimise(columns, idx[:sampleLen])
		if !informative {
			est.Uninformative++
		}
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

// optimise is one sample's optimisation, on rows idx (nil for all).
func optimise(columns [][]float64, idx []int) ([]float64, bool) {
	c := Correlation(columns, idx)
	mu := make([]float64, len(columns))
	for j, col := range columns {
		mu[j] = sampleSharpe(col, idx)
	}
	if w, ok := MaxSharpeWeights(c, mu); ok {
		return w, true
	}
	return MinVarianceWeights(c), false
}

func sampleSharpe(col []float64, idx []int) float64 {
	var x []float64
	if idx == nil {
		x = col
	} else {
		x = make([]float64, len(idx))
		for i, r := range idx {
			x[i] = col[r]
		}
	}
	if sd := stddev(x); sd > 0 {
		return mean(x) / sd
	}
	return 0
}

// MaxSharpeWeights is the long-only portfolio with the highest Sharpe ratio
// for correlation matrix c and member Sharpe ratios mu. It is found exactly by
// trying every support: on the optimal support the weights are proportional
// to C_S⁻¹·μ_S with every weight positive, so the best such candidate is the
// optimum. ok is false when no portfolio has a positive expected return.
// Member counts here are a dozen at most, so 2^k supports is cheap.
func MaxSharpeWeights(c [][]float64, mu []float64) ([]float64, bool) {
	k := len(mu)
	if k > 16 {
		panic(fmt.Sprintf("evidence: %d members is too many to enumerate — group them first (Law 6)", k))
	}
	best := math.Inf(-1)
	var bestW []float64
	for mask := 1; mask < 1<<k; mask++ {
		var s []int
		for i := 0; i < k; i++ {
			if mask&(1<<i) != 0 {
				s = append(s, i)
			}
		}
		a := make([][]float64, len(s))
		b := make([]float64, len(s))
		for r, i := range s {
			a[r] = make([]float64, len(s))
			for q, j := range s {
				a[r][q] = c[i][j]
			}
			b[r] = mu[i]
		}
		y, ok := solve(a, b)
		if !ok {
			continue
		}
		var sum float64
		positive := true
		for _, v := range y {
			if v <= 0 {
				positive = false
				break
			}
			sum += v
		}
		if !positive {
			continue
		}
		w := make([]float64, k)
		for r, i := range s {
			w[i] = y[r] / sum
		}
		var ret, variance float64
		for i := range w {
			ret += w[i] * mu[i]
			for j := range w {
				variance += w[i] * w[j] * c[i][j]
			}
		}
		if ret <= 0 || variance <= 0 {
			continue
		}
		if sr := ret / math.Sqrt(variance); sr > best {
			best, bestW = sr, w
		}
	}
	return bestW, bestW != nil
}

// solve is Gaussian elimination with partial pivoting; ok is false when the
// system is singular.
func solve(a [][]float64, b []float64) ([]float64, bool) {
	n := len(b)
	m := make([][]float64, n)
	for i := range m {
		m[i] = append(append([]float64(nil), a[i]...), b[i])
	}
	for col := 0; col < n; col++ {
		piv := col
		for r := col + 1; r < n; r++ {
			if math.Abs(m[r][col]) > math.Abs(m[piv][col]) {
				piv = r
			}
		}
		if math.Abs(m[piv][col]) < 1e-12 {
			return nil, false
		}
		m[col], m[piv] = m[piv], m[col]
		for r := col + 1; r < n; r++ {
			f := m[r][col] / m[col][col]
			for q := col; q <= n; q++ {
				m[r][q] -= f * m[col][q]
			}
		}
	}
	x := make([]float64, n)
	for r := n - 1; r >= 0; r-- {
		s := m[r][n]
		for q := r + 1; q < n; q++ {
			s -= m[r][q] * x[q]
		}
		x[r] = s / m[r][r]
	}
	return x, true
}

// Correlation is the correlation matrix of the columns, read at rows idx
// (nil for every row). A column with no variance in the rows read — a rule
// that sat flat through a sample — is treated as uncorrelated with the rest
// rather than poisoning the matrix with NaN.
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
// gradient descent onto the simplex — the bootstrap's answer for a sample
// whose means say nothing. Member counts are a dozen at most, so a method
// that is obviously correct beats a fast one.
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
