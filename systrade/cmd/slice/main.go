// Command slice asks where and when a rule worked, and what it costs to trade.
//
//	slice explore -rule ewmac32_128 -horizon 20
//	slice cost    -rule ewmac32_128
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
	"github.com/ranedk/systrader/internal/explore"
	"github.com/ranedk/systrader/internal/rules"
	"github.com/ranedk/systrader/internal/store"
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
	if len(os.Args) > 1 && os.Args[1] == "coi" {
		runCOI(os.Args[2:])
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
	to := flag.String("to", "2026-06-30", "last decision date")
	minTurnover := flag.Float64("min-turnover", 1e7, "minimum 60-bar median traded value in INR")
	quantiles := flag.Int("quantiles", 5, "buckets per continuous dimension")
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

	ctx := context.Background()
	st, err := store.Open(ctx)
	fatalIf(err)
	defer st.Close()
	sectors, err := st.SectorCodes(ctx)
	fatalIf(err)
	mcaps, err := st.MarketCaps(ctx)
	fatalIf(err)

	days := build(*cache, rule, *horizon, mustDate(*from), mustDate(*to), *minTurnover, sectors, mcaps)
	sort.Slice(days, func(i, j int) bool { return days[i].Date.Before(days[j].Date) })

	dims := []explore.Dimension{
		explore.QuantileDimension("liquidity", *quantiles, func(o explore.Obs) float64 { return o.Turnover }),
		explore.QuantileDimension("size (mcap)", *quantiles, func(o explore.Obs) float64 { return o.Mcap }),
		explore.QuantileDimension("own volatility", *quantiles, func(o explore.Obs) float64 { return o.Vol }),
		explore.QuantileDimension("price level", *quantiles, func(o explore.Obs) float64 { return o.Price }),
		explore.CategoryDimension("sector", func(o explore.Obs) string { return o.Sector }),
		explore.BooleanDimension("own trend", "above 200DMA", "below 200DMA", func(o explore.Obs) bool { return o.AboveSMA }),
		explore.DayDimension("market breadth", explore.BreadthLabel, explore.BreadthOrder),
		explore.DayDimension("year", func(d explore.Day) string { return fmt.Sprintf("%d", d.Date.Year()) }, nil),
	}

	res := explore.Run(days, dims, rule.Name(), *horizon)
	printResult(res, mcaps, sectors, false)
}

func printResult(res explore.Result, mcaps map[string][]store.MCapPoint, sectors map[string]string, event bool) {
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
	fmt.Println()
	fmt.Println("NO BUCKET BELOW IS A RESULT. With this many buckets the best one is what noise")
	fmt.Println("looks like. Read the halves and the years: an edge that lives in one of them is")
	fmt.Println("the shape of LEDGER row 13's dead effect. Take a MECHANISM from here, then test")
	fmt.Println("it where this command has not looked.")
	fmt.Println()

	o := res.Overall
	pos, tot := o.YearsPositive()
	second := "bottom"
	if event {
		second = "held"
	}
	header := func(first string) {
		fmt.Printf("%-28s %6s %7s %8s %8s %8s %8s %6s %6s\n",
			first, "names", "days", "edge", second, "1st half", "2nd half", "vol x", "yrs +")
	}
	row := func(label string, b explore.Bucket) {
		p, t := b.YearsPositive()
		secondVal := 100 * b.BottomEdge
		format := "%-28s %6.0f %7d %7.3f%% %7.3f%% %7.3f%% %7.3f%% %6.2f %4d/%d\n"
		if event {
			secondVal = b.Held
			format = "%-28s %6.0f %7d %7.3f%% %8.1f %7.3f%% %7.3f%% %6.2f %4d/%d\n"
		}
		fmt.Printf(format, truncate(label, 28), b.Names, b.Days, 100*b.Edge, secondVal,
			100*b.FirstHalf, 100*b.SecondHal, b.VolRatio, p, t)
	}
	header("unsliced yardstick")
	row("whole universe", o)
	fmt.Println()
	_ = pos
	_ = tot

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
			row(label, b)
		}
		// Dispersion across a dimension says more than any single bucket: a
		// rule whose edge is identical everywhere has no conditioning to find.
		fmt.Printf("%-28s spread %.3f%% between the widest and narrowest bucket\n\n", "", 100*spread(bs))
	}
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
	sort.Slice(members, func(i, j int) bool { return members[i].mcap > members[j].mcap })
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

// build streams the bar cache and produces the daily cross-sections.
func build(cache string, rule rules.Rule, horizon int, from, to time.Time,
	minTurnover float64, sectors map[string]string, mcaps map[string][]store.MCapPoint) []explore.Day {

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
		fc := rules.Forecast(rule, inst, vol).Values
		sma := core.SMA(prices, trendWin).Values
		turnover := bars.MedianTurnover(ser.Bars, turnoverWin)
		mc := mcaps[ser.Symbol]
		sector := sectors[ser.Symbol]

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
			f := fc[i]
			if entry <= 0 || exit <= 0 || math.IsNaN(f) || math.IsNaN(sma[i]) || math.IsNaN(vol.Values[i]) {
				continue
			}
			local = append(local, struct {
				d time.Time
				o explore.Obs
			}{d, explore.Obs{
				Forecast: f,
				FwdRet:   exit/entry - 1,
				Turnover: turnover[i],
				Vol:      vol.Values[i] / closes[i],
				Price:    closes[i],
				Mcap:     mcapAt(mc, d),
				Sector:   sector,
				AboveSMA: closes[i] > sma[i],
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

	days := make([]explore.Day, 0, len(byDate))
	for d, obs := range byDate {
		days = append(days, explore.Day{Date: d, Obs: obs})
	}
	if len(days) == 0 {
		fatal(fmt.Errorf("no observations in %s..%s", from.Format("2006-01-02"), to.Format("2006-01-02")))
	}
	return days
}

// mcapAt returns the latest market cap reported on or before d — point in
// time, never the value that was only published later.
func mcapAt(pts []store.MCapPoint, d time.Time) float64 {
	i := sort.Search(len(pts), func(i int) bool { return pts[i].Date.After(d) })
	if i == 0 {
		return 0
	}
	return pts[i-1].MCap
}

func findRule(name string) rules.Rule {
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
