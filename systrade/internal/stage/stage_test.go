package stage

import (
	"math"
	"testing"
	"time"

	"github.com/ranedk/systrader/internal/core"
)

func weeklyTimes(n int) []time.Time {
	start := time.Date(2020, 1, 3, 0, 0, 0, 0, time.UTC) // a Friday
	out := make([]time.Time, n)
	for i := range out {
		out[i] = start.AddDate(0, 0, 7*i)
	}
	return out
}

func series(vals []float64) core.Series {
	return core.New(weeklyTimes(len(vals)), vals)
}

func TestClassify_WarmUpIsUnknown(t *testing.T) {
	vals := make([]float64, 40)
	for i := range vals {
		vals[i] = 100 + float64(i)
	}
	cs := Classify(series(vals), nil)
	if cs[0].Stage != StageUnknown {
		t.Fatalf("index 0 should be StageUnknown, got %v", cs[0].Stage)
	}
	if cs[MAWindowWeeks+SlopeWindowWeeks-2].Stage != StageUnknown {
		t.Fatalf("index %d (one short of MA+slope warm-up) should still be StageUnknown", MAWindowWeeks+SlopeWindowWeeks-2)
	}
	if cs[MAWindowWeeks+SlopeWindowWeeks-1].Stage == StageUnknown {
		t.Fatalf("index %d (MA+slope warm-up complete) should have a real stage", MAWindowWeeks+SlopeWindowWeeks-1)
	}
}

func TestClassify_MonotonicRiseEndsStage2Advancing(t *testing.T) {
	n := 60
	vals := make([]float64, n)
	for i := range vals {
		vals[i] = 100 + 2*float64(i)
	}
	cs := Classify(series(vals), nil)
	last := cs[n-1]
	if last.Stage != Stage2Advancing {
		t.Fatalf("monotonic rise should end in Stage2Advancing, got %v (close=%.1f ma=%.1f slope=%.2f%%)",
			last.Stage, last.Close, last.MA30, last.MASlopePct)
	}
	if !(last.Close > last.MA30) {
		t.Fatal("Stage2Advancing requires close > MA30")
	}
	if !(last.MASlopePct > FlatThresholdPct) {
		t.Fatal("Stage2Advancing requires MA slope clearly positive")
	}
}

func TestClassify_MonotonicFallEndsStage4Declining(t *testing.T) {
	n := 60
	vals := make([]float64, n)
	for i := range vals {
		vals[i] = 300 - 2*float64(i)
	}
	cs := Classify(series(vals), nil)
	last := cs[n-1]
	if last.Stage != Stage4Declining {
		t.Fatalf("monotonic fall should end in Stage4Declining, got %v (close=%.1f ma=%.1f slope=%.2f%%)",
			last.Stage, last.Close, last.MA30, last.MASlopePct)
	}
}

func TestClassify_FlatBeforeAnyTrendIsStage1Basing(t *testing.T) {
	n := 45
	vals := make([]float64, n)
	for i := range vals {
		vals[i] = 100 // perfectly flat: MA constant, slope exactly 0
	}
	cs := Classify(series(vals), nil)
	last := cs[n-1]
	if last.Stage != Stage1Basing {
		t.Fatalf("flat series with no prior trend should default to Stage1Basing, got %v", last.Stage)
	}
}

func TestClassify_FlatAfterAdvanceIsStage3Topping(t *testing.T) {
	rise := 60
	flat := 45
	n := rise + flat
	vals := make([]float64, n)
	peak := 100 + 3*float64(rise-1)
	for i := 0; i < rise; i++ {
		vals[i] = 100 + 3*float64(i)
	}
	for i := rise; i < n; i++ {
		vals[i] = peak // flatten out at the top, deep enough for the MA to catch up
	}
	cs := Classify(series(vals), nil)
	mid := cs[rise-1]
	if mid.Stage != Stage2Advancing {
		t.Fatalf("end of the rise (index %d) should read Stage2Advancing, got %v", rise-1, mid.Stage)
	}
	last := cs[n-1]
	if last.Stage != Stage3Topping {
		t.Fatalf("flat period well after an advance should read Stage3Topping (not Stage1), got %v (slope=%.2f%%)",
			last.Stage, last.MASlopePct)
	}
}

