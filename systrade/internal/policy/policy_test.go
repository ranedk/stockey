package policy

import (
	"math"
	"math/rand"
	"testing"
	"time"
)

// synth builds a two-branch history of nPer periods of perLen trading days,
// each branch's daily return given by a function of the period index.
func synth(nPer, perLen int, r0, r1 func(k int) float64) History {
	h := History{Daily: [][]float64{{}, {}}}
	day := time.Date(2013, 7, 1, 0, 0, 0, 0, time.UTC)
	for k := 0; k < nPer; k++ {
		h.Rebalance = append(h.Rebalance, len(h.Daily[0]))
		for i := 0; i < perLen; i++ {
			h.Daily[0] = append(h.Daily[0], r0(k))
			h.Daily[1] = append(h.Daily[1], r1(k))
			h.Dates = append(h.Dates, day)
			day = day.AddDate(0, 0, 1)
		}
	}
	return h
}

func constant(v float64) func(int) float64 { return func(int) float64 { return v } }

func TestPeriodsCompound(t *testing.T) {
	h := synth(3, 5, constant(0.01), constant(0))
	per := h.Periods()
	want := math.Pow(1.01, 5) - 1
	for k := 0; k < 3; k++ {
		if math.Abs(per[0][k]-want) > 1e-12 {
			t.Fatalf("period %d: got %v want %v", k, per[0][k], want)
		}
		if per[1][k] != 0 {
			t.Fatalf("period %d branch 1: got %v want 0", k, per[1][k])
		}
	}
}

func TestHedgeFirstDecisionIsTheIncumbent(t *testing.T) {
	h := synth(4, 5, constant(0.01), constant(0))
	w, err := Hedge{HalfLife: 12}.Weights(h)
	if err != nil {
		t.Fatal(err)
	}
	if w[0] != Incumbent {
		t.Fatalf("first weight %v, want the incumbent %v — nothing has been observed yet", w[0], Incumbent)
	}
}

// The declared scale (HedgeScale = 1%/period) fixes what a given advantage
// buys: one percent a month ahead is exp(1) times the weight, a 73/27 split.
func TestHedgeScaleIsTheDeclaredOne(t *testing.T) {
	// One percent per PERIOD, delivered as a single day's return per period.
	h := synth(200, 1, constant(0.01), constant(0))
	w, err := Hedge{HalfLife: 12}.Weights(h)
	if err != nil {
		t.Fatal(err)
	}
	want := 1 / (1 + math.Exp(-1))
	if got := w[len(w)-1]; math.Abs(got-want) > 1e-6 {
		t.Fatalf("settled weight %.4f, want %.4f", got, want)
	}
}

func TestHedgeChasesTheRecentWinner(t *testing.T) {
	// Branch 1 wins for the first half, branch 0 for the second.
	flip := 60
	h := synth(120, 21,
		func(k int) float64 { return map[bool]float64{true: 0.001, false: 0}[k >= flip] },
		func(k int) float64 { return map[bool]float64{true: 0.001, false: 0}[k < flip] })
	fast, err := Hedge{HalfLife: 6}.Weights(h)
	if err != nil {
		t.Fatal(err)
	}
	slow, err := Hedge{HalfLife: 48}.Weights(h)
	if err != nil {
		t.Fatal(err)
	}
	if fast[flip-1] >= 0.5 {
		t.Fatalf("before the flip the fast policy should favour branch 1, got %.3f", fast[flip-1])
	}
	if fast[len(fast)-1] <= 0.5 {
		t.Fatalf("after the flip the fast policy should favour branch 0, got %.3f", fast[len(fast)-1])
	}
	// Ten periods after the flip the short memory must have moved further.
	i := flip + 10
	if !(fast[i] > slow[i]) {
		t.Fatalf("half-life 6 (%.3f) should adapt faster than half-life 48 (%.3f)", fast[i], slow[i])
	}
}

