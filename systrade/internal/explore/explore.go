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

	"github.com/ranedk/systrader/internal/evidence"
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
	Sector   string // static attribute, "" if unknown
	AboveSMA bool   // the symbol's own 200-day trend state; feeds market breadth
	// The trait library's additions (LEDGER rows 27-28), NaN where undefined.
	Beta       float64 // 252-day beta to the equal-weight eligible universe
	Delivery   float64 // 20-day median delivery %
	Dist52     float64 // distance below the 52-week high, at most 0
	UpCircuits float64 // upper price-band hits in the last 60 trading days
	LoCircuits float64 // lower price-band hits in the last 60 trading days
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
	VolRatio float64
	// Churn is the share of the selected names that were not selected one
	// holding period earlier — the part of the book that actually trades each
	// period, and so what costs are charged on. NaN when no bucket-day had a
	// comparable one a horizon back.
	Churn     float64
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
	hist      map[int][]int32 // selected symbols by day index, for churn
	daily     []float64       // this bucket's edge on each day index, NaN where absent
	ctrlDaily []float64       // a same-size random group's edge that day, NaN where absent
	dayLevel  bool            // the bucket labels whole days (year, breadth), not stocks
	churnSum  float64
	churnN    int
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
	// Daily marks a dimension that labels whole days rather than stocks, so
	// its buckets are compared with other days, not with random stocks.
	Daily bool
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
	get := func(dim, label string, dayLevel bool) *Bucket {
		k := dim + "\x00" + label
		b, ok := acc[k]
		if !ok {
			b = &Bucket{Dimension: dim, Label: label,
				ByYear: map[int]float64{}, yearSums: map[int]float64{}, yearN: map[int]int{}, hist: map[int][]int32{},
				daily: nanSlice(len(days)), ctrlDaily: nanSlice(len(days)), dayLevel: dayLevel}
			acc[k] = b
		}
		return b
	}

	// Days must arrive in date order, one per trading day: churn compares each
	// bucket-day with the same bucket `horizon` days back by position.
	for di, d := range days {
		res.Observations += len(d.Obs)
		// The unsliced yardstick: one bucket holding the whole day.
		add(get("(none)", "whole universe", true), d, allIndices(len(d.Obs)), nil, di, horizon, mid, event)

		for _, dim := range dims {
			labels := dim.Assign(d)
			byLabel := map[string][]int{}
			var covered []int // the names this dimension could place today: controls are drawn from them
			for i, l := range labels {
				if l == "" {
					continue
				}
				byLabel[l] = append(byLabel[l], i)
				covered = append(covered, i)
			}
			for l, idx := range byLabel {
				add(get(dim.Name, l, dim.Daily), d, idx, covered, di, horizon, mid, event)
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
		b.Churn = math.NaN()
		if b.churnN > 0 {
			b.Churn = b.churnSum / float64(b.churnN)
		}
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
func add(b *Bucket, d Day, idx, covered []int, di, horizon int, mid time.Time, event bool) {
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
	sel := make([]int32, n)
	for i := 0; i < n; i++ {
		sel[i] = d.Obs[sorted[i]].Sym
	}
	if prev, ok := b.hist[di-horizon]; ok {
		held := make(map[int32]bool, len(prev))
		for _, s := range prev {
			held[s] = true
		}
		fresh := 0
		for _, s := range sel {
			if !held[s] {
				fresh++
			}
		}
		b.churnSum += float64(fresh) / float64(len(sel))
		b.churnN++
		delete(b.hist, di-horizon)
	}
	b.hist[di] = sel
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
	b.daily[di] = edge
	if !b.dayLevel && len(covered) > len(idx) {
		b.ctrlDaily[di] = matchedControl(d, covered, len(idx), bucketSeed(b, di), event)
	}
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

// Deviation answers the exploration question for every bucket at once: does
// the rule do better or worse there than it would anyway, by more than the
// day-to-day wobble allows — and by more than searching this many buckets
// would turn up with nothing going on?
//
// "Than it would anyway" needs a matched control (Law 3), and a size-matched
// one: a bucket's edge depends on how many names it holds (the top fifth of a
// small bucket is less extreme than the top fifth of the universe), and so
// does a rank correlation's expected value. Comparing buckets with the whole
// universe therefore manufactures differences for small buckets even where
// the rule works identically — it flagged 7, then 11, of 30 uniform-effect
// test datasets. So:
//
//   - a bucket of STOCKS is compared, day by day, with random groups of the
//     same size drawn from the names its dimension covered that day;
//   - a bucket of DAYS (year, market breadth) holds every name, so it is
//     compared with the rule on all the other days.
//
// Each difference is resampled in blocks long enough to carry the dependence
// of overlapping forward returns and slow-changing selections, the SAME
// blocks for every bucket, and the threshold is family-wise: the 95th
// percentile of the largest centred score any bucket reached.
type Deviation struct {
	Z         map[string]float64 // per bucket: its difference over that difference's standard error
	Threshold float64            // family-wise: 95th percentile of the max |Z| under no difference
	Reps      int
	MeanBlock float64
}

// BucketKey identifies a bucket.
func BucketKey(b Bucket) string { return b.Dimension + "\x00" + b.Label }

// controlsPerBucketDay random same-size groups are averaged into each day's
// control, to keep the control's own noise well below the bucket's.
const controlsPerBucketDay = 3

// Deviations computes Z for every bucket of res and the family-wise threshold.
func Deviations(res Result, bs evidence.Bootstrap) (Deviation, error) {
	dev := Deviation{Z: map[string]float64{}, Reps: bs.Reps, MeanBlock: bs.MeanBlock, Threshold: math.Inf(1)}
	all := res.Overall.daily
	n := len(all)
	type scorer struct {
		key  string
		obs  float64
		stat func(idx []int) float64
		boot []float64
	}
	var ss []*scorer
	identity := allIndices(n)
	for _, b := range res.Buckets {
		b := b
		var stat func(idx []int) float64
		if b.dayLevel {
			// This bucket's days against every day, on the universe's edge.
			stat = func(idx []int) float64 {
				var in, tot float64
				var nin, ntot int
				for _, t := range idx {
					u := all[t]
					if math.IsNaN(u) {
						continue
					}
					tot += u
					ntot++
					if t < len(b.daily) && !math.IsNaN(b.daily[t]) {
						in += u
						nin++
					}
				}
				if nin == 0 || ntot == 0 {
					return math.NaN()
				}
				return in/float64(nin) - tot/float64(ntot)
			}
		} else {
			d := make([]float64, n)
			for t := range d {
				d[t] = math.NaN()
				if t < len(b.daily) && t < len(b.ctrlDaily) && !math.IsNaN(b.daily[t]) && !math.IsNaN(b.ctrlDaily[t]) {
					d[t] = b.daily[t] - b.ctrlDaily[t]
				}
			}
			stat = func(idx []int) float64 {
				var sum float64
				k := 0
				for _, t := range idx {
					if v := d[t]; !math.IsNaN(v) {
						sum += v
						k++
					}
				}
				if k == 0 {
					return math.NaN()
				}
				return sum / float64(k)
			}
		}
		if obs := stat(identity); !math.IsNaN(obs) {
			ss = append(ss, &scorer{key: BucketKey(b), obs: obs, stat: stat})
		}
	}
	if len(ss) == 0 {
		return dev, nil
	}
	err := bs.Each(n, func(_ int, idx []int) {
		for _, s := range ss {
			s.boot = append(s.boot, s.stat(idx))
		}
	})
	if err != nil {
		return dev, err
	}
	se := make([]float64, len(ss))
	for i, s := range ss {
		var m, q float64
		k := 0
		for _, v := range s.boot {
			if !math.IsNaN(v) {
				m += v
				k++
			}
		}
		if k < 2 {
			continue
		}
		m /= float64(k)
		for _, v := range s.boot {
			if !math.IsNaN(v) {
				q += (v - m) * (v - m)
			}
		}
		se[i] = math.Sqrt(q / float64(k-1))
		if se[i] > 0 {
			dev.Z[s.key] = s.obs / se[i]
		}
	}
	maxZ := make([]float64, bs.Reps)
	for i, s := range ss {
		if !(se[i] > 0) {
			continue
		}
		for r, v := range s.boot {
			if z := math.Abs(v-s.obs) / se[i]; !math.IsNaN(z) && z > maxZ[r] {
				maxZ[r] = z
			}
		}
	}
	sort.Float64s(maxZ)
	pos := 0.95 * float64(len(maxZ)-1)
	lo := int(pos)
	if lo >= len(maxZ)-1 {
		dev.Threshold = maxZ[len(maxZ)-1]
	} else {
		dev.Threshold = maxZ[lo] + (pos-float64(lo))*(maxZ[lo+1]-maxZ[lo])
	}
	return dev, nil
}

// matchedControl is the mean edge of controlsPerBucketDay random groups of
// size n drawn from the covered names — the same statistic add computes for
// the bucket, on names that share nothing with it but the day and the count.
// NaN if no group produced an edge (an event rule that fired in none).
func matchedControl(d Day, covered []int, n int, seed uint64, event bool) float64 {
	scratch := append([]int(nil), covered...)
	var sum float64
	k := 0
	state := seed
	for c := 0; c < controlsPerBucketDay; c++ {
		for i := 0; i < n; i++ { // partial Fisher-Yates: the first n are a uniform draw
			state += 0x9E3779B97F4A7C15
			j := i + int(mix(state)%uint64(len(scratch)-i))
			scratch[i], scratch[j] = scratch[j], scratch[i]
		}
		if e, ok := edgeOf(d, scratch[:n], event); ok {
			sum += e
			k++
		}
	}
	if k == 0 {
		return math.NaN()
	}
	return sum / float64(k)
}

// edgeOf is add's headline statistic alone: the long side's mean forward
// return minus the group's.
func edgeOf(d Day, idx []int, event bool) (float64, bool) {
	if len(idx) < minNamesPerBucket {
		return 0, false
	}
	var all float64
	for _, k := range idx {
		all += d.Obs[k].FwdRet
	}
	mean := all / float64(len(idx))
	if event {
		var top float64
		fired := 0
		for _, k := range idx {
			if d.Obs[k].Selected {
				top += d.Obs[k].FwdRet
				fired++
			}
		}
		if fired == 0 {
			return 0, false
		}
		return top/float64(fired) - mean, true
	}
	sorted := append([]int(nil), idx...)
	sort.Slice(sorted, func(i, j int) bool { return d.Obs[sorted[i]].Forecast > d.Obs[sorted[j]].Forecast })
	nTop := int(math.Round(topFraction * float64(len(sorted))))
	if nTop < 1 {
		return 0, false
	}
	var top float64
	for _, k := range sorted[:nTop] {
		top += d.Obs[k].FwdRet
	}
	return top/float64(nTop) - mean, true
}

// bucketSeed makes each bucket-day's controls reproducible regardless of the
// order buckets are visited in.
func bucketSeed(b *Bucket, di int) uint64 {
	h := uint64(1469598103934665603)
	for _, c := range []byte(b.Dimension + "\x00" + b.Label) {
		h ^= uint64(c)
		h *= 1099511628211
	}
	return mix(h ^ uint64(di)*0x9E3779B97F4A7C15)
}

func mix(x uint64) uint64 {
	x ^= x >> 30
	x *= 0xBF58476D1CE4E5B9
	x ^= x >> 27
	x *= 0x94D049BB133111EB
	x ^= x >> 31
	return x
}

func nanSlice(n int) []float64 {
	out := make([]float64, n)
	for i := range out {
		out[i] = math.NaN()
	}
	return out
}
