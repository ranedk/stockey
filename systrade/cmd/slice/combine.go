package main

import (
	"flag"
	"fmt"
	"math"
	"sync"
	"time"

	"github.com/ranedk/systrader/internal/bars"
	"github.com/ranedk/systrader/internal/evidence"
	"github.com/ranedk/systrader/internal/paper"
	"github.com/ranedk/systrader/internal/policy"
	"github.com/ranedk/systrader/internal/research"
	"github.com/ranedk/systrader/internal/sleeve"
	"github.com/ranedk/systrader/internal/traits"
)

// `slice combine` — the pre-registered test of combination policies beyond
// the handcrafted one (research/preregistrations/2026-09-23_combination_policies.md,
// the README item open since LEDGER row 34).
//
// The live combination track holds the momentum blend and the low-volatility
// blend at a fixed 48/52, set from costs and correlation and never from
// returns. This asks whether moving that split beats leaving it alone: on
// realised performance (Hedge, four memories), on realised risk (risk parity,
// three windows), or on the market regime (a purged walk-forward ridge). One
// family, eight trials, judged against the handcrafted book itself and
// against the same equal-weight and stable-shuffle controls the parents used.
//
// Every policy pays for its own adaptation — moving the split is a round trip
// on the capital moved — because a policy that adapts for free is not one
// anyone could run.

const (
	comboTrials    = 8
	comboFitYears  = 3
	comboStepYears = 1
	comboPurgeDays = 31 // the 21-trading-day decision horizon in calendar days
	comboRidgeLag  = 12 // periods the time-shifted control reads stale features
	comboLambda    = 1.0
)

