package main

import (
	"context"
	"flag"
	"fmt"
	"math"
	"sort"
	"sync"
	"time"

	"github.com/ranedk/systrader/internal/bars"
	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/explore"
	"github.com/ranedk/systrader/internal/patterns/coi"
	"github.com/ranedk/systrader/internal/store"
)

// The COI pattern, re-examined with the slicing method.
//
// LEDGER rows 11-13 already rejected it three ways: no edge against a
// nearby-date control, no edge in four follow-up families, and the one
// surviving direction falsified once the pattern was removed from the day
// definition. This asks a different question — not "does it work" but "is
// there a slice of the universe where it works" — and it asks it with a
// control the earlier rounds did not have.
//
// The control is the whole point. Every indicator the original screener uses
// is computed for EVERY liquid name every day, not only for the ones that
// fired. So a bucket like "names touching the lower Bollinger band today"
// contains both the COI setups and every other name in the same state, and the
// statistic is what the pattern added over them. Row 11 found the pattern
// SUBTRACTED from the band condition (-1.40%, t=-3.86) using a hand-built
// control; if this tool is worth anything it should reproduce that.
//
// Prior: very low. Three rejections and no economic story. This is exploration
// under docs/RESEARCH_PROTOCOL.md — it can produce a hypothesis, never a result.

// Extra attribute indices, defined here because explore.Obs.Extra is untyped
// by design.
const (
	xMACDD1       = iota // MACD first derivative
	xEMALongD1           // 20-EMA slope: row 12's "dial"
	xBBSlopeDiff         // lower-band slope minus upper-band slope: "very important"
	xBandWidth           // (upper-lower)/close: volatility compression
	xMACDvsSignal        // MACD - Signal
	xLowerBand           // 1 if the bar touched or crossed the lower band: "important"
	xBBUpperD1           // upper-band slope
	xDropFromHigh        // how far below the 120-bar high this bar's low sits, %
)

func runCOI(args []string) {
	fs := flag.NewFlagSet("coi", flag.ExitOnError)
	cache := fs.String("cache", defaultCache, "bar cache file")
	horizon := fs.Int("horizon", 20, "forward holding period in trading days")
	from := fs.String("from", "2013-07-01", "first decision date")
	to := fs.String("to", "2026-06-30", "last decision date")
	minTurnover := fs.Float64("min-turnover", 1e7, "minimum 60-bar median traded value in INR")
	quantiles := fs.Int("quantiles", 5, "buckets per continuous dimension")
	fatalIf(fs.Parse(args))

	ctx := context.Background()
	st, err := store.Open(ctx)
	fatalIf(err)
	defer st.Close()
	sectors, err := st.SectorCodes(ctx)
	fatalIf(err)
	mcaps, err := st.MarketCaps(ctx)
	fatalIf(err)

	days := buildCOI(*cache, *horizon, mustDate(*from), mustDate(*to), *minTurnover, sectors)
	sort.Slice(days, func(i, j int) bool { return days[i].Date.Before(days[j].Date) })

	dims := []explore.Dimension{
		// The screener's own conditions, computed for every name so the
		// pattern is compared against others in the same state.
		explore.BooleanDimension("lower band ('important')", "touching the band", "away from it",
			func(o explore.Obs) bool { return o.Extra[xLowerBand] > 0 }),
		explore.BooleanDimension("BB slope diff ('very important')", "bands converging", "bands diverging",
			func(o explore.Obs) bool { return o.Extra[xBBSlopeDiff] > 0 }),
		explore.BooleanDimension("MACD vs signal", "MACD above", "MACD below",
			func(o explore.Obs) bool { return o.Extra[xMACDvsSignal] > 0 }),
		explore.BooleanDimension("upper band", "rising", "flat or falling",
			func(o explore.Obs) bool { return o.Extra[xBBUpperD1] > 0 }),
		explore.QuantileDimension("MACD 1st derivative", *quantiles,
			func(o explore.Obs) float64 { return shift(o.Extra[xMACDD1]) }),
		explore.QuantileDimension("20-EMA slope (the dial)", *quantiles,
			func(o explore.Obs) float64 { return shift(o.Extra[xEMALongD1]) }),
		explore.QuantileDimension("band width", *quantiles,
			func(o explore.Obs) float64 { return o.Extra[xBandWidth] }),
		explore.QuantileDimension("drop from 120-bar high", *quantiles,
			func(o explore.Obs) float64 { return o.Extra[xDropFromHigh] }),
		// The universal dimensions, identical to `slice explore`.
		explore.QuantileDimension("liquidity", *quantiles, func(o explore.Obs) float64 { return o.Turnover }),
		explore.QuantileDimension("own volatility", *quantiles, func(o explore.Obs) float64 { return o.Vol }),
		explore.QuantileDimension("price level", *quantiles, func(o explore.Obs) float64 { return o.Price }),
		explore.CategoryDimension("sector", func(o explore.Obs) string { return o.Sector }),
		explore.BooleanDimension("own trend", "above 200DMA", "below 200DMA",
			func(o explore.Obs) bool { return o.AboveSMA }),
		explore.DayDimension("market breadth", explore.BreadthLabel, explore.BreadthOrder),
		explore.DayDimension("year", func(d explore.Day) string { return fmt.Sprintf("%d", d.Date.Year()) }, nil),
	}

	res := explore.RunEvent(days, dims, "COI (full variant)", *horizon)
	fmt.Println("EXPLORATION — the COI pattern, sliced. LEDGER rows 11-13 already rejected it;")
	fmt.Println("this asks whether any slice of the universe hides an exception.")
	fmt.Println()
	fmt.Println("Every indicator the screener uses is computed for EVERY liquid name, not just")
	fmt.Println("the ones that fired, so each bucket holds the pattern's setups AND every other")
	fmt.Println("name in the same indicator state that day. 'edge' is what the pattern added")
	fmt.Println("over them; 'held' is how many setups fired per bucket-day.")
	printResult(res, mcaps, sectors, true)
}

