package main

import (
	"context"
	"flag"
	"fmt"
	"math"
	"strconv"
	"strings"
	"time"

	"github.com/ranedk/systrader/internal/costs"
	"github.com/ranedk/systrader/internal/paper"
	"github.com/ranedk/systrader/internal/store"
	"github.com/ranedk/systrader/internal/traits"
)

// runBuffer is `paper buffer` (TODO A2): each registered track re-run over history with
// the rank buffer off and at 2x / 3x, to measure what it does to turnover, cost and --
// through the A1 tax model -- tax. A measurement, not a decision: the frozen tracks are
// untouched, and adopting a buffer means a new spec and a new clock (Law 19; TODO A3).
func runBuffer(args []string) {
	fs := flag.NewFlagSet("buffer", flag.ExitOnError)
	strategy := fs.String("strategy", "all", "registered strategy, or all")
	fromS := fs.String("from", "2013-07-01", "first decision day")
	toS := fs.String("to", "2021-12-31", "last decision day (2022+ is the confirmation period: leave it alone)")
	book := fs.Float64("book", 1e7, "book size in rupees, for the long-term-gains exemption")
	clocksS := fs.String("clocks", "", "comma-separated rebalance intervals in trading days to compare (default: each spec's own)")
	keepsS := fs.String("keeps", "1,2,3", "comma-separated buffer multiples K to compare")
	fatalIf(fs.Parse(args))
	from, err := time.Parse("2006-01-02", *fromS)
	fatalIf(err)
	to, err := time.Parse("2006-01-02", *toS)
	fatalIf(err)

	specs := paper.Specs()
	if *strategy != "all" {
		s, ok := paper.SpecFor(*strategy)
		if !ok {
			fatal(fmt.Errorf("no registered strategy %q", *strategy))
		}
		specs = []paper.Spec{s}
	}
	ctx := context.Background()
	st, err := store.Open(ctx)
	fatalIf(err)
	defer st.Close()

	fmt.Printf("RANK BUFFER — hold a name until it falls outside the top N x K (TODO A2; LEDGER, trials=0)\n")
	fmt.Printf("decisions %s..%s, each track's own spec otherwise; tax: A1 model at a Rs %.0f book\n\n", *fromS, *toS, *book)
	clocks, err := intList(*clocksS)
	fatalIf(err)
	keeps, err := intList(*keepsS)
	fatalIf(err)
	fmt.Printf("%-28s %4s %2s %9s %9s %9s %9s %8s %9s %9s\n", "track", "reb", "K", "turn/yr", "churn/reb", "gross/yr", "net/yr", "LT share", "tax/yr", "after-tax")
	for _, spec := range specs {
		spec.Start = from
		sig, err := parseSignal(spec.Signal)
		fatalIf(err)
		sig2, err := parseSignal("reversal20")
		fatalIf(err)
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
		specClocks := clocks
		if len(specClocks) == 0 {
			specClocks = []int{spec.RebalanceEvery}
		}
		for _, reb := range specClocks {
			for _, k := range keeps {
				s := spec
				s.RebalanceEvery = reb
				s.KeepMultiple = k
				net, err := paper.Compute(s, days)
				fatalIf(err)
				g := s
				g.CostBpsRoundTrip = 0
				gross, err := paper.Compute(g, days)
				fatalIf(err)
				m := bufferMetrics(net, gross, s.RebalanceEvery, *book)
				fmt.Printf("%-28s %4d %2d %8.0f%% %8.1f%% %8.2f%% %8.2f%% %7.1f%% %8.2f%% %8.2f%%\n",
					spec.Name, reb, k, 100*m.turnPerYear, 100*m.churn, 100*m.grossCAGR, 100*m.netCAGR,
					100*m.longTerm, 100*m.taxPerYear, 100*m.afterTax)
			}
		}
		fmt.Println()
	}
	fmt.Println("turn/yr: one-way sum of weight changes a year. churn/reb: share of the book replaced per")
	fmt.Println("rebalance (turnover/2). LT share: the A1 model's long-term share of realised gains at that churn")
	fmt.Println("and clock. after-tax: net CAGR less tax on it as if realised yearly -- an upper bound on tax,")
	fmt.Println("since a lower-churn book also DEFERS gains, which this does not credit.")
}

func intList(s string) ([]int, error) {
	var out []int
	for _, part := range strings.Split(s, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		n, err := strconv.Atoi(part)
		if err != nil {
			return nil, fmt.Errorf("bad list %q: %w", s, err)
		}
		out = append(out, n)
	}
	return out, nil
}

type bufferResult struct {
	turnPerYear, churn, grossCAGR, netCAGR, longTerm, taxPerYear, afterTax float64
}

func bufferMetrics(net, gross *paper.Track, rebalanceEvery int, book float64) bufferResult {
	nav := net.Books[paper.BookStrategy].NAV
	gnav := gross.Books[paper.BookStrategy].NAV
	var r bufferResult
	if len(nav) < 2 {
		return r
	}
	years := float64(len(nav)) / 252
	var turn, rebTurn float64
	rebs := 0
	for _, p := range nav {
		turn += p.Turnover
		if p.Rebalanced {
			rebTurn += p.Turnover
			rebs++
		}
	}
	r.turnPerYear = turn / years
	if rebs > 1 {
		// the first rebalance buys the whole book; churn is about the ones after it
		r.churn = (rebTurn - nav[0].Turnover) / float64(rebs-1) / 2
	}
	r.netCAGR = math.Pow(nav[len(nav)-1].NAV/100, 1/years) - 1
	r.grossCAGR = math.Pow(gnav[len(gnav)-1].NAV/100, 1/years) - 1
	r.longTerm = costs.LongTermShare(rebalanceEvery, math.Min(math.Max(r.churn, 1e-6), 1))
	r.taxPerYear = costs.AnnualTax(r.netCAGR*book, r.longTerm) / book
	r.afterTax = r.netCAGR - r.taxPerYear
	return r
}