func runCombine(args []string) {
	fs := flag.NewFlagSet("combine", flag.ExitOnError)
	cache := fs.String("cache", defaultCache, "bar cache file")
	from := fs.String("from", "2013-07-01", "first decision date")
	to := fs.String("to", "2021-12-31", "last decision date — the confirmation years stay unread")
	floor := fs.Float64("floor", 1e8, "liquidity floor (Rs 10cr, the paper tracks' universe)")
	costBps := fs.Float64("cost-bps", 50, "round-trip cost in basis points (100 bps is reported alongside)")
	reps := fs.Int("reps", 10000, "bootstrap resamples")
	seed := fs.Int64("seed", 1, "bootstrap seed — declared, never drawn")
	q := fs.Float64("fdr", 0.10, "false-discovery rate within the family")
	readConfirm := fs.Bool("include-confirmation-years", false, "let -to reach 2022 onward")
	fatalIf(fs.Parse(args))
	guardConfirmationYears(mustDate(*to), *readConfirm)

	mom := paper.MomentumLookbackBlendSpec()
	lv, ok := paper.SpecFor("low-volatility-blend")
	if !ok {
		fatal(fmt.Errorf("the combination needs the low-volatility track frozen first"))
	}
	members := append(append([]string(nil), mom.Variants...), lv.Variants...)

	mkt, _, err := traits.MarketReturns(*cache, *floor)
	fatalIf(err)
	breadth, err := breadthSeries(*cache, *floor)
	fatalIf(err)

	days := buildScores(*cache, memberScorers(members, mkt), mustDate(*from), mustDate(*to), *floor, month+3)
	res, err := sleeve.Run(days, members, sleeve.Config{
		CostBpsRoundTrip: *costBps, Seeds: []int64{1, 2, 3, 4, 5}, RebalanceEvery: month,
	})
	fatalIf(err)

	var start time.Time
	for _, r := range res {
		if t := firstTraded(r.Signal); t.After(start) {
			start = t
		}
	}

	// Each branch is its own track's frozen weights over its own members —
	// the books row 34 combined, rebuilt here unchanged.
	pick := func(off, n int, w []float64, name string, get func(sleeve.RuleResult) sleeve.Book) sleeve.Book {
		var out sleeve.Book
		for k := 0; k < n; k++ {
			b := trimFrom(get(res[off+k]), start)
			if out.Dates == nil {
				out = sleeve.Book{Name: name, Dates: b.Dates, Gross: make([]float64, len(b.Dates)),
					Net: make([]float64, len(b.Dates)), Turnover: make([]float64, len(b.Dates))}
			}
			if len(b.Dates) != len(out.Dates) {
				fatal(fmt.Errorf("member books misaligned: %d vs %d days", len(b.Dates), len(out.Dates)))
			}
			for i := range b.Dates {
				if !b.Dates[i].Equal(out.Dates[i]) {
					fatal(fmt.Errorf("member books disagree on day %d: %s vs %s", i, b.Dates[i], out.Dates[i]))
				}
				out.Gross[i] += w[k] * b.Gross[i]
				out.Net[i] += w[k] * b.Net[i]
				out.Turnover[i] += w[k] * b.Turnover[i]
			}
		}
		return out
	}
	nm := len(mom.Variants)
	sigFn := func(r sleeve.RuleResult) sleeve.Book { return r.Signal }
	eqFn := func(r sleeve.RuleResult) sleeve.Book { return r.EqualWeight }
	shFn := func(r sleeve.RuleResult) sleeve.Book { return r.StableShuffled }
	branches := [][]sleeve.Book{
		{pick(0, nm, mom.VariantWeights, "momentum", sigFn), pick(nm, len(lv.Variants), lv.VariantWeights, "low risk", sigFn)},
		{pick(0, nm, mom.VariantWeights, "momentum eq", eqFn), pick(nm, len(lv.Variants), lv.VariantWeights, "low risk eq", eqFn)},
		{pick(0, nm, mom.VariantWeights, "momentum sh", shFn), pick(nm, len(lv.Variants), lv.VariantWeights, "low risk sh", shFn)},
	}

	dates := branches[0][0].Dates
	at := monthlyGrid(len(dates))
	feats := comboFeatures(dates, at, mkt, breadth, branches[0][0].Net, branches[0][1].Net)
	hist := policy.History{
		Dates:     dates,
		Daily:     [][]float64{branches[0][0].Net, branches[0][1].Net},
		Rebalance: at,
		Features:  feats,
	}

	var folds []policy.Fold
	for _, w := range research.PurgedWalkForward(dates[0], dates[len(dates)-1], comboFitYears, comboStepYears, comboPurgeDays) {
		folds = append(folds, policy.Fold{FitEnd: w.FitEnd, ValStart: w.ValStart, ValEnd: w.ValEnd})
	}

	incumbent := policy.Fixed{W: policy.Incumbent, Label: "handcrafted 48/52"}
	cells := []policy.Policy{
		policy.Hedge{HalfLife: 6}, policy.Hedge{HalfLife: 12}, policy.Hedge{HalfLife: 24}, policy.Hedge{HalfLife: 48},
		policy.RiskParity{Window: 63}, policy.RiskParity{Window: 126}, policy.RiskParity{Window: 252},
		policy.Ridge{Lambda: comboLambda, Folds: folds, Label: "ridge (regime)"},
	}
	if len(cells) != comboTrials {
		fatal(fmt.Errorf("%d cells but %d trials declared", len(cells), comboTrials))
	}
	twin := policy.Ridge{Lambda: comboLambda, Folds: folds, LagPeriods: comboRidgeLag,
		Label: fmt.Sprintf("ridge, features lagged %d months", comboRidgeLag)}

	run := func(p policy.Policy) (sig, eq, sh sleeve.Book, blended policy.Blended) {
		path, err := p.Weights(hist)
		fatalIf(err)
		books := make([]sleeve.Book, 3)
		for j, pair := range branches {
			b, err := policy.Blend([]policy.Branch{
				{Gross: pair[0].Gross, Net: pair[0].Net, Turnover: pair[0].Turnover},
				{Gross: pair[1].Gross, Net: pair[1].Net, Turnover: pair[1].Turnover},
			}, path, at, *costBps)
			fatalIf(err)
			books[j] = sleeve.Book{Name: p.Name(), Dates: dates, Gross: b.Gross, Net: b.Net, Turnover: b.Turnover}
			if j == 0 {
				blended = b
			}
		}
		books[1].Name, books[2].Name = "equal-weight", "stable-shuffle"
		return books[0], books[1], books[2], blended
	}

	baseSig, baseEq, baseSh, baseBlend := run(incumbent)
	// The spec's own nine weights, as the live track holds them, so the
	// reader can see that the whole-percent rounding is immaterial.
	specBook := pick(0, len(members), paper.MomentumLowVolCombinationSpec().VariantWeights, "spec 16/16/8/8+6/6/6/17/17", sigFn)

	boot := evidence.Bootstrap{MeanBlock: 5, Reps: *reps, Seed: *seed}
	monthly := func(b sleeve.Book) []float64 { _, r := sleeve.Monthly(b); return r }
	edge := func(cand, ctl sleeve.Book) evidence.Edge {
		e, err := evidence.PairedEdge(monthly(atRisk(cand, ctl)), monthly(ctl), boot, 0.90)
		fatalIf(err)
		return e
	}
	edge2 := func(cand, ctl sleeve.Book) float64 {
		c2, k2 := doubleCost(cand), doubleCost(ctl)
		return sleeve.PairedMonthly(atRisk(c2, k2), k2).MeanDiff
	}
	diffSeries := func(cand, ctl sleeve.Book) []float64 {
		a, b := monthly(atRisk(cand, ctl)), monthly(ctl)
		out := make([]float64, len(a))
		for i := range a {
			out[i] = a[i] - b[i]
		}
		return out
	}

	fmt.Printf("COMBINATION POLICIES — pre-registered 2026-09-23 (trials=%d). %s..%s, Rs %.0f cr floor, %.0f bps\n",
		comboTrials, *from, *to, *floor/1e7, *costBps)
	fmt.Printf("branches: momentum lookback blend %v over %v; low-volatility blend %v over %v\n",
		mom.VariantWeights, mom.Variants, lv.VariantWeights, lv.Variants)
	fmt.Printf("%d trading days from %s, %d monthly decisions; the split moves on a %d-day clock and pays for moving.\n",
		len(dates), dates[0].Format("2006-01-02"), len(at), month)
	fmt.Printf("ridge folds: %d expanding windows, %d-year minimum fit, %d-year blocks, %d calendar days purged.\n\n",
		len(folds), comboFitYears, comboStepYears, comboPurgeDays)

	fmt.Println("the books")
	fmt.Printf("  %-28s %6s %7s %7s %6s %6s %7s %8s\n", "", "w_mom", "moved", "%/yr", "vol", "SR", "maxDD", "turnover")
	line := func(name string, b sleeve.Book, bl policy.Blended) {
		s := sleeve.Summarize(b)
		fmt.Printf("  %-28s %5.2f  %5.1f%%  %6.2f%% %5.2f%% %6.2f %6.1f%% %7.2f%%\n",
			name, bl.MeanWeight, 100*bl.WeightTurnover, 100*s.AnnReturn, 100*s.AnnVol, s.SR, 100*s.MaxDD, 100*s.MeanTurnover)
	}
	line("handcrafted 48/52", baseSig, baseBlend)
	type cell struct {
		name          string
		sig           sleeve.Book
		bl            policy.Blended
		eEq           evidence.Edge
		eSh           evidence.Edge
		eBase, e2Base float64
		edgeBase      evidence.Edge
		diff          []float64
	}
	var cs []cell
	for _, p := range cells {
		sig, eq, sh, bl := run(p)
		line(p.Name(), sig, bl)
		c := cell{name: p.Name(), sig: sig, bl: bl,
			eEq: edge(sig, eq), eSh: edge(sig, sh), edgeBase: edge(sig, baseSig),
			e2Base: edge2(sig, baseSig), diff: diffSeries(sig, baseSig)}
		c.eBase = c.edgeBase.MeanDiff
		cs = append(cs, c)
	}
	sb := sleeve.Summarize(specBook)
	fmt.Printf("\n  for reference, the live track's own nine weights: %.2f%%/yr, vol %.2f%%, SR %.2f — the whole-percent\n",
		100*sb.AnnReturn, 100*sb.AnnVol, sb.SR)
	fmt.Println("  rounding of 48/52 across nine members, not a different policy. The comparisons below use the")
	fmt.Println("  48/52 branch blend, so a difference is the POLICY and never the rounding.")

	fmt.Println("\nedge over the HANDCRAFTED combination — monthly paired, at matched risk, 90% bootstrap (the deciding statistic)")
	members2 := make([]evidence.Member, len(cs))
	for i, c := range cs {
		members2[i] = evidence.Member{Name: c.name, Returns: c.diff, P: c.edgeBase.P}
	}
	verdicts, err := evidence.Judge(members2, comboTrials, *q)
	fatalIf(err)
	fmt.Printf("  %-28s %-28s %9s %7s %6s\n", "", "edge %/mo [90%] p", "100bps", "FDR q", "DSR")
	for i, c := range cs {
		fmt.Printf("  %-28s %-28s %+8.2f %7.3f %6.3f\n", c.name, edgeCell(c.edgeBase), 100*c.e2Base,
			verdicts[i].Q, verdicts[i].Deflated.DSR)
	}
	fmt.Println("\n  The deflated Sharpe here is computed on the DIFFERENCE series — the improvement over the")
	fmt.Println("  incumbent is what these eight trials searched for, so it is what the family's best draw must beat.")

	fmt.Println("\nfor comparison with rows 33-34, the same books against the parents' own controls (matched risk)")
	fmt.Printf("  %-28s %-28s %-28s\n", "", "vs equal-weight", "vs stable-shuffle")
	fmt.Printf("  %-28s %-28s %-28s\n", "handcrafted 48/52", edgeCell(edge(baseSig, baseEq)), edgeCell(edge(baseSig, baseSh)))
	for _, c := range cs {
		fmt.Printf("  %-28s %-28s %-28s\n", c.name, edgeCell(c.eEq), edgeCell(c.eSh))
	}

	// Arm C's own control: the same machinery reading stale features.
	twinSig, _, _, twinBl := run(twin)
	twinEdge := edge(cs[len(cs)-1].sig, twinSig)
	ts := sleeve.Summarize(twinSig)
	fmt.Printf("\narm C's time-shifted twin (%s): %.2f%%/yr, vol %.2f%%, SR %.2f, mean w_mom %.2f\n",
		twin.Name(), 100*ts.AnnReturn, 100*ts.AnnVol, ts.SR, twinBl.MeanWeight)
	fmt.Printf("  ridge vs its twin, matched risk: %s\n", edgeCell(twinEdge))

	fmt.Println("\npre-registered decision")
	anyPass := false
	for i, c := range cs {
		p1 := c.edgeBase.P < 0.10 && verdicts[i].Discovery
		p2 := c.e2Base > 0
		p3 := true
		if i == len(cs)-1 {
			p3 = twinEdge.MeanDiff > 0
		}
		pass := p1 && p2 && p3
		anyPass = anyPass || pass
		note := ""
		if i == len(cs)-1 {
			note = fmt.Sprintf("  P3 %s", passFail(p3))
		}
		fmt.Printf("  %-28s P1 %s  P2 %s%s   %s\n", c.name, passFail(p1), passFail(p2), note,
			map[bool]string{true: "CANDIDATE", false: "no"}[pass])
	}
	if anyPass {
		fmt.Println("\n  VERDICT: at least one policy is a CANDIDATE for a new pre-registered forward track.")
		fmt.Println("  Law 5 still forbids taking the best cell: an arm enters as the handcrafted blend of its")
		fmt.Println("  survivors or not at all. Nothing about the live track changes today.")
		return
	}
	fmt.Println("\n  VERDICT: NO POLICY SURVIVES — the handcrafted 48/52 stands. The README item closes as")
	fmt.Println("  answered and the live combination track is unchanged.")
}

