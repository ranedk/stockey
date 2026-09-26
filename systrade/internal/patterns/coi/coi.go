// Package coi implements the "COI" (change-of-intent) three-bar reversal
// scanner ported from a discretionary trader's Python screener
// (rahul_ta_1.py at the repo root).
//
// STATUS: this is a RESEARCH PATTERN, not a trading rule. It emits a discrete
// event, not the continuous vol-standardized forecast TRADING_BIBLE.md Law 8
// requires, and nothing here may size a position. It exists so the pattern
// can be measured honestly (internal/patterns/coi's study.go) before anyone
// decides whether a forecast can be built from it.
//
// The pattern, in the original author's terms:
//
//	C-1  a down candle (close < open)
//	C0   makes a lower high AND a lower low than C-1 — the last push down,
//	     and a pivot low (its low is below both neighbours')
//	C+1  makes a higher high AND a higher low than C0, and closes above
//	     C0's high — the "intent" of the market has changed
//
// Confirmation therefore lands at C+1's close and needs no data after it:
// the pattern is causal. Three optional context tags grade the setup:
//
//	important        one of the three bars touched or crossed the lower
//	                 Bollinger band (20, 2) — the reversal happened where
//	                 price was statistically stretched, not mid-range
//	veryImportant    at C+1 the band width was CONTRACTING (lower band
//	                 rising faster than the upper band)
//	upperBandRising  important AND the upper band was rising at C+1
//
// Everything else the original computed (MACD 6/13 with a 5-period signal,
// EMA10/EMA20, both bands, and first and second derivatives of each) is
// recorded but never filtered on. It is a feature table for later study,
// which is exactly how it is treated here.
package coi

import (
	"fmt"
	"math"

	"github.com/ranedk/systrader/internal/bars"
)

// Params holds every number the original screener hard-coded. They are
// captured as parameters rather than constants because each one is a
// research degree of freedom: varying any of them is a new trial that
// research/LEDGER.md must count toward M (Law 2).
type Params struct {
	MACDFast   int // 6
	MACDSlow   int // 13
	MACDSignal int // 5
	BBWindow   int // 20
	BBMult     float64
	EMAShort   int // 10
	EMALong    int // 20

	// RescanTerminator fixes a quirk in the original pivot scan: after
	// emitting a pivot, the bar that terminated it was never re-examined as
	// the start of the next one, so some pivot lows are missed. False
	// reproduces the original exactly; true detects every pivot low.
	RescanTerminator bool
}

// DefaultParams is the original screener's configuration, unchanged.
func DefaultParams() Params {
	return Params{
		MACDFast: 6, MACDSlow: 13, MACDSignal: 5,
		BBWindow: 20, BBMult: 2, EMAShort: 10, EMALong: 20,
	}
}

// Indicators holds the derived series, all aligned 1:1 with the input bars
// and NaN-padded at the front where a window is not yet full.
type Indicators struct {
	MACD, Signal           []float64
	MACDD1, MACDD2         []float64
	SignalD1, SignalD2     []float64
	SMA, StdDev            []float64
	BBUpper, BBLower       []float64
	BBUpperD1, BBLowerD1   []float64
	BBUpperD2, BBLowerD2   []float64
	BBSlopeDiff            []float64
	EMAShort, EMALong      []float64
	EMAShortD1, EMAShortD2 []float64
	EMALongD1, EMALongD2   []float64
}

