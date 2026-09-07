package futures

import (
	"math"
	"math/rand"
	"testing"
	"time"

	"github.com/ranedk/systrader/internal/core"
)

// synthCurve builds a futures curve whose truth we know: a spot random walk, a
// constant contango carry, monthly expiries, and slots that concatenate
// successive generations WITHOUT adjustment — exactly what Dhan serves.
type synth struct {
	dates      []time.Time
	spot       core.Series
	pos1, pos2 core.Series
	rolls      []time.Time
	// trueDiff[i] is the P&L a holder who rolls at expiry actually earns on
	// dates[i]: the change in the contract they held across that night.
	trueDiff  []float64
	carryRate float64 // price units per year of contango
}

func makeSynth(days, monthLen int, carryRate float64, seed int64) synth {
	rng := rand.New(rand.NewSource(seed))
	start := time.Date(2016, 1, 4, 0, 0, 0, 0, time.UTC)
	var s synth
	s.carryRate = carryRate

	spot := make([]float64, days)
	p := 10000.0
	for i := range spot {
		p += 60 * rng.NormFloat64()
		spot[i] = p
		s.dates = append(s.dates, start.AddDate(0, 0, i))
	}
	s.spot = core.New(s.dates, spot)

	// Expiry every monthLen bars; the front contract is the next expiry.
	expiryIdx := func(i int) int { return ((i / monthLen) + 1) * monthLen }
	// F(t, T) = spot + carry * (T - t)/365, in trading-day units of ~1 day.
	f := func(i, tIdx int) float64 {
		return spot[i] + carryRate*float64(tIdx-i)/365
	}

	p1 := make([]float64, days)
	p2 := make([]float64, days)
	s.trueDiff = make([]float64, days)
	for i := 0; i < days; i++ {
		e1 := expiryIdx(i)
		e2 := e1 + monthLen
		p1[i] = f(i, e1)
		p2[i] = f(i, e2)
		if i == 0 {
			s.trueDiff[i] = math.NaN()
			continue
		}
		// Which contract did the holder own overnight? The one the front slot
		// holds TODAY — on a roll day that is yesterday's second position.
		s.trueDiff[i] = f(i, e1) - f(i-1, e1)
		if expiryIdx(i-1) != e1 {
			s.rolls = append(s.rolls, s.dates[i])
		}
	}
	s.pos1 = core.New(s.dates, p1)
	s.pos2 = core.New(s.dates, p2)
	return s
}

// TestFromLadder_IsMeasurablyUnreliable pins down a limitation rather than a
// feature, so that nobody later mistakes this detector for a good one.
//
// A positional ladder re-labels itself at a roll: every slot shifts at once,
// so the only tell is the front's own price step, which is indistinguishable
// from a market move of the same size. Here the basis is 1.7% against 0.6%
// daily volatility — a ratio no real contract offers — and it still misses
// roughly a third of the rolls. Use it to choose the DAY once the calendar has
// chosen the month (SnapToLadder), never on its own.
func TestFromLadder_IsMeasurablyUnreliable(t *testing.T) {
	s := makeSynth(750, 21, 3000, 5)
	got := FromLadder(s.pos1, s.pos2)
	matched, falsePos, missed := Agreement(got, s.rolls, 0)
	if matched == len(s.rolls) && falsePos == 0 {
		t.Fatal("the ladder detector was exact on this fixture — if that is genuinely true, " +
			"the comment above and the calendar fallback need revisiting, not this test")
	}
	if matched < len(s.rolls)/2 {
		t.Errorf("ladder detector found only %d of %d rolls (%d false positives) — worse than documented",
			matched, len(s.rolls), falsePos)
	}
	t.Logf("measured: %d/%d rolls found, %d false positives, %d missed", matched, len(s.rolls), falsePos, missed)
}

