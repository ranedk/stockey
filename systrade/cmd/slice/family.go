package main

import (
	"flag"
	"fmt"
	"math"
	"strings"

	"github.com/ranedk/systrader/internal/evidence"
	"github.com/ranedk/systrader/internal/sleeve"
)

// The row-18 family re-scored with the statistics docs/RESEARCH_PROTOCOL.md
// requires and row 18 predates: a block-bootstrap p against each control, FDR
// within the family, and a deflated Sharpe against the family's own search.
//
// NOT A NEW TRIAL. Every configuration below was already run and read in row
// 18; this reads the same books with better arithmetic. The family is rebuilt
// exactly as the ledger declared it — three portfolio variants at two
// liquidity floors, plus the rule-speed x rebalance plateau at Rs 10cr — and
// the cell the two groups share is judged once while the declared trial count
// stays 15.
//
// Each member is judged against its TOUGHER control: whichever of the
// equal-weight book (beta) and the stable-shuffle ranking (selection) leaves
// the larger p. Its deflated Sharpe is taken on the return in excess of that
// same control, because Law 3 says edge is performance minus the matched
// baseline — deflating the raw book would credit it with the market.

type familyMember struct {
	name   string
	frozen bool
	res    sleeve.RuleResult
}

func runFamily(args []string) {
	fs := flag.NewFlagSet("family", flag.ExitOnError)
	cache := fs.String("cache", defaultCache, "bar cache file")
	from := fs.String("from", "2013-07-01", "first decision date")
	to := fs.String("to", "2026-06-30", "last decision date")
	costBps := fs.Float64("cost-bps", 50, "round-trip cost in basis points")
	trials := fs.Int("trials", 15, "configurations examined in the family (LEDGER row 18 declared 15)")
	q := fs.Float64("fdr", 0.10, "false-discovery rate within the family (amended Law 2)")
	reps := fs.Int("reps", 10000, "bootstrap resamples")
	seed := fs.Int64("seed", 1, "bootstrap seed — declared, never drawn")
	fatalIf(fs.Parse(args))

	names := make([]string, len(variants))
	for i, v := range variants {
		names[i] = v.name
	}
	var members []familyMember

	// One cross-section at a time, used for every rebalance clock and then
	// dropped: holding all four at once is how this workspace met the OOM
	// killer.
	type build struct {
		rule       string
		floor      float64
		rebalances []int
	}
	for _, b := range []build{
		{"ewmac32_128", 1e7, []int{20}},
		{"ewmac32_128", 1e8, []int{10, 20, 40}},
		{"ewmac16_64", 1e8, []int{10, 20, 40}},
		{"ewmac64_256", 1e8, []int{10, 20, 40}},
	} {
		rule := findRule(b.rule)
		if rule == nil {
			fatal(fmt.Errorf("unknown rule %q", b.rule))
		}
		days := buildCost(*cache, rule, mustDate(*from), mustDate(*to), b.floor)
		for _, reb := range b.rebalances {
			res, err := sleeve.Run(days, names, sleeve.Config{
				CostBpsRoundTrip: *costBps,
				Seeds:            []int64{1, 2, 3, 4, 5},
				RebalanceEvery:   reb,
			})
			fatalIf(err)
			speed := strings.SplitN(b.rule, "_", 2)[0]
			if b.rule == "ewmac32_128" && reb == 20 {
				// The portfolio-variant group: all three variants.
				for i, v := range variants {
					members = append(members, familyMember{
						name:   fmt.Sprintf("%s %s r%d Rs%.0fcr", v.name, speed, reb, b.floor/1e7),
						frozen: i == 0 && b.floor == 1e8,
						res:    res[i],
					})
				}
				continue
			}
			// The plateau group: always-trend only.
			members = append(members, familyMember{
				name: fmt.Sprintf("%s %s r%d Rs%.0fcr", variants[0].name, speed, reb, b.floor/1e7),
				res:  res[0],
			})
		}
	}

	type scored struct {
		sum      sleeve.Summary
		eq, sh   evidence.Edge
		deciding string
	}
	sc := make([]scored, len(members))
	judged := make([]evidence.Member, len(members))
	var boot evidence.Bootstrap
	for i, m := range members {
		_, sig := sleeve.Monthly(m.res.Signal)
		_, eq := sleeve.Monthly(m.res.EqualWeight)
		_, sh := sleeve.Monthly(m.res.StableShuffled)
		boot = evidence.Bootstrap{MeanBlock: evidence.DefaultBlock(len(sig)), Reps: *reps, Seed: *seed}
		eEq, err := evidence.PairedEdge(sig, eq, boot, 0.90)
		fatalIf(err)
		eSh, err := evidence.PairedEdge(sig, sh, boot, 0.90)
		fatalIf(err)
		ctl, e, deciding := eq, eEq, "equal-wt"
		if eSh.P > eEq.P {
			ctl, e, deciding = sh, eSh, "shuffle"
		}
		excess := make([]float64, len(sig))
		for t := range sig {
			excess[t] = sig[t] - ctl[t]
		}
		judged[i] = evidence.Member{Name: m.name, Returns: excess, P: e.P}
		sc[i] = scored{sum: sleeve.Summarize(m.res.Signal), eq: eEq, sh: eSh, deciding: deciding}
	}
	verdicts, err := evidence.Judge(judged, *trials, *q)
	fatalIf(err)

	fmt.Printf("ROW-18 FAMILY RE-SCORED — %s..%s, %.0f bps round trip, monthly paired differences\n", *from, *to, *costBps)
	fmt.Printf("%d distinct members, %d trials declared | FDR q = %.2f | stationary bootstrap: mean block %.0f months, %d resamples, seed %d\n",
		len(members), *trials, *q, boot.MeanBlock, *reps, *seed)
	fmt.Println("Not a new trial: the same books row 18 already read, judged with the protocol's arithmetic.")
	fmt.Println("Intervals are 90% bootstrap intervals on the mean monthly difference; p is one-sided, no-edge.")
	fmt.Println()
	fmt.Printf("  %-34s %7s %5s | %-26s | %-26s | %-8s %6s %6s %s\n",
		"member", "ann", "SR", "vs equal-weight %/mo", "vs stable-shuffle %/mo", "decides", "q", "DSR", "")
	for i, m := range members {
		s, v := sc[i], verdicts[i]
		mark := " "
		if m.frozen {
			mark = "*"
		}
		verdict := "—"
		switch {
		case v.Discovery && v.Deflated.DSR >= 0.95:
			verdict = "survives both"
		case v.Discovery:
			verdict = "FDR only"
		case v.Deflated.DSR >= 0.95:
			verdict = "DSR only"
		}
		fmt.Printf("%s %-34s %6.2f%% %5.2f | %s | %s | %-8s %6.3f %6.3f %s\n",
			mark, m.name, 100*s.sum.AnnReturn, s.sum.SR, edgeCell(s.eq), edgeCell(s.sh), s.deciding, v.Q, v.Deflated.DSR, verdict)
	}
	if len(verdicts) > 0 {
		b := verdicts[0].Deflated
		fmt.Printf("\nDeflation benchmark: the best of %d no-skill configurations with this family's spread of Sharpe\n", b.Trials)
		fmt.Printf("ratios would show %.3f per month (%.2f annualised) on excess returns. DSR = P(true excess SR > that).\n",
			b.Benchmark, b.Benchmark*math.Sqrt(12))
	}
	fmt.Println("* = the frozen paper-trading configuration (docs/strategies/2026-09-08_trend_quintile.md).")
}

func edgeCell(e evidence.Edge) string {
	return fmt.Sprintf("%+5.2f [%+5.2f,%+5.2f] p%.3f", 100*e.MeanDiff, 100*e.Low, 100*e.High, e.P)
}
