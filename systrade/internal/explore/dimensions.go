package explore

import (
	"fmt"
	"math"
	"sort"
	"strings"
)

// QuantileDimension buckets the day's cross-section by rank on a continuous
// attribute — liquidity, size, volatility, price. Ranks are computed WITHIN
// the day, so a bucket means "the most liquid tenth of what was tradable that
// day", not "above some rupee threshold that meant something different in
// 2015 than it does now". Observations whose value is missing are left out of
// this dimension rather than lumped into a bucket.
func QuantileDimension(name string, k int, value func(Obs) float64) Dimension {
	return quantileDimension(name, k, value, func(v float64) bool { return v > 0 })
}

// RankDimension is QuantileDimension for a signed attribute — beta, distance
// below a high — where zero and negative values are real values, not missing
// ones. Only NaN is left out.
func RankDimension(name string, k int, value func(Obs) float64) Dimension {
	return quantileDimension(name, k, value, func(v float64) bool { return !math.IsNaN(v) })
}

func quantileDimension(name string, k int, value func(Obs) float64, keep func(float64) bool) Dimension {
	labels := make([]string, k)
	for i := range labels {
		labels[i] = quantileLabel(i, k)
	}
	return Dimension{
		Name:  name,
		Order: labels,
		Assign: func(d Day) []string {
			out := make([]string, len(d.Obs))
			idx := make([]int, 0, len(d.Obs))
			for i, o := range d.Obs {
				if v := value(o); math.IsNaN(v) || !keep(v) {
					continue
				}
				idx = append(idx, i)
			}
			if len(idx) < k*minNamesPerBucket {
				return out // too thin a day to cut this finely: leave it out
			}
			sort.Slice(idx, func(a, b int) bool { return value(d.Obs[idx[a]]) < value(d.Obs[idx[b]]) })
			for rank, i := range idx {
				b := rank * k / len(idx)
				if b >= k {
					b = k - 1
				}
				out[i] = labels[b]
			}
			return out
		},
	}
}

func quantileLabel(i, k int) string {
	switch {
	case i == 0:
		return fmt.Sprintf("Q%d (lowest)", i+1)
	case i == k-1:
		return fmt.Sprintf("Q%d (highest)", i+1)
	default:
		return fmt.Sprintf("Q%d", i+1)
	}
}

// CategoryDimension buckets by a static label such as sector.
func CategoryDimension(name string, value func(Obs) string) Dimension {
	return Dimension{
		Name: name,
		Assign: func(d Day) []string {
			out := make([]string, len(d.Obs))
			for i, o := range d.Obs {
				out[i] = value(o)
			}
			return out
		},
	}
}

// BooleanDimension splits the cross-section in two — the symbol's own trend
// state, index membership, anything with a yes and a no.
func BooleanDimension(name, yes, no string, pred func(Obs) bool) Dimension {
	return Dimension{
		Name: name, Order: []string{yes, no},
		Assign: func(d Day) []string {
			out := make([]string, len(d.Obs))
			for i, o := range d.Obs {
				if pred(o) {
					out[i] = yes
				} else {
					out[i] = no
				}
			}
			return out
		},
	}
}

// DayDimension labels the whole day at once: market regime, calendar year —
// anything that is a property of the date rather than of the symbol. Every
// name that day lands in the same bucket, so the statistic still compares the
// bucket to itself.
func DayDimension(name string, label func(d Day) string, order []string) Dimension {
	return Dimension{
		Name: name, Order: order, Daily: true,
		Assign: func(d Day) []string {
			l := label(d)
			out := make([]string, len(d.Obs))
			for i := range out {
				out[i] = l
			}
			return out
		},
	}
}

