// Command costs measures what fills actually cost, as designed in
// research/preregistrations/2026-09-12_fill_costs.md: statutory charges (facts),
// the spread (estimated from 1-minute bars), where a fill lands (measured
// against the opening auction) and market impact (a labelled model), per
// liquidity tier and position size, against the flat 50 bps every backtest
// assumed.
//
//	costs            the full report (2023-06 .. 2026-08, every 5th trading day)
//	costs speed      the speed limit: what a book costs to run at each holding
//	                 period, before and after Indian equity tax (arithmetic only —
//	                 no market data read)
//
// A measurement, not a hypothesis test: LEDGER trials=0.
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
	"github.com/ranedk/systrader/internal/costs"
	"github.com/ranedk/systrader/internal/store"
)

// Tiers by 60-trading-day median traded value, rupees (fixed in the design).
var tierEdges = []float64{1e7, 1e8, 2.5e8, 1e9, 5e9}
var tierNames = []string{"Rs 1-10 cr", "Rs 10-25 cr", "Rs 25-100 cr", "Rs 100-500 cr", "Rs 500 cr+"}

// Position sizes, rupees: the combination at Rs 30 lakh, a quintile book, a
// 150-name book, 30 names, 10 names.
var positions = []float64{6000, 12500, 20000, 100000, 300000}

const (
	windowBars  = 30 // 09:15-09:44 IST
	minBars     = 20
	vwapBars    = 15 // 09:15-09:29
	lookbackDay = 60
)

func tierOf(v float64) int {
	for i := len(tierEdges) - 1; i >= 0; i-- {
		if v >= tierEdges[i] {
			return i
		}
	}
	return -1
}

type tierAcc struct {
	nameDays, thin   int
	prodSum          float64 // Abdi-Ranaldo products, pooled
	prodN            int
	perDay           []float64 // per name-day spread estimates, floored at 0
	gapSigned, gapAb []float64 // post-open VWAP vs auction, fraction
	dayValue         []float64
	firstMinValue    []float64
	sigma            []float64
}

func main() {
	if len(os.Args) > 1 && os.Args[1] == "speed" {
		runSpeed(os.Args[2:])
		return
	}
	from := flag.String("from", "2023-06-01", "first sampled day")
	to := flag.String("to", "2026-08-31", "last sampled day")
	every := flag.Int("every", 5, "sample every Nth trading day")
	flag.Parse()
	fromT, toT := mustDate(*from), mustDate(*to)

	ctx := context.Background()
	st, err := store.Open(ctx)
	fatalIf(err)
	defer st.Close()
	loadFrom := fromT.AddDate(0, 0, -120)
	values, err := st.TradedValues(ctx, loadFrom, toT)
	fatalIf(err)
	closes, err := st.RawCloses(ctx, loadFrom, toT)
	fatalIf(err)

	// Trading days from the bhavcopy itself.
	daySet := map[time.Time]bool{}
	for _, pts := range values {
		for _, p := range pts {
			daySet[p.Date] = true
		}
	}
	var days []time.Time
	for d := range daySet {
		days = append(days, d)
	}
	sort.Slice(days, func(i, j int) bool { return days[i].Before(days[j]) })
	var sample []time.Time
	k := 0
	for _, d := range days {
		if d.Before(fromT) || d.After(toT) {
			continue
		}
		if k%*every == 0 {
			sample = append(sample, d)
		}
		k++
	}

	acc := make([]*tierAcc, len(tierNames))
	for i := range acc {
		acc[i] = &tierAcc{}
	}
	for si, d := range sample {
		open := time.Date(d.Year(), d.Month(), d.Day(), 3, 45, 0, 0, time.UTC) // 09:15 IST
		mb, err := st.IntradayBars(ctx, open, open.Add(windowBars*time.Minute))
		fatalIf(err)
		for sym, pts := range values {
			medVal, today, ok := trailingMedian(pts, d, lookbackDay)
			if !ok {
				continue
			}
			t := tierOf(medVal)
			if t < 0 {
				continue
			}
			a := acc[t]
			a.nameDays++
			b := mb[sym]
			if len(b) < minBars || !b[0].Date.Equal(open) {
				a.thin++
				continue
			}
			a.dayValue = append(a.dayValue, today)
			a.firstMinValue = append(a.firstMinValue, typical(b[0])*b[0].Vol)
			if sig, ok := dailySigma(closes[sym], d, lookbackDay); ok {
				a.sigma = append(a.sigma, sig)
			}
			sum, n := arProducts(b)
			a.prodSum += sum
			a.prodN += n
			if n > 0 {
				a.perDay = append(a.perDay, math.Sqrt(math.Max(0, 4*sum/float64(n))))
			}
			if g, ok := vwapGap(b, vwapBars); ok {
				a.gapSigned = append(a.gapSigned, g)
				a.gapAb = append(a.gapAb, math.Abs(g))
			}
		}
		if (si+1)%20 == 0 {
			fmt.Fprintf(os.Stderr, "%d/%d days sampled\n", si+1, len(sample))
		}
	}
	report(acc, sample)
}

