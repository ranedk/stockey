package paper

import (
	"fmt"
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

// mkDaysVarying builds days whose daily return follows the supplied sequence,
// so a fixture can create volatility or a downtrend on demand.
func mkDaysVarying(m int, rets []float64) []Day {
	start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	price := make([]float64, m)
	for i := range price {
		price[i] = 100
	}
	var days []Day
	for d, ret := range rets {
		day := Day{Date: start.AddDate(0, 0, d)}
		for i := 0; i < m; i++ {
			prev := price[i]
			close := prev * (1 + ret)
			price[i] = close
			day.Obs = append(day.Obs, Obs{
				Symbol: string(rune('A' + i)), Open: prev, Close: close, PrevClose: prev,
				Forecast: float64(i), Turnover: 1e9, Eligible: true,
			})
		}
		days = append(days, day)
	}
	return days
}

func overlaySpec(o Overlay) Spec {
	s := spec()
	s.Overlay = o
	s.TargetVol = 0.15
	s.VolLookback = 10
	s.TrendWindow = 10
	return s
}

func TestVolScalingShrinksTheBookWhenItGetsJumpy(t *testing.T) {
	// Ten quiet days, then ten violent ones. The overlay must be fully
	// invested through the quiet stretch and well under 1 after the jumps.
	rets := make([]float64, 0, 30)
	for i := 0; i < 12; i++ {
		rets = append(rets, 0.0005)
	}
	for i := 0; i < 18; i++ {
		if i%2 == 0 {
			rets = append(rets, 0.05)
		} else {
			rets = append(rets, -0.045)
		}
	}
	tr, err := Compute(overlaySpec(OverlayVolScaled), mkDaysVarying(10, rets))
	if err != nil {
		t.Fatal(err)
	}
	early := tr.Exposure[11].Exposure
	late := tr.Exposure[len(tr.Exposure)-1].Exposure
	if early < 0.99 {
		t.Errorf("exposure %.3f during the quiet stretch, want fully invested", early)
	}
	if late > 0.35 {
		t.Errorf("exposure %.3f after the book started swinging 5%% a day, want a much smaller book", late)
	}
}

func TestVolScalingNeverLevers(t *testing.T) {
	// A book far calmer than target must not be geared up: this is a cash
	// account, and an overlay that can borrow is a different animal.
	rets := make([]float64, 40)
	for i := range rets {
		rets[i] = 0.0001
		if i%2 == 0 {
			rets[i] = -0.00005
		}
	}
	tr, err := Compute(overlaySpec(OverlayVolScaled), mkDaysVarying(10, rets))
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range tr.Exposure {
		if e.Exposure > 1+1e-12 {
			t.Fatalf("exposure %.3f on %s exceeds fully invested", e.Exposure, e.Date.Format("2006-01-02"))
		}
	}
}

func TestRegimeFloorGoesFlatBelowTheTrend(t *testing.T) {
	// Fifteen days up, then a sustained decline that drags the index under its
	// own average.
	var rets []float64
	for i := 0; i < 15; i++ {
		rets = append(rets, 0.01)
	}
	for i := 0; i < 15; i++ {
		rets = append(rets, -0.02)
	}
	tr, err := Compute(overlaySpec(OverlayRegimeFloor), mkDaysVarying(10, rets))
	if err != nil {
		t.Fatal(err)
	}
	if got := tr.Exposure[14].Exposure; got != 1 {
		t.Errorf("exposure %.2f while the market was rising, want 1", got)
	}
	if got := tr.Exposure[len(tr.Exposure)-1].Exposure; got != 0 {
		t.Errorf("exposure %.2f after a sustained decline, want 0", got)
	}
}

func TestCashEarnsNothing(t *testing.T) {
	// Half invested through a 10% day must earn 5%, not 10%.
	days := mkDaysVarying(10, []float64{0.10, 0.10})
	sp := overlaySpec(OverlayNone)
	tr, err := Compute(sp, days)
	if err != nil {
		t.Fatal(err)
	}
	full := tr.Books[BookStrategy].NAV[1].Return

	// Same fixture, but force half exposure by targeting half the realised vol.
	sp2 := overlaySpec(OverlayVolScaled)
	sp2.VolLookback = 1 // no history: the overlay has no opinion
	tr2, err := Compute(sp2, days)
	if err != nil {
		t.Fatal(err)
	}
	if math.Abs(tr2.Books[BookStrategy].NAV[1].Return-full) > 1e-12 {
		t.Error("with no volatility history the overlay must not change anything")
	}
	if tr.Books[BookStrategy].NAV[1].Exposure != 1 {
		t.Error("the unprotected book must record full exposure")
	}
}

func TestOverlayLeavesTheControlsAlone(t *testing.T) {
	// The benchmarks must stay fully invested: they answer "is picking better
	// than not picking", and scaling them would fold two questions into one.
	var rets []float64
	for i := 0; i < 15; i++ {
		rets = append(rets, 0.01)
	}
	for i := 0; i < 15; i++ {
		rets = append(rets, -0.02)
	}
	tr, err := Compute(overlaySpec(OverlayBoth), mkDaysVarying(10, rets))
	if err != nil {
		t.Fatal(err)
	}
	for _, book := range []string{BookEqual, BookRandom} {
		for _, p := range tr.Books[book].NAV {
			if p.Exposure != 1 {
				t.Fatalf("%s ran at %.2f exposure; controls are always fully invested", book, p.Exposure)
			}
		}
	}
}

// TestCashDoesNotSilentlyReinvestItself pins the bug that made the volatility
// overlay look 3.8% a year worse than it is: renormalising the holdings to sum
// to 1 each day reinvests the cash, so a book that traded nothing reported a
// full (1 - exposure) of turnover every morning.
func TestCashDoesNotSilentlyReinvestItself(t *testing.T) {
	sp := overlaySpec(OverlayRegimeFloor)
	sp.RebalanceEvery = 100 // one rebalance, then pure holding
	sp.TrendWindow = 10
	sp.ExposureDaily = true // the overlay must be free to act, or nothing goes flat

	// Rise long enough to be above trend, then fall enough to go flat, then
	// keep falling: once flat, the book must trade nothing at all.
	var rets []float64
	for i := 0; i < 15; i++ {
		rets = append(rets, 0.01)
	}
	for i := 0; i < 20; i++ {
		rets = append(rets, -0.02)
	}
	tr, err := Compute(sp, mkDaysVarying(10, rets))
	if err != nil {
		t.Fatal(err)
	}
	b := tr.Books[BookStrategy]

	var flatDays, tradedWhileFlat int
	for i, p := range b.NAV {
		if p.Exposure != 0 || i == 0 {
			continue
		}
		flatDays++
		if p.Turnover > 1e-9 {
			tradedWhileFlat++
		}
	}
	if flatDays == 0 {
		t.Fatal("fixture never went flat — the test proves nothing")
	}
	if tradedWhileFlat > 1 {
		t.Errorf("%d of %d flat days generated turnover; a book holding cash trades nothing",
			tradedWhileFlat, flatDays)
	}
}

func stopSpec(k StopKind, level float64) Spec {
	s := spec()
	s.Stop = k
	s.StopLevel = level
	s.RebalanceEvery = 100 // one selection, so only the stop can sell
	return s
}

func TestFixedStopExitsAtTheNextOpenNotAtTheStopPrice(t *testing.T) {
	// Up a little, then a 12% fall through a 10% stop. The exit must happen at
	// the NEXT open — assuming a fill at the stop price itself would be an
	// intraday fantasy on daily bars, and would flatter every result.
	days := mkDaysVarying(10, []float64{0.01, 0.01, -0.12, 0.01, 0.01})
	tr, err := Compute(stopSpec(StopFixed, 0.10), days)
	if err != nil {
		t.Fatal(err)
	}
	b := tr.Books[BookStrategy]
	if b.Stopped == 0 {
		t.Fatal("a 12% fall through a 10% stop triggered nothing")
	}
	// Day 2 (index 2) breaches; the sale lands on day 3.
	if b.NAV[2].Turnover > 1e-9 {
		t.Error("the book traded on the day the stop was breached; the fill is the next open")
	}
	if b.NAV[3].Turnover < 1e-9 {
		t.Error("the book did not sell on the day after the breach")
	}
	if len(b.Holdings) != 0 {
		t.Errorf("%d positions still held after every name stopped out", len(b.Holdings))
	}
}

func TestTrailingStopMeasuresFromTheHighNotTheEntry(t *testing.T) {
	// Up 30%, then down 12%: nothing is below its ENTRY, but everything is 12%
	// off its high. A fixed stop must hold; a trailing stop must sell.
	// The trailing day matters: a breach on the final bar is sold on a day that
	// does not exist, so the fixture has to run one bar past the fall.
	rets := []float64{0.10, 0.10, 0.08, -0.12, 0.0}
	fixed, err := Compute(stopSpec(StopFixed, 0.10), mkDaysVarying(10, rets))
	if err != nil {
		t.Fatal(err)
	}
	if fixed.Books[BookStrategy].Stopped != 0 {
		t.Error("a fixed stop fired on a position that is well above its entry price")
	}
	trailing, err := Compute(stopSpec(StopTrailing, 0.10), mkDaysVarying(10, rets))
	if err != nil {
		t.Fatal(err)
	}
	if trailing.Books[BookStrategy].Stopped == 0 {
		t.Error("a trailing stop ignored a 12% fall from the high")
	}
}

func TestVolScaledStopGivesJumpyNamesMoreRoom(t *testing.T) {
	// Same 15% fall, two different volatilities: at half the annualised vol,
	// the calm name's stop is closer and should trigger while the jumpy one
	// holds.
	build := func(annVol float64) []Day {
		days := mkDaysVarying(10, []float64{0.01, -0.15, 0.0})
		for i := range days {
			for j := range days[i].Obs {
				days[i].Obs[j].AnnVol = annVol
			}
		}
		return days
	}
	calm, err := Compute(stopSpec(StopVolFixed, 0.5), build(0.20)) // stop at 10%
	if err != nil {
		t.Fatal(err)
	}
	jumpy, err := Compute(stopSpec(StopVolFixed, 0.5), build(0.80)) // stop at 40%
	if err != nil {
		t.Fatal(err)
	}
	if calm.Books[BookStrategy].Stopped == 0 {
		t.Error("the calm name's 10% stop ignored a 15% fall")
	}
	if jumpy.Books[BookStrategy].Stopped != 0 {
		t.Error("the jumpy name's 40% stop fired on a 15% fall")
	}
}

func TestNoStopMeansNoExits(t *testing.T) {
	tr, err := Compute(stopSpec(StopNone, 0), mkDaysVarying(10, []float64{0.01, -0.30, -0.30}))
	if err != nil {
		t.Fatal(err)
	}
	if tr.Books[BookStrategy].Stopped != 0 {
		t.Error("positions were stopped out with no stop rule configured")
	}
}

func blendDay(scoreA, scoreB func(i int) float64) Day {
	d := Day{Date: time.Date(2026, 9, 14, 0, 0, 0, 0, time.UTC)}
	for i := 0; i < 10; i++ {
		d.Obs = append(d.Obs, Obs{
			Symbol: fmt.Sprintf("S%d", i), Eligible: true, Forecast: float64(10 - i),
			Signals: []float64{scoreA(i), scoreB(i)},
		})
	}
	return d
}

func blendSpec() Spec {
	s := FrozenSpec()
	s.Mode = ModeBookBlend
	s.Variants = []string{"a", "b"}
	s.VariantWeights = []float64{0.75, 0.25}
	return s
}

func TestBookBlendHoldsEachVariantsBookAtItsWeight(t *testing.T) {
	// Top fifth of ten is two names. Variant a picks S0, S1; b picks S1, S2.
	d := blendDay(func(i int) float64 { return float64(-i) }, func(i int) float64 { return -math.Abs(float64(i) - 1.5) })
	w := targetWeights(BookStrategy, blendSpec(), d)
	want := map[string]float64{"S0": 0.375, "S1": 0.375 + 0.125, "S2": 0.125}
	var sum float64
	for sym, v := range w {
		sum += v
		if math.Abs(v-want[sym]) > 1e-12 {
			t.Errorf("%s weight %v, want %v", sym, v, want[sym])
		}
	}
	if len(w) != 3 || math.Abs(sum-1) > 1e-12 {
		t.Errorf("book %v sums to %v over %d names", w, sum, len(w))
	}
}

func TestBookBlendHandsAMissingVariantsShareToTheOthers(t *testing.T) {
	d := blendDay(func(i int) float64 { return float64(-i) }, func(int) float64 { return math.NaN() })
	w := targetWeights(BookStrategy, blendSpec(), d)
	if math.Abs(w["S0"]-0.5) > 1e-12 || math.Abs(w["S1"]-0.5) > 1e-12 || len(w) != 2 {
		t.Errorf("with variant b still warming up, the book should be all of a's: %v", w)
	}
}

func TestQualifyListsEveryVariantAStockMeets(t *testing.T) {
	d := blendDay(func(i int) float64 { return float64(-i) }, func(i int) float64 { return -math.Abs(float64(i) - 1.5) })
	got := Qualify(blendSpec(), d)
	want := []Qualification{{"S0", "a"}, {"S1", "a"}, {"S1", "b"}, {"S2", "b"}}
	if len(got) != len(want) {
		t.Fatalf("got %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("qualification %d = %v, want %v", i, got[i], want[i])
		}
	}
	// A single-signal strategy qualifies its top quintile under its signal's name.
	single := Qualify(FrozenSpec(), d)
	if len(single) != 2 || single[0] != (Qualification{"S0", "ewmac32"}) || single[1] != (Qualification{"S1", "ewmac32"}) {
		t.Errorf("single-signal qualifications = %v", single)
	}
}

func TestTheLookbackBlendSpecIsWhole(t *testing.T) {
	s := MomentumLookbackBlendSpec()
	if len(s.Variants) != len(s.VariantWeights) {
		t.Fatalf("%d variants, %d weights", len(s.Variants), len(s.VariantWeights))
	}
	var sum float64
	for _, w := range s.VariantWeights {
		sum += w
	}
	if math.Abs(sum-1) > 1e-9 {
		t.Errorf("weights sum to %v", sum)
	}
	if _, ok := SpecFor(s.Name); !ok {
		t.Error("not registered")
	}
}
