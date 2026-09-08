package paper

import (
	"math"
	"testing"
	"time"
)

func spec() Spec {
	s := FrozenSpec()
	s.Start = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	s.RebalanceEvery = 3
	s.Quantile = 2 // hold half, so a 10-name fixture holds 5
	return s
}

// mkDays builds n days of m symbols. Symbol i has forecast i (so the top half
// is always the same names) and a constant daily return of `ret`.
func mkDays(n, m int, ret float64) []Day {
	start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	price := make([]float64, m)
	for i := range price {
		price[i] = 100
	}
	var days []Day
	for d := 0; d < n; d++ {
		day := Day{Date: start.AddDate(0, 0, d)}
		for i := 0; i < m; i++ {
			prev := price[i]
			open := prev
			close := prev * (1 + ret)
			price[i] = close
			day.Obs = append(day.Obs, Obs{
				Symbol: string(rune('A' + i)), Open: open, Close: close, PrevClose: prev,
				Forecast: float64(i), Turnover: 1e9, Eligible: true,
			})
		}
		days = append(days, day)
	}
	return days
}

func TestFirstDayBuysTheBookAndChargesForIt(t *testing.T) {
	tr, err := Compute(spec(), mkDays(1, 10, 0))
	if err != nil {
		t.Fatal(err)
	}
	b := tr.Books[BookStrategy]
	if got := b.NAV[0].Turnover; math.Abs(got-1) > 1e-9 {
		t.Errorf("day-one turnover %.4f, want 1.0 — a new book buys everything", got)
	}
	want := spec().CostBpsRoundTrip / 2 / 10000
	if got := b.NAV[0].Cost; math.Abs(got-want) > 1e-12 {
		t.Errorf("day-one cost %.6f, want %.6f (half of a round trip on 100%% turnover)", got, want)
	}
	if len(b.Holdings) != 5 {
		t.Errorf("held %d names, want the top half of ten", len(b.Holdings))
	}
	if len(b.Orders) != 5 {
		t.Errorf("emitted %d orders, want 5", len(b.Orders))
	}
}

func TestHoldDaysTradeNothing(t *testing.T) {
	tr, err := Compute(spec(), mkDays(3, 10, 0.01))
	if err != nil {
		t.Fatal(err)
	}
	b := tr.Books[BookStrategy]
	for i := 1; i < len(b.NAV); i++ {
		if b.NAV[i].Turnover != 0 || b.NAV[i].Cost != 0 {
			t.Errorf("day %d traded on a hold day: turnover %.4f cost %.6f",
				i, b.NAV[i].Turnover, b.NAV[i].Cost)
		}
		if b.NAV[i].Rebalanced {
			t.Errorf("day %d marked as a rebalance; the schedule is every 3 days", i)
		}
	}
}

func TestNavCompoundsTheHoldingsReturn(t *testing.T) {
	// Every name gains 1% a day, so the book must too, less the entry cost.
	tr, err := Compute(spec(), mkDays(4, 10, 0.01))
	if err != nil {
		t.Fatal(err)
	}
	b := tr.Books[BookStrategy]
	entryCost := 1 - spec().CostBpsRoundTrip/2/10000
	want := 100 * entryCost * math.Pow(1.01, 4)
	if got := b.NAV[len(b.NAV)-1].NAV; math.Abs(got-want) > 1e-6 {
		t.Errorf("NAV %.6f, want %.6f", got, want)
	}
}

func TestEqualWeightHoldsEverythingAndTheRandomBookMatchesTheStrategysSize(t *testing.T) {
	tr, err := Compute(spec(), mkDays(1, 10, 0))
	if err != nil {
		t.Fatal(err)
	}
	if n := len(tr.Books[BookEqual].Holdings); n != 10 {
		t.Errorf("equal-weight holds %d, want the whole eligible universe of 10", n)
	}
	strat := len(tr.Books[BookStrategy].Holdings)
	if n := len(tr.Books[BookRandom].Holdings); n != strat {
		t.Errorf("random book holds %d against the strategy's %d — the control must be the same size", n, strat)
	}
}

