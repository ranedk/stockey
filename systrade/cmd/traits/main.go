// Command traits builds the candidate trait library for the slicer and
// judges it WITHOUT forward returns (LEDGER row 27): is each trait's missing
// data random, does it persist over the 20-day holding period, and which
// traits duplicate each other.
//
//	traits              the full report
//
// Backward-looking returns enter only where a trait is defined by them —
// beta, idiosyncratic volatility, illiquidity — exactly as the slicer's own
// volatility trait already is. The single look forward is whether a name
// stops trading within a year, used for one purpose: asking whether a
// trait's gaps follow the failures.
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
	"github.com/ranedk/systrader/internal/handcraft"
	"github.com/ranedk/systrader/internal/store"
	"github.com/ranedk/systrader/internal/traits"
)

const (
	defaultCache = "data/cache/bars_daily.bin"
	turnoverWin  = 60
	betaWin      = 252 // a year of days, the published convention for beta and idiosyncratic vol
	betaMin      = 200
	highWin      = 252 // the 52-week high
	amihudWin    = 60
	amihudMin    = 40
	tradeWin     = 60
	tradeMin     = 40
	deliveryWin  = 20
	deliveryMin  = 15
	// A daily move beyond ±50% is a data error or a mis-adjusted corporate
	// action, not a price: NSE's circuit limits cap real daily moves at 20%.
	maxDailyMove = 0.5
)

// The library, current traits first. Sector, market breadth and year are not
// here: sector is a category, and breadth and year label a whole day, so none
// of the three can rank stocks against each other.
var traitNames = []string{
	"liquidity", "own volatility", "price", "own trend (0/1)",
	"beta", "idio volatility", "dist 52w high", "illiquidity", "trade size", "delivery %",
	"upper circuits 60d", "lower circuits 60d",
}

// Market cap is not in the library: our data holds no full-history source for
// it (LEDGER row 27 — large caps only before 2024), so liquidity is the size
// axis. The operator removed it from the slicer on 2026-09-11.
const firstNew = 4

// circuitWin is the window band hits are counted over: a quarter of trading.
const circuitWin = 60