func TestRiskParityEqualisesRisk(t *testing.T) {
	rng := rand.New(rand.NewSource(7))
	n := 600
	h := History{Daily: [][]float64{make([]float64, n), make([]float64, n)}}
	day := time.Date(2013, 7, 1, 0, 0, 0, 0, time.UTC)
	for i := 0; i < n; i++ {
		h.Daily[0][i] = 0.02 * rng.NormFloat64() // twice as volatile
		h.Daily[1][i] = 0.01 * rng.NormFloat64()
		h.Dates = append(h.Dates, day)
		day = day.AddDate(0, 0, 1)
	}
	for k := 0; k*21 < n; k++ {
		h.Rebalance = append(h.Rebalance, k*21)
	}
	w, err := RiskParity{Window: 252}.Weights(h)
	if err != nil {
		t.Fatal(err)
	}
	// Weight proportional to 1/vol: the twice-as-volatile branch gets a third.
	last := w[len(w)-1]
	if math.Abs(last-1.0/3.0) > 0.05 {
		t.Fatalf("risk parity weight %.3f, want about 0.333 for a branch twice as volatile", last)
	}
}

func TestRiskParityFallsBackBeforeItsWindow(t *testing.T) {
	h := synth(30, 21, constant(0.001), constant(0.002))
	w, err := RiskParity{Window: 252}.Weights(h)
	if err != nil {
		t.Fatal(err)
	}
	if w[0] != Incumbent {
		t.Fatalf("first weight %v, want the incumbent while the window is unfilled", w[0])
	}
	// Constant returns have zero volatility: the policy must refuse to divide
	// by it rather than produce a weight from nothing.
	for k, v := range w {
		if v != Incumbent {
			t.Fatalf("weight %d is %v on zero-volatility branches, want the incumbent", k, v)
		}
	}
}

// A policy may read only what was knowable at its decision. Perturbing every
// daily return from a rebalance onward must leave the weights up to and
// including that rebalance untouched.
func TestNoLookAhead(t *testing.T) {
	rng := rand.New(rand.NewSource(11))
	n, per := 1260, 21
	h := History{Daily: [][]float64{make([]float64, n), make([]float64, n)}}
	day := time.Date(2013, 7, 1, 0, 0, 0, 0, time.UTC)
	for i := 0; i < n; i++ {
		h.Daily[0][i] = 0.015*rng.NormFloat64() + 0.0004
		h.Daily[1][i] = 0.009 * rng.NormFloat64()
		h.Dates = append(h.Dates, day)
		day = day.AddDate(0, 0, 1)
	}
	for k := 0; k*per < n; k++ {
		h.Rebalance = append(h.Rebalance, k*per)
	}
	policies := []Policy{Hedge{HalfLife: 6}, Hedge{HalfLife: 48}, RiskParity{Window: 63}, RiskParity{Window: 252}}
	for _, p := range policies {
		base, err := p.Weights(h)
		if err != nil {
			t.Fatal(err)
		}
		for _, k := range []int{5, 20, 40} {
			alt := History{Dates: h.Dates, Rebalance: h.Rebalance,
				Daily: [][]float64{append([]float64(nil), h.Daily[0]...), append([]float64(nil), h.Daily[1]...)}}
			for i := h.Rebalance[k]; i < n; i++ {
				alt.Daily[0][i] = 0.05 * rng.NormFloat64()
				alt.Daily[1][i] = -0.05 * rng.NormFloat64()
			}
			got, err := p.Weights(alt)
			if err != nil {
				t.Fatal(err)
			}
			for j := 0; j <= k; j++ {
				if math.Abs(got[j]-base[j]) > 1e-12 {
					t.Fatalf("%s: weight %d changed (%v -> %v) when only later returns moved",
						p.Name(), j, base[j], got[j])
				}
			}
		}
	}
}

// ridgeHistory plants a signal: the feature predicts the NEXT period's
// difference of branch returns with the given strength, plus noise.
func ridgeHistory(t *testing.T, nPer int, strength float64, seed int64) History {
	t.Helper()
	rng := rand.New(rand.NewSource(seed))
	per := 21
	h := History{Daily: [][]float64{{}, {}}}
	day := time.Date(2013, 7, 1, 0, 0, 0, 0, time.UTC)
	feats := make([]float64, nPer)
	for k := range feats {
		feats[k] = rng.NormFloat64()
	}
	for k := 0; k < nPer; k++ {
		h.Rebalance = append(h.Rebalance, len(h.Daily[0]))
		// This period's difference is driven by the PREVIOUS period's feature.
		var drift float64
		if k > 0 {
			drift = strength * feats[k-1] / float64(per)
		}
		for i := 0; i < per; i++ {
			h.Daily[0] = append(h.Daily[0], drift+0.004*rng.NormFloat64())
			h.Daily[1] = append(h.Daily[1], 0.004*rng.NormFloat64())
			h.Dates = append(h.Dates, day)
			day = day.AddDate(0, 0, 1)
		}
		h.Features = append(h.Features, []float64{feats[k]})
	}
	return h
}

