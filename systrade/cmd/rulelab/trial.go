package main

import (
	"flag"
	"fmt"
	"math"
	"os"
	"sync"
	"time"

	"github.com/ranedk/systrader/internal/backtest"
	"github.com/ranedk/systrader/internal/bars"
	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/data"
	"github.com/ranedk/systrader/internal/research"
	"github.com/ranedk/systrader/internal/rules"
	"github.com/ranedk/systrader/internal/sleeve"
)

// The trial registered in research/preregistrations/2026-09-07_acceleration_meanrev.md.
// Every value below is fixed by that file. Changing one makes this a different
// experiment that needs its own pre-registration and its own ledger row.
var (
	trialRules = []rules.Rule{
		rules.Acceleration{Fast: 16},
		rules.Acceleration{Fast: 32},
		rules.Acceleration{Fast: 64},
		rules.MeanReversion{Window: 1280},
	}
	trialSeeds = []int64{1, 2, 3, 4, 5, 6, 7, 8, 9, 10}

	constructionFrom = mustDate("2013-07-01")
	constructionTo   = mustDate("2021-12-31")
	holdoutFrom      = mustDate("2022-01-01")
	holdoutTo        = mustDate("2026-06-30")

	holdoutName = "accel-meanrev-equity-2022-01-to-2026-06"
)

func runTrial(args []string) {
	fs := flag.NewFlagSet("trial", flag.ExitOnError)
	cache := fs.String("cache", defaultCache, "bar cache file")
	period := fs.String("period", "construction", "construction | holdout")
	minTurnover := fs.Float64("min-turnover", 1e7, "minimum 60-bar median traded value in INR at the decision")
	turnWin := fs.Int("turnover-window", 60, "bars in the rolling median turnover")
	costBps := fs.Float64("cost-bps", 50, "round-trip cost in basis points")
	workers := fs.Int("workers", 0, "parallel symbol workers (0 = NumCPU)")
	burn := fs.Bool("burn-the-holdout", false, "required to run -period=holdout: this look is the only one there will be")
	ledger := fs.String("burns", "research/holdout_burns.json", "holdout burn registry")
	check(fs.Parse(args))

	from, to := constructionFrom, constructionTo
	if *period == "holdout" {
		from, to = holdoutFrom, holdoutTo
		if !*burn {
			check(fmt.Errorf("refusing to read the holdout without -burn-the-holdout: %s..%s is spent the moment it is looked at",
				from.Format("2006-01-02"), to.Format("2006-01-02")))
		}
		reg, err := research.OpenRegistry(*ledger)
		check(err)
		if reg.Burned(holdoutName) {
			check(fmt.Errorf("holdout %q is already burned — there is no second look, and a rerun would be a lie by omission", holdoutName))
		}
		// Burn BEFORE printing anything. A burn that depends on liking the
		// answer is not a burn.
		check(reg.Burn(holdoutName, "accel16,accel32,accel64,meanrev1280|xs-longonly-gross1|50bps|seeds1-10",
			"Pre-registered in research/preregistrations/2026-09-07_acceleration_meanrev.md. Burned by `rulelab trial -period=holdout`."))
		fmt.Printf("holdout %s BURNED and recorded in %s — this is the only look.\n\n", holdoutName, *ledger)
	} else if *period != "construction" {
		check(fmt.Errorf("unknown period %q (construction | holdout)", *period))
	}

	names := make([]string, len(trialRules))
	for i, r := range trialRules {
		check(rules.Validate(r))
		names[i] = r.Name()
	}

	days := buildDays(*cache, from, to, *minTurnover, *turnWin, *workers)

	fmt.Printf("%s window %s..%s | %d decision days | %d rules | costs %.0f bps round trip (and %.0f bps in the sensitivity)\n",
		*period, from.Format("2006-01-02"), to.Format("2006-01-02"), len(days), len(trialRules), *costBps, 2**costBps)
	fmt.Printf("universe: NSE cash equity, long-only, weights proportional to forecast/vol, gross exposure 1.0, fills at the open after the decision close\n\n")

	m, err := research.CountM("research/LEDGER.md")
	check(err)
	bar := backtest.BonferroniBar(m)
	fmt.Printf("ledger M = %d -> Bonferroni admission bar t = %.2f | pre-registered construction gate t > 2.0 vs BOTH controls\n\n", m, bar)

	base, err := sleeve.Run(days, names, sleeve.Config{CostBpsRoundTrip: *costBps, Seeds: trialSeeds})
	check(err)
	doubled, err := sleeve.Run(days, names, sleeve.Config{CostBpsRoundTrip: 2 * *costBps, Seeds: trialSeeds})
	check(err)

	for i := range base {
		report(base[i], doubled[i], bar, *period)
	}
}

