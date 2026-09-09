// Command paper keeps the forward record for the frozen trend-quintile
// strategy: what it would hold, what it would trade, and what it earned
// against an equal-weight book and a random-ranking book of the same size.
//
//	paper run          recompute the whole track and store it
//	paper run -dry     compute and print, write nothing
//
// Safe to run daily, repeatedly, or after a gap: the track is recomputed from
// the strategy's start date every time, so running twice changes nothing and a
// missed day fills itself in.
//
// Spec: docs/strategies/2026-09-08_trend_quintile.md. Nothing here is a
// decision — it is a record, and the whole point is that it accumulates
// evidence nobody chose.
package main

import (
	"context"
	"flag"
	"fmt"
	"math"
	"os"
	"sort"
	"time"

	"github.com/ranedk/systrader/internal/bars"
	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/data"
	"github.com/ranedk/systrader/internal/paper"
	"github.com/ranedk/systrader/internal/rules"
	"github.com/ranedk/systrader/internal/store"
)

// warmupDays is history loaded before the start date so the 32/128 crossover
// and the 60-bar turnover median are both fully formed on day one.
const warmupDays = 500

func main() {
	// "paper run" reads better than "paper", and flag.Parse stops at the first
	// positional argument, so the verb has to come off before the flags are
	// read — otherwise every flag after it is silently ignored.
	if len(os.Args) > 1 && os.Args[1] == "run" {
		os.Args = append(os.Args[:1], os.Args[2:]...)
	}
	dry := flag.Bool("dry", false, "compute and print, write nothing")
	start := flag.String("start", "", "override the spec's start date")
	name := flag.String("name", "", "override the strategy name (use with -start for the in-sample reference track)")
	overlay := flag.String("overlay", "", "exposure overlay: none | vol | regime | both")
	targetVol := flag.Float64("target-vol", 0, "annualised volatility the vol overlay aims at (0 = spec default)")
	daily := flag.Bool("exposure-daily", false, "let the overlay act every day rather than on the rebalance clock")
	constant := flag.Float64("constant-exposure", 0, "for -overlay=constant: the fixed fraction to hold")
	stop := flag.String("stop", "", "per-position stop: none | fixed | trailing | volfixed | voltrailing | random")
	stopLevel := flag.Float64("stop-level", 0, "stop distance: a fraction for fixed/trailing, a multiple of annualised vol for the vol-scaled ones")
	exitRate := flag.Float64("random-exit-rate", 0, "for -stop=random: exits per position per day")
	rebalance := flag.Int("rebalance", 0, "trading days between rebalances (0 = spec default)")
	holdCount := flag.Int("hold-count", 0, "hold a fixed number of names instead of a quantile")
	signal := flag.String("signal", "ewmac32", "selection signal: ewmac32 | ewmac16 | ret5 | reversal20 | reversal60")
	signal2 := flag.String("signal2", "reversal20", "the second signal, used by -mode=switch and -mode=blend")
	mode := flag.String("mode", "single", "selection mode: single | switch | blend")
	switchBreadth := flag.Float64("switch-breadth", 0.40, "for -mode=switch: use the second signal below this share of the universe in an uptrend")
	flag.Parse()

	spec := paper.FrozenSpec()
	if *start != "" {
		t, err := time.Parse("2006-01-02", *start)
		fatalIf(err)
		spec.Start = t
	}
	if *name != "" {
		spec.Name = *name
	}
	if *overlay != "" {
		o, err := paper.ParseOverlay(*overlay)
		fatalIf(err)
		spec.Overlay = o
	}
	if *targetVol > 0 {
		spec.TargetVol = *targetVol
	}
	spec.ExposureDaily = *daily
	if *stop != "" {
		k, err := paper.ParseStop(*stop)
		fatalIf(err)
		spec.Stop = k
	}
	if *stopLevel > 0 {
		spec.StopLevel = *stopLevel
	}
	if *exitRate > 0 {
		spec.RandomExitRate = *exitRate
	}
	if *rebalance > 0 {
		spec.RebalanceEvery = *rebalance
	}
	if *holdCount > 0 {
		spec.HoldCount = *holdCount
	}
	sig, err := parseSignal(*signal)
	fatalIf(err)
	sig2, err := parseSignal(*signal2)
	fatalIf(err)
	m, err := paper.ParseMode(*mode)
	fatalIf(err)
	spec.Mode = m
	spec.SwitchBreadth = *switchBreadth
	if *constant > 0 {
		spec.ConstantExposure = *constant
	}

	ctx := context.Background()
	st, err := store.Open(ctx)
	fatalIf(err)
	defer st.Close()

	from := spec.Start.AddDate(0, 0, -warmupDays)
	to := time.Now().UTC()
	fmt.Printf("%s: loading adjusted bars %s..%s\n", spec.Name,
		from.Format("2006-01-02"), to.Format("2006-01-02"))

	all, err := buildDays(ctx, st, spec, sig, sig2, from, to, true)
	fatalIf(err)
	if len(all) == 0 {
		fatal(fmt.Errorf("no price data at all in %s..%s", from.Format("2006-01-02"), to.Format("2006-01-02")))
	}
	var tracked []paper.Day
	for _, d := range all {
		if !d.Date.Before(spec.Start) {
			tracked = append(tracked, d)
		}
	}

	track, err := paper.Compute(spec, tracked)
	fatalIf(err)

	// The order sheet is computed from the most recent close available, even
	// when the forward record itself has not started yet: on day one the sheet
	// IS the product, and the history is empty by definition.
	current := map[string]float64{}
	daysToDue := 1
	if track != nil && len(track.Dates) > 0 {
		current = track.Books[paper.BookStrategy].Holdings
		daysToDue = track.DaysToNextRebalance()
	}
	sheet := paper.Pending(spec, all[len(all)-1], current, daysToDue)

	report(spec, track, sheet)

	if *dry {
		fmt.Println("\n-dry: nothing written")
		return
	}
	fatalIf(st.EnsurePaperTables(ctx))
	fatalIf(st.SavePaperTrack(ctx, track))
	fatalIf(st.SavePending(ctx, spec.Name, sheet))
	fmt.Printf("\nstored: %d tracked days x %d books, %d pending orders\n",
		len(track.Dates), len(track.Books), len(sheet.Orders))
}

