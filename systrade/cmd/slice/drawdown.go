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
)

// How far a position has fallen BELOW WHAT ITS OWN VOLATILITY EXPLAINS, and
// what happens to it next.
//
// The question this answers is the one the stop-loss screen could not: those
// runs picked three distances up front and scored the P&L, which conflates the
// exit decision with its trading cost, its cash drag and its re-entry timing.
// Here nothing is traded. For every liquid name on every day we measure how
// deep it sits below its own 120-day high in units of its own volatility, and
// then simply look at what happened over the following weeks.
//
// The shape is the finding. If forward returns decline smoothly with depth
// there is no level to exit at, and no stop can work — which would explain the
// stop screen rather than merely agreeing with it. If they flip sign at some
// depth and stay negative, that depth IS the level, derived from the data's
// structure instead of guessed.
//
// Every bucket carries a same-day, same-depth control: every OTHER liquid name
// that had fallen just as far in its own volatility units. Without it "names
// down three sigma bounce" cannot be told from "the market bounced that week".

const (
	highWindow  = 120 // the window whose high a drawdown is measured from
	recoverWait = 60  // bars allowed to regain that high
)

// depthBands are fixed sigma bands rather than quantiles: the question is
// about absolute depth, and quantile buckets would move with the sample.
var depthBands = []struct {
	lo, hi float64
	label  string
}{
	{0, 0.5, "0.0 - 0.5 sigma"},
	{0.5, 1, "0.5 - 1.0 sigma"},
	{1, 1.5, "1.0 - 1.5 sigma"},
	{1.5, 2, "1.5 - 2.0 sigma"},
	{2, 3, "2.0 - 3.0 sigma"},
	{3, 4, "3.0 - 4.0 sigma"},
	{4, 6, "4.0 - 6.0 sigma"},
	{6, math.Inf(1), "6.0+ sigma"},
}

func bandOf(z float64) int {
	for i, b := range depthBands {
		if z >= b.lo && z < b.hi {
			return i
		}
	}
	return -1
}

type ddObs struct {
	sym      int32
	forecast float64
	z        float64
	fwd20    float64
	fwd60    float64
	recover  float64 // 1 if it regained the high within recoverWait bars
	hasFwd60 bool
	hasRec   bool
}

type bandStat struct {
	momN, ctrlN                int
	momFwd20, momFwd60, momRec float64
	ctrlFwd20, ctrlFwd60       float64
	momRecN, ctrlRecN          int
	ctrlRec                    float64
	edgeSum                    float64
	edgeDays                   int
	fallPct                    float64
}

func runDrawdown(args []string) {
	fs := flag.NewFlagSet("drawdown", flag.ExitOnError)
	cache := fs.String("cache", defaultCache, "bar cache file")
	ruleName := fs.String("rule", "ewmac32_128", "the momentum rule whose holdings are being followed")
	from := fs.String("from", "2013-07-01", "first decision date")
	to := fs.String("to", "2026-06-30", "last decision date")
	minTurnover := fs.Float64("min-turnover", 1e8, "minimum 60-bar median traded value in INR")
	quantile := fs.Int("quantile", 5, "the book holds the top 1/N by forecast")
	fatalIf(fs.Parse(args))

	rule := findRule(*ruleName)
	if rule == nil {
		fatal(fmt.Errorf("unknown rule %q (try -list)", *ruleName))
	}

	days := buildDrawdown(*cache, rule, mustDate(*from), mustDate(*to), *minTurnover)
	sort.Slice(days, func(i, j int) bool { return days[i].date.Before(days[j].date) })

	stats := make([]bandStat, len(depthBands))
	var totalObs, momObs int
	for _, d := range days {
		if len(d.obs) < *quantile*5 {
			continue
		}
		order := make([]int, len(d.obs))
		for i := range order {
			order[i] = i
		}
		sort.Slice(order, func(a, b int) bool { return d.obs[order[a]].forecast > d.obs[order[b]].forecast })
		q := len(order) / *quantile
		inBook := make([]bool, len(d.obs))
		for r, i := range order {
			if r < q {
				inBook[i] = true
			}
		}

		// Per band, per day: the book's names against everyone at that depth.
		type acc struct {
			momSum, allSum float64
			momN, allN     int
		}
		daily := make([]acc, len(depthBands))
		for i, o := range d.obs {
			b := bandOf(o.z)
			if b < 0 {
				continue
			}
			totalObs++
			s := &stats[b]
			s.ctrlN++
			s.ctrlFwd20 += o.fwd20
			s.fallPct += o.z
			if o.hasFwd60 {
				s.ctrlFwd60 += o.fwd60
			}
			if o.hasRec {
				s.ctrlRec += o.recover
				s.ctrlRecN++
			}
			daily[b].allSum += o.fwd20
			daily[b].allN++
			if inBook[i] {
				momObs++
				s.momN++
				s.momFwd20 += o.fwd20
				if o.hasFwd60 {
					s.momFwd60 += o.fwd60
				}
				if o.hasRec {
					s.momRec += o.recover
					s.momRecN++
				}
				daily[b].momSum += o.fwd20
				daily[b].momN++
			}
		}
		for b := range daily {
			if daily[b].momN == 0 || daily[b].allN < 5 {
				continue
			}
			stats[b].edgeSum += daily[b].momSum/float64(daily[b].momN) - daily[b].allSum/float64(daily[b].allN)
			stats[b].edgeDays++
		}
	}

	fmt.Printf("EXPLORATION — how deep a fall has to be before it stops bouncing\n")
	fmt.Printf("%s, %d days, %d symbol-days (%d of them held by the book)\n",
		rule.Name(), len(days), totalObs, momObs)
	fmt.Printf("Depth = fall from the %d-day high, divided by what the name's own volatility says\n", highWindow)
	fmt.Printf("a move of that many days should be. Nothing is traded and no costs are charged:\n")
	fmt.Printf("this measures whether a deep fall PREDICTS anything, not whether acting on it pays.\n\n")

	fmt.Printf("%-16s %9s %9s %10s %10s %9s %9s %9s\n",
		"depth", "book obs", "all obs", "book fwd20", "all fwd20", "vs same", "book fwd60", "regains high")
	for i, b := range depthBands {
		s := stats[i]
		if s.ctrlN == 0 {
			continue
		}
		momFwd20, momFwd60, rec := math.NaN(), math.NaN(), math.NaN()
		if s.momN > 0 {
			momFwd20 = s.momFwd20 / float64(s.momN)
			momFwd60 = s.momFwd60 / float64(s.momN)
		}
		if s.momRecN > 0 {
			rec = s.momRec / float64(s.momRecN)
		}
		edge := math.NaN()
		if s.edgeDays > 0 {
			edge = s.edgeSum / float64(s.edgeDays)
		}
		fmt.Printf("%-16s %9d %9d %9.2f%% %9.2f%% %8.2f%% %9.2f%% %8.0f%%\n",
			b.label, s.momN, s.ctrlN, 100*momFwd20, 100*s.ctrlFwd20/float64(s.ctrlN),
			100*edge, 100*momFwd60, 100*rec)
	}
	fmt.Printf("\n'vs same' is the book's names minus EVERY liquid name at the same depth on the same\n")
	fmt.Printf("day, so the market's own move that week cancels. 'regains high' is the share that\n")
	fmt.Printf("got back to the high they fell from within %d trading days.\n", recoverWait)
	fmt.Printf("\nRead the SHAPE, not any one row. A smooth decline means there is no level to exit\n")
	fmt.Printf("at; a sign flip that stays flipped is a level.\n")
	fmt.Printf("\nTWO CAVEATS, both biting hardest in exactly the deep buckets that matter:\n")
	fmt.Printf("  * SURVIVORSHIP. A name needs %d more bars of history to contribute a forward\n", recoverWait)
	fmt.Printf("    return, so one that falls hard and then stops trading is silently absent. The\n")
	fmt.Printf("    worst outcomes are missing from the deep rows, which flatters them.\n")
	fmt.Printf("  * CLUSTERING. Deep falls happen in crises, so the thousands of observations in\n")
	fmt.Printf("    the tail rows come from a handful of weeks and are nothing like independent.\n")
}

