package rules

import (
	"math"
	"math/rand"
	"testing"
	"time"

	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/data"
)

// synth builds a deterministic instrument: a random walk with drift, long
// enough to warm up the slowest rule in the library.
func synth(n int, seed int64) (*data.Instrument, core.Series) {
	rng := rand.New(rand.NewSource(seed))
	times := make([]time.Time, n)
	px := make([]float64, n)
	p := 100.0
	start := time.Date(2010, 1, 1, 0, 0, 0, 0, time.UTC)
	for i := range px {
		p *= 1 + 0.0003 + 0.015*rng.NormFloat64()
		times[i], px[i] = start.AddDate(0, 0, i), p
	}
	prices := core.New(times, px)
	inst := &data.Instrument{Meta: data.Meta{Symbol: "SYNTH", PointValue: 1, Block: 1}, Prices: prices}
	return inst, core.PriceUnitVol(prices, 36, 10)
}

func TestLibraryRulesAreLegalCitizens(t *testing.T) {
	inst, vol := synth(2000, 1)
	for _, r := range Library() {
		if err := Validate(r); err != nil {
			t.Errorf("%s: %v", r.Name(), err)
		}
		// A missing frozen scalar panics by design; catch it here rather than
		// in the middle of a backtest.
		if s := r.Scalar(); !(s > 0) {
			t.Errorf("%s: scalar %v is not positive", r.Name(), s)
		}
		f := Forecast(r, inst, vol)
		seen := 0
		for _, v := range f.Values {
			if math.IsNaN(v) {
				continue
			}
			seen++
			if math.Abs(v) > ForecastCap+1e-9 {
				t.Fatalf("%s: forecast %v breaches the cap", r.Name(), v)
			}
		}
		if seen == 0 {
			t.Errorf("%s: produced no finite forecast over 2000 bars", r.Name())
		}
	}
}

// TestNoLookahead is the property that kills most silent backtest frauds:
// rewrite the future and the past must not move.
func TestNoLookahead(t *testing.T) {
	const n, cut = 2000, 1500
	inst, vol := synth(n, 7)

	mutated := &data.Instrument{Meta: inst.Meta, Prices: core.New(inst.Prices.Times, append([]float64(nil), inst.Prices.Values...))}
	for i := cut; i < n; i++ {
		mutated.Prices.Values[i] *= 3 // an implausible future, deliberately
	}
	mutVol := core.PriceUnitVol(mutated.Prices, 36, 10)

	for _, r := range Library() {
		base := Forecast(r, inst, vol)
		after := Forecast(r, mutated, mutVol)
		for i := 0; i < cut; i++ {
			a, b := base.Values[i], after.Values[i]
			if math.IsNaN(a) && math.IsNaN(b) {
				continue
			}
			if math.Abs(a-b) > 1e-12 {
				t.Fatalf("%s: bar %d changed when the future was rewritten (%v -> %v)", r.Name(), i, a, b)
			}
		}
	}
}

func TestBreakoutReadsPositionInRange(t *testing.T) {
	// A price that rises in a straight line sits at the top of every trailing
	// range, so the raw value must pin near +0.5; the mirror fall pins at -0.5.
	const n = 400
	times := make([]time.Time, n)
	up := make([]float64, n)
	down := make([]float64, n)
	start := time.Date(2010, 1, 1, 0, 0, 0, 0, time.UTC)
	for i := 0; i < n; i++ {
		times[i] = start.AddDate(0, 0, i)
		up[i] = 100 + float64(i)
		down[i] = 500 - float64(i)
	}
	b := Breakout{N: 40}
	upInst := &data.Instrument{Prices: core.New(times, up)}
	downInst := &data.Instrument{Prices: core.New(times, down)}
	upRaw := b.Raw(upInst, core.Series{})
	downRaw := b.Raw(downInst, core.Series{})
	last := n - 1
	if got := upRaw.Values[last]; math.Abs(got-0.5) > 0.02 {
		t.Errorf("rising line: raw breakout = %v, want ~+0.5", got)
	}
	if got := downRaw.Values[last]; math.Abs(got+0.5) > 0.02 {
		t.Errorf("falling line: raw breakout = %v, want ~-0.5", got)
	}
	// Warm-up must be NaN, not a partially-populated range.
	if !math.IsNaN(upRaw.Values[b.N-2]) {
		t.Errorf("breakout emitted a value at bar %d, before its %d-bar window filled", b.N-2, b.N)
	}
}

