package main

import (
	"flag"
	"fmt"
	"time"

	"github.com/ranedk/systrader/internal/backtest"
	"github.com/ranedk/systrader/internal/evidence"
	"github.com/ranedk/systrader/internal/research"
	"github.com/ranedk/systrader/internal/rules"
	"github.com/ranedk/systrader/internal/sleeve"
)

// The kill screen for the speed blend, exactly as pre-registered in
// research/preregistrations/2026-09-10_speed_blend_screen.md. One
// configuration, frozen in code before this ran (rules.SpeedBlend,
// paper.SpeedBlendSpec), trials=1.
//
// Like row 18 it runs on the mined 2013-2026 sample, so it can kill and cannot
// confirm. The criteria are the registration's, not chosen here:
//
//	K1  at 50 bps, one-sided block-bootstrap p >= 0.10 against EITHER control
//	K2  at 100 bps, a negative mean monthly difference against either control
//
// Everything else it prints — the matched-risk comparison with trend-quintile's
// own book above all — is reported and decides nothing. Choosing between the
// two on this sample would be pick-the-winner.

func runBlend(args []string) {
	fs := flag.NewFlagSet("blend", flag.ExitOnError)
	cache := fs.String("cache", defaultCache, "bar cache file")
	from := fs.String("from", "2013-07-01", "first decision date")
	to := fs.String("to", "2026-06-30", "last decision date")
	costBps := fs.Float64("cost-bps", 50, "round-trip cost in basis points")
	floor := fs.Float64("floor", 1e8, "liquidity floor (the spec's Rs 10cr)")
	rebalance := fs.Int("rebalance", 20, "trade every N decision days (the spec's 20)")
	reps := fs.Int("reps", 10000, "bootstrap resamples")
	seed := fs.Int64("seed", 1, "bootstrap seed — declared, never drawn")
	ledger := fs.String("ledger", "research/LEDGER.md", "ledger, for the workspace-wide bar printed as context")
	fatalIf(fs.Parse(args))

	names := make([]string, len(variants))
	for i, v := range variants {
		names[i] = v.name
	}
	cfg := func(mult float64) sleeve.Config {
		return sleeve.Config{CostBpsRoundTrip: *costBps * mult, Seeds: []int64{1, 2, 3, 4, 5}, RebalanceEvery: *rebalance}
	}
	run := func(rule rules.Rule, mult float64, days []sleeve.Day) sleeve.RuleResult {
		res, err := sleeve.Run(days, names, cfg(mult))
		fatalIf(err)
		return res[0] // always-trend: top quintile, the spec's selection
	}

	blend := rules.SpeedBlend()
	fatalIf(rules.Validate(blend))
	days := buildCost(*cache, blend, mustDate(*from), mustDate(*to), *floor)
	b1 := run(blend, 1, days)
	b2 := run(blend, 2, days)
	days = nil
	frozenRule := findRule("ewmac32_128")
	fdays := buildCost(*cache, frozenRule, mustDate(*from), mustDate(*to), *floor)
	f1 := run(frozenRule, 1, fdays)
	fdays = nil

	fmt.Printf("SPEED-BLEND KILL SCREEN — %s..%s, Rs %.0f cr floor, top quintile, rebalance %d\n", *from, *to, *floor/1e7, *rebalance)
	fmt.Printf("signal: %s = EWMAC16/32/64 at %.0f/%.0f/%.0f%%, FDM %.2f | trials=1 | pre-registered 2026-09-10\n",
		blend.Name(), 100*blend.Weights[0], 100*blend.Weights[1], 100*blend.Weights[2], blend.FDM)
	fmt.Println("Mined sample: this can kill the blend, never confirm it.")
	fmt.Println()

	fmt.Printf("  %-26s %8s %8s %6s %8s %11s\n", "book", "ann ret", "ann vol", "SR", "maxDD", "turnover/d")
	line := func(name string, b sleeve.Book) {
		s := sleeve.Summarize(b)
		fmt.Printf("  %-26s %7.2f%% %7.2f%% %6.2f %7.1f%% %10.2f%%\n", name, 100*s.AnnReturn, 100*s.AnnVol, s.SR, 100*s.MaxDD, 100*s.MeanTurnover)
	}
	line("speed blend", b1.Signal)
	line("trend-quintile (ewmac32)", f1.Signal)
	line("equal-weight (beta)", b1.EqualWeight)
	line("stable-shuffle (selection)", b1.StableShuffled)
	line("speed blend at 100 bps", b2.Signal)

	_, sig := sleeve.Monthly(b1.Signal)
	_, eq := sleeve.Monthly(b1.EqualWeight)
	_, sh := sleeve.Monthly(b1.StableShuffled)
	boot := evidence.Bootstrap{MeanBlock: evidence.DefaultBlock(len(sig)), Reps: *reps, Seed: *seed}
	eEq, err := evidence.PairedEdge(sig, eq, boot, 0.90)
	fatalIf(err)
	eSh, err := evidence.PairedEdge(sig, sh, boot, 0.90)
	fatalIf(err)
	e2Eq := sleeve.PairedMonthly(b2.Signal, b2.EqualWeight)
	e2Sh := sleeve.PairedMonthly(b2.Signal, b2.StableShuffled)

	fmt.Printf("\nedge, monthly paired, %d months, 90%% bootstrap intervals (mean block %.0f months, %d resamples)\n", eEq.N, boot.MeanBlock, *reps)
	fmt.Printf("  vs equal-weight    %s\n", edgeCell(eEq))
	fmt.Printf("  vs stable-shuffle  %s\n", edgeCell(eSh))
	fmt.Printf("  at 100 bps: vs equal-weight %+.2f%%/mo, vs stable-shuffle %+.2f%%/mo\n", 100*e2Eq.MeanDiff, 100*e2Sh.MeanDiff)

	k1 := eEq.P < 0.10 && eSh.P < 0.10
	k2 := e2Eq.MeanDiff > 0 && e2Sh.MeanDiff > 0
	fmt.Println("\npre-registered criteria")
	fmt.Printf("  K1  p < 0.10 against both controls at 50 bps        %s (p %.4f, %.4f)\n", passFail(k1), eEq.P, eSh.P)
	fmt.Printf("  K2  positive mean against both controls at 100 bps  %s\n", passFail(k2))
	verdict := "SURVIVES — the forward track may start"
	if !k1 || !k2 {
		verdict = "KILLED — the forward track does not start"
	}
	fmt.Printf("  VERDICT: %s\n", verdict)

	// Reported, deciding nothing.
	fmt.Println("\nreported, deciding nothing")
	ctl, e := eq, eEq
	if eSh.P > eEq.P {
		ctl, e = sh, eSh
	}
	excess := make([]float64, len(sig))
	for i := range sig {
		excess[i] = sig[i] - ctl[i]
	}
	d := evidence.DeflatedSharpe(excess, 1, 0)
	fmt.Printf("  excess over the tougher control: %.3f SR/month, P(true SR > 0) = %.3f (one trial; row 18's family context is LEDGER row 25)\n", d.SR, d.DSR)
	_ = e

	bs, fs2 := alignBooks(b1.Signal, f1.Signal)
	scaled, lev := evidence.ScaleToRisk(bs.Net, fs2.Net)
	sb := sleeve.Book{Name: "blend at trend-quintile's risk", Dates: bs.Dates, Gross: scaled, Net: scaled, Turnover: bs.Turnover}
	fmt.Printf("  matched risk (leverage %.3f on the blend, hindsight-fixed — a reading device):\n", lev)
	line("  blend at matched risk", sb)
	line("  trend-quintile", fs2)
	_, ms := sleeve.Monthly(sb)
	_, mf := sleeve.Monthly(fs2)
	mr, err := evidence.PairedEdge(ms, mf, boot, 0.90)
	fatalIf(err)
	fmt.Printf("  blend − trend-quintile at matched risk: %s\n", edgeCell(mr))
	fmt.Println("  Choosing between these two on this sample would be pick-the-winner (Law 5); it informs nothing here.")
	if m, err := research.CountM(*ledger); err == nil {
		fmt.Printf("\nWorkspace-wide Bonferroni bar, context only (amended Law 2): t = %.2f at M = %d.\n", backtest.BonferroniBar(m), m)
	}
}

func passFail(ok bool) string {
	if ok {
		return "PASS"
	}
	return "FAIL"
}

// alignBooks restricts two books to the dates both traded.
func alignBooks(a, b sleeve.Book) (sleeve.Book, sleeve.Book) {
	idx := map[time.Time]int{}
	for i, d := range b.Dates {
		idx[d] = i
	}
	var oa, ob sleeve.Book
	oa.Name, ob.Name = a.Name, b.Name
	for i, d := range a.Dates {
		j, ok := idx[d]
		if !ok {
			continue
		}
		oa.Dates = append(oa.Dates, d)
		oa.Gross = append(oa.Gross, a.Gross[i])
		oa.Net = append(oa.Net, a.Net[i])
		oa.Turnover = append(oa.Turnover, a.Turnover[i])
		ob.Dates = append(ob.Dates, d)
		ob.Gross = append(ob.Gross, b.Gross[j])
		ob.Net = append(ob.Net, b.Net[j])
		ob.Turnover = append(ob.Turnover, b.Turnover[j])
	}
	return oa, ob
}
