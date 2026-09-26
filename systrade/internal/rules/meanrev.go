package rules

import (
	"fmt"
	"math"

	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/data"
)

// ---------------------------------------------------------------------------
// MeanReversion: slow, negatively-skewed complement to trend. Distance of
// price from a multi-year average, in the units that distance would have
// under a random walk. docs/rule_ideas.md tier 1 #3.
// ---------------------------------------------------------------------------

// MeanReversion is the NEGATIVE of the price's deviation from its Window-day
// simple average, normalized by vol x sqrt(Window) — the standard deviation a
// random walk would accumulate over that horizon, which is what makes the
// forecast comparable across instruments and across window lengths.
//
// Deliberately slow. A 2-5 year window turns over a handful of times per
// decade, which is the only reason a rule with this skew profile can clear the
// speed limit; the short-horizon version of the same idea is tier 2 (#7) and
// expected to die on costs.
type MeanReversion struct {
	Window int // trading days in the average (256 ~ 1 year)
}

func (m MeanReversion) Name() string { return fmt.Sprintf("meanrev%d", m.Window) }

func (MeanReversion) Story() string {
	return "Investors extrapolate multi-year returns into the future: money chases " +
		"whatever has already compounded and abandons whatever has already fallen " +
		"(De Bondt-Thaler). Career-risk keeps institutions in that same queue — " +
		"owning the fallen name is the one that gets you fired. We are paid for " +
		"taking the unglamorous side and waiting years, which is precisely the " +
		"horizon most capital cannot commit to."
}

func (m MeanReversion) Scalar() float64 { return lookupScalar(m.Name(), meanrevScalars, m.Window) }

func (m MeanReversion) Raw(inst *data.Instrument, vol core.Series) core.Series {
	p := inst.Prices
	avg := core.SMA(p, m.Window)
	horizon := math.Sqrt(float64(m.Window))
	out := make([]float64, p.Len())
	for i := range out {
		x, a, v := p.Values[i], avg.Values[i], vol.Values[i]
		if math.IsNaN(x) || math.IsNaN(a) || math.IsNaN(v) || v <= 0 {
			out[i] = math.NaN()
			continue
		}
		out[i] = -(x - a) / (v * horizon)
	}
	return core.New(p.Times, out)
}
