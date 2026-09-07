// Command backtest runs the full pipeline.
//
//	backtest                          synthetic demo (harness check only)
//	backtest -tickers RELIANCE,TCS    real data from the systrade database
//
// Real runs use cash-equity economics (block = 1 share, LongOnly) until
// futures metadata is wired up. Carry is skipped automatically when no carry
// series exists (the combiner renormalizes weights over available rules).
package main

import (
	"context"
	"flag"
	"fmt"
	"math"
	"math/rand"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/joho/godotenv"

	"github.com/ranedk/systrader/internal/backtest"
	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/data"
	"github.com/ranedk/systrader/internal/research"
	"github.com/ranedk/systrader/internal/rules"
	"github.com/ranedk/systrader/internal/store"
)

// envFloat: capital & risk enter via .env (Law 10); flags only override.
func envFloat(key string, def float64) float64 {
	if v := os.Getenv(key); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			return f
		}
	}
	return def
}

func main() {
	_ = godotenv.Load()
	tickers := flag.String("tickers", "", "comma-separated tickers from systrade db (empty = synthetic demo)")
	capital := flag.Float64("capital", envFloat("TRADING_CAPITAL", 5_000_000), "trading capital (default from TRADING_CAPITAL)")
	volTarget := flag.Float64("voltarget", envFloat("VOL_TARGET_PCT", 0.20), "annual volatility target (default from VOL_TARGET_PCT)")
	weekly := flag.Bool("weekly", false, "decide weekly (Friday close) instead of daily")
	flag.Parse()
	if *tickers != "" {
		runReal(strings.Split(*tickers, ","), *capital, *volTarget, *weekly)
		return
	}
	runSynthetic()
}

func runReal(tickers []string, capital, volTarget float64, weekly bool) {
	ctx := context.Background()
	st, err := store.Open(ctx)
	if err != nil {
		panic(err)
	}
	defer st.Close()

	var instruments []*data.Instrument
	weights := map[string]float64{}
	for _, tk := range tickers {
		tk = strings.TrimSpace(tk)
		// Adjusted OHLC first (splits/bonuses corrected, 2013+, delisted
		// included, opens for T+1 fills); adjusted closes if the bhavcopy
		// row is missing; raw dhan series as the last resort — raw returns
		// lie at corporate-action dates.
		var opens *core.Series
		src := "adjusted OHLC"
		o, prices, err := st.AdjustedOHLC(ctx, tk)
		if err == nil {
			opens = &o
		} else {
			prices, err = st.AdjustedCloses(ctx, tk)
			src = "adjusted close-only"
			if err != nil {
				prices, err = st.DailyCloses(ctx, tk)
				src = "dhan RAW (beware corporate actions)"
			}
		}
		if err != nil {
			fmt.Printf("skip %s: %v\n", tk, err)
			continue
		}
		// 2026-08-22: this run always sets ExecuteAtOpen (below), which now
		// REQUIRES Opens on every instrument (backtest.Run fails the whole
		// batch otherwise, by design — see engine.go's 2026-08-22 fix). An
		// instrument that fell back past adjusted-OHLC has no Opens; better
		// to drop it from this run and say so than to silently degrade its
		// fills to same-day closes, which used to happen with no visibility.
		if opens == nil {
			fmt.Printf("skip %s: no Opens available (fell back to %s) — ExecuteAtOpen requires Opens on every instrument\n", tk, src)
			continue
		}
		instruments = append(instruments, &data.Instrument{
			Meta: data.Meta{Symbol: tk, PointValue: 1, Block: 1, LongOnly: true,
				// cash-equity cost model: ~5bps spread+impact, ₹0 brokerage
				// (delivery), 0.1% STT+charges per side
				SpreadPoints: 0, FeePerBlock: 0, PercentValueFee: 0.0012},
			Prices: prices,
			Opens:  opens,
		})
		fmt.Printf("%s: %d bars (%s → %s) [%s]\n", tk, prices.Len(),
			prices.Times[0].Format("2006-01-02"), prices.Times[prices.Len()-1].Format("2006-01-02"), src)
	}
	if len(instruments) == 0 {
		panic("no instruments loaded")
	}
	for _, in := range instruments {
		weights[in.Meta.Symbol] = 1.0 / float64(len(instruments))
	}
	cfg := backtest.Config{
		Capital: capital, VolTargetPct: volTarget, Compounding: true,
		Rules: []backtest.RuleSpec{
			{Rule: rules.EWMAC{Fast: 16}, Weight: 0.42}, // carry absent → trend-only,
			{Rule: rules.EWMAC{Fast: 32}, Weight: 0.16}, // Table-8 within-group weights
			{Rule: rules.EWMAC{Fast: 64}, Weight: 0.42},
		},
		FDM:               1.1, // three correlated EWMAC variations only
		InstrumentWeights: weights,
		IDM:               0,   // derive point-in-time from realized correlations
		MaxGrossLeverage:  1.0, // cash delivery account: no borrowing, ever
		ExecuteAtOpen:     true,
	}
	if weekly {
		cfg.Schedule = backtest.Weekly
	}
	res, err := backtest.Run(cfg, instruments)
	if err != nil {
		panic(err)
	}
	ledgerM, lerr := research.CountM("research/LEDGER.md")
	if lerr != nil {
		ledgerM = 2 // inherited rows; never report a bar easier than reality
		fmt.Printf("WARN: cannot read research/LEDGER.md (%v), assuming M=%d\n", lerr, ledgerM)
	}
	fmt.Println("\n=== REAL DATA (cash-equity economics, long-only) ===")
	fmt.Printf("IDM (correlation-derived, point-in-time): %.2f\n", res.IDMUsed)
	fmt.Println(res.Metrics.Report(ledgerM))
	fmt.Println()
	for _, in := range instruments {
		ir := res.Instruments[in.Meta.Symbol]
		fmt.Printf("%-12s turnover=%.1f/yr  avg|pos|=%.0f  costs=₹%.0f\n",
			in.Meta.Symbol, ir.Turnover, ir.AvgAbsPos, ir.CostCash)
	}
	fmt.Printf("\nEnd capital: ₹%.0f (started ₹%.0f)\n", res.EndCapital, capital)
}

func runSynthetic() {
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
