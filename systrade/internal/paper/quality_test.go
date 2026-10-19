package paper

import (
	"math"
	"testing"
	"time"
)

func day(s string) time.Time { t, _ := time.Parse("2006-01-02", s); return t }

func TestTrackHoldingsAnswersFromTheLastRebalanceOnOrBeforeTheDay(t *testing.T) {
	th := NewTrackHoldings([]HoldingRow{
		{day("2026-10-02"), "A"}, {day("2026-10-02"), "B"},
		{day("2026-11-02"), "B"}, {day("2026-11-02"), "C"},
	})
	cases := []struct {
		sym  string
		on   string
		want bool
	}{
		{"A", "2026-10-01", false}, // before the first rebalance
		{"A", "2026-10-02", true},
		{"A", "2026-11-01", true},
		{"A", "2026-11-02", false}, // dropped at the November rebalance
		{"C", "2026-11-03", true},
	}
	for _, c := range cases {
		if got := th.Held(c.sym, day(c.on)); got != c.want {
			t.Errorf("Held(%s, %s) = %v, want %v", c.sym, c.on, got, c.want)
		}
	}
}

func TestHoldAllVariantHoldsEveryDefinedNameAtItsHalf(t *testing.T) {
	spec := MomentumQualityCombinationSpec()
	nan := math.NaN()
	var obs []Obs
	// 10 momentum-ranked names; Q1..Q3 are the quality track's (signal 1), the rest NaN for it
	for i := 0; i < 10; i++ {
		sym := string(rune('a' + i))
		q := nan
		if i < 3 {
			sym = "Q" + string(rune('1'+i))
			q = 1
		}
		m := float64(i)
		obs = append(obs, Obs{Symbol: sym, Eligible: true, Signals: []float64{m, m, m, m, q}})
	}
	tops := variantTopsBuffered(obs, spec, nil)
	if len(tops[4]) != 3 {
		t.Fatalf("quality book = %v, want all 3 held names", tops[4])
	}
	w := weightsFromTops(tops, spec)
	var qualityShare float64
	for _, s := range tops[4] {
		qualityShare += spec.VariantWeights[4] / float64(len(tops[4]))
		if w[s] < 0.5/3-1e-9 {
			t.Errorf("%s weight %.4f below its quality share", s, w[s])
		}
	}
	if math.Abs(qualityShare-0.5) > 1e-9 {
		t.Fatalf("quality half = %.3f", qualityShare)
	}
	var total float64
	for _, x := range w {
		total += x
	}
	if math.Abs(total-1) > 1e-9 {
		t.Fatalf("weights sum to %.4f", total)
	}
}