// Compute derives every indicator the original screener used.
func Compute(b []bars.Bar, p Params) Indicators {
	close := make([]float64, len(b))
	for i := range b {
		close[i] = b[i].Close
	}

	emaFast := ema(close, p.MACDFast)
	emaSlow := ema(close, p.MACDSlow)
	macd := sub(emaFast, emaSlow)
	signal := ema(macd, p.MACDSignal)

	sma := rollingMean(close, p.BBWindow)
	sd := rollingStd(close, p.BBWindow)
	upper := make([]float64, len(close))
	lower := make([]float64, len(close))
	for i := range close {
		upper[i] = sma[i] + p.BBMult*sd[i]
		lower[i] = sma[i] - p.BBMult*sd[i]
	}

	upperD1, lowerD1 := d1(upper), d1(lower)
	slopeDiff := make([]float64, len(close))
	for i := range close {
		slopeDiff[i] = lowerD1[i] - upperD1[i]
	}

	emaS := ema(close, p.EMAShort)
	emaL := ema(close, p.EMALong)

	return Indicators{
		MACD: macd, Signal: signal,
		MACDD1: d1(macd), MACDD2: d2(macd),
		SignalD1: d1(signal), SignalD2: d2(signal),
		SMA: sma, StdDev: sd,
		BBUpper: upper, BBLower: lower,
		BBUpperD1: upperD1, BBLowerD1: lowerD1,
		BBUpperD2: d1(upperD1), BBLowerD2: d1(lowerD1),
		BBSlopeDiff: slopeDiff,
		EMAShort:    emaS, EMALong: emaL,
		EMAShortD1: d1(emaS), EMAShortD2: d2(emaS),
		EMALongD1: d1(emaL), EMALongD2: d2(emaL),
	}
}

// PivotLows returns the indices of pivot-low bars: a bar whose low is below
// the next bar's low and at or below the previous bar's low.
//
// The original scans BACKWARDS, tracking a running minimum, and emits the
// running-minimum index when a bar fails to make a new low. That is exactly a
// pivot low, with one wrinkle: the bar that terminates a run is consumed and
// never becomes the seed of the next run, so a pivot immediately to the left
// of another pivot's run is skipped. Params.RescanTerminator controls whether
// that wrinkle is reproduced (false, the default) or fixed (true).
//
// Indices are returned in the order the backward scan produced them
// (descending), which is the order the original used.
func PivotLows(b []bars.Bar, p Params) []int {
	var out []int
	candidate := -1
	var candidateLow float64

	for i := len(b) - 2; i >= 0; i-- {
		low := b[i].Low
		if candidate < 0 {
			if low < b[i+1].Low {
				candidate, candidateLow = i, low
			}
			continue
		}
		if low < candidateLow {
			candidate, candidateLow = i, low
			continue
		}
		out = append(out, candidate)
		candidate = -1
		if p.RescanTerminator && low < b[i+1].Low {
			candidate, candidateLow = i, low
		}
	}
	if candidate >= 0 {
		out = append(out, candidate)
	}
	return out
}

// Setup is one confirmed COI pattern. C0 is the pivot-low bar; the pattern is
// confirmed at the close of C0+1, which is the first moment it is knowable.
type Setup struct {
	Symbol string
	C0     int // index of the pivot-low bar

	// Confirm is the index of the bar at whose CLOSE this setup becomes
	// knowable, so the earliest fillable entry is Confirm+1's open. It is
	// carried explicitly rather than assumed to be C0+1 because the whole
	// point of the "enter earlier" variants is to move it.
	Confirm int

	Important       bool
	VeryImportant   bool
	UpperBandRising bool

	// Context recorded at C+1, unfiltered — the original author's feature
	// table, kept so a later study can ask which of these, if any, separates
	// the winners from the losers.
	MACD, Signal            float64
	MACDAboveSignal         bool
	MACDD1, MACDD2          float64
	SignalD1, SignalD2      float64
	EMAShortD1, EMAShortD2  float64
	EMALongD1, EMALongD2    float64
	BBSlopeDiff             float64
	BBUpperD1, BBLowerD1    float64
	BBUpperD2, BBLowerD2    float64
	BandWidthPct            float64 // (upper-lower)/close at confirmation
	ConfirmCloseVsC0HighPct float64 // how far the confirming bar closed above C0's high
	C0LowStopDistancePct    float64 // (confirm close - C0 low) / confirm close

	// Context for the panic-day and dial experiments. All of it is measured
	// strictly at or before C0, so nothing here can see the confirmation.
	PriorHigh            float64 // highest high in the PriorHighLookback bars before C0
	DropFromPriorHighPct float64 // how far C0's low sits below that high
	PriorSwingHigh1      float64 // most recent pivot high before C0 (exit target 1)
	PriorSwingHigh2      float64 // the pivot high before that (exit target 2)
	PriorUptrendMACD     bool    // MACD above its signal AND positive, PriorTrendLag bars before C0
	PriorUptrendSMA      bool    // close above its 200-bar SMA, PriorTrendLag bars before C0
	C0DayReturn          float64 // C0's own close-to-close return
	SlopeZ               float64 // EMALong slope at confirmation in daily-vol units (the "dial")
}

