package main

import (
	"flag"
	"fmt"
	"math"
	"math/rand"
	"sort"
	"sync"
	"time"

	"github.com/ranedk/systrader/internal/backtest"
	"github.com/ranedk/systrader/internal/bars"
	"github.com/ranedk/systrader/internal/patterns/coi"
)

// cmdRS tests relative strength on washout days.
//
// STORY FIRST (Law 1). On a day when a large share of the market breaks to new
// lows at once, the selling is not about any individual company: index and ETF
// redemptions, margin calls and risk de-grossing sell whatever is in the
// basket. Somebody has to take the other side. The names that DON'T break on
// such a day are the ones where real buyers are absorbing that flow, and the
// premium for supplying liquidity into forced selling is a documented, paid-for
// risk — you are paid because you must hold when everyone else must sell.
// That is a story about who pays us and why, which the COI pattern never had.
//
// This deliberately does NOT use the COI pattern. The earlier result ranked
// COI candidates; if relative strength is the real effect then the pattern is
// just an arbitrary filter on the universe, and the test should drop it.
//
// The panic day itself is defined without any pattern: the share of the liquid
// universe closing at or below its own 20-day low.
//
// IN-SAMPLE. This sample was fully consumed by LEDGER rows 11-12, so nothing
// here can confirm an edge. It can do two useful things: falsify (if the
// effect is not even present in the data that produced it, it is dead), and
// characterise its shape — monotonicity across deciles, stability across
// sub-periods, sensitivity to the threshold. Read it that way.
func cmdRS(args []string) {
	fs := flag.NewFlagSet("rs", flag.ExitOnError)
	cache := fs.String("cache", defaultCache, "bar cache file")
	from := fs.String("from", "2013-07-01", "start date")
	to := fs.String("to", "2026-06-30", "end date")
	minTurnover := fs.Float64("min-turnover", 1e7, "minimum 60-bar median traded value in INR")
	costBps := fs.Float64("cost-bps", 50, "round-trip cost in basis points")
	breadthPct := fs.Float64("breadth", 0, "washout day: this absolute share of the liquid universe at a 20-day low (0 = use -days-pct)")
	daysPct := fs.Float64("days-pct", 0.05, "washout days = the top this-fraction of sessions by breadth")
	deciles := fs.Int("deciles", 10, "rank buckets across the liquid universe")
	measure := fs.String("rs", "drawdown", "relative-strength measure: drawdown | ret63 | dayrel")
	ledgerM := fs.Int("ledger-m", 110, "multiple-testing count M for the Bonferroni bar")
	seed := fs.Int64("seed", 1, "RNG seed")
	split := fs.Bool("split", false, "also report the two halves of the sample separately")
	fs.Parse(args)

	fromT, toT := mustDate(*from), mustDate(*to)

	policies := []coi.ExitPolicy{
		{Name: "hold 20d", MaxBars: 20},
		{Name: "hold 40d", MaxBars: 40},
		{Name: "8% stop / 20d", FixedStopPct: 0.08, MaxBars: 20},
	}

	// One record per liquid bar: the ranking inputs, all known at that bar's
	// close, and the outcome of entering at the NEXT open under each policy.
	type rec struct {
		sym      string
		date     time.Time
		atLow    bool
		drawdown float64 // fall from the 120-bar high (smaller = stronger)
		ret63    float64
		dayRel   float64 // that bar's own return (made market-relative below)
		out      [3]float64
		ok       bool
	}

	var mu sync.Mutex
	byDate := map[time.Time][]rec{}
	symbols := 0

	check(scanParallel(*cache, func(ser bars.Series) {
		b := ser.Bars
		if len(b) < 260 {
			return
		}
		turn := coi.MedianTurnover(b, 60)
		sc := coi.SimContext{ATR: coi.ATR(b, 14), BearReversal: coi.BearishReversals(b)}

		var local []rec
		for i := 130; i < len(b)-1; i++ {
			if b[i].Date.Before(fromT) || b[i].Date.After(toT) {
				continue
			}
			if t := turn[i]; !(t == t && t >= *minTurnover) {
				continue
			}
			if !b[i].Valid() || b[i-63].Close <= 0 {
				continue
			}

			low20, high120 := math.Inf(1), 0.0
			for k := i - 19; k <= i; k++ {
				low20 = math.Min(low20, b[k].Low)
			}
			for k := i - 119; k <= i; k++ {
				high120 = math.Max(high120, b[k].High)
			}

			r := rec{
				sym: ser.Symbol, date: b[i].Date,
				atLow:  b[i].Close <= low20*1.0001,
				ret63:  b[i].Close/b[i-63].Close - 1,
				dayRel: b[i].Close/b[i-1].Close - 1,
				ok:     true,
			}
			if high120 > 0 {
				r.drawdown = (high120 - b[i].Close) / high120
			}

			// Outcomes. A synthetic setup is used so the tested Simulate path
			// is shared with every other experiment rather than reimplemented.
			s := coi.Setup{Symbol: ser.Symbol, C0: i, Confirm: i}
			for pi, pol := range policies {
				t, ok := coi.Simulate(b, s, i+1, 0, pol, sc, *costBps)
				if !ok {
					r.ok = false
					break
				}
				r.out[pi] = t.NetRet
			}
			if r.ok {
				local = append(local, r)
			}
		}

		mu.Lock()
		symbols++
		for _, r := range local {
			byDate[r.date] = append(byDate[r.date], r)
		}
		mu.Unlock()
	}))

	// --- washout days, defined without any pattern ------------------------
	type dayStat struct {
		date    time.Time
		breadth float64
		n       int
	}
	var days []dayStat
	for d, recs := range byDate {
		if len(recs) < 200 {
			continue
		}
		n := 0
		for _, r := range recs {
			if r.atLow {
				n++
			}
		}
		days = append(days, dayStat{d, float64(n) / float64(len(recs)), len(recs)})
	}
	sort.Slice(days, func(i, j int) bool { return days[i].breadth > days[j].breadth })

	fmt.Printf("relative strength on washout days — %s to %s\n", *from, *to)
	fmt.Printf("%d symbols, %d sessions, RS measure %q, %s\n\n", symbols, len(days), *measure,
		"IN-SAMPLE (this data produced the hypothesis — falsification only, not confirmation)")
	fmt.Println("share of the liquid universe closing at a 20-day low:")
	for _, q := range []float64{0.001, 0.01, 0.05, 0.10, 0.25, 0.50} {
		idx := int(float64(len(days)) * q)
		if idx >= len(days) {
			idx = len(days) - 1
		}
		fmt.Printf("  top %5.1f%% of days: >= %5.1f%%\n", q*100, days[idx].breadth*100)
	}
	if len(days) > 0 {
		fmt.Printf("  widest: %s at %.1f%%\n\n", days[0].date.Format("2006-01-02"), days[0].breadth*100)
	}

	// Days are sorted breadth-descending. An absolute threshold is brittle
	// across regimes (a 20% reading simply never happens), so the default
	// picks the widest N% of sessions the market actually produced.
	washout := map[time.Time]bool{}
	cut := 0.0
	if *breadthPct > 0 {
		for _, d := range days {
			if d.breadth >= *breadthPct {
				washout[d.date] = true
			}
		}
		cut = *breadthPct
	} else {
		n := int(float64(len(days)) * *daysPct)
		if n < 1 {
			n = 1
		}
		for _, d := range days[:n] {
			washout[d.date] = true
		}
		cut = days[n-1].breadth
	}
	fmt.Printf("washout days: %d (%.1f%% of sessions), breadth >= %.2f%%\n\n",
		len(washout), float64(len(washout))/float64(len(days))*100, cut*100)
	if len(washout) == 0 {
		fmt.Println("no day reaches that threshold; lower -breadth")
		return
	}

	mid := fromT.Add(toT.Sub(fromT) / 2)
	report := func(label string, only func(time.Time) bool) {
		// Per washout day, rank the whole liquid universe and bucket it.
		bucketed := make([][][]float64, *deciles) // decile -> policy -> returns
		for i := range bucketed {
			bucketed[i] = make([][]float64, len(policies))
		}
		monthly := make([]map[string][][]float64, *deciles)
		for i := range monthly {
			monthly[i] = map[string][][]float64{}
		}
		rng := rand.New(rand.NewSource(*seed))
		_ = rng
		daysUsed := 0

		for d := range washout {
			if only != nil && !only(d) {
				continue
			}
			recs := byDate[d]
			if len(recs) < *deciles*5 {
				continue
			}
			sorted := append([]rec(nil), recs...)
			// Strongest first, on whichever measure is selected.
			sort.Slice(sorted, func(i, j int) bool {
				switch *measure {
				case "ret63":
					return sorted[i].ret63 > sorted[j].ret63
				case "dayrel":
					return sorted[i].dayRel > sorted[j].dayRel
				}
				return sorted[i].drawdown < sorted[j].drawdown
			})
			daysUsed++
			size := len(sorted) / *deciles
			mk := d.Format("2006-01")
			for b := 0; b < *deciles; b++ {
				lo, hi := b*size, (b+1)*size
				if b == *deciles-1 {
					hi = len(sorted)
				}
				for pi := range policies {
					var vals []float64
					for _, r := range sorted[lo:hi] {
						vals = append(vals, r.out[pi])
					}
					bucketed[b][pi] = append(bucketed[b][pi], vals...)
					if monthly[b][mk] == nil {
						monthly[b][mk] = make([][]float64, len(policies))
					}
					monthly[b][mk][pi] = append(monthly[b][mk][pi], vals...)
				}
			}
		}

		fmt.Printf("--- %s: %d washout days ---\n", label, daysUsed)
		if daysUsed < 5 {
			fmt.Println("  too few days to say anything")
			return
		}
		bar := backtest.BonferroniBar(*ledgerM)
		for pi, pol := range policies {
			fmt.Printf("\n  exit %q\n", pol.Name)
			fmt.Printf("    %-10s %9s %10s %10s\n", "decile", "n", "mean net", "vs univ")
			var means []float64
			var universe []float64
			for b := 0; b < *deciles; b++ {
				universe = append(universe, bucketed[b][pi]...)
			}
			uMean := mean(universe)
			for b := 0; b < *deciles; b++ {
				m := mean(bucketed[b][pi])
				means = append(means, m)
				tag := ""
				if b == 0 {
					tag = "  <- strongest"
				}
				if b == *deciles-1 {
					tag = "  <- weakest"
				}
				fmt.Printf("    %-10d %9d %9.2f%% %9.2f%%%s\n",
					b+1, len(bucketed[b][pi]), m*100, (m-uMean)*100, tag)
			}
			rho := spearman(reverse(means))
			spread := means[0] - means[len(means)-1]
			tm := monthlyT(monthly[0], monthly[*deciles-1], pi)
			fmt.Printf("    universe mean %.2f%% | top-minus-bottom %+.2f%% | monotonicity rho %+.2f | t(monthly, top vs bottom) %+.2f vs bar %.2f -> %s\n",
				uMean*100, spread*100, rho, tm, bar, verdict(tm, bar))
		}
		fmt.Println()
	}

	report("full sample", nil)
	if *split {
		report("first half", func(d time.Time) bool { return d.Before(mid) })
		report("second half", func(d time.Time) bool { return !d.Before(mid) })
	}
}

