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

// SMA computes a simple (arithmetic) moving average over a trailing window
// of `window` observations. Strict: a single NaN inside the window makes
// that output NaN too — no silent skipping, the window must be fully
// populated. internal/stage's Weinstein classifier depends on this being a
// genuine simple average (not EWMA, which already exists in this package):
// Weinstein's method is published against a 30-week SIMPLE average, and
// substituting EWMA would silently turn a well-established, literature-
// verified method into an untested variant (see internal/stage's doc comment
// on why that distinction matters for research/LEDGER.md's M-accounting).
func SMA(s Series, window int) Series {
	if window <= 0 {
		panic("core: SMA window must be positive")
	}
	out := make([]float64, s.Len())
	sum := 0.0
	nanCount := 0
	for i := 0; i < s.Len(); i++ {
		if v := s.Values[i]; math.IsNaN(v) {
			nanCount++
		} else {
			sum += v
		}
		if i >= window {
			if old := s.Values[i-window]; math.IsNaN(old) {
				nanCount--
			} else {
				sum -= old
			}
		}
		if i >= window-1 && nanCount == 0 {
			out[i] = sum / float64(window)
		} else {
			out[i] = math.NaN()
		}
	}
	return New(s.Times, out)
}

// ResampleWeeklyLast reduces a series to one point per ISO week: the LAST
// observation seen in that week, stamped with its actual date (not assumed
// Friday — a holiday-shortened week just ends on whatever day last traded).
// Input must be sorted ascending by time, as every series in this package is.
// internal/stage's Weinstein classifier is defined on this weekly-close
// convention.
func ResampleWeeklyLast(s Series) Series {
	var times []time.Time
	var vals []float64
	haveGroup := false
	var groupYear, groupWeek int
	var lastT time.Time
	var lastV float64
	for i := 0; i < s.Len(); i++ {
		y, w := s.Times[i].ISOWeek()
		if haveGroup && (y != groupYear || w != groupWeek) {
			times = append(times, lastT)
			vals = append(vals, lastV)
		}
		groupYear, groupWeek = y, w
		lastT, lastV = s.Times[i], s.Values[i]
		haveGroup = true
	}
	if haveGroup {
		times = append(times, lastT)
		vals = append(vals, lastV)
	}
	return New(times, vals)
}

// AlignByTime returns a series on `base`'s dates, where value[i] is
// `other`'s value at that exact date if present, else NaN.
//
// BUG FOUND LIVE 2026-08-21 (code review): this package's own convention
// (see the header comment) assumes every series for an instrument is
// positionally aligned by index -- true for anything DERIVED inside this
// package (EWMA, SMA, resampling, ...), but NOT automatically true for two
// series pulled from independent sources with different filters (e.g.
// store.AdjustedCloses filters adj_close > 0 while store.AdjustedVolume
// filters adj_volume >= 0 on the same table -- they can silently drop
// different dates). internal/stage.Classify used to pair such series by a
// length check alone, which can't catch "same length, different dates"
// silently mispairing the two. Any caller combining series from outside
// this package must run them through AlignByTime first; do not just check
// .Len().
func AlignByTime(base Series, other Series) Series {
	idx := make(map[time.Time]float64, other.Len())
	for i, t := range other.Times {
		idx[t] = other.Values[i]
	}
	out := make([]float64, base.Len())
	for i, t := range base.Times {
		if v, ok := idx[t]; ok {
			out[i] = v
		} else {
			out[i] = math.NaN()
		}
	}
	return New(base.Times, out)
}

// ResampleWeeklySum reduces a series to one point per ISO week: the SUM of
// that week's values. NaN entries are treated as contributing nothing (a
// missing day just means less was seen that week, not that the week is
// unknown) UNLESS every value in the week is NaN, in which case the week is
// NaN too. Intended for weekly volume (internal/stage's VolumeRatio field).
func ResampleWeeklySum(s Series) Series {
	var times []time.Time
	var vals []float64
	haveGroup := false
	var groupYear, groupWeek int
	var lastT time.Time
	var sum float64
	var sawValue bool
	flush := func() {
		times = append(times, lastT)
		if sawValue {
			vals = append(vals, sum)
		} else {
			vals = append(vals, math.NaN())
		}
	}
	for i := 0; i < s.Len(); i++ {
		y, w := s.Times[i].ISOWeek()
		if haveGroup && (y != groupYear || w != groupWeek) {
			flush()
			sum, sawValue = 0, false
		}
		groupYear, groupWeek = y, w
		lastT = s.Times[i]
		if v := s.Values[i]; !math.IsNaN(v) {
			sum += v
			sawValue = true
		}
		haveGroup = true
	}
	if haveGroup {
		flush()
	}
	return New(times, vals)
}

// RollingMax returns the maximum over a trailing window of `window`
// observations. Strict in the same way as SMA: a single NaN anywhere in the
// window makes that output NaN — a range computed off a partially-populated
// window is a different statistic, and the breakout rule that consumes this
// would silently widen or narrow its denominator without saying so.
func RollingMax(s Series, window int) Series {
	return rollingExtreme(s, window, func(a, b float64) bool { return a >= b })
}

// RollingMin is RollingMax's mirror; same strictness.
func RollingMin(s Series, window int) Series {
	return rollingExtreme(s, window, func(a, b float64) bool { return a <= b })
}

// rollingExtreme is the shared monotonic-deque scan: `dominates(a, b)` reports
// whether a incoming value a makes an older value b useless (>= for a max,
// <= for a min), which is what lets the window be maintained in O(1) amortized
// per bar instead of rescanning it.
func rollingExtreme(s Series, window int, dominates func(a, b float64) bool) Series {
	if window <= 0 {
		panic("core: rolling window must be positive")
	}
	out := make([]float64, s.Len())
	dq := make([]int, 0, window) // indices, values monotonic by `dominates`
	nanCount := 0
	for i := 0; i < s.Len(); i++ {
		v := s.Values[i]
		if math.IsNaN(v) {
			nanCount++
		} else {
			for len(dq) > 0 && dominates(v, s.Values[dq[len(dq)-1]]) {
				dq = dq[:len(dq)-1]
			}
			dq = append(dq, i)
		}
		if i >= window {
			if old := s.Values[i-window]; math.IsNaN(old) {
				nanCount--
			}
			if len(dq) > 0 && dq[0] <= i-window {
				dq = dq[1:]
			}
		}
		if i >= window-1 && nanCount == 0 {
			out[i] = s.Values[dq[0]]
		} else {
			out[i] = math.NaN()
		}
	}
	return New(s.Times, out)
}
