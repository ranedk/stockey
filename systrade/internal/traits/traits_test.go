package traits

import (
	"math"
	"math/rand"
	"testing"
)

func near(t *testing.T, what string, got, want, tol float64) {
	t.Helper()
	if math.Abs(got-want) > tol {
		t.Errorf("%s = %.4f, want %.4f", what, got, want)
	}
}

func TestBetaIdioRecoversAKnownRegression(t *testing.T) {
	rng := rand.New(rand.NewSource(1))
	n := 2000
	mkt, ret := make([]float64, n), make([]float64, n)
	for i := range mkt {
		mkt[i] = rng.NormFloat64() * 0.01
		ret[i] = 0.0002 + 1.5*mkt[i] + rng.NormFloat64()*0.02
	}
	ret[10] = math.NaN() // a missing day is skipped, not fatal
	b, idio, ok := BetaIdio(ret, mkt, n-1, n, 1500)
	if !ok {
		t.Fatal("no estimate")
	}
	near(t, "beta", b, 1.5, 0.1)
	near(t, "idiosyncratic vol", idio, 0.02, 0.001)
	if _, _, ok := BetaIdio(ret, mkt, 100, 250, 200); ok {
		t.Error("a window reaching before the start of history produced an estimate")
	}
}

func TestDistFromHighAndAmihudByHand(t *testing.T) {
	high := []float64{10, 12, 11, 9}
	close := []float64{9, 11, 10, 9}
	near(t, "25% below", DistFromHigh(high, close, 3, 4), 9.0/12-1, 1e-12)
	ret := []float64{math.NaN(), 0.02, -0.01, 0.03}
	value := []float64{0, 1e6, 2e6, 0}
	near(t, "amihud", Amihud(ret, value, 3, 4, 2), (0.02/1e6+0.01/2e6)/2, 1e-18)
	if !math.IsNaN(Amihud(ret, value, 3, 4, 3)) {
		t.Error("too few trading days should give no value")
	}
}

func TestMedianAndSpearman(t *testing.T) {
	near(t, "median", Median([]float64{5, math.NaN(), 1, 3, 2}, 4, 5, 3), 2.5, 1e-12)
	a := []float64{1, 2, 3, 4, 5}
	near(t, "monotone", Spearman(a, []float64{1, 4, 9, 16, 25}), 1, 1e-12)
	near(t, "reversed", Spearman(a, []float64{5, 4, 3, 2, 1}), -1, 1e-12)
	near(t, "ties", Spearman([]float64{1, 1, 2, 3}, []float64{1, 1, 2, 3}), 1, 1e-12)
}

func panel(seed int64, days, names int, value func(rng *rand.Rand, name, day int) float64, delists func(name int) bool) []Sample {
	rng := rand.New(rand.NewSource(seed))
	out := make([]Sample, days)
	for d := range out {
		for i := 0; i < names; i++ {
			out[d] = append(out[d], Row{Sym: string(rune('A'+i%26)) + string(rune('a'+i/26)), Delists: delists(i), Values: []float64{value(rng, i, d)}})
		}
	}
	return out
}

func TestPersistenceSeparatesAStableTraitFromNoise(t *testing.T) {
	never := func(int) bool { return false }
	stable := panel(1, 10, 200, func(rng *rand.Rand, name, _ int) float64 { return float64(name) + rng.Float64()*0.1 }, never)
	noisy := panel(2, 10, 200, func(rng *rand.Rand, _, _ int) float64 { return rng.Float64() }, never)
	if p := Persistence(stable, 1)[0]; p < 0.99 {
		t.Errorf("a stock's own stable level persisted only %.3f", p)
	}
	if p := Persistence(noisy, 1)[0]; math.Abs(p) > 0.1 {
		t.Errorf("pure noise persisted %.3f", p)
	}
}

