package rules

import (
	"fmt"
	"math"

	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/data"
)

// ---------------------------------------------------------------------------
// Reversal: how far a price has fallen below its own recent high, measured in
// units of what that name's volatility says a fall of that duration should be.
//
// This is not the mean reversion of docs/rule_ideas.md tier 1 #3, which reverts
// to a multi-year average and which LEDGER rows 14-15 killed twice (it measured
// -0.95 correlated with slow EWMAC, i.e. it was negative trend wearing a
// different name, and it lost 21.7% a year as a book). This one asks a
// different question on a much shorter clock: not "is this cheap against its
// own history" but "has this fallen further than its own noise can explain".
//
// LEDGER row 22 measured the raw shape and found forward returns RISING with
// depth — 1.63% over twenty days at half a sigma against 2.83% at four to six.
// That is what motivated writing it down as a rule.
// ---------------------------------------------------------------------------

// Reversal scores a name by its drawdown from the highest close of the last
// Window bars, divided by the move its own volatility would explain over the
// time since that high. Deeper than normal reads positive: buy what has fallen
// too far.
type Reversal struct {
	Window int // bars whose high the fall is measured from
}

func (r Reversal) Name() string { return fmt.Sprintf("reversal%d", r.Window) }

func (Reversal) Story() string {
	return "Forced sellers do not choose their moment. Margin calls, redemptions, " +
		"index deletions and risk limits all produce selling that must happen now and " +
		"at any price, and it lands hardest on whatever has already fallen. We are paid " +
		"for supplying the liquidity those sellers need — which is why the premium is " +
		"in the depth of the fall relative to the name's own noise, not in the fall itself."
}

func (r Reversal) Scalar() float64 { return lookupScalar(r.Name(), reversalScalars, r.Window) }

func (r Reversal) Raw(inst *data.Instrument, vol core.Series) core.Series {
	if r.Window < 5 {
		panic(fmt.Sprintf("reversal: window %d too short", r.Window))
	}
	p := inst.Prices
	high := core.RollingMax(p, r.Window)
	out := make([]float64, p.Len())
	for i := range out {
		out[i] = math.NaN()
		h, x, v := high.Values[i], p.Values[i], vol.Values[i]
		if math.IsNaN(h) || math.IsNaN(x) || math.IsNaN(v) || v <= 0 || h <= 0 {
			continue
		}
		// How long ago that high was set: the scale of a fall grows with the
		// square root of the time it had to happen in, so a 10% fall over two
		// days and the same fall over two months are not the same event.
		since := 1
		for j := i; j > i-r.Window && j > 0; j-- {
			if p.Values[j] == h {
				since = i - j
				break
			}
		}
		if since < 1 {
			since = 1
		}
		out[i] = (h - x) / (v * math.Sqrt(float64(since)))
	}
	return core.New(p.Times, out)
}
