package backtest

import (
	"math"
	"math/rand"
	"strings"
	"testing"
	"time"

	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/data"
	"github.com/ranedk/systrader/internal/portfolio"
	"github.com/ranedk/systrader/internal/rules"
)

func days(n int) []time.Time {
	out := make([]time.Time, 0, n)
	t := time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC)
	for len(out) < n {
		if wd := t.Weekday(); wd != time.Saturday && wd != time.Sunday {
			out = append(out, t)
		}
		t = t.AddDate(0, 0, 1)
	}
	return out
}

func randomWalk(seed int64, n int, start, vol float64) core.Series {
	rng := rand.New(rand.NewSource(seed))
	v := make([]float64, n)
	p := start
	for i := range v {
		p *= 1 + vol*rng.NormFloat64()
		v[i] = p
	}
	return core.New(days(n), v)
}

func TestEWMAConstantSeries(t *testing.T) {
	n := 100
	vals := make([]float64, n)
	for i := range vals {
		vals[i] = 42.0
	}
	s := core.New(days(n), vals)
	e := core.EWMA(s, 10, 5)
	if got := e.Values[n-1]; math.Abs(got-42.0) > 1e-9 {
		t.Fatalf("EWMA of constant = %v, want 42", got)
	}
	sd := core.EWMAStd(s, 10, 5)
	if got := sd.Values[n-1]; got > 1e-9 {
		t.Fatalf("EWMAStd of constant = %v, want 0", got)
	}
	if !math.IsNaN(e.Values[2]) {
		t.Fatal("EWMA should be NaN during warm-up")
	}
}

func TestEWMACSignAndCap(t *testing.T) {
	n := 400
	vals := make([]float64, n)
	for i := range vals {
		vals[i] = 100 * math.Pow(1.004, float64(i)) // strong steady uptrend
	}
	inst := &data.Instrument{Meta: data.Meta{Symbol: "UP", PointValue: 1}, Prices: core.New(days(n), vals)}
	vol := core.PriceUnitVol(inst.Prices, 36, 10)
	f := rules.Forecast(rules.EWMAC{Fast: 16}, inst, vol)
	last, ok := f.Last()
	if !ok || last <= 0 {
		t.Fatalf("EWMAC on uptrend should be positive, got %v", last)
	}
	for _, v := range f.Values {
		if !math.IsNaN(v) && math.Abs(v) > rules.ForecastCap+1e-9 {
			t.Fatalf("forecast %v exceeds cap ±20", v)
		}
	}
}

func TestLongOnlyClipping(t *testing.T) {
	n := 400
	vals := make([]float64, n)
	for i := range vals {
		vals[i] = 100 * math.Pow(0.996, float64(i)) // downtrend
	}
	inst := &data.Instrument{Meta: data.Meta{Symbol: "DN", PointValue: 1, LongOnly: true},
		Prices: core.New(days(n), vals)}
	vol := core.PriceUnitVol(inst.Prices, 36, 10)
	f := rules.Forecast(rules.EWMAC{Fast: 16}, inst, vol)
	for _, v := range f.Values {
		if !math.IsNaN(v) && v < 0 {
			t.Fatalf("long-only forecast went negative: %v", v)
		}
	}
}

func TestPositionInertia(t *testing.T) {
	// Within 10% of target → hold.
	if got := portfolio.ApplyInertia(133, 133.48, 1); got != 133 {
		t.Fatalf("inertia should hold 133 vs target 133.48, got %v", got)
	}
	// Outside 10% → trade to rounded target.
	if got := portfolio.ApplyInertia(42, 50, 1); got != 50 {
		t.Fatalf("should trade 42→50, got %v", got)
	}
	// Zero target always closes.
	if got := portfolio.ApplyInertia(3, 0, 1); got != 0 {
		t.Fatalf("zero target should flatten, got %v", got)
	}
	// NaN target: hold, never trade on missing data.
	if got := portfolio.ApplyInertia(5, math.NaN(), 1); got != 5 {
		t.Fatalf("NaN target should hold, got %v", got)
	}
}

func demoConfig() Config {
	return Config{
		Capital: 5_000_000, VolTargetPct: 0.20, Compounding: true,
		Rules: []RuleSpec{
			{Rule: rules.EWMAC{Fast: 16}, Weight: 0.25},
			{Rule: rules.EWMAC{Fast: 32}, Weight: 0.25},
			{Rule: rules.EWMAC{Fast: 64}, Weight: 0.50},
		},
		FDM:               1.2,
		InstrumentWeights: map[string]float64{"A": 1.0},
		IDM:               1.0,
	}
}

func TestEngineRuns(t *testing.T) {
	inst := &data.Instrument{
		Meta:   data.Meta{Symbol: "A", PointValue: 10, Block: 1, SpreadPoints: 0.5, FeePerBlock: 5},
		Prices: randomWalk(7, 1000, 5000, 0.01),
	}
	res, err := Run(demoConfig(), []*data.Instrument{inst})
	if err != nil {
		t.Fatal(err)
	}
	if res.Metrics.Days != 1000 {
		t.Fatalf("expected 1000 days, got %d", res.Metrics.Days)
	}
	for _, v := range res.Daily.Values {
		if math.IsNaN(v) || math.IsInf(v, 0) {
			t.Fatal("engine produced NaN/Inf P&L")
		}
	}
	if res.Instruments["A"].CostCash < 0 {
		t.Fatal("negative costs")
	}
}

