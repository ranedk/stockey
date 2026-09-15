package explore

import (
	"math"
	"testing"
	"time"

	"github.com/ranedk/systrader/internal/evidence"
)

// synthDays builds a universe where the rule's forecast predicts the forward
// return ONLY inside one sector, and is pure noise everywhere else. A slice
// explorer that cannot find a planted effect is useless; one that finds it
// everywhere is worse.
func synthDays(nDays, perSector int, liveSector string, edge float64) []Day {
	return synthDaysSeed(42, nDays, perSector, liveSector, edge)
}

func synthDaysSeed(seed int64, nDays, perSector int, liveSector string, edge float64) []Day {
	sectors := []string{"A", "B", "C", "D"}
	rng := newRng(uint64(seed))
	var days []Day
	start := time.Date(2015, 1, 1, 0, 0, 0, 0, time.UTC)
	for d := 0; d < nDays; d++ {
		day := Day{Date: start.AddDate(0, 0, d)}
		marketMove := 0.01 * rng.normal() // a common move that must cancel out
		for si, s := range sectors {
			for i := 0; i < perSector; i++ {
				f := rng.normal()
				r := marketMove + 0.02*rng.normal()
				if s == liveSector {
					r += edge * f // the planted effect
				}
				day.Obs = append(day.Obs, Obs{
					Sym:      int32(si*perSector + i),
					Forecast: f,
					FwdRet:   r,
					Sector:   s,
					Turnover: 1,
					Vol:      1,
					Price:    100,
					AboveSMA: rng.normal() > 0,
				})
			}
		}
		days = append(days, day)
	}
	return days
}

func TestFindsAPlantedEffectInExactlyOneSector(t *testing.T) {
	days := synthDays(400, 20, "C", 0.01)
	dims := []Dimension{CategoryDimension("sector", func(o Obs) string { return o.Sector })}
	res := Run(days, dims, "synthetic", 20)

	byLabel := map[string]Bucket{}
	for _, b := range res.Buckets {
		byLabel[b.Label] = b
	}
	live := byLabel["C"]
	if live.Edge < 0.005 {
		t.Errorf("planted sector C shows edge %.4f, expected clearly positive", live.Edge)
	}
	for _, dead := range []string{"A", "B", "D"} {
		if e := byLabel[dead].Edge; math.Abs(e) > live.Edge/3 {
			t.Errorf("sector %s shows edge %.4f against the planted sector's %.4f — the effect leaked",
				dead, e, live.Edge)
		}
	}
	// The unsliced view must be positive but diluted: this is exactly the case
	// the whole package exists for — an effect that a pooled test underrates.
	if res.Overall.Edge >= live.Edge {
		t.Errorf("pooled edge %.4f is not smaller than the live sector's %.4f", res.Overall.Edge, live.Edge)
	}
}

// TestMarketMoveCancels is the property that makes a bucket's number mean
// anything: a day where every name jumps together must contribute nothing.
func TestMarketMoveCancels(t *testing.T) {
	day := Day{Date: time.Date(2020, 3, 23, 0, 0, 0, 0, time.UTC)}
	for i := 0; i < 50; i++ {
		day.Obs = append(day.Obs, Obs{
			Sym: int32(i), Forecast: float64(i), FwdRet: 0.25, // everything +25%
			Sector: "A", Turnover: 1, Vol: 1, Price: 100,
		})
	}
	res := Run([]Day{day}, []Dimension{CategoryDimension("sector", func(o Obs) string { return o.Sector })}, "r", 20)
	if math.Abs(res.Overall.Edge) > 1e-12 {
		t.Errorf("a day where every name moved identically produced edge %.6f, want 0", res.Overall.Edge)
	}
}

func TestThinBucketDaysAreSkippedNotGuessed(t *testing.T) {
	day := Day{Date: time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC)}
	for i := 0; i < 4; i++ { // fewer than minNamesPerBucket
		day.Obs = append(day.Obs, Obs{Sym: int32(i), Forecast: float64(i), FwdRet: 0.1, Sector: "A"})
	}
	res := Run([]Day{day}, []Dimension{CategoryDimension("sector", func(o Obs) string { return o.Sector })}, "r", 20)
	if len(res.Buckets) != 0 {
		t.Errorf("a 4-name cross-section produced %d buckets; 'the top fifth' of four names is one coin flip", len(res.Buckets))
	}
}

