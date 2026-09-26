package main

import (
	"math"
	"testing"
	"time"

	"github.com/ranedk/systrader/internal/bars"
	"github.com/ranedk/systrader/internal/stage"
)

func TestWeeklyViewUsesOnlyCompletedWeeks(t *testing.T) {
	// Two weeks of Monday-Friday bars, the second a holiday-shortened four days.
	start := time.Date(2024, 1, 1, 0, 0, 0, 0, time.UTC) // a Monday
	var b []bars.Bar
	for d := 0; d < 12; d++ {
		day := start.AddDate(0, 0, d)
		if day.Weekday() == time.Saturday || day.Weekday() == time.Sunday || d == 11 {
			continue // d == 11 is Friday of week two: a holiday
		}
		b = append(b, bars.Bar{Date: day, Open: 1, Close: 1, Vol: 1})
	}
	w := weeklyView(b)
	if len(w.end) != 2 || w.end[0] != 4 || w.end[1] != 8 {
		t.Fatalf("week ends %v, want [4 8] (Friday, then the holiday week's Thursday)", w.end)
	}
	got := w.daily(len(b), []float64{10, 20})
	for i, want := range []float64{math.NaN(), math.NaN(), math.NaN(), math.NaN(), 10, 10, 10, 10, 20} {
		if !(got[i] == want || (math.IsNaN(got[i]) && math.IsNaN(want))) {
			t.Fatalf("day %d carries %v, want %v: a day must use the last week completed on or before it", i, got[i], want)
		}
	}
}

func cl(s stage.Stage, close, ma, vol float64) stage.Classification {
	return stage.Classification{Stage: s, Close: close, MA30: ma, VolumeRatio: vol}
}

func TestHoldStage2RidesStage3UntilTheBreakBelowTheAverage(t *testing.T) {
	cls := []stage.Classification{
		cl(stage.StageUnknown, 10, math.NaN(), math.NaN()),
		cl(stage.Stage1Basing, 10, 10, 1),
		cl(stage.Stage2Advancing, 12, 10, 1), // enter
		cl(stage.Stage3Topping, 11, 10.5, 1), // topping, still above the average: hold
		cl(stage.Stage3Topping, 10, 10.6, 1), // weekly close below it: out
		cl(stage.Stage1Basing, 10, 10.5, 1),
	}
	got := holdPositions(cls, enterStage2(cls))
	want := []float64{math.NaN(), 0, 1, 1, 0, 0}
	for i := range want {
		if !(got[i] == want[i] || (math.IsNaN(got[i]) && math.IsNaN(want[i]))) {
			t.Fatalf("week %d: %v, want %v (all %v)", i, got[i], want[i], got)
		}
	}
}

func TestBreakoutNeedsABaseVolumeAndStrength(t *testing.T) {
	rsUp := []float64{0.1, 0.1}
	cases := []struct {
		name string
		prev stage.Stage
		vol  float64
		rs   []float64
		mkt  func(time.Time) bool
		want bool
	}{
		{"a clean breakout", stage.Stage1Basing, 2.5, rsUp, nil, true},
		{"out of a decline, not a base", stage.Stage4Declining, 2.5, rsUp, nil, false},
		{"on thin volume", stage.Stage1Basing, 1.5, rsUp, nil, false},
		{"with the stock lagging the market", stage.Stage1Basing, 2.5, []float64{0.1, -0.05}, nil, false},
		{"in a market that is not in Stage 2", stage.Stage1Basing, 2.5, rsUp, func(time.Time) bool { return false }, false},
	}
	for _, c := range cases {
		cls := []stage.Classification{cl(c.prev, 10, 10, 1), cl(stage.Stage2Advancing, 12, 10, c.vol)}
		if got := enterBreakout(cls, c.rs, c.mkt)(1); got != c.want {
			t.Errorf("%s: entry %v, want %v", c.name, got, c.want)
		}
	}
}

func TestMansfieldAgainstItsOwnYearlyAverage(t *testing.T) {
	var cls []stage.Classification
	base := time.Date(2020, 1, 3, 0, 0, 0, 0, time.UTC)
	for k := 0; k < 53; k++ {
		c := 50.0
		if k == 52 {
			c = 60 // the stock jumps in the last week; the market does not
		}
		cls = append(cls, stage.Classification{Time: base.AddDate(0, 0, 7*k), Close: c})
	}
	rs := mansfield(cls, func(time.Time) float64 { return 100 })
	if !math.IsNaN(rs[50]) {
		t.Fatalf("defined before 52 weeks: %v", rs[50])
	}
	if math.Abs(rs[51]) > 1e-12 {
		t.Fatalf("a stock moving with the market reads %v, want 0", rs[51])
	}
	// ratio 0.6 against a 52-week average of (51*0.5 + 0.6)/52.
	want := 0.6/((51*0.5+0.6)/52) - 1
	if math.Abs(rs[52]-want) > 1e-12 {
		t.Fatalf("rs %v, want %v", rs[52], want)
	}
}

