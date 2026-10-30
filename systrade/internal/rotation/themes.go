package rotation

// Theme price views for stockey's thematic watchlist (stockey docs/THEMATIC_WATCHLIST_PRD.md
// section 4.3-4.4, build step 5). REPORTING, like the rest of this package: stockey owns the
// theme map and the ranking; systrader supplies only what is price-based --
//   - a theme index: the exposure-weighted weekly return of members liquid at the end of the
//     week before (60-bar median traded value >= ThemeMinTurnover, the watchlist's Rs 1 cr
//     floor), each capped at +-WeeklyReturnCap; a week with fewer than MinMembers such
//     members counts 0% and the theme's RS is not reported;
//   - breadth: exposure-weighted share of members with a stage that are in Stage 2, and the
//     share above their 30-week average;
//   - per stock: stage, weeks in it, RS26 against the same equal-weight market as the
//     rotation view, distance from the 30-week average, from the 52-week high and from the
//     breakout level, and a trend-and-breakout score.
//
// Trend-and-breakout score = tier + RS26 percentile among all theme members (0..0.99):
//   tier 4  early Stage 2: in Stage 2 for at most EarlyStage2Weeks and no more than
//           MaxEarlyExtension above the 30-week average (broke out recently, not stretched)
//   tier 3  any other Stage 2
//   tier 2  Stage 1 (a base; may break out)
//   tier 1  Stage 3
//   tier 0  Stage 4 or no stage
// so an early Stage 2 always outranks an extended one, and Stage 3/4 rank last (PRD 4.4).

import (
	"math"
	"sort"

	"github.com/ranedk/systrader/internal/stage"
)

const (
	ThemeMinTurnover    = 1e7 // Rs 1 crore
	EarlyStage2Weeks    = 13
	MaxEarlyExtension   = 0.25
	BaseLookbackWeeks   = 26 // a breakout is a close above the prior 26-week high
	BreakoutSearchWeeks = 13 // how far before the confirmed Stage 2 to look for it
	HighWeeks           = 52
	RetGapWeeks         = 2 // nearest close for a stock's RS when the exact week has none
)

// ThemeMember is one stock in one theme, as stockey's map gives it.
type ThemeMember struct {
	Key    string  // NSE symbol or "BSE:<scrip>"
	Weight float64 // revenue share in the theme (0..1); stockey uses 0.5 when unknown
}

type ThemeView struct {
	ThemeID       string    `json:"theme_id"`
	Members       int       `json:"members"`
	WithStage     int       `json:"with_stage"`
	IndexMembers  int       `json:"index_members"` // liquid members in the index this week
	IndexStage    int       `json:"index_stage"`
	IndexRS26Pct  *float64  `json:"index_rs26_pct"`
	IndexRet13Pct *float64  `json:"index_return_13w_pct"`
	Stage2Pct     *float64  `json:"stage2_pct"`     // exposure-weighted
	AboveMA30Pct  *float64  `json:"above_ma30_pct"` // exposure-weighted share above the 30-week average
	EarlyStage2   int       `json:"early_stage2"`   // members in tier 4
	IndexHistory  []float64 `json:"index_history"`  // last 52 weekly levels, oldest first
}

type ThemeStockView struct {
	Key             string   `json:"key"`
	Themes          []string `json:"themes"`
	Close           float64  `json:"close"`
	Stage           int      `json:"stage"`
	StageLabel      string   `json:"stage_label"`
	WeeksInStage    int      `json:"weeks_in_stage"`
	RS26Pct         *float64 `json:"rs26_pct"`
	AboveMA30Pct    *float64 `json:"above_ma30_pct"`
	FromHigh52Pct   *float64 `json:"from_52w_high_pct"` // <= 0
	BreakoutLevel   *float64 `json:"breakout_level"`    // Stage 2: the base's high; Stage 1: the current base's high
	FromBreakoutPct *float64 `json:"from_breakout_pct"`
	VolumeRatio     *float64 `json:"volume_ratio"`
	Turnover60Cr    *float64 `json:"turnover_60d_cr"`
	Liquid          bool     `json:"liquid"`
	Tier            int      `json:"tier"`
	TrendScore      *float64 `json:"trend_score"`
}

type ThemeSnapshot struct {
	AsOf   string           `json:"as_of"`
	Themes []ThemeView      `json:"themes"`
	Stocks []ThemeStockView `json:"stocks"`
	// Missing lists member keys with no price series at all (not listed long, or a key
	// systrader does not know) -- shown, not silently dropped.
	Missing []string `json:"missing"`
}

