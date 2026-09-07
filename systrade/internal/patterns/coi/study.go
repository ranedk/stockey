package coi

import (
	"math"
	"math/rand"
	"sort"
	"time"

	"github.com/ranedk/systrader/internal/bars"
)

// --- the original author's own diagnostic ----------------------------------

// Excursion is the statistic the Python screener actually reported: from the
// C+1 close, how far did price travel UP before the setup was invalidated by
// a close below C0's low.
//
// It is a maximum-favourable-excursion measure, NOT a return. It answers "how
// good could this have been for a trader with perfect foresight about when to
// take profit", which is why it looks so much better than any tradeable exit
// rule. It is reproduced here only so the Go port can be checked against the
// Python, and so the size of that gap can be quantified rather than argued
// about.
type Excursion struct {
	EntryClose  float64
	C0Low       float64
	HighestHigh float64
	MaxMovePct  float64
	DaysToHigh  int
	Status      string // "BROKEN" or "ACTIVE"
	DaysActive  int
	BreakDate   time.Time
	HighDate    time.Time
}

// Excursions computes the original diagnostic for one setup.
func (s Setup) Excursion(b []bars.Bar) Excursion {
	j := s.Confirm
	if j >= len(b) {
		return Excursion{}
	}
	entry := b[j].Close
	c0Low := b[s.C0].Low

	hh, hhIdx := b[j].High, j
	breakIdx := -1
	for k := j + 1; k < len(b); k++ {
		if b[k].Close < c0Low {
			breakIdx = k
			break
		}
		if b[k].High > hh {
			hh, hhIdx = b[k].High, k
		}
	}

	e := Excursion{
		EntryClose: entry, C0Low: c0Low,
		HighestHigh: hh,
		MaxMovePct:  (hh - entry) / entry * 100,
		DaysToHigh:  hhIdx - j,
		HighDate:    b[hhIdx].Date,
	}
	if breakIdx >= 0 {
		e.Status = "BROKEN"
		e.DaysActive = breakIdx - j
		e.BreakDate = b[breakIdx].Date
	} else {
		e.Status = "ACTIVE"
		e.DaysActive = len(b) - 1 - j
	}
	return e
}

// --- an honest, tradeable evaluation ---------------------------------------

// ExitPolicy is a complete, mechanical exit. The original strategy has none —
// it has an invalidation level (a close below C0's low) and a hindsight
// measurement, which is not the same thing. Each policy below is one research
// trial and must be counted toward research/LEDGER.md's M.
//
// Every exit here is decided on a CLOSE and filled at the NEXT OPEN, matching
// internal/backtest's convention. That is deliberately pessimistic for stops:
// a real resting stop order would fill intrabar, and usually worse than the
// next open in a fast market. Modelling intrabar fills off daily bars invents
// a fill price that the data cannot support.
type ExitPolicy struct {
	Name string

	UseC0Stop    bool    // exit on a close below C0's low
	FixedStopPct float64 // exit on a close FixedStopPct below entry (0 = none)
	MaxBars      int     // exit after N bars held (0 = no limit)
	TargetR      float64 // exit on a close at entry + TargetR x initial risk (0 = none)
	TargetPct    float64 // exit on a close TargetPct above entry (0 = none)
	SwingTarget  int     // 1 or 2: exit at the Nth prior swing high (0 = none)
	TrailATR     float64 // trail a stop TrailATR x ATR below the highest close (0 = none)
	ATRPeriod    int
	OnReversal   bool // exit when a bearish three-bar reversal confirms
}

// Trade is one simulated round trip.
type Trade struct {
	Symbol      string
	C0Date      time.Time
	ConfirmDate time.Time // C+1 close: the moment the signal is knowable
	EntryDate   time.Time // fill, at the next open
	ExitDate    time.Time
	EntryPx     float64
	ExitPx      float64
	BarsHeld    int
	GrossRet    float64
	NetRet      float64
	Reason      string // stop | target | time | trail | truncated
	MFEPct      float64
	MAEPct      float64
	RiskPct     float64 // (entry - C0 low) / entry, the initial stop distance
	TurnoverINR float64 // 60-bar median traded value at the signal
	Setup       Setup
}