func TestClassify_FlatAfterDeclineIsStage1Basing(t *testing.T) {
	fall := 60
	flat := 45
	n := fall + flat
	vals := make([]float64, n)
	trough := 300 - 3*float64(fall-1)
	for i := 0; i < fall; i++ {
		vals[i] = 300 - 3*float64(i)
	}
	for i := fall; i < n; i++ {
		vals[i] = trough
	}
	cs := Classify(series(vals), nil)
	mid := cs[fall-1]
	if mid.Stage != Stage4Declining {
		t.Fatalf("end of the decline (index %d) should read Stage4Declining, got %v", fall-1, mid.Stage)
	}
	last := cs[n-1]
	if last.Stage != Stage1Basing {
		t.Fatalf("flat period well after a decline should read Stage1Basing (not Stage3), got %v (slope=%.2f%%)",
			last.Stage, last.MASlopePct)
	}
}

func TestClassify_NilVolumeIsSafeAndDoesNotAffectStage(t *testing.T) {
	n := 60
	vals := make([]float64, n)
	for i := range vals {
		vals[i] = 100 + 2*float64(i)
	}
	withoutVol := Classify(series(vals), nil)
	vol := series(make([]float64, n)) // zeros, deliberately not realistic
	withVol := Classify(series(vals), &vol)

	for _, c := range withoutVol {
		if !math.IsNaN(c.VolumeRatio) {
			t.Fatalf("VolumeRatio should be NaN with nil volume input, got %v at %v", c.VolumeRatio, c.Time)
		}
	}
	for i := range withoutVol {
		if withoutVol[i].Stage != withVol[i].Stage {
			t.Fatalf("presence of a volume series must not change the stage classification at index %d: %v vs %v",
				i, withoutVol[i].Stage, withVol[i].Stage)
		}
	}
}

func TestClassify_MisalignedVolumeDatesDoNotSilentlyMispair(t *testing.T) {
	// BUG FOUND LIVE 2026-08-21 (code review): a volume series with the SAME
	// length as the close series but DIFFERENT dates (exactly what two
	// independently-filtered DB queries can produce) used to get paired by
	// index anyway. It must now align by actual date via core.AlignByTime.
	n := 60
	vals := make([]float64, n)
	for i := range vals {
		vals[i] = 100 + 2*float64(i)
	}
	closeSeries := series(vals)

	// Volume series: same length, but shifted 10 weeks later, with a huge
	// distinctive value -- if this leaked into VolumeRatio via positional
	// pairing, it would show up misaligned in the overlapping region.
	shiftedTimes := make([]time.Time, n)
	volVals := make([]float64, n)
	for i := range shiftedTimes {
		shiftedTimes[i] = weeklyTimes(n)[0].AddDate(0, 0, 7*(i+10))
		volVals[i] = 999999
	}
	shiftedVol := core.New(shiftedTimes, volVals)

	withoutVol := Classify(closeSeries, nil)
	withMisalignedVol := Classify(closeSeries, &shiftedVol)

	for i := range withoutVol {
		if withoutVol[i].Stage != withMisalignedVol[i].Stage {
			t.Fatalf("misaligned volume must not change the stage classification at index %d: %v vs %v",
				i, withoutVol[i].Stage, withMisalignedVol[i].Stage)
		}
	}
	// The first 10 weeks of closeSeries have no matching date in the shifted
	// volume series at all -- VolumeRatio there must be NaN, not the huge
	// shifted value that a positional pairing would have leaked in.
	for i := 0; i < 10; i++ {
		if !math.IsNaN(withMisalignedVol[i].VolumeRatio) {
			t.Fatalf("index %d has no date-matching volume, want NaN VolumeRatio, got %v",
				i, withMisalignedVol[i].VolumeRatio)
		}
	}
}

func TestLatest(t *testing.T) {
	if _, ok := Latest(nil); ok {
		t.Fatal("Latest on empty slice should return ok=false")
	}
	cs := []Classification{{Stage: Stage1Basing}, {Stage: Stage2Advancing}}
	got, ok := Latest(cs)
	if !ok || got.Stage != Stage2Advancing {
		t.Fatalf("Latest should return the last element, got %+v ok=%v", got, ok)
	}
}

func TestStageString_CoversEveryValue(t *testing.T) {
	for _, s := range []Stage{StageUnknown, Stage1Basing, Stage2Advancing, Stage3Topping, Stage4Declining} {
		if s.String() == "" {
			t.Fatalf("Stage(%d).String() is empty", s)
		}
	}
}