// monthlyGrid is the clock the split moves on: every `month` trading days
// from the first common day. The branches keep their own rebalance clocks;
// this one decides only how much of each is held.
func monthlyGrid(n int) []int {
	var out []int
	for i := 0; i < n; i += month {
		out = append(out, i)
	}
	return out
}

// comboFeatures builds the conditioning variables for the ridge, each read
// from data STRICTLY BEFORE its decision day: the market's 12-month trailing
// return, its 60-day realised volatility, breadth (the share of the eligible
// universe above its 200-day average), and the branches' own 12-month
// trailing return difference. A decision without the full history gets NaN
// and the ridge leaves it at the incumbent.
func comboFeatures(dates []time.Time, at []int, mkt, breadth map[time.Time]float64, mom, low []float64) [][]float64 {
	mr := make([]float64, len(dates))
	for i, d := range dates {
		v, ok := mkt[traits.Day(d)]
		if !ok {
			v = math.NaN()
		}
		mr[i] = v
	}
	trail := func(x []float64, end, win int) float64 {
		if end-win < 0 {
			return math.NaN()
		}
		acc := 1.0
		for i := end - win; i < end; i++ {
			if math.IsNaN(x[i]) {
				return math.NaN()
			}
			acc *= 1 + x[i]
		}
		return acc - 1
	}
	vol := func(x []float64, end, win int) float64 {
		if end-win < 0 {
			return math.NaN()
		}
		var n, sum float64
		for i := end - win; i < end; i++ {
			if math.IsNaN(x[i]) {
				return math.NaN()
			}
			n, sum = n+1, sum+x[i]
		}
		m := sum / n
		var ss float64
		for i := end - win; i < end; i++ {
			ss += (x[i] - m) * (x[i] - m)
		}
		return math.Sqrt(ss/(n-1)) * math.Sqrt(252)
	}
	out := make([][]float64, len(at))
	for k, i := range at {
		b := math.NaN()
		if i > 0 {
			if v, ok := breadth[traits.Day(dates[i-1])]; ok {
				b = v
			}
		}
		out[k] = []float64{
			trail(mr, i, 252),
			vol(mr, i, 60),
			b,
			trail(mom, i, 252) - trail(low, i, 252),
		}
	}
	return out
}