func TestMissingShareExposesGapsThatFollowTheFailures(t *testing.T) {
	delists := func(name int) bool { return name < 50 }
	p := panel(3, 5, 200, func(_ *rand.Rand, name, _ int) float64 {
		if name < 50 {
			return math.NaN() // the trait is missing for exactly the names that later delist
		}
		return 1
	}, delists)
	all, dl, sv := MissingShare(p, 1)
	near(t, "overall", all[0], 0.25, 1e-12)
	near(t, "delisting", dl[0], 1, 1e-12)
	near(t, "surviving", sv[0], 0, 1e-12)
}

func TestOverlapFindsDuplicateTraits(t *testing.T) {
	rng := rand.New(rand.NewSource(4))
	var samples []Sample
	for d := 0; d < 5; d++ {
		var s Sample
		for i := 0; i < 300; i++ {
			x := rng.NormFloat64()
			s = append(s, Row{Sym: "s", Values: []float64{x, math.Exp(x), rng.NormFloat64()}})
		}
		samples = append(samples, s)
	}
	o := Overlap(samples, 3)
	near(t, "a monotone transform is a duplicate", o[0][1], 1, 1e-12)
	if math.Abs(o[0][2]) > 0.15 {
		t.Errorf("independent traits overlap %.3f", o[0][2])
	}
}

func TestCountOverATrailingWindow(t *testing.T) {
	hits := []float64{0, 1, 0, 1, 1, -1, 0}
	near(t, "upper hits in the last 5", Count(hits, 1, 6, 5), 2, 0)
	near(t, "lower hits in the last 5", Count(hits, -1, 6, 5), 1, 0)
	if !math.IsNaN(Count(hits, 1, 2, 5)) {
		t.Error("a window reaching before the series start must not count as zero hits")
	}
}

func TestRollingFormsMatchTheirPointForms(t *testing.T) {
	rng := rand.New(rand.NewSource(8))
	n := 700
	mkt, ret, high, close, x, hits := make([]float64, n), make([]float64, n), make([]float64, n), make([]float64, n), make([]float64, n), make([]float64, n)
	p := 100.0
	for i := 0; i < n; i++ {
		mkt[i] = rng.NormFloat64() * 0.01
		ret[i] = 0.8*mkt[i] + rng.NormFloat64()*0.015
		if i%37 == 0 {
			ret[i], mkt[i] = math.NaN(), math.NaN() // gaps must be skipped identically
		}
		p *= 1 + rng.NormFloat64()*0.02
		close[i], high[i] = p, p*(1+rng.Float64()*0.02)
		x[i] = rng.Float64() * 100
		if i%11 == 0 {
			x[i] = math.NaN()
		}
		hits[i] = float64(rng.Intn(3) - 1)
	}
	rb, ri := RollingBetaIdio(ret, mkt, 252, 200)
	dh := RollingDistFromHigh(high, close, 252)
	md := RollingMedian(x, 20, 15)
	up := RollingCount(hits, 1, 60)
	for i := 0; i < n; i++ {
		b, idio, ok := BetaIdio(ret, mkt, i, 252, 200)
		if !ok {
			if !math.IsNaN(rb[i]) {
				t.Fatalf("i=%d: rolling beta %v where the point form has none", i, rb[i])
			}
		} else {
			near(t, "beta", rb[i], b, 1e-9)
			near(t, "idio", ri[i], idio, 1e-9)
		}
		if want := DistFromHigh(high, close, i, 252); !(math.IsNaN(want) && math.IsNaN(dh[i])) {
			near(t, "dist from high", dh[i], want, 1e-12)
		}
		if want := Median(x, i, 20, 15); !(math.IsNaN(want) && math.IsNaN(md[i])) {
			near(t, "median", md[i], want, 0)
		}
		if want := Count(hits, 1, i, 60); !(math.IsNaN(want) && math.IsNaN(up[i])) {
			near(t, "count", up[i], want, 0)
		}
	}
}