// Simulate runs one setup through one exit policy. entryIdx is the bar whose
// OPEN is the fill; for a real signal that is C0+2. stopLevel is the price a
// close below which invalidates the trade — C0's low for a real signal, and
// for a matched control the level that reproduces the SAME percentage risk
// from its own entry. ok is false when the series has no room to enter.
func Simulate(b []bars.Bar, s Setup, entryIdx int, stopLevel float64, pol ExitPolicy, sc SimContext, costBps float64) (Trade, bool) {
	if entryIdx <= 0 || entryIdx >= len(b) {
		return Trade{}, false
	}
	entry := b[entryIdx].Open
	if entry <= 0 {
		return Trade{}, false
	}
	risk := entry - stopLevel
	if pol.FixedStopPct > 0 {
		// A fixed percentage stop replaces the structural one entirely, so
		// the two are never silently combined into whichever is nearer.
		stopLevel = entry * (1 - pol.FixedStopPct)
		risk = entry - stopLevel
	}
	target := math.Inf(1)
	if pol.TargetR > 0 && risk > 0 {
		target = entry + pol.TargetR*risk
	}
	if pol.TargetPct > 0 {
		target = math.Min(target, entry*(1+pol.TargetPct))
	}
	if pol.SwingTarget == 1 && s.PriorSwingHigh1 > 0 {
		target = math.Min(target, s.PriorSwingHigh1)
	}
	if pol.SwingTarget == 2 && s.PriorSwingHigh2 > 0 {
		target = math.Min(target, s.PriorSwingHigh2)
	}
	useStop := pol.UseC0Stop || pol.FixedStopPct > 0
	useTarget := !math.IsInf(target, 1)

	hh, ll := entry, entry
	trail := math.Inf(-1)
	exitIdx, reason := -1, ""

	for k := entryIdx; k < len(b); k++ {
		if b[k].High > hh {
			hh = b[k].High
		}
		if b[k].Low < ll {
			ll = b[k].Low
		}

		held := k - entryIdx + 1
		c := b[k].Close

		switch {
		case useStop && c < stopLevel:
			reason = "stop"
		case useTarget && c >= target:
			reason = "target"
		case pol.TrailATR > 0 && !math.IsInf(trail, -1) && c < trail:
			reason = "trail"
		case pol.OnReversal && k < len(sc.BearReversal) && sc.BearReversal[k]:
			reason = "reversal"
		case pol.MaxBars > 0 && held >= pol.MaxBars:
			reason = "time"
		}
		if pol.TrailATR > 0 && k < len(sc.ATR) && !math.IsNaN(sc.ATR[k]) {
			if lvl := hh - pol.TrailATR*sc.ATR[k]; lvl > trail {
				trail = lvl
			}
		}
		if reason != "" {
			exitIdx = k + 1 // fill at the next open
			break
		}
	}

	var exitPx float64
	var exitDate time.Time
	var barsHeld int
	if exitIdx < 0 || exitIdx >= len(b) {
		// Ran off the end of the data: the trade is unresolved, not a win.
		last := len(b) - 1
		exitPx, exitDate, barsHeld = b[last].Close, b[last].Date, last-entryIdx+1
		if reason == "" {
			reason = "truncated"
		}
	} else {
		exitPx, exitDate, barsHeld = b[exitIdx].Open, b[exitIdx].Date, exitIdx-entryIdx
	}

	gross := (exitPx - entry) / entry
	t := Trade{
		Symbol:    s.Symbol,
		EntryDate: b[entryIdx].Date, ExitDate: exitDate,
		EntryPx: entry, ExitPx: exitPx,
		BarsHeld: barsHeld,
		GrossRet: gross,
		NetRet:   gross - costBps/10000.0,
		Reason:   reason,
		MFEPct:   (hh - entry) / entry,
		MAEPct:   (ll - entry) / entry,
		Setup:    s,
	}
	if risk > 0 {
		t.RiskPct = risk / entry
	}
	if s.C0 >= 0 && s.C0 < len(b) {
		t.C0Date = b[s.C0].Date
	}
	if s.C0+1 < len(b) {
		t.ConfirmDate = b[s.C0+1].Date
	}
	return t, true
}

// SignalStop is the invalidation level of a real signal: C0's low.
func SignalStop(b []bars.Bar, s Setup) float64 { return b[s.C0].Low }

// MatchedControl draws a control trade for a real signal: the SAME symbol,
// the SAME exit policy and the SAME percentage stop distance, entered on a
// random nearby date that is not itself the signal. Law 3 — a pattern that
// wins 40% of the time means nothing until you know what an arbitrary entry
// in the same stock over the same stretch of market did. window is the
// half-width, in bars, of the draw.
//
// The stop must be matched in PERCENTAGE terms, not carried over as a price.
// An earlier version of this reused C0's low as an absolute level for the
// control too; because the control enters on a different date the level then
// sat at an arbitrary distance from its own entry, which stopped the control
// out 88% of the time and manufactured a large fake edge for the pattern.
func MatchedControl(b []bars.Bar, s Setup, pol ExitPolicy, sc SimContext, costBps float64, window int, rng *rand.Rand) (Trade, bool) {
	realEntry := s.Confirm + 1
	if realEntry >= len(b) {
		return Trade{}, false
	}
	signalEntry := b[realEntry].Open
	if signalEntry <= 0 {
		return Trade{}, false
	}
	riskFrac := (signalEntry - b[s.C0].Low) / signalEntry

	lo := max(1, realEntry-window)
	hi := min(len(b)-1, realEntry+window)
	if hi <= lo {
		return Trade{}, false
	}
	for attempt := 0; attempt < 20; attempt++ {
		idx := lo + rng.Intn(hi-lo)
		if abs(idx-realEntry) <= 3 {
			continue
		}
		return Simulate(b, s, idx, b[idx].Open*(1-riskFrac), pol, sc, costBps)
	}
	return Trade{}, false
}

