package main

import (
	"flag"
	"fmt"
	"math"
	"sort"
	"strings"
	"time"

	"github.com/ranedk/systrader/internal/evidence"
	"github.com/ranedk/systrader/internal/handcraft"
	"github.com/ranedk/systrader/internal/paper"
	"github.com/ranedk/systrader/internal/sleeve"
)

// The construction measurements for the momentum lookback blend, and only
// those — the Law 6 inputs: what each lookback's book costs to trade (a fact)
// and how the books co-move (estimable). The members are the lookbacks whose
// cells survived LEDGER row 30 at a one-month hold; the blend's weights come
// from the handcrafting tree, never from which member scored best.
//
// Near-duplicates are grouped first: members whose excess returns correlate at
// 0.9 or more form one group (the 12-month and 12-minus-1 books differ by one
// month of formation). If the top of the tree would still hold four members
// with unequal correlations, the grouping threshold steps down to 0.7 and then
// 0.5 — Table 8's own grid — and the report prints which one was used.

var lookbackNames = []string{"mom6", "mom9", "mom12", "mom12_1"}

func lookbackMembers() []windowSignal {
	var out []windowSignal
	for _, n := range lookbackNames {
		for _, w := range momentumWindows {
			if w.Name == n {
				out = append(out, w)
			}
		}
	}
	return out
}

func runLookbacks(args []string) {
	fs := flag.NewFlagSet("lookbacks", flag.ExitOnError)
	cache := fs.String("cache", defaultCache, "bar cache file")
	from := fs.String("from", "2013-07-01", "first decision date")
	to := fs.String("to", "2021-12-31", "last decision date — the confirmation years stay unread")
	floor := fs.Float64("floor", 1e8, "liquidity floor (Rs 10cr, the paper tracks' universe)")
	costBps := fs.Float64("cost-bps", 50, "round-trip cost in basis points")
	reps := fs.Int("reps", 500, "bootstrap resamples for the cross-check")
	seed := fs.Int64("seed", 1, "bootstrap seed — declared, never drawn")
	readConfirm := fs.Bool("include-confirmation-years", false, "let -to reach 2022 onward")
	screen := fs.Bool("screen", false, "run the frozen blend's pre-registered kill screen instead of the construction")
	fatalIf(fs.Parse(args))
	guardConfirmationYears(mustDate(*to), *readConfirm)

	members := lookbackMembers()
	days := buildWindows(*cache, members, mustDate(*from), mustDate(*to), *floor)
	res, err := sleeve.Run(days, lookbackNames, sleeve.Config{
		CostBpsRoundTrip: *costBps, Seeds: []int64{1, 2, 3, 4, 5}, RebalanceEvery: month,
	})
	fatalIf(err)

	// Every member ran over the same days, so from a common start their dates align.
	var start time.Time
	for _, r := range res {
		if t := firstTraded(r.Signal); t.After(start) {
			start = t
		}
	}
	cols := make([][]float64, len(members))
	costSR := make([]float64, len(members))
	turn := make([]float64, len(members))
	for k, r := range res {
		sb, eb := trimFrom(r.Signal, start), trimFrom(r.EqualWeight, start)
		s := sleeve.Summarize(sb)
		costSR[k], turn[k] = s.CostDrag/s.AnnVol, s.MeanTurnover
		cols[k] = make([]float64, len(sb.Net))
		for i := range sb.Net {
			cols[k][i] = sb.Net[i] - eb.Net[i]
		}
		if len(cols[k]) != len(cols[0]) {
			fatal(fmt.Errorf("member books misaligned: %d vs %d days", len(cols[k]), len(cols[0])))
		}
	}
	if *screen {
		screenLookbackBlend(res, start, *from, *to, *floor, *costBps)
		return
	}
	corr := evidence.Correlation(cols, nil)

	fmt.Printf("MOMENTUM LOOKBACK BLEND — CONSTRUCTION. %s..%s, Rs %.0f cr floor, top quintile, monthly rebalance, %.0f bps\n",
		*from, *to, *floor/1e7, *costBps)
	fmt.Println("Law 6 inputs only: costs (facts) and correlations (estimable). No return, SR or edge printed;")
	fmt.Println("only the bootstrap cross-check at the end sees sample means, as Carver's method requires.")
	fmt.Printf("%d common trading days from %s.\n\n", len(cols[0]), start.Format("2006-01-02"))
	fmt.Println("cost of trading each lookback's book")
	for k, n := range lookbackNames {
		fmt.Printf("  %-8s turnover %5.2f%%/day one-way   cost %.3f SR/yr\n", n, 100*turn[k], costSR[k])
	}
	fmt.Println("\ncorrelation of daily returns in excess of the equal-weight book")
	printCorr(lookbackNames, corr)

	tree, thr := lookbackTree(corr)
	w, steps, err := handcraft.Weights(tree, corr)
	fatalIf(err)
	fmt.Printf("\nhandcrafted (Table 8), near-duplicates grouped at rho >= %.1f\n", thr)
	for _, s := range steps {
		fmt.Println("  " + s.String())
	}

	neg := make([]float64, len(costSR))
	var avg float64
	for k := range costSR {
		neg[k] = -costSR[k]
		avg += neg[k]
	}
	avg /= float64(len(neg))
	adj := handcraft.AdjustForSharpe(w, neg, handcraft.Certain)
	pct := wholePercent(adj)
	fmt.Println("\nTable 12 column A on cost differences (SR vs group average -> factor), then whole percent")
	for k, n := range lookbackNames {
		fmt.Printf("  %-8s %+.3f -> x%.3f   %.1f%% -> %.1f%% -> %d%%\n", n, neg[k]-avg,
			handcraft.SharpeFactor(neg[k]-avg, handcraft.Certain), 100*w[k], 100*adj[k], pct[k])
	}
	fmt.Println("\nNo FDM: this is a blend of BOOKS (each variant's own top quintile), not an average of")
	fmt.Println("forecasts, so there is no combined forecast to rescale.")

	est, err := evidence.BootstrapWeights(cols, evidence.Bootstrap{
		MeanBlock: evidence.DefaultBlock(len(cols[0])), Reps: *reps, Seed: *seed,
	}, len(cols[0])/10)
	fatalIf(err)
	fmt.Printf("\ncross-check: Carver's bootstrap, max-Sharpe on %d-day samples (%d resamples; %d uninformative)\n",
		len(cols[0])/10, *reps, est.Uninformative)
	fmt.Println("  read only after the weights above are fixed; it cannot move them")
	for k, n := range lookbackNames {
		fmt.Printf("  %-8s handcrafted %3d%%   bootstrap %5.1f%%   single fit %5.1f%%\n", n, pct[k], 100*est.Mean[k], 100*est.Full[k])
	}
}

