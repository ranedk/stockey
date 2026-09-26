package main

import (
	"context"
	"flag"
	"fmt"
	"time"

	"github.com/ranedk/systrader/internal/capacity"
	"github.com/ranedk/systrader/internal/paper"
	"github.com/ranedk/systrader/internal/store"
	"github.com/ranedk/systrader/internal/traits"
)

// runCapacity is `paper capacity`: a frozen track's book in rupees — whole
// shares, Law 12's inertia, Dhan's real costs — at Rs 30 lakh and Rs 1 crore,
// with and without a Rs 10,000 minimum position, set against the weight book
// it implements. research/preregistrations/2026-09-12_capacity.md.
func runCapacity(args []string) {
	fs := flag.NewFlagSet("capacity", flag.ExitOnError)
	strategy := fs.String("strategy", "trend-quintile", "registered strategy")
	fromS := fs.String("from", "2013-07-01", "first decision day")
	toS := fs.String("to", "2021-12-31", "last decision day (2022+ is the confirmation period: leave it alone)")
	fatalIf(fs.Parse(args))

	spec, ok := paper.SpecFor(*strategy)
	if !ok {
		fatal(fmt.Errorf("no registered strategy %q", *strategy))
	}
	from, err := time.Parse("2006-01-02", *fromS)
	fatalIf(err)
	to, err := time.Parse("2006-01-02", *toS)
	fatalIf(err)
	spec.Start = from

	sig, err := parseSignal(spec.Signal)
	fatalIf(err)
	sig2, err := parseSignal("reversal20")
	fatalIf(err)
	ctx := context.Background()
	st, err := store.Open(ctx)
	fatalIf(err)
	defer st.Close()

	load := from.AddDate(0, 0, -warmupDays)
	if traits.NeedsMarket(spec.Variants) {
		marketForBeta, err = marketReturns(ctx, st, load, to, spec.MinTurnover)
		fatalIf(err)
	}
	var variants []signalFn
	for _, v := range spec.Variants {
		fn, err := parseSignal(v)
		fatalIf(err)
		variants = append(variants, fn)
	}
	days, err := buildDays(ctx, st, spec, sig, sig2, variants, load, to, false)
	fatalIf(err)
	if len(days) == 0 {
		fatal(fmt.Errorf("no days in %s..%s", *fromS, *toS))
	}

	gross := spec
	gross.CostBpsRoundTrip = 0
	idealG, err := paper.Compute(gross, days)
	fatalIf(err)
	idealN, err := paper.Compute(spec, days)
	fatalIf(err)

	ratios := map[time.Time]map[string]float64{}
	raw := func(d time.Time) (map[string]float64, error) {
		if r, ok := ratios[d]; ok {
			return r, nil
		}
		r, err := st.RawRatios(ctx, d)
		ratios[d] = r
		return r, err
	}

	fmt.Printf("CAPACITY — %s in rupees, research/preregistrations/2026-09-12_capacity.md (LEDGER trials=0)\n", spec.Name)
	fmt.Printf("%d trading days %s..%s, rebalance every %d; whole shares at the raw open, Law 12 inertia 10%%,\n",
		len(days), days[0].Date.Format("2006-01-02"), days[len(days)-1].Date.Format("2006-01-02"), spec.RebalanceEvery)
	fmt.Println("Dhan statutory + Rs 14.75 DP per sale + square-root impact, auction fills. Capital constant, nominal rupees.")
	fmt.Println()
	fmt.Printf("%-22s %6s %9s %9s %8s %7s %7s %7s %7s %7s %8s %6s %6s %7s %8s %s\n",
		"policy", "names", "median", "p10", "deployed", "stat", "DP", "impact", "cost", "model", "turnover", "<2 sh", "zero", "TE/yr", "diff/yr", "F1 F2 F3")
	fmt.Printf("%-22s %6s %9s %9s %8s %7s %7s %7s %7s %7s %8s %6s %6s %7s %8s\n",
		"", "held", "position", "position", "", "/yr", "/yr", "/yr", "/yr", "50bp/yr", "/yr", "days", "", "", "")

	ref, ok := todaysNames[spec.Name]
	if !ok {
		fatal(fmt.Errorf("no today's book size recorded for %s", spec.Name))
	}
	var results []*capacity.Result
	for _, sized := range []float64{0, ref} {
		if sized > 0 {
			fmt.Printf("-- today-sized: capital scaled at each rebalance by names held / %.0f (today's book)\n", ref)
		}
		for _, capital := range []float64{3e6, 1e7} {
			for _, min := range []float64{0, 10000} {
				pol := capacity.Policy{Capital: capital, MinPosition: min, Inertia: 0.10, RefNames: sized}
				r, err := capacity.Simulate(spec, days, raw, pol)
				fatalIf(err)
				results = append(results, r)
				s := capacity.Summarize(r, idealG.Books[paper.BookStrategy].NAV, idealN.Books[paper.BookStrategy].NAV)
				f1, f2, f3 := s.Fit()
				fmt.Printf("%-22s %6.0f %9.0f %9.0f %7.0f%% %6.2f%% %6.2f%% %6.2f%% %6.2f%% %6.2f%% %7.0f%% %5.1f%% %5.1f%% %6.2f%% %+7.2f%%  %s  %s  %s\n",
					pol.Label(), s.Names, s.MedianPos, s.P10Pos, 100*s.Deployed,
					100*s.StatYr, 100*s.DPYr, 100*s.ImpactYr, 100*s.CostYr(), 100*s.ModelYr,
					100*s.TurnoverYr, 100*s.Small, 100*s.Zero, 100*s.TE, 100*s.MeanDiff,
					mark(f1), mark(f2), mark(f3))
			}
		}
	}
	fmt.Println()
	fmt.Println("F1 tracking error of gross daily returns vs the weight book <= 2%/yr; F2 realised cost <= the 50 bps")
	fmt.Println("model's; F3 under 5% of position-days below 2 shares (Law 14). Costs are fractions of deployed rupees.")
	fmt.Println("'diff/yr' is the mean gross difference against the weight book, reported, not a decision input.")

	// Nominal rupees: 2013's universe is smaller than today's, so show how
	// the book's size moved year by year for the plain policy at each capital.
	fmt.Println("\nplain book by year: median names held / median position, nominal and today-sized")
	fmt.Printf("  %-6s %22s %22s %22s %22s\n", "year", "Rs 30 lakh", "Rs 1 crore", "30 lakh today-sized", "1 crore today-sized")
	byYear := func(r *capacity.Result) map[int][2]float64 {
		names := map[int][]float64{}
		pos := map[int][]float64{}
		for _, rb := range r.Rebalances {
			y := rb.Date.Year()
			names[y] = append(names[y], float64(len(rb.Positions)))
			pos[y] = append(pos[y], rb.Positions...)
		}
		out := map[int][2]float64{}
		for y := range names {
			out[y] = [2]float64{capacity.Quantile(names[y], 0.5), capacity.Quantile(pos[y], 0.5)}
		}
		return out
	}
	a, b, c, e := byYear(results[0]), byYear(results[2]), byYear(results[4]), byYear(results[6])
	for y := from.Year(); y <= to.Year(); y++ {
		if _, ok := a[y]; !ok {
			continue
		}
		fmt.Printf("  %-6d %10.0f / Rs %7.0f %10.0f / Rs %7.0f %10.0f / Rs %7.0f %10.0f / Rs %7.0f\n",
			y, a[y][0], a[y][1], b[y][0], b[y][1], c[y][0], c[y][1], e[y][0], e[y][1])
	}
}

func mark(ok bool) string {
	if ok {
		return "✓"
	}
	return "✗"
}

// todaysNames is each track's book size on the 2026-09-12 order sheets
// (systrader_paper_pending), the reference the pre-registration's amendment
// sizes history to.
var todaysNames = map[string]float64{
	"trend-quintile":              160,
	"trend-speed-blend":           160,
	"momentum-lookback-blend":     241,
	"low-volatility-blend":        279,
	"momentum-lowvol-combination": 493,
}
