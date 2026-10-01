package rotation

import (
	"math"
	"testing"
	"time"

	"github.com/ranedk/systrader/internal/bars"
	"github.com/ranedk/systrader/internal/stage"
)

// daily bars on weekdays from 2015-01-05, growing at `weekly` per week, liquid.
func series(sym string, weeks int, weekly float64) bars.Series {
	var bs []bars.Bar
	d := time.Date(2015, 1, 5, 0, 0, 0, 0, time.UTC)
	price := 100.0
	daily := math.Pow(1+weekly, 1.0/5)
	for w := 0; w < weeks; w++ {
		for k := 0; k < 5; k++ {
			price *= daily
			// 1e7 shares x ~100 = 1e9 traded value: far above the floor
			bs = append(bs, bars.Bar{Date: d, Open: price, High: price, Low: price, Close: price, Vol: 1e7})
			d = d.AddDate(0, 0, 1)
		}
		d = d.AddDate(0, 0, 2)
	}
	return bars.Series{Symbol: sym, Bars: bs}
}

func universe(t *testing.T) *Universe {
	t.Helper()
	b := NewBuilder()
	membership := map[string]Membership{}
	// industry FAST: 5 names rising ~1%/week; SLOW: 5 names flat; THIN: 4 names (below MinMembers)
	for i, sym := range []string{"F1", "F2", "F3", "F4", "F5"} {
		b.Add(series(sym, 120, 0.01+float64(i)*0.001))
		membership[sym] = Membership{Sector: "S1", Industry: "FAST"}
	}
	for _, sym := range []string{"L1", "L2", "L3", "L4", "L5"} {
		b.Add(series(sym, 120, 0.0))
		membership[sym] = Membership{Sector: "S2", Industry: "SLOW"}
	}
	for _, sym := range []string{"T1", "T2", "T3", "T4"} {
		b.Add(series(sym, 120, 0.02))
		membership[sym] = Membership{Sector: "S3", Industry: "THIN"}
	}
	return Build(b.Panel(), membership)
}

func TestWeeklyPanelAndEligibilityLagOneWeek(t *testing.T) {
	u := universe(t)
	if got := len(u.Panel.Weeks); got != 120 {
		t.Fatalf("weeks = %d, want 120", got)
	}
	s := u.Panel.Stocks["F1"]
	// 60 bars = 12 weeks before the median turnover exists: weeks 0..10 not eligible
	if s.Eligible[10] || !s.Eligible[11] {
		t.Fatalf("eligibility should start at week 11 (60th bar), got %v %v", s.Eligible[10], s.Eligible[11])
	}
	if !math.IsNaN(weeklyReturn(s, 11)) || math.IsNaN(weeklyReturn(s, 12)) {
		t.Fatal("a week's return needs eligibility at the END of the previous week")
	}
}

func TestLeaderIsTheRisingIndustryAndThinIndustriesAreNotRanked(t *testing.T) {
	u := universe(t)
	last := len(u.Panel.Weeks) - 1
	fast, slow, thin := u.Industries["FAST"], u.Industries["SLOW"], u.Industries["THIN"]
	if thin.Rank[last] != 0 || !math.IsNaN(thin.RS[last]) {
		t.Fatalf("4-member industry must not be ranked: rank %d rs %v", thin.Rank[last], thin.RS[last])
	}
	if fast.Rank[last] != 1 || slow.Rank[last] != 2 {
		t.Fatalf("ranks fast=%d slow=%d", fast.Rank[last], slow.Rank[last])
	}
	if fast.Stage[last].Stage != stage.Stage2Advancing || !fast.Leading[last] {
		t.Fatalf("FAST should be Stage 2 and leading, got stage %v leading %v", fast.Stage[last].Stage, fast.Leading[last])
	}
	if slow.Leading[last] {
		t.Fatal("a flat industry outside the top fifth must not lead")
	}
	if !(fast.RS[last] > 0 && slow.RS[last] < 0) {
		t.Fatalf("RS signs: fast %v slow %v", fast.RS[last], slow.RS[last])
	}
}

func TestStatsAndWeeksInStage2(t *testing.T) {
	u := universe(t)
	last := len(u.Panel.Weeks) - 1
	if w := WeeksInStage2(u.StockCls["F1"], last); w < 50 {
		t.Fatalf("a steady riser should have a long Stage 2 run, got %d", w)
	}
	st := u.Stats(u.Panel.Weeks[0])
	// runs still open at the end are not completed runs
	if st.Stage2Runs != 0 {
		t.Fatalf("no completed Stage 2 runs expected, got %d", st.Stage2Runs)
	}
}
