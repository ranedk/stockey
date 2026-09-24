package main

import (
	"flag"
	"fmt"
	"math"
	"strings"

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
	gross := fs.Float64("gross", 0.16, "the book's return a year BEFORE costs and tax, for the after-tax table")
	book := fs.Float64("book", 1e7, "book size in rupees (the Rs 1.25 L long-term exemption is per year, not per rupee)")
	position := fs.Float64("position", 100000, "position size in rupees for the after-tax table")
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

	printAfterTax(*churn, *gross, *book, *position, dayValue, *sigma)

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

// afterTaxRow is one holding period of the after-tax table, as fractions of the book
// a year. Auction fills: the operating rule.
type afterTaxRow struct {
	cost, preTax, longTerm, tax, afterTax float64
}

func afterTax(holdDays int, churn, roundTrip, gross, book float64) afterTaxRow {
	r := afterTaxRow{cost: annualCost(holdDays, churn, roundTrip)}
	r.preTax = gross - r.cost
	r.longTerm = costs.LongTermShare(holdDays, churn)
	r.tax = costs.AnnualTax(r.preTax*book, r.longTerm) / book
	r.afterTax = r.preTax - r.tax
	return r
}

func printAfterTax(churn, gross, book, position, dayValue, sigma float64) {
	rt := costs.StatutoryRoundTrip() + costs.DPFraction(position) + 2*costs.Impact(position, dayValue, sigma)/position
	fmt.Printf("\nAfter tax — a book returning %.0f%% a year before costs, Rs %s book, %s positions, auction fills\n",
		100*gross, inrAmount(book), rupees(position))
	fmt.Printf("Short-term gains (held 12 months or less) pay %.1f%%; long-term pay %.1f%% above Rs %s a year.\n",
		100*costs.STCGRate, 100*costs.LTCGRate, inrAmount(costs.LTCGExemption))
	fmt.Println("The long-term share is a MODEL: a name is sold at each rebalance with probability -churn,")
	fmt.Println("and its gain grows with its age. Tax is charged as gains are realised; deferral is not credited.")
	fmt.Printf("  %-12s %10s %10s %10s %10s %10s\n", "hold", "costs", "pre-tax", "long-term", "tax", "after-tax")
	rows := map[int]afterTaxRow{}
	for _, h := range holdDays {
		r := afterTax(h, churn, rt, gross, book)
		rows[h] = r
		fmt.Printf("  %-12s %9.1f%% %9.1f%% %9.1f%% %9.1f%% %9.1f%%\n", fmt.Sprintf("%d day%s", h, plural(h)),
			100*r.cost, 100*r.preTax, 100*r.longTerm, 100*r.tax, 100*r.afterTax)
	}
	monthly, quarterly := rows[21], rows[63]
	fmt.Printf("\nMonthly vs quarterly, after costs and tax, at %.0f%% churn: quarterly keeps %+.2f points a year\n",
		100*churn, 100*(quarterly.afterTax-monthly.afterTax))
	fmt.Printf("(%+.2f from costs, %+.2f from tax).\n",
		100*(monthly.cost-quarterly.cost), 100*(monthly.tax-quarterly.tax))
	if math.Max(monthly.longTerm, quarterly.longTerm) < 0.05 {
		fmt.Println("At this churn both books realise almost nothing long-term: the 12.5% rate is only reached")
		fmt.Println("by holding names past a year, which is a churn question (A2), not a clock question.")
	} else {
		fmt.Printf("Long-term share of gains: %.0f%% monthly, %.0f%% quarterly -- at this churn the clock moves the tax rate too.\n",
			100*monthly.longTerm, 100*quarterly.longTerm)
	}
}

// inrAmount prints rupees in lakh or crore without rounding away the digits that
// matter here (the exemption is Rs 1.25 L, not "1L").
func inrAmount(v float64) string {
	if v >= 1e7 {
		return strings.TrimSuffix(strings.TrimSuffix(fmt.Sprintf("%.2f", v/1e7), "0"), ".0") + " cr"
	}
	return strings.TrimSuffix(strings.TrimSuffix(fmt.Sprintf("%.2f", v/1e5), "0"), ".0") + " L"
}
