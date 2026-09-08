// Package explore answers "where and when did this rule work?" — the question
// six trials in this ledger have never once asked.
//
// It is EXPLORATION, and the distinction is not decorative. Nothing here
// produces a verdict, a p-value on a winning bucket, or a number anyone may
// put in a ledger row as evidence. Its output is a map of where an effect
// lives, from which a hypothesis with a mechanism can be written down and then
// confirmed somewhere this package has never looked.
//
// The reason to be careful is arithmetic, not superstition. This package cuts
// the data along eight dimensions at up to ten buckets each; the best of ~60
// buckets shows t ~ 3 on pure noise. LEDGER row 12 is the worked example from
// this very workspace: "buy the names that fell least on panic days" scored
// +1.17% at t=+2.49, and row 13 showed the slice WAS the finding — it vanished
// once the pattern was removed from both the day definition and the ranking.
//
// So every statistic here is built to expose fragility rather than hide it:
//
//   - Each bucket carries its OWN matched control. The statistic is the
//     forward return of the top-forecast names WITHIN a bucket minus the mean
//     of every name in that same bucket on that same day. A sector is compared
//     to itself, never to the market, so neither the market's move nor the
//     sector's own drift can masquerade as selection skill.
//   - Every bucket is reported split by half-sample and by year. An effect
//     that lives in one half is what a dead effect looks like from the inside
//     (row 13 again), and the reader can see that at a glance.
//   - The header states how many buckets were examined, because that number is
//     what determines whether the best of them means anything.
package explore

import (
	"math"
	"sort"
	"time"
)

// Obs is one symbol on one decision day: the rule's forecast, what the trade
// earned, and the attributes we can slice by. Attributes are point-in-time —
// each is computed from data available at that close and nothing later.
type Obs struct {
	Sym      int32
	Forecast float64
	FwdRet   float64
	// Selected marks an EVENT signal — a pattern that either fired on this
	// name today or did not. When any observation in a bucket-day is
	// selected, those names become the long side instead of the top fifth by
	// forecast, and a bucket-day where nothing fired is skipped rather than
	// filled with the least-bad alternative. This is how a discrete screener
	// (a candle pattern, a breakout alert) is measured against the same
	// same-day, same-bucket control as a continuous rule.
	Selected bool

	Turnover float64 // 60-bar median traded value
	Vol      float64 // trailing realized volatility
	Price    float64
	Mcap     float64 // latest reported market cap on or before this date, 0 if unknown
	Sector   string  // static attribute, "" if unknown
	AboveSMA bool    // the symbol's own 200-day trend state
	// Extra carries caller-defined attributes — the indicator state a
	// specific screener cares about, computed for EVERY name rather than only
	// the ones that fired, so a slice on "names touching the lower band" holds
	// the pattern's candidates and the control side by side. A fixed array
	// rather than a map: this struct exists 2.5 million times.
	Extra [8]float64
}

// Day is one decision day's cross-section.
type Day struct {
	Date time.Time
	Obs  []Obs
}

// minNamesPerBucket: a bucket-day with fewer names than this has no usable
// cross-section — "the top fifth" of four names is one name and a coin flip.
const minNamesPerBucket = 5

// topFraction of a bucket's names, by forecast, form the long side.
const topFraction = 0.2

// Bucketed is one bucket's exploratory record.
type Bucket struct {
	Dimension string
	Label     string
	Days      int
	Names     float64 // mean names per day in this bucket
	Held      float64 // mean names actually held per day (the long side)
	Edge      float64 // mean daily edge: top-forecast names minus the bucket's own mean
	// BottomEdge is the same statistic for the BOTTOM forecast fifth. A real
	// signal is roughly monotone: if the top wins and the bottom does not
	// lose, whatever is being measured is not the forecast's ordering.
	BottomEdge float64
	// VolRatio is the mean own-volatility of the selected names over the
	// bucket's mean. A number well above 1 says the "edge" may simply be
	// risk: higher-beta names paying more in a rising market, which is what
	// LEDGER row 4 caught calling itself selection alpha.
	VolRatio  float64
	FirstHalf float64
	SecondHal float64
	ByYear    map[int]float64
	yearSums  map[int]float64
	yearN     map[int]int
	sum       float64
	botSum    float64
	volSum    float64
	firstSum  float64
	firstN    int
	secondSum float64
	secondN   int
	nameSum   float64
	heldSum   float64
}

// YearsPositive reports how many of the years in which this bucket traded had
// a positive edge, and how many years there were. Consistency across years is
// the cheapest available guard against a single lucky stretch.
func (b Bucket) YearsPositive() (pos, total int) {
	for _, v := range b.ByYear {
		total++
		if v > 0 {
			pos++
		}
	}
	return pos, total
}

// Dimension slices the cross-section. Bucket returns the label an observation
// belongs to on a given day, or "" to leave it out.
type Dimension struct {
	Name string
	// Assign labels every observation of one day at once, because most of
	// these are cross-sectional ranks that only exist relative to that day.
	Assign func(d Day) []string
	// Order sorts labels for display; nil means alphabetical.
	Order []string
}

// Result is the whole exploration.
type Result struct {
	Rule         string
	Horizon      int
	Days         int
	Observations int
	Overall      Bucket   // the rule with no slicing at all: the yardstick
	Buckets      []Bucket // every dimension x bucket examined
	Dimensions   int
}

// Run computes the per-bucket edges for a CONTINUOUS rule: the long side is
// the top fifth of each bucket by forecast.
func Run(days []Day, dims []Dimension, ruleName string, horizon int) Result {
	return run(days, dims, ruleName, horizon, false)
}