func verdict(t, bar float64) string {
	if t >= bar {
		return "clears the bar (in-sample — cannot confirm)"
	}
	return "FAILS the bar"
}

func mean(x []float64) float64 {
	if len(x) == 0 {
		return 0
	}
	s := 0.0
	for _, v := range x {
		s += v
	}
	return s / float64(len(x))
}

func reverse(x []float64) []float64 {
	out := make([]float64, len(x))
	for i, v := range x {
		out[len(x)-1-i] = v
	}
	return out
}

// monthlyT tests the top-minus-bottom decile spread with months as the unit of
// observation. Every name bought on one washout day shares that day's market
// move, so treating individual positions as independent would inflate the
// t-stat by roughly the square root of the number of names bought.
func monthlyT(top, bottom map[string][][]float64, pi int) float64 {
	var diffs []float64
	for mk, tv := range top {
		bv, ok := bottom[mk]
		if !ok || len(tv) <= pi || len(bv) <= pi {
			continue
		}
		if len(tv[pi]) == 0 || len(bv[pi]) == 0 {
			continue
		}
		diffs = append(diffs, mean(tv[pi])-mean(bv[pi]))
	}
	if len(diffs) < 3 {
		return 0
	}
	m := mean(diffs)
	ss := 0.0
	for _, d := range diffs {
		ss += (d - m) * (d - m)
	}
	sd := math.Sqrt(ss / float64(len(diffs)-1))
	if sd == 0 {
		return 0
	}
	return m / (sd / math.Sqrt(float64(len(diffs))))
}