func TestMeanReversionIsContrarian(t *testing.T) {
	inst, vol := synth(2000, 3)
	m := MeanReversion{Window: 512}
	avg := core.SMA(inst.Prices, m.Window)
	raw := m.Raw(inst, vol)
	checked := 0
	for i := range raw.Values {
		v, a, p := raw.Values[i], avg.Values[i], inst.Prices.Values[i]
		if math.IsNaN(v) || math.IsNaN(a) {
			continue
		}
		checked++
		if p > a && v >= 0 {
			t.Fatalf("bar %d: price %v above its average %v but forecast %v is not negative", i, p, a, v)
		}
		if p < a && v <= 0 {
			t.Fatalf("bar %d: price %v below its average %v but forecast %v is not positive", i, p, a, v)
		}
	}
	if checked == 0 {
		t.Fatal("no bars checked — the test proved nothing")
	}
}

// TestAccelerationIgnoresASteadyTrend is the whole reason acceleration is in
// the library: a trend running at a constant rate is old news, and the rule
// must say so even while EWMAC is at full forecast.
//
// Volatility is passed in as a constant on purpose. A rule's Raw takes vol as
// an argument precisely so this kind of property can be tested without the
// estimator in the way: on a noiseless ramp the ESTIMATED vol is near zero, so
// EWMAC's raw value is enormous and its ordinary wobble swamps everything —
// which is a fact about dividing by a tiny denominator, not about the rule.
func TestAccelerationIgnoresASteadyTrend(t *testing.T) {
	const n, slope = 1000, 0.5
	times := make([]time.Time, n)
	px := make([]float64, n)
	constVol := make([]float64, n)
	start := time.Date(2010, 1, 1, 0, 0, 0, 0, time.UTC)
	for i := range px {
		px[i] = 100 + slope*float64(i)
		times[i] = start.AddDate(0, 0, i)
		constVol[i] = slope // the ramp's true daily move
	}
	prices := core.New(times, px)
	inst := &data.Instrument{Prices: prices}
	vol := core.New(times, constVol)

	trend := Forecast(EWMAC{Fast: 32}, inst, vol)
	accel := Forecast(Acceleration{Fast: 32}, inst, vol)
	last := n - 1
	if math.Abs(trend.Values[last]) < 15 {
		t.Fatalf("setup wrong: EWMAC should be near its cap on a steady ramp, got %v", trend.Values[last])
	}
	if math.Abs(accel.Values[last]) > 0.5 {
		t.Errorf("acceleration = %v on a constant-rate trend, want ~0", accel.Values[last])
	}
}

// TestAccelerationFiresOnAChangeOfRate is the other half: when the rate of the
// trend doubles, acceleration must respond even though EWMAC was already long.
func TestAccelerationFiresOnAChangeOfRate(t *testing.T) {
	const n, kink = 1000, 700
	times := make([]time.Time, n)
	px := make([]float64, n)
	constVol := make([]float64, n)
	start := time.Date(2010, 1, 1, 0, 0, 0, 0, time.UTC)
	p := 100.0
	for i := range px {
		if i >= kink {
			p += 2.0
		} else {
			p += 0.5
		}
		px[i], times[i], constVol[i] = p, start.AddDate(0, 0, i), 0.5
	}
	inst := &data.Instrument{Prices: core.New(times, px)}
	vol := core.New(times, constVol)

	accel := Forecast(Acceleration{Fast: 32}, inst, vol)
	before, after := accel.Values[kink-1], accel.Values[kink+40]
	if math.Abs(before) > 0.5 {
		t.Fatalf("setup wrong: acceleration should be ~0 before the kink, got %v", before)
	}
	if after <= 5 {
		t.Errorf("acceleration = %v after the trend rate quadrupled, want a strong positive", after)
	}
}
