package main

import (
	"flag"
	"fmt"
	"math"
	"sort"
	"strings"
	"time"

	"github.com/ranedk/systrader/internal/backtest"
	"github.com/ranedk/systrader/internal/bars"
	"github.com/ranedk/systrader/internal/evidence"
	"github.com/ranedk/systrader/internal/research"
	"github.com/ranedk/systrader/internal/sleeve"
	"github.com/ranedk/systrader/internal/traits"
)

// The low-volatility anomaly across time windows, exactly as pre-registered in
// research/preregistrations/2026-09-12_low_volatility.md: six risk measures x
// three holding periods, each book the LOWEST-risk fifth, on the exploration
// years only.
//
// The published claim is about return per unit of risk — low-risk stocks earn
// about as much as risky ones — so the deciding statistic is the edge at
// MATCHED risk: the book levered, as a reading device fixed with hindsight, to
// the realised volatility of the control it is compared with (row 24's
// standard). A cash book cannot actually lever; raw return, volatility and
// drawdown are printed beside it.

var lowVolHolds = []int{1, 3, 6}

// lowVolScorers score LOWER risk higher, so the top quintile is the calmest
// fifth. A measure needs 80% of its window's daily returns; beta and residual
// volatility need 200 of 252, as internal/traits does.
func lowVolScorers(mkt map[time.Time]float64) []scorer {
	var out []scorer
	for _, n := range []string{"vol1", "vol3", "vol6", "vol12", "beta12", "idio12"} {
		n := n
		out = append(out, scorer{Name: n, Score: func(b []bars.Bar) []float64 {
			s, err := traits.LowRiskScore(n, b, mkt)
			fatalIf(err)
			return s
		}})
	}
	return out
}

// atRisk levers a book's daily returns to a reference book's realised
// volatility (evidence.ScaleToRisk) — costs and turnover scale with it.
func atRisk(b, ref sleeve.Book) sleeve.Book {
	scaled, lev := evidence.ScaleToRisk(b.Net, ref.Net)
	out := sleeve.Book{Name: b.Name, Dates: b.Dates, Net: scaled,
		Gross: make([]float64, len(b.Gross)), Turnover: make([]float64, len(b.Turnover))}
	for i := range b.Gross {
		out.Gross[i] = b.Gross[i] * lev
		out.Turnover[i] = b.Turnover[i] * lev
	}
	return out
}

