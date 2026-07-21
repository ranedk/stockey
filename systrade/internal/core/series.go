// Package core provides date-aligned series math for the trading framework.
//
// Convention: all derived series preserve the length and dates of their
// input, using NaN for warm-up/undefined values. This keeps every series for
// an instrument aligned by index, which makes look-ahead bugs structurally
// harder: value[i] may only ever be computed from values[0..i].
package core

import (
	"math"
	"time"
)

type Series struct {
	Times  []time.Time
	Values []float64
}

func New(times []time.Time, values []float64) Series {
	if len(times) != len(values) {
		panic("core: times/values length mismatch")
	}
	return Series{Times: times, Values: values}
}

func (s Series) Len() int { return len(s.Values) }

// Last returns the most recent non-NaN value and true, or 0 and false.
func (s Series) Last() (float64, bool) {
	for i := s.Len() - 1; i >= 0; i-- {
		if !math.IsNaN(s.Values[i]) {
			return s.Values[i], true
		}
	}
	return 0, false
}

// Diff returns v[i] - v[i-1], NaN at index 0.
func Diff(s Series) Series {
	out := make([]float64, s.Len())
	if s.Len() > 0 {
		out[0] = math.NaN()
	}
	for i := 1; i < s.Len(); i++ {
		out[i] = s.Values[i] - s.Values[i-1]
	}
	return New(s.Times, out)
}

// EWMA computes an exponentially weighted moving average with the given span
// (alpha = 2/(span+1)). Output is NaN until minPeriods non-NaN inputs have
// been seen. NaN inputs are skipped (carry previous state).
func EWMA(s Series, span, minPeriods int) Series {
	alpha := 2.0 / (float64(span) + 1.0)
	out := make([]float64, s.Len())
	var m float64
	n := 0
	for i, v := range s.Values {
		if math.IsNaN(v) {
			out[i] = math.NaN()
			continue
		}
		if n == 0 {
			m = v
		} else {
			m = alpha*v + (1-alpha)*m
		}
		n++
		if n >= minPeriods {
			out[i] = m
		} else {
			out[i] = math.NaN()
		}
	}
	return New(s.Times, out)
}

// EWMAStd computes an exponentially weighted standard deviation of the input
// values using var = E[x^2] - E[x]^2 with EWMA expectations.
func EWMAStd(s Series, span, minPeriods int) Series {
	alpha := 2.0 / (float64(span) + 1.0)
	out := make([]float64, s.Len())
	var m1, m2 float64
	n := 0
	for i, v := range s.Values {
		if math.IsNaN(v) {
			out[i] = math.NaN()
			continue
		}
		if n == 0 {
			m1, m2 = v, v*v
		} else {
			m1 = alpha*v + (1-alpha)*m1
			m2 = alpha*v*v + (1-alpha)*m2
		}
		n++
		if n >= minPeriods {
			out[i] = math.Sqrt(math.Max(0, m2-m1*m1))
		} else {
			out[i] = math.NaN()
		}
	}
	return New(s.Times, out)
}

// Clip bounds x to [-cap, +cap], passing NaN through.
func Clip(x, capAbs float64) float64 {
	if math.IsNaN(x) {
		return x
	}
	if x > capAbs {
		return capAbs
	}
	if x < -capAbs {
		return -capAbs
	}
	return x
}

// PriceUnitVol estimates daily price volatility in PRICE UNITS: the EWMA
// standard deviation (default span 36 ≈ Carver's 25-business-day window) of
// daily price changes. This is the denominator for EWMAC forecasts and the
// basis of instrument value volatility for position sizing.
func PriceUnitVol(prices Series, span, minPeriods int) Series {
	if span <= 0 {
		span = 36
	}
	if minPeriods <= 0 {
		minPeriods = 10
	}
	v := EWMAStd(Diff(prices), span, minPeriods)
	// A volatility of exactly zero would order an infinite position (Law 11);
	// treat it as unknown instead.
	for i, x := range v.Values {
		if x == 0 {
			v.Values[i] = math.NaN()
		}
	}
	return v
}