// RunEvent computes them for a DISCRETE screener: the long side is whatever
// the pattern marked Selected, and a bucket-day it did not fire on is skipped
// rather than filled with the least-bad alternative. The mode is explicit
// because "nothing fired today" and "this rule has no opinion today" look
// identical in the data and mean opposite things.
func RunEvent(days []Day, dims []Dimension, name string, horizon int) Result {
	return run(days, dims, name, horizon, true)
}

func run(days []Day, dims []Dimension, ruleName string, horizon int, event bool) Result {
	res := Result{Rule: ruleName, Horizon: horizon, Days: len(days), Dimensions: len(dims)}
	if len(days) == 0 {
		return res
	}
	mid := days[len(days)/2].Date

	acc := map[string]*Bucket{}
	get := func(dim, label string) *Bucket {
		k := dim + "\x00" + label
		b, ok := acc[k]
		if !ok {
			b = &Bucket{Dimension: dim, Label: label,
				ByYear: map[int]float64{}, yearSums: map[int]float64{}, yearN: map[int]int{}}
			acc[k] = b
		}
		return b
	}

	for _, d := range days {
		res.Observations += len(d.Obs)
		// The unsliced yardstick: one bucket holding the whole day.
		add(get("(none)", "whole universe"), d, allIndices(len(d.Obs)), mid, event)

		for _, dim := range dims {
			labels := dim.Assign(d)
			byLabel := map[string][]int{}
			for i, l := range labels {
				if l == "" {
					continue
				}
				byLabel[l] = append(byLabel[l], i)
			}
			for l, idx := range byLabel {
				add(get(dim.Name, l), d, idx, mid, event)
			}
		}
	}

	for _, b := range acc {
		if b.Days == 0 {
			continue
		}
		b.Edge = b.sum / float64(b.Days)
		b.BottomEdge = b.botSum / float64(b.Days)
		b.VolRatio = b.volSum / float64(b.Days)
		b.Names = b.nameSum / float64(b.Days)
		b.Held = b.heldSum / float64(b.Days)
		if b.firstN > 0 {
			b.FirstHalf = b.firstSum / float64(b.firstN)
		} else {
			b.FirstHalf = math.NaN()
		}
		if b.secondN > 0 {
			b.SecondHal = b.secondSum / float64(b.secondN)
		} else {
			b.SecondHal = math.NaN()
		}
		for y, s := range b.yearSums {
			b.ByYear[y] = s / float64(b.yearN[y])
		}
		if b.Dimension == "(none)" {
			res.Overall = *b
			continue
		}
		res.Buckets = append(res.Buckets, *b)
	}
	sort.Slice(res.Buckets, func(i, j int) bool {
		if res.Buckets[i].Dimension != res.Buckets[j].Dimension {
			return res.Buckets[i].Dimension < res.Buckets[j].Dimension
		}
		return res.Buckets[i].Label < res.Buckets[j].Label
	})
	return res
}

// add records one bucket-day: the mean forward return of the top-forecast
// names inside this bucket, minus the mean of every name in it. Same day, same
// bucket, so the market's move and the bucket's own drift both cancel.
func add(b *Bucket, d Day, idx []int, mid time.Time, event bool) {
	if len(idx) < minNamesPerBucket {
		return
	}
	sorted := append([]int(nil), idx...)
	// Event mode: the pattern picked the names, so no ranking is needed and a
	// day it did not fire on is not an observation.
	fired := 0
	for _, k := range idx {
		if d.Obs[k].Selected {
			fired++
		}
	}
	if event {
		sort.Slice(sorted, func(i, j int) bool {
			return boolScore(d.Obs[sorted[i]].Selected) > boolScore(d.Obs[sorted[j]].Selected)
		})
	} else {
		sort.Slice(sorted, func(i, j int) bool {
			return d.Obs[sorted[i]].Forecast > d.Obs[sorted[j]].Forecast
		})
	}
	n := int(math.Round(topFraction * float64(len(sorted))))
	if event {
		n = fired
	}
	if n < 1 {
		return
	}
	var top, bottom, all, topVol, allVol float64
	for i, k := range sorted {
		o := d.Obs[k]
		all += o.FwdRet
		allVol += o.Vol
		if i < n {
			top += o.FwdRet
			topVol += o.Vol
		}
		if i >= len(sorted)-n {
			bottom += o.FwdRet
		}
	}
	mean := all / float64(len(sorted))
	edge := top/float64(n) - mean
	meanVol := allVol / float64(len(sorted))

	b.Days++
	b.sum += edge
	b.heldSum += float64(n)
	if event {
		// "The worst fifth by forecast" means nothing when the pattern did the
		// choosing: there is no ordering to invert.
		b.botSum = math.NaN()
	} else {
		b.botSum += bottom/float64(n) - mean
	}
	if meanVol > 0 {
		b.volSum += (topVol / float64(n)) / meanVol
	} else {
		b.volSum += 1
	}
	b.nameSum += float64(len(sorted))
	y := d.Date.Year()
	b.yearSums[y] += edge
	b.yearN[y]++
	if d.Date.Before(mid) {
		b.firstSum += edge
		b.firstN++
	} else {
		b.secondSum += edge
		b.secondN++
	}
}

func boolScore(b bool) float64 {
	if b {
		return 1
	}
	return 0
}

func allIndices(n int) []int {
	out := make([]int, n)
	for i := range out {
		out[i] = i
	}
	return out
}