// TestCalendarSnappedToLadder_FindsExactlyTheRealRolls is the detector that
// commodities actually use: the month comes from the expiry convention, the
// day from the ladder.
func TestCalendarSnappedToLadder_FindsExactlyTheRealRolls(t *testing.T) {
	s := makeSynth(750, 21, 3000, 5)
	// The fixture rolls every 21 calendar days rather than on a fixed day of
	// the month, so feed the candidates the fixture's own cadence: this test
	// is about the snapping, not about guessing MCX conventions.
	var candidates []time.Time
	for _, r := range s.rolls {
		candidates = append(candidates, r.AddDate(0, 0, -2)) // deliberately off by two days
	}
	_ = LadderMonths
	got := SnapToLadder(candidates, s.pos1, s.pos2, 4)
	matched, falsePos, missed := Agreement(got, s.rolls, 0)
	bare, _, _ := Agreement(FromLadder(s.pos1, s.pos2), s.rolls, 0)

	// Measured, not aspirational: snapping lands ~32 of 35 exactly, against
	// ~23 for the bare ladder. The residual few are rolls whose day the market
	// move masked — the basis and the day's move nearly cancelled, so a
	// neighbouring day scored higher. A roll snapped one day off corrupts that
	// one daily difference by roughly the basis and nothing else: the level
	// shift, and therefore every other difference, is unchanged.
	if matched <= bare {
		t.Errorf("snapping to the calendar found %d rolls, no better than the bare ladder's %d", matched, bare)
	}
	if matched*10 < len(s.rolls)*9 {
		t.Errorf("calendar+ladder found %d of %d rolls (%d false positives, %d missed) — worse than the documented ~32/35",
			matched, len(s.rolls), falsePos, missed)
	}
	t.Logf("measured: %d/%d exact (bare ladder %d), %d false positives", matched, len(s.rolls), bare, falsePos)
}

func TestFromReference_FindsExactlyTheRealRolls(t *testing.T) {
	s := makeSynth(750, 21, 3000, 6)
	// 20 sigmas, not the 3-4 an outlier test would normally use: in clean data
	// a splice is ~150 robust sigmas and the noisiest ordinary day is ~12, so
	// the threshold sits in an empty gap. Real data is dirtier; the report
	// command prints the score distribution so the gap can be seen or its
	// absence noticed.
	got := FromReference(s.pos1, s.spot, 20)
	matched, onlyGot, onlyTrue := Agreement(got, s.rolls, 0)
	if onlyGot != 0 || onlyTrue != 0 {
		t.Errorf("reference detector: %d matched, %d false positives, %d missed (of %d real rolls)",
			matched, onlyGot, onlyTrue, len(s.rolls))
	}
}

// TestPanamaReproducesTheRealPnL is the test that matters: the whole point of
// stitching is that differences on the adjusted series equal what a position
// actually earned, roll nights included.
func TestPanamaReproducesTheRealPnL(t *testing.T) {
	s := makeSynth(750, 21, 3000, 7)
	adj, gaps, err := Panama(s.pos1, s.pos2, s.rolls)
	if err != nil {
		t.Fatal(err)
	}
	if len(gaps) != len(s.rolls) {
		t.Fatalf("measured %d splices, expected %d", len(gaps), len(s.rolls))
	}
	for i := 1; i < adj.Len(); i++ {
		got := adj.Values[i] - adj.Values[i-1]
		want := s.trueDiff[i]
		if math.Abs(got-want) > 1e-6 {
			t.Fatalf("bar %d (%s): adjusted change %.6f, real P&L %.6f",
				i, adj.Times[i].Format("2006-01-02"), got, want)
		}
	}
	// And the raw series must NOT have that property, or the test proves nothing.
	var worstRaw float64
	for i := 1; i < s.pos1.Len(); i++ {
		if d := math.Abs((s.pos1.Values[i] - s.pos1.Values[i-1]) - s.trueDiff[i]); d > worstRaw {
			worstRaw = d
		}
	}
	if worstRaw < 1 {
		t.Fatalf("the unadjusted series was already correct (worst error %.6f) — the fixture has no splices", worstRaw)
	}
}

