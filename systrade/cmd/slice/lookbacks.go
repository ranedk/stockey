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
	"github.com/ranedk/systrader/internal/traits"
)

// Construction measurements and kill screens for the paper tracks that blend
// member books — `slice lookbacks -family momentum|lowvol|combo [-screen]`.
//
// Construction reads only the Law 6 inputs: what each member's book costs to
// trade (a fact) and how the books co-move (estimable). The weights come from
// the handcrafting tree, never from which member scored best. Near-duplicates
// are grouped first — members whose excess returns correlate at 0.9 or more —
// and the threshold steps down Table 8's own grid (0.7, 0.5) until the top of
// the tree holds three members or fewer; a group that would still hold four or
// more is split the same way from above. The report prints the threshold used.
//
// The combination is not re-derived from its members: it is a two-branch tree
// — the momentum track's books and the low-volatility track's books, each at
// its own frozen weights — split by Table 8 row 2 (two branches: halves) and
// tilted by Table 12 column A on the two branches' costs.

var lookbackNames = []string{"mom6", "mom9", "mom12", "mom12_1"}

// The low-risk measures whose ONE-MONTH-hold cells survived LEDGER row 32
// (1-month volatility held one month did not).
var lowVolMemberNames = []string{"vol3", "vol6", "vol12", "beta12", "idio12"}

type blendFamily struct {
	Name     string
	Title    string
	Root     string // label of the tree's root in the report
	SpecName string
	Members  []string
	Matched  bool // judge the screen at matched risk (a low-risk book) rather than raw
}

func familyByName(name string) blendFamily {
	switch name {
	case "momentum":
		return blendFamily{Name: name, Title: "MOMENTUM LOOKBACK BLEND", Root: "lookbacks",
			SpecName: "momentum-lookback-blend", Members: lookbackNames}
	case "lowvol":
		return blendFamily{Name: name, Title: "LOW-VOLATILITY BLEND", Root: "low risk",
			SpecName: "low-volatility-blend", Members: lowVolMemberNames, Matched: true}
	case "combo":
		mom := paper.MomentumLookbackBlendSpec()
		lv, ok := paper.SpecFor("low-volatility-blend")
		if !ok {
			fatal(fmt.Errorf("the combination needs the low-volatility track frozen first"))
		}
		return blendFamily{Name: name, Title: "MOMENTUM + LOW-VOLATILITY COMBINATION", Root: "strategies",
			SpecName: "momentum-lowvol-combination", Members: append(append([]string(nil), mom.Variants...), lv.Variants...),
			Matched: true}
	}
	fatal(fmt.Errorf("unknown family %q (momentum | lowvol | combo)", name))
	return blendFamily{}
}

// memberScorers finds each named member among the momentum windows and the
// low-risk measures.
func memberScorers(names []string, mkt map[time.Time]float64) []scorer {
	all := map[string]scorer{}
	for _, w := range momentumWindows {
		all[w.Name] = windowScorer(w)
	}
	if mkt != nil {
		for _, s := range lowVolScorers(mkt) {
			all[s.Name] = s
		}
	}
	out := make([]scorer, len(names))
	for i, n := range names {
		s, ok := all[n]
		if !ok {
			fatal(fmt.Errorf("no scorer %q", n))
		}
		out[i] = s
	}
	return out
}