// BreadthLabel buckets a day by how much of its own cross-section is in an
// uptrend — the market regime, computed from the universe itself rather than
// from an index, so it needs no second data source and no alignment.
func BreadthLabel(d Day) string {
	if len(d.Obs) == 0 {
		return ""
	}
	var up float64
	for _, o := range d.Obs {
		if o.AboveSMA {
			up++
		}
	}
	switch f := up / float64(len(d.Obs)); {
	case f < 0.2:
		return "breadth <20% (washout)"
	case f < 0.4:
		return "breadth 20-40%"
	case f < 0.6:
		return "breadth 40-60%"
	case f < 0.8:
		return "breadth 60-80%"
	default:
		return "breadth >80% (broad rally)"
	}
}

// BreadthOrder is BreadthLabel's buckets, weakest tape first.
var BreadthOrder = []string{
	"breadth <20% (washout)", "breadth 20-40%", "breadth 40-60%",
	"breadth 60-80%", "breadth >80% (broad rally)",
}

// BinDimension buckets a count by fixed edges rather than by rank — for an
// attribute most names share one value of (most stocks hit no price band in a
// quarter), where rank buckets would split identical names by tie order.
// edges[i] is bucket i's lower bound, ascending; values below edges[0] and
// NaN are left out.
func BinDimension(name string, value func(Obs) float64, edges []float64, labels []string) Dimension {
	return Dimension{
		Name: name, Order: labels,
		Assign: func(d Day) []string {
			out := make([]string, len(d.Obs))
			for i, o := range d.Obs {
				v := value(o)
				if math.IsNaN(v) {
					continue
				}
				for j := len(edges) - 1; j >= 0; j-- {
					if v >= edges[j] {
						out[i] = labels[j]
						break
					}
				}
			}
			return out
		},
	}
}

// CrossDimension crosses two dimensions into one grid: a cell is a name that
// sits in a bucket of BOTH, on the same day. It exists to answer the question
// one trait at a time cannot — whether an effect that looks like "small names"
// is really "small AND volatile names", with the two traits confounded in
// every single-trait cut.
//
// A grid multiplies buckets, and the best of many buckets is what noise looks
// like, so the rule this tool is built under is that the PAIRS ARE NAMED IN
// ADVANCE (README, step 2 of the slicer). Nothing here stops a caller crossing
// everything with everything; the caller has to be the one that refuses.
//
// A name either dimension leaves out is left out of the grid: the cell must
// mean "both traits said this", never "one trait said this and the other had
// no opinion". Cells too thin on a day are dropped by the same guard every
// bucket-day passes through, so a grid fine enough to empty its corners loses
// those days rather than scoring five names as a cross-section.
//
// The result is day-level only if BOTH parents are: a market regime crossed
// with a stock trait still picks out stocks, and must be controlled against
// same-size random groups of stocks rather than against other days.
func CrossDimension(a, b Dimension) Dimension {
	var order []string
	if a.Order != nil && b.Order != nil {
		for _, la := range a.Order {
			for _, lb := range b.Order {
				order = append(order, crossLabel(la, lb))
			}
		}
	}
	return Dimension{
		Name:  a.Name + " x " + b.Name,
		Order: order,
		Daily: a.Daily && b.Daily,
		Assign: func(d Day) []string {
			la, lb := a.Assign(d), b.Assign(d)
			out := make([]string, len(d.Obs))
			for i := range out {
				if i >= len(la) || i >= len(lb) || la[i] == "" || lb[i] == "" {
					continue
				}
				out[i] = crossLabel(la[i], lb[i])
			}
			return out
		},
	}
}

// crossLabel keeps a grid cell short enough to read in a table: the quantile
// hints ("lowest", "highest") are dropped because the grid's own header says
// which trait comes first and that Q1 is the low end.
func crossLabel(a, b string) string { return compactLabel(a) + " | " + compactLabel(b) }

func compactLabel(s string) string {
	for _, suffix := range []string{" (lowest)", " (highest)"} {
		if strings.HasSuffix(s, suffix) {
			return strings.TrimSuffix(s, suffix)
		}
	}
	return s
}
