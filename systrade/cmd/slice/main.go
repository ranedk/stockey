// Command slice asks where and when a rule worked, and what it costs to trade.
//
//	slice explore -rule ewmac32_128 -horizon 20
//	slice cost     -rule ewmac32_128
//	slice family   (row 18's configurations, judged with internal/evidence)
//	slice speeds   (speed-blend construction: costs and correlations only)
//	slice blend    (the speed blend's pre-registered kill screen)
//	slice windows  (momentum across formation x holding windows, pre-registered)
//	slice lookbacks (the momentum lookback blend's construction: costs and correlations)
//	slice lowvol   (the low-volatility anomaly across time windows, pre-registered)
//	slice stage    (Weinstein's stage analysis: -part buckets | strategy, pre-registered)
//	slice drawdown -rule ewmac32_128
//
// This is EXPLORATION. It prints no verdict and no p-value on a winning
// bucket, because the best of sixty buckets shows t ~ 3 on noise and this
// workspace has already been burned by exactly that (LEDGER rows 12 and 13).
// What it prints is a map: the rule's edge inside each slice of the universe,
// each slice measured against ITSELF on the same day, split by half-sample and
// by year so a stretch-dependent effect cannot hide.
//
// Use it to find a mechanism worth writing down. Confirm the mechanism
// somewhere this command has never looked — a later period, or forward.
package main

import (
	"context"
	"flag"
	"fmt"
	"math"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/ranedk/systrader/internal/bars"
	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/data"
	"github.com/ranedk/systrader/internal/evidence"
	"github.com/ranedk/systrader/internal/explore"
	"github.com/ranedk/systrader/internal/rules"
	"github.com/ranedk/systrader/internal/store"
	"github.com/ranedk/systrader/internal/traits"
)

const (
	defaultCache = "data/cache/bars_daily.bin"
	volSpan      = 36
	volMin       = 10
	turnoverWin  = 60
	trendWin     = 200
)