func runLookbacks(args []string) {
	fs := flag.NewFlagSet("lookbacks", flag.ExitOnError)
	cache := fs.String("cache", defaultCache, "bar cache file")
	family := fs.String("family", "momentum", "which blend: momentum | lowvol | combo")
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

	fam := familyByName(*family)
	var mkt map[time.Time]float64
	if fam.Name != "momentum" {
		m, _, err := traits.MarketReturns(*cache, *floor)
		fatalIf(err)
		mkt = m
	}
	days := buildScores(*cache, memberScorers(fam.Members, mkt), mustDate(*from), mustDate(*to), *floor, month+3)
	res, err := sleeve.Run(days, fam.Members, sleeve.Config{
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
	cols := make([][]float64, len(fam.Members))
	costSR := make([]float64, len(fam.Members))
	turn := make([]float64, len(fam.Members))
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
		screenFamily(fam, res, start, *from, *to, *floor, *costBps)
		return
	}

	fmt.Printf("%s — CONSTRUCTION. %s..%s, Rs %.0f cr floor, top quintile, monthly rebalance, %.0f bps\n",
		fam.Title, *from, *to, *floor/1e7, *costBps)
	fmt.Println("Law 6 inputs only: costs (facts) and correlations (estimable). No return, SR or edge printed;")
	fmt.Println("only the bootstrap cross-check at the end sees sample means, as Carver's method requires.")
	fmt.Printf("%d common trading days from %s.\n\n", len(cols[0]), start.Format("2006-01-02"))
	fmt.Println("cost of trading each member's book")
	for k, n := range fam.Members {
		fmt.Printf("  %-8s turnover %5.2f%%/day one-way   cost %.3f SR/yr\n", n, 100*turn[k], costSR[k])
	}
	if fam.Name == "combo" {
		comboConstruction(fam, cols, costSR, *reps, *seed)
		return
	}
	corr := evidence.Correlation(cols, nil)
	fmt.Println("\ncorrelation of daily returns in excess of the equal-weight book")
	printCorr(fam.Members, corr)

	idx := make([]int, len(fam.Members))
	for i := range idx {
		idx[i] = i
	}
	tree, thr := buildTree(fam.Root, fam.Members, idx, corr, []float64{0.9, 0.7, 0.5})
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
	for k, n := range fam.Members {
		fmt.Printf("  %-8s %+.3f -> x%.3f   %.1f%% -> %.1f%% -> %d%%\n", n, neg[k]-avg,
			handcraft.SharpeFactor(neg[k]-avg, handcraft.Certain), 100*w[k], 100*adj[k], pct[k])
	}
	fmt.Println("\nNo FDM: this is a blend of BOOKS (each variant's own top quintile), not an average of")
	fmt.Println("forecasts, so there is no combined forecast to rescale.")

	est, err := evidence.BootstrapWeights(cols, evidence.Bootstrap{
		MeanBlock: evidence.DefaultBlock(len(cols[0])), Reps: reps0(reps), Seed: *seed,
	}, len(cols[0])/10)
	fatalIf(err)
	fmt.Printf("\ncross-check: Carver's bootstrap, max-Sharpe on %d-day samples (%d resamples; %d uninformative)\n",
		len(cols[0])/10, *reps, est.Uninformative)
	fmt.Println("  read only after the weights above are fixed; it cannot move them")
	for k, n := range fam.Members {
		fmt.Printf("  %-8s handcrafted %3d%%   bootstrap %5.1f%%   single fit %5.1f%%\n", n, pct[k], 100*est.Mean[k], 100*est.Full[k])
	}
}

func reps0(p *int) int { return *p }

// buildTree groups near-duplicates first: at each threshold of the ladder the
// members cluster (complete linkage); the first threshold that leaves three
// groups or fewer is used. A group of two or three stays a group; a group of
// four or more is split the same way with the thresholds above the one used.
func buildTree(label string, names []string, idx []int, corr [][]float64, ladder []float64) (handcraft.Node, float64) {
	leaves := func(ix []int) []handcraft.Node {
		out := make([]handcraft.Node, len(ix))
		for i, j := range ix {
			out[i] = handcraft.Leaf(names[j], j)
		}
		return out
	}
	if len(idx) <= 3 {
		return handcraft.Group(label, leaves(idx)...), 0
	}
	for li, t := range ladder {
		groups := handcraft.Cluster(len(idx), func(a, b int) float64 { return corr[idx[a]][idx[b]] }, t)
		if len(groups) > 3 {
			continue
		}
		var children []handcraft.Node
		for _, g := range groups {
			sub := make([]int, len(g))
			for i, a := range g {
				sub[i] = idx[a]
			}
			var lab []string
			for _, j := range sub {
				lab = append(lab, names[j])
			}
			switch {
			case len(sub) == 1:
				children = append(children, handcraft.Leaf(names[sub[0]], sub[0]))
			case len(sub) <= 3:
				children = append(children, handcraft.Group(strings.Join(lab, "+"), leaves(sub)...))
			default:
				above := []float64{0.95}
				for _, u := range ladder[:li] {
					if u > t {
						above = append(above, u)
					}
				}
				sort.Sort(sort.Reverse(sort.Float64Slice(above)))
				node, _ := buildTree(strings.Join(lab, "+"), names, sub, corr, above)
				children = append(children, node)
			}
		}
		return handcraft.Group(label, children...), t
	}
	// Four or more apart even at the lowest threshold: one group, which Table 8
	// row 3 accepts only if every rounded correlation is the same.
	return handcraft.Group(label, leaves(idx)...), ladder[len(ladder)-1]
}

// comboConstruction splits the two tracks' books by Table 8 row 2 and tilts
// the split by Table 12 column A on the two branches' costs.
func comboConstruction(fam blendFamily, cols [][]float64, costSR []float64, reps int, seed int64) {
	mom := paper.MomentumLookbackBlendSpec()
	lv, _ := paper.SpecFor("low-volatility-blend")
	nm := len(mom.Variants)
	branch := func(off int, w []float64) ([]float64, float64) {
		out := make([]float64, len(cols[0]))
		var cost float64
		for k, wk := range w {
			cost += wk * costSR[off+k]
			for t := range out {
				out[t] += wk * cols[off+k][t]
			}
		}
		return out, cost
	}
	bm, cm := branch(0, mom.VariantWeights)
	bl, cl := branch(nm, lv.VariantWeights)
	corr := evidence.Correlation([][]float64{bm, bl}, nil)
	fmt.Println("\neach branch at its own track's frozen weights")
	fmt.Printf("  momentum        %v over %v   cost %.3f SR/yr\n", mom.VariantWeights, mom.Variants, cm)
	fmt.Printf("  low volatility  %v over %v   cost %.3f SR/yr\n", lv.VariantWeights, lv.Variants, cl)
	fmt.Printf("\ncorrelation of the two branches' daily returns in excess of equal-weight: %+.2f\n", corr[0][1])
	fmt.Println("(Table 8 row 2 splits two branches in halves whatever it is; it is printed as the diversification evidence)")

	tree := handcraft.Group(fam.Root, handcraft.Leaf("momentum", 0), handcraft.Leaf("low volatility", 1))
	w, steps, err := handcraft.Weights(tree, corr)
	fatalIf(err)
	fmt.Println("\nhandcrafted (Table 8)")
	for _, s := range steps {
		fmt.Println("  " + s.String())
	}
	neg := []float64{-cm, -cl}
	avg := (neg[0] + neg[1]) / 2
	adj := handcraft.AdjustForSharpe(w, neg, handcraft.Certain)
	fmt.Println("\nTable 12 column A on the branches' cost difference")
	for k, n := range []string{"momentum", "low volatility"} {
		fmt.Printf("  %-15s %+.3f -> x%.3f   %.1f%% -> %.1f%%\n", n, neg[k]-avg,
			handcraft.SharpeFactor(neg[k]-avg, handcraft.Certain), 100*w[k], 100*adj[k])
	}
	var final []float64
	for _, v := range mom.VariantWeights {
		final = append(final, adj[0]*v)
	}
	for _, v := range lv.VariantWeights {
		final = append(final, adj[1]*v)
	}
	pct := wholePercent(final)
	fmt.Println("\nfinal member weights (branch weight x the track's own weight), whole percent")
	for k, n := range fam.Members {
		fmt.Printf("  %-8s %.1f%% -> %d%%\n", n, 100*final[k], pct[k])
	}
	est, err := evidence.BootstrapWeights([][]float64{bm, bl}, evidence.Bootstrap{
		MeanBlock: evidence.DefaultBlock(len(bm)), Reps: reps, Seed: seed,
	}, len(bm)/10)
	fatalIf(err)
	fmt.Printf("\ncross-check: Carver's bootstrap on the two branches (%d resamples; %d uninformative), read after the weights\n", reps, est.Uninformative)
	fmt.Printf("  momentum %.1f%%, low volatility %.1f%% (single fit %.1f%% / %.1f%%)\n",
		100*est.Mean[0], 100*est.Mean[1], 100*est.Full[0], 100*est.Full[1])
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

// screenFamily is a blend's pre-registered kill screen: the frozen weights
// (read from the spec, so they cannot drift from what paper trades) applied to
// the member books, against the same weighting of the members' controls.
// Summing member books charges a name held by two variants twice, which the
// real book nets — conservative. A low-risk blend is judged at matched risk.
func screenFamily(fam blendFamily, res []sleeve.RuleResult, start time.Time, from, to string, floor, costBps float64) {
	spec, ok := paper.SpecFor(fam.SpecName)
	if !ok {
		fatal(fmt.Errorf("%s is not registered", fam.SpecName))
	}
	if strings.Join(spec.Variants, ",") != strings.Join(fam.Members, ",") {
		fatal(fmt.Errorf("spec variants %v do not match the members %v", spec.Variants, fam.Members))
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
	title := strings.ToLower(fam.Title)
	if fam.Name == "momentum" {
		title = "lookback blend"
	}
	sig := comp(title, func(r sleeve.RuleResult) sleeve.Book { return r.Signal })
	eq := comp("equal-weight", func(r sleeve.RuleResult) sleeve.Book { return r.EqualWeight })
	sh := comp("stable-shuffle", func(r sleeve.RuleResult) sleeve.Book { return r.StableShuffled })

	boot := evidence.Bootstrap{MeanBlock: 5, Reps: 10000, Seed: 1}
	edges := func(a, e, h sleeve.Book) (evidence.Edge, evidence.Edge, float64, float64) {
		_, ma := sleeve.Monthly(a)
		_, me := sleeve.Monthly(e)
		_, mh := sleeve.Monthly(h)
		eEq, err := evidence.PairedEdge(ma, me, boot, 0.90)
		fatalIf(err)
		eSh, err := evidence.PairedEdge(ma, mh, boot, 0.90)
		fatalIf(err)
		return eEq, eSh, 0, 0
	}
	rawEq, rawSh, _, _ := edges(sig, eq, sh)
	raw2Eq := sleeve.PairedMonthly(doubleCost(sig), doubleCost(eq)).MeanDiff
	raw2Sh := sleeve.PairedMonthly(doubleCost(sig), doubleCost(sh)).MeanDiff

	fmt.Printf("%s — KILL SCREEN (trials=1, pre-registered 2026-09-12). %s..%s, Rs %.0f cr, %.0f bps\n", fam.Title, from, to, floor/1e7, costBps)
	fmt.Printf("weights %v over %v, from %s (every member live). Exploration years: this can kill, never confirm.\n\n",
		spec.VariantWeights, spec.Variants, start.Format("2006-01-02"))
	for _, b := range []sleeve.Book{sig, eq, sh} {
		s := sleeve.Summarize(b)
		fmt.Printf("  %-18s %7.2f%%/yr  vol %6.2f%%  SR %5.2f  maxDD %6.1f%%  turnover %5.2f%%/day\n",
			b.Name, 100*s.AnnReturn, 100*s.AnnVol, s.SR, 100*s.MaxDD, 100*s.MeanTurnover)
	}
	fmt.Printf("\nedge, monthly paired, %d months, 90%% bootstrap intervals\n", rawEq.N)
	fmt.Printf("  vs equal-weight    %s\n", edgeCell(rawEq))
	fmt.Printf("  vs stable-shuffle  %s\n", edgeCell(rawSh))
	fmt.Printf("  at 100 bps: vs equal-weight %+.2f%%/mo, vs stable-shuffle %+.2f%%/mo\n", 100*raw2Eq, 100*raw2Sh)

	dEq, dSh, d2Eq, d2Sh := rawEq, rawSh, raw2Eq, raw2Sh
	if fam.Matched {
		_, ma := sleeve.Monthly(atRisk(sig, eq))
		_, me := sleeve.Monthly(eq)
		_, mb := sleeve.Monthly(atRisk(sig, sh))
		_, mh := sleeve.Monthly(sh)
		var err error
		dEq, err = evidence.PairedEdge(ma, me, boot, 0.90)
		fatalIf(err)
		dSh, err = evidence.PairedEdge(mb, mh, boot, 0.90)
		fatalIf(err)
		d2Eq = sleeve.PairedMonthly(atRisk(doubleCost(sig), doubleCost(eq)), doubleCost(eq)).MeanDiff
		d2Sh = sleeve.PairedMonthly(atRisk(doubleCost(sig), doubleCost(sh)), doubleCost(sh)).MeanDiff
		fmt.Println("\nedge at MATCHED risk (the book scaled, with hindsight, to each control's volatility) — the deciding statistic")
		fmt.Printf("  vs equal-weight    %s\n", edgeCell(dEq))
		fmt.Printf("  vs stable-shuffle  %s\n", edgeCell(dSh))
		fmt.Printf("  at 100 bps: vs equal-weight %+.2f%%/mo, vs stable-shuffle %+.2f%%/mo\n", 100*d2Eq, 100*d2Sh)
	}
	k1 := dEq.P < 0.10 && dSh.P < 0.10
	k2 := d2Eq > 0 && d2Sh > 0
	fmt.Printf("\n  K1  p < 0.10 against both controls at 50 bps        %s (p %.4f, %.4f)\n", passFail(k1), dEq.P, dSh.P)
	fmt.Printf("  K2  positive mean against both controls at 100 bps  %s\n", passFail(k2))
	v := "SURVIVES — the forward track may start"
	if !k1 || !k2 {
		v = "KILLED — the forward track does not start"
	}
	fmt.Printf("  VERDICT: %s\n", v)
}
