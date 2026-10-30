package rotation

import (
	"math"
	"testing"
	"time"

	"github.com/ranedk/systrader/internal/bars"
)

// segments: (weeks, weekly growth) pieces, daily bars on weekdays from 2015-01-05; vol sets liquidity.
func shaped(sym string, vol float64, segs ...[2]float64) bars.Series {
	var bs []bars.Bar
	d := time.Date(2015, 1, 5, 0, 0, 0, 0, time.UTC)
	price := 100.0
	for _, sg := range segs {
		daily := math.Pow(1+sg[1], 1.0/5)
		for w := 0; w < int(sg[0]); w++ {
			for k := 0; k < 5; k++ {
				price *= daily
				bs = append(bs, bars.Bar{Date: d, Open: price, High: price, Low: price, Close: price, Vol: vol})
				d = d.AddDate(0, 0, 1)
			}
			d = d.AddDate(0, 0, 2)
		}
	}
	return bars.Series{Symbol: sym, Bars: bs}
}

func themePanel() (*Panel, Index, map[string][]ThemeMember) {
	b := NewBuilder()
	themes := map[string][]ThemeMember{}
	var all []string
	// RISE: five steady risers (long Stage 2) and one fresh breakout after a long flat base
	for _, s := range []string{"R1", "R2", "R3", "R4", "R5"} {
		b.Add(shaped(s, 1e6, [2]float64{120, 0.01}))
		themes["rise"] = append(themes["rise"], ThemeMember{Key: s, Weight: 1})
		all = append(all, s)
	}
	b.Add(shaped("BRK", 1e6, [2]float64{110, 0}, [2]float64{10, 0.02}))
	themes["rise"] = append(themes["rise"], ThemeMember{Key: "BRK", Weight: 0.5})
	all = append(all, "BRK")
	// FALL: five decliners; one of them also in RISE would be a two-theme stock
	for _, s := range []string{"D1", "D2", "D3", "D4", "D5"} {
		b.Add(shaped(s, 1e6, [2]float64{60, 0.01}, [2]float64{60, -0.01}))
		themes["fall"] = append(themes["fall"], ThemeMember{Key: s, Weight: 1})
		all = append(all, s)
	}
	themes["fall"] = append(themes["fall"], ThemeMember{Key: "R1", Weight: 0.2}, ThemeMember{Key: "NOPE", Weight: 1})
	// an illiquid member: Rs 100 x 1,000 shares = Rs 1 lakh a day, under the Rs 1 cr floor
	b.Add(shaped("ILQ", 1e3, [2]float64{120, 0.01}))
	themes["rise"] = append(themes["rise"], ThemeMember{Key: "ILQ", Weight: 1})
	p := b.Panel()
	var mkt []*Stock
	for _, s := range all {
		mkt = append(mkt, p.Stocks[s])
	}
	return p, BuildIndex(p, mkt, 1), themes
}

func TestThemeViewsRankAFreshBreakoutAboveExtendedRisersAndWeightBreadth(t *testing.T) {
	p, market, themes := themePanel()
	snap := BuildThemes(p, market, themes, len(p.Weeks)-1)
	st := map[string]ThemeStockView{}
	for _, s := range snap.Stocks {
		st[s.Key] = s
	}
	brk, r2, d1 := st["BRK"], st["R2"], st["D1"]
	if brk.Tier != 4 || r2.Tier != 3 || d1.Tier != 0 {
		t.Fatalf("tiers: breakout %d (weeks %d, above MA30 %v), riser %d, decliner %d", brk.Tier, brk.WeeksInStage, *brk.AboveMA30Pct, r2.Tier, d1.Tier)
	}
	if !(*brk.TrendScore > *r2.TrendScore && *r2.TrendScore > *d1.TrendScore) {
		t.Fatalf("scores: breakout %v riser %v decliner %v", *brk.TrendScore, *r2.TrendScore, *d1.TrendScore)
	}
	if brk.BreakoutLevel == nil || math.Abs(*brk.BreakoutLevel-100) > 0.5 || *brk.FromBreakoutPct < 15 {
		t.Fatalf("breakout level should be the flat base at 100: %v, from it %v, weeks in stage %d", *brk.BreakoutLevel, *brk.FromBreakoutPct, brk.WeeksInStage)
	}
	if st["ILQ"].Liquid || !r2.Liquid {
		t.Fatal("liquidity flag at the Rs 1 cr floor")
	}
	if len(st["R1"].Themes) != 2 {
		t.Fatalf("a stock in two themes appears once with both: %v", st["R1"].Themes)
	}
	if len(snap.Missing) != 1 || snap.Missing[0] != "NOPE" {
		t.Fatalf("missing = %v", snap.Missing)
	}
	var rise, fall ThemeView
	for _, tv := range snap.Themes {
		switch tv.ThemeID {
		case "rise":
			rise = tv
		case "fall":
			fall = tv
		}
	}
	// RISE: every member with a stage is in Stage 2 -> 100%; the illiquid one is not in the index
	if rise.Stage2Pct == nil || *rise.Stage2Pct != 100 || rise.IndexMembers != 6 || rise.EarlyStage2 != 1 {
		t.Fatalf("rise: stage2 %v index members %d early %d", rise.Stage2Pct, rise.IndexMembers, rise.EarlyStage2)
	}
	// FALL: R1 (weight 0.2) is its only Stage 2 member: 0.2 / 5.2
	if fall.Stage2Pct == nil || math.Abs(*fall.Stage2Pct-100*0.2/5.2) > 0.01 {
		t.Fatalf("fall stage2 = %v", fall.Stage2Pct)
	}
	if !(*rise.IndexRS26Pct > 0 && *fall.IndexRS26Pct < 0) {
		t.Fatalf("index RS: rise %v fall %v", *rise.IndexRS26Pct, *fall.IndexRS26Pct)
	}
}
