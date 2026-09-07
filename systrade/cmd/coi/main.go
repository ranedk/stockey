// Command coi ports and evaluates the "COI" three-bar reversal screener from
// rahul_ta_1.py.
//
//	coi cache   read the universe once from postgres into a local bar cache
//	coi scan    list confirmed setups (the original screener's output)
//	coi study   evaluate real exit policies against a matched control
//
// Every subcommand except "cache" reads the cache file and never touches the
// database. That split is the whole point: research iterates, postgres does
// not need to.
package main

import (
	"context"
	"encoding/csv"
	"flag"
	"fmt"
	"math/rand"
	"os"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/ranedk/systrader/internal/backtest"
	"github.com/ranedk/systrader/internal/bars"
	"github.com/ranedk/systrader/internal/patterns/coi"
	"github.com/ranedk/systrader/internal/store"
)

const defaultCache = "data/cache/bars_daily.bin"

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	switch os.Args[1] {
	case "cache":
		cmdCache(os.Args[2:])
	case "scan":
		cmdScan(os.Args[2:])
	case "study":
		cmdStudy(os.Args[2:])
	case "dump":
		cmdDump(os.Args[2:])
	case "slice":
		cmdSlice(os.Args[2:])
	case "rs":
		cmdRS(os.Args[2:])
	case "dial":
		cmdDial(os.Args[2:])
	case "panic":
		cmdPanic(os.Args[2:])
	default:
		usage()
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: coi cache|scan|study|dump|panic|dial|rs [flags]")
	os.Exit(2)
}

// cmdDump writes raw bars back out as CSV. It exists for parity testing: the
// Python reference implementation and the Go port must be fed byte-identical
// inputs, or a disagreement in results cannot be attributed to either.
func cmdDump(args []string) {
	fs := flag.NewFlagSet("dump", flag.ExitOnError)
	cache := fs.String("cache", defaultCache, "bar cache file")
	symbols := fs.String("symbols", "", "comma-separated symbols (required)")
	tail := fs.Int("tail", 500, "last N bars per symbol")
	outDir := fs.String("out", ".", "directory to write <symbol>.csv into")
	fs.Parse(args)

	want := map[string]bool{}
	for _, s := range strings.Split(*symbols, ",") {
		if s = strings.TrimSpace(s); s != "" {
			want[s] = true
		}
	}
	if len(want) == 0 {
		check(fmt.Errorf("dump: -symbols is required"))
	}
	check(os.MkdirAll(*outDir, 0o755))

	written := 0
	check(bars.Scan(*cache, func(ser bars.Series) error {
		if !want[ser.Symbol] {
			return nil
		}
		b := ser.Bars
		if *tail > 0 && len(b) > *tail {
			b = b[len(b)-*tail:]
		}
		f, err := os.Create(*outDir + "/" + ser.Symbol + ".csv")
		if err != nil {
			return err
		}
		defer f.Close()
		w := csv.NewWriter(f)
		defer w.Flush()
		if err := w.Write([]string{"date", "open", "high", "low", "close", "volume"}); err != nil {
			return err
		}
		for _, x := range b {
			if err := w.Write([]string{d(x.Date), n(x.Open), n(x.High), n(x.Low), n(x.Close), n(x.Vol)}); err != nil {
				return err
			}
		}
		written++
		return nil
	}))
	fmt.Printf("dumped %d/%d symbols to %s\n", written, len(want), *outDir)
}

func cmdCache(args []string) {
	fs := flag.NewFlagSet("cache", flag.ExitOnError)
	out := fs.String("out", defaultCache, "cache file to write")
	from := fs.String("from", "2013-01-01", "start date (inclusive)")
	to := fs.String("to", time.Now().Format("2006-01-02"), "end date (inclusive)")
	fs.Parse(args)

	f, t := mustDate(*from), mustDate(*to)
	ctx := context.Background()
	s, err := store.Open(ctx)
	check(err)
	defer s.Close()

	check(os.MkdirAll(dir(*out), 0o755))
	w, err := bars.Create(*out)
	check(err)

	started := time.Now()
	total := 0
	err = s.StreamAdjustedBars(ctx, f, t, func(ser bars.Series) error {
		total += len(ser.Bars)
		return w.Write(ser)
	})
	check(err)
	check(w.Close())

	fmt.Printf("cached %d symbols / %d bars from %s to %s in %s -> %s\n",
		w.Count(), total, *from, *to, time.Since(started).Round(time.Second), *out)
}