func TestRandomBookIsDeterministic(t *testing.T) {
	a, _ := Compute(spec(), mkDays(2, 12, 0.005))
	b, _ := Compute(spec(), mkDays(2, 12, 0.005))
	for sym, w := range a.Books[BookRandom].Holdings {
		if math.Abs(b.Books[BookRandom].Holdings[sym]-w) > 1e-12 {
			t.Fatalf("random book differs between runs at %s — the control must be reproducible", sym)
		}
	}
}

func TestASymbolThatDoesNotTradeIsHeldNotSold(t *testing.T) {
	days := mkDays(2, 10, 0)
	// On day two, the top-forecast name has no bar at all.
	var kept []Obs
	for _, o := range days[1].Obs {
		if o.Symbol != "J" { // 'A'+9, the highest forecast
			kept = append(kept, o)
		}
	}
	days[1].Obs = kept

	tr, err := Compute(spec(), days)
	if err != nil {
		t.Fatal(err)
	}
	if _, held := tr.Books[BookStrategy].Holdings["J"]; !held {
		t.Error("a name that did not trade was dropped from the book; a missing bar is not a sale")
	}
}

func TestDaysToNextRebalance(t *testing.T) {
	tr, err := Compute(spec(), mkDays(4, 10, 0)) // rebalances on days 0 and 3
	if err != nil {
		t.Fatal(err)
	}
	// Four days means indices 0..3; the schedule fires on 0, 3, 6. The last
	// computed day IS a rebalance day, so the next one is three days out.
	if got := tr.DaysToNextRebalance(); got != 3 {
		t.Errorf("days to next rebalance = %d, want 3 (last index 3, next at 6)", got)
	}
}

func TestPendingSheetIsTheWholeBookOnDayOne(t *testing.T) {
	days := mkDays(1, 10, 0)
	sheet := Pending(spec(), days[0], map[string]float64{}, 1)
	if len(sheet.Orders) != 5 {
		t.Fatalf("day-one sheet has %d orders, want the 5 names of an empty book's first buy", len(sheet.Orders))
	}
	for _, o := range sheet.Orders {
		if o.Side != "BUY" || o.FromWeight != 0 {
			t.Errorf("%s is a %s from %.4f; an empty book only buys", o.Symbol, o.Side, o.FromWeight)
		}
	}
	if !sheet.Due {
		t.Error("a sheet one day from the rebalance should read as due")
	}
}

func TestPendingSheetExitsWhatLeftTheTopQuintile(t *testing.T) {
	days := mkDays(1, 10, 0)
	// Hold a name the rule no longer wants, at full weight.
	sheet := Pending(spec(), days[0], map[string]float64{"A": 1.0}, 5)
	var exits int
	for _, o := range sheet.Orders {
		if o.Side == "EXIT" && o.Symbol == "A" {
			exits++
		}
	}
	if exits != 1 {
		t.Errorf("the sheet did not exit the dropped name (%d exits)", exits)
	}
	if sheet.Due {
		t.Error("a sheet five days out must not read as due")
	}
}

func TestEntryPriceSurvivesTopUpsAndIsForgottenOnExit(t *testing.T) {
	// Six days at 1%/day with a rebalance every three: the book is topped up
	// on day four, and the entry price must still be day one's.
	days := mkDays(6, 10, 0.01)
	tr, err := Compute(spec(), days)
	if err != nil {
		t.Fatal(err)
	}
	b := tr.Books[BookStrategy]
	for sym, e := range b.Entries {
		if !e.Date.Equal(days[0].Date) {
			t.Errorf("%s records entry on %s, want day one — a top-up is not a new position",
				sym, e.Date.Format("2006-01-02"))
		}
		// Bought at day one's open (100) and marked at day six's close.
		want := days[5].Obs[0].Close/days[0].Obs[0].Open - 1
		if math.Abs(e.Return()-want) > 1e-9 {
			t.Errorf("%s return %.4f, want %.4f", sym, e.Return(), want)
		}
	}
	if len(b.Entries) != len(b.Holdings) {
		t.Errorf("%d entries against %d holdings — they must track the same names",
			len(b.Entries), len(b.Holdings))
	}
}