func report(r, r2x sleeve.RuleResult, bar float64, period string) {
	fmt.Printf("=== %s ===\n", r.Rule)
	fmt.Printf("%d days traded, %.0f eligible names on an average day\n", r.Days, r.MeanEligible)
	fmt.Printf("%-22s %9s %8s %8s %9s %9s %10s\n", "book", "ann ret", "ann vol", "SR", "skew(m)", "maxDD", "turnover/d")
	for _, b := range []sleeve.Book{r.Signal, r.EqualWeight, r.Shuffled, r.StableShuffled} {
		s := sleeve.Summarize(b)
		fmt.Printf("%-22s %8.2f%% %7.2f%% %8.2f %9.2f %8.1f%% %9.1f%%\n",
			b.Name, 100*s.AnnReturn, 100*s.AnnVol, s.SR, s.Skew, 100*s.MaxDD, 100*s.MeanTurnover)
	}

	fmt.Printf("\n%-34s %8s %8s %10s %8s\n", "paired monthly, rule minus control", "mean", "t", "months won", "verdict")
	type pair struct {
		label string
		p     sleeve.Paired
	}
	// Only the two PRE-REGISTERED controls decide anything. Everything after
	// them in this report is diagnostics added after the first run, and is
	// labelled so no one can mistake it for part of the decision.
	pairs := []pair{
		{"vs equal-weight (beta control)", sleeve.PairedMonthly(r.Signal, r.EqualWeight)},
		{"vs shuffled (selection control)", sleeve.PairedMonthly(r.Signal, r.Shuffled)},
	}
	minT := math.Inf(1)
	for _, pr := range pairs {
		v := verdict(pr.p.T, bar, period)
		if pr.p.T < minT {
			minT = pr.p.T
		}
		fmt.Printf("%-34s %7.3f%% %8.2f %9.1f%% %8s\n", pr.label, 100*pr.p.MeanDiff, pr.p.T, 100*pr.p.MonthsWon, v)
	}
	for _, pr := range []pair{
		{"vs equal-weight, costs doubled", sleeve.PairedMonthly(r2x.Signal, r2x.EqualWeight)},
		{"vs shuffled, costs doubled", sleeve.PairedMonthly(r2x.Signal, r2x.Shuffled)},
	} {
		fmt.Printf("%-34s %7.3f%% %8.2f %9.1f%%\n", pr.label, 100*pr.p.MeanDiff, pr.p.T, 100*pr.p.MonthsWon)
	}

	fmt.Printf("\n%-34s %8s %8s %10s   (post-hoc diagnostics, decide nothing)\n", "", "mean", "t", "months won")
	for _, pr := range []pair{
		{"GROSS vs equal-weight", sleeve.PairedMonthly(sleeve.GrossBook(r.Signal), sleeve.GrossBook(r.EqualWeight))},
		{"GROSS vs shuffled", sleeve.PairedMonthly(sleeve.GrossBook(r.Signal), sleeve.GrossBook(r.Shuffled))},
		{"vs stable-shuffle (C3)", sleeve.PairedMonthly(r.Signal, r.StableShuffled)},
		{"GROSS vs stable-shuffle (C3)", sleeve.PairedMonthly(sleeve.GrossBook(r.Signal), sleeve.GrossBook(r.StableShuffled))},
	} {
		fmt.Printf("%-34s %7.3f%% %8.2f %9.1f%%\n", pr.label, 100*pr.p.MeanDiff, pr.p.T, 100*pr.p.MonthsWon)
	}

	switch {
	case period == "construction" && minT > 2.0:
		fmt.Printf("\nGATE PASSED: weakest control t = %.2f > 2.0 — eligible for the holdout.\n\n", minT)
	case period == "construction":
		fmt.Printf("\nGATE FAILED: weakest control t = %.2f, needs > 2.0 against both. Do not spend the holdout on this.\n\n", minT)
	case minT > bar:
		fmt.Printf("\nCLEARS THE BAR: weakest control t = %.2f > %.2f.\n\n", minT, bar)
	default:
		fmt.Printf("\nREJECTED: weakest control t = %.2f, bar is %.2f.\n\n", minT, bar)
	}
}

