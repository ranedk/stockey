// Command backtest runs the full pipeline. With no arguments it runs a
// SYNTHETIC demo (random data, seeded) purely to exercise the machinery —
// its performance numbers are meaningless by construction and the report
// says so. Real usage arrives with real data: see cmd/run and docs/.
package main

import (
	"fmt"
	"math"
	"math/rand"
	"time"

	"github.com/ranedk/systrader/internal/backtest"
	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/data"
	"github.com/ranedk/systrader/internal/rules"
)

func main() {
	rng := rand.New(rand.NewSource(42))
	days := tradingDays(time.Date(2016, 1, 1, 0, 0, 0, 0, time.UTC), 2500)

	// Synthetic instrument 1: regime-switching trending series (EWMAC food).
	trend := makeTrending(rng, days, 20000, 0.009)
	instA := &data.Instrument{
		Meta: data.Meta{Symbol: "SYNTH-TREND", PointValue: 75, Block: 1,
			SpreadPoints: 1.0, FeePerBlock: 20, PercentValueFee: 0.0001},
		Prices: trend,
	}

	// Synthetic instrument 2: mean-reverting series with positive carry.
	chop, carry := makeChoppyWithCarry(rng, days, 9000, 0.007, 0.05)
	instB := &data.Instrument{
		Meta: data.Meta{Symbol: "SYNTH-CARRY", PointValue: 100, Block: 1,
			SpreadPoints: 2.0, FeePerBlock: 20, PercentValueFee: 0.0001},
		Prices:   chop,
		AnnCarry: &carry,
	}

	cfg := backtest.Config{
		Capital:      5_000_000, // ₹50L demo
		VolTargetPct: 0.20,      // Law 10
		Compounding:  true,
		Rules: []backtest.RuleSpec{ // Carver ch-15 reference weights
			{Rule: rules.EWMAC{Fast: 16}, Weight: 0.21},
			{Rule: rules.EWMAC{Fast: 32}, Weight: 0.08},
			{Rule: rules.EWMAC{Fast: 64}, Weight: 0.21},
			{Rule: rules.Carry{}, Weight: 0.50},
		},
		FDM:               1.31,
		InstrumentWeights: map[string]float64{"SYNTH-TREND": 0.5, "SYNTH-CARRY": 0.5},
		IDM:               1.41, // two ~uncorrelated subsystems
	}

	res, err := backtest.Run(cfg, []*data.Instrument{instA, instB})
	if err != nil {
		panic(err)
	}

	fmt.Println("=== SYNTHETIC DEMO — numbers are meaningless by construction ===")
	fmt.Println(res.Metrics.Report(2)) // ledger M=2 (the two inherited rules)
	fmt.Println()
	for _, sym := range []string{"SYNTH-TREND", "SYNTH-CARRY"} {
		ir := res.Instruments[sym]
		fmt.Printf("%-12s turnover=%.1f/yr  blocksTraded=%.0f  avg|pos|=%.1f  max|pos|=%.0f  costs=₹%.0f\n",
			sym, ir.Turnover, ir.BlocksTraded, ir.AvgAbsPos, ir.MaxAbsPos, ir.CostCash)
	}
	fmt.Printf("\nEnd capital: ₹%.0f (started ₹%.0f)\n", res.EndCapital, cfg.Capital)
}

func tradingDays(start time.Time, n int) []time.Time {
	out := make([]time.Time, 0, n)
	t := start
	for len(out) < n {
		if wd := t.Weekday(); wd != time.Saturday && wd != time.Sunday {
			out = append(out, t)
		}
		t = t.AddDate(0, 0, 1)
	}
	return out
}

// makeTrending: random walk whose drift flips sign every ~9 months.
func makeTrending(rng *rand.Rand, days []time.Time, start, dailyVol float64) core.Series {
	vals := make([]float64, len(days))
	p := start
	drift := 0.0004
	for i := range days {
		if i%190 == 0 && rng.Float64() < 0.6 {
			drift = -drift
		}
		p *= 1 + drift + dailyVol*rng.NormFloat64()
		vals[i] = p
	}
	return core.New(days, vals)
}

// makeChoppyWithCarry: OU-style mean reversion around a slow anchor plus a
// constant positive carry series (annualized, price units).
func makeChoppyWithCarry(rng *rand.Rand, days []time.Time, start, dailyVol, carryPct float64) (core.Series, core.Series) {
	vals := make([]float64, len(days))
	cvals := make([]float64, len(days))
	anchor, p := start, start
	for i := range days {
		p += 0.05*(anchor-p) + p*dailyVol*rng.NormFloat64()
		vals[i] = p
		cvals[i] = carryPct * p // e.g. 5%/yr in price units
		anchor *= 1 + 0.00005*math.Sin(float64(i)/300)
	}
	return core.New(days, vals), core.New(days, cvals)
}