func cmdScan(args []string) {
	fs := flag.NewFlagSet("scan", flag.ExitOnError)
	cache := fs.String("cache", defaultCache, "bar cache file")
	tail := fs.Int("tail", 0, "use only the last N bars per symbol (0 = all; the original used 500)")
	minBars := fs.Int("min-bars", 500, "skip symbols with fewer bars (the original required 500)")
	since := fs.String("since", "", "only report setups whose C0 is on or after this date")
	out := fs.String("out", "", "write CSV here (default: stdout summary only)")
	rescan := fs.Bool("fix-pivot-scan", false, "fix the original's skipped-pivot quirk")
	only := fs.String("symbols", "", "restrict to these comma-separated symbols (default: all)")
	fs.Parse(args)

	want := map[string]bool{}
	for _, x := range strings.Split(*only, ",") {
		if x = strings.TrimSpace(x); x != "" {
			want[x] = true
		}
	}

	p := coi.DefaultParams()
	p.RescanTerminator = *rescan
	var sinceT time.Time
	if *since != "" {
		sinceT = mustDate(*since)
	}

	var mu sync.Mutex
	var all []row
	symbols := 0

	check(scanParallel(*cache, func(ser bars.Series) {
		if len(want) > 0 && !want[ser.Symbol] {
			return
		}
		if len(ser.Bars) < *minBars {
			return
		}
		if *tail > 0 && len(ser.Bars) > *tail {
			ser.Bars = ser.Bars[len(ser.Bars)-*tail:]
		}
		ind := coi.Compute(ser.Bars, p)
		setups := coi.Detect(ser, ind, p)
		var local []row
		for _, s := range setups {
			if !sinceT.IsZero() && ser.Bars[s.C0].Date.Before(sinceT) {
				continue
			}
			local = append(local, row{s, s.Excursion(ser.Bars), ser.Bars[s.C0].Date, ser.Bars[s.Confirm].Date})
		}
		mu.Lock()
		all = append(all, local...)
		symbols++
		mu.Unlock()
	}))

	sort.Slice(all, func(i, j int) bool {
		if !all[i].c0Date.Equal(all[j].c0Date) {
			return all[i].c0Date.After(all[j].c0Date)
		}
		return all[i].s.Symbol < all[j].s.Symbol
	})

	fmt.Printf("scanned %d symbols, %d confirmed setups\n", symbols, len(all))
	var imp, vimp, ubr int
	for _, r := range all {
		if r.s.Important {
			imp++
		}
		if r.s.VeryImportant {
			vimp++
		}
		if r.s.UpperBandRising {
			ubr++
		}
	}
	fmt.Printf("  important %d (%.1f%%)  very-important %d (%.1f%%)  upper-band-rising %d (%.1f%%)\n",
		imp, pct(imp, len(all)), vimp, pct(vimp, len(all)), ubr, pct(ubr, len(all)))

	if *out != "" {
		writeCSV(*out, all)
		fmt.Printf("wrote %s\n", *out)
	}
}

type row struct {
	s               coi.Setup
	e               coi.Excursion
	c0Date, cp1Date time.Time
}

func writeCSV(path string, rows []row) {
	f, err := os.Create(path)
	check(err)
	defer f.Close()
	w := csv.NewWriter(f)
	defer w.Flush()
	check(w.Write([]string{
		"symbol", "c0_date", "confirm_date", "c0_low", "entry_close_cp1",
		"important", "very_important", "upper_band_rising",
		"macd_cp1", "signal_cp1", "macd_above_signal",
		"macd_d1", "macd_d2", "signal_d1", "signal_d2",
		"ema10_d1", "ema10_d2", "ema20_d1", "ema20_d2",
		"bb_slope_diff", "bb_upper_d1", "bb_lower_d1", "bb_upper_d2", "bb_lower_d2",
		"band_width_pct", "confirm_above_c0_high_pct", "stop_distance_pct",
		"highest_high", "max_move_pct", "days_to_high", "status", "days_active", "break_date",
	}))
	for _, r := range rows {
		check(w.Write([]string{
			r.s.Symbol, d(r.c0Date), d(r.cp1Date), n(r.e.C0Low), n(r.e.EntryClose),
			yn(r.s.Important), yn(r.s.VeryImportant), yn(r.s.UpperBandRising),
			n(r.s.MACD), n(r.s.Signal), yn(r.s.MACDAboveSignal),
			n(r.s.MACDD1), n(r.s.MACDD2), n(r.s.SignalD1), n(r.s.SignalD2),
			n(r.s.EMAShortD1), n(r.s.EMAShortD2), n(r.s.EMALongD1), n(r.s.EMALongD2),
			n(r.s.BBSlopeDiff), n(r.s.BBUpperD1), n(r.s.BBLowerD1), n(r.s.BBUpperD2), n(r.s.BBLowerD2),
			n(r.s.BandWidthPct), n(r.s.ConfirmCloseVsC0HighPct), n(r.s.C0LowStopDistancePct),
			n(r.e.HighestHigh), n(r.e.MaxMovePct), strconv.Itoa(r.e.DaysToHigh),
			r.e.Status, strconv.Itoa(r.e.DaysActive), d(r.e.BreakDate),
		}))
	}
}