// breadthSeries is the share of the eligible universe trading above its
// 200-day average, by day — the market regime read off the cross-section
// itself, the same way internal/explore's BreadthLabel does it. Computed in
// its own pass so the member books stay byte-identical to the rows that built
// them.
func breadthSeries(cache string, floor float64) (map[time.Time]float64, error) {
	var mu sync.Mutex
	up := map[time.Time]float64{}
	tot := map[time.Time]float64{}
	err := bars.ScanParallel(cache, 0, func(ser bars.Series) {
		b := ser.Bars
		if len(b) < trendWin+1 {
			return
		}
		turn := bars.MedianTurnover(b, turnoverWin)
		type row struct {
			d     time.Time
			above bool
		}
		var local []row
		var sum float64
		for i := range b {
			sum += b[i].Close
			if i >= trendWin {
				sum -= b[i-trendWin].Close
			}
			if i < trendWin-1 || math.IsNaN(turn[i]) || turn[i] < floor || b[i].Close <= 0 {
				continue
			}
			local = append(local, row{b[i].Date, b[i].Close > sum/float64(trendWin)})
		}
		if len(local) == 0 {
			return
		}
		mu.Lock()
		for _, r := range local {
			d := traits.Day(r.d)
			tot[d]++
			if r.above {
				up[d]++
			}
		}
		mu.Unlock()
	})
	if err != nil {
		return nil, err
	}
	out := make(map[time.Time]float64, len(tot))
	for d, n := range tot {
		if n > 0 {
			out[d] = up[d] / n
		}
	}
	return out, nil
}