func TestBreakoutEventFiresOnlyOnTheBreakoutWeeksLastDay(t *testing.T) {
	// 60 flat weeks (a base), then one week that jumps 50% on 30x volume.
	start := time.Date(2020, 1, 6, 0, 0, 0, 0, time.UTC) // a Monday
	var b []bars.Bar
	ret := map[time.Time]float64{}
	for wk := 0; wk < 62; wk++ {
		for d := 0; d < 5; d++ {
			day := start.AddDate(0, 0, 7*wk+d)
			px, vol := 100.0, 1.0
			if wk >= 60 {
				px, vol = 150, 30
			}
			b = append(b, bars.Bar{Date: day, Open: px, Close: px, Vol: vol})
			ret[day] = 0 // a flat market
		}
	}
	fired, extra := weinsteinEvent("breakout", newMarket(ret))(b)
	var days []int
	for i, f := range fired {
		if f == 1 {
			days = append(days, i)
		}
	}
	if len(days) != 1 || days[0] != 60*5+4 {
		t.Fatalf("fired on %v, want only day %d — the Friday the breakout week completed", days, 60*5+4)
	}
	if extra[days[0]][xWkVolRatio] < breakoutVolume || extra[days[0]][xMansfield] <= 0 {
		t.Fatalf("breakout day extras %v", extra[days[0]][:4])
	}
	if fired[60*5+3] != 0 {
		t.Fatal("the Thursday of the breakout week must not know Friday's close")
	}
	stg, _ := weinsteinEvent("stage2", newMarket(ret))(b)
	if stg[0] != -1 {
		t.Fatal("a name whose stage is unknown must be left out, not counted as 'not in Stage 2'")
	}
	if stg[60*5+4] != 1 || stg[60*5+3] == 1 {
		t.Fatalf("stage2 membership %d on the breakout Friday, %d the day before", stg[60*5+4], stg[60*5+3])
	}
}

func TestFaithfulBreakoutIsWeinsteinsBuyPoint(t *testing.T) {
	// 30 weeks of a base (closes at most 100, the MA flat), then the week under test.
	mk := func(close, ma, slope, prevSlope, vol, rs float64) ([]stage.Classification, []float64) {
		cls := make([]stage.Classification, 31)
		r := make([]float64, 31)
		for k := 0; k < 30; k++ {
			cls[k] = stage.Classification{Stage: stage.Stage1Basing, Close: 95 + float64(k%6), MA30: 98, MASlopePct: 0.2}
		}
		cls[29].MASlopePct = prevSlope
		cls[30] = stage.Classification{Stage: stage.Stage3Topping, Close: close, MA30: ma, MASlopePct: slope, VolumeRatio: vol}
		r[30] = rs
		return cls, r
	}
	cases := []struct {
		name                                 string
		close, ma, slope, prevSlope, vol, rs float64
		want                                 bool
	}{
		{"a clean breakout from a flat base", 110, 99, 0.8, 0.3, 2.5, 0.05, true},
		{"the MA already rising: fires even though our Stage 2 would not", 110, 99, 0.8, 0.9, 2.5, 0.05, true},
		{"not above the base's highest close", 100, 99, 0.8, 0.3, 2.5, 0.05, false},
		{"out of a rising MA, not a base", 110, 99, 2.0, 1.5, 2.5, 0.05, false},
		{"into a falling MA", 110, 99, -1.5, 0.3, 2.5, 0.05, false},
		{"on thin volume", 110, 99, 0.8, 0.3, 1.5, 0.05, false},
		{"lagging the market", 110, 99, 0.8, 0.3, 2.5, -0.01, false},
		{"still below the MA", 110, 115, 0.8, 0.3, 2.5, 0.05, false},
	}
	for _, c := range cases {
		cls, rs := mk(c.close, c.ma, c.slope, c.prevSlope, c.vol, c.rs)
		if got := enterFaithful(cls, rs)(30); got != c.want {
			t.Errorf("%s: %v, want %v", c.name, got, c.want)
		}
	}
	cls, rs := mk(110, 99, 0.8, 0.3, 2.5, 0.05)
	if enterFaithful(cls, rs)(29) {
		t.Error("fewer than 30 prior weeks: no base to break out of")
	}
}

func TestTopFifthCut(t *testing.T) {
	if c := topFifthCut([]float64{5, 1, 4, 2, 3, 10, 9, 8, 7, 6}); c != 9 {
		t.Fatalf("cut %v, want 9 (the two largest of ten are cut)", c)
	}
	if !math.IsInf(topFifthCut([]float64{1, 2, 3, 4}), 1) {
		t.Fatal("four values have no top fifth: cut nothing")
	}
}