// shift moves a signed quantity away from zero so QuantileDimension, which
// drops non-positive values as missing, can rank it. Slopes are signed and
// their sign is the interesting part.
func shift(v float64) float64 {
	if math.IsNaN(v) {
		return math.NaN()
	}
	return v + 1e6
}

func buildCOI(cache string, horizon int, from, to time.Time, minTurnover float64,
	sectors map[string]string) []explore.Day {

	byDate := map[time.Time][]explore.Obs{}
	var mu sync.Mutex
	ids := map[string]int32{}
	params := coi.DefaultParams()
	var fired int

	fatalIf(bars.ScanParallel(cache, 0, func(ser bars.Series) {
		n := len(ser.Bars)
		if n < trendWin+horizon+2 {
			return
		}
		ind := coi.Compute(ser.Bars, params)
		setups := coi.Detect(ser, ind, params)
		confirmDay := make(map[int]bool, len(setups))
		for _, s := range setups {
			confirmDay[s.Confirm] = true
		}

		times := make([]time.Time, n)
		closes := make([]float64, n)
		opens := make([]float64, n)
		for i, b := range ser.Bars {
			times[i], closes[i], opens[i] = b.Date, b.Close, b.Open
		}
		prices := core.New(times, closes)
		vol := core.PriceUnitVol(prices, volSpan, volMin)
		sma := core.SMA(prices, trendWin).Values
		turnover := bars.MedianTurnover(ser.Bars, turnoverWin)
		sector := sectors[ser.Symbol]

		var local []struct {
			d time.Time
			o explore.Obs
		}
		var localFired int
		for i := 0; i < n-horizon-1; i++ {
			d := times[i]
			if d.Before(from) || d.After(to) {
				continue
			}
			if math.IsNaN(turnover[i]) || turnover[i] < minTurnover {
				continue
			}
			entry, exit := opens[i+1], opens[i+1+horizon]
			if entry <= 0 || exit <= 0 || math.IsNaN(sma[i]) || math.IsNaN(ind.BBLower[i]) ||
				math.IsNaN(ind.MACDD1[i]) || math.IsNaN(ind.BBSlopeDiff[i]) {
				continue
			}
			b := ser.Bars[i]
			var extra [8]float64
			extra[xMACDD1] = ind.MACDD1[i]
			extra[xEMALongD1] = ind.EMALongD1[i]
			extra[xBBSlopeDiff] = ind.BBSlopeDiff[i]
			extra[xBandWidth] = (ind.BBUpper[i] - ind.BBLower[i]) / closes[i]
			extra[xMACDvsSignal] = ind.MACD[i] - ind.Signal[i]
			extra[xLowerBand] = boolTo(touchesLowerBand(b, ind.BBLower[i]))
			extra[xBBUpperD1] = ind.BBUpperD1[i]
			extra[xDropFromHigh] = dropFromHigh(ser.Bars, i, coi.PriorHighLookback)

			if confirmDay[i] {
				localFired++
			}
			local = append(local, struct {
				d time.Time
				o explore.Obs
			}{d, explore.Obs{
				FwdRet:   exit/entry - 1,
				Selected: confirmDay[i],
				Turnover: turnover[i],
				Vol:      vol.Values[i] / closes[i],
				Price:    closes[i],
				Sector:   sector,
				AboveSMA: closes[i] > sma[i],
				Extra:    extra,
			}})
		}
		if len(local) == 0 {
			return
		}
		mu.Lock()
		fired += localFired
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

	fmt.Printf("%d COI confirmations across %d symbols\n\n", fired, len(ids))
	days := make([]explore.Day, 0, len(byDate))
	for d, obs := range byDate {
		days = append(days, explore.Day{Date: d, Obs: obs})
	}
	return days
}

// touchesLowerBand is the screener's own "important" test: the bar's range or
// its body straddles the lower Bollinger band.
func touchesLowerBand(b bars.Bar, lower float64) bool {
	if math.IsNaN(lower) {
		return false
	}
	if b.Low <= lower && lower <= b.High {
		return true
	}
	bodyLow, bodyHigh := math.Min(b.Open, b.Close), math.Max(b.Open, b.Close)
	return bodyLow <= lower && lower <= bodyHigh
}

func dropFromHigh(b []bars.Bar, i, lookback int) float64 {
	start := i - lookback
	if start < 0 {
		start = 0
	}
	var high float64
	for j := start; j <= i; j++ {
		if b[j].High > high {
			high = b[j].High
		}
	}
	if high <= 0 {
		return math.NaN()
	}
	return 100 * (high - b[i].Low) / high
}
