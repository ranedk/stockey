// Package stage implements Stan Weinstein's four-stage regime classification
// (Secrets for Profiting in Bull and Bear Markets, 1988) as a REPORTING-ONLY
// classifier.
//
// This is deliberately NOT a rules.Rule: it emits a discrete label, not a
// continuous vol-standardized forecast, and Part IV/corollary-1 of
// TRADING_BIBLE.md is explicit that "[n]othing else may influence
// positions." Ships as regime-tagged reporting (innovation #7 in
// docs/rule_ideas.md: "performance broken out by regime for diagnostics
// only — acting on it would be meddling," Law 16) until/unless it earns
// promotion to a real forecast through the normal admission protocol —
// which would mean recasting it as a continuous forecast, not wiring this
// discrete label directly into sizing.
//
// Story (Law 1): institutional accumulation and distribution take months to
// complete on price and volume before the move fully diffuses into price —
// the same under-reaction-to-information behavioral premium that funds
// EWMAC (Law 1's "behavioral biases (under-reaction → trends)" category),
// read on a slower, weekly clock instead of EWMAC's daily one.
//
// M-accounting (Law 2, research/LEDGER.md): every numeric parameter below
// (30-week MA, 4-week slope window, 1% flat threshold) is fixed ahead of any
// test against NSE data — none is fit or swept against this dataset. Per the
// ledger's treatment of rows 1-2 (EWMAC/carry: "adopted a priori... our
// backtests are verification, not selection"), implementing this classifier
// is logged the same way and does not by itself inflate M. The one number
// here that Weinstein's own book does NOT state numerically is the 4-week/
// 1% flat test — his language is the qualitative "the average flattens
// out." What's here is a documented, common practitioner reading of that
// phrase, chosen before looking at any NSE output, not tuned to it — flagged
// explicitly so a future sensitivity check knows exactly where discretion
// entered.
package stage

import (
	"math"
	"time"

	"github.com/ranedk/systrader/internal/core"
)

// Stage is Weinstein's four-way regime label.
type Stage int

const (
	// StageUnknown covers warm-up (fewer than MAWindowWeeks+SlopeWindowWeeks
	// weeks of history).
	StageUnknown Stage = iota
	Stage1Basing
	Stage2Advancing
	Stage3Topping
	Stage4Declining
)

func (s Stage) String() string {
	switch s {
	case Stage1Basing:
		return "Stage 1 (Basing)"
	case Stage2Advancing:
		return "Stage 2 (Advancing)"
	case Stage3Topping:
		return "Stage 3 (Topping)"
	case Stage4Declining:
		return "Stage 4 (Declining)"
	default:
		return "Unknown"
	}
}

const (
	MAWindowWeeks    = 30  // Weinstein's own published parameter
	SlopeWindowWeeks = 4   // common practitioner reading of "the average flattens out"
	FlatThresholdPct = 1.0 // trailing-4-week MA %-change below this counts as flat
	VolumeAvgWeeks   = 10  // trailing window for the descriptive VolumeRatio field
)

// Classification is one week's stage read plus the descriptive fields a
// human report would want. Only Stage/Close/MA30/MASlopePct feed the
// classification itself — VolumeRatio is diagnostic only (Weinstein treats
// volume as a CONFIRMATION signal on stage-2 breakouts, not part of the core
// 4-way split) and is NaN whenever no volume series was supplied.
type Classification struct {
	Time        time.Time
	Stage       Stage
	Close       float64
	MA30        float64
	MASlopePct  float64
	VolumeRatio float64
}