func foldsOver(h History, minFitYears, stepYears int) []Fold {
	start := h.Dates[0]
	end := h.Dates[len(h.Dates)-1]
	var out []Fold
	fitEnd := start.AddDate(minFitYears, 0, 0)
	for {
		valEnd := fitEnd.AddDate(stepYears, 0, 0)
		if valEnd.After(end) {
			valEnd = end
		}
		if !fitEnd.Before(valEnd) {
			break
		}
		out = append(out, Fold{FitEnd: fitEnd.AddDate(0, 0, -31), ValStart: fitEnd, ValEnd: valEnd})
		if valEnd.Equal(end) {
			break
		}
		fitEnd = valEnd
	}
	return out
}

func TestRidgeHoldsTheIncumbentBeforeItsFirstFold(t *testing.T) {
	h := ridgeHistory(t, 200, 0.02, 3)
	folds := foldsOver(h, 3, 1)
	w, err := Ridge{Lambda: 1, Folds: folds}.Weights(h)
	if err != nil {
		t.Fatal(err)
	}
	for k, at := range h.Rebalance {
		if h.Dates[at].Before(folds[0].ValStart) && w[k] != Incumbent {
			t.Fatalf("decision %d is before the first fold and got %v, want the incumbent", k, w[k])
		}
	}
}

func TestRidgeFindsAPlantedSignal(t *testing.T) {
	h := ridgeHistory(t, 300, 0.03, 5)
	folds := foldsOver(h, 3, 1)
	w, err := Ridge{Lambda: 1, Folds: folds}.Weights(h)
	if err != nil {
		t.Fatal(err)
	}
	// Out of sample, the weight must move WITH the planted driver.
	var n, agree float64
	for k, at := range h.Rebalance {
		if h.Dates[at].Before(folds[0].ValStart) {
			continue
		}
		n++
		if (w[k]-Incumbent)*h.Features[k][0] > 0 {
			agree++
		}
	}
	if n < 20 {
		t.Fatalf("only %v out-of-sample decisions to judge", n)
	}
	if agree/n < 0.8 {
		t.Fatalf("weight agreed with the planted feature %.0f%% of the time, want 80%%+", 100*agree/n)
	}
}

// The null: a feature that predicts nothing must leave no systematic tilt.
// It will still swing the weight around — that is the policy honestly trading
// noise — but the average must stay at the incumbent.
func TestRidgeOnNoiseKeepsNoSystematicTilt(t *testing.T) {
	var sum, n float64
	for seed := int64(1); seed <= 30; seed++ {
		h := ridgeHistory(t, 300, 0, seed)
		folds := foldsOver(h, 3, 1)
		w, err := Ridge{Lambda: 1, Folds: folds}.Weights(h)
		if err != nil {
			t.Fatal(err)
		}
		for k, at := range h.Rebalance {
			if h.Dates[at].Before(folds[0].ValStart) {
				continue
			}
			sum, n = sum+w[k], n+1
		}
	}
	if got := sum / n; math.Abs(got-Incumbent) > 0.02 {
		t.Fatalf("mean out-of-sample weight on pure noise %.4f, want the incumbent %.2f", got, Incumbent)
	}
}

func TestRidgeRespectsTheTiltBounds(t *testing.T) {
	h := ridgeHistory(t, 300, 0.5, 9) // an absurdly strong signal
	w, err := Ridge{Lambda: 1, Folds: foldsOver(h, 3, 1)}.Weights(h)
	if err != nil {
		t.Fatal(err)
	}
	for k, v := range w {
		if v < TiltFloor-1e-12 || v > TiltCeil+1e-12 {
			t.Fatalf("weight %d is %v, outside [%v, %v]", k, v, TiltFloor, TiltCeil)
		}
	}
}