// PriorHighLookback and PriorTrendLag fix how far back the "was this going up
// before it fell" context reads. Both are fixed here rather than swept: every
// value tried is a separate trial under Law 2, and the point of the panic
// experiment is the breadth idea, not a lookback search.
const (
	PriorHighLookback = 120
	PriorTrendLag     = 20
	PriorTrendSMA     = 200
)

// Variant selects how much of the original pattern is required. The original
// waits for a strong bounce bar before entering, which means paying up for a
// reversal that has already happened; the looser variants exist to test
// whether that confirmation helps or hurts.
type Variant int

const (
	// Full is the original: pivot low, down bar before it, and a bounce bar
	// that makes a higher high, a higher low, AND closes above C0's high.
	// Knowable at C0+1's close.
	Full Variant = iota
	// Weak drops only the close-above-C0's-high requirement. Knowable at
	// C0+1's close, but entered without waiting for a decisive close.
	Weak
	// Early drops the whole bounce-bar requirement: a pivot low preceded by
	// a down bar and a lower-low bar is enough. Still knowable only at
	// C0+1's close, because "C0 is a pivot low" needs C0+1 to exist.
	Early
	// Falling drops the pivot requirement too — a down bar followed by a
	// lower-high, lower-low bar. This is the only variant knowable at C0's
	// OWN close, so it enters a full day earlier than everything above.
	Falling
)

func (v Variant) String() string {
	switch v {
	case Weak:
		return "weak"
	case Early:
		return "early"
	case Falling:
		return "falling"
	}
	return "full"
}

// ParseVariant maps a flag value to a Variant.
func ParseVariant(s string) (Variant, error) {
	switch s {
	case "full":
		return Full, nil
	case "weak":
		return Weak, nil
	case "early":
		return Early, nil
	case "falling":
		return Falling, nil
	}
	return Full, fmt.Errorf("coi: unknown variant %q (want full|weak|early|falling)", s)
}

// Detect returns every confirmed setup in the series for the given variant.
//
// No look-ahead: each setup uses only bars up to and including its Confirm
// index, and is reported as knowable at that bar's close.
func Detect(s bars.Series, ind Indicators, p Params) []Setup {
	return DetectVariant(s, ind, p, Full)
}