func TestHalvesAreReportedSeparately(t *testing.T) {
	// An effect that exists only in the first half must show up as such: this
	// is LEDGER row 13's failure mode, and the report has to make it visible.
	days := synthDays(200, 20, "C", 0.01)
	for i := len(days) / 2; i < len(days); i++ {
		for j := range days[i].Obs {
			days[i].Obs[j].FwdRet -= 0.01 * days[i].Obs[j].Forecast // cancel the effect
		}
	}
	res := Run(days, []Dimension{CategoryDimension("sector", func(o Obs) string { return o.Sector })}, "r", 20)
	for _, b := range res.Buckets {
		if b.Label != "C" {
			continue
		}
		if !(b.FirstHalf > 3*b.SecondHal) {
			t.Errorf("decayed effect reported as first half %.4f, second half %.4f — the split is not exposing it",
				b.FirstHalf, b.SecondHal)
		}
	}
}

// newRng is a tiny deterministic generator so the fixtures do not depend on
// the standard library's stream staying stable across versions.
type rng struct{ s uint64 }

func newRng(seed uint64) *rng { return &rng{s: seed} }

func (r *rng) next() uint64 {
	r.s ^= r.s << 13
	r.s ^= r.s >> 7
	r.s ^= r.s << 17
	return r.s
}

func (r *rng) normal() float64 {
	// Irwin-Hall: sum of 12 uniforms minus 6 is near enough to a standard
	// normal for a test fixture, and needs no library.
	var s float64
	for i := 0; i < 12; i++ {
		s += float64(r.next()%1000000) / 1000000
	}
	return s - 6
}

// TestEventSignalUsesTheNamesThatFired checks the mode a discrete screener
// needs: the pattern chooses the long side, and a day it did not fire on is
// not an observation at all.
func TestEventSignalUsesTheNamesThatFired(t *testing.T) {
	mk := func(fire bool, ret float64) Obs {
		return Obs{Forecast: 0, FwdRet: ret, Selected: fire, Sector: "A", Vol: 1}
	}
	// Day 1: two names fire and earn 10%, eight do not and earn 0%.
	d1 := Day{Date: time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC)}
	for i := 0; i < 2; i++ {
		d1.Obs = append(d1.Obs, mk(true, 0.10))
	}
	for i := 0; i < 8; i++ {
		d1.Obs = append(d1.Obs, mk(false, 0))
	}
	// Day 2: nothing fires. It must not contribute.
	d2 := Day{Date: time.Date(2020, 1, 2, 0, 0, 0, 0, time.UTC)}
	for i := 0; i < 10; i++ {
		d2.Obs = append(d2.Obs, mk(false, 0.50))
	}

	res := RunEvent([]Day{d1, d2}, []Dimension{CategoryDimension("sector", func(o Obs) string { return o.Sector })}, "event", 20)
	b := res.Overall
	if b.Days != 1 {
		t.Fatalf("recorded %d bucket-days, want 1 — the day nothing fired is not an observation", b.Days)
	}
	// Edge = 10% minus the day's mean of 2%.
	if math.Abs(b.Edge-0.08) > 1e-12 {
		t.Errorf("edge = %.4f, want 0.08 (10%% earned against a 2%% universe)", b.Edge)
	}
	if math.Abs(b.Held-2) > 1e-9 {
		t.Errorf("held %.1f names, want the 2 that fired", b.Held)
	}
	if !math.IsNaN(b.BottomEdge) {
		t.Errorf("bottom edge = %.4f; a pattern that chose its own names has no ordering to invert", b.BottomEdge)
	}
}

func TestRankDimensionKeepsWhatQuantileDimensionDrops(t *testing.T) {
	d := Day{}
	for i, v := range []float64{-2, -1, 0, 1, 2, -3, -4, 5, 6, 7} {
		d.Obs = append(d.Obs, Obs{Sym: int32(i), Beta: v})
	}
	q := QuantileDimension("q", 2, func(o Obs) float64 { return o.Beta }).Assign(d)
	r := RankDimension("r", 2, func(o Obs) float64 { return o.Beta }).Assign(d)
	var qn, rn int
	for i := range d.Obs {
		if q[i] != "" {
			qn++
		}
		if r[i] != "" {
			rn++
		}
	}
	if rn != 10 {
		t.Errorf("RankDimension assigned %d of 10 signed values", rn)
	}
	if qn >= rn {
		t.Errorf("QuantileDimension (%d) should drop the non-positive values RankDimension (%d) keeps", qn, rn)
	}
}

