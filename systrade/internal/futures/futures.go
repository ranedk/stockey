// Package futures turns Dhan's futures "slots" into something a backtest may
// read: a cleaned curve, the roll dates hidden inside each slot's history, a
// Panama-stitched continuous price, and a splice-safe carry series.
//
// What the raw data actually is (verified 2026-09-07, superseding the sketch
// in docs/data_notes.md):
//
//   - A slot is a Dhan security id, named for the contract it held when the
//     backfill ran. Its history is NOT that contract's life: it is a
//     continuous series at roughly one curve position, formed by concatenating
//     successive contract generations WITHOUT adjusting for the roll.
//   - Several ids return byte-identical histories. Five of GOLDM's seven do.
//     Only about three genuine curve positions exist per underlying.
//   - Dhan recycles security ids, and the history attached to a recycled id
//     belongs to whatever instrument held it before. Two of NIFTY's five slots
//     are option premium series: NIFTY-2026-06-30 prints 117.10 on a day the
//     index closed 10458.40.
//
// So every series must be proven before it is used. Clean rejects what cannot
// be proven and says why; nothing here silently repairs anything.
package futures

import (
	"fmt"
	"math"
	"sort"
	"time"

	"github.com/ranedk/systrader/internal/core"
)

// Slot is one security id's history plus the expiry it was named for.
type Slot struct {
	Ticker string
	Expiry time.Time
	Closes core.Series
}

// Rejection records a slot that did not survive cleaning, and why. Kept in the
// result rather than logged and forgotten: "which series did you throw away"
// is the first question to ask of any cleaned dataset.
type Rejection struct {
	Ticker string
	Reason string
}

// Curve is the cleaned ladder: Positions[0] is the front, Positions[1] the
// next, and so on.
type Curve struct {
	Underlying string
	Positions  []Slot
	Rejected   []Rejection
}

// Cleaning thresholds. Deliberately loose: they exist to catch a series that
// belongs to a different instrument, not to fine-tune which futures series we
// like. A recycled option id misses by a factor of 50, not by 10%.
const (
	minBars = 250 // a year of history before a slot is worth anything
	// maxLevelRatio: adjacent curve positions differ ONLY by the cost of carry
	// between their expiries — a percent or so a month, and 5.7% across
	// SILVERM's quarterly ladder, the widest genuine spread in this data. A
	// series sitting 15% away from the rest of the curve is not a position on
	// it. Loosening this to 2.0 let a BANKNIFTY slot through that was 23% off
	// and produced a Panama series with negative prices.
	maxLevelRatio     = 1.15
	minReturnCorr     = 0.80 // vs the median slot's daily log returns
	duplicateTol      = 1e-9 // closes this close on every shared date are one series
	minOverlapForTest = 60
)

// Clean rejects slots that cannot be shown to belong to this underlying's
// curve, collapses duplicate streams, and orders what is left by expiry —
// which is curve position, because the whole ladder shifts together at a roll.
func Clean(underlying string, slots []Slot) Curve {
	c := Curve{Underlying: underlying}
	var kept []Slot
	for _, s := range slots {
		if s.Closes.Len() < minBars {
			c.Rejected = append(c.Rejected, Rejection{s.Ticker,
				fmt.Sprintf("only %d bars, need %d", s.Closes.Len(), minBars)})
			continue
		}
		kept = append(kept, s)
	}
	if len(kept) < 2 {
		c.Positions = kept
		return c
	}

	// The reference is the cross-slot median close and median daily return on
	// each date: robust to a minority of impostors, and needs no external
	// series, so it works for MCX commodities where no spot exists.
	refLevel, refRet := medianReference(kept)

	var proven []Slot
	for _, s := range kept {
		ratio, corr, n := compareToReference(s, refLevel, refRet)
		switch {
		case n < minOverlapForTest:
			c.Rejected = append(c.Rejected, Rejection{s.Ticker,
				fmt.Sprintf("only %d dates overlap the rest of the curve", n)})
		case ratio > maxLevelRatio || ratio < 1/maxLevelRatio:
			c.Rejected = append(c.Rejected, Rejection{s.Ticker,
				fmt.Sprintf("level is %.3gx the rest of the curve — recycled security id, not this instrument", ratio)})
		case corr < minReturnCorr:
			c.Rejected = append(c.Rejected, Rejection{s.Ticker,
				fmt.Sprintf("daily returns correlate %.2f with the rest of the curve — different instrument", corr)})
		default:
			proven = append(proven, s)
		}
	}

	c.Positions = dedupe(&c, proven)
	sort.Slice(c.Positions, func(i, j int) bool { return c.Positions[i].Expiry.Before(c.Positions[j].Expiry) })
	return c
}

