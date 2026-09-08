package explore

import (
	"math"
	"testing"
	"time"
)

// synthDays builds a universe where the rule's forecast predicts the forward
// return ONLY inside one sector, and is pure noise everywhere else. A slice
// explorer that cannot find a planted effect is useless; one that finds it
// everywhere is worse.
func synthDays(nDays, perSector int, liveSector string, edge float64) []Day {
	sectors := []string{"A", "B", "C", "D"}
	rng := newRng(42)
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
