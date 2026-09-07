package rules

import (
	"fmt"
	"math"

	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/data"
)

// ---------------------------------------------------------------------------
// Acceleration: the change in EWMAC's raw forecast over its own fast span —
// momentum of momentum. Fires early in a trend and fades a stale one.
// docs/rule_ideas.md tier 1 #2.
// ---------------------------------------------------------------------------

// Acceleration differences the EWMAC raw forecast against its value Fast days
// ago. Reading the DERIVATIVE rather than the level is what makes it partly
// independent of EWMAC: a mature trend running at a constant rate scores ~0
// here while EWMAC is still at full forecast.
type Acceleration struct {
	Fast int // fast span of the underlying EWMAC; slow = 4x, lag = Fast
}

func (a Acceleration) Name() string { return fmt.Sprintf("accel%d", a.Fast) }

func (Acceleration) Story() string {
	return "Herding builds gradually: the flow that creates a trend arrives in a " +
		"queue, as slower investors (allocators on quarterly cycles, retail " +
		"following the news cycle) act on the same information at different times. " +
		"Being paid for absorbing that flow is worth most while the queue is still " +
		"forming and nothing while it has emptied — so we bet on the rate of change " +
		"of the move rather than on the move, and leave stale trends to others."
}

func (a Acceleration) Scalar() float64 { return lookupScalar(a.Name(), accelScalars, a.Fast) }

func (a Acceleration) Raw(inst *data.Instrument, vol core.Series) core.Series {
	base := EWMAC{Fast: a.Fast}.Raw(inst, vol)
	out := make([]float64, base.Len())
	for i := range out {
		if i < a.Fast {
			out[i] = math.NaN()
			continue
		}
		now, then := base.Values[i], base.Values[i-a.Fast]
		if math.IsNaN(now) || math.IsNaN(then) {
			out[i] = math.NaN()
			continue
		}
		out[i] = now - then
	}
	return core.New(base.Times, out)
}