func main() {
	cache := flag.String("cache", defaultCache, "bar cache file")
	from := flag.String("from", "2014-07-01", "first sample date (a year into the cache, for the 252-day traits)")
	to := flag.String("to", "2026-06-30", "last sample date")
	floor := flag.Float64("floor", 1e7, "liquidity floor, 60-bar median traded value (the slicer's Rs 1cr)")
	every := flag.Int("every", 20, "trading days between samples — the holding period persistence is read over")
	groupAt := flag.Float64("group-at", 0.7, "rank correlation at which two traits count as duplicates")
	flag.Parse()
	fromT, toT := mustDate(*from), mustDate(*to)

	ctx := context.Background()
	st, err := store.Open(ctx)
	fatalIf(err)
	defer st.Close()
	loadFrom := fromT.AddDate(0, 0, -120)
	sizes, err := st.TradeSizes(ctx, loadFrom, toT)
	fatalIf(err)
	deliv, err := st.DeliveryPercents(ctx, loadFrom, toT)
	fatalIf(err)
	circuits, err := st.CircuitHits(ctx, loadFrom, toT)
	fatalIf(err)

	// Pass 1: the market — the equal-weight mean return of the eligible
	// universe, eligibility decided the day before — and each name's last day.
	type acc struct {
		sum float64
		n   int
	}
	mkt := map[time.Time]*acc{}
	last := map[string]time.Time{}
	var mu sync.Mutex
	fatalIf(bars.ScanParallel(*cache, 0, func(ser bars.Series) {
		b := ser.Bars
		if len(b) < 2 {
			return
		}
		turn := bars.MedianTurnover(b, turnoverWin)
		local := map[time.Time]float64{}
		for i := 1; i < len(b); i++ {
			r := dailyReturn(b, i)
			if math.IsNaN(r) || math.IsNaN(turn[i-1]) || turn[i-1] < *floor {
				continue
			}
			local[day(b[i].Date)] = r
		}
		mu.Lock()
		for d, r := range local {
			a := mkt[d]
			if a == nil {
				a = &acc{}
				mkt[d] = a
			}
			a.sum += r
			a.n++
		}
		last[ser.Symbol] = day(b[len(b)-1].Date)
		mu.Unlock()
	}))
	mktRet := map[time.Time]float64{}
	var dates []time.Time
	for d, a := range mkt {
		if a.n >= 100 {
			mktRet[d] = a.sum / float64(a.n)
			dates = append(dates, d)
		}
	}
	sort.Slice(dates, func(i, j int) bool { return dates[i].Before(dates[j]) })
	var end time.Time
	for _, t := range last {
		if t.After(end) {
			end = t
		}
	}
	sampleIdx := map[time.Time]int{}
	var sampleDates []time.Time
	k := 0
	for _, d := range dates {
		if d.Before(fromT) || d.After(toT) {
			continue
		}
		if k%*every == 0 {
			sampleIdx[d] = len(sampleDates)
			sampleDates = append(sampleDates, d)
		}
		k++
	}

	// Pass 2: every trait, for every eligible name, on every sample date.
	samples := make([]traits.Sample, len(sampleDates))
	fatalIf(bars.ScanParallel(*cache, 0, func(ser bars.Series) {
		b := ser.Bars
		n := len(b)
		if n < 2 {
			return
		}
		times := make([]time.Time, n)
		closes := make([]float64, n)
		highs := make([]float64, n)
		ret := make([]float64, n)
		value := make([]float64, n)
		mk := make([]float64, n)
		for i, x := range b {
			times[i], closes[i], highs[i] = day(x.Date), x.Close, x.High
			value[i] = x.Close * x.Vol
			ret[i] = dailyReturn(b, i)
			mk[i] = math.NaN()
			if v, ok := mktRet[times[i]]; ok {
				mk[i] = v
			}
		}
		turn := bars.MedianTurnover(b, turnoverWin)
		prices := core.New(times, closes)
		pvol := core.PriceUnitVol(prices, 36, 10).Values
		sma := core.SMA(prices, 200).Values
		ts := align(times, sizes[ser.Symbol])
		dl := align(times, deliv[ser.Symbol])
		ch := align(times, circuits[ser.Symbol])
		for i := range ch {
			if math.IsNaN(ch[i]) {
				ch[i] = 0 // the band-hit file lists hits only: no row is no hit
			}
		}
		lastDay := last[ser.Symbol]

		type placed struct {
			idx int
			row traits.Row
		}
		var rows []placed
		for i := range b {
			si, ok := sampleIdx[times[i]]
			if !ok || math.IsNaN(turn[i]) || turn[i] < *floor || closes[i] <= 0 {
				continue
			}
			beta, idio, _ := traits.BetaIdio(ret, mk, i, betaWin, betaMin)
			trend := math.NaN()
			if !math.IsNaN(sma[i]) {
				trend = 0
				if closes[i] > sma[i] {
					trend = 1
				}
			}
			vals := []float64{
				turn[i],
				pvol[i] / closes[i],
				closes[i],
				trend,
				beta,
				idio,
				traits.DistFromHigh(highs, closes, i, highWin),
				traits.Amihud(ret, value, i, amihudWin, amihudMin),
				traits.Median(ts, i, tradeWin, tradeMin),
				traits.Median(dl, i, deliveryWin, deliveryMin),
				traits.Count(ch, 1, i, circuitWin),
				traits.Count(ch, -1, i, circuitWin),
			}
			delists := lastDay.Before(times[i].AddDate(1, 0, 0)) && lastDay.Before(end.AddDate(0, 0, -30))
			rows = append(rows, placed{si, traits.Row{Sym: ser.Symbol, Delists: delists, Values: vals}})
		}
		mu.Lock()
		for _, r := range rows {
			samples[r.idx] = append(samples[r.idx], r.row)
		}
		mu.Unlock()
	}))

	report(samples, sampleDates, *floor, *every, *groupAt)
}