// The time-shifted twin must read stale features: lagging by one period turns
// the planted signal into the previous period's, which predicts nothing.
func TestRidgeLaggedControlLosesThePlantedSignal(t *testing.T) {
	h := ridgeHistory(t, 300, 0.03, 5)
	folds := foldsOver(h, 3, 1)
	real, err := Ridge{Lambda: 1, Folds: folds}.Weights(h)
	if err != nil {
		t.Fatal(err)
	}
	lagged, err := Ridge{Lambda: 1, Folds: folds, LagPeriods: 1}.Weights(h)
	if err != nil {
		t.Fatal(err)
	}
	agree := func(w []float64) float64 {
		var n, a float64
		for k, at := range h.Rebalance {
			if h.Dates[at].Before(folds[0].ValStart) {
				continue
			}
			n++
			if (w[k]-Incumbent)*h.Features[k][0] > 0 {
				a++
			}
		}
		return a / n
	}
	if agree(lagged) >= agree(real)-0.2 {
		t.Fatalf("lagged control agreed %.2f vs the real policy's %.2f — the control is not stale enough",
			agree(lagged), agree(real))
	}
}

func TestBlendConstantWeightIsAPlainSum(t *testing.T) {
	br := []Branch{
		{Gross: []float64{0.01, 0.02, -0.01}, Net: []float64{0.009, 0.019, -0.011}, Turnover: []float64{0.2, 0, 0}},
		{Gross: []float64{0.00, 0.01, 0.02}, Net: []float64{-0.001, 0.009, 0.019}, Turnover: []float64{0.1, 0, 0}},
	}
	b, err := Blend(br, []float64{0.48, 0.48}, []int{0, 2}, 50)
	if err != nil {
		t.Fatal(err)
	}
	for i := range b.Net {
		want := 0.48*br[0].Net[i] + 0.52*br[1].Net[i]
		if math.Abs(b.Net[i]-want) > 1e-15 {
			t.Fatalf("day %d net %v, want %v — a constant split trades nothing of its own", i, b.Net[i], want)
		}
		if b.ReallocCost[i] != 0 {
			t.Fatalf("day %d charged %v for a split that never moved", i, b.ReallocCost[i])
		}
	}
}

func TestBlendChargesMovingTheSplit(t *testing.T) {
	zeros := make([]float64, 4)
	br := []Branch{{Gross: zeros, Net: append([]float64(nil), zeros...), Turnover: append([]float64(nil), zeros...)},
		{Gross: zeros, Net: append([]float64(nil), zeros...), Turnover: append([]float64(nil), zeros...)}}
	// Move ten points of capital at the second rebalance, at 50 bps round trip.
	b, err := Blend(br, []float64{0.48, 0.58}, []int{0, 2}, 50)
	if err != nil {
		t.Fatal(err)
	}
	want := 0.10 * 50 / 10000 // 5 bps
	if math.Abs(b.ReallocCost[2]-want) > 1e-15 {
		t.Fatalf("reallocation cost %v, want %v", b.ReallocCost[2], want)
	}
	if math.Abs(b.Net[2]+want) > 1e-15 {
		t.Fatalf("net %v, want %v: the move is the only thing that happened", b.Net[2], -want)
	}
	// Turnover is the sleeve convention: selling 10% and buying 10% reads 0.2.
	if math.Abs(b.Turnover[2]-0.2) > 1e-15 {
		t.Fatalf("turnover %v, want 0.2", b.Turnover[2])
	}
	if math.Abs(b.WeightTurnover-0.10) > 1e-15 {
		t.Fatalf("mean weight turnover %v, want 0.10", b.WeightTurnover)
	}
}

func TestBlendRejectsMisalignedInput(t *testing.T) {
	br := []Branch{{Net: []float64{0, 0}, Gross: []float64{0, 0}, Turnover: []float64{0, 0}},
		{Net: []float64{0}, Gross: []float64{0}, Turnover: []float64{0}}}
	if _, err := Blend(br, []float64{0.5}, []int{0}, 50); err == nil {
		t.Fatal("misaligned branches should be an error, not a silent truncation")
	}
}

func TestFixedIsTheIncumbent(t *testing.T) {
	h := synth(10, 21, constant(0.001), constant(0.001))
	w, err := Fixed{W: Incumbent, Label: "handcrafted 48/52"}.Weights(h)
	if err != nil {
		t.Fatal(err)
	}
	for _, v := range w {
		if v != Incumbent {
			t.Fatalf("got %v, want %v", v, Incumbent)
		}
	}
}