func report(spec paper.Spec, t *paper.Track, sheet paper.PendingSheet) {
	if len(t.Dates) == 0 {
		fmt.Printf("\n%s — the forward record starts %s; no trading days yet.\n",
			spec.Name, spec.Start.Format("2006-01-02"))
		printSheet(sheet)
		return
	}
	last := t.Dates[len(t.Dates)-1]
	fmt.Printf("\n%s — %d trading days, %s..%s\n", t.Spec.Name,
		len(t.Dates), t.Dates[0].Format("2006-01-02"), last.Format("2006-01-02"))
	clock := "rebalance clock"
	if t.Spec.ExposureDaily {
		clock = "daily"
	}
	fmt.Printf("selection: %s", t.Spec.Mode)
	if t.Spec.Mode == paper.ModeSwitch {
		fmt.Printf(" below %.0f%% breadth", 100*t.Spec.SwitchBreadth)
	}
	fmt.Printf("\nbook: rebalance every %d days, ", t.Spec.RebalanceEvery)
	if t.Spec.HoldCount > 0 {
		fmt.Printf("top %d names\n", t.Spec.HoldCount)
	} else {
		fmt.Printf("top 1/%d of the universe\n", t.Spec.Quantile)
	}
	fmt.Printf("overlay: %s, acting %s | stop: %s", t.Spec.Overlay, clock, t.Spec.Stop)
	if t.Spec.Stop != paper.StopNone {
		fmt.Printf(" (%d exits taken)", t.Books[paper.BookStrategy].Stopped)
	}
	fmt.Println()
	fmt.Printf("%-16s %9s %9s %8s %8s %9s %8s %9s\n",
		"book", "NAV", "return", "ann vol", "maxDD", "holdings", "cost/yr", "exposure")
	names := []string{paper.BookStrategy, paper.BookEqual, paper.BookRandom}
	for _, n := range names {
		b := t.Books[n]
		p := b.NAV[len(b.NAV)-1]
		var cost, sum, ss, peak, dd, expo float64
		peak = b.NAV[0].NAV
		for _, x := range b.NAV {
			cost += x.Cost
			sum += x.Return
			ss += x.Return * x.Return
			expo += x.Exposure
			if x.NAV > peak {
				peak = x.NAV
			}
			if d := x.NAV/peak - 1; d < dd {
				dd = d
			}
		}
		n1 := float64(len(b.NAV))
		mean := sum / n1
		vol := math.Sqrt(math.Max(0, ss/n1-mean*mean)) * math.Sqrt(252)
		years := n1 / 252
		ann := math.Pow(p.NAV/100, 1/years) - 1
		fmt.Printf("%-16s %9.2f %8.2f%% %7.2f%% %7.1f%% %9d %7.2f%% %8.0f%%\n",
			n, p.NAV, 100*ann, 100*vol, 100*dd, p.Holdings, 100*cost/n1*252, 100*expo/n1)
	}

	fmt.Printf("\nnext rebalance in %d trading days\n", t.DaysToNextRebalance())
	printSheet(sheet)
}