func report(acc []*tierAcc, sample []time.Time) {
	stat := costs.StatutoryRoundTrip()
	fmt.Println("WHAT FILLS ACTUALLY COST — research/preregistrations/2026-09-12_fill_costs.md (LEDGER trials=0)")
	fmt.Printf("%d sampled days, %s..%s, first half-hour (09:15-09:44 IST) of 1-minute bars\n\n",
		len(sample), sample[0].Format("2006-01-02"), sample[len(sample)-1].Format("2006-01-02"))
	fmt.Printf("statutory charges, equity delivery round trip: %.2f bps (STT 20.00, stamp 1.50, exchange 0.61, SEBI+IPFT 0.04, GST %.2f)\n",
		1e4*stat, 1e4*costs.GSTOnCharges())
	fmt.Println("plus the DP charge on every sale: Rs 14.75 per stock, fixed in rupees")
	fmt.Println()
	fmt.Printf("  %-14s %9s %7s %11s %13s %10s %10s %10s %10s %8s\n", "tier (60d med)", "name-days", "thin", "day value", "1st-min value",
		"spread", "spread", "gap after", "|gap|", "daily σ")
	fmt.Printf("  %-14s %9s %7s %11s %13s %10s %10s %10s %10s %8s\n", "", "", "", "median", "median", "pooled", "median", "open, mean", "median", "median")
	type tierMed struct{ spread, sigma, value float64 }
	meds := make([]tierMed, len(acc))
	for i, a := range acc {
		pooled := math.NaN()
		if a.prodN > 0 {
			pooled = math.Sqrt(math.Max(0, 4*a.prodSum/float64(a.prodN)))
		}
		meds[i] = tierMed{spread: pooled, sigma: median(a.sigma), value: median(a.dayValue)}
		fmt.Printf("  %-14s %9d %6.0f%% %8.1f cr %10.1f lakh %8.1f bp %8.1f bp %+8.1f bp %8.1f bp %7.2f%%\n",
			tierNames[i], a.nameDays, 100*float64(a.thin)/math.Max(1, float64(a.nameDays)),
			median(a.dayValue)/1e7, median(a.firstMinValue)/1e5, 1e4*pooled, 1e4*median(a.perDay),
			1e4*mean(a.gapSigned), 1e4*median(a.gapAb), 100*median(a.sigma))
	}
	fmt.Println("\n'thin' = no 09:15 bar or fewer than 20 of the first 30 minutes traded. 'gap after open' = where the")
	fmt.Println("09:15-09:29 volume-weighted price sat against the auction price: an order placed after the open lands")
	fmt.Println("there instead, and pays the spread; an auction order pays neither.")

	fmt.Println("\nROUND TRIP, basis points, for a median name in each tier — against the 50 bps every backtest assumed")
	fmt.Printf("  %-14s", "position")
	for _, q := range positions {
		fmt.Printf(" %17s", fmt.Sprintf("Rs %s", rupees(q)))
	}
	fmt.Println()
	for i := range acc {
		for _, mode := range []string{"auction", "after open"} {
			fmt.Printf("  %-14s", map[bool]string{true: tierNames[i], false: ""}[mode == "auction"])
			for _, q := range positions {
				c := stat + costs.DPFraction(q) + 2*costs.ImpactY*meds[i].sigma*math.Sqrt(q/meds[i].value)
				if mode == "after open" {
					c += meds[i].spread
				}
				fmt.Printf(" %11.1f %-5s", 1e4*c, abbrevMode(mode))
			}
			fmt.Println()
		}
	}
	fmt.Println("\nauction = statutory + DP + modelled impact (square-root law, Y=1, both legs); after open = the same plus")
	fmt.Println("the pooled spread. Impact is a MODEL; statutory and DP are facts; the spread is estimated from bars.")
}

