// Package traits computes the per-stock attributes the slicer cuts by and
// judges them WITHOUT looking at returns — the trait-library counterpart of
// what LEDGER row 14 did for rules.
//
// Three checks, each reported rather than used as a cutoff:
//
//   - Missing data: is it random? A trait that exists for most stocks can
//     still single out a small group, and that is fine. What is not fine is a
//     trait whose gaps line up with an outcome — minute data, for one, is
//     missing for every company that delisted, so a trait built on it quietly
//     drops the failures. Missing is compared between names that stop trading
//     within a year and names that do not.
//   - Persistence: does a stock's trait value survive the 20-day holding
//     period? Measured as the rank correlation between one sample and the
//     next. A volatile stock that is ALWAYS volatile has a perfectly
//     persistent volatility trait; what this catches is a trait whose value
//     is mostly measurement noise, so the stock is in another bucket by the
//     time the book has bought it.
//   - Overlap: the one hard rule. Two traits whose cross-sectional ranks
//     largely agree cut the universe the same way, and keeping both adds
//     buckets — and noise — without adding a view.
//
// Rank (Spearman) correlation throughout: the slicer buckets by rank within
// the day, so rank agreement is exactly what makes two traits redundant.
package traits

import (
	"math"
	"sort"
)

// BetaIdio regresses a stock's returns on the market's over the window
// ending at end and returns the slope and the daily standard deviation of
// what is left. At least minValid paired observations are required.
func BetaIdio(ret, mkt []float64, end, window, minValid int) (beta, idio float64, ok bool) {
	start := end - window + 1
	if start < 0 || end >= len(ret) || end >= len(mkt) {
		return math.NaN(), math.NaN(), false
	}
	var n, sx, sy, sxx, sxy float64
	for i := start; i <= end; i++ {
		x, y := mkt[i], ret[i]
		if math.IsNaN(x) || math.IsNaN(y) {
			continue
		}
		n++
		sx += x
		sy += y
		sxx += x * x
		sxy += x * y
	}
	if int(n) < minValid {
		return math.NaN(), math.NaN(), false
	}
	varX := sxx/n - (sx/n)*(sx/n)
	if varX <= 0 {
		return math.NaN(), math.NaN(), false
	}
	beta = (sxy/n - (sx/n)*(sy/n)) / varX
	alpha := sy/n - beta*sx/n
	var ss float64
	for i := start; i <= end; i++ {
		x, y := mkt[i], ret[i]
		if math.IsNaN(x) || math.IsNaN(y) {
			continue
		}
		r := y - alpha - beta*x
		ss += r * r
	}
	return beta, math.Sqrt(ss / (n - 2)), true
}

// DistFromHigh is the close's distance below the highest high of the window
// ending at end: 0 at a new high, -0.3 thirty percent below it.
func DistFromHigh(high, close []float64, end, window int) float64 {
	start := end - window + 1
	if start < 0 || end >= len(close) || close[end] <= 0 {
		return math.NaN()
	}
	hi := 0.0
	for i := start; i <= end; i++ {
		if high[i] > hi {
			hi = high[i]
		}
	}
	if hi <= 0 {
		return math.NaN()
	}
	return close[end]/hi - 1
}

// Amihud is the mean absolute daily return per rupee traded over the window
// (Amihud 2002): how far a rupee of trading moves the price, i.e. what our
// own orders would cost in impact. Days with no trading are skipped; at
// least minValid are required.
func Amihud(ret, value []float64, end, window, minValid int) float64 {
	start := end - window + 1
	if start < 0 || end >= len(ret) {
		return math.NaN()
	}
	var sum float64
	n := 0
	for i := start; i <= end; i++ {
		if math.IsNaN(ret[i]) || !(value[i] > 0) {
			continue
		}
		sum += math.Abs(ret[i]) / value[i]
		n++
	}
	if n < minValid {
		return math.NaN()
	}
	return sum / float64(n)
}

// Median of the non-NaN values in the window ending at end, if at least
// minValid exist.
func Median(x []float64, end, window, minValid int) float64 {
	start := end - window + 1
	if start < 0 || end >= len(x) {
		return math.NaN()
	}
	var v []float64
	for i := start; i <= end; i++ {
		if !math.IsNaN(x[i]) {
			v = append(v, x[i])
		}
	}
	if len(v) < minValid {
		return math.NaN()
	}
	sort.Float64s(v)
	m := len(v) / 2
	if len(v)%2 == 1 {
		return v[m]
	}
	return (v[m-1] + v[m]) / 2
}