// lookbackTree groups near-duplicates first, stepping the threshold down
// Table 8's grid until the top of the tree holds three members or fewer.
func lookbackTree(corr [][]float64) (handcraft.Node, float64) {
	k := len(lookbackNames)
	var groups [][]int
	thr := 0.9
	for _, t := range []float64{0.9, 0.7, 0.5} {
		thr = t
		groups = handcraft.Cluster(k, func(a, b int) float64 { return corr[a][b] }, t)
		if len(groups) <= 3 {
			break
		}
	}
	var children []handcraft.Node
	for _, g := range groups {
		if len(g) == 1 {
			children = append(children, handcraft.Leaf(lookbackNames[g[0]], g[0]))
			continue
		}
		var leaves []handcraft.Node
		var label []string
		for _, i := range g {
			leaves = append(leaves, handcraft.Leaf(lookbackNames[i], i))
			label = append(label, lookbackNames[i])
		}
		children = append(children, handcraft.Group(strings.Join(label, "+"), leaves...))
	}
	return handcraft.Group("lookbacks", children...), thr
}

// wholePercent rounds weights to whole percents that still sum to 100, by
// largest remainder.
func wholePercent(w []float64) []int {
	out := make([]int, len(w))
	type rem struct {
		i int
		r float64
	}
	var rs []rem
	total := 0
	for i, v := range w {
		f := math.Floor(100 * v)
		out[i] = int(f)
		total += out[i]
		rs = append(rs, rem{i, 100*v - f})
	}
	sort.Slice(rs, func(a, b int) bool { return rs[a].r > rs[b].r })
	for j := 0; total < 100 && j < len(rs); j++ {
		out[rs[j].i]++
		total++
	}
	return out
}

