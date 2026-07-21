// Package rules implements trading rules as forecast generators.
//
// Bible Law 8: a rule emits a CONTINUOUS forecast, proportional to expected
// return ÷ volatility, scaled so its long-run average absolute value is 10,
// hard-capped at ±20. Anything speaking this interface is a legal citizen of
// the system (EWMAC, carry, breakout, an ML model); nothing else may
// influence positions.
package rules

import (
	"fmt"
	"math"

	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/data"
)

const ForecastCap = 20.0

type Rule interface {
	Name() string
	// Raw returns the unscaled forecast, aligned to inst.Prices dates.
	// vol is daily price-unit volatility (core.PriceUnitVol).
	Raw(inst *data.Instrument, vol core.Series) core.Series
	// Scalar converts raw values to the average-absolute-10 convention.
	// Fixed constants fitted by Carver WITHOUT performance data (Law 8).
	Scalar() float64
}

// Forecast applies scalar + cap (+ long-only clipping) to a rule's raw output.
func Forecast(r Rule, inst *data.Instrument, vol core.Series) core.Series {
	raw := r.Raw(inst, vol)
	out := make([]float64, raw.Len())
	for i, v := range raw.Values {
		f := core.Clip(v*r.Scalar(), ForecastCap)
		if inst.Meta.LongOnly && f < 0 {
			f = 0
		}
		out[i] = f
	}
	return core.New(raw.Times, out)
}

// ---------------------------------------------------------------------------
// EWMAC: exponentially weighted moving average crossover (trend following).
// Positive skew. Story: investors under-react (prospect theory) → prices
// drift toward fair value → trends persist.
// ---------------------------------------------------------------------------

type EWMAC struct {
	Fast int // fast span; slow = 4×fast per Carver's appendix B
}

// Carver's forecast scalars (appendix B), keyed by fast span.
var ewmacScalars = map[int]float64{
	2: 10.6, 4: 7.5, 8: 5.3, 16: 3.75, 32: 2.65, 64: 1.87,
}

func (e EWMAC) Name() string { return fmt.Sprintf("ewmac%d_%d", e.Fast, e.Fast*4) }

func (e EWMAC) Scalar() float64 {
	if s, ok := ewmacScalars[e.Fast]; ok {
		return s
	}
	panic(fmt.Sprintf("ewmac: no forecast scalar for fast span %d (allowed: 2,4,8,16,32,64)", e.Fast))
}

func (e EWMAC) Raw(inst *data.Instrument, vol core.Series) core.Series {
	slow := e.Fast * 4
	fast := core.EWMA(inst.Prices, e.Fast, 2)
	slowS := core.EWMA(inst.Prices, slow, 2)
	out := make([]float64, inst.Prices.Len())
	for i := range out {
		f, s, v := fast.Values[i], slowS.Values[i], vol.Values[i]
		if math.IsNaN(f) || math.IsNaN(s) || math.IsNaN(v) || v <= 0 {
			out[i] = math.NaN()
			continue
		}
		out[i] = (f - s) / v
	}
	return core.New(inst.Prices.Times, out)
}

// ---------------------------------------------------------------------------
// Carry: expected return if prices stand still. Negative skew. Story: we are
// paid for bearing unpleasant skew and providing liquidity to hedgers/forced
// traders. Complements EWMAC: earns when nothing happens.
// ---------------------------------------------------------------------------

type Carry struct{}

func (Carry) Name() string { return "carry" }

// Carver's carry forecast scalar (appendix B).
func (Carry) Scalar() float64 { return 30.0 }

func (Carry) Raw(inst *data.Instrument, vol core.Series) core.Series {
	out := make([]float64, inst.Prices.Len())
	for i := range out {
		out[i] = math.NaN()
	}
	if inst.AnnCarry != nil {
		for i := range out {
			if i >= inst.AnnCarry.Len() {
				break
			}
			c, v := inst.AnnCarry.Values[i], vol.Values[i]
			if math.IsNaN(c) || math.IsNaN(v) || v <= 0 {
				continue
			}
			// annualized carry (price units/yr) ÷ annualized vol (√256 ≈ 16).
			out[i] = c / (v * 16)
		}
	}
	return core.New(inst.Prices.Times, out)
}