type ddDay struct {
	date time.Time
	obs  []ddObs
}

func buildDrawdown(cache string, rule rules.Rule, from, to time.Time, minTurnover float64) []ddDay {
	byDate := map[time.Time][]ddObs{}
	var mu sync.Mutex
	ids := map[string]int32{}

	fatalIf(bars.ScanParallel(cache, 0, func(ser bars.Series) {
		n := len(ser.Bars)
		if n < highWindow+recoverWait+2 {
			return
		}
		times := make([]time.Time, n)
		closes := make([]float64, n)
		opens := make([]float64, n)
		highs := make([]float64, n)
		for i, b := range ser.Bars {
			times[i], closes[i], opens[i], highs[i] = b.Date, b.Close, b.Open, b.High
		}
		prices := core.New(times, closes)
		inst := &data.Instrument{Meta: data.Meta{Symbol: ser.Symbol, PointValue: 1, Block: 1}, Prices: prices}
		vol := core.PriceUnitVol(prices, volSpan, volMin)
		fc := rules.Forecast(rule, inst, vol).Values
		turnover := bars.MedianTurnover(ser.Bars, turnoverWin)

		var local []struct {
			d time.Time
			o ddObs
		}
		for i := highWindow; i < n-2; i++ {
			d := times[i]
			if d.Before(from) || d.After(to) {
				continue
			}
			if math.IsNaN(turnover[i]) || turnover[i] < minTurnover || math.IsNaN(fc[i]) {
				continue
			}
			// The high this fall is measured from, and how long ago it was set.
			high, at := 0.0, i
			for j := i - highWindow + 1; j <= i; j++ {
				if highs[j] > high {
					high, at = highs[j], j
				}
			}
			daysSince := i - at
			if high <= 0 || closes[i] <= 0 {
				continue
			}
			dailyVol := vol.Values[i] / closes[i]
			if math.IsNaN(dailyVol) || dailyVol <= 0 {
				continue
			}
			fall := (high - closes[i]) / high
			scale := dailyVol * math.Sqrt(math.Max(1, float64(daysSince)))
			z := fall / scale

			entry := opens[i+1]
			if entry <= 0 {
				continue
			}
			o := ddObs{forecast: fc[i], z: z}
			exit20 := i + 1 + 20
			if exit20 >= n || opens[exit20] <= 0 {
				continue
			}
			o.fwd20 = opens[exit20]/entry - 1
			if exit60 := i + 1 + 60; exit60 < n && opens[exit60] > 0 {
				o.fwd60 = opens[exit60]/entry - 1
				o.hasFwd60 = true
			}
			// Did it get back to the high it fell from?
			if i+recoverWait < n {
				o.hasRec = true
				for j := i + 1; j <= i+recoverWait; j++ {
					if highs[j] >= high {
						o.recover = 1
						break
					}
				}
			}
			local = append(local, struct {
				d time.Time
				o ddObs
			}{d, o})
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

	out := make([]ddDay, 0, len(byDate))
	for d, obs := range byDate {
		out = append(out, ddDay{date: d, obs: obs})
	}
	return out
}