func verdict(t, bar float64, period string) string {
	if period == "construction" {
		if t > 2.0 {
			return "pass"
		}
		return "fail"
	}
	if t > bar {
		return "clears"
	}
	return "under"
}

// buildDays turns the bar cache into the per-day cross-sections the sleeve
// backtest consumes. Indicators are computed on each symbol's FULL history so
// the date window never shortens a rule's warm-up; only the observations
// inside the window are emitted.
func buildDays(cache string, from, to time.Time, minTurnover float64, turnWin, workers int) []sleeve.Day {
	byDate := map[time.Time][]sleeve.Obs{}
	var mu sync.Mutex
	symIDs := map[string]int32{}

	check(bars.ScanParallel(cache, workers, func(ser bars.Series) {
		n := len(ser.Bars)
		if n < 3 {
			return
		}
		times := make([]time.Time, n)
		closes := make([]float64, n)
		opens := make([]float64, n)
		for i, b := range ser.Bars {
			times[i], closes[i], opens[i] = b.Date, b.Close, b.Open
		}
		prices := core.New(times, closes)
		// LongOnly true: this is the sleeve that would actually trade, and
		// Indian cash equity cannot be shorted overnight. Forecasts clip to
		// [0, +20] and a negative view simply means "hold nothing".
		inst := &data.Instrument{
			Meta:   data.Meta{Symbol: ser.Symbol, PointValue: 1, Block: 1, LongOnly: true},
			Prices: prices,
		}
		vol := core.PriceUnitVol(prices, volSpan, volMin)

		fc := make([][]float64, len(trialRules))
		for k, r := range trialRules {
			fc[k] = rules.Forecast(r, inst, vol).Values
		}
		turnover := bars.MedianTurnover(ser.Bars, turnWin)

		var local []struct {
			date time.Time
			obs  sleeve.Obs
		}
		for i := 0; i < n-2; i++ {
			d := times[i]
			if d.Before(from) || d.After(to) {
				continue
			}
			if math.IsNaN(turnover[i]) || turnover[i] < minTurnover {
				continue
			}
			entry, exit := opens[i+1], opens[i+2]
			if entry <= 0 || exit <= 0 || math.IsNaN(entry) || math.IsNaN(exit) {
				continue
			}
			pctVol := vol.Values[i] / closes[i] // daily vol as a fraction of price
			if math.IsNaN(pctVol) || pctVol <= 0 {
				continue
			}
			u := make([]float64, len(trialRules))
			any := false
			for k := range trialRules {
				f := fc[k][i]
				if math.IsNaN(f) {
					u[k] = math.NaN()
					continue
				}
				u[k] = f / pctVol
				any = true
			}
			if !any {
				continue
			}
			local = append(local, struct {
				date time.Time
				obs  sleeve.Obs
			}{d, sleeve.Obs{U: u, Ret: exit/entry - 1}})
		}
		if len(local) == 0 {
			return
		}
		mu.Lock()
		id, ok := symIDs[ser.Symbol]
		if !ok {
			id = int32(len(symIDs) + 1)
			symIDs[ser.Symbol] = id
		}
		for _, e := range local {
			o := e.obs
			o.Sym = id
			byDate[e.date] = append(byDate[e.date], o)
		}
		mu.Unlock()
	}))

	days := make([]sleeve.Day, 0, len(byDate))
	for d, obs := range byDate {
		days = append(days, sleeve.Day{Date: d, Obs: obs})
	}
	if len(days) == 0 {
		fmt.Fprintln(os.Stderr, "rulelab: no eligible observations in the window")
		os.Exit(1)
	}
	return days
}