func weeksInSameStage(cls []stage.Classification, i int) int {
	n := 0
	for k := i; k >= 0 && cls[k].Stage == cls[i].Stage; k-- {
		n++
	}
	return n
}

func maxClose(c []float64, from, to int) float64 {
	m := math.NaN()
	for k := max(from, 0); k <= to && k < len(c); k++ {
		if !math.IsNaN(c[k]) && (math.IsNaN(m) || c[k] > m) {
			m = c[k]
		}
	}
	return m
}

// breakoutLevel of a Stage 2 run that the classifier confirmed at week start: the stage lags
// the move (the 30-week average must turn up first), so the breakout is the FIRST close above
// its prior BaseLookbackWeeks-week high in the BreakoutSearchWeeks up to start, and the level is
// that high. With no such close (a slow grind into Stage 2) it is the high of the weeks before start.
func breakoutLevel(c []float64, start int) float64 {
	for b := max(start-BreakoutSearchWeeks, 1); b <= start; b++ {
		prior := maxClose(c, b-BaseLookbackWeeks, b-1)
		if !math.IsNaN(c[b]) && prior > 0 && c[b] > prior {
			return prior
		}
	}
	return maxClose(c, start-BaseLookbackWeeks, start-1)
}

// retNear is Ret for one stock's closes, taking the nearest close within RetGapWeeks of the
// start week when that week has none (a suspension, or a week traded only in another series):
// one missing bar must not leave a staged stock without relative strength.
func retNear(c []float64, i, w int) float64 {
	if i >= len(c) || math.IsNaN(c[i]) || c[i] <= 0 {
		return math.NaN()
	}
	for d := 0; d <= RetGapWeeks; d++ {
		for _, k := range []int{i - w - d, i - w + d} {
			if k >= 0 && k < i && !math.IsNaN(c[k]) && c[k] > 0 {
				return c[i]/c[k] - 1
			}
		}
	}
	return math.NaN()
}

func tierOf(cls stage.Classification, weeks int) int {
	switch cls.Stage {
	case stage.Stage2Advancing:
		if weeks <= EarlyStage2Weeks && cls.MA30 > 0 && cls.Close/cls.MA30-1 <= MaxEarlyExtension {
			return 4
		}
		return 3
	case stage.Stage1Basing:
		return 2
	case stage.Stage3Topping:
		return 1
	}
	return 0
}

