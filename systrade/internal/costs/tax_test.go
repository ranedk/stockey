package costs

import (
	"math"
	"testing"
)

func TestLongTermShareMatchesTheGeometricLifeBySimulation(t *testing.T) {
	// Closed form against a direct sum over the holding-life distribution.
	for _, tc := range []struct {
		hold  int
		churn float64
	}{{21, 2.0 / 3}, {21, 0.2}, {63, 2.0 / 3}, {63, 0.25}, {5, 0.1}} {
		q := 1 - tc.churn
		var all, long float64
		for k := 1; k < 20000; k++ {
			w := tc.churn * math.Pow(q, float64(k-1)) * float64(k) // P(sold at k) x gain ~ age
			all += w
			if k*tc.hold > YearDays {
				long += w
			}
		}
		if got, want := LongTermShare(tc.hold, tc.churn), long/all; math.Abs(got-want) > 1e-9 {
			t.Errorf("hold %d churn %.3f: closed form %.9f, sum %.9f", tc.hold, tc.churn, got, want)
		}
	}
}

func TestLongTermShareEdges(t *testing.T) {
	// A full replacement every period never survives a year.
	if s := LongTermShare(21, 1); s != 0 {
		t.Errorf("monthly, churn 1: %v, want 0", s)
	}
	// Exactly 12 months (21 x 12 = 252 days) is NOT long-term; one more rebalance is.
	if s := LongTermShare(252, 1); s != 0 {
		t.Errorf("held exactly a year: %v, want 0", s)
	}
	if s := LongTermShare(253, 1); s != 1 {
		t.Errorf("held a year and a day: %v, want 1", s)
	}
	if !math.IsNaN(LongTermShare(0, 0.5)) || !math.IsNaN(LongTermShare(21, 0)) {
		t.Error("invalid inputs should be NaN")
	}
}

func TestAnnualTax(t *testing.T) {
	// All short-term: 20% of the gain.
	if got := AnnualTax(1e6, 0); math.Abs(got-200000) > 1e-6 {
		t.Errorf("short-term: %v", got)
	}
	// All long-term: 12.5% above the Rs 1.25 L exemption.
	if got := AnnualTax(1e6, 1); math.Abs(got-0.125*875000) > 1e-6 {
		t.Errorf("long-term: %v", got)
	}
	// A long-term gain inside the exemption pays nothing.
	if got := AnnualTax(100000, 1); got != 0 {
		t.Errorf("inside exemption: %v", got)
	}
	if AnnualTax(-5e5, 0) != 0 {
		t.Error("a loss pays no tax")
	}
}
