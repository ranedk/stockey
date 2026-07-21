// Package combine blends per-rule forecasts into one combined forecast per
// instrument (Bible Law 9): handcrafted weighted average × forecast
// diversification multiplier (FDM ≤ 2.5), re-capped at ±20.
package combine

import (
	"math"

	"github.com/ranedk/systrader/internal/core"
)

const MaxFDM = 2.5

// Combined computes the combined forecast. Weights must be positive; they are
// renormalized each day over the rules that have a valid (non-NaN) forecast,
// so an instrument without carry data simply runs on its trend rules.
func Combined(forecasts []core.Series, weights []float64, fdm float64) core.Series {
	if len(forecasts) == 0 || len(forecasts) != len(weights) {
		panic("combine: forecasts/weights mismatch")
	}
	if fdm > MaxFDM {
		fdm = MaxFDM // Law 9: never amplify beyond 2.5
	}
	if fdm < 1 {
		fdm = 1
	}
	n := forecasts[0].Len()
	out := make([]float64, n)
	for i := 0; i < n; i++ {
		var sum, wsum float64
		for j, f := range forecasts {
			v := f.Values[i]
			if math.IsNaN(v) {
				continue
			}
			sum += weights[j] * v
			wsum += weights[j]
		}
		if wsum == 0 {
			out[i] = math.NaN()
			continue
		}
		out[i] = core.Clip(sum/wsum*fdm, 20.0)
	}
	return core.New(forecasts[0].Times, out)
}
