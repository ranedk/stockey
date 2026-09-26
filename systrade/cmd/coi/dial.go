package main

import (
	"flag"
	"fmt"
	"math"
	"math/rand"
	"sort"
	"sync"

	"github.com/ranedk/systrader/internal/backtest"
	"github.com/ranedk/systrader/internal/bars"
	"github.com/ranedk/systrader/internal/patterns/coi"
)

// cmdDial tests whether the signal can become a DIAL instead of a switch.
//
// TRADING_BIBLE.md Law 8 wants a continuous, volatility-standardized forecast,
// not a yes/no event. The candidate dial here is the long-EMA slope at the
// confirming bar, divided by the stock's own daily price volatility so it
// means the same thing in a Rs 50 stock and a Rs 5,000 one.
//
// The test is MONOTONICITY, not significance. A dial is only a dial if turning
// it up produces more return: the buckets have to line up. A single bucket
// that beats its control while its neighbours do not is one of ten lottery
// tickets paying off, and building a forecast on it would be fitting noise.
func cmdDial(args []string) {
	fs := flag.NewFlagSet("dial", flag.ExitOnError)
	cache := fs.String("cache", defaultCache, "bar cache file")
	from := fs.String("from", "2013-07-01", "start date")
	to := fs.String("to", "2026-06-30", "end date")
	minTurnover := fs.Float64("min-turnover", 1e7, "minimum 60-bar median traded value in INR")
	costBps := fs.Float64("cost-bps", 50, "round-trip cost in basis points")
	variant := fs.String("variant", "full", "pattern strictness: full|weak|early|falling")
	buckets := fs.Int("buckets", 10, "number of equal-count slope buckets")
	ledgerM := fs.Int("ledger-m", 60, "multiple-testing count M for the Bonferroni bar")
	seed := fs.Int64("seed", 1, "RNG seed")
	fs.Parse(args)

	fromT, toT := mustDate(*from), mustDate(*to)
	p := coi.DefaultParams()
	v, err := coi.ParseVariant(*variant)
	check(err)
	pol := coi.ExitPolicy{Name: "8% stop / 20d", FixedStopPct: 0.08, MaxBars: 20}

	type obs struct {
		slope   float64
		signal  coi.Trade
		control coi.Trade
	}
	var mu sync.Mutex
	var all []obs

	check(scanParallel(*cache, func(ser bars.Series) {
		if len(ser.Bars) < 260 {
			return
		}
		ind := coi.Compute(ser.Bars, p)
		sc := coi.SimContext{ATR: coi.ATR(ser.Bars, 14), BearReversal: coi.BearishReversals(ser.Bars)}
		turn := coi.MedianTurnover(ser.Bars, 60)
		rng := rand.New(rand.NewSource(*seed + int64(len(ser.Symbol)) + int64(len(ser.Bars))))
		var local []obs
		for _, s := range coi.DetectVariant(ser, ind, p, v) {
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
			if math.IsNaN(s.SlopeZ) || s.SlopeZ == 0 {
				continue
			}
			st, sok := coi.Simulate(ser.Bars, s, s.Confirm+1, coi.SignalStop(ser.Bars, s), pol, sc, *costBps)
			if !sok {
				continue
			}
			ct, cok := coi.MatchedControl(ser.Bars, s, pol, sc, *costBps, 60, rng)
			if !cok {
				continue
			}
			local = append(local, obs{s.SlopeZ, st, ct})
		}
		mu.Lock()
		all = append(all, local...)
		mu.Unlock()
	}))

	if len(all) < *buckets*10 {
		fmt.Printf("dial: only %d observations, not enough to bucket\n", len(all))
		return
	}
	sort.Slice(all, func(i, j int) bool { return all[i].slope < all[j].slope })

	fmt.Printf("dial test — variant %q, exit %q, %s to %s\n", v, pol.Name, *from, *to)
	fmt.Printf("%d signals, bucketed by trend slope at confirmation (in daily-vol units)\n\n", len(all))
	fmt.Printf("  %-8s %8s %10s %9s %9s %9s %8s\n",
		"bucket", "n", "slope mid", "signal", "control", "edge", "t(mo)")

	bar := backtest.BonferroniBar(*ledgerM)
	size := len(all) / *buckets
	var edges, sigMeans []float64
	for b := 0; b < *buckets; b++ {
		lo := b * size
		hi := lo + size
		if b == *buckets-1 {
			hi = len(all)
		}
		var sig, ctl []coi.Trade
		for _, o := range all[lo:hi] {
			sig = append(sig, o.signal)
			ctl = append(ctl, o.control)
		}
		as, ac := coi.Aggregate(sig), coi.Aggregate(ctl)
		e := coi.PairedEdge(sig, ctl)
		edges = append(edges, e.Mean)
		sigMeans = append(sigMeans, as.MeanRet)
		fmt.Printf("  %-8d %8d %10.2f %8.2f%% %8.2f%% %8.2f%% %8.2f\n",
			b+1, len(sig), all[(lo+hi)/2].slope, as.MeanRet*100, ac.MeanRet*100, e.Mean*100, e.TStatMonthly)
	}

	// Spearman rank correlation between bucket order and edge: the actual
	// question. +1 means a perfect dial, 0 means the slope tells you nothing.
	rho := spearman(edges)
	rhoSig := spearman(sigMeans)
	spread := sigMeans[len(sigMeans)-1] - sigMeans[0]
	fmt.Printf("\n  monotonicity of EDGE vs control (Spearman rho):  %+.2f\n", rho)
	fmt.Printf("  monotonicity of the SIGNAL's OWN return:        %+.2f  (top minus bottom bucket: %+.2f%%)\n",
		rhoSig, spread*100)
	fmt.Println("\n  Which of those two matters depends on the use. Sizing (Law 11) scales a")
	fmt.Println("  position by EXPECTED RETURN over vol, so a dial for sizing needs the")
	fmt.Println("  SIGNAL's own return to rise with the dial. An edge that only appears")
	fmt.Println("  relative to a control tells you when the signal is worth preferring to")
	fmt.Println("  a coin flip — useful for a filter, useless for a position size.")
	switch {
	case rho >= 0.7:
		fmt.Println("  -> the buckets line up: this is a usable dial, worth building a forecast on")
	case rho <= -0.7:
		fmt.Println("  -> the buckets line up INVERSELY: the dial works with the sign flipped")
	default:
		fmt.Println("  -> the buckets do not line up: not a dial. Any single bucket that looks")
		fmt.Println("     good here is one of N lottery tickets, not a signal strength.")
	}
	fmt.Printf("  (Bonferroni bar for M=%d is %.2f; no single bucket should be read on its own)\n",
		*ledgerM, bar)
}

// spearman returns the rank correlation between position (1..n) and value.
func spearman(v []float64) float64 {
	n := len(v)
	if n < 3 {
		return 0
	}
	type kv struct {
		i int
		v float64
	}
	s := make([]kv, n)
	for i, x := range v {
		s[i] = kv{i, x}
	}
	sort.Slice(s, func(a, b int) bool { return s[a].v < s[b].v })
	rank := make([]float64, n)
	for r, x := range s {
		rank[x.i] = float64(r + 1)
	}
	mean := float64(n+1) / 2
	var num, d1, d2 float64
	for i := 0; i < n; i++ {
		a := float64(i+1) - mean
		b := rank[i] - mean
		num += a * b
		d1 += a * a
		d2 += b * b
	}
	if d1 == 0 || d2 == 0 {
		return 0
	}
	return num / math.Sqrt(d1*d2)
}