func TestBinDimensionUsesFixedEdges(t *testing.T) {
	d := Day{}
	for _, v := range []float64{0, 0, 1, 2, 3, 5, 6, 40, math.NaN()} {
		d.Obs = append(d.Obs, Obs{UpCircuits: v})
	}
	got := BinDimension("c", func(o Obs) float64 { return o.UpCircuits },
		[]float64{0, 1, 3, 6}, []string{"0", "1-2", "3-5", "6+"}).Assign(d)
	want := []string{"0", "0", "1-2", "1-2", "3-5", "3-5", "6+", "6+", ""}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("value %v -> %q, want %q", d.Obs[i].UpCircuits, got[i], want[i])
		}
	}
}

func churnDays(n int, forecast func(sym, day int) float64) []Day {
	var days []Day
	start := time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC)
	for di := 0; di < n; di++ {
		d := Day{Date: start.AddDate(0, 0, di)}
		for s := 0; s < 10; s++ {
			d.Obs = append(d.Obs, Obs{Sym: int32(s), Forecast: forecast(s, di), FwdRet: 0.01, Sector: "A", Vol: 1})
		}
		days = append(days, d)
	}
	return days
}

func TestChurnIsZeroForAStableBookAndOneForAFreshOne(t *testing.T) {
	stable := Run(churnDays(30, func(s, _ int) float64 { return float64(s) }), nil, "stable", 5)
	if stable.Overall.Churn != 0 {
		t.Errorf("the same top names every day churned %.2f", stable.Overall.Churn)
	}
	// The top two move by two ranks every five days, so today's pair never
	// overlaps the pair a horizon back.
	fresh := Run(churnDays(30, func(s, di int) float64 { return float64((s + 2*(di/5)) % 10) }), nil, "fresh", 5)
	if fresh.Overall.Churn != 1 {
		t.Errorf("a completely new selection each period churned %.2f, want 1", fresh.Overall.Churn)
	}
}

func boot() evidence.Bootstrap { return evidence.Bootstrap{MeanBlock: 10, Reps: 400, Seed: 1} }

func TestDeviationsFindThePlantedSectorAndNothingElse(t *testing.T) {
	dims := []Dimension{
		CategoryDimension("sector", func(o Obs) string { return o.Sector }),
		BooleanDimension("trend", "up", "down", func(o Obs) bool { return o.AboveSMA }),
	}
	res := Run(synthDays(300, 40, "B", 0.01), dims, "planted", 20)
	dev, err := Deviations(res, boot())
	if err != nil {
		t.Fatal(err)
	}
	for _, b := range res.Buckets {
		z := dev.Z[BucketKey(b)]
		switch {
		case b.Dimension == "sector" && b.Label == "B" && math.Abs(z) <= dev.Threshold:
			t.Errorf("the planted sector scores %.1f, under the %.1f threshold", z, dev.Threshold)
		case b.Dimension == "trend" && math.Abs(z) > dev.Threshold:
			t.Errorf("trend bucket %q flagged at %.1f — the effect is not conditional on trend", b.Label, z)
		}
	}
}

func TestAUniformEffectFlagsBucketsNoMoreOftenThanTheLevel(t *testing.T) {
	// A rule that works EVERYWHERE should flag a bucket in about 5% of
	// datasets — the family-wise level — not in most of them. Working is not
	// the same as working unusually somewhere; the first, random-score version
	// of this yardstick flagged half the buckets of a real rule for exactly
	// that reason. One dataset is one draw, so the level is checked over many.
	dims := []Dimension{
		CategoryDimension("sector", func(o Obs) string { return o.Sector }),
		BooleanDimension("trend", "up", "down", func(o Obs) bool { return o.AboveSMA }),
	}
	const datasets = 30
	flagged := 0
	for seed := int64(1); seed <= datasets; seed++ {
		days := synthDaysSeed(seed, 300, 40, "B", 0)
		for di := range days {
			for i := range days[di].Obs {
				o := &days[di].Obs[i]
				o.FwdRet += 0.01 * o.Forecast
			}
		}
		res := Run(days, dims, "everywhere", 20)
		if res.Overall.Edge < 0.005 {
			t.Fatalf("test premise: the uniform effect should show overall, got %.4f", res.Overall.Edge)
		}
		dev, err := Deviations(res, boot())
		if err != nil {
			t.Fatal(err)
		}
		for _, b := range res.Buckets {
			if math.Abs(dev.Z[BucketKey(b)]) > dev.Threshold {
				flagged++
				break
			}
		}
	}
	t.Logf("uniform effect: %d of %d datasets flagged any bucket (nominal 5%%)", flagged, datasets)
	if flagged > 5 {
		t.Errorf("%d of %d datasets flagged a bucket — the scores are inflated, not calibrated to 5%%", flagged, datasets)
	}
}

