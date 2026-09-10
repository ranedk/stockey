package main

import (
	"flag"
	"fmt"
	"strings"
	"time"

	"github.com/ranedk/systrader/internal/evidence"
	"github.com/ranedk/systrader/internal/handcraft"
	"github.com/ranedk/systrader/internal/rules"
	"github.com/ranedk/systrader/internal/sleeve"
)

// The construction measurements for the speed-blend spec, and ONLY those.
//
// Law 6 admits two inputs into handcrafted weights: correlations, because
// they are estimable from a realistic sample, and costs, because they are
// facts. It forbids the third — relative performance — so this command never
// prints a return, a Sharpe ratio or an edge. What it measures:
//
//   - each speed's cost of trading, in Sharpe-ratio units, for Table 12
//     column A (the one column that adjusts aggressively, because costs are
//     known with certainty);
//   - the correlation of the three speeds' books IN EXCESS of the
//     equal-weight book. Carver weights on rule-return correlations (Table 57)
//     rather than forecast correlations; in a long-only cross-section the raw
//     books share the whole market move and correlate ~0.97, which would say
//     nothing, so the excess return — the selection each speed adds — is the
//     return that carries the information;
//   - Table 8 on those correlations, the Table 12 cost adjustment, and the
//     FDM over row 14's pooled FORECAST correlations, which is what the FDM
//     is defined on (book p. 131);
//   - Carver's bootstrap on the same excess returns, as the cross-check. It
//     uses each sample's means, as his method must (evidence/weights.go), so
//     it is the one line here that performance reaches — which is why it is
//     read only after the weights were frozen in rules.SpeedBlend, and cannot
//     move them.

// Row 14's pooled forecast correlations (research/reports/
// 2026-09-07_rule_library_characterisation.txt), in speed order 16, 32, 64.
var speedForecastCorr = [][]float64{
	{1, 0.87, 0.59},
	{0.87, 1, 0.87},
	{0.59, 0.87, 1},
}