// DetectVariant is Detect with the pattern strictness selectable.
func DetectVariant(s bars.Series, ind Indicators, p Params, v Variant) []Setup {
	b := s.Bars
	var out []Setup

	candidates := func() []int {
		if v == Falling {
			// No pivot needed: every bar is a candidate C0.
			idx := make([]int, 0, len(b))
			for i := range b {
				idx = append(idx, i)
			}
			return idx
		}
		return PivotLows(b, p)
	}()

	swingHighs := PivotHighs(b)

	for _, i := range candidates {
		confirm := i + 1
		if v == Falling {
			confirm = i
		}
		if i <= 0 || confirm >= len(b) {
			continue
		}
		cm1, c0 := b[i-1], b[i]
		if !cm1.Valid() || !c0.Valid() {
			continue
		}
		// Shared by every variant: a down bar, then a bar that pushes lower
		// on both ends. That is the "last leg down" the whole idea rests on.
		if !(cm1.Close < cm1.Open && c0.High < cm1.High && c0.Low < cm1.Low) {
			continue
		}
		if v == Full || v == Weak {
			cp1 := b[i+1]
			if !cp1.Valid() || !(cp1.High > c0.High && cp1.Low > c0.Low) {
				continue
			}
			if v == Full && !(cp1.Close > c0.High) {
				continue
			}
		}

		j := confirm // every context reading is taken at the confirming bar

		important := false
		for k := i - 1; k <= i+1 && k < len(b); k++ {
			if k > confirm {
				break // never read a bar the setup could not have seen
			}
			bb := ind.BBLower[k]
			if math.IsNaN(bb) {
				continue
			}
			c := b[k]
			bodyLow := math.Min(c.Open, c.Close)
			bodyHigh := math.Max(c.Open, c.Close)
			if (c.Low <= bb && bb <= c.High) || (bodyLow <= bb && bb <= bodyHigh) {
				important = true
				break
			}
		}

		width := ind.BBUpper[j] - ind.BBLower[j]

		st := Setup{
			Symbol: s.Symbol, C0: i, Confirm: confirm,

			Important:       important,
			VeryImportant:   ind.BBSlopeDiff[j] > 0,
			UpperBandRising: important && ind.BBUpperD1[j] > 0,

			MACD:            ind.MACD[j],
			Signal:          ind.Signal[j],
			MACDAboveSignal: ind.MACD[j] > ind.Signal[j],
			MACDD1:          ind.MACDD1[j], MACDD2: ind.MACDD2[j],
			SignalD1: ind.SignalD1[j], SignalD2: ind.SignalD2[j],
			EMAShortD1: ind.EMAShortD1[j], EMAShortD2: ind.EMAShortD2[j],
			EMALongD1: ind.EMALongD1[j], EMALongD2: ind.EMALongD2[j],
			BBSlopeDiff: ind.BBSlopeDiff[j],
			BBUpperD1:   ind.BBUpperD1[j], BBLowerD1: ind.BBLowerD1[j],
			BBUpperD2: ind.BBUpperD2[j], BBLowerD2: ind.BBLowerD2[j],

			BandWidthPct:            width / b[j].Close,
			ConfirmCloseVsC0HighPct: (b[j].Close - c0.High) / c0.High,
			C0LowStopDistancePct:    (b[j].Close - c0.Low) / b[j].Close,
		}
		fillContext(&st, b, ind, swingHighs)
		out = append(out, st)
	}
	return out
}

// fillContext adds the "was this going up before it fell, and how far did it
// fall" readings. Everything here is measured at or before C0 — never at the
// confirming bar and never after it.
func fillContext(st *Setup, b []bars.Bar, ind Indicators, swingHighs []int) {
	i := st.C0

	lo := max(0, i-PriorHighLookback)
	high := 0.0
	for k := lo; k < i; k++ {
		if b[k].High > high {
			high = b[k].High
		}
	}
	st.PriorHigh = high
	if high > 0 {
		st.DropFromPriorHighPct = (high - b[i].Low) / high
	}

	// The two most recent pivot highs strictly before C0 — the natural
	// "get back to where it broke down from" exit targets.
	found := 0
	for k := len(swingHighs) - 1; k >= 0 && found < 2; k-- {
		if swingHighs[k] >= i {
			continue
		}
		if found == 0 {
			st.PriorSwingHigh1 = b[swingHighs[k]].High
		} else {
			st.PriorSwingHigh2 = b[swingHighs[k]].High
		}
		found++
	}

	if lag := i - PriorTrendLag; lag >= 0 {
		st.PriorUptrendMACD = ind.MACD[lag] > ind.Signal[lag] && ind.MACD[lag] > 0
		if lag >= PriorTrendSMA {
			sum := 0.0
			for k := lag - PriorTrendSMA + 1; k <= lag; k++ {
				sum += b[k].Close
			}
			st.PriorUptrendSMA = b[lag].Close > sum/float64(PriorTrendSMA)
		}
	}

	if i > 0 && b[i-1].Close > 0 {
		st.C0DayReturn = (b[i].Close - b[i-1].Close) / b[i-1].Close
	}

	// The dial: the long-EMA slope at confirmation, expressed in units of the
	// stock's own daily volatility so it is comparable across names. Law 8
	// wants a continuous, vol-standardized reading, not a yes/no.
	j := st.Confirm
	if vol := dailyVol(b, j, 36); vol > 0 && !math.IsNaN(ind.EMALongD1[j]) {
		st.SlopeZ = ind.EMALongD1[j] / vol
	}
}

