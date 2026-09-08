package main

import (
	"flag"
	"fmt"
	"math"
	"sort"
	"sync"
	"time"

	"github.com/ranedk/systrader/internal/bars"
	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/data"
	"github.com/ranedk/systrader/internal/rules"
	"github.com/ranedk/systrader/internal/sleeve"
)

// The cost screen for the hypothesis row 17 produced: cross-sectional trend is
// a flow premium in normal tape and reverses under forced selling.
//
// THIS TEST CAN ONLY KILL, NEVER CONFIRM. The hypothesis was mined out of this
// very sample, so a good number here is exactly what a mined hypothesis
// produces and means nothing. A bad number, on the other hand, is decisive:
// the exploration's edge sits in the illiquid, low-priced tail, and if it does
// not survive the cost of trading there then no amount of confirmation
// elsewhere will save it. Spending an afternoon here is cheaper than spending
// a pre-registration.
//
// Six configurations, declared before running: three portfolio rules at two
// liquidity floors.

const washoutBreadth = 0.20 // the regime line row 17 found, not tuned here

var variants = []struct {
	name string
	desc string
}{
	{"always-trend", "long the top forecast quintile, every day"},
	{"washout-reverse", "top quintile, but the BOTTOM quintile when breadth < 20%"},
	{"washout-neutral", "top quintile, but equal-weight the universe when breadth < 20%"},
}

func runCost(args []string) {
	fs := flag.NewFlagSet("cost", flag.ExitOnError)
	cache := fs.String("cache", defaultCache, "bar cache file")
	ruleName := fs.String("rule", "ewmac32_128", "rule to screen")
	from := fs.String("from", "2013-07-01", "first decision date")
	to := fs.String("to", "2026-06-30", "last decision date")
	costBps := fs.Float64("cost-bps", 50, "round-trip cost in basis points")
	rebalance := fs.Int("rebalance", 20, "trade every N decision days, hold in between")
	fatalIf(fs.Parse(args))

	rule := findRule(*ruleName)
	if rule == nil {
		fatal(fmt.Errorf("unknown rule %q (try -list)", *ruleName))
	}

	fmt.Printf("COST SCREEN — %s, top/bottom quintile, rebalanced every %d days\n", rule.Name(), *rebalance)
	fmt.Printf("%s..%s | %.0f bps round trip (and %.0f bps in the sensitivity)\n", *from, *to, *costBps, 2**costBps)
	fmt.Println()
	fmt.Println("This screen can only KILL the hypothesis, never support it: it was mined from")
	fmt.Println("this same sample (LEDGER row 17), so a good number here is what a mined")
	fmt.Println("hypothesis produces by construction. A bad one is decisive.")
	fmt.Println()

	for _, floor := range []float64{1e7, 1e8} {
		fmt.Printf("========== liquidity floor: Rs %.0f cr/day median turnover ==========\n", floor/1e7)
		days := buildCost(*cache, rule, mustDate(*from), mustDate(*to), floor)
		if len(days) == 0 {
			fmt.Println("  no eligible days")
			continue
		}
		names := make([]string, len(variants))
		for i, v := range variants {
			names[i] = v.name
		}
		for _, mult := range []float64{1, 2} {
			cfgs := sleeve.Config{
				CostBpsRoundTrip: *costBps * mult,
				Seeds:            []int64{1, 2, 3, 4, 5},
				RebalanceEvery:   *rebalance,
			}
			res, err := sleeve.Run(days, names, cfgs)
			fatalIf(err)
			fmt.Printf("\n  --- costs x%.0f ---\n", mult)
			fmt.Printf("  %-18s %9s %8s %7s %9s %10s %11s %12s\n",
				"book", "ann ret", "ann vol", "SR", "maxDD", "turnover/d", "vs equal-wt", "vs shuffle")
			for i := range res {
				r := res[i]
				s := sleeve.Summarize(r.Signal)
				vsEq := sleeve.PairedMonthly(r.Signal, r.EqualWeight)
				vsSh := sleeve.PairedMonthly(r.Signal, r.StableShuffled)
				fmt.Printf("  %-18s %8.2f%% %7.2f%% %7.2f %8.1f%% %9.1f%% %6.2f%% t%5.2f %5.2f%% t%5.2f\n",
					r.Rule, 100*s.AnnReturn, 100*s.AnnVol, s.SR, 100*s.MaxDD, 100*s.MeanTurnover,
					100*vsEq.MeanDiff, vsEq.T, 100*vsSh.MeanDiff, vsSh.T)
				if mult == 1 && i == 0 {
					eq := sleeve.Summarize(r.EqualWeight)
					sh := sleeve.Summarize(r.StableShuffled)
					fmt.Printf("  %-18s %8.2f%% %7.2f%% %7.2f %8.1f%% %9.1f%%   (beta control)\n",
						"equal-weight", 100*eq.AnnReturn, 100*eq.AnnVol, eq.SR, 100*eq.MaxDD, 100*eq.MeanTurnover)
					fmt.Printf("  %-18s %8.2f%% %7.2f%% %7.2f %8.1f%% %9.1f%%   (selection control)\n",
						"stable-shuffle", 100*sh.AnnReturn, 100*sh.AnnVol, sh.SR, 100*sh.MaxDD, 100*sh.MeanTurnover)
				}
			}
		}
		fmt.Println()
	}
	fmt.Println("Read it as: does the signal beat the equal-weight book (beta) AND a")
	fmt.Println("turnover-matched random ranking (selection)? Anything that fails either at")
	fmt.Println("normal costs is dead and stays dead.")
}

