package explore

import (
	"fmt"
	"math"
	"sort"
)

// QuantileDimension buckets the day's cross-section by rank on a continuous
// attribute — liquidity, size, volatility, price. Ranks are computed WITHIN
// the day, so a bucket means "the most liquid tenth of what was tradable that
// day", not "above some rupee threshold that meant something different in
// 2015 than it does now". Observations whose value is missing are left out of
// this dimension rather than lumped into a bucket.
func QuantileDimension(name string, k int, value func(Obs) float64) Dimension {
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
				v := value(o)
				if math.IsNaN(v) || v <= 0 {
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
		Name: name, Order: order,
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
