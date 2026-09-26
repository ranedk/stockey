package rules

import (
	"fmt"
	"math"

	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/data"
)

// ---------------------------------------------------------------------------
// Breakout: normalized Donchian channel position. Positive skew, same
// under-reaction premium as EWMAC but read off LEVELS instead of smoothed
// velocity — it fires when price leaves a range, regardless of the path it
// took to get there. docs/rule_ideas.md tier 1 #1: "first rule to add".
// ---------------------------------------------------------------------------

// Breakout is the position of price within its trailing N-day high/low range,
// centred so mid-range reads 0 and a new N-day high reads +0.5 before scaling.
// The output is smoothed with an EWMA of span N/4 (Carver's convention): the
// unsmoothed version is a step function that trades every time the range
// boundary rolls off, which fails the speed limit on anything but the slowest
// lookbacks.
type Breakout struct {
	N int // lookback in trading days
}

func (b Breakout) Name() string { return fmt.Sprintf("breakout%d", b.N) }

func (Breakout) Story() string {
	return "The same under-reaction counterparties EWMAC is paid by, met at a " +
		"different moment: stops resting above a well-known range are placed by " +
		"traders who must exit on a level, and index/option hedgers must buy as " +
		"price leaves the range they wrote. We are paid for taking the other side " +
		"of flow that is triggered by a price, not by a forecast — which is why " +
		"the trigger is path-independent where EWMAC's is not."
}

// Scalar: see scalars.go. Carver defines this rule as 40 x (normalized
// position), but that constant is for the UNSMOOTHED series; after the span
// N/4 smoothing the average absolute value falls, and by how much depends on
// the lookback. Ours are measured on the forecast distribution (no returns) —
// same procedure, our data.
func (b Breakout) Scalar() float64 { return lookupScalar(b.Name(), breakoutScalars, b.N) }

func (b Breakout) Raw(inst *data.Instrument, _ core.Series) core.Series {
	if b.N < 4 {
		panic(fmt.Sprintf("breakout: lookback %d too short to smooth (need >= 4)", b.N))
	}
	p := inst.Prices
	hi := core.RollingMax(p, b.N)
	lo := core.RollingMin(p, b.N)
	pos := make([]float64, p.Len())
	for i := range pos {
		h, l, x := hi.Values[i], lo.Values[i], p.Values[i]
		rng := h - l
		if math.IsNaN(h) || math.IsNaN(l) || math.IsNaN(x) || rng <= 0 {
			// A dead-flat range would divide by zero and order an infinite
			// position (Law 11); an unknown range is not a zero forecast.
			pos[i] = math.NaN()
			continue
		}
		pos[i] = (x - (h+l)/2) / rng
	}
	return core.EWMA(core.New(p.Times, pos), b.N/4, b.N/4)
}