// dedupe collapses slots whose closes agree on every shared date. The survivor
// is the one whose history runs longest — the live slot still being updated —
// with the nearer expiry breaking a tie.
func dedupe(c *Curve, slots []Slot) []Slot {
	var out []Slot
	for _, s := range slots {
		dup := -1
		for i, k := range out {
			if identical(s.Closes, k.Closes) {
				dup = i
				break
			}
		}
		if dup < 0 {
			out = append(out, s)
			continue
		}
		keep, drop := out[dup], s
		if lastDate(s.Closes).After(lastDate(keep.Closes)) ||
			(lastDate(s.Closes).Equal(lastDate(keep.Closes)) && s.Expiry.Before(keep.Expiry)) {
			keep, drop = s, out[dup]
		}
		out[dup] = keep
		c.Rejected = append(c.Rejected, Rejection{drop.Ticker,
			"byte-identical to " + keep.Ticker + " — Dhan serves one stream under several ids"})
	}
	return out
}

func identical(a, b core.Series) bool {
	shared, av, bv := align(a, b)
	if shared < minOverlapForTest {
		return false
	}
	for i := range av {
		if math.Abs(av[i]-bv[i]) > duplicateTol {
			return false
		}
	}
	return true
}

func lastDate(s core.Series) time.Time {
	if s.Len() == 0 {
		return time.Time{}
	}
	return s.Times[s.Len()-1]
}

// medianReference builds the cross-slot median level and median daily log
// return per date.
func medianReference(slots []Slot) (level, ret map[time.Time]float64) {
	levels := map[time.Time][]float64{}
	rets := map[time.Time][]float64{}
	for _, s := range slots {
		for i := 0; i < s.Closes.Len(); i++ {
			d, v := s.Closes.Times[i], s.Closes.Values[i]
			if v <= 0 || math.IsNaN(v) {
				continue
			}
			levels[d] = append(levels[d], v)
			if i > 0 {
				p := s.Closes.Values[i-1]
				if p > 0 && !math.IsNaN(p) {
					rets[d] = append(rets[d], math.Log(v/p))
				}
			}
		}
	}
	level = make(map[time.Time]float64, len(levels))
	for d, xs := range levels {
		level[d] = median(xs)
	}
	ret = make(map[time.Time]float64, len(rets))
	for d, xs := range rets {
		ret[d] = median(xs)
	}
	return level, ret
}

// compareToReference returns the slot's median level ratio to the curve, the
// correlation of its daily returns with the curve's, and the overlap count.
func compareToReference(s Slot, refLevel, refRet map[time.Time]float64) (ratio, corr float64, n int) {
	var ratios []float64
	var xs, ys []float64
	for i := 0; i < s.Closes.Len(); i++ {
		d, v := s.Closes.Times[i], s.Closes.Values[i]
		if r, ok := refLevel[d]; ok && r > 0 && v > 0 {
			ratios = append(ratios, v/r)
			n++
		}
		if i == 0 {
			continue
		}
		p := s.Closes.Values[i-1]
		if r, ok := refRet[d]; ok && p > 0 && v > 0 {
			xs = append(xs, math.Log(v/p))
			ys = append(ys, r)
		}
	}
	if len(ratios) == 0 {
		return 0, 0, n
	}
	return median(ratios), pearson(xs, ys), n
}

func align(a, b core.Series) (n int, av, bv []float64) {
	idx := make(map[time.Time]float64, b.Len())
	for i, d := range b.Times {
		idx[d] = b.Values[i]
	}
	for i, d := range a.Times {
		if v, ok := idx[d]; ok {
			av = append(av, a.Values[i])
			bv = append(bv, v)
			n++
		}
	}
	return n, av, bv
}

func median(x []float64) float64 {
	if len(x) == 0 {
		return math.NaN()
	}
	c := append([]float64(nil), x...)
	sort.Float64s(c)
	return c[len(c)/2]
}

func pearson(a, b []float64) float64 {
	if len(a) < 2 || len(a) != len(b) {
		return math.NaN()
	}
	n := float64(len(a))
	var sa, sb float64
	for i := range a {
		sa += a[i]
		sb += b[i]
	}
	ma, mb := sa/n, sb/n
	var cov, va, vb float64
	for i := range a {
		da, db := a[i]-ma, b[i]-mb
		cov += da * db
		va += da * da
		vb += db * db
	}
	if va <= 0 || vb <= 0 {
		return math.NaN()
	}
	return cov / math.Sqrt(va*vb)
}
