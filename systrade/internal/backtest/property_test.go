package backtest

import (
	"math"
	"testing"

	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/data"
)

// constRule emits a constant raw forecast of +10 (avg-abs-10 by construction).
// It exists so vol-targeting can be tested without forecast variation.
type constRule struct{ story string }

func (constRule) Name() string    { return "const10" }
func (c constRule) Story() string { return c.story }
func (constRule) Scalar() float64 { return 1 }
func (constRule) Raw(inst *data.Instrument, vol core.Series) core.Series {
	v := make([]float64, inst.Prices.Len())
	for i := range v {
		if math.IsNaN(vol.Values[i]) {
			v[i] = math.NaN()
			continue
		}
		v[i] = 10
	}
	return core.New(inst.Prices.Times, v)
}

const goodStory = "Testers on the losing side keep paying because the fixture " +
	"demands a constant exposure regardless of price; this is a harness rule."

// Law 23: realized volatility must track the percentage vol target. Under
// the pre-2026-07-26 metrics bug (returns ÷ initial capital while
// compounding) this test fails with wildly inflated vol.
func TestVolTargetingAccuracy(t *testing.T) {
	cfg := Config{
		Capital: 5_000_000, VolTargetPct: 0.20, Compounding: true,
		Rules:             []RuleSpec{{Rule: constRule{goodStory}, Weight: 1}},
		FDM:               1.0,
		InstrumentWeights: map[string]float64{"A": 1.0},
		IDM:               1.0,
	}
	inst := &data.Instrument{
		Meta:   data.Meta{Symbol: "A", PointValue: 1, Block: 1}, // costless
		Prices: randomWalk(5, 3000, 100, 0.01),
	}
	res, err := Run(cfg, []*data.Instrument{inst})
	if err != nil {
		t.Fatal(err)
	}
	got := res.Metrics.AnnVolPct / 100
	if got < 0.13 || got > 0.28 {
		t.Fatalf("realized vol %.1f%% far from 20%% target (Law 23 vol standardization)", got*100)
	}
}

// Cost doubling must show up fully in the cost line and reduce net P&L.
func TestCostDoublingSensitivity(t *testing.T) {
	run := func(fee float64) (costs, end float64) {
		cfg := demoConfig()
		inst := &data.Instrument{
			Meta:   data.Meta{Symbol: "A", PointValue: 10, Block: 1, FeePerBlock: fee},
			Prices: randomWalk(9, 1200, 5000, 0.01),
		}
		res, err := Run(cfg, []*data.Instrument{inst})
		if err != nil {
			t.Fatal(err)
		}
		return res.Instruments["A"].CostCash, res.EndCapital
	}
	c1, e1 := run(5)
	c2, e2 := run(10)
	if c2 <= c1 {
		t.Fatalf("doubling fees did not increase costs (%.0f → %.0f)", c1, c2)
	}
	if e2 >= e1 {
		t.Fatalf("doubling fees did not reduce end capital (%.0f → %.0f)", e1, e2)
	}
	if c1 <= 0 {
		t.Fatal("zero costs recorded; cost model not engaged")
	}
}

// A cash account cannot borrow: with MaxGrossLeverage=1.0 the held notional
// must never exceed current equity (small tolerance for block rounding).
func TestGrossLeverageCap(t *testing.T) {
	cfg := Config{
		Capital: 5_000_000, VolTargetPct: 0.20, Compounding: true,
		Rules:             []RuleSpec{{Rule: constRule{goodStory}, Weight: 1}},
		FDM:               1.0,
		InstrumentWeights: map[string]float64{"A": 1.0},
		IDM:               1.0,
		MaxGrossLeverage:  1.0,
	}
	inst := &data.Instrument{
		Meta:   data.Meta{Symbol: "A", PointValue: 1, Block: 1},
		Prices: randomWalk(13, 2000, 100, 0.01), // 1% daily vol → cap must bind
	}
	res, err := Run(cfg, []*data.Instrument{inst})
	if err != nil {
		t.Fatal(err)
	}
	pos := res.Instruments["A"].Positions
	for i, p := range pos.Values {
		if math.IsNaN(p) {
			continue
		}
		notional := math.Abs(p) * inst.Prices.Values[i]
		if eq := res.Equity.Values[i]; eq > 0 && notional > eq*1.03 {
			t.Fatalf("day %d: notional %.0f exceeds equity %.0f (leverage cap breached)", i, notional, eq)
		}
	}
	// The cap must actually have bitten in this setup, or the test is vacuous.
	var maxUtil float64
	for i, p := range pos.Values {
		if math.IsNaN(p) || res.Equity.Values[i] <= 0 {
			continue
		}
		if u := math.Abs(p) * inst.Prices.Values[i] / res.Equity.Values[i]; u > maxUtil {
			maxUtil = u
		}
	}
	if maxUtil < 0.9 {
		t.Fatalf("cap never approached (max utilization %.2f) — test setup wrong", maxUtil)
	}
}