func TestPersistentWobbleIsNotMistakenForEvidence(t *testing.T) {
	// A bucket whose difference from the universe drifts in long runs with no
	// true mean: blocks must widen its error bar enough that it stays below
	// the threshold, where an i.i.d. reading would call it a large effect.
	n := 600
	all := make([]float64, n)
	daily := make([]float64, n)
	rng := newRng(7)
	level := 0.0
	for i := range daily {
		if i%100 == 0 {
			level = 0.004 * rng.normal() // a new regime every 100 days
		}
		daily[i] = level + 0.001*rng.normal()
	}
	res := Result{Overall: Bucket{daily: all}, Buckets: []Bucket{{Dimension: "d", Label: "drifter", daily: daily, ctrlDaily: make([]float64, n)}}}
	iid, _ := Deviations(res, evidence.Bootstrap{MeanBlock: 1, Reps: 400, Seed: 1})
	blocked, _ := Deviations(res, evidence.Bootstrap{MeanBlock: 100, Reps: 400, Seed: 1})
	zi, zb := math.Abs(iid.Z["d\x00drifter"]), math.Abs(blocked.Z["d\x00drifter"])
	if zb >= zi {
		t.Errorf("blocks did not widen the error bar: |z| %.1f blocked vs %.1f i.i.d.", zb, zi)
	}
}

func TestDayLevelBucketsAreComparedWithOtherDays(t *testing.T) {
	// The rule works only on "hot" days. A whole-day bucket holds every name,
	// so comparing it with the universe that day would always read zero; it
	// must be compared with the other days instead.
	days := synthDaysSeed(3, 300, 40, "B", 0)
	hot := func(d Day) bool { return d.Date.YearDay()%3 == 0 }
	for di := range days {
		if !hot(days[di]) {
			continue
		}
		for i := range days[di].Obs {
			o := &days[di].Obs[i]
			o.FwdRet += 0.01 * o.Forecast
		}
	}
	dims := []Dimension{DayDimension("regime", func(d Day) string {
		if hot(d) {
			return "hot"
		}
		return "cold"
	}, nil)}
	res := Run(days, dims, "regime", 20)
	dev, err := Deviations(res, boot())
	if err != nil {
		t.Fatal(err)
	}
	for _, b := range res.Buckets {
		z, ok := dev.Z[BucketKey(b)]
		if !ok {
			t.Fatalf("day-level bucket %q got no score", b.Label)
		}
		if b.Label == "hot" && z <= dev.Threshold {
			t.Errorf("hot days score %.1f, under the %.1f threshold", z, dev.Threshold)
		}
		if b.Label == "cold" && z >= -dev.Threshold {
			t.Errorf("cold days score %.1f; they should stand out on the low side", z)
		}
	}
}

func TestSparseBucketsGetNoScoreAndLeaveTheThresholdAlone(t *testing.T) {
	// Sector B's names fire every day; sector A's on only five days — the
	// Weinstein breakout shape that once scored -1.6e14 and marked six sectors.
	days := synthDaysSeed(7, 300, 30, "", 0)
	for di := range days {
		for i := range days[di].Obs {
			o := &days[di].Obs[i]
			o.Selected = o.Forecast > 1 && (o.Sector == "B" || (o.Sector == "A" && di < 5))
		}
	}
	res := RunEvent(days, []Dimension{CategoryDimension("sector", func(o Obs) string { return o.Sector })}, "sparse", 20)
	dev, err := Deviations(res, boot())
	if err != nil {
		t.Fatal(err)
	}
	if z, ok := dev.Z[BucketKey(Bucket{Dimension: "sector", Label: "A"})]; ok {
		t.Fatalf("a bucket seen on 5 days was scored %v; it cannot carry a block bootstrap", z)
	}
	if _, ok := dev.Z[BucketKey(Bucket{Dimension: "sector", Label: "B"})]; !ok {
		t.Fatal("the bucket that fired every day lost its score")
	}
	if math.IsInf(dev.Threshold, 0) || dev.Threshold > 10 {
		t.Fatalf("threshold %v: a sparse bucket leaked into the family", dev.Threshold)
	}
}