func TestPanamaRefusesToGuessAMissingSplice(t *testing.T) {
	s := makeSynth(300, 21, 3000, 8)
	// Blank the second position entirely: the basis can no longer be read on
	// the same day, and inventing it by differencing across the roll is
	// exactly what this package must never do.
	empty := core.New(nil, nil)
	if _, _, err := Panama(s.pos1, empty, s.rolls); err == nil {
		t.Fatal("Panama stitched a series with no second position to measure the splice against")
	}
}

func TestCarryIsTheContangoAndSurvivesRolls(t *testing.T) {
	s := makeSynth(400, 21, 3000, 9)
	// The fixture's ladder is monthLen bars wide; expiries are that far apart.
	yg := float64(21) / 365
	c, err := Carry(s.pos1, s.pos2, yg)
	if err != nil {
		t.Fatal(err)
	}
	// Contango: the front is cheaper, so carry is negative — holding costs.
	// Magnitude: (p1-p2) = -carryRate*monthLen/365, over yearGap = monthLen/365.
	want := -s.carryRate
	for i := 0; i < c.Len(); i++ {
		if math.IsNaN(c.Values[i]) {
			continue
		}
		if math.Abs(c.Values[i]-want) > 0.02*math.Abs(want) {
			t.Fatalf("bar %d: carry %.1f, want ~%.1f price units/yr", i, c.Values[i], want)
		}
	}
	// A roll must not show up in the carry series at all.
	for _, r := range s.rolls {
		var prev, at float64
		for i, d := range c.Times {
			if d.Equal(r) && i > 0 {
				prev, at = c.Values[i-1], c.Values[i]
			}
		}
		if prev != 0 && math.Abs(at-prev) > 0.02*math.Abs(want) {
			t.Errorf("carry jumped %.1f -> %.1f across the roll on %s — the splice did not cancel",
				prev, at, r.Format("2006-01-02"))
		}
	}
}

func TestCleanRejectsRecycledIdsAndCollapsesDuplicates(t *testing.T) {
	s := makeSynth(600, 21, 3000, 10)
	// A recycled security id: an option premium series, two orders of
	// magnitude below the curve and moving to its own drum. This is the
	// NIFTY-2026-06-30 case, which printed 117.10 when the index was 10458.40.
	rng := rand.New(rand.NewSource(11))
	junk := make([]float64, s.pos1.Len())
	v := 200.0
	for i := range junk {
		v *= 1 + 0.05*rng.NormFloat64()
		junk[i] = math.Max(v, 1)
	}

	slots := []Slot{
		{Ticker: "X-2026-01-01", Expiry: date(2026, 1, 1), Closes: s.pos1},
		{Ticker: "X-2026-02-01", Expiry: date(2026, 2, 1), Closes: s.pos2},
		{Ticker: "X-2026-03-01", Expiry: date(2026, 3, 1), Closes: s.pos1}, // duplicate stream
		{Ticker: "X-2026-04-01", Expiry: date(2026, 4, 1), Closes: core.New(s.pos1.Times, junk)},
	}
	c := Clean("X", slots)

	if len(c.Positions) != 2 {
		var names []string
		for _, p := range c.Positions {
			names = append(names, p.Ticker)
		}
		t.Fatalf("kept %v, want exactly the two genuine curve positions", names)
	}
	if !c.Positions[0].Expiry.Before(c.Positions[1].Expiry) {
		t.Error("positions are not ordered by expiry — position 1 must be the front")
	}
	var sawJunk, sawDup bool
	for _, r := range c.Rejected {
		switch r.Ticker {
		case "X-2026-04-01":
			sawJunk = true
		case "X-2026-03-01":
			sawDup = true
		}
	}
	if !sawJunk {
		t.Error("the recycled-id series was not rejected")
	}
	if !sawDup {
		t.Error("the duplicate stream was not collapsed")
	}
}

func date(y, m, d int) time.Time { return time.Date(y, time.Month(m), d, 0, 0, 0, 0, time.UTC) }
