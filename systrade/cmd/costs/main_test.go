package main

import (
	"math"
	"math/rand"
	"testing"
	"time"

	"github.com/ranedk/systrader/internal/bars"
	"github.com/ranedk/systrader/internal/costs"
)

// synthBars builds one-minute bars from trades that bounce between a bid and
// an ask `spread` apart around a slowly wandering mid.
func synthBars(rng *rand.Rand, minutes, tradesPerMin int, spread, midVol float64) []bars.Bar {
	mid := 0.0
	start := time.Date(2024, 1, 1, 3, 45, 0, 0, time.UTC)
	var out []bars.Bar
	for m := 0; m < minutes; m++ {
		var o, h, l, c float64
		for k := 0; k < tradesPerMin; k++ {
			mid += rng.NormFloat64() * midVol
			side := 1.0
			if rng.Intn(2) == 0 {
				side = -1
			}
			p := math.Exp(mid + side*spread/2)
			if k == 0 {
				o, h, l = p, p, p
			}
			h, l, c = math.Max(h, p), math.Min(l, p), p
		}
		out = append(out, bars.Bar{Date: start.Add(time.Duration(m) * time.Minute), Open: o, High: h, Low: l, Close: c, Vol: 100})
	}
	return out
}

func TestAbdiRanaldoRecoversAKnownSpread(t *testing.T) {
	rng := rand.New(rand.NewSource(1))
	for _, spread := range []float64{0.0010, 0.0020, 0.0050} {
		var sum float64
		n := 0
		for day := 0; day < 400; day++ {
			s, k := arProducts(synthBars(rng, 30, 40, spread, 0.00015))
			sum += s
			n += k
		}
		est := math.Sqrt(math.Max(0, 4*sum/float64(n)))
		if math.Abs(est-spread)/spread > 0.3 {
			t.Errorf("spread %.1f bps estimated at %.1f bps", 1e4*spread, 1e4*est)
		}
	}
}

func TestVwapGapIsMeasuredFromTheAuctionPrice(t *testing.T) {
	b := []bars.Bar{{Open: 100, High: 101, Low: 99, Close: 100, Vol: 10}, {Open: 100, High: 103, Low: 101, Close: 102, Vol: 30}}
	g, ok := vwapGap(b, 2)
	// typical prices 100 and 102, weights 10 and 30 -> 101.5; against 100 -> +1.5%
	if !ok || math.Abs(g-0.015) > 1e-12 {
		t.Errorf("gap = %v, want 0.015", g)
	}
}

func TestSpeedLimitIsRoundTripsTimesCost(t *testing.T) {
	// A book replaced every 21 days trades twelve times a year; at 40 bps a
	// round trip that is 4.8% a year, whatever the signal does.
	if got := annualCost(21, 1, 0.0040); math.Abs(got-0.048) > 1e-12 {
		t.Fatalf("got %.6f, want 0.048", got)
	}
	// Churn scales it linearly: replacing two thirds costs two thirds.
	if got := annualCost(21, 2.0/3, 0.0040); math.Abs(got-0.032) > 1e-12 {
		t.Fatalf("got %.6f, want 0.032", got)
	}
	// Halving the holding period doubles the bill — the whole point of the table.
	fast, slow := annualCost(5, 1, 0.0040), annualCost(10, 1, 0.0040)
	if math.Abs(fast-2*slow) > 1e-12 {
		t.Fatalf("5-day %.6f is not twice the 10-day %.6f", fast, slow)
	}
	if !math.IsNaN(annualCost(0, 1, 0.004)) {
		t.Fatal("a zero holding period should be NaN, not a division by zero")
	}
}

// The DP charge is flat rupees, so it is the small positions it eats.
func TestDPChargeHurtsSmallSlicesMost(t *testing.T) {
	small := costs.DPFraction(6000)
	large := costs.DPFraction(300000)
	if !(small > 10*large) {
		t.Fatalf("DP charge is %.5f of a Rs 6,000 position and %.5f of a Rs 3 lakh one", small, large)
	}
}

func TestAfterTaxIsPreTaxLessTheTaxOnWhatIsLeft(t *testing.T) {
	// Churn 1 monthly: nothing survives a year, so the whole net gain pays 20%.
	r := afterTax(21, 1, 0.004, 0.16, 1e7)
	if math.Abs(r.cost-0.048) > 1e-12 || math.Abs(r.preTax-0.112) > 1e-12 {
		t.Fatalf("cost %.4f pre-tax %.4f", r.cost, r.preTax)
	}
	if r.longTerm != 0 || math.Abs(r.tax-0.2*0.112) > 1e-12 || math.Abs(r.afterTax-0.8*0.112) > 1e-12 {
		t.Fatalf("long-term %.4f tax %.4f after %.4f", r.longTerm, r.tax, r.afterTax)
	}
	// A losing year pays no tax.
	if l := afterTax(1, 1, 0.004, 0.16, 1e7); l.tax != 0 || l.afterTax != l.preTax {
		t.Fatalf("losing year taxed: %+v", l)
	}
	// Lower churn at the same clock moves gains long-term and cuts the tax rate.
	lo := afterTax(21, 0.1, 0.004, 0.16, 1e7)
	if !(lo.longTerm > 0.5 && lo.tax/lo.preTax < 0.2) {
		t.Fatalf("churn 0.1: long-term %.3f, effective rate %.3f", lo.longTerm, lo.tax/lo.preTax)
	}
}
