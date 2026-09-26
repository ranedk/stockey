package costs

import (
	"math"
	"testing"
)

func TestStatutoryMatchesMeasurement(t *testing.T) {
	// LEDGER row 35 reports 22.27 bps a round trip, before the DP charge.
	if got := 1e4 * StatutoryRoundTrip(); math.Abs(got-22.27) > 0.01 {
		t.Fatalf("statutory round trip = %.3f bps, want 22.27", got)
	}
	// Buying and selling the same value reproduces it, plus the DP charge.
	v := 100000.0
	rt := (Buy(v) + Sell(v) - DPCharge) / v
	if math.Abs(rt-StatutoryRoundTrip()) > 1e-12 {
		t.Fatalf("Buy+Sell = %.6f, want %.6f", rt, StatutoryRoundTrip())
	}
}

func TestDPCharge(t *testing.T) {
	if math.Abs(DPCharge-14.75) > 1e-9 {
		t.Fatalf("DP = %v, want 14.75", DPCharge)
	}
	if got := 1e4 * DPFraction(6000); math.Abs(got-24.58) > 0.01 {
		t.Fatalf("DP on Rs 6,000 = %.2f bps, want 24.58", got)
	}
	if Sell(0) != 0 {
		t.Fatal("no sale, no DP charge")
	}
}

func TestImpactSquareRoot(t *testing.T) {
	// 1% of a day's value at 2% daily vol: 2% × 0.1 = 20 bps.
	if got := Impact(1e5, 1e7, 0.02) / 1e5; math.Abs(got-0.002) > 1e-12 {
		t.Fatalf("impact = %v, want 0.002", got)
	}
	// Four times the size, twice the rate.
	if r := (Impact(4e5, 1e7, 0.02) / 4e5) / (Impact(1e5, 1e7, 0.02) / 1e5); math.Abs(r-2) > 1e-9 {
		t.Fatalf("impact ratio = %v, want 2", r)
	}
	if Impact(1e5, 0, 0.02) != 0 || Impact(1e5, 1e7, math.NaN()) != 0 {
		t.Fatal("undefined inputs must cost nothing rather than NaN")
	}
}