func cmdStudy(args []string) {
	fs := flag.NewFlagSet("study", flag.ExitOnError)
	cache := fs.String("cache", defaultCache, "bar cache file")
	from := fs.String("from", "2013-01-01", "only signals confirmed on or after this date")
	to := fs.String("to", "2026-12-31", "only signals confirmed on or before this date")
	minTurnover := fs.Float64("min-turnover", 1e7, "minimum 60-bar median traded value in INR at the signal")
	costBps := fs.Float64("cost-bps", 50, "round-trip cost in basis points")
	minBars := fs.Int("min-bars", 260, "skip symbols with fewer bars")
	seed := fs.Int64("seed", 1, "RNG seed for matched controls")
	ledgerM := fs.Int("ledger-m", 8, "multiple-testing count M for the Bonferroni bar (Law 2)")
	variant := fs.String("variant", "full", "pattern strictness: full|weak|early|falling")
	policySet := fs.String("policies", "base", "exit policy set: base|fixed|swing")
	bandCtl := fs.Bool("band-control", false, "extra arm: control entries drawn from lower-band touches WITHOUT the pattern")
	tagged := fs.Bool("tags", false, "also break results down by the original's importance tags")
	fs.Parse(args)

	fromT, toT := mustDate(*from), mustDate(*to)
	p := coi.DefaultParams()
	v, err := coi.ParseVariant(*variant)
	check(err)
	policies := policySets[*policySet]
	if policies == nil {
		check(fmt.Errorf("unknown -policies %q (want base|fixed|swing)", *policySet))
	}

	sig := make([][]coi.Trade, len(policies))
	ctl := make([][]coi.Trade, len(policies))
	band := make([][]coi.Trade, len(policies))
	var mu sync.Mutex
	symbols, signals, dropped := 0, 0, 0
	var sigGapSum, sigGapN, allGapSum, allGapN float64

	check(scanParallel(*cache, func(ser bars.Series) {
		if len(ser.Bars) < *minBars {
			return
		}
		ind := coi.Compute(ser.Bars, p)
		setups := coi.DetectVariant(ser, ind, p, v)
		if len(setups) == 0 {
			mu.Lock()
			symbols++
			mu.Unlock()
			return
		}
		sc := coi.SimContext{ATR: coi.ATR(ser.Bars, 14), BearReversal: coi.BearishReversals(ser.Bars)}
		var bandBars []int
		if *bandCtl {
			bandBars = coi.LowerBandTouches(ser.Bars, ind)
		}
		turn := coi.MedianTurnover(ser.Bars, 60)
		rng := rand.New(rand.NewSource(*seed + int64(len(ser.Symbol)) + int64(len(ser.Bars))))

		// The original enters at the CONFIRMATION close — a price that is
		// only knowable at that close and therefore not fillable. Measuring
		// the overnight gap to the next open, against the same stock's
		// average overnight gap, says how much of the reported move is an
		// entry price nobody could get.
		var gapSum, gapN float64
		var baseGapSum, baseGapN float64
		for k := 1; k < len(ser.Bars); k++ {
			if c := ser.Bars[k-1].Close; c > 0 {
				baseGapSum += (ser.Bars[k].Open - c) / c
				baseGapN++
			}
		}

		localSig := make([][]coi.Trade, len(policies))
		localCtl := make([][]coi.Trade, len(policies))
		localBand := make([][]coi.Trade, len(policies))
		nSig, nDrop := 0, 0

		for _, s := range setups {
			confirm := s.Confirm
			if confirm+1 >= len(ser.Bars) {
				continue
			}
			cd := ser.Bars[confirm].Date
			if cd.Before(fromT) || cd.After(toT) {
				continue
			}
			t := turn[confirm]
			if t != t || t < *minTurnover { // NaN-safe
				nDrop++
				continue
			}
			nSig++
			if c := ser.Bars[confirm].Close; c > 0 {
				gapSum += (ser.Bars[confirm+1].Open - c) / c
				gapN++
			}
			for pi, pol := range policies {
				// A pair is all-or-nothing. The paired edge test below reads
				// signal[i] and control[i] as the same setup, so a signal
				// whose control could not be drawn must be dropped too —
				// silently appending to one slice and not the other shifts
				// every later pair by one and quietly compares unrelated
				// trades.
				st, sok := coi.Simulate(ser.Bars, s, confirm+1, coi.SignalStop(ser.Bars, s), pol, sc, *costBps)
				if !sok {
					continue
				}
				ct, cok := coi.MatchedControl(ser.Bars, s, pol, sc, *costBps, 60, rng)
				if !cok {
					continue
				}
				var bt coi.Trade
				if *bandCtl {
					var bok bool
					bt, bok = coi.ControlFrom(ser.Bars, s, bandBars, pol, sc, *costBps, 60, rng)
					if !bok {
						continue
					}
				}
				st.TurnoverINR = t
				localSig[pi] = append(localSig[pi], st)
				localCtl[pi] = append(localCtl[pi], ct)
				if *bandCtl {
					localBand[pi] = append(localBand[pi], bt)
				}
			}
		}

		mu.Lock()
		symbols++
		signals += nSig
		dropped += nDrop
		sigGapSum += gapSum
		sigGapN += gapN
		if gapN > 0 {
			allGapSum += baseGapSum / baseGapN
			allGapN++
		}
		for pi := range policies {
			sig[pi] = append(sig[pi], localSig[pi]...)
			ctl[pi] = append(ctl[pi], localCtl[pi]...)
			band[pi] = append(band[pi], localBand[pi]...)
		}
		mu.Unlock()
	}))

	fmt.Printf("COI study — variant %q, policies %q, %s to %s\n", v, *policySet, *from, *to)
	fmt.Printf("universe %d symbols | %d signals kept | %d dropped below Rs %.0f/day median turnover | round-trip cost %.0f bps\n\n",
		symbols, signals, dropped, *minTurnover, *costBps)

	if sigGapN > 0 && allGapN > 0 {
		fmt.Printf("entry-price reality check: the confirmation close is not fillable. Gap from that\n"+
			"close to the next open averages %+.3f%% on signal days vs %+.3f%% on an average day —\n"+
			"buying at the price the original reports means capturing %+.3f%% that no order can get.\n\n",
			sigGapSum/sigGapN*100, allGapSum/allGapN*100, sigGapSum/sigGapN*100-allGapSum/allGapN*100)
	}

	bar := backtest.BonferroniBar(*ledgerM)
	for pi, pol := range policies {
		fmt.Print(coi.Report(pol.Name, coi.Aggregate(sig[pi]), coi.Aggregate(ctl[pi])))
		fmt.Print(coi.EdgeLine(coi.PairedEdge(sig[pi], ctl[pi]), bar))
		if *bandCtl && len(band[pi]) > 0 {
			fmt.Print(coi.Report(pol.Name+" [vs band]", coi.Aggregate(sig[pi]), coi.Aggregate(band[pi])))
			fmt.Print(coi.EdgeLine(coi.PairedEdge(sig[pi], band[pi]), bar))
		}
		fmt.Println()
	}

	if *tagged {
		fmt.Println("--- breakdown by the original's tags (policy: c0stop + 20d cap) ---")
		base := sig[1]
		for _, split := range []struct {
			name string
			keep func(coi.Trade) bool
		}{
			{"important", func(t coi.Trade) bool { return t.Setup.Important }},
			{"NOT important", func(t coi.Trade) bool { return !t.Setup.Important }},
			{"very important", func(t coi.Trade) bool { return t.Setup.VeryImportant }},
			{"upper band rising", func(t coi.Trade) bool { return t.Setup.UpperBandRising }},
			{"macd > signal", func(t coi.Trade) bool { return t.Setup.MACDAboveSignal }},
			{"macd rising (d1>0)", func(t coi.Trade) bool { return t.Setup.MACDD1 > 0 }},
		} {
			var s, c, bd []coi.Trade
			for i, t := range base {
				if split.keep(t) {
					s = append(s, t)
					if i < len(ctl[1]) {
						c = append(c, ctl[1][i])
					}
					if i < len(band[1]) {
						bd = append(bd, band[1][i])
					}
				}
			}
			fmt.Print(coi.Report(split.name, coi.Aggregate(s), coi.Aggregate(c)))
			fmt.Print(coi.EdgeLine(coi.PairedEdge(s, c), bar))
			if len(bd) > 0 {
				fmt.Print(coi.Report(split.name+" [vs band]", coi.Aggregate(s), coi.Aggregate(bd)))
				fmt.Print(coi.EdgeLine(coi.PairedEdge(s, bd), bar))
			}
			fmt.Println()
		}
	}
}