func runLowVol(args []string) {
	fs := flag.NewFlagSet("lowvol", flag.ExitOnError)
	cache := fs.String("cache", defaultCache, "bar cache file")
	from := fs.String("from", "2013-07-01", "first decision date")
	to := fs.String("to", "2021-12-31", "last decision date — the confirmation years stay unread")
	floor := fs.Float64("floor", 1e8, "liquidity floor (Rs 10cr, the tradable universe)")
	costBps := fs.Float64("cost-bps", 50, "round-trip cost in basis points (100 bps is reported alongside)")
	reps := fs.Int("reps", 10000, "bootstrap resamples")
	seed := fs.Int64("seed", 1, "bootstrap seed — declared, never drawn")
	q := fs.Float64("fdr", 0.10, "false-discovery rate within the family")
	readConfirm := fs.Bool("include-confirmation-years", false, "let -to reach 2022 onward")
	ledger := fs.String("ledger", "research/LEDGER.md", "ledger, for the workspace-wide bar printed as context")
	fatalIf(fs.Parse(args))
	guardConfirmationYears(mustDate(*to), *readConfirm)

	mkt, _, err := traits.MarketReturns(*cache, *floor)
	fatalIf(err)
	scs := lowVolScorers(mkt)
	names := make([]string, len(scs))
	for i, s := range scs {
		names[i] = s.Name
	}
	days := buildScores(*cache, scs, mustDate(*from), mustDate(*to), *floor, 3)
	if len(days) == 0 {
		fatal(fmt.Errorf("no eligible days"))
	}
	books := staggeredBooks(days, names, lowVolHolds, *costBps)

	boot := evidence.Bootstrap{MeanBlock: 5, Reps: *reps, Seed: *seed}
	type cell struct {
		name       string
		k          int
		sum, eqSum sleeve.Summary
		raw        evidence.Edge
		mEq, mSh   evidence.Edge
		m2         float64
		deciding   string
		excess     []float64
		p, qv, dsr float64
		survivor   bool
		months     int
		shSum      sleeve.Summary
	}
	var cells []*cell
	for _, k := range lowVolHolds {
		for _, n := range names {
			b := books[n][k]
			_, ms := sleeve.Monthly(b.Signal)
			_, me := sleeve.Monthly(b.Equal)
			_, mh := sleeve.Monthly(b.Shuffle)
			_, mse := sleeve.Monthly(atRisk(b.Signal, b.Equal))
			_, msh := sleeve.Monthly(atRisk(b.Signal, b.Shuffle))
			if len(ms) != len(me) || len(ms) != len(mh) || len(ms) < 12 {
				fatal(fmt.Errorf("%s held %d: books misaligned or too short", n, k))
			}
			raw, err := evidence.PairedEdge(ms, me, boot, 0.90)
			fatalIf(err)
			mEq, err := evidence.PairedEdge(mse, me, boot, 0.90)
			fatalIf(err)
			mSh, err := evidence.PairedEdge(msh, mh, boot, 0.90)
			fatalIf(err)
			c := &cell{name: n, k: k, sum: sleeve.Summarize(b.Signal), eqSum: sleeve.Summarize(b.Equal),
				shSum: sleeve.Summarize(b.Shuffle), raw: raw, mEq: mEq, mSh: mSh, months: len(ms)}
			scaled, ctl, e := mse, me, mEq
			c.deciding = "equal-wt"
			if mSh.P > mEq.P {
				scaled, ctl, e, c.deciding = msh, mh, mSh, "shuffle"
			}
			c.excess = make([]float64, len(scaled))
			for i := range scaled {
				c.excess[i] = scaled[i] - ctl[i]
			}
			c.p = e.P
			// 100 bps: the doubled-cost book at the doubled-cost control's risk.
			worst := math.Inf(1)
			for _, ref := range []sleeve.Book{b.Equal, b.Shuffle} {
				d := sleeve.PairedMonthly(atRisk(doubleCost(b.Signal), doubleCost(ref)), doubleCost(ref)).MeanDiff
				worst = math.Min(worst, d)
			}
			c.m2 = worst
			cells = append(cells, c)
		}
	}
	members := make([]evidence.Member, len(cells))
	for i, c := range cells {
		members[i] = evidence.Member{Name: c.name, Returns: c.excess, P: c.p}
	}
	verdicts, err := evidence.Judge(members, len(members), *q)
	fatalIf(err)
	for i, v := range verdicts {
		c := cells[i]
		c.qv, c.dsr = v.Q, v.Deflated.DSR
		c.survivor = v.Discovery && c.mEq.MeanDiff > 0 && c.mSh.MeanDiff > 0
	}
	lookup := map[string]*cell{}
	for _, c := range cells {
		lookup[fmt.Sprintf("%s/%d", c.name, c.k)] = c
	}

	fmt.Println("LOW-VOLATILITY ACROSS TIME WINDOWS — pre-registered, research/preregistrations/2026-09-12_low_volatility.md")
	fmt.Printf("NSE adjusted EQ, Rs %.0f cr floor, the LOWEST-risk quintile, equal weight, fills at the next open, %s..%s\n", *floor/1e7, *from, *to)
	fmt.Printf("%.0f bps round trip (100 bps shown separately); a K-month hold = K staggered books a month apart, averaged\n", *costBps)
	fmt.Println("Deciding statistic: the edge at MATCHED risk — each book scaled (hindsight, a reading device) to the")
	fmt.Println("realised volatility of the control it is compared with. Exploration years: can kill, never confirm.")
	fmt.Println()
	grid := func(title string, cell func(*cell) string) {
		fmt.Println(title)
		fmt.Printf("  %-8s", "")
		for _, k := range lowVolHolds {
			fmt.Printf(" %22s", fmt.Sprintf("hold %dm", k))
		}
		fmt.Println()
		for _, n := range names {
			fmt.Printf("  %-8s", n)
			for _, k := range lowVolHolds {
				fmt.Printf(" %22s", cell(lookup[fmt.Sprintf("%s/%d", n, k)]))
			}
			fmt.Println()
		}
		fmt.Println()
	}
	grid("the book: annual return / volatility / max drawdown", func(c *cell) string {
		return fmt.Sprintf("%.1f%% / %.1f%% / %.0f%%", 100*c.sum.AnnReturn, 100*c.sum.AnnVol, 100*c.sum.MaxDD)
	})
	ref := lookup["vol12/1"]
	fmt.Printf("reference: equal-weight %.1f%% / %.1f%% / %.0f%%, stable-shuffle %.1f%% / %.1f%% / %.0f%% (held 1m, %d months)\n\n",
		100*ref.eqSum.AnnReturn, 100*ref.eqSum.AnnVol, 100*ref.eqSum.MaxDD,
		100*ref.shSum.AnnReturn, 100*ref.shSum.AnnVol, 100*ref.shSum.MaxDD, ref.months)
	grid("RAW edge over equal-weight, %/month (the 'similar return' half of the claim)", func(c *cell) string {
		return fmt.Sprintf("%+.2f", 100*c.raw.MeanDiff)
	})
	grid("edge at MATCHED risk over the TOUGHER control, %/month at 50 bps  (✓ = beats both at matched risk and survives FDR)", func(c *cell) string {
		m := " "
		if c.survivor {
			m = "✓"
		}
		e := c.mEq.MeanDiff
		if c.deciding == "shuffle" {
			e = c.mSh.MeanDiff
		}
		return fmt.Sprintf("%+.2f%s", 100*e, m)
	})
	grid("same at 100 bps (worse of the two controls)", func(c *cell) string { return fmt.Sprintf("%+.2f", 100*c.m2) })
	grid("deflated Sharpe on the matched-risk excess, 18 trials", func(c *cell) string { return fmt.Sprintf("%.3f", c.dsr) })

	survivors := 0
	rows, cols := map[string]bool{}, map[int]bool{}
	var list []string
	for _, c := range cells {
		if c.survivor {
			survivors++
			rows[c.name] = true
			cols[c.k] = true
			list = append(list, fmt.Sprintf("%s/%dm", c.name, c.k))
		}
	}
	verdict := "IN BETWEEN"
	switch {
	case survivors >= 12 && len(rows) >= 2 && len(cols) >= 2:
		verdict = "PLATEAU"
	case survivors < 6 || len(rows) <= 1 || len(cols) <= 1:
		verdict = "SPIKE"
	}
	sort.Strings(list)
	fmt.Printf("PRE-REGISTERED VERDICT: %d of 18 cells beat both controls at matched risk and survive FDR, spanning %d of 6 measures and %d of 3 holding periods -> %s\n",
		survivors, len(rows), len(cols), verdict)
	fmt.Println("(plateau: >= 12 survivors not confined to one row or column; spike: < 6, or all in one row or column)")
	fmt.Printf("Surviving cells: %s\n", strings.Join(list, ", "))
	if m, err := research.CountM(*ledger); err == nil {
		fmt.Printf("Workspace-wide Bonferroni bar, context only (amended Law 2): t = %.2f at M = %d.\n", backtest.BonferroniBar(m), m)
	}
}
