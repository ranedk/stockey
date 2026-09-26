package main

import (
	"flag"
	"fmt"
	"math/rand"
	"sort"
	"sync"
	"time"

	"github.com/ranedk/systrader/internal/backtest"
	"github.com/ranedk/systrader/internal/bars"
	"github.com/ranedk/systrader/internal/patterns/coi"
)

// cmdPanic tests the breadth idea: when the pattern fires across a large
// share of the liquid universe on one day, that day is market-wide panic
// rather than a set of independent stock signals. Buy the names that fell
// hardest but were in an uptrend before the fall.
//
// This needs two passes over the cache because the decision is
// cross-sectional — you cannot know a day was a panic day from one symbol.
// Pass one collects every candidate and the liquid-universe count per date;
// pass two simulates only what the ranking selected.
//
// Two controls, because there are two claims:
//
//	timing     same symbol, random nearby entry — is the panic day special?
//	selection  a random OTHER candidate on the SAME panic day — is the
//	           "fell hardest, was going up" ranking doing anything, or would
//	           any name off that day's list have done as well?
func cmdPanic(args []string) {
	fs := flag.NewFlagSet("panic", flag.ExitOnError)
	cache := fs.String("cache", defaultCache, "bar cache file")
	from := fs.String("from", "2013-07-01", "start date")
	to := fs.String("to", "2026-06-30", "end date")
	minTurnover := fs.Float64("min-turnover", 1e7, "minimum 60-bar median traded value in INR")
	costBps := fs.Float64("cost-bps", 50, "round-trip cost in basis points")
	variant := fs.String("variant", "full", "pattern strictness: full|weak|early|falling")
	breadthPct := fs.Float64("breadth", 0.10, "a day is a panic day when this share of the liquid universe fires")
	topK := fs.Int("top-k", 5, "how many names to buy per panic day")
	rank := fs.String("rank", "drop", "ranking: drop|drop_asc (fall from prior high, deepest/shallowest first) | dayret|dayret_asc (that day's return, worst/best first)")
	requireUptrend := fs.Bool("require-uptrend", true, "only buy names that were in an uptrend before the fall (MACD)")
	ledgerM := fs.Int("ledger-m", 40, "multiple-testing count M for the Bonferroni bar")
	seed := fs.Int64("seed", 1, "RNG seed")
	fs.Parse(args)

	fromT, toT := mustDate(*from), mustDate(*to)
	p := coi.DefaultParams()
	v, err := coi.ParseVariant(*variant)
	check(err)

	type cand struct {
		symbol string
		setup  coi.Setup
		date   time.Time
	}

	var mu sync.Mutex
	byDate := map[time.Time][]cand{}
	liquidPerDate := map[time.Time]int{}
	symbols := 0

	// --- pass one: candidates and the liquid-universe denominator ---------
	check(scanParallel(*cache, func(ser bars.Series) {
		if len(ser.Bars) < 260 {
			return
		}
		turn := coi.MedianTurnover(ser.Bars, 60)
		ind := coi.Compute(ser.Bars, p)
		setups := coi.DetectVariant(ser, ind, p, v)

		localLiquid := map[time.Time]int{}
		for i, b := range ser.Bars {
			if b.Date.Before(fromT) || b.Date.After(toT) {
				continue
			}
			if t := turn[i]; t == t && t >= *minTurnover {
				localLiquid[b.Date]++
			}
		}
		var local []cand
		for _, s := range setups {
			if s.Confirm+1 >= len(ser.Bars) {
				continue
			}
			d := ser.Bars[s.Confirm].Date
			if d.Before(fromT) || d.After(toT) {
				continue
			}
			if t := turn[s.Confirm]; !(t == t && t >= *minTurnover) {
				continue
			}
			local = append(local, cand{ser.Symbol, s, d})
		}

		mu.Lock()
		symbols++
		for d, n := range localLiquid {
			liquidPerDate[d] += n
		}
		for _, c := range local {
			byDate[c.date] = append(byDate[c.date], c)
		}
		mu.Unlock()
	}))

	// --- breadth distribution --------------------------------------------
	type dayStat struct {
		date    time.Time
		breadth float64
		n       int
	}
	var days []dayStat
	for d, n := range liquidPerDate {
		if n < 200 {
			continue // not a real session in our data
		}
		days = append(days, dayStat{d, float64(len(byDate[d])) / float64(n), len(byDate[d])})
	}
	sort.Slice(days, func(i, j int) bool { return days[i].breadth > days[j].breadth })

	fmt.Printf("panic-breadth study — variant %q, %s to %s\n", v, *from, *to)
	fmt.Printf("%d symbols, %d sessions with a liquid universe\n\n", symbols, len(days))
	fmt.Println("How often does the pattern fire across the market at once?")
	for _, q := range []float64{0.999, 0.99, 0.95, 0.90, 0.75, 0.50} {
		idx := int(float64(len(days)) * (1 - q))
		if idx >= len(days) {
			idx = len(days) - 1
		}
		fmt.Printf("  top %5.1f%% of days: breadth >= %5.2f%% of the liquid universe\n",
			(1-q)*100, days[idx].breadth*100)
	}
	if len(days) > 0 {
		fmt.Printf("  single widest day: %s at %.2f%% (%d names)\n\n",
			days[0].date.Format("2006-01-02"), days[0].breadth*100, days[0].n)
	}

	panicDays := map[time.Time]bool{}
	for _, d := range days {
		if d.breadth >= *breadthPct {
			panicDays[d.date] = true
		}
	}
	fmt.Printf("panic threshold %.1f%% -> %d panic days (%.1f%% of sessions)\n",
		*breadthPct*100, len(panicDays), float64(len(panicDays))/float64(len(days))*100)
	if len(panicDays) == 0 {
		fmt.Println("\nNo day reaches that breadth. Lower -breadth to something the market actually does.")
		return
	}

	// --- rank and select --------------------------------------------------
	selected := map[string]map[int]bool{} // symbol -> confirm index -> chosen
	sameDay := map[string]map[int]time.Time{}
	nSelected, nPool := 0, 0
	for d := range panicDays {
		pool := byDate[d]
		var eligible []cand
		for _, c := range pool {
			if *requireUptrend && !c.setup.PriorUptrendMACD {
				continue
			}
			if c.setup.DropFromPriorHighPct <= 0 {
				continue
			}
			eligible = append(eligible, c)
		}
		if len(eligible) < *topK+2 {
			continue // too thin to have both a selection and a control
		}
		sort.Slice(eligible, func(i, j int) bool {
			a, b := eligible[i].setup, eligible[j].setup
			switch *rank {
			case "dayret":
				return a.C0DayReturn < b.C0DayReturn
			case "dayret_asc":
				return a.C0DayReturn > b.C0DayReturn
			case "drop_asc":
				return a.DropFromPriorHighPct < b.DropFromPriorHighPct
			}
			return a.DropFromPriorHighPct > b.DropFromPriorHighPct
		})
		for i, c := range eligible {
			if selected[c.symbol] == nil {
				selected[c.symbol] = map[int]bool{}
				sameDay[c.symbol] = map[int]time.Time{}
			}
			sameDay[c.symbol][c.setup.Confirm] = d
			if i < *topK {
				selected[c.symbol][c.setup.Confirm] = true
				nSelected++
			}
			nPool++
		}
	}
	fmt.Printf("ranking %q, uptrend filter %v, top %d per day -> %d buys from a pool of %d candidates\n\n",
		*rank, *requireUptrend, *topK, nSelected, nPool)

	// --- pass two: simulate ----------------------------------------------
	policies := []coi.ExitPolicy{
		{Name: "c0stop / prior high", UseC0Stop: true, SwingTarget: 1, MaxBars: 60},
		{Name: "c0stop / 2nd prior high", UseC0Stop: true, SwingTarget: 2, MaxBars: 60},
		{Name: "8% stop / 10% target", FixedStopPct: 0.08, TargetPct: 0.10, MaxBars: 60},
		{Name: "8% stop / 20d", FixedStopPct: 0.08, MaxBars: 20},
	}
	type keyed struct {
		date   time.Time
		trade  coi.Trade
		top    bool
		timing coi.Trade // same symbol, random nearby entry
	}
	all := make([][]keyed, len(policies))

	check(scanParallel(*cache, func(ser bars.Series) {
		picks, ok := selected[ser.Symbol]
		if !ok {
			return
		}
		_ = picks
		ind := coi.Compute(ser.Bars, p)
		sc := coi.SimContext{ATR: coi.ATR(ser.Bars, 14), BearReversal: coi.BearishReversals(ser.Bars)}
		rng := rand.New(rand.NewSource(*seed + int64(len(ser.Symbol))))
		local := make([][]keyed, len(policies))
		for _, s := range coi.DetectVariant(ser, ind, p, v) {
			d, in := sameDay[ser.Symbol][s.Confirm]
			if !in {
				continue
			}
			top := selected[ser.Symbol][s.Confirm]
			for pi, pol := range policies {
				st, sok := coi.Simulate(ser.Bars, s, s.Confirm+1, coi.SignalStop(ser.Bars, s), pol, sc, *costBps)
				if !sok {
					continue
				}
				ct, cok := coi.MatchedControl(ser.Bars, s, pol, sc, *costBps, 60, rng)
				if !cok {
					continue
				}
				st.Setup = s
				ct.Setup = s
				local[pi] = append(local[pi], keyed{d, st, top, ct})
			}
		}
		mu.Lock()
		for pi := range policies {
			all[pi] = append(all[pi], local[pi]...)
		}
		mu.Unlock()
	}))

	bar := backtest.BonferroniBar(*ledgerM)
	for pi, pol := range policies {
		// Group by the panic day the trade came from, so a top-K pick is
		// only ever compared against candidates from ITS OWN day. Comparing
		// across days would smuggle the market's direction into the answer.
		type dayGroup struct{ top, pool []coi.Trade }
		groups := map[time.Time]*dayGroup{}
		for _, k := range all[pi] {
			g := groups[k.date]
			if g == nil {
				g = &dayGroup{}
				groups[k.date] = g
			}
			if k.top {
				g.top = append(g.top, k.trade)
			} else {
				g.pool = append(g.pool, k.trade)
			}
		}
		rng := rand.New(rand.NewSource(*seed))
		var picks, controls []coi.Trade
		for _, g := range groups {
			if len(g.pool) == 0 {
				continue
			}
			for _, t := range g.top {
				picks = append(picks, t)
				controls = append(controls, g.pool[rng.Intn(len(g.pool))])
			}
		}
		if len(picks) == 0 {
			continue
		}
		var timing []coi.Trade
		for _, k := range all[pi] {
			if k.top {
				timing = append(timing, k.timing)
			}
		}
		var topOnly []coi.Trade
		for _, k := range all[pi] {
			if k.top {
				topOnly = append(topOnly, k.trade)
			}
		}
		fmt.Print(coi.Report(pol.Name+" [top-K vs random nearby day]",
			coi.Aggregate(topOnly), coi.Aggregate(timing)))
		fmt.Print(coi.EdgeLine(coi.PairedEdge(topOnly, timing), bar))
		fmt.Print(coi.Report(pol.Name+" [top-K vs same-day pool]",
			coi.Aggregate(picks), coi.Aggregate(controls)))
		fmt.Print(coi.EdgeLine(coi.PairedEdge(picks, controls), bar))
		fmt.Println()
	}
}