// Classify computes the weekly stage series from an already-weekly close
// series (core.ResampleWeeklyLast) and an optional already-weekly volume
// series (core.ResampleWeeklySum; pass nil if unavailable — VolumeRatio
// comes back NaN throughout, the stage classification itself is unaffected).
// weeklyVolume does NOT need to be pre-aligned to weeklyClose's dates —
// Classify runs it through core.AlignByTime internally, since the two are
// typically sourced from independently-filtered queries that can drop
// different dates (see AlignByTime's doc comment).
//
// Algorithm (a faithful reading of Weinstein ch. 2-3, not a novel invention):
//   - ma = 30-week SMA of weekly close.
//   - slope = % change of ma over the trailing 4 weeks.
//   - Stage 2 (Advancing): close > ma AND slope > +FlatThresholdPct — ma
//     clearly rising, price riding above it. The buy zone.
//   - Stage 4 (Declining): close < ma AND slope < -FlatThresholdPct — ma
//     clearly falling, price riding below it. The avoid/sell zone.
//   - Otherwise the ma is flat (|slope| <= FlatThresholdPct), or price and
//     the slope's sign disagree (a whipsaw week): this is a consolidation.
//     Weinstein's Stage 1 (basing, follows a decline) and Stage 3 (topping,
//     follows an advance) are indistinguishable from a single week's
//     snapshot — Weinstein always reads them relative to what came before.
//     Classify tracks the last CLEAR trend (Stage 2 or Stage 4) seen and
//     uses it to disambiguate: flat-after-decline -> Stage 1,
//     flat-after-advance -> Stage 3. Before any clear trend has been seen
//     (start of history), flat defaults to Stage 1 — "not yet trending" is
//     closer to basing than topping.
func Classify(weeklyClose core.Series, weeklyVolume *core.Series) []Classification {
	n := weeklyClose.Len()
	ma := core.SMA(weeklyClose, MAWindowWeeks)
	out := make([]Classification, n)

	// BUG FOUND LIVE 2026-08-21 (code review): used to gate on
	// weeklyVolume.Len() == n alone. Close and volume are typically fetched
	// via independent, differently-filtered DB queries (store.AdjustedCloses
	// vs store.AdjustedVolume) that can silently drop different dates -- two
	// series can have the SAME length while covering DIFFERENT weeks, which
	// a length check can't catch and which would silently mispair
	// VolumeRatio with the wrong week. core.AlignByTime re-keys by actual
	// date instead of trusting position.
	haveVol := weeklyVolume != nil && weeklyVolume.Len() > 0
	var alignedVol, volAvg core.Series
	if haveVol {
		alignedVol = core.AlignByTime(weeklyClose, *weeklyVolume)
		volAvg = core.SMA(alignedVol, VolumeAvgWeeks)
	}

	lastTrend := 0 // 0 = none yet, +1 = last clear trend was Stage 2, -1 = Stage 4
	for i := 0; i < n; i++ {
		c := Classification{
			Time:        weeklyClose.Times[i],
			Close:       weeklyClose.Values[i],
			MA30:        ma.Values[i],
			MASlopePct:  math.NaN(),
			VolumeRatio: math.NaN(),
		}

		if i >= SlopeWindowWeeks && !math.IsNaN(ma.Values[i]) && !math.IsNaN(ma.Values[i-SlopeWindowWeeks]) && ma.Values[i-SlopeWindowWeeks] != 0 {
			c.MASlopePct = (ma.Values[i] - ma.Values[i-SlopeWindowWeeks]) / ma.Values[i-SlopeWindowWeeks] * 100
		}
		if haveVol && !math.IsNaN(volAvg.Values[i]) && volAvg.Values[i] != 0 && !math.IsNaN(alignedVol.Values[i]) {
			c.VolumeRatio = alignedVol.Values[i] / volAvg.Values[i]
		}

		switch {
		case math.IsNaN(c.MA30) || math.IsNaN(c.MASlopePct):
			c.Stage = StageUnknown
		case c.Close > c.MA30 && c.MASlopePct > FlatThresholdPct:
			c.Stage = Stage2Advancing
			lastTrend = 1
		case c.Close < c.MA30 && c.MASlopePct < -FlatThresholdPct:
			c.Stage = Stage4Declining
			lastTrend = -1
		case lastTrend > 0:
			c.Stage = Stage3Topping
		default:
			c.Stage = Stage1Basing
		}
		out[i] = c
	}
	return out
}

// Latest returns the most recent classification, or the zero value and
// false if the series is empty.
func Latest(cs []Classification) (Classification, bool) {
	if len(cs) == 0 {
		return Classification{}, false
	}
	return cs[len(cs)-1], true
}

// DailyView classifies a daily series on weekly bars (last close, summed volume per ISO
// week) and returns, for each DAY, the classification of the last week completed at
// that day's close -- the day's own week only on its last bar in the series. Days before
// any classified week carry the zero value (StageUnknown). Used by the paper engine's
// Stage 2 filter (docs/strategies/2026-10-01_stage2_rs_leaders.md).
func DailyView(times []time.Time, closes, vols []float64) []Classification {
	n := len(times)
	out := make([]Classification, n)
	var wt []time.Time
	var wc, wv []float64
	var ends []int
	vol := 0.0
	for i := 0; i < n; i++ {
		if vols != nil && !math.IsNaN(vols[i]) {
			vol += vols[i]
		}
		last := i+1 >= n
		if !last {
			y, k := times[i].ISOWeek()
			y2, k2 := times[i+1].ISOWeek()
			last = y != y2 || k != k2
		}
		if last {
			wt, wc, wv, ends = append(wt, times[i]), append(wc, closes[i]), append(wv, vol), append(ends, i)
			vol = 0
		}
	}
	if len(wt) == 0 {
		return out
	}
	var vp *core.Series
	if vols != nil {
		v := core.New(wt, wv)
		vp = &v
	}
	cls := Classify(core.New(wt, wc), vp)
	k := -1
	for i := 0; i < n; i++ {
		for k+1 < len(ends) && ends[k+1] <= i {
			k++
		}
		if k >= 0 {
			out[i] = cls[k]
		}
	}
	return out
}