// BuildThemes renders week i. market is the equal-weight market index (Universe.Market).
func BuildThemes(p *Panel, market Index, themes map[string][]ThemeMember, i int) ThemeSnapshot {
	snap := ThemeSnapshot{AsOf: p.Weeks[i].Format("2006-01-02"), Missing: []string{}}
	stocks := map[string]*ThemeStockView{}
	cls := map[string][]stage.Classification{}
	var rsKeys, staged []string
	ids := make([]string, 0, len(themes))
	for id := range themes {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	missing := map[string]bool{}
	for _, id := range ids {
		for _, m := range themes[id] {
			s := p.Stocks[m.Key]
			if s == nil {
				missing[m.Key] = true
				continue
			}
			if sv := stocks[m.Key]; sv != nil {
				sv.Themes = append(sv.Themes, id)
				continue
			}
			c := Stages(p.Weeks, s.Close, s.Volume)
			cls[m.Key] = c
			sv := &ThemeStockView{Key: m.Key, Themes: []string{id}}
			stocks[m.Key] = sv
			if math.IsNaN(s.Close[i]) {
				continue // no bar this week: listed but not trading
			}
			cur := c[i]
			sv.Close = math.Round(s.Close[i]*100) / 100
			sv.Stage, sv.StageLabel = int(cur.Stage), cur.Stage.String()
			sv.Turnover60Cr = ptr(s.Turnover[i] / 1e7)
			sv.Liquid = s.Turnover[i] >= ThemeMinTurnover
			if cur.Stage == stage.StageUnknown {
				continue
			}
			sv.WeeksInStage = weeksInSameStage(c, i)
			sv.RS26Pct = pct(retNear(s.Close, i, RSWeeks) - Ret(market.Level, i, RSWeeks))
			sv.VolumeRatio = ptr(cur.VolumeRatio)
			if cur.MA30 > 0 {
				sv.AboveMA30Pct = pct(cur.Close/cur.MA30 - 1)
			}
			if h := maxClose(s.Close, i-HighWeeks+1, i); h > 0 {
				sv.FromHigh52Pct = pct(s.Close[i]/h - 1)
			}
			var base float64 = math.NaN()
			switch cur.Stage {
			case stage.Stage2Advancing:
				base = breakoutLevel(s.Close, i-sv.WeeksInStage+1)
			case stage.Stage1Basing:
				base = maxClose(s.Close, i-max(sv.WeeksInStage, 2)+1, i-1)
			}
			if base > 0 {
				sv.BreakoutLevel = ptr(base)
				sv.FromBreakoutPct = pct(s.Close[i]/base - 1)
			}
			sv.Tier = tierOf(cur, sv.WeeksInStage)
			staged = append(staged, m.Key)
			if sv.RS26Pct != nil {
				rsKeys = append(rsKeys, m.Key)
			}
		}
	}
	// a staged stock with no RS (too short a history) scores its tier alone
	for _, k := range staged {
		score := float64(stocks[k].Tier)
		stocks[k].TrendScore = &score
	}
	// RS percentile among members with an RS (ties share the lower rank)
	sort.Slice(rsKeys, func(a, b int) bool { return *stocks[rsKeys[a]].RS26Pct < *stocks[rsKeys[b]].RS26Pct })
	for r, k := range rsKeys {
		sv := stocks[k]
		frac := 0.0
		if len(rsKeys) > 1 {
			frac = 0.99 * float64(r) / float64(len(rsKeys)-1)
		}
		score := math.Round((float64(sv.Tier)+frac)*1000) / 1000
		sv.TrendScore = &score
	}

	for _, id := range ids {
		mem := themes[id]
		tv := ThemeView{ThemeID: id, Members: len(mem)}
		var wAll, wS2, wAbove float64
		level := 100.0
		levels := make([]float64, len(p.Weeks))
		for w := range p.Weeks {
			sum, wsum, n := 0.0, 0.0, 0
			for _, m := range mem {
				s := p.Stocks[m.Key]
				if s == nil || w == 0 || !(s.Turnover[w-1] >= ThemeMinTurnover) {
					continue
				}
				a, b := s.Close[w-1], s.Close[w]
				if math.IsNaN(a) || math.IsNaN(b) || a <= 0 {
					continue
				}
				r := math.Max(-WeeklyReturnCap, math.Min(WeeklyReturnCap, b/a-1))
				sum += m.Weight * r
				wsum += m.Weight
				n++
			}
			if n >= MinMembers && wsum > 0 {
				level *= 1 + sum/wsum
			}
			levels[w] = level
			if w == i {
				tv.IndexMembers = n
			}
		}
		if tv.IndexMembers >= MinMembers {
			tv.IndexRS26Pct = pct(Ret(levels, i, RSWeeks) - Ret(market.Level, i, RSWeeks))
			tv.IndexRet13Pct = pct(Ret(levels, i, 13))
			tv.IndexStage = int(Stages(p.Weeks, levels, nil)[i].Stage)
		}
		for k := max(0, i-HighWeeks+1); k <= i; k++ {
			tv.IndexHistory = append(tv.IndexHistory, math.Round(levels[k]*100)/100)
		}
		for _, m := range mem {
			sv := stocks[m.Key]
			if sv == nil || sv.Stage == int(stage.StageUnknown) {
				continue
			}
			tv.WithStage++
			wAll += m.Weight
			if sv.Stage == int(stage.Stage2Advancing) {
				wS2 += m.Weight
			}
			if sv.AboveMA30Pct != nil && *sv.AboveMA30Pct > 0 {
				wAbove += m.Weight
			}
			if sv.Tier == 4 {
				tv.EarlyStage2++
			}
		}
		if tv.WithStage >= MinMembers && wAll > 0 {
			tv.Stage2Pct = pct(wS2 / wAll)
			tv.AboveMA30Pct = pct(wAbove / wAll)
		}
		snap.Themes = append(snap.Themes, tv)
	}
	keys := make([]string, 0, len(stocks))
	for k := range stocks {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		snap.Stocks = append(snap.Stocks, *stocks[k])
	}
	for k := range missing {
		snap.Missing = append(snap.Missing, k)
	}
	sort.Strings(snap.Missing)
	return snap
}