func abbrevMode(m string) string {
	if m == "auction" {
		return "auct"
	}
	return "open"
}

func rupees(q float64) string {
	if q >= 1e5 {
		return fmt.Sprintf("%.0fL", q/1e5)
	}
	return fmt.Sprintf("%.1fk", q/1e3)
}

// arProducts returns the sum and count of the Abdi-Ranaldo products
// (c_t − η_t)(c_t − η_{t+1}) over consecutive bars, in logs.
func arProducts(b []bars.Bar) (float64, int) {
	var sum float64
	n := 0
	for t := 0; t+1 < len(b); t++ {
		if b[t].High <= 0 || b[t].Low <= 0 || b[t].Close <= 0 || b[t+1].High <= 0 || b[t+1].Low <= 0 {
			continue
		}
		c := math.Log(b[t].Close)
		eta := (math.Log(b[t].High) + math.Log(b[t].Low)) / 2
		eta1 := (math.Log(b[t+1].High) + math.Log(b[t+1].Low)) / 2
		sum += (c - eta) * (c - eta1)
		n++
	}
	return sum, n
}

// vwapGap is the first n bars' volume-weighted price against the auction
// price (the first bar's open), as a fraction.
func vwapGap(b []bars.Bar, n int) (float64, bool) {
	if len(b) < n || b[0].Open <= 0 {
		return 0, false
	}
	var pv, v float64
	for _, x := range b[:n] {
		pv += typical(x) * x.Vol
		v += x.Vol
	}
	if v <= 0 {
		return 0, false
	}
	return pv/v/b[0].Open - 1, true
}

func typical(b bars.Bar) float64 { return (b.High + b.Low + b.Close) / 3 }

// trailingMedian is the median traded value over the n trading days before d,
// and d's own value.
func trailingMedian(pts []store.DatedValue, d time.Time, n int) (float64, float64, bool) {
	i := sort.Search(len(pts), func(i int) bool { return !pts[i].Date.Before(d) })
	if i >= len(pts) || !pts[i].Date.Equal(d) || i < n {
		return 0, 0, false
	}
	w := make([]float64, n)
	for j := 0; j < n; j++ {
		w[j] = pts[i-n+j].Value
	}
	return median(w), pts[i].Value, true
}

// dailySigma is the standard deviation of raw daily close-to-close returns
// over the n days before d, leaving out moves beyond ±20% — raw closes carry
// splits and bonuses, which are not risk.
func dailySigma(pts []store.DatedValue, d time.Time, n int) (float64, bool) {
	i := sort.Search(len(pts), func(i int) bool { return !pts[i].Date.Before(d) })
	if i < n+1 {
		return 0, false
	}
	var r []float64
	for j := i - n; j < i; j++ {
		x := pts[j].Value/pts[j-1].Value - 1
		if math.Abs(x) <= 0.2 {
			r = append(r, x)
		}
	}
	if len(r) < n/2 {
		return 0, false
	}
	m := mean(r)
	var ss float64
	for _, x := range r {
		ss += (x - m) * (x - m)
	}
	return math.Sqrt(ss / float64(len(r)-1)), true
}

func mean(x []float64) float64 {
	if len(x) == 0 {
		return math.NaN()
	}
	var s float64
	for _, v := range x {
		s += v
	}
	return s / float64(len(x))
}

func median(x []float64) float64 {
	if len(x) == 0 {
		return math.NaN()
	}
	s := append([]float64(nil), x...)
	sort.Float64s(s)
	m := len(s) / 2
	if len(s)%2 == 1 {
		return s[m]
	}
	return (s[m-1] + s[m]) / 2
}

func mustDate(s string) time.Time {
	t, err := time.Parse("2006-01-02", s)
	fatalIf(err)
	return t
}

func fatalIf(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "costs:", err)
		os.Exit(1)
	}
}
