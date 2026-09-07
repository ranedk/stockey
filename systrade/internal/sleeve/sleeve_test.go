package sleeve

import (
	"math"
	"testing"
	"time"
)

func day(n int, obs ...Obs) Day {
	return Day{Date: time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC).AddDate(0, 0, n), Obs: obs}
}

func cfg() Config { return Config{CostBpsRoundTrip: 50, Seeds: []int64{1, 2, 3}} }

func TestFirstDayReturnAndCostAreExactlyWhatWasOrdered(t *testing.T) {
	// Two names, sizing units 3 and 1 -> weights 0.75 / 0.25.
	// Gross = 0.75*10% + 0.25*(-2%) = 7.0%.
	// Turnover on day one is the full book, 1.0, so cost = 25bps.
	days := []Day{day(0,
		Obs{Sym: 1, U: []float64{3}, Ret: 0.10},
		Obs{Sym: 2, U: []float64{1}, Ret: -0.02},
	)}
	res, err := Run(days, []string{"r"}, cfg())
	if err != nil {
		t.Fatal(err)
	}
	b := res[0].Signal
	if got, want := b.Gross[0], 0.07; math.Abs(got-want) > 1e-12 {
		t.Errorf("gross = %v, want %v", got, want)
	}
	if got, want := b.Turnover[0], 1.0; math.Abs(got-want) > 1e-12 {
		t.Errorf("turnover = %v, want %v (a new book buys everything)", got, want)
	}
	if got, want := b.Net[0], 0.07-0.0025; math.Abs(got-want) > 1e-12 {
		t.Errorf("net = %v, want %v (50bps round trip = 25bps one way)", got, want)
	}
}

func TestHoldingStillCostsNothing(t *testing.T) {
	// Same weights two days running, and the drift is identical across names,
	// so the book needs no trade on day two. A turnover model that ignored
	// drift would invent a trade here.
	obs := []Obs{
		{Sym: 1, U: []float64{3}, Ret: 0.05},
		{Sym: 2, U: []float64{1}, Ret: 0.05},
	}
	days := []Day{day(0, obs...), day(1, obs...)}
	res, err := Run(days, []string{"r"}, cfg())
	if err != nil {
		t.Fatal(err)
	}
	if got := res[0].Signal.Turnover[1]; got > 1e-12 {
		t.Errorf("day-two turnover = %v, want 0 — nothing changed", got)
	}
}

func TestShuffleControlKeepsTheExposureAndLosesTheSelection(t *testing.T) {
	// Every name returns the same 4%: a control that only permutes WHICH name
	// holds which weight must earn exactly what the signal earned.
	var obs []Obs
	for i := 1; i <= 20; i++ {
		obs = append(obs, Obs{Sym: int32(i), U: []float64{float64(i)}, Ret: 0.04})
	}
	res, err := Run([]Day{day(0, obs...)}, []string{"r"}, cfg())
	if err != nil {
		t.Fatal(err)
	}
	sig, shuf := res[0].Signal.Gross[0], res[0].Shuffled.Gross[0]
	if math.Abs(sig-shuf) > 1e-12 {
		t.Errorf("signal %v vs shuffled %v — with identical returns the shuffle must be a no-op", sig, shuf)
	}
	if math.Abs(sig-0.04) > 1e-12 {
		t.Errorf("gross = %v, want 4%% — the book is fully invested", sig)
	}
}

func TestAllZeroForecastsHoldNothing(t *testing.T) {
	// Long-only rules clip to zero. A day with no signal must sit flat, not
	// quietly fall back to equal weight and collect the market's return.
	days := []Day{day(0,
		Obs{Sym: 1, U: []float64{0}, Ret: 0.10},
		Obs{Sym: 2, U: []float64{0}, Ret: 0.10},
	)}
	res, err := Run(days, []string{"r"}, cfg())
	if err != nil {
		t.Fatal(err)
	}
	if got := res[0].Signal.Gross[0]; got != 0 {
		t.Errorf("gross = %v on a no-signal day, want 0", got)
	}
	if got := res[0].EqualWeight.Gross[0]; math.Abs(got-0.10) > 1e-12 {
		t.Errorf("the equal-weight control should still be long: got %v, want 10%%", got)
	}
}

func TestEachRuleRunsOnItsOwnEligibleSet(t *testing.T) {
	// Rule 0 is defined for both names, rule 1 only for the second — a rule
	// still warming up must not silently inherit the other's universe.
	days := []Day{day(0,
		Obs{Sym: 1, U: []float64{1, math.NaN()}, Ret: 0.10},
		Obs{Sym: 2, U: []float64{1, 1}, Ret: -0.10},
	)}
	res, err := Run(days, []string{"a", "b"}, cfg())
	if err != nil {
		t.Fatal(err)
	}
	if got := res[0].Signal.Gross[0]; math.Abs(got) > 1e-12 {
		t.Errorf("rule a holds both names equally: gross = %v, want 0", got)
	}
	if len(res[1].Signal.Dates) != 0 {
		t.Errorf("rule b had one eligible name; a one-name cross-section is not a book, want the day skipped")
	}
}

func TestRunRefusesUndeclaredSeeds(t *testing.T) {
	if _, err := Run(nil, []string{"r"}, Config{CostBpsRoundTrip: 50}); err == nil {
		t.Fatal("Run accepted an empty seed list — the control would differ run to run")
	}
}

func TestMonthlyCompoundsWithinTheMonth(t *testing.T) {
	b := Book{
		Dates: []time.Time{
			time.Date(2020, 1, 10, 0, 0, 0, 0, time.UTC),
			time.Date(2020, 1, 20, 0, 0, 0, 0, time.UTC),
			time.Date(2020, 2, 3, 0, 0, 0, 0, time.UTC),
		},
		Net: []float64{0.10, 0.10, -0.05},
	}
	months, rets := Monthly(b)
	if len(months) != 2 {
		t.Fatalf("got %d months, want 2", len(months))
	}
	if want := 1.1*1.1 - 1; math.Abs(rets[0]-want) > 1e-12 {
		t.Errorf("January = %v, want %v (compounded, not summed)", rets[0], want)
	}
}

func TestPairedMonthlyUsesOnlySharedMonths(t *testing.T) {
	mk := func(n int, r float64) Book {
		b := Book{}
		for i := 0; i < n; i++ {
			b.Dates = append(b.Dates, time.Date(2020, time.Month(i+1), 15, 0, 0, 0, 0, time.UTC))
			b.Net = append(b.Net, r)
		}
		return b
	}
	p := PairedMonthly(mk(6, 0.02), mk(4, 0.01))
	if p.Months != 4 {
		t.Errorf("paired over %d months, want 4 — the two books only overlap for four", p.Months)
	}
	if math.Abs(p.MeanDiff-0.01) > 1e-12 {
		t.Errorf("mean difference = %v, want 1%%", p.MeanDiff)
	}
	if !math.IsInf(p.T, 1) && p.T < 1e6 {
		t.Errorf("a constant positive difference has no variance: t = %v", p.T)
	}
}