func report(samples []traits.Sample, dates []time.Time, floor float64, every int, groupAt float64) {
	k := len(traitNames)
	all, dl, sv := traits.MissingShare(samples, k)
	pers := traits.Persistence(samples, k)
	ov := traits.Overlap(samples, k)
	var rows, delisting int
	for _, s := range samples {
		rows += len(s)
		for _, r := range s {
			if r.Delists {
				delisting++
			}
		}
	}
	fmt.Printf("TRAIT LIBRARY — returns-free characterisation (trials=0; LEDGER rows 27-28)\n")
	fmt.Printf("NSE adjusted EQ, Rs %.0f cr floor, %d sample dates every %d trading days, %s..%s\n",
		floor/1e7, len(dates), every, dates[0].Format("2006-01-02"), dates[len(dates)-1].Format("2006-01-02"))
	fmt.Printf("%d stock-dates, %.0f names per date; %d stock-dates (%.1f%%) are names that stop trading within a year\n\n",
		rows, float64(rows)/float64(len(samples)), delisting, 100*float64(delisting)/float64(rows))

	fmt.Println("  + = new candidate")
	fmt.Printf("  %-18s %9s %26s %13s\n", "trait", "coverage", "missing: stop trading / rest", "persists 20d")
	for j, name := range traitNames {
		mark := " "
		if j >= firstNew {
			mark = "+"
		}
		flag := ""
		if math.Abs(dl[j]-sv[j]) > 0.10 {
			flag = "   <- gaps are not random"
		}
		fmt.Printf("%s %-18s %8.1f%% %13.1f%% / %5.1f%% %12.2f%s\n",
			mark, name, 100*(1-all[j]), 100*dl[j], 100*sv[j], pers[j], flag)
	}

	fmt.Println("\noverlap: mean cross-sectional rank correlation")
	short := make([]string, k)
	for j, n := range traitNames {
		short[j] = abbrev(n)
	}
	fmt.Printf("  %-18s", "")
	for _, s := range short {
		fmt.Printf(" %6s", s)
	}
	fmt.Println()
	for a := 0; a < k; a++ {
		fmt.Printf("  %-18s", traitNames[a])
		for b := 0; b < k; b++ {
			if a == b {
				fmt.Printf(" %6s", "·")
			} else {
				fmt.Printf(" %6.2f", ov[a][b])
			}
		}
		fmt.Println()
	}

	fmt.Printf("\nduplicate groups at |rho| >= %.2f (complete linkage — every pair in a group clears it):\n", groupAt)
	for _, g := range handcraft.Cluster(k, func(a, b int) float64 { return ov[a][b] }, groupAt) {
		names := make([]string, len(g))
		for i, j := range g {
			names[i] = traitNames[j]
		}
		tag := ""
		if len(g) > 1 {
			tag = "   <- duplicates: keep one"
		}
		fmt.Printf("  %s%s\n", strings.Join(names, ", "), tag)
	}
	fmt.Println("\nNot yet in the library: intraday (minute-data) volatility. Its history starts 2021 and holds only")
	fmt.Println("names still listed in 2026, so it needs its own survivorship-aware design before it can be judged.")
}

func abbrev(s string) string {
	m := map[string]string{
		"liquidity": "liq", "own volatility": "vol", "price": "price",
		"own trend (0/1)": "trend", "beta": "beta", "idio volatility": "ivol", "dist 52w high": "52wh",
		"illiquidity": "amih", "trade size": "trsz", "delivery %": "deliv",
		"upper circuits 60d": "upC", "lower circuits 60d": "loC",
	}
	return m[s]
}

func dailyReturn(b []bars.Bar, i int) float64 {
	if i == 0 || b[i-1].Close <= 0 || b[i].Close <= 0 {
		return math.NaN()
	}
	r := b[i].Close/b[i-1].Close - 1
	if math.Abs(r) > maxDailyMove {
		return math.NaN()
	}
	return r
}

func align(times []time.Time, pts []store.DatedValue) []float64 {
	byDate := make(map[time.Time]float64, len(pts))
	for _, p := range pts {
		byDate[p.Date] = p.Value
	}
	out := make([]float64, len(times))
	for i, t := range times {
		out[i] = math.NaN()
		if v, ok := byDate[t]; ok {
			out[i] = v
		}
	}
	return out
}

func day(t time.Time) time.Time {
	u := t.UTC()
	return time.Date(u.Year(), u.Month(), u.Day(), 0, 0, 0, 0, time.UTC)
}

func mustDate(s string) time.Time {
	t, err := time.Parse("2006-01-02", s)
	fatalIf(err)
	return t
}

func fatalIf(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "traits:", err)
		os.Exit(1)
	}
}