// ATR is Wilder's average true range, NaN until period bars are available.
func ATR(b []bars.Bar, period int) []float64 {
	out := make([]float64, len(b))
	if len(b) == 0 {
		return out
	}
	out[0] = math.NaN()
	var sum, prev float64
	for i := 1; i < len(b); i++ {
		tr := math.Max(b[i].High-b[i].Low,
			math.Max(math.Abs(b[i].High-b[i-1].Close), math.Abs(b[i].Low-b[i-1].Close)))
		switch {
		case i < period:
			sum += tr
			out[i] = math.NaN()
		case i == period:
			sum += tr
			prev = sum / float64(period)
			out[i] = prev
		default:
			prev = (prev*float64(period-1) + tr) / float64(period)
			out[i] = prev
		}
	}
	return out
}

// MedianTurnover is retained here as the name the pattern code reads; the
// implementation moved to internal/bars once a second research command needed
// the same liquidity filter.
func MedianTurnover(b []bars.Bar, window int) []float64 {
	return bars.MedianTurnover(b, window)
}

func abs(x int) int {
	if x < 0 {
		return -x
	}
	return x
}

// LowerBandTouches returns every bar that touched or crossed the lower
// Bollinger band — the "important" condition WITHOUT the three-bar pattern.
//
// This exists to answer the question that decides whether the pattern is
// real. The tagged results show COI setups at the lower band beating a
// random-date control. But the control was not matched on being oversold, so
// that edge is equally consistent with "buying a stock 2 sigma below its
// 20-day mean pays" — a much older and much better documented effect than
// any three-bar candle formation. Using these bars as the control holds the
// oversold condition fixed and isolates what the pattern itself adds.
func LowerBandTouches(b []bars.Bar, ind Indicators) []int {
	var out []int
	for i := range b {
		bb := ind.BBLower[i]
		if math.IsNaN(bb) || !b[i].Valid() {
			continue
		}
		bodyLow := math.Min(b[i].Open, b[i].Close)
		bodyHigh := math.Max(b[i].Open, b[i].Close)
		if (b[i].Low <= bb && bb <= b[i].High) || (bodyLow <= bb && bb <= bodyHigh) {
			out = append(out, i)
		}
	}
	return out
}

// ControlFrom draws a control entry from a supplied set of candidate bar
// indices near the signal, matching the signal's percentage stop distance.
// candidates must be sorted ascending.
func ControlFrom(b []bars.Bar, s Setup, candidates []int, pol ExitPolicy, sc SimContext, costBps float64, window int, rng *rand.Rand) (Trade, bool) {
	realEntry := s.Confirm + 1
	if realEntry >= len(b) || b[realEntry].Open <= 0 {
		return Trade{}, false
	}
	riskFrac := (b[realEntry].Open - b[s.C0].Low) / b[realEntry].Open

	lo := sort.SearchInts(candidates, realEntry-window)
	hi := sort.SearchInts(candidates, realEntry+window)
	if hi <= lo {
		return Trade{}, false
	}
	for attempt := 0; attempt < 30; attempt++ {
		c := candidates[lo+rng.Intn(hi-lo)]
		// The band touch plays C0's role, so the control waits the same
		// number of bars from event to fill that the signal does.
		idx := c + (realEntry - s.C0)
		if idx <= 0 || idx >= len(b) || abs(idx-realEntry) <= 3 {
			continue
		}
		return Simulate(b, s, idx, b[idx].Open*(1-riskFrac), pol, sc, costBps)
	}
	return Trade{}, false
}

// SimContext carries the per-symbol series a simulation may need, so adding a
// new exit mechanism does not mean threading another parameter through every
// call site.
type SimContext struct {
	ATR []float64
	// BearReversal[i] is true when a BEARISH three-bar reversal confirms at
	// bar i — the exact mirror of the entry pattern.
	BearReversal []bool
}

// BearishReversals marks the bars at whose close a bearish three-bar reversal
// is confirmed: an up candle, then a bar making a higher high and higher low,
// then a bar making a lower high and lower low that closes below the middle
// bar's low.
//
// This is the mirror image of the entry signal, and it is the "exit when the
// pattern says the move is over" rule — the natural exit the original
// screener never wrote down. Being the mirror matters: any other exit would
// mix two different theories of what a reversal is.
func BearishReversals(b []bars.Bar) []bool {
	out := make([]bool, len(b))
	for i := 1; i < len(b)-1; i++ {
		cm1, c0, cp1 := b[i-1], b[i], b[i+1]
		if !cm1.Valid() || !c0.Valid() || !cp1.Valid() {
			continue
		}
		if cm1.Close > cm1.Open &&
			c0.High > cm1.High && c0.Low > cm1.Low &&
			cp1.High < c0.High && cp1.Low < c0.Low &&
			cp1.Close < c0.Low {
			out[i+1] = true // knowable only at the close of the third bar
		}
	}
	return out
}
