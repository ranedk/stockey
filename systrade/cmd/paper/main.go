// Command paper keeps the forward record for each frozen strategy in
// paper.Specs (trend-quintile, trend-speed-blend): what it would hold, what it
// would trade, and what it earned against an equal-weight book and a
// random-ranking book of the same size.
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
	"github.com/ranedk/systrader/internal/capacity"
	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/data"
	"github.com/ranedk/systrader/internal/paper"
	"github.com/ranedk/systrader/internal/rules"
	"github.com/ranedk/systrader/internal/stage"
	"github.com/ranedk/systrader/internal/store"
	"github.com/ranedk/systrader/internal/traits"
)

// warmupDays is history loaded before the start date so the 32/128 crossover
// and the 60-bar turnover median are both fully formed on day one.
const warmupDays = 500

// minBars is the history a series needs before it is scored at all.
const minBars = 150

func main() {
	// "paper run" reads better than "paper", and flag.Parse stops at the first
	// positional argument, so the verb has to come off before the flags are
	// read — otherwise every flag after it is silently ignored.
	if len(os.Args) > 1 && os.Args[1] == "buffer" {
		runBuffer(os.Args[2:])
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "capacity" {
		runCapacity(os.Args[2:])
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "run" {
		os.Args = append(os.Args[:1], os.Args[2:]...)
	}
	strategy := flag.String("strategy", "trend-quintile", "registered strategy to run: trend-quintile | trend-speed-blend")
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
	keepMultiple := flag.Int("keep-multiple", 0, "rank buffer: keep a held name until it falls outside the top N times this (0 = spec default)")
	noFunds := flag.Bool("exclude-funds", false, "leave ETFs, fund units, REITs and InvITs out of the universe")
	keepWeights := flag.Bool("keep-weights", false, "at a rebalance trade only the names that change; held names keep their drifted weight")
	signal := flag.String("signal", "", "override the spec's selection signal: ewmac32 | ewmac16 | trend-speed-blend | ret5 | reversal20 | reversal60")
	signal2 := flag.String("signal2", "reversal20", "the second signal, used by -mode=switch and -mode=blend")
	mode := flag.String("mode", "", "override the spec's selection mode: single | switch | blend | bookblend")
	switchBreadth := flag.Float64("switch-breadth", 0.40, "for -mode=switch: use the second signal below this share of the universe in an uptrend")
	asOf := flag.String("as-of", "", "replay with bars through this date (YYYY-MM-DD), always -dry; lists that night's sheet and that day's account fills, one per line, to reconcile them (stability gate S3)")
	flag.Parse()

	spec, ok := paper.SpecFor(*strategy)
	if !ok {
		fatal(fmt.Errorf("no registered strategy %q", *strategy))
	}
	if *signal == "" {
		*signal = spec.Signal
	}
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
	if *keepMultiple > 0 {
		spec.KeepMultiple = *keepMultiple
	}
	if *keepWeights {
		spec.KeepWeights = true
	}
	if *noFunds {
		spec.ExcludeFunds = true
	}
	sig, err := parseSignal(*signal)
	fatalIf(err)
	sig2, err := parseSignal(*signal2)
	fatalIf(err)
	ctx := context.Background()
	st, err := store.Open(ctx)
	fatalIf(err)
	defer st.Close()

	from := spec.Start.AddDate(0, 0, -warmupDays)
	if spec.MarketWarmup {
		from = from.AddDate(0, 0, -400) // the signals' warm-up plus ~280 sessions for the floor's average
	}
	to := time.Now().UTC()
	if *asOf != "" {
		to, err = time.Parse("2006-01-02", *asOf)
		fatalIf(err)
		*dry = true
	}
	if traits.NeedsMarket(spec.Variants) {
		marketForBeta, err = marketReturns(ctx, st, from, to, spec.MinTurnover)
		fatalIf(err)
	}
	var variants []signalFn
	for _, v := range spec.Variants {
		fn, err := parseSignal(v)
		fatalIf(err)
		variants = append(variants, fn)
	}
	// Only an explicit -mode overrides the spec. A default of "single" here once
	// silently turned the lookback book-blend into a plain 6-month book.
	if *mode != "" {
		m, err := paper.ParseMode(*mode)
		fatalIf(err)
		spec.Mode = m
	}
	spec.SwitchBreadth = *switchBreadth
	if *constant > 0 {
		spec.ConstantExposure = *constant
	}

	fmt.Printf("%s: loading adjusted bars %s..%s\n", spec.Name,
		from.Format("2006-01-02"), to.Format("2006-01-02"))

	all, next, err := buildDays(ctx, st, spec, sig, sig2, variants, from, to, true)
	fatalIf(err)
	if len(all) == 0 {
		fatal(fmt.Errorf("no price data at all in %s..%s", from.Format("2006-01-02"), to.Format("2006-01-02")))
	}
	var tracked, prior []paper.Day
	for _, d := range all {
		if !d.Date.Before(spec.Start) {
			tracked = append(tracked, d)
		} else if spec.MarketWarmup {
			prior = append(prior, d)
		}
	}
	if spec.MarketWarmup && len(prior) <= spec.TrendWindow {
		fatal(fmt.Errorf("%s: %d warm-up sessions before the start, the floor needs more than %d", spec.Name, len(prior), spec.TrendWindow))
	}

	track, err := paper.ComputeWithPrior(spec, prior, tracked)
	fatalIf(err)

	// The Rs 1 crore account: the same targets in whole shares, Law 12 inertia
	// and Dhan's charges per trade (internal/capacity, LEDGER row 36). Forward
	// records only — a reference track's nominal rupees in 2022 would size
	// positions today's book never holds.
	var account *capacity.Result
	if *name == "" && *start == "" && capacity.Supports(spec) {
		raw := func(d time.Time) (map[string]float64, error) { return st.RawRatios(ctx, d) }
		account, err = capacity.Simulate(spec, tracked, raw, capacity.AccountPolicy())
		fatalIf(err)
		track.Books[paper.BookAccount] = account.Book(paper.BookAccount)
	}

	// The order sheet is computed from the most recent close available, even
	// when the forward record itself has not started yet: on day one the sheet
	// IS the product, and the history is empty by definition.
	current := map[string]float64{}
	daysToDue := 1
	if track != nil && len(track.Dates) > 0 {
		current = track.Books[paper.BookStrategy].Holdings
		daysToDue = track.DaysToNextRebalance()
	}
	sheet := paper.PendingAt(spec, *next, current, daysToDue, track.NextExposure, track.Members())
	if track != nil && !sheet.Due {
		// Not a rebalance: the sheet would show only "if it were due" orders, so a stop exit
		// queued for the next open is listed explicitly (review 2026-10-02).
		latest := all[len(all)-1]
		closes := map[string]float64{}
		for _, o := range latest.Obs {
			closes[o.Symbol] = o.Close
		}
		var exits []paper.Order
		stops := track.PendingExits()
		// A daily overlay that turns to cash sells the whole book at the next open, not only
		// the stopped names.
		if spec.Overlay != paper.OverlayNone && spec.ExposureDaily && track.NextExposure == 0 {
			stops = stops[:0]
			for sym, w := range current {
				if w > 0 {
					stops = append(stops, sym)
				}
			}
			sort.Strings(stops)
		}
		for _, sym := range stops {
			exits = append(exits, paper.Order{Date: latest.Date, Book: paper.BookStrategy, Symbol: sym, Side: "EXIT",
				FromWeight: current[sym], ToWeight: 0, FillPrice: closes[sym]})
		}
		sheet.Orders = append(exits, sheet.Orders...)
	}
	if account != nil {
		latest := all[len(all)-1]
		ratios, err := st.RawRatios(ctx, latest.Date)
		fatalIf(err)
		// A held name NSE moved from EQ to BE / BZ has no EQ price today; price it from the
		// series it trades in, so it is sold properly instead of failing the order check.
		anyClose, anySeries, err := st.RawClosesAnySeries(ctx, latest.Date)
		fatalIf(err)
		trades, held, err := account.PlanOrders(spec, *next, ratios, anyClose)
		fatalIf(err)
		for _, t := range trades {
			if ser := anySeries[t.Symbol]; ser != "" && ser != "EQ" {
				fmt.Printf("note: %s now trades in %s (trade-for-trade); priced and ordered in that series\n", t.Symbol, ser)
			}
		}
		sheet.Orders = withShares(sheet, current, trades, held)
	}

	report(spec, track, sheet)

	if *asOf != "" && account != nil {
		for _, o := range sheet.Orders {
			if o.InShares && o.ValueRs > 0 {
				fmt.Printf("sheet\t%s\t%s\t%.0f\t%.0f\n", o.Symbol, o.Side, o.FromShares, o.ToShares)
			}
		}
		last := account.Dates[len(account.Dates)-1]
		for _, t := range account.Trades {
			if t.Date.Equal(last) {
				fmt.Printf("fill\t%s\t%s\t%.0f\t%.0f\t%s\n", t.Symbol, t.Side, t.FromShares, t.ToShares, t.Date.Format("2006-01-02"))
			}
		}
	}

	if *dry {
		fmt.Println("\n-dry: nothing written")
		return
	}
	fatalIf(st.EnsurePaperTables(ctx))
	fatalIf(st.SavePaperTrack(ctx, track))
	fatalIf(st.SavePending(ctx, spec.Name, sheet))
	// What qualifies today, per variant — for the screener's cross-strategy
	// view. Forward records only: a reference track's latest day is the same
	// day and would only duplicate it.
	if *name == "" && *start == "" {
		quals := paper.Qualify(spec, *next)
		fatalIf(st.SaveQualifications(ctx, spec.Name, next.Date, quals))
		fmt.Printf("qualifying: %d stock-variant pairs on %s\n", len(quals), next.Date.Format("2006-01-02"))
	}
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
	if b, ok := t.Books[paper.BookAccount]; ok && len(b.NAV) > 0 {
		names = append(names, paper.BookAccount)
	}
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
	var trades int
	var traded, charges float64
	for _, o := range sheet.Orders {
		if o.InShares && o.ValueRs > 0 {
			trades++
			traded += o.ValueRs
			charges += o.CostRs
		}
	}
	if trades > 0 {
		fmt.Printf("Rs %.0f crore account: %d orders, Rs %.0f traded, charges Rs %.0f (%.1f bps)\n",
			paper.AccountCapital/1e7, trades, traded, charges, 1e4*charges/traded)
	}
	for i, o := range sheet.Orders {
		if i >= 10 {
			fmt.Printf("  ... and %d more\n", len(sheet.Orders)-10)
			break
		}
		line := fmt.Sprintf("  %-5s %-14s %6.2f%% -> %6.2f%%  ref %.2f",
			o.Side, o.Symbol, 100*o.FromWeight, 100*o.ToWeight, o.FillPrice)
		if o.InShares {
			line += fmt.Sprintf("   %5.0f -> %5.0f shares  Rs %9.0f  charges Rs %5.0f",
				o.FromShares, o.ToShares, o.ValueRs, o.CostRs)
		}
		fmt.Println(line)
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
		return ruleSignal(rules.EWMAC{Fast: 32}), nil
	case "ewmac16":
		return ruleSignal(rules.EWMAC{Fast: 16}), nil
	case "trend-speed-blend":
		return ruleSignal(rules.SpeedBlend()), nil
	case "mom6":
		return windowReturn(6*month, 0), nil
	case "stage2-rs26":
		// Ranked by 6-month return; the Stage 2 filter and exit come from Spec.Stage2Only.
		return windowReturn(6*month, 0), nil
	case "mom9":
		return windowReturn(9*month, 0), nil
	case "mom12":
		return windowReturn(12*month, 0), nil
	case "mom12_1":
		return windowReturn(12*month, month), nil
	case "vol1", "vol3", "vol6", "vol12", "beta12", "idio12":
		return func(ser bars.Series, _ core.Series, _ core.Series) []float64 {
			s, err := traits.LowRiskScore(name, ser.Bars, marketForBeta)
			fatalIf(err)
			return s
		}, nil
	case "quality":
		// Not computed from prices: buildDays fills it from stockey's quality track holdings.
		return func(ser bars.Series, _ core.Series, _ core.Series) []float64 {
			out := make([]float64, len(ser.Bars))
			for i := range out {
				out[i] = math.NaN()
			}
			return out
		}, nil
	case "low-volatility-blend", "momentum-lowvol-combination", "momentum-quality-combination":
		// Book-blends rank by their variants; eligibility comes from them.
		return windowReturn(6*month, 0), nil
	case "momentum-lookback-blend":
		// A book-blend ranks by its variants (spec.Variants); the primary
		// signal only decides eligibility, which buildDays sets from them.
		return windowReturn(6*month, 0), nil
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
	return nil, fmt.Errorf("unknown signal %q (ewmac32 | ewmac16 | trend-speed-blend | ret5 | ret5inv | reversal20 | reversal60 | reversal120)", name)
}

// marketForBeta is the market the beta-based low-risk scores measure against,
// loaded once in main when a spec's variants need it.
var marketForBeta map[time.Time]float64

// marketReturns builds that market from the same stream the tracker reads:
// the equal-weight mean daily return of the names eligible the day before —
// the construction traits.MarketReturns uses on the research bar cache.
func marketReturns(ctx context.Context, st *store.Store, from, to time.Time, floor float64) (map[time.Time]float64, error) {
	type acc struct {
		sum float64
		n   int
	}
	sums := map[time.Time]*acc{}
	err := st.StreamAdjustedBars(ctx, from, to, func(ser bars.Series) error {
		b := ser.Bars
		if len(b) < 2 {
			return nil
		}
		turn := bars.MedianTurnover(b, traits.TurnoverWindow)
		for i := 1; i < len(b); i++ {
			r := traits.DailyReturn(b, i)
			if math.IsNaN(r) || math.IsNaN(turn[i-1]) || turn[i-1] < floor {
				continue
			}
			d := traits.Day(b[i].Date)
			a := sums[d]
			if a == nil {
				a = &acc{}
				sums[d] = a
			}
			a.sum += r
			a.n++
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	out := map[time.Time]float64{}
	for d, a := range sums {
		if a.n >= 100 {
			out[d] = a.sum / float64(a.n)
		}
	}
	return out, nil
}

// month is 21 trading days, as in `slice windows`.
const month = 21

// windowReturn is the trailing total return over t−from → t−to trading days,
// the formation windows of LEDGER row 30.
func windowReturn(from, to int) signalFn {
	return func(_ bars.Series, closes core.Series, _ core.Series) []float64 {
		v := closes.Values
		out := make([]float64, len(v))
		for i := range out {
			out[i] = math.NaN()
			if i-from >= 0 && v[i-from] > 0 && v[i-to] > 0 {
				out[i] = v[i-to]/v[i-from] - 1
			}
		}
		return out
	}
}

func ruleSignal(r rules.Rule) signalFn {
	return func(ser bars.Series, closes core.Series, vol core.Series) []float64 {
		inst := &data.Instrument{
			Meta:   data.Meta{Symbol: ser.Symbol, PointValue: 1, Block: 1, LongOnly: true},
			Prices: closes,
		}
		return rules.Forecast(r, inst, vol).Values
	}
}

// buildDays streams the adjusted bars into one paper.Day per session.
//
// With `all`, every recent day also carries Decided -- every name that traded at
// the previous close, decided there, which is what the sheet printed that evening
// chose from -- and next is the session after the last bar, decided at its close.
// The order sheet is planned on next and the account fills on Decided, so the two
// are the same orders. A day's own Obs carry signals from the close BEFORE it:
// planning tonight's sheet on the last real day read signals a session stale, and
// a name that did not trade on the fill day dropped out of the account's ranking
// but not the sheet's (2026-10-20).
func buildDays(ctx context.Context, st *store.Store, spec paper.Spec, signal, signal2 signalFn, variants []signalFn, from, to time.Time, all bool) (days []paper.Day, next *paper.Day, err error) {
	byDate := map[time.Time][]paper.Obs{}
	// decided[close] is every name that traded at that close, decided there; only
	// kept from just before the tracked record (or the sheet) starts.
	decided := map[time.Time][]paper.Obs{}
	decidedFrom := spec.Start
	if to.Before(decidedFrom) {
		decidedFrom = to
	}
	decidedFrom = decidedFrom.AddDate(0, 0, -10)
	var fundamentals *paper.FundamentalPass
	if spec.FundamentalFilter {
		rows, err := st.StoryFilter(ctx)
		if err != nil {
			return nil, nil, err
		}
		in := make([]paper.FilterRow, len(rows))
		for i, r := range rows {
			in[i] = paper.FilterRow{Date: r.Date, Symbol: r.Symbol, Score: r.Score, HasFlaw: r.HasFlaw, Version: r.ScoreVersion}
		}
		fundamentals = paper.NewFundamentalPass(in)
		if latest := fundamentals.Latest(); latest.IsZero() || to.Sub(latest) > 7*24*time.Hour {
			// Carried forward, never silently: a stale filter keeps yesterday's verdicts.
			fmt.Fprintf(os.Stderr, "paper: WARNING %s: newest stockey story score is %s (> 7 days before %s) -- filter carried forward\n",
				spec.Name, latest.Format("2006-01-02"), to.Format("2006-01-02"))
		}
	}
	// A "quality" variant holds stockey's quality track (TODO D1, LEDGER row 60): the names
	// it held after its last rebalance on or before the decision day.
	qualityAt := -1
	var quality *paper.TrackHoldings
	for k, v := range spec.Variants {
		if v == "quality" {
			qualityAt = k
		}
	}
	if qualityAt >= 0 {
		rows, err := st.ForwardTrackHoldings(ctx, "quality", 1)
		if err != nil {
			return nil, nil, err
		}
		in := make([]paper.HoldingRow, len(rows))
		for i, r := range rows {
			in[i] = paper.HoldingRow{RebalanceDate: r.RebalanceDate, Symbol: r.Symbol}
		}
		quality = paper.NewTrackHoldings(in)
		if latest := quality.Latest(); latest.IsZero() || to.Sub(latest) > 40*24*time.Hour {
			// a monthly track: more than 40 days without a rebalance means the feed stopped
			fmt.Fprintf(os.Stderr, "paper: WARNING %s: newest quality rebalance is %s (> 40 days before %s) -- holdings carried forward\n",
				spec.Name, latest.Format("2006-01-02"), to.Format("2006-01-02"))
		}
	}
	var groups map[string][2]string
	if spec.MaxPerGroup > 0 {
		var err error
		if groups, err = st.RotationMembership(ctx); err != nil {
			return nil, nil, err
		}
	}
	var funds map[string]bool
	if spec.ExcludeFunds {
		if funds, err = st.FundSymbols(ctx); err != nil {
			return nil, nil, err
		}
	}
	err = st.StreamAdjustedBars(ctx, from, to, func(ser bars.Series) error {
		n := len(ser.Bars)
		if n < minBars || funds[ser.Symbol] {
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
		vs := make([][]float64, len(variants))
		for k, fn := range variants {
			vs[k] = fn(ser, prices, vol)
		}
		sma := core.SMA(prices, 200).Values
		turnover := bars.MedianTurnover(ser.Bars, spec.TurnoverWindow)
		var weekly []stage.Classification
		if spec.Stage2Only {
			vols := make([]float64, n)
			for i, b := range ser.Bars {
				vols[i] = b.Vol
			}
			weekly = stage.DailyView(times, closes, vols)
		}
		group := ""
		if groups != nil {
			group = groups[ser.Symbol][1]
		}

		// Bars before the start date are kept when `all` is set: the order
		// sheet needs the latest close even when the tracked record has not
		// begun. The warm-up window before that is dropped either way.
		earliest := spec.Start
		if all {
			earliest = from.AddDate(0, 0, 200)
		}
		// obsAt is the session after bar j, decided at j's close.
		obsAt := func(j int) paper.Obs {
			f, t60 := fc[j], turnover[j]
			var sigs []float64
			if len(vs) > 0 {
				sigs = make([]float64, len(vs))
				defined := false
				for k := range vs {
					sigs[k] = vs[k][j]
					if k == qualityAt {
						sigs[k] = math.NaN()
						if quality.Held(ser.Symbol, times[j]) {
							sigs[k] = 1
						}
					}
					if !math.IsNaN(sigs[k]) {
						defined = true
					}
				}
				if spec.Mode == paper.ModeBookBlend {
					// Eligible if any variant can score the name yet.
					f = math.NaN()
					if defined {
						f = 0
					}
				}
			}
			eligible := !math.IsNaN(f) && !math.IsNaN(t60) && t60 >= spec.MinTurnover
			annVol := math.NaN()
			if v := vol.Values[j]; !math.IsNaN(v) && closes[j] > 0 {
				annVol = v / closes[j] * 16 // daily price vol -> annualised fraction
			}
			o := paper.Obs{
				Symbol: ser.Symbol, PrevClose: closes[j],
				Forecast: f, Turnover: t60, Eligible: eligible, AnnVol: annVol,
				Forecast2: fc2[j],
				Signals:   sigs,
				AboveSMA:  !math.IsNaN(sma[j]) && closes[j] > sma[j],
				Group:     group,
			}
			if weekly != nil {
				o.Excluded = weekly[j].Stage != stage.Stage2Advancing // buying: the last week completed by j's close
			}
			if fundamentals != nil {
				if ok, _ := fundamentals.Passes(ser.Symbol, times[j]); !ok {
					o.Excluded = true
				}
			}
			return o
		}
		// A name qualifies once minBars bars have closed by the decision. Gating on
		// the whole series admitted a new listing a session early in the replay,
		// against a sheet that could not have had it (2026-10-20).
		for j := minBars - 1; all && j < n; j++ {
			if closes[j] > 0 && !times[j].Before(decidedFrom) {
				o := obsAt(j)
				o.Open, o.Close = closes[j], closes[j]
				decided[times[j]] = append(decided[times[j]], o)
			}
		}
		for i := minBars; i < n; i++ {
			d := times[i]
			if d.Before(earliest) {
				continue
			}
			b := ser.Bars[i]
			if b.Open <= 0 || b.Close <= 0 {
				continue
			}
			o := obsAt(i - 1) // decided at yesterday's close
			o.Open, o.Close = b.Open, b.Close
			if weekly != nil {
				// Exiting: the week completed at TODAY's close. The stop check runs after this
				// close and sells at the next open, so a Friday break sells Monday -- with
				// weekly[i-1] it sold a session late (review 2026-10-02). No look-ahead: it is
				// only read after day i's close.
				x := weekly[i]
				o.ExitSignal = x.Stage != stage.StageUnknown && x.Close < x.MA30
			}
			byDate[d] = append(byDate[d], o)
		}
		return nil
	})
	if err != nil {
		return nil, nil, err
	}

	days = make([]paper.Day, 0, len(byDate))
	for d, obs := range byDate {
		days = append(days, paper.Day{Date: d, Obs: obs})
	}
	sort.Slice(days, func(i, j int) bool { return days[i].Date.Before(days[j].Date) })
	if all && len(days) > 0 {
		for k := 1; k < len(days); k++ {
			days[k].Decided = decided[days[k-1].Date]
		}
		latest := days[len(days)-1].Date
		next = &paper.Day{Date: latest, Obs: decided[latest]}
	}
	return days, next, nil
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

// withShares puts the Rs 1 crore account's orders beside the weight book's.
// Each weight order gets the account's share count, rupees and charges; a
// name inside Law 12's band shows its holding unchanged and no rupees; a name
// the account trades that the weight sheet does not list (a rounding
// difference) gets a row of its own.
func withShares(sheet paper.PendingSheet, current map[string]float64, trades []capacity.Trade, held map[string]float64) []paper.Order {
	bySym := make(map[string]capacity.Trade, len(trades))
	for _, t := range trades {
		bySym[t.Symbol] = t
	}
	out := make([]paper.Order, 0, len(sheet.Orders)+len(trades))
	seen := map[string]bool{}
	for _, o := range sheet.Orders {
		seen[o.Symbol] = true
		o.InShares = true
		if t, ok := bySym[o.Symbol]; ok {
			o.FromShares, o.ToShares, o.ValueRs, o.CostRs = t.FromShares, t.ToShares, t.Value, t.Cost
			o.Side = t.Side // the shares' direction: rounding can run against the weight's
		} else {
			o.FromShares, o.ToShares = held[o.Symbol], held[o.Symbol]
		}
		out = append(out, o)
	}
	for _, t := range trades {
		if seen[t.Symbol] {
			continue
		}
		o := capacity.ToOrder(t, paper.BookStrategy, 0)
		o.Date = sheet.BasedOn
		o.FromWeight, o.ToWeight = current[t.Symbol], sheet.Target[t.Symbol]
		out = append(out, o)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Side != out[j].Side {
			return out[i].Side < out[j].Side
		}
		return out[i].Symbol < out[j].Symbol
	})
	return out
}
