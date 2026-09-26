package coi

import (
	"fmt"
	"math"
	"sort"
	"strings"
)

// Agg summarizes a set of trades. Two t-statistics are reported and the
// difference between them is the point:
//
//	TStat        treats every trade as an independent observation. It is
//	             almost always too generous here, because COI signals fire in
//	             clusters — a market-wide bounce prints the pattern in 300
//	             names on the same day, and those 300 trades are one bet.
//	TStatMonthly averages trades within an entry month first, then tests
//	             across months. Overlapping, correlated trades collapse into
//	             one observation, which is the honest count.
//
// Neither is the final bar: Law 2 requires comparing against the Bonferroni
// threshold for the ledger's M, and Law 3 requires subtracting the matched
// control before any of this means anything.
type Agg struct {
	N            int
	MeanRet      float64
	MedianRet    float64
	SD           float64
	TStat        float64
	TStatMonthly float64
	Months       int
	HitRate      float64
	MeanWin      float64
	MeanLoss     float64
	PayoffRatio  float64
	MeanBars     float64
	MeanMFE      float64
	MeanMAE      float64
	RetPerBar    float64
	AnnRet       float64 // mean return per bar x 252, i.e. always-invested equivalent
	ByReason     map[string]int
}

// Aggregate summarizes trades on NET returns (costs already deducted).
func Aggregate(ts []Trade) Agg {
	a := Agg{N: len(ts), ByReason: map[string]int{}}
	if len(ts) == 0 {
		return a
	}
	rets := make([]float64, len(ts))
	byMonth := map[string][]float64{}
	var wins, losses []float64
	var bars, mfe, mae float64

	for i, t := range ts {
		rets[i] = t.NetRet
		a.ByReason[t.Reason]++
		if t.NetRet > 0 {
			wins = append(wins, t.NetRet)
		} else {
			losses = append(losses, t.NetRet)
		}
		bars += float64(t.BarsHeld)
		mfe += t.MFEPct
		mae += t.MAEPct
		k := t.EntryDate.Format("2006-01")
		byMonth[k] = append(byMonth[k], t.NetRet)
	}

	a.MeanRet, a.SD = meanSD(rets)
	a.MedianRet = median(rets)
	if a.SD > 0 {
		a.TStat = a.MeanRet / (a.SD / math.Sqrt(float64(len(rets))))
	}
	a.HitRate = float64(len(wins)) / float64(len(ts))
	a.MeanWin, _ = meanSD(wins)
	a.MeanLoss, _ = meanSD(losses)
	if a.MeanLoss != 0 {
		a.PayoffRatio = a.MeanWin / -a.MeanLoss
	}
	a.MeanBars = bars / float64(len(ts))
	a.MeanMFE = mfe / float64(len(ts))
	a.MeanMAE = mae / float64(len(ts))
	if a.MeanBars > 0 {
		a.RetPerBar = a.MeanRet / a.MeanBars
		a.AnnRet = a.RetPerBar * 252
	}

	monthly := make([]float64, 0, len(byMonth))
	for _, v := range byMonth {
		m, _ := meanSD(v)
		monthly = append(monthly, m)
	}
	a.Months = len(monthly)
	if len(monthly) > 1 {
		m, sd := meanSD(monthly)
		if sd > 0 {
			a.TStatMonthly = m / (sd / math.Sqrt(float64(len(monthly))))
		}
	}
	return a
}

// Report renders one policy's signal-vs-control comparison. The edge line is
// the only number Law 3 permits quoting on its own.
func Report(policy string, signal, control Agg) string {
	var b strings.Builder
	f := func(format string, args ...any) { fmt.Fprintf(&b, format, args...) }

	f("  %-22s  %8s  %8s  %8s\n", policy, "signal", "control", "edge")
	f("  %-22s  %8d  %8d  %8s\n", "trades", signal.N, control.N, "-")
	f("  %-22s  %7.2f%%  %7.2f%%  %7.2f%%\n", "mean net return",
		signal.MeanRet*100, control.MeanRet*100, (signal.MeanRet-control.MeanRet)*100)
	f("  %-22s  %7.2f%%  %7.2f%%  %7.2f%%\n", "median net return",
		signal.MedianRet*100, control.MedianRet*100, (signal.MedianRet-control.MedianRet)*100)
	f("  %-22s  %7.1f%%  %7.1f%%  %7.1f%%\n", "hit rate",
		signal.HitRate*100, control.HitRate*100, (signal.HitRate-control.HitRate)*100)
	f("  %-22s  %8.2f  %8.2f  %8s\n", "payoff (win/loss)", signal.PayoffRatio, control.PayoffRatio, "-")
	f("  %-22s  %8.1f  %8.1f  %8s\n", "bars held", signal.MeanBars, control.MeanBars, "-")
	f("  %-22s  %7.2f%%  %7.2f%%  %8s\n", "mean MFE", signal.MeanMFE*100, control.MeanMFE*100, "-")
	f("  %-22s  %7.2f%%  %7.2f%%  %8s\n", "mean MAE", signal.MeanMAE*100, control.MeanMAE*100, "-")
	f("  %-22s  %8.2f  %8.2f  %8s\n", "t-stat (per trade)", signal.TStat, control.TStat, "-")
	f("  %-22s  %8.2f  %8.2f  %8s\n", "t-stat (by month)", signal.TStatMonthly, control.TStatMonthly, "-")
	f("  %-22s  %8s  %8s\n", "exit mix", reasonMix(signal.ByReason), reasonMix(control.ByReason))
	return b.String()
}