// buildCost produces the cross-sections the sleeve harness trades, with one U
// column per variant. Quintiles and the breadth regime are computed on the
// day's own eligible set — point in time, like everything else here.
func buildCost(cache string, rule rules.Rule, from, to time.Time, minTurnover float64) []sleeve.Day {
	type obs struct {
		sym      int32
		forecast float64
		ret      float64
		aboveSMA bool
	}
	byDate := map[time.Time][]obs{}
	var mu sync.Mutex
	ids := map[string]int32{}

	fatalIf(bars.ScanParallel(cache, 0, func(ser bars.Series) {
		n := len(ser.Bars)
		if n < trendWin+3 {
			return
		}
		times := make([]time.Time, n)
		closes := make([]float64, n)
		opens := make([]float64, n)
		for i, b := range ser.Bars {
			times[i], closes[i], opens[i] = b.Date, b.Close, b.Open
		}
		prices := core.New(times, closes)
		inst := &data.Instrument{Meta: data.Meta{Symbol: ser.Symbol, PointValue: 1, Block: 1}, Prices: prices}
		vol := core.PriceUnitVol(prices, volSpan, volMin)
		fc := rules.Forecast(rule, inst, vol).Values
		sma := core.SMA(prices, trendWin).Values
		turnover := bars.MedianTurnover(ser.Bars, turnoverWin)

		var local []struct {
			d time.Time
			o obs
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
			if entry <= 0 || exit <= 0 || math.IsNaN(fc[i]) || math.IsNaN(sma[i]) {
				continue
			}
			local = append(local, struct {
				d time.Time
				o obs
			}{d, obs{forecast: fc[i], ret: exit/entry - 1, aboveSMA: closes[i] > sma[i]}})
		}
		if len(local) == 0 {
			return
		}
		mu.Lock()
		id, ok := ids[ser.Symbol]
		if !ok {
			id = int32(len(ids) + 1)
			ids[ser.Symbol] = id
		}
		for _, e := range local {
			o := e.o
			o.sym = id
			byDate[e.d] = append(byDate[e.d], o)
		}
		mu.Unlock()
	}))

	days := make([]sleeve.Day, 0, len(byDate))
	for d, obs := range byDate {
		if len(obs) < 25 { // fewer than five names a quintile
			continue
		}
		order := make([]int, len(obs))
		for i := range order {
			order[i] = i
		}
		sort.Slice(order, func(a, b int) bool { return obs[order[a]].forecast > obs[order[b]].forecast })
		q := len(order) / 5
		if q < 1 {
			q = 1
		}
		inTop := make([]bool, len(obs))
		inBottom := make([]bool, len(obs))
		for r, i := range order {
			if r < q {
				inTop[i] = true
			}
			if r >= len(order)-q {
				inBottom[i] = true
			}
		}
		var up float64
		for _, o := range obs {
			if o.aboveSMA {
				up++
			}
		}
		washout := up/float64(len(obs)) < washoutBreadth

		day := sleeve.Day{Date: d}
		for i, o := range obs {
			u := make([]float64, len(variants))
			// always-trend
			u[0] = boolTo(inTop[i])
			// washout-reverse
			if washout {
				u[1] = boolTo(inBottom[i])
			} else {
				u[1] = boolTo(inTop[i])
			}
			// washout-neutral
			if washout {
				u[2] = 1
			} else {
				u[2] = boolTo(inTop[i])
			}
			day.Obs = append(day.Obs, sleeve.Obs{Sym: o.sym, U: u, Ret: o.ret})
		}
		days = append(days, day)
	}
	sort.Slice(days, func(i, j int) bool { return days[i].Date.Before(days[j].Date) })
	return days
}

func boolTo(b bool) float64 {
	if b {
		return 1
	}
	return 0
}
