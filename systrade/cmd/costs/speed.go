package main

import (
	"flag"
	"fmt"
	"math"

	"github.com/ranedk/systrader/internal/costs"
)

// `costs speed` — the speed limit (Bible Law 13), as arithmetic.
//
// Before hunting for a faster book, price what speed costs. A book that
// replaces itself every H trading days trades 252/H times a year, and each
// round trip pays Dhan's statutory charges, the flat DP charge on every sale,
// modelled impact, and — unless the order goes into the opening auction — the
// spread. Multiply and the answer is what the idea must EARN before it has
// earned anything.
//
// This reads no market data. The charges are Dhan's published schedule, the
// spreads are LEDGER row 35's measurements, and impact is that row's model
// (square-root law, Y=1) at a declared daily volatility. No ledger row: it is
// a calculator, not a trial.

// spreadBps is the round-trip cost of NOT filling in the opening auction, by
// liquidity tier, measured in LEDGER row 35 (research/reports/2026-09-12_fill_costs.txt).
var spreadBps = []float64{16.5, 9.6, 5.6, 3.7, 4.2}

// holdDays are the holding periods the table prices, from a one-day book to a
// quarterly one. The five live paper tracks all sit at 21.
var holdDays = []int{1, 2, 3, 5, 10, 21, 42, 63}

func runSpeed(args []string) {
	fs := flag.NewFlagSet("speed", flag.ExitOnError)
	churn := fs.Float64("churn", 1.0, "share of the book replaced each period (1.0 = a fresh book every time)")
	sigma := fs.Float64("sigma", 0.02, "daily volatility used by the impact model")
	tier := fs.Int("tier", 1, "liquidity tier for spread and impact: 0=Rs 1-10 cr, 1=10-25, 2=25-100, 3=100-500, 4=500+")
	fatalIf(fs.Parse(args))
	if *tier < 0 || *tier >= len(tierNames) {
		fatalIf(fmt.Errorf("tier %d is out of range (0-%d)", *tier, len(tierNames)-1))
	}
	// A representative day's traded value for the tier: its lower edge, the
	// conservative end of the band.
	dayValue := tierEdges[*tier]

	fmt.Printf("THE SPEED LIMIT — what a book must earn before it has earned anything\n\n")
	fmt.Printf("Tier %s, day value Rs %.1f cr, daily volatility %.1f%%, %.0f%% of the book replaced each period.\n",
		tierNames[*tier], dayValue/1e7, 100**sigma, 100**churn)
	fmt.Println("Charges are Dhan's published schedule; the spread is LEDGER row 35's measurement; impact is")
	fmt.Println("that row's square-root model. Every number below is a cost, so every number is a hurdle.")
	fmt.Printf("\nOne round trip, by position size (statutory %.1f bps + DP charge + impact):\n", 1e4*costs.StatutoryRoundTrip())
	fmt.Printf("  %-12s", "")
	for _, q := range positions {
		fmt.Printf(" %10s", rupees(q))
	}
	fmt.Println()
	rt := make([]float64, len(positions))
	for i, q := range positions {
		imp := 2 * costs.Impact(q, dayValue, *sigma) / q // both sides
		rt[i] = costs.StatutoryRoundTrip() + costs.DPFraction(q) + imp
	}
	fmt.Printf("  %-12s", "auction fill")
	for _, v := range rt {
		fmt.Printf(" %9.1f ", 1e4*v)
	}
	fmt.Println("bps")
	fmt.Printf("  %-12s", "after open")
	for _, v := range rt {
		fmt.Printf(" %9.1f ", 1e4*(v+spreadBps[*tier]/1e4))
	}
	fmt.Println("bps")

	for _, afterOpen := range []bool{false, true} {
		label := "auction fills (our operating rule, row 35)"
		if afterOpen {
			label = "fills after the open (the spread added)"
		}
		fmt.Printf("\nAnnual cost of running the book — %s\n", label)
		fmt.Printf("  %-12s %10s", "hold", "trips/yr")
		for _, q := range positions {
			fmt.Printf(" %10s", rupees(q))
		}
		fmt.Println()
		for _, h := range holdDays {
			fmt.Printf("  %-12s %10.1f", fmt.Sprintf("%d day%s", h, plural(h)), 252/float64(h)**churn)
			for i := range positions {
				c := rt[i]
				if afterOpen {
					c += spreadBps[*tier] / 1e4
				}
				fmt.Printf(" %9.1f%%", 100*annualCost(h, *churn, c))
			}
			fmt.Println()
		}
	}

	fmt.Println("\nHow to read it. The five live tracks hold 21 days and replace about two thirds of the")
	fmt.Println("book each time, which is the 21-day row scaled by -churn: a cost in the low single")
	fmt.Println("digits a year, against books that returned 13-19%. A swing book held a week is the")
	fmt.Println("5-day row, and it starts every year that many points behind. Law 13's question is not")
	fmt.Println("whether a fast signal predicts — it is whether it predicts by MORE than this table.")
	fmt.Println("\nTwo levers move these numbers, and neither is the signal: bigger positions (the DP")
	fmt.Println("charge is flat, so it hurts small slices most) and auction fills (which pay no spread).")
}

func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}

// annualCost is the table's whole arithmetic: round trips a year times what a
// round trip costs.
func annualCost(holdDays int, churn, roundTrip float64) float64 {
	if holdDays <= 0 {
		return math.NaN()
	}
	return 252 / float64(holdDays) * churn * roundTrip
}
