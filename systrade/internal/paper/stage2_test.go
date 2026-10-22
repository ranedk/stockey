package paper

import (
	"testing"
	"time"
)

func obs(sym, group string, f float64, excluded bool) Obs {
	return Obs{Symbol: sym, Group: group, Forecast: f, Eligible: true, Excluded: excluded, Open: 100, Close: 100, PrevClose: 100}
}

func TestStage2OnlyBuysOnlyStage2ButBenchmarksKeepTheUniverse(t *testing.T) {
	spec := Stage2RSLeadersSpec()
	spec.HoldCount = 2
	d := Day{Date: time.Now(), Obs: []Obs{
		obs("A", "g1", 0.9, true), // best return but not Stage 2
		obs("B", "g1", 0.5, false), obs("C", "g2", 0.4, false), obs("D", "g3", 0.1, false),
		obs("E", "g4", 0.0, false), obs("F", "g5", -0.1, false),
	}}
	w, _ := selectWeights(BookStrategy, spec, d, nil)
	if _, ok := w["A"]; ok || len(w) != 2 || w["B"] == 0 || w["C"] == 0 {
		t.Fatalf("strategy should hold B and C only, got %v", w)
	}
	eq, _ := selectWeights(BookEqual, spec, d, nil)
	if len(eq) != 6 {
		t.Fatalf("equal-weight benchmark keeps the whole universe, got %d names", len(eq))
	}
}

func TestIndustryCapSkipsAFullGroup(t *testing.T) {
	ranked := []Obs{obs("A", "g1", 5, false), obs("B", "g1", 4, false), obs("C", "g1", 3, false), obs("D", "g2", 2, false)}
	got := pickTopCapped(ranked, 3, 1, nil, 2)
	if len(got) != 3 || got[0] != "A" || got[1] != "B" || got[2] != "D" {
		t.Fatalf("cap 2 per group should give A B D, got %v", got)
	}
	// a kept name counts against its group first
	got = pickTopCapped(ranked, 2, 2, map[string]bool{"C": true}, 1)
	if got[0] != "C" || got[1] != "D" {
		t.Fatalf("kept C fills g1's single slot, got %v", got)
	}
}

func TestSignalStopExitsOnTheExitFlag(t *testing.T) {
	spec := Stage2RSLeadersSpec()
	b := &Book{Holdings: map[string]float64{"A": 0.5, "B": 0.5}, Entries: map[string]Entry{
		"A": {Price: 100, LastPrice: 100}, "B": {Price: 100, LastPrice: 100}}}
	prices := map[string]Obs{"A": {Symbol: "A", ExitSignal: true}, "B": {Symbol: "B"}}
	out := breachedStops(spec, b, prices, 0)
	if !out["A"] || out["B"] {
		t.Fatalf("only A's exit signal fired, got %v", out)
	}
}

func TestFrozenSpecsAreUntouched(t *testing.T) {
	for _, s := range Specs() {
		if s.Name == "stage2-rs-leaders" {
			continue
		}
		if s.Name == "stage2-rs-leaders-clean" {
			continue
		}
		if s.Stage2Only || s.MaxPerGroup != 0 || s.Stop == StopSignal || s.FundamentalFilter || s.StoppedSlotsToCash {
			t.Fatalf("%s picked up a new field", s.Name)
		}
	}
}

func TestFundamentalPassUsesTheDaysMedianNewestVersionAndNoLookAhead(t *testing.T) {
	d1 := time.Date(2026, 9, 29, 0, 0, 0, 0, time.UTC)
	d2 := d1.AddDate(0, 0, 1)
	f := NewFundamentalPass([]FilterRow{
		{d1, "A", 90, false, 1}, {d1, "B", 10, false, 1}, {d1, "C", 50, false, 1},
		{d2, "A", 90, true, 2}, {d2, "B", 80, false, 2}, {d2, "C", 50, false, 2}, {d2, "A", 99, false, 1},
	})
	if ok, _ := f.Passes("A", d1.AddDate(0, 0, -1)); ok {
		t.Fatal("no score before the first date: nothing passes")
	}
	if ok, _ := f.Passes("A", d1); !ok {
		t.Fatal("A is above the median with no flaw on d1")
	}
	if ok, _ := f.Passes("B", d1); ok {
		t.Fatal("B is below the median on d1")
	}
	// on d2 only version 2 counts: A has a flaw, B passes, an unscored name never does
	if ok, _ := f.Passes("A", d2.AddDate(0, 0, 3)); ok {
		t.Fatal("A's flaw on d2 must fail it")
	}
	if ok, _ := f.Passes("B", d2); !ok {
		t.Fatal("B passes on d2")
	}
	if ok, _ := f.Passes("ZZZ", d2); ok {
		t.Fatal("no score -> fails")
	}
}

func TestStoppedSlotStaysInCashUntilTheNextRebalance(t *testing.T) {
	spec := Stage2RSLeadersSpec()
	spec.HoldCount = 4
	spec.RebalanceEvery = 10
	spec.MaxPerGroup = 0
	day := func(d int, exitA bool) Day {
		var obs []Obs
		for k, s := range []string{"A", "B", "C", "D", "E"} {
			obs = append(obs, Obs{Symbol: s, Open: 100, Close: 100, PrevClose: 100, Forecast: float64(10 - k),
				Eligible: true, ExitSignal: exitA && s == "A"})
		}
		return Day{Date: time.Date(2026, 10, 1+d, 0, 0, 0, 0, time.UTC), Obs: obs}
	}
	days := []Day{day(0, false), day(1, true), day(2, false), day(3, false)}
	tr, err := Compute(spec, days)
	if err != nil {
		t.Fatal(err)
	}
	b := tr.Books[BookStrategy]
	// day 1's close fires A's exit; day 2 sells it at the open; the 25% stays in cash after
	sum := 0.0
	for _, w := range b.Holdings {
		sum += w
	}
	if _, held := b.Holdings["A"]; held || sum > 0.76 || sum < 0.74 {
		t.Fatalf("A sold and its quarter left in cash; holdings %v (sum %.3f)", b.Holdings, sum)
	}
	if b.NAV[3].Turnover != 0 {
		t.Fatalf("no reinvestment the day after the exit, turnover %v", b.NAV[3].Turnover)
	}
}