// scanParallel streams the cache on one goroutine and fans symbols out to
// workers, so only a handful of symbol series are resident at any time.
// policySets groups exit rules so that a run tests one coherent family at a
// time. Each policy in a set is one trial under Law 2, so the sets are kept
// small and deliberate rather than swept.
var policySets = map[string][]coi.ExitPolicy{
	"base": {
		{Name: "c0stop only", UseC0Stop: true},
		{Name: "c0stop + 20d cap", UseC0Stop: true, MaxBars: 20},
		{Name: "c0stop + 40d cap", UseC0Stop: true, MaxBars: 40},
		{Name: "time 10d only", MaxBars: 10},
		{Name: "time 20d only", MaxBars: 20},
		{Name: "c0stop + 2R target", UseC0Stop: true, TargetR: 2, MaxBars: 60},
		{Name: "c0stop + 3R target", UseC0Stop: true, TargetR: 3, MaxBars: 60},
		{Name: "3xATR trailing", TrailATR: 3, ATRPeriod: 14, MaxBars: 120},
	},
	// Fixed stop and fixed exit: no structural levels at all, so the result
	// cannot be explained by where C0's low happened to sit.
	"fixed": {
		{Name: "5% stop / 20d", FixedStopPct: 0.05, MaxBars: 20},
		{Name: "8% stop / 20d", FixedStopPct: 0.08, MaxBars: 20},
		{Name: "5% stop / 5% target", FixedStopPct: 0.05, TargetPct: 0.05, MaxBars: 60},
		{Name: "5% stop / 10% target", FixedStopPct: 0.05, TargetPct: 0.10, MaxBars: 60},
		{Name: "8% stop / 10% target", FixedStopPct: 0.08, TargetPct: 0.10, MaxBars: 60},
		{Name: "8% stop / 15% target", FixedStopPct: 0.08, TargetPct: 0.15, MaxBars: 60},
	},
	// "Get back to where it broke down from": exit at the swing high that
	// preceded the fall, or the one before that.
	"swing": {
		{Name: "c0stop / prior high", UseC0Stop: true, SwingTarget: 1, MaxBars: 60},
		{Name: "c0stop / 2nd prior high", UseC0Stop: true, SwingTarget: 2, MaxBars: 60},
		{Name: "8% stop / prior high", FixedStopPct: 0.08, SwingTarget: 1, MaxBars: 60},
		{Name: "8% stop / 2nd prior high", FixedStopPct: 0.08, SwingTarget: 2, MaxBars: 60},
	},
}

func scanParallel(cache string, fn func(bars.Series)) error {
	return bars.ScanParallel(cache, runtime.NumCPU(), fn)
}

func mustDate(s string) time.Time {
	t, err := time.Parse("2006-01-02", s)
	check(err)
	return t
}

func check(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "coi:", err)
		os.Exit(1)
	}
}

func dir(p string) string {
	for i := len(p) - 1; i >= 0; i-- {
		if p[i] == '/' {
			return p[:i]
		}
	}
	return "."
}

func pct(a, b int) float64 {
	if b == 0 {
		return 0
	}
	return float64(a) / float64(b) * 100
}

func n(f float64) string { return strconv.FormatFloat(f, 'f', 4, 64) }
func yn(b bool) string {
	if b {
		return "YES"
	}
	return "NO"
}
func d(t time.Time) string {
	if t.IsZero() {
		return ""
	}
	return t.Format("2006-01-02")
}