func printSheet(sheet paper.PendingSheet) {
	verb := "would place if it were due"
	if sheet.Due {
		verb = "TO PLACE at the next open"
	}
	fmt.Printf("\norder sheet %s — %d orders, priced off %s (rebalance in %d trading days)\n",
		verb, len(sheet.Orders), sheet.BasedOn.Format("2006-01-02"), sheet.DaysToDue)
	for i, o := range sheet.Orders {
		if i >= 10 {
			fmt.Printf("  ... and %d more\n", len(sheet.Orders)-10)
			break
		}
		fmt.Printf("  %-5s %-14s %6.2f%% -> %6.2f%%  ref %.2f\n",
			o.Side, o.Symbol, 100*o.FromWeight, 100*o.ToWeight, o.FillPrice)
	}
}

// buildDays turns the adjusted price history into the daily cross-sections the
// tracker walks. Forecasts and eligibility are lagged one bar: the decision is
// made at a close and filled at the NEXT open.
// signalFn turns one symbol's history into the selection score, one value per
// bar, NaN where it is not yet defined.
type signalFn func(ser bars.Series, closes core.Series, vol core.Series) []float64

func parseSignal(name string) (signalFn, error) {
	switch name {
	case "ewmac32":
		return ewmacSignal(32), nil
	case "ewmac16":
		return ewmacSignal(16), nil
	case "reversal20", "reversal60", "reversal120":
		w := map[string]int{"reversal20": 20, "reversal60": 60, "reversal120": 120}[name]
		return func(ser bars.Series, closes core.Series, vol core.Series) []float64 {
			inst := &data.Instrument{
				Meta:   data.Meta{Symbol: ser.Symbol, PointValue: 1, Block: 1, LongOnly: true},
				Prices: closes,
			}
			return rules.Forecast(rules.Reversal{Window: w}, inst, vol).Values
		}, nil
	case "ret5inv":
		// The mirror of ret5, to tell a mechanism from an accident: if buying
		// last week's winners loses, buying last week's losers should win by
		// about as much. A diagnostic, not a candidate — a book that has to
		// buy whatever just fell hardest is the least tradeable thing there is.
		base, _ := parseSignal("ret5")
		return func(ser bars.Series, closes core.Series, vol core.Series) []float64 {
			out := base(ser, closes, vol)
			for i := range out {
				out[i] = -out[i]
			}
			return out
		}, nil
	case "ret5":
		// Literally "last week's momentum": the trailing five-bar return, raw.
		// Not volatility-normalised, because the proposal being tested is the
		// plain one — normalising it is a different rule and would need its
		// own row.
		return func(ser bars.Series, closes core.Series, _ core.Series) []float64 {
			v := closes.Values
			out := make([]float64, len(v))
			for i := range out {
				if i < 5 || v[i-5] <= 0 {
					out[i] = math.NaN()
					continue
				}
				out[i] = v[i]/v[i-5] - 1
			}
			return out
		}, nil
	}
	return nil, fmt.Errorf("unknown signal %q (ewmac32 | ewmac16 | ret5)", name)
}