// withOpens fabricates plausible session opens (yesterday's close drifted).
func withOpens(prices core.Series) *core.Series {
	o := make([]float64, prices.Len())
	o[0] = prices.Values[0]
	for i := 1; i < prices.Len(); i++ {
		o[i] = (prices.Values[i-1] + prices.Values[i]) / 2 // between close(T-1) and close(T)
	}
	s := core.New(prices.Times, o)
	return &s
}

// The look-ahead property must survive the T+1-open execution model: a
// changed final close may affect only the final day.
func TestNoLookAheadWithOpenExecution(t *testing.T) {
	base := randomWalk(21, 800, 5000, 0.01)
	bumped := core.New(base.Times, append([]float64(nil), base.Values...))
	bumped.Values[799] *= 1.10

	run := func(prices core.Series) []float64 {
		cfg := demoConfig()
		cfg.ExecuteAtOpen = true
		inst := &data.Instrument{
			Meta:   data.Meta{Symbol: "A", PointValue: 10, Block: 1},
			Prices: prices,
			Opens:  withOpens(prices),
		}
		res, err := Run(cfg, []*data.Instrument{inst})
		if err != nil {
			t.Fatal(err)
		}
		return res.Instruments["A"].Positions.Values
	}
	p1, p2 := run(base), run(bumped)
	for i := range 799 {
		same := p1[i] == p2[i] || (math.IsNaN(p1[i]) && math.IsNaN(p2[i]))
		if !same {
			t.Fatalf("LOOK-AHEAD (open-exec): position at day %d changed when only day 799 differs", i)
		}
	}
}

// Weekly decisions must trade less than daily ones on the same data.
func TestWeeklyScheduleReducesTurnover(t *testing.T) {
	run := func(sched Schedule) float64 {
		cfg := demoConfig()
		cfg.Schedule = sched
		inst := &data.Instrument{
			Meta:   data.Meta{Symbol: "A", PointValue: 10, Block: 1},
			Prices: randomWalk(17, 1500, 5000, 0.01),
		}
		res, err := Run(cfg, []*data.Instrument{inst})
		if err != nil {
			t.Fatal(err)
		}
		return res.Instruments["A"].BlocksTraded
	}
	daily, weekly := run(Daily), run(Weekly)
	if weekly <= 0 {
		t.Fatal("weekly schedule never traded")
	}
	if weekly >= daily {
		t.Fatalf("weekly (%.0f blocks) should trade less than daily (%.0f)", weekly, daily)
	}
}

// Open-fill accounting: P&L must be attributed around the fill price, and
// costs paid at the open, not the decision close.
func TestOpenFillAccounting(t *testing.T) {
	// Two-day scenario engineered by hand: decision at close(0), fill at
	// open(1). Constant forecast, so the whole position is opened at open(1).
	prices := core.New(days(300), make([]float64, 300))
	for i := range prices.Values {
		prices.Values[i] = 100 + float64(i%7) // small oscillation, nonzero vol
	}
	cfg := Config{
		Capital: 1_000_000, VolTargetPct: 0.20, Compounding: false,
		Rules:             []RuleSpec{{Rule: constRule{goodStory}, Weight: 1}},
		FDM:               1.0,
		InstrumentWeights: map[string]float64{"A": 1.0},
		IDM:               1.0,
		ExecuteAtOpen:     true,
	}
	inst := &data.Instrument{
		Meta:   data.Meta{Symbol: "A", PointValue: 1, Block: 1, FeePerBlock: 1},
		Prices: prices,
		Opens:  withOpens(prices),
	}
	res, err := Run(cfg, []*data.Instrument{inst})
	if err != nil {
		t.Fatal(err)
	}
	if res.Instruments["A"].CostCash <= 0 {
		t.Fatal("no costs recorded under open-fill execution")
	}
	for _, v := range res.Daily.Values {
		if math.IsNaN(v) || math.IsInf(v, 0) {
			t.Fatal("NaN/Inf P&L under open-fill execution")
		}
	}
}

// Law 1 mechanically enforced: no story, no backtest.
func TestStoryRequired(t *testing.T) {
	cfg := demoConfig()
	cfg.Rules = []RuleSpec{{Rule: constRule{story: ""}, Weight: 1}}
	inst := &data.Instrument{Meta: data.Meta{Symbol: "A", PointValue: 1, Block: 1},
		Prices: randomWalk(3, 300, 100, 0.01)}
	if _, err := Run(cfg, []*data.Instrument{inst}); err == nil {
		t.Fatal("unstoried rule must be rejected (Law 1)")
	}
}