func reasonMix(m map[string]int) string {
	keys := make([]string, 0, len(m))
	total := 0
	for k, v := range m {
		keys = append(keys, k)
		total += v
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, fmt.Sprintf("%s %.0f%%", k, float64(m[k])/float64(total)*100))
	}
	return strings.Join(parts, " ")
}

func meanSD(x []float64) (float64, float64) {
	if len(x) == 0 {
		return 0, 0
	}
	m := 0.0
	for _, v := range x {
		m += v
	}
	m /= float64(len(x))
	if len(x) < 2 {
		return m, 0
	}
	ss := 0.0
	for _, v := range x {
		d := v - m
		ss += d * d
	}
	return m, math.Sqrt(ss / float64(len(x)-1))
}

func median(x []float64) float64 {
	if len(x) == 0 {
		return 0
	}
	c := append([]float64(nil), x...)
	sort.Float64s(c)
	return c[len(c)/2]
}

// Edge is the paired signal-minus-control test Law 3 actually calls for.
// Comparing two separate t-stats does not test anything: the question is
// whether the DIFFERENCE between a signal trade and its own matched control
// is reliably positive. Pairs are formed trade by trade, then averaged within
// an entry month before testing, because signals cluster (one market-wide
// bounce prints the pattern in hundreds of names on the same day, and those
// are one bet, not hundreds).
type Edge struct {
	N            int
	Mean         float64
	TStat        float64
	TStatMonthly float64
	Months       int
	WinRate      float64 // share of pairs where the signal beat its control
}

// PairedEdge computes the edge for equal-length, index-aligned slices.
func PairedEdge(signal, control []Trade) Edge {
	n := len(signal)
	if len(control) < n {
		n = len(control)
	}
	e := Edge{N: n}
	if n == 0 {
		return e
	}
	diffs := make([]float64, n)
	byMonth := map[string][]float64{}
	wins := 0
	for i := 0; i < n; i++ {
		d := signal[i].NetRet - control[i].NetRet
		diffs[i] = d
		if d > 0 {
			wins++
		}
		byMonth[signal[i].EntryDate.Format("2006-01")] = append(
			byMonth[signal[i].EntryDate.Format("2006-01")], d)
	}
	e.WinRate = float64(wins) / float64(n)
	m, sd := meanSD(diffs)
	e.Mean = m
	if sd > 0 {
		e.TStat = m / (sd / math.Sqrt(float64(n)))
	}
	monthly := make([]float64, 0, len(byMonth))
	for _, v := range byMonth {
		mm, _ := meanSD(v)
		monthly = append(monthly, mm)
	}
	e.Months = len(monthly)
	if len(monthly) > 1 {
		mm, msd := meanSD(monthly)
		if msd > 0 {
			e.TStatMonthly = mm / (msd / math.Sqrt(float64(len(monthly))))
		}
	}
	return e
}

// EdgeLine renders the paired test, with the Bonferroni bar for the given
// ledger M alongside it so the number is never read on its own (Law 2).
func EdgeLine(e Edge, bonferroniBar float64) string {
	verdict := "FAILS the bar"
	if math.Abs(e.TStatMonthly) >= bonferroniBar && e.TStatMonthly > 0 {
		verdict = "clears the bar"
	}
	return fmt.Sprintf("  %-22s  mean %+.2f%%  pairs won %.1f%%  t(paired,monthly) %+.2f  vs Bonferroni %.2f -> %s\n",
		"EDGE vs control", e.Mean*100, e.WinRate*100, e.TStatMonthly, bonferroniBar, verdict)
}