func ewmacSignal(fast int) signalFn {
	return func(ser bars.Series, closes core.Series, vol core.Series) []float64 {
		inst := &data.Instrument{
			Meta:   data.Meta{Symbol: ser.Symbol, PointValue: 1, Block: 1, LongOnly: true},
			Prices: closes,
		}
		return rules.Forecast(rules.EWMAC{Fast: fast}, inst, vol).Values
	}
}

func buildDays(ctx context.Context, st *store.Store, spec paper.Spec, signal, signal2 signalFn, from, to time.Time, all bool) ([]paper.Day, error) {
	byDate := map[time.Time][]paper.Obs{}
	err := st.StreamAdjustedBars(ctx, from, to, func(ser bars.Series) error {
		n := len(ser.Bars)
		if n < 150 {
			return nil
		}
		times := make([]time.Time, n)
		closes := make([]float64, n)
		for i, b := range ser.Bars {
			times[i], closes[i] = b.Date, b.Close
		}
		prices := core.New(times, closes)
		vol := core.PriceUnitVol(prices, 36, 10)
		fc := signal(ser, prices, vol)
		fc2 := signal2(ser, prices, vol)
		sma := core.SMA(prices, 200).Values
		turnover := bars.MedianTurnover(ser.Bars, spec.TurnoverWindow)

		// Bars before the start date are kept when `all` is set: the order
		// sheet needs the latest close even when the tracked record has not
		// begun. The warm-up window before that is dropped either way.
		earliest := spec.Start
		if all {
			earliest = from.AddDate(0, 0, 200)
		}
		for i := 1; i < n; i++ {
			d := times[i]
			if d.Before(earliest) {
				continue
			}
			b := ser.Bars[i]
			if b.Open <= 0 || b.Close <= 0 {
				continue
			}
			f, t60 := fc[i-1], turnover[i-1] // decided at yesterday's close
			eligible := !math.IsNaN(f) && !math.IsNaN(t60) && t60 >= spec.MinTurnover
			annVol := math.NaN()
			if v := vol.Values[i-1]; !math.IsNaN(v) && closes[i-1] > 0 {
				annVol = v / closes[i-1] * 16 // daily price vol -> annualised fraction
			}
			byDate[d] = append(byDate[d], paper.Obs{
				Symbol: ser.Symbol, Open: b.Open, Close: b.Close, PrevClose: closes[i-1],
				Forecast: f, Turnover: t60, Eligible: eligible, AnnVol: annVol,
				Forecast2: fc2[i-1],
				AboveSMA:  !math.IsNaN(sma[i-1]) && closes[i-1] > sma[i-1],
			})
		}
		return nil
	})
	if err != nil {
		return nil, err
	}

	days := make([]paper.Day, 0, len(byDate))
	for d, obs := range byDate {
		days = append(days, paper.Day{Date: d, Obs: obs})
	}
	sort.Slice(days, func(i, j int) bool { return days[i].Date.Before(days[j].Date) })
	return days, nil
}

func fatalIf(err error) {
	if err != nil {
		fatal(err)
	}
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "paper:", err)
	os.Exit(1)
}