// The load-bearing test: changing tomorrow's price must not change today's
// position (Bible engineering corollary 3).
func TestNoLookAhead(t *testing.T) {
	base := randomWalk(11, 800, 5000, 0.01)
	bumped := core.New(base.Times, append([]float64(nil), base.Values...))
	bumped.Values[799] *= 1.10 // change ONLY the final day

	run := func(prices core.Series) []float64 {
		inst := &data.Instrument{
			Meta:   data.Meta{Symbol: "A", PointValue: 10, Block: 1},
			Prices: prices,
		}
		res, err := Run(demoConfig(), []*data.Instrument{inst})
		if err != nil {
			t.Fatal(err)
		}
		return res.Instruments["A"].Positions.Values
	}
	p1, p2 := run(base), run(bumped)
	for i := 0; i < 799; i++ { // every position BEFORE the mutated day
		same := p1[i] == p2[i] || (math.IsNaN(p1[i]) && math.IsNaN(p2[i]))
		if !same {
			t.Fatalf("LOOK-AHEAD: position at day %d changed (%v→%v) when only day 799 differs", i, p1[i], p2[i])
		}
	}
	if p1[799] == p2[799] {
		t.Log("note: final-day position unchanged (inertia may absorb the bump); acceptable")
	}
}

func TestBonferroniBar(t *testing.T) {
	cases := map[int]float64{1: 1.96, 10: 2.81, 100: 3.48, 1000: 4.06}
	for m, want := range cases {
		if got := BonferroniBar(m); math.Abs(got-want) > 0.02 {
			t.Fatalf("BonferroniBar(%d)=%.3f, want ≈%.2f", m, got, want)
		}
	}
}

func TestVolTargetLawEnforced(t *testing.T) {
	cfg := demoConfig()
	cfg.VolTargetPct = 0.8 // illegal per Law 10
	inst := &data.Instrument{Meta: data.Meta{Symbol: "A", PointValue: 1, Block: 1},
		Prices: randomWalk(3, 300, 100, 0.01)}
	if _, err := Run(cfg, []*data.Instrument{inst}); err == nil {
		t.Fatal("vol target 80% should be rejected (Law 10)")
	}
}

// BUG FOUND LIVE 2026-08-22 (code review): once compounding capital floored
// to 0, every subsequent day's "return" silently computed as exactly 0%
// (Go's zero value, never explicitly set) instead of being marked invalid —
// dragging Sharpe/vol/skew toward "flat and safe" instead of showing the
// blow-up. ComputeMetrics must now stop at the bust and say so.
func TestComputeMetrics_BustedStopsReturnComputationAtBust(t *testing.T) {
	capital := 1000.0
	times := days(4)
	pnl := []float64{-500, -500, 999, 999} // days 2-3 are unreachable post-bust
	eq := []float64{500, 0, 500, 1500}     // eq[1]=0 -> base for day 2 is <=0
	res := &Result{
		Daily:       core.New(times, pnl),
		Equity:      core.New(times, eq),
		Instruments: map[string]*InstrumentResult{},
	}

	m := ComputeMetrics(res, capital)

	if !m.Busted {
		t.Fatal("expected Busted=true")
	}
	if m.BustedDay != 2 {
		t.Fatalf("expected BustedDay=2 (base=eq[1]=0 at day 2), got %d", m.BustedDay)
	}
	// The pre-bust days are a real disaster (-50%, then -100% of what's
	// left) -- the OLD bug would have diluted this toward 0 with two extra
	// silent 0%-return days instead of stopping.
	if m.AnnReturnPct >= 0 {
		t.Fatalf("expected a strongly negative annualized return reflecting the bust, got %.1f%%", m.AnnReturnPct)
	}
	if math.Abs(m.MaxDDPct-100) > 0.01 {
		t.Fatalf("expected MaxDDPct=100 (eq hit exactly 0 against a 1000 peak), got %.2f", m.MaxDDPct)
	}
}

func TestComputeMetrics_NormalRunLeavesBustedFalse(t *testing.T) {
	inst := &data.Instrument{
		Meta:   data.Meta{Symbol: "A", PointValue: 10, Block: 1},
		Prices: randomWalk(7, 1000, 5000, 0.01),
	}
	res, err := Run(demoConfig(), []*data.Instrument{inst})
	if err != nil {
		t.Fatal(err)
	}
	if res.Metrics.Busted {
		t.Fatalf("a normal run should not be Busted, got BustedDay=%d", res.Metrics.BustedDay)
	}
	if res.Metrics.BustedDay != -1 {
		t.Fatalf("BustedDay should be -1 when never busted, got %d", res.Metrics.BustedDay)
	}
}

func TestReport_PrintsBustedWarning(t *testing.T) {
	m := Metrics{Days: 10, Busted: true, BustedDay: 3}
	report := m.Report(1)
	if !strings.Contains(report, "BUSTED at day 3") {
		t.Fatalf("Report() should surface the bust prominently, got:\n%s", report)
	}
}
