package core

import (
	"math"
	"testing"
	"time"
)

func nan() float64 { return math.NaN() }

// sameFloat treats NaN as equal to NaN — these tests assert WHERE a series is
// undefined as carefully as they assert its values.
func sameFloat(a, b float64) bool {
	if math.IsNaN(a) && math.IsNaN(b) {
		return true
	}
	return math.Abs(a-b) < 1e-9
}

func mkDays(start time.Time, n int) []time.Time {
	out := make([]time.Time, n)
	for i := range out {
		out[i] = start.AddDate(0, 0, i)
	}
	return out
}

func TestSMA_ConstantSeries(t *testing.T) {
	n := 40
	vals := make([]float64, n)
	for i := range vals {
		vals[i] = 7.0
	}
	s := New(mkDays(time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC), n), vals)
	sma := SMA(s, 10)
	if got := sma.Values[n-1]; math.Abs(got-7.0) > 1e-9 {
		t.Fatalf("SMA of constant = %v, want 7", got)
	}
	if !math.IsNaN(sma.Values[8]) {
		t.Fatal("SMA should be NaN during warm-up (window=10, index 8 has only 9 obs)")
	}
	if math.IsNaN(sma.Values[9]) {
		t.Fatal("SMA should be defined once the window is full (index 9, 10 obs)")
	}
}

func TestSMA_SingleNaNPoisonsWindow(t *testing.T) {
	vals := []float64{1, 2, 3, math.NaN(), 5, 6}
	s := New(mkDays(time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC), 6), vals)
	sma := SMA(s, 3)
	// window at index 3 = [1,2,3] -> defined; index 4 = [2,3,NaN] -> NaN;
	// index 5 = [3,NaN,5] -> NaN.
	if math.IsNaN(sma.Values[2]) {
		t.Fatal("index 2 window=[1,2,3] should be defined")
	}
	if !math.IsNaN(sma.Values[3]) {
		t.Fatal("index 3 window includes the NaN itself, should be NaN")
	}
	if !math.IsNaN(sma.Values[4]) || !math.IsNaN(sma.Values[5]) {
		t.Fatal("windows still containing the NaN should stay NaN, not silently skip it")
	}
}

func TestResampleWeeklyLast_TakesLastObservationPerISOWeek(t *testing.T) {
	// Mon 2024-01-01 .. Fri 2024-01-12: two full ISO weeks (1 and 2).
	start := time.Date(2024, 1, 1, 0, 0, 0, 0, time.UTC) // Monday
	var times []time.Time
	var vals []float64
	for i := 0; i < 10; i++ {
		d := start.AddDate(0, 0, i)
		if wd := d.Weekday(); wd == time.Saturday || wd == time.Sunday {
			continue
		}
		times = append(times, d)
		vals = append(vals, float64(i))
	}
	s := New(times, vals)
	w := ResampleWeeklyLast(s)
	if w.Len() != 2 {
		t.Fatalf("expected 2 weekly points, got %d", w.Len())
	}
	// Last weekday of week 1 (Fri Jan 5) has index value 4; week 2's last
	// weekday present (Fri Jan 12) has index value 9.
	if w.Values[0] != 4 {
		t.Fatalf("week 1 last value = %v, want 4 (Friday close)", w.Values[0])
	}
	if w.Values[1] != 9 {
		t.Fatalf("week 2 last value = %v, want 9 (Friday close)", w.Values[1])
	}
}

func TestResampleWeeklyLast_HolidayShortenedWeekUsesActualLastDay(t *testing.T) {
	// Only Mon/Tue/Wed traded this week (holiday Thu/Fri) -- last value must
	// be Wednesday's, not an assumed Friday.
	start := time.Date(2024, 1, 1, 0, 0, 0, 0, time.UTC) // Monday
	s := New([]time.Time{start, start.AddDate(0, 0, 1), start.AddDate(0, 0, 2)}, []float64{10, 11, 12})
	w := ResampleWeeklyLast(s)
	if w.Len() != 1 || w.Values[0] != 12 {
		t.Fatalf("got %v, want single point = 12 (Wednesday, the actual last trading day)", w.Values)
	}
	if !w.Times[0].Equal(start.AddDate(0, 0, 2)) {
		t.Fatalf("stamped date = %v, want Wednesday %v", w.Times[0], start.AddDate(0, 0, 2))
	}
}

