package main

import (
	"encoding/csv"
	"flag"
	"fmt"
	"math"
	"math/rand"
	"os"
	"sort"
	"strconv"
	"sync"
	"time"

	"github.com/ranedk/systrader/internal/bars"
	"github.com/ranedk/systrader/internal/patterns/coi"
)

// cmdSlice is exploratory: it cuts the pattern's results by time period and by
// what kind of stock the signal fired in, under several exit rules, always
// beside a matched random entry.
//
// This is a MAP, not a verdict. Cutting one dataset 28 ways and reporting the
// best cut is the textbook way to find nothing and believe it is something —
// the point here is to see the SHAPE of the results (does the sign flip
// between regimes? does it concentrate in illiquid names? is one era carrying
// the whole thing?), not to pick a winner. Read the columns for pattern, not
// for the largest number.
//
// On the slices themselves: there is no usable sector or industry mapping in
// either database (nseindia_earnings_events has an `industry` column covering
// two symbols, valued "-"; historical_mcap covers 200-450 names before 2024,
// so bucketing by it would quietly restrict the sample to large caps). The
// substitutes below are computed from the price data itself for all 3,899
// symbols, point-in-time at the signal.
func cmdSlice(args []string) {
	fs := flag.NewFlagSet("slice", flag.ExitOnError)
	cache := fs.String("cache", defaultCache, "bar cache file")
	from := fs.String("from", "2013-07-01", "start date")
	to := fs.String("to", "2026-06-30", "end date")
	minTurnover := fs.Float64("min-turnover", 1e7, "minimum 60-bar median traded value in INR")
	costBps := fs.Float64("cost-bps", 50, "round-trip cost in basis points")
	variant := fs.String("variant", "full", "pattern strictness: full|weak|early|falling")
	out := fs.String("out", "", "write the full detail (n, t-stats, every cut) to this CSV")
	seed := fs.Int64("seed", 1, "RNG seed")
	fs.Parse(args)

	fromT, toT := mustDate(*from), mustDate(*to)
	p := coi.DefaultParams()
	v, err := coi.ParseVariant(*variant)
	check(err)

	policies := []coi.ExitPolicy{
		{Name: "5d", MaxBars: 5},
		{Name: "10d", MaxBars: 10},
		{Name: "20d", MaxBars: 20},
		{Name: "40d", MaxBars: 40},
		{Name: "+10%/-8%", FixedStopPct: 0.08, TargetPct: 0.10, MaxBars: 120},
		{Name: "+20%/-8%", FixedStopPct: 0.08, TargetPct: 0.20, MaxBars: 120},
		{Name: "reversal", OnReversal: true, MaxBars: 120},
		{Name: "reversal-8%", OnReversal: true, FixedStopPct: 0.08, MaxBars: 120},
	}

	type obs struct {
		date     time.Time
		turnover float64
		volPct   float64 // ATR / price at the signal
		aboveSMA bool
		sig      []coi.Trade
		ctl      []coi.Trade
	}

	var mu sync.Mutex
	var all []obs

	check(scanParallel(*cache, func(ser bars.Series) {
		b := ser.Bars
		if len(b) < 260 {
			return
		}
		ind := coi.Compute(b, p)
		sc := coi.SimContext{ATR: coi.ATR(b, 14), BearReversal: coi.BearishReversals(b)}
		turn := coi.MedianTurnover(b, 60)
		sma200 := sma(b, 200)
		rng := rand.New(rand.NewSource(*seed + int64(len(ser.Symbol)) + int64(len(b))))

		var local []obs
		for _, s := range coi.DetectVariant(ser, ind, p, v) {
			j := s.Confirm
			if j+1 >= len(b) {
				continue
			}
			d := b[j].Date
			if d.Before(fromT) || d.After(toT) {
				continue
			}
			t := turn[j]
			if !(t == t && t >= *minTurnover) {
				continue
			}
			o := obs{date: d, turnover: t, aboveSMA: !math.IsNaN(sma200[j]) && b[j].Close > sma200[j]}
			if a := sc.ATR[j]; !math.IsNaN(a) && b[j].Close > 0 {
				o.volPct = a / b[j].Close
			}
			ok := true
			for _, pol := range policies {
				st, sok := coi.Simulate(b, s, j+1, coi.SignalStop(b, s), pol, sc, *costBps)
				ct, cok := coi.MatchedControl(b, s, pol, sc, *costBps, 60, rng)
				if !sok || !cok {
					ok = false
					break
				}
				o.sig = append(o.sig, st)
				o.ctl = append(o.ctl, ct)
			}
			if ok {
				local = append(local, o)
			}
		}
		mu.Lock()
		all = append(all, local...)
		mu.Unlock()
	}))

	if len(all) == 0 {
		fmt.Println("no signals")
		return
	}
	fmt.Printf("COI slice — variant %q, %d signals, %s to %s, %.0f bps round trip\n",
		v, len(all), *from, *to, *costBps)
	fmt.Println("matched control throughout = same stock, random nearby entry, same % stop, same exit")
	fmt.Println("no sector/industry data exists in either DB; liquidity, volatility and trend stand in for it")

	// Quintile edges are computed once over the whole sample so a bucket
	// means the same thing in every cut.
	turnQ := quintiles(all, func(o obs) float64 { return o.turnover })
	volQ := quintiles(all, func(o obs) float64 { return o.volPct })

	type cut struct {
		dim   string
		label string
		keep  func(obs) bool
	}
	var cuts []cut

	for y := 2013; y <= 2026; y++ {
		yy := y
		cuts = append(cuts, cut{"year", strconv.Itoa(yy), func(o obs) bool { return o.date.Year() == yy }})
	}
	for _, blk := range [][2]int{{2013, 2014}, {2015, 2016}, {2017, 2018}, {2019, 2020}, {2021, 2022}, {2023, 2024}, {2025, 2026}} {
		lo, hi := blk[0], blk[1]
		cuts = append(cuts, cut{"2yr", fmt.Sprintf("%d-%d", lo, hi),
			func(o obs) bool { return o.date.Year() >= lo && o.date.Year() <= hi }})
	}
	covidStart := time.Date(2020, 2, 1, 0, 0, 0, 0, time.UTC)
	covidEnd := time.Date(2020, 6, 30, 0, 0, 0, 0, time.UTC)
	cuts = append(cuts,
		cut{"era", "pre-covid", func(o obs) bool { return o.date.Before(covidStart) }},
		cut{"era", "covid crash", func(o obs) bool { return !o.date.Before(covidStart) && !o.date.After(covidEnd) }},
		cut{"era", "post-covid", func(o obs) bool { return o.date.After(covidEnd) }},
	)
	for i := 0; i < 5; i++ {
		k := i
		lo, hi := turnQ[k], turnQ[k+1]
		cuts = append(cuts, cut{"liquidity", fmt.Sprintf("Q%d %s", k+1, money(lo)),
			func(o obs) bool { return o.turnover >= lo && (k == 4 || o.turnover < hi) }})
	}
	for i := 0; i < 5; i++ {
		k := i
		lo, hi := volQ[k], volQ[k+1]
		cuts = append(cuts, cut{"volatility", fmt.Sprintf("Q%d %.1f%%/day", k+1, lo*100),
			func(o obs) bool { return o.volPct >= lo && (k == 4 || o.volPct < hi) }})
	}
	cuts = append(cuts,
		cut{"trend", "above 200DMA", func(o obs) bool { return o.aboveSMA }},
		cut{"trend", "below 200DMA", func(o obs) bool { return !o.aboveSMA }},
	)
	// The trend split is the only cut where the sign flips, so it gets an
	// interaction with time: an effect that only exists in one or two years
	// is a story about those years, not about trend.
	for _, blk := range [][2]int{{2013, 2014}, {2015, 2016}, {2017, 2018}, {2019, 2020}, {2021, 2022}, {2023, 2024}, {2025, 2026}} {
		lo, hi := blk[0], blk[1]
		cuts = append(cuts,
			cut{"below-200DMA by era", fmt.Sprintf("%d-%d", lo, hi), func(o obs) bool {
				return !o.aboveSMA && o.date.Year() >= lo && o.date.Year() <= hi
			}},
		)
	}
	for _, blk := range [][2]int{{2013, 2014}, {2015, 2016}, {2017, 2018}, {2019, 2020}, {2021, 2022}, {2023, 2024}, {2025, 2026}} {
		lo, hi := blk[0], blk[1]
		cuts = append(cuts,
			cut{"above-200DMA by era", fmt.Sprintf("%d-%d", lo, hi), func(o obs) bool {
				return o.aboveSMA && o.date.Year() >= lo && o.date.Year() <= hi
			}},
		)
	}

	var w *csv.Writer
	if *out != "" {
		f, err := os.Create(*out)
		check(err)
		defer f.Close()
		w = csv.NewWriter(f)
		defer w.Flush()
		check(w.Write([]string{"dimension", "bucket", "exit", "n", "signal_mean_pct",
			"control_mean_pct", "edge_pct", "signal_hit_rate", "edge_t_monthly", "bars_held"}))
	}

	header := func(title string) {
		fmt.Printf("\n%s\n  %-16s %7s", title, "bucket", "n")
		for _, pol := range policies {
			fmt.Printf(" %11s", pol.Name)
		}
		fmt.Println()
	}

	lastDim := ""
	for _, mode := range []string{"signal", "edge"} {
		if mode == "signal" {
			fmt.Println("\n\n=========== A. THE PATTERN'S OWN MEAN NET RETURN PER TRADE ===========")
		} else {
			fmt.Println("\n\n=========== B. EDGE: PATTERN MINUS ITS MATCHED RANDOM ENTRY ===========")
			fmt.Println("(this is the column that matters; A is mostly the market)")
		}
		lastDim = ""
		for _, c := range cuts {
			if c.dim != lastDim {
				header("-- by " + c.dim + " --")
				lastDim = c.dim
			}
			var idx []int
			for i, o := range all {
				if c.keep(o) {
					idx = append(idx, i)
				}
			}
			if len(idx) < 50 {
				fmt.Printf("  %-16s %7d  (too few to report)\n", c.label, len(idx))
				continue
			}
			fmt.Printf("  %-16s %7d", c.label, len(idx))
			for pi := range policies {
				var sig, ctl []coi.Trade
				for _, i := range idx {
					sig = append(sig, all[i].sig[pi])
					ctl = append(ctl, all[i].ctl[pi])
				}
				as, ac := coi.Aggregate(sig), coi.Aggregate(ctl)
				e := coi.PairedEdge(sig, ctl)
				if mode == "signal" {
					fmt.Printf(" %10.2f%%", as.MeanRet*100)
				} else {
					fmt.Printf(" %10.2f%%", e.Mean*100)
				}
				if w != nil && mode == "edge" {
					check(w.Write([]string{c.dim, c.label, policies[pi].Name,
						strconv.Itoa(len(sig)),
						fmt.Sprintf("%.4f", as.MeanRet*100),
						fmt.Sprintf("%.4f", ac.MeanRet*100),
						fmt.Sprintf("%.4f", e.Mean*100),
						fmt.Sprintf("%.4f", as.HitRate),
						fmt.Sprintf("%.3f", e.TStatMonthly),
						fmt.Sprintf("%.1f", as.MeanBars)}))
				}
			}
			fmt.Println()
		}
	}
	if *out != "" {
		fmt.Printf("\nfull detail (n, control means, hit rates, t-stats, bars held): %s\n", *out)
	}
}

func sma(b []bars.Bar, w int) []float64 {
	out := make([]float64, len(b))
	sum := 0.0
	for i := range b {
		sum += b[i].Close
		if i >= w {
			sum -= b[i-w].Close
		}
		if i >= w-1 {
			out[i] = sum / float64(w)
		} else {
			out[i] = math.NaN()
		}
	}
	return out
}

func quintiles[T any](xs []T, f func(T) float64) [6]float64 {
	var vals []float64
	for _, x := range xs {
		if v := f(x); v == v && v > 0 {
			vals = append(vals, v)
		}
	}
	sort.Float64s(vals)
	var q [6]float64
	if len(vals) == 0 {
		return q
	}
	for i := 0; i <= 5; i++ {
		idx := i * (len(vals) - 1) / 5
		q[i] = vals[idx]
	}
	q[5] = math.Inf(1)
	return q
}

func money(v float64) string {
	switch {
	case v >= 1e7:
		return fmt.Sprintf("%.0fcr+", v/1e7)
	case v >= 1e5:
		return fmt.Sprintf("%.0fL+", v/1e5)
	}
	return fmt.Sprintf("%.0f+", v)
}
