package rules

import (
	"fmt"
	"math"
	"strings"

	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/data"
)

// MaxFDM is Law 9's ceiling on the forecast diversification multiplier.
const MaxFDM = 2.5

// Blend is several rules combined into one forecast (Law 9): the weighted
// average of their capped forecasts, times the forecast diversification
// multiplier, re-capped at ±20. Weights and FDM are FROZEN inputs — from
// internal/handcraft, written into a spec — and nothing here fits them.
//
// Because Blend speaks the Rule interface, anything that trades a rule trades
// a blend, and the Law 1 story check applies to it like any other rule.
type Blend struct {
	Label   string
	Rules   []Rule
	Weights []float64
	FDM     float64
}

func (b Blend) Name() string { return b.Label }

// Story is the components' stories. A blend has no mechanism of its own; if
// its members have none, neither does it.
func (b Blend) Story() string {
	seen := map[string]bool{}
	var parts []string
	for _, r := range b.Rules {
		if s := r.Story(); !seen[s] {
			seen[s] = true
			parts = append(parts, s)
		}
	}
	return strings.Join(parts, " | ")
}

// Scalar is 1: every component is already on the average-absolute-10 scale,
// and the FDM is what restores that scale after averaging.
func (Blend) Scalar() float64 { return 1 }

// Raw combines the components BEFORE any long-only clip. A long-only book
// still needs to know that one speed is strongly negative on a name: clipping
// each component at zero first would throw that away and rank a name with
// forecasts of +12, -15, -15 level with one at +12, 0, 0. So the components
// are computed two-sided and the instrument's own clip is applied once, by
// Forecast, to the combined value — Carver's order.
//
// Strict on missing values: if any component is undefined the blend is too.
// Re-weighting over whichever components happen to exist would put a name
// with one warmed-up rule, multiplied by a three-rule FDM, into the same
// ranking as names with all three.
func (b Blend) Raw(inst *data.Instrument, vol core.Series) core.Series {
	b.check()
	twoSided := *inst
	twoSided.Meta.LongOnly = false
	fs := make([]core.Series, len(b.Rules))
	for k, r := range b.Rules {
		fs[k] = Forecast(r, &twoSided, vol)
	}
	out := make([]float64, inst.Prices.Len())
	for i := range out {
		var sum float64
		for k := range fs {
			v := fs[k].Values[i]
			if math.IsNaN(v) {
				sum = math.NaN()
				break
			}
			sum += b.Weights[k] * v
		}
		out[i] = core.Clip(sum*b.FDM, ForecastCap)
	}
	return core.New(inst.Prices.Times, out)
}

func (b Blend) check() {
	if len(b.Rules) == 0 || len(b.Rules) != len(b.Weights) {
		panic(fmt.Sprintf("blend %q: %d rules and %d weights", b.Label, len(b.Rules), len(b.Weights)))
	}
	var sum float64
	for _, w := range b.Weights {
		if w <= 0 {
			panic(fmt.Sprintf("blend %q: weights must be positive (Law 9), got %v", b.Label, b.Weights))
		}
		sum += w
	}
	if math.Abs(sum-1) > 1e-9 {
		panic(fmt.Sprintf("blend %q: weights sum to %v, not 1", b.Label, sum))
	}
	if b.FDM < 1 || b.FDM > MaxFDM {
		panic(fmt.Sprintf("blend %q: FDM %v outside [1, %v] (Law 9)", b.Label, b.FDM, MaxFDM))
	}
}

// SpeedBlend is the frozen trend-speed-blend forecast
// (docs/strategies/2026-09-10_trend_speed_blend.md): the three slow EWMAC
// speeds, weighted by Table 8 row 11 on the correlations of their books'
// excess returns (0.84/0.61/0.82 — the same row row 14's forecast
// correlations and Carver's own example give), adjusted for cost by Table 12
// column A, with the FDM over their pooled forecast correlations. Derived by
// `slice speeds` (research/reports/2026-09-10_speed_blend_construction.txt).
// Frozen: no performance entered these numbers and none may change them.
func SpeedBlend() Blend {
	return Blend{
		Label:   "trend-speed-blend",
		Rules:   []Rule{EWMAC{Fast: 16}, EWMAC{Fast: 32}, EWMAC{Fast: 64}},
		Weights: []float64{0.40, 0.16, 0.44},
		FDM:     1.10,
	}
}