func main() {
	if len(os.Args) > 1 && os.Args[1] == "cost" {
		runCost(os.Args[2:])
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "lowvol" {
		runLowVol(os.Args[2:])
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "stage" {
		runStage(os.Args[2:])
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "lookbacks" {
		runLookbacks(os.Args[2:])
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "windows" {
		runWindows(os.Args[2:])
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "blend" {
		runBlend(os.Args[2:])
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "speeds" {
		runSpeeds(os.Args[2:])
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "family" {
		runFamily(os.Args[2:])
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "coi" {
		runCOI(os.Args[2:])
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "drawdown" {
		runDrawdown(os.Args[2:])
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "explore" {
		os.Args = append(os.Args[:1], os.Args[2:]...)
	}
	cache := flag.String("cache", defaultCache, "bar cache file")
	ruleName := flag.String("rule", "ewmac32_128", "rule to explore (see -list)")
	list := flag.Bool("list", false, "list available rules and exit")
	horizon := flag.Int("horizon", 20, "forward holding period in trading days")
	from := flag.String("from", "2013-07-01", "first decision date")
	to := flag.String("to", "2021-12-31", "last decision date — exploration stops before the confirmation years (see -include-confirmation-years)")
	minTurnover := flag.Float64("min-turnover", 1e7, "minimum 60-bar median traded value in INR")
	quantiles := flag.Int("quantiles", 5, "buckets per continuous dimension")
	costBps := flag.Float64("cost-bps", 50, "round-trip cost charged on each bucket's churn")
	reps := flag.Int("reps", 1000, "block-bootstrap resamples for each bucket's 'vs ctl' score (0 = skip)")
	readConfirm := flag.Bool("include-confirmation-years", false, "let -to reach 2022 onward, the years kept unread for confirmation")
	flag.Parse()

	if *list {
		for _, r := range rules.Library() {
			fmt.Println(r.Name())
		}
		return
	}
	rule := findRule(*ruleName)
	if rule == nil {
		fatal(fmt.Errorf("unknown rule %q (try -list)", *ruleName))
	}

	guardConfirmationYears(mustDate(*to), *readConfirm)

	ctx := context.Background()
	st, err := store.Open(ctx)
	fatalIf(err)
	defer st.Close()
	sectors, err := st.SectorCodes(ctx)
	fatalIf(err)
	mcaps, err := st.MarketCaps(ctx) // names sector buckets only; not a slicing trait
	fatalIf(err)
	tc, err := traits.LoadContext(ctx, st, *cache, *minTurnover, mustDate(*from), mustDate(*to))
	fatalIf(err)

	days := build(*cache, rule, nil, *horizon, mustDate(*from), mustDate(*to), *minTurnover, sectors, tc)
	sort.Slice(days, func(i, j int) bool { return days[i].Date.Before(days[j].Date) })

	dims := universalDims(*quantiles)

	res := explore.Run(days, dims, rule.Name(), *horizon)
	printResult(res, deviations(res, *horizon, *reps), *costBps, mcaps, sectors, false)
}

func printResult(res explore.Result, dev explore.Deviation, costBps float64, mcaps map[string][]store.MCapPoint, sectors map[string]string, event bool) {
	fmt.Printf("EXPLORATION — %s, %d-day forward return, entries at the open after the decision close\n",
		res.Rule, res.Horizon)
	fmt.Printf("%d decision days, %d symbol-days, %d dimensions, %d buckets examined\n",
		res.Days, res.Observations, res.Dimensions, len(res.Buckets))
	fmt.Println()
	if event {
		fmt.Println("Statistic: mean forward return of the names the pattern FIRED on inside a bucket,")
		fmt.Println("minus the mean of every name in that same bucket on that same day.")
	} else {
		fmt.Println("Statistic: mean forward return of the top-forecast fifth INSIDE a bucket, minus the")
		fmt.Println("mean of every name in that same bucket on that same day. Each slice is its own")
		fmt.Println("control, so the market's move and the slice's own drift both cancel.")
		fmt.Println("'bottom' is the same for the WORST-forecast fifth: a real ordering loses there.")
	}
	fmt.Println("'vol x' is how volatile the selected names are relative to their bucket — well")
	fmt.Println("above 1.0 means the edge may be risk premium, not selection (LEDGER row 4).")
	fmt.Println("'churn' is the share of the selected names replaced each holding period — what")
	fmt.Printf("actually trades; 'net' is the edge after %.0f bps round trip on that churn.\n", costBps)
	fmt.Println()
	if dev.Reps > 0 {
		fmt.Println("'vs ctl' asks whether the rule does better or worse in a bucket than it would anyway.")
		fmt.Println("A bucket of stocks is compared, day by day, with random groups of the SAME size drawn")
		fmt.Println("from the names its dimension covered that day (a small bucket's top fifth is less")
		fmt.Println("extreme, so comparing it with the whole universe would invent differences). A bucket")
		fmt.Println("of days — year, breadth — is compared with the rule on all other days. The score is")
		fmt.Println("that difference over its own standard error, from a block bootstrap")
		fmt.Printf("(%d resamples, mean block %.0f trading days,\n", dev.Reps, dev.MeanBlock)
		fmt.Println("so overlapping forward returns and slow-changing selections are not mistaken for")
		fmt.Println("independent evidence). The same blocks are drawn for every bucket, so the threshold is")
		fmt.Printf("family-wise: with no real differences, the largest score of all %d buckets exceeds %.1f\n", len(res.Buckets), dev.Threshold)
		fmt.Println("in only 5% of resamples. ◆ marks a real bucket beyond it — worth writing a mechanism")
		fmt.Println("down for; still not a result.")
	} else {
		fmt.Println("NO RESAMPLES (-reps 0): no bucket below can be told apart from luck.")
	}
	fmt.Println()
	fmt.Println("Read the halves and the years too: an edge that lives in one of them is the shape")
	fmt.Println("of LEDGER row 13's dead effect.")
	fmt.Println()

	second := "bottom"
	if event {
		second = "held"
	}
	cost := costBps / 1e4
	header := func(first string) {
		fmt.Printf("  %-28s %6s %7s %8s %7s %8s %6s %8s %8s %8s %6s %6s\n",
			first, "names", "days", "edge", "vs ctl", second, "churn", "net", "1st half", "2nd half", "vol x", "yrs +")
	}
	row := func(label string, b explore.Bucket, markable bool) {
		p, t := b.YearsPositive()
		mark, vsAll := "  ", "      ·"
		if markable {
			if z, ok := dev.Z[explore.BucketKey(b)]; ok {
				vsAll = fmt.Sprintf("%+6.1f", z)
				if math.Abs(z) > dev.Threshold {
					mark = "◆ "
				}
			}
		}
		secondVal := fmt.Sprintf("%7.3f%%", 100*b.BottomEdge)
		if event {
			secondVal = fmt.Sprintf("%8.1f", b.Held)
		}
		net := b.Edge - b.Churn*cost
		fmt.Printf("%s%-28s %6.0f %7d %7.3f%% %s %s %5.0f%% %7.3f%% %7.3f%% %7.3f%% %6.2f %4d/%d\n",
			mark, truncate(label, 28), b.Names, b.Days, 100*b.Edge, vsAll, secondVal, 100*b.Churn, 100*net,
			100*b.FirstHalf, 100*b.SecondHal, b.VolRatio, p, t)
	}
	header("unsliced yardstick")
	row("whole universe", res.Overall, false)
	fmt.Println()

	byDim := map[string][]explore.Bucket{}
	var order []string
	for _, b := range res.Buckets {
		if _, seen := byDim[b.Dimension]; !seen {
			order = append(order, b.Dimension)
		}
		byDim[b.Dimension] = append(byDim[b.Dimension], b)
	}
	for _, dim := range order {
		bs := byDim[dim]
		sort.Slice(bs, func(i, j int) bool { return bs[i].Label < bs[j].Label })
		fmt.Printf("--- %s ---\n", dim)
		header("bucket")
		for _, b := range bs {
			label := b.Label
			if dim == "sector" {
				label = fmt.Sprintf("%s %s", b.Label, sectorHint(b.Label, sectors, mcaps))
			}
			row(label, b, true)
		}
		// Dispersion across a dimension says more than any single bucket: a
		// rule whose edge is identical everywhere has no conditioning to find.
		fmt.Printf("  %-28s spread %.3f%% between the widest and narrowest bucket\n\n", "", 100*spread(bs))
	}
}

// universalDims are the traits every slicing command cuts by — chosen by the
// operator 2026-09-11 from LEDGER rows 27-28: one member of each duplicate
// group (liquidity, own volatility), price, beta, delivery %, distance below
// the 52-week high (in place of the binary 200-day trend flag), upper and
// lower price-band hits, sector, and the two whole-day labels.
func universalDims(q int) []explore.Dimension {
	circuitEdges := []float64{0, 1, 3, 6}
	circuitLabels := []string{"0", "1-2", "3-5", "6+"}
	return []explore.Dimension{
		explore.QuantileDimension("liquidity", q, func(o explore.Obs) float64 { return o.Turnover }),
		explore.QuantileDimension("own volatility", q, func(o explore.Obs) float64 { return o.Vol }),
		explore.QuantileDimension("price level", q, func(o explore.Obs) float64 { return o.Price }),
		explore.RankDimension("beta", q, func(o explore.Obs) float64 { return o.Beta }),
		explore.RankDimension("delivery %", q, func(o explore.Obs) float64 { return o.Delivery }),
		explore.RankDimension("below 52w high", q, func(o explore.Obs) float64 { return o.Dist52 }),
		explore.BinDimension("upper circuits (60d)", func(o explore.Obs) float64 { return o.UpCircuits }, circuitEdges, circuitLabels),
		explore.BinDimension("lower circuits (60d)", func(o explore.Obs) float64 { return o.LoCircuits }, circuitEdges, circuitLabels),
		explore.CategoryDimension("sector", func(o explore.Obs) string { return o.Sector }),
		explore.DayDimension("market breadth", explore.BreadthLabel, explore.BreadthOrder),
		explore.DayDimension("year", func(d explore.Day) string { return fmt.Sprintf("%d", d.Date.Year()) }, nil),
	}
}

// confirmationStart is where exploration stops by default. Explore freely,
// confirm elsewhere (docs/RESEARCH_PROTOCOL.md): everything from here on stays
// unread so a mechanism found in the earlier years can be tested on years it
// has never seen. Reading them here has to be asked for on the command line.
var confirmationStart = time.Date(2022, 1, 1, 0, 0, 0, 0, time.UTC)

func guardConfirmationYears(to time.Time, allowed bool) {
	if !to.Before(confirmationStart) && !allowed {
		fatal(fmt.Errorf("-to %s reaches into the confirmation years (%s onward), which exploration leaves unread; "+
			"pass -include-confirmation-years to read them anyway, and say so in the ledger row",
			to.Format("2006-01-02"), confirmationStart.Format("2006-01-02")))
	}
}

// deviations scores every bucket against the whole universe. The mean block
// is three holding periods: consecutive forward returns overlap for a whole
// horizon, and a slow rule's selections persist for several.
func deviations(res explore.Result, horizon, reps int) explore.Deviation {
	if reps <= 0 {
		return explore.Deviation{}
	}
	dev, err := explore.Deviations(res, evidence.Bootstrap{MeanBlock: float64(3 * horizon), Reps: reps, Seed: 1})
	fatalIf(err)
	return dev
}

func spread(bs []explore.Bucket) float64 {
	lo, hi := math.Inf(1), math.Inf(-1)
	for _, b := range bs {
		if b.Edge < lo {
			lo = b.Edge
		}
		if b.Edge > hi {
			hi = b.Edge
		}
	}
	if math.IsInf(lo, 1) {
		return math.NaN()
	}
	return hi - lo
}

// sectorHint names a sector bucket by its three largest members, since the
// local dim_security carries NSE's sector CODES and no lookup table for them.
func sectorHint(code string, sectors map[string]string, mcaps map[string][]store.MCapPoint) string {
	type sm struct {
		sym  string
		mcap float64
	}
	var members []sm
	for sym, c := range sectors {
		if c != code {
			continue
		}
		if pts := mcaps[sym]; len(pts) > 0 {
			members = append(members, sm{sym, pts[len(pts)-1].MCap})
		}
	}
	sort.Slice(members, func(i, j int) bool {
		if members[i].mcap != members[j].mcap {
			return members[i].mcap > members[j].mcap
		}
		return members[i].sym < members[j].sym // ties by name: the same label every run
	})
	var names []string
	for i := 0; i < len(members) && i < 3; i++ {
		names = append(names, members[i].sym)
	}
	if len(names) == 0 {
		return ""
	}
	return "(" + strings.Join(names, ", ") + ")"
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n-1] + "…"
}

// eventFn replaces a rule with a discrete screener: for every bar, whether it
// fired (1), did not (0), or the name should be left out that day (-1, e.g. a
// classifier still warming up), plus caller-defined Extra attributes computed
// for EVERY bar so a slice holds the pattern's names and the control together.
type eventFn func(b []bars.Bar) (fired []int8, extra [][8]float64)

// build streams the bar cache and produces the daily cross-sections — ranked
// by rule's forecast, or, when ev is set (and rule nil), marked by the event.
func build(cache string, rule rules.Rule, ev eventFn, horizon int, from, to time.Time,
	minTurnover float64, sectors map[string]string, tc traits.Context) []explore.Day {

	byDate := map[time.Time][]explore.Obs{}
	var mu sync.Mutex
	ids := map[string]int32{}

	fatalIf(bars.ScanParallel(cache, 0, func(ser bars.Series) {
		n := len(ser.Bars)
		if n < trendWin+horizon+2 {
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
		var fc []float64
		if rule != nil {
			fc = rules.Forecast(rule, inst, vol).Values
		}
		var fired []int8
		var extra [][8]float64
		if ev != nil {
			fired, extra = ev(ser.Bars)
		}
		sma := core.SMA(prices, trendWin).Values
		turnover := bars.MedianTurnover(ser.Bars, turnoverWin)
		sector := sectors[ser.Symbol]
		ta := tc.For(ser)

		var local []struct {
			d time.Time
			o explore.Obs
		}
		for i := 0; i < n-horizon-1; i++ {
			d := times[i]
			if d.Before(from) || d.After(to) {
				continue
			}
			if math.IsNaN(turnover[i]) || turnover[i] < minTurnover {
				continue
			}
			entry, exit := opens[i+1], opens[i+1+horizon]
			f := 0.0
			if fc != nil {
				f = fc[i]
			}
			if entry <= 0 || exit <= 0 || math.IsNaN(f) || math.IsNaN(sma[i]) || math.IsNaN(vol.Values[i]) {
				continue
			}
			var sel bool
			var ext [8]float64
			if ev != nil {
				if fired[i] < 0 {
					continue
				}
				sel, ext = fired[i] == 1, extra[i]
			}
			local = append(local, struct {
				d time.Time
				o explore.Obs
			}{d, explore.Obs{
				Forecast:   f,
				FwdRet:     exit/entry - 1,
				Turnover:   turnover[i],
				Vol:        vol.Values[i] / closes[i],
				Price:      closes[i],
				Sector:     sector,
				AboveSMA:   closes[i] > sma[i],
				Beta:       ta.Beta[i],
				Delivery:   ta.Delivery[i],
				Dist52:     ta.Dist52[i],
				UpCircuits: ta.UpCircuits[i],
				LoCircuits: ta.LoCircuits[i],
				Selected:   sel,
				Extra:      ext,
			}})
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
			o.Sym = id
			byDate[e.d] = append(byDate[e.d], o)
		}
		mu.Unlock()
	}))

	// Symbol ids arrive in whatever order the scan's workers finish, and the
	// explorer's matched controls draw from each day's names by position: left
	// as is, 'vs ctl' scores moved by up to 0.4 between identical runs and a ◆
	// came and went (found 2026-09-15; the fix buildScores got in LEDGER row 31).
	remap := stableIDs(ids)
	days := make([]explore.Day, 0, len(byDate))
	for d, obs := range byDate {
		for i := range obs {
			obs[i].Sym = remap[obs[i].Sym]
		}
		sort.Slice(obs, func(a, b int) bool { return obs[a].Sym < obs[b].Sym })
		days = append(days, explore.Day{Date: d, Obs: obs})
	}
	if len(days) == 0 {
		fatal(fmt.Errorf("no observations in %s..%s", from.Format("2006-01-02"), to.Format("2006-01-02")))
	}
	return days
}

func findRule(name string) rules.Rule {
	if b := rules.SpeedBlend(); name == b.Name() {
		return b
	}
	for _, r := range rules.Library() {
		if r.Name() == name {
			return r
		}
	}
	return nil
}

func mustDate(s string) time.Time {
	t, err := time.Parse("2006-01-02", s)
	fatalIf(err)
	return t
}

func fatalIf(err error) {
	if err != nil {
		fatal(err)
	}
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "slice:", err)
	os.Exit(1)
}