// screenLookbackBlend is the kill screen pre-registered in
// research/preregistrations/2026-09-12_lookback_blend_screen.md: the frozen
// weights (read from the spec, so they cannot drift from what paper trades)
// applied to the member books, against the same weighting of the members'
// controls. Summing the member books charges a name held by two variants
// twice for trading it, which the real book nets — conservative, so a pass
// here is not flattered by it.
func screenLookbackBlend(res []sleeve.RuleResult, start time.Time, from, to string, floor, costBps float64) {
	spec := paper.MomentumLookbackBlendSpec()
	if strings.Join(spec.Variants, ",") != strings.Join(lookbackNames, ",") {
		fatal(fmt.Errorf("spec variants %v do not match the members %v", spec.Variants, lookbackNames))
	}
	comp := func(name string, get func(sleeve.RuleResult) sleeve.Book) sleeve.Book {
		var out sleeve.Book
		for k, r := range res {
			b := trimFrom(get(r), start)
			if out.Dates == nil {
				out = sleeve.Book{Name: name, Dates: b.Dates, Gross: make([]float64, len(b.Dates)),
					Net: make([]float64, len(b.Dates)), Turnover: make([]float64, len(b.Dates))}
			}
			w := spec.VariantWeights[k]
			for i := range b.Dates {
				out.Gross[i] += w * b.Gross[i]
				out.Net[i] += w * b.Net[i]
				out.Turnover[i] += w * b.Turnover[i]
			}
		}
		return out
	}
	sig := comp("lookback blend", func(r sleeve.RuleResult) sleeve.Book { return r.Signal })
	eq := comp("equal-weight", func(r sleeve.RuleResult) sleeve.Book { return r.EqualWeight })
	sh := comp("stable-shuffle", func(r sleeve.RuleResult) sleeve.Book { return r.StableShuffled })

	_, ms := sleeve.Monthly(sig)
	_, me := sleeve.Monthly(eq)
	_, mh := sleeve.Monthly(sh)
	boot := evidence.Bootstrap{MeanBlock: 5, Reps: 10000, Seed: 1}
	eEq, err := evidence.PairedEdge(ms, me, boot, 0.90)
	fatalIf(err)
	eSh, err := evidence.PairedEdge(ms, mh, boot, 0.90)
	fatalIf(err)
	e2Eq := sleeve.PairedMonthly(doubleCost(sig), doubleCost(eq)).MeanDiff
	e2Sh := sleeve.PairedMonthly(doubleCost(sig), doubleCost(sh)).MeanDiff

	fmt.Printf("MOMENTUM LOOKBACK BLEND — KILL SCREEN (trials=1, pre-registered 2026-09-12). %s..%s, Rs %.0f cr, %.0f bps\n", from, to, floor/1e7, costBps)
	fmt.Printf("weights %v over %v, from %s (every member live). Exploration years: this can kill, never confirm.\n\n",
		spec.VariantWeights, spec.Variants, start.Format("2006-01-02"))
	for _, b := range []sleeve.Book{sig, eq, sh} {
		s := sleeve.Summarize(b)
		fmt.Printf("  %-18s %7.2f%%/yr  vol %6.2f%%  SR %5.2f  maxDD %6.1f%%  turnover %5.2f%%/day\n",
			b.Name, 100*s.AnnReturn, 100*s.AnnVol, s.SR, 100*s.MaxDD, 100*s.MeanTurnover)
	}
	fmt.Printf("\nedge, monthly paired, %d months, 90%% bootstrap intervals\n", eEq.N)
	fmt.Printf("  vs equal-weight    %s\n", edgeCell(eEq))
	fmt.Printf("  vs stable-shuffle  %s\n", edgeCell(eSh))
	fmt.Printf("  at 100 bps: vs equal-weight %+.2f%%/mo, vs stable-shuffle %+.2f%%/mo\n", 100*e2Eq, 100*e2Sh)
	k1 := eEq.P < 0.10 && eSh.P < 0.10
	k2 := e2Eq > 0 && e2Sh > 0
	fmt.Printf("\n  K1  p < 0.10 against both controls at 50 bps        %s (p %.4f, %.4f)\n", passFail(k1), eEq.P, eSh.P)
	fmt.Printf("  K2  positive mean against both controls at 100 bps  %s\n", passFail(k2))
	v := "SURVIVES — the forward track may start"
	if !k1 || !k2 {
		v = "KILLED — the forward track does not start"
	}
	fmt.Printf("  VERDICT: %s\n", v)
}