// Spearman is the rank correlation of paired values, pairs with a NaN on
// either side dropped, ties given their average rank.
func Spearman(a, b []float64) float64 {
	var x, y []float64
	for i := range a {
		if math.IsNaN(a[i]) || math.IsNaN(b[i]) {
			continue
		}
		x = append(x, a[i])
		y = append(y, b[i])
	}
	if len(x) < 3 {
		return math.NaN()
	}
	rx, ry := ranks(x), ranks(y)
	var mx, my float64
	for i := range rx {
		mx += rx[i]
		my += ry[i]
	}
	mx /= float64(len(rx))
	my /= float64(len(ry))
	var sxy, sxx, syy float64
	for i := range rx {
		dx, dy := rx[i]-mx, ry[i]-my
		sxy += dx * dy
		sxx += dx * dx
		syy += dy * dy
	}
	if sxx == 0 || syy == 0 {
		return math.NaN()
	}
	return sxy / math.Sqrt(sxx*syy)
}

func ranks(x []float64) []float64 {
	idx := make([]int, len(x))
	for i := range idx {
		idx[i] = i
	}
	sort.Slice(idx, func(a, b int) bool { return x[idx[a]] < x[idx[b]] })
	r := make([]float64, len(x))
	for i := 0; i < len(idx); {
		j := i
		for j+1 < len(idx) && x[idx[j+1]] == x[idx[i]] {
			j++
		}
		avg := float64(i+j)/2 + 1
		for k := i; k <= j; k++ {
			r[idx[k]] = avg
		}
		i = j + 1
	}
	return r
}

// Row is one stock on one sample date: its trait values (NaN = missing) and
// whether it stops trading within the following year.
type Row struct {
	Sym     string
	Delists bool
	Values  []float64
}

// Sample is one date's cross-section.
type Sample []Row

// MissingShare reports, per trait, the share of rows with no value: overall,
// among names that stop trading within a year, and among names that do not.
// A gap between the last two is the signature of non-random missing data.
func MissingShare(samples []Sample, k int) (all, delisting, surviving []float64) {
	var n, nd, ns float64
	all, delisting, surviving = make([]float64, k), make([]float64, k), make([]float64, k)
	for _, s := range samples {
		for _, r := range s {
			n++
			if r.Delists {
				nd++
			} else {
				ns++
			}
			for j := 0; j < k; j++ {
				if math.IsNaN(r.Values[j]) {
					all[j]++
					if r.Delists {
						delisting[j]++
					} else {
						surviving[j]++
					}
				}
			}
		}
	}
	for j := 0; j < k; j++ {
		all[j] = safeDiv(all[j], n)
		delisting[j] = safeDiv(delisting[j], nd)
		surviving[j] = safeDiv(surviving[j], ns)
	}
	return all, delisting, surviving
}

// Persistence is, per trait, the mean rank correlation of each stock's value
// between one sample and the next (same stocks on both dates).
func Persistence(samples []Sample, k int) []float64 {
	out := make([]float64, k)
	for j := 0; j < k; j++ {
		var sum float64
		n := 0
		for t := 0; t+1 < len(samples); t++ {
			next := map[string]float64{}
			for _, r := range samples[t+1] {
				next[r.Sym] = r.Values[j]
			}
			var a, b []float64
			for _, r := range samples[t] {
				if v, ok := next[r.Sym]; ok {
					a = append(a, r.Values[j])
					b = append(b, v)
				}
			}
			if c := Spearman(a, b); !math.IsNaN(c) {
				sum += c
				n++
			}
		}
		out[j] = safeDiv(sum, float64(n))
		if n == 0 {
			out[j] = math.NaN()
		}
	}
	return out
}

// Overlap is the mean, over samples, of the cross-sectional rank correlation
// between every pair of traits.
func Overlap(samples []Sample, k int) [][]float64 {
	out := make([][]float64, k)
	for a := range out {
		out[a] = make([]float64, k)
		out[a][a] = 1
	}
	for a := 0; a < k; a++ {
		for b := a + 1; b < k; b++ {
			var sum float64
			n := 0
			for _, s := range samples {
				x := make([]float64, len(s))
				y := make([]float64, len(s))
				for i, r := range s {
					x[i], y[i] = r.Values[a], r.Values[b]
				}
				if c := Spearman(x, y); !math.IsNaN(c) {
					sum += c
					n++
				}
			}
			v := math.NaN()
			if n > 0 {
				v = sum / float64(n)
			}
			out[a][b], out[b][a] = v, v
		}
	}
	return out
}

func safeDiv(a, b float64) float64 {
	if b == 0 {
		return math.NaN()
	}
	return a / b
}

// Count is the number of days in the window ending at end on which x equals
// v, or NaN if the window reaches before the start of the series.
func Count(x []float64, v float64, end, window int) float64 {
	start := end - window + 1
	if start < 0 || end >= len(x) {
		return math.NaN()
	}
	n := 0
	for i := start; i <= end; i++ {
		if x[i] == v {
			n++
		}
	}
	return float64(n)
}