func TestResampleWeeklySum_SumsWithinWeekTreatingNaNAsZero(t *testing.T) {
	start := time.Date(2024, 1, 1, 0, 0, 0, 0, time.UTC) // Monday
	times := []time.Time{start, start.AddDate(0, 0, 1), start.AddDate(0, 0, 2)}
	vals := []float64{100, math.NaN(), 300}
	s := New(times, vals)
	w := ResampleWeeklySum(s)
	if w.Len() != 1 {
		t.Fatalf("expected 1 weekly point, got %d", w.Len())
	}
	if w.Values[0] != 400 {
		t.Fatalf("weekly sum = %v, want 400 (NaN day contributes 0, not unknown)", w.Values[0])
	}
}

func TestResampleWeeklySum_AllNaNWeekIsNaN(t *testing.T) {
	start := time.Date(2024, 1, 1, 0, 0, 0, 0, time.UTC)
	s := New([]time.Time{start, start.AddDate(0, 0, 1)}, []float64{math.NaN(), math.NaN()})
	w := ResampleWeeklySum(s)
	if w.Len() != 1 || !math.IsNaN(w.Values[0]) {
		t.Fatalf("all-NaN week should resample to NaN, got %v", w.Values)
	}
}

func TestAlignByTime_SameLengthDifferentDatesDoesNotSilentlyMispair(t *testing.T) {
	// The bug this guards against: two series independently filtered can end
	// up the SAME length but covering DIFFERENT dates. A naive .Len() check
	// would treat them as alignable; AlignByTime must not just zip them.
	start := time.Date(2024, 1, 1, 0, 0, 0, 0, time.UTC)
	base := New([]time.Time{start, start.AddDate(0, 0, 1), start.AddDate(0, 0, 2)}, []float64{10, 20, 30})
	// other has 3 points too, but shifted by one day (day 1..3, not 0..2) --
	// same length, no date overlap with base's first day.
	other := New([]time.Time{start.AddDate(0, 0, 1), start.AddDate(0, 0, 2), start.AddDate(0, 0, 3)}, []float64{100, 200, 300})

	aligned := AlignByTime(base, other)
	if aligned.Len() != base.Len() {
		t.Fatalf("aligned length = %d, want %d (base's length)", aligned.Len(), base.Len())
	}
	if !math.IsNaN(aligned.Values[0]) {
		t.Fatalf("base's day 0 has no match in other, want NaN, got %v", aligned.Values[0])
	}
	if aligned.Values[1] != 100 || aligned.Values[2] != 200 {
		t.Fatalf("dates 1,2 should pull other's values at those exact dates, got %v", aligned.Values)
	}
}

func TestAlignByTime_ExactMatchIsIdentity(t *testing.T) {
	days := mkDays(time.Date(2024, 1, 1, 0, 0, 0, 0, time.UTC), 5)
	base := New(days, []float64{1, 2, 3, 4, 5})
	other := New(days, []float64{10, 20, 30, 40, 50})
	aligned := AlignByTime(base, other)
	for i, v := range aligned.Values {
		if v != other.Values[i] {
			t.Fatalf("index %d: got %v, want %v (exact date match)", i, v, other.Values[i])
		}
	}
}

func TestRollingMaxMin_TrackTheWindowNotTheWholeHistory(t *testing.T) {
	// A spike must leave the max as soon as it falls out of the window; the
	// bug this catches is a running max that never forgets.
	vals := []float64{1, 9, 2, 3, 4, 5}
	s := New(mkDays(time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC), len(vals)), vals)
	max := RollingMax(s, 3)
	min := RollingMin(s, 3)
	wantMax := []float64{nan(), nan(), 9, 9, 4, 5}
	wantMin := []float64{nan(), nan(), 1, 2, 2, 3}
	for i := range vals {
		if !sameFloat(max.Values[i], wantMax[i]) {
			t.Errorf("max[%d] = %v, want %v", i, max.Values[i], wantMax[i])
		}
		if !sameFloat(min.Values[i], wantMin[i]) {
			t.Errorf("min[%d] = %v, want %v", i, min.Values[i], wantMin[i])
		}
	}
}

func TestRollingMax_SingleNaNPoisonsWindow(t *testing.T) {
	// Same strictness as SMA: a range computed off a partially populated
	// window is a different statistic, and callers divide by it.
	vals := []float64{1, 2, math.NaN(), 4, 5, 6}
	s := New(mkDays(time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC), len(vals)), vals)
	got := RollingMax(s, 3)
	want := []float64{nan(), nan(), nan(), nan(), nan(), 6}
	for i := range vals {
		if !sameFloat(got.Values[i], want[i]) {
			t.Errorf("max[%d] = %v, want %v", i, got.Values[i], want[i])
		}
	}
}
