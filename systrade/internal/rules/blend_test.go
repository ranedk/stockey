package rules

import (
	"math"
	"testing"
	"time"

	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/data"
)

// fixed is a rule that emits one value everywhere, already on the forecast
// scale.
type fixed struct {
	v    float64
	name string
}

func (f fixed) Name() string    { return f.name }
func (f fixed) Scalar() float64 { return 1 }
func (fixed) Story() string {
	return "A test rule standing in for any component with a real mechanism behind it."
}
func (f fixed) Raw(inst *data.Instrument, _ core.Series) core.Series {
	out := make([]float64, inst.Prices.Len())
	for i := range out {
		out[i] = f.v
	}
	return core.New(inst.Prices.Times, out)
}

var oneDay = []time.Time{time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)}

func unitVol() core.Series { return core.New(oneDay, []float64{1}) }

func instrument(longOnly bool) *data.Instrument {
	return &data.Instrument{Meta: data.Meta{Symbol: "X", LongOnly: longOnly}, Prices: core.New(oneDay, []float64{100})}
}

func TestBlendIsTheWeightedAverageTimesFDM(t *testing.T) {
	b := Blend{Label: "b", Rules: []Rule{fixed{12, "a"}, fixed{6, "b"}}, Weights: []float64{0.5, 0.5}, FDM: 1.2}
	got := Forecast(b, instrument(false), unitVol()).Values[0]
	if math.Abs(got-10.8) > 1e-12 {
		t.Errorf("blend = %v, want (12+6)/2 x 1.2 = 10.8", got)
	}
}

func TestBlendClipsOnceAfterCombiningNotBefore(t *testing.T) {
	// +12 and two -15s: two-sided the blend is negative, so a long-only book
	// holds none of it. Clipping each at zero first would have made it +4.
	b := Blend{Label: "b", Rules: []Rule{fixed{12, "f"}, fixed{-15, "m"}, fixed{-15, "s"}},
		Weights: []float64{0.42, 0.16, 0.42}, FDM: 1.1}
	if got := Forecast(b, instrument(true), unitVol()).Values[0]; got != 0 {
		t.Errorf("long-only blend = %v, want 0", got)
	}
	two := Forecast(b, instrument(false), unitVol()).Values[0]
	want := (0.42*12 + 0.16*-15 + 0.42*-15) * 1.1
	if math.Abs(two-want) > 1e-12 {
		t.Errorf("two-sided blend = %v, want %v", two, want)
	}
}

func TestBlendIsCappedAndStrictOnMissingComponents(t *testing.T) {
	b := Blend{Label: "b", Rules: []Rule{fixed{20, "a"}, fixed{20, "b"}}, Weights: []float64{0.5, 0.5}, FDM: 2}
	if got := Forecast(b, instrument(false), unitVol()).Values[0]; got != ForecastCap {
		t.Errorf("blend = %v, want the cap", got)
	}
	nan := Blend{Label: "n", Rules: []Rule{fixed{10, "a"}, fixed{math.NaN(), "b"}}, Weights: []float64{0.5, 0.5}, FDM: 1}
	if got := Forecast(nan, instrument(false), unitVol()).Values[0]; !math.IsNaN(got) {
		t.Errorf("a blend with a missing component = %v, want NaN", got)
	}
}

func TestBlendRefusesAnFDMAboveTheLaw(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Error("FDM 3 accepted")
		}
	}()
	b := Blend{Label: "b", Rules: []Rule{fixed{1, "a"}}, Weights: []float64{1}, FDM: 3}
	b.Raw(instrument(false), unitVol())
}

func TestBlendCarriesItsComponentsStory(t *testing.T) {
	b := Blend{Label: "b", Rules: []Rule{EWMAC{Fast: 16}, EWMAC{Fast: 64}}, Weights: []float64{0.5, 0.5}, FDM: 1}
	if err := Validate(b); err != nil {
		t.Error(err)
	}
	if b.Story() != (EWMAC{}).Story() {
		t.Error("two EWMACs should share one story, not repeat it")
	}
}