func runSpeeds(args []string) {
	fs := flag.NewFlagSet("speeds", flag.ExitOnError)
	cache := fs.String("cache", defaultCache, "bar cache file")
	from := fs.String("from", "2013-07-01", "first decision date")
	to := fs.String("to", "2026-06-30", "last decision date")
	costBps := fs.Float64("cost-bps", 50, "round-trip cost in basis points")
	floor := fs.Float64("floor", 1e8, "liquidity floor, 60-bar median traded value (the frozen spec's Rs 10cr)")
	rebalance := fs.Int("rebalance", 20, "trade every N decision days (the frozen spec's 20)")
	reps := fs.Int("reps", 500, "bootstrap resamples for the cross-check")
	seed := fs.Int64("seed", 1, "bootstrap seed — declared, never drawn")
	fatalIf(fs.Parse(args))

	speeds := []string{"ewmac16_64", "ewmac32_128", "ewmac64_256"}
	names := make([]string, len(variants))
	for i, v := range variants {
		names[i] = v.name
	}
	excessBy := make([]map[time.Time]float64, len(speeds))
	costSR := make([]float64, len(speeds))
	turnover := make([]float64, len(speeds))
	for k, sp := range speeds {
		rule := findRule(sp)
		days := buildCost(*cache, rule, mustDate(*from), mustDate(*to), *floor)
		res, err := sleeve.Run(days, names, sleeve.Config{
			CostBpsRoundTrip: *costBps,
			Seeds:            []int64{1, 2, 3, 4, 5},
			RebalanceEvery:   *rebalance,
		})
		fatalIf(err)
		r := res[0] // always-trend: the frozen book's construction
		s := sleeve.Summarize(r.Signal)
		costSR[k] = s.CostDrag / s.AnnVol
		turnover[k] = s.MeanTurnover
		excessBy[k] = map[time.Time]float64{}
		for i, d := range r.Signal.Dates {
			excessBy[k][d] = r.Signal.Net[i] - r.EqualWeight.Net[i]
		}
	}

	// Align on the days every speed traded.
	var dates []time.Time
	for d := range excessBy[0] {
		ok := true
		for k := 1; k < len(speeds); k++ {
			if _, has := excessBy[k][d]; !has {
				ok = false
			}
		}
		if ok {
			dates = append(dates, d)
		}
	}
	cols := make([][]float64, len(speeds))
	for k := range speeds {
		cols[k] = make([]float64, 0, len(dates))
	}
	// Order does not matter for a correlation, but the bootstrap resamples
	// blocks of ADJACENT days, so the rows must be in date order.
	sortDates(dates)
	for _, d := range dates {
		for k := range speeds {
			cols[k] = append(cols[k], excessBy[k][d])
		}
	}
	corr := evidence.Correlation(cols, nil)

	fmt.Printf("SPEED-BLEND CONSTRUCTION — %s..%s, Rs %.0f cr floor, top quintile, rebalance %d, %.0f bps\n",
		*from, *to, *floor/1e7, *rebalance, *costBps)
	fmt.Println("Inputs Law 6 admits: correlations (estimable) and costs (facts). No return, SR or edge is printed;")
	fmt.Println("only the bootstrap cross-check at the end sees sample means, as Carver's method requires.")
	fmt.Printf("%d common trading days.\n\n", len(dates))

	fmt.Println("cost of trading each speed's book")
	for k, sp := range speeds {
		fmt.Printf("  %-12s turnover %5.2f%%/day one-way   cost %.3f SR/yr\n", sp, 100*turnover[k], costSR[k])
	}

	fmt.Println("\ncorrelation of daily returns in excess of the equal-weight book (the weighting input)")
	printCorr(speeds, corr)
	fmt.Println("\nforecast correlation, pooled (row 14 — the FDM input)")
	printCorr(speeds, speedForecastCorr)

	tree := handcraft.Group("trend", handcraft.Leaf(speeds[0], 0), handcraft.Leaf(speeds[1], 1), handcraft.Leaf(speeds[2], 2))
	w, steps, err := handcraft.Weights(tree, corr)
	fatalIf(err)
	fmt.Println("\nhandcrafted (Table 8)")
	for _, s := range steps {
		fmt.Println("  " + s.String())
	}

	// Cost is a negative contribution to Sharpe ratio; Table 12 wants each
	// speed's SR relative to the group average, and a fixed pre-cost SR for
	// every speed (Law 6) leaves only the cost difference.
	neg := make([]float64, len(costSR))
	var avg float64
	for k := range costSR {
		neg[k] = -costSR[k]
		avg += neg[k]
	}
	avg /= float64(len(neg))
	adj := handcraft.AdjustForSharpe(w, neg, handcraft.Certain)
	fmt.Println("\nTable 12 column A on cost differences (SR vs group average -> factor)")
	for k, sp := range speeds {
		fmt.Printf("  %-12s %+.3f -> x%.3f   %.1f%% -> %.1f%%\n", sp, neg[k]-avg,
			handcraft.SharpeFactor(neg[k]-avg, handcraft.Certain), 100*w[k], 100*adj[k])
	}

	fdm := handcraft.DiversificationMultiplier(adj, speedForecastCorr, rules.MaxFDM)
	fmt.Printf("\nFDM over forecast correlations with these weights: %.3f\n", fdm)

	sampleLen := len(dates) / 10 // appendix C: samples of 10% of the history
	est, err := evidence.BootstrapWeights(cols, evidence.Bootstrap{
		MeanBlock: evidence.DefaultBlock(len(dates)), Reps: *reps, Seed: *seed,
	}, sampleLen)
	fatalIf(err)
	fmt.Printf("\ncross-check: Carver's bootstrap, max-Sharpe on %d-day samples (%d resamples, mean block %.0f days; %d samples uninformative)\n",
		sampleLen, *reps, evidence.DefaultBlock(len(dates)), est.Uninformative)
	fmt.Println("  read only after the weights above were frozen in rules.SpeedBlend; it cannot move them")
	for k, sp := range speeds {
		flag := ""
		if adj[k] < est.Low[k] || adj[k] > est.High[k] {
			flag = "  <- handcrafted weight outside the bootstrap's 10-90% range"
		}
		fmt.Printf("  %-12s handcrafted %5.1f%%   bootstrap %5.1f%% [%5.1f, %5.1f]   single fit %5.1f%%%s\n",
			sp, 100*adj[k], 100*est.Mean[k], 100*est.Low[k], 100*est.High[k], 100*est.Full[k], flag)
	}
}

func printCorr(names []string, c [][]float64) {
	fmt.Printf("  %-12s %s\n", "", strings.Join(names, "  "))
	for i, n := range names {
		fmt.Printf("  %-12s", n)
		for j := range names {
			fmt.Printf(" %10.2f ", c[i][j])
		}
		fmt.Println()
	}
}

func sortDates(d []time.Time) {
	for i := 1; i < len(d); i++ {
		for j := i; j > 0 && d[j].Before(d[j-1]); j-- {
			d[j], d[j-1] = d[j-1], d[j]
		}
	}
}