// dailyVol is a simple trailing standard deviation of price CHANGES (not
// returns) over span bars ending at i, in price units — the same units the
// EMA slope is in, so the ratio is dimensionless.
func dailyVol(b []bars.Bar, i, span int) float64 {
	lo := i - span + 1
	if lo < 1 {
		return math.NaN()
	}
	n := 0
	mean := 0.0
	for k := lo; k <= i; k++ {
		mean += b[k].Close - b[k-1].Close
		n++
	}
	mean /= float64(n)
	ss := 0.0
	for k := lo; k <= i; k++ {
		d := (b[k].Close - b[k-1].Close) - mean
		ss += d * d
	}
	return math.Sqrt(ss / float64(n-1))
}

// PivotHighs returns indices of bars whose high is above the next bar's high
// and at or above the previous bar's — the mirror of PivotLows, used to place
// "exit at the previous high" targets. Ascending order.
func PivotHighs(b []bars.Bar) []int {
	var out []int
	for i := 1; i < len(b)-1; i++ {
		if b[i].High > b[i+1].High && b[i].High >= b[i-1].High {
			out = append(out, i)
		}
	}
	return out
}

// --- small numeric helpers -------------------------------------------------
//
// These deliberately mirror pandas semantics, because the reference
// implementation is pandas and a silent difference here would look like a
// disagreement about the strategy rather than about arithmetic:
//   - ema  == Series.ewm(span=n, adjust=False).mean(), seeded on the first value
//   - rollingStd == Series.rolling(n).std(), i.e. SAMPLE std (ddof=1)

func ema(x []float64, span int) []float64 {
	alpha := 2.0 / (float64(span) + 1.0)
	out := make([]float64, len(x))
	var m float64
	started := false
	for i, v := range x {
		if math.IsNaN(v) {
			out[i] = math.NaN()
			continue
		}
		if !started {
			m, started = v, true
		} else {
			m = alpha*v + (1-alpha)*m
		}
		out[i] = m
	}
	return out
}

func rollingMean(x []float64, w int) []float64 {
	out := make([]float64, len(x))
	sum := 0.0
	for i, v := range x {
		sum += v
		if i >= w {
			sum -= x[i-w]
		}
		if i >= w-1 {
			out[i] = sum / float64(w)
		} else {
			out[i] = math.NaN()
		}
	}
	return out
}

func rollingStd(x []float64, w int) []float64 {
	out := make([]float64, len(x))
	for i := range x {
		if i < w-1 {
			out[i] = math.NaN()
			continue
		}
		mean := 0.0
		for _, v := range x[i-w+1 : i+1] {
			mean += v
		}
		mean /= float64(w)
		ss := 0.0
		for _, v := range x[i-w+1 : i+1] {
			d := v - mean
			ss += d * d
		}
		out[i] = math.Sqrt(ss / float64(w-1))
	}
	return out
}

func sub(a, b []float64) []float64 {
	out := make([]float64, len(a))
	for i := range a {
		out[i] = a[i] - b[i]
	}
	return out
}

func d1(x []float64) []float64 {
	out := make([]float64, len(x))
	if len(x) > 0 {
		out[0] = math.NaN()
	}
	for i := 1; i < len(x); i++ {
		out[i] = x[i] - x[i-1]
	}
	return out
}

func d2(x []float64) []float64 {
	out := make([]float64, len(x))
	for i := range x {
		if i < 2 {
			out[i] = math.NaN()
			continue
		}
		out[i] = x[i] - 2*x[i-1] + x[i-2]
	}
	return out
}
