package rotation

import (
	"math"
	"sort"
	"time"

	"github.com/ranedk/systrader/internal/stage"
)

// StatsFrom is where the "how long does it play out" measurements start (the tests' window).
var StatsFrom = time.Date(2013, 7, 1, 0, 0, 0, 0, time.UTC)

// HistoryWeeks of rank history kept per industry for the rotation chart.
const HistoryWeeks = 26

type MarketView struct {
	Stage          int     `json:"stage"`
	StageLabel     string  `json:"stage_label"`
	BreadthPct     float64 `json:"breadth_pct"` // eligible names in Stage 2
	EligibleCount  int     `json:"eligible_count"`
	Return26Pct    *float64 `json:"return_26w_pct"`
}

type IndustryView struct {
	Code            string  `json:"code"`
	Name            string  `json:"name"`
	SectorCode      string  `json:"sector_code"`
	SectorName      string  `json:"sector_name"`
	Members         int     `json:"members"`
	EligibleMembers int     `json:"eligible_members"`
	RS26Pct         *float64 `json:"rs26_pct"`
	Rank            int     `json:"rank"`      // 0 = not ranked (thin)
	Rank4w          int     `json:"rank_4w"`
	Rank13w         int     `json:"rank_13w"`
	RankHistory     []int   `json:"rank_history"` // oldest first, HistoryWeeks long
	Stage           int     `json:"stage"`
	Stage2Pct       float64 `json:"stage2_pct"` // eligible members in Stage 2
	Leading         bool    `json:"leading"`
	WeeksLeading    int     `json:"weeks_leading"`
}

type StockView struct {
	Symbol        string   `json:"symbol"`
	IndustryCode  string   `json:"industry_code"`
	Close         float64  `json:"close"`
	Stage         int      `json:"stage"`
	WeeksInStage2 int      `json:"weeks_in_stage2"`
	RS26Pct       *float64 `json:"rs26_pct"`
	RankInIndustry int     `json:"rank_in_industry"` // by RS26 among eligible members
	AboveMA30Pct  *float64 `json:"above_ma30_pct"`
	SlopePct      *float64 `json:"slope_pct"`
	VolumeRatio   *float64 `json:"volume_ratio"`
	Eligible      bool     `json:"eligible"`
	Candidate     bool     `json:"candidate"` // eligible, Stage 2, in a leading industry
}

type QuantilesView struct {
	P50  *float64 `json:"p50"`
	P75  *float64 `json:"p75"`
	P90  *float64 `json:"p90"`
	Mean *float64 `json:"mean"`
}

func qview(q Quantiles) QuantilesView { return QuantilesView{ptr(q.P50), ptr(q.P75), ptr(q.P90), ptr(q.Mean)} }

type StatsView struct {
	Stage2Weeks          QuantilesView `json:"stage2_weeks"`
	Stage2Runs           int           `json:"stage2_runs"`
	LeadershipWeeks      QuantilesView `json:"leadership_weeks"`
	LeadershipRuns       int           `json:"leadership_runs"`
	TopFifthWeeks        QuantilesView `json:"top_fifth_weeks"`
	NewLeadersPerQuarter *float64      `json:"new_leaders_per_quarter"`
	From                 string        `json:"from"`
}

type Snapshot struct {
	AsOf       string         `json:"as_of"`
	Market     MarketView     `json:"market"`
	Industries []IndustryView `json:"industries"`
	Stocks     []StockView    `json:"stocks"`
	Stats      StatsView      `json:"stats"`
}

func ptr(v float64) *float64 {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return nil
	}
	r := math.Round(v*100) / 100
	return &r
}

func pct(v float64) *float64 { return ptr(v * 100) }

// Snapshot renders week i (normally the last) for the view.
func (u *Universe) Snapshot(i int, names map[string]string) Snapshot {
	p := u.Panel
	snap := Snapshot{AsOf: p.Weeks[i].Format("2006-01-02")}
	mc := u.MarketCls[i]
	snap.Market = MarketView{Stage: int(mc.Stage), StageLabel: mc.Stage.String(), Return26Pct: pct(Ret(u.Market.Level, i, RSWeeks))}

	inStage2, eligible := 0, 0
	stocksByInd := map[string][]StockView{}
	for sym, s := range p.Stocks {
		if math.IsNaN(s.Close[i]) {
			continue // not trading this week
		}
		cls := u.StockCls[sym][i]
		el := s.Eligible[i]
		if el {
			eligible++
			if cls.Stage == stage.Stage2Advancing {
				inStage2++
			}
		}
		m, ok := u.Membership[sym]
		if !ok {
			continue
		}
		sv := StockView{Symbol: sym, IndustryCode: m.Industry, Close: math.Round(s.Close[i]*100) / 100, Stage: int(cls.Stage),
			WeeksInStage2: WeeksInStage2(u.StockCls[sym], i), RS26Pct: pct(u.StockRS[sym][i]),
			SlopePct: ptr(cls.MASlopePct), VolumeRatio: ptr(cls.VolumeRatio), Eligible: el}
		if cls.MA30 > 0 {
			sv.AboveMA30Pct = pct(cls.Close/cls.MA30 - 1)
		}
		if ind := u.Industries[m.Industry]; ind != nil {
			sv.Candidate = el && cls.Stage == stage.Stage2Advancing && ind.Leading[i]
		}
		stocksByInd[m.Industry] = append(stocksByInd[m.Industry], sv)
	}
	if eligible > 0 {
		snap.Market.BreadthPct = math.Round(float64(inStage2)/float64(eligible)*1000) / 10
	}
	snap.Market.EligibleCount = eligible

	for code, ind := range u.Industries {
		iv := IndustryView{Code: code, Name: names[code], SectorCode: ind.Sector, SectorName: names[ind.Sector],
			Members: len(ind.Members), EligibleMembers: ind.Index.Members[i], RS26Pct: pct(ind.RS[i]),
			Rank: ind.Rank[i], Stage: int(ind.Stage[i].Stage), Leading: ind.Leading[i]}
		if i >= 4 {
			iv.Rank4w = ind.Rank[i-4]
		}
		if i >= 13 {
			iv.Rank13w = ind.Rank[i-13]
		}
		for k := i - HistoryWeeks + 1; k <= i; k++ {
			if k >= 0 {
				iv.RankHistory = append(iv.RankHistory, ind.Rank[k])
			}
		}
		for k := i; k >= 0 && ind.Leading[k]; k-- {
			iv.WeeksLeading++
		}
		members := stocksByInd[code]
		el, s2 := 0, 0
		var eligibleMembers []*StockView
		for k := range members {
			if members[k].Eligible {
				el++
				eligibleMembers = append(eligibleMembers, &members[k])
				if members[k].Stage == int(stage.Stage2Advancing) {
					s2++
				}
			}
		}
		if el > 0 {
			iv.Stage2Pct = math.Round(float64(s2)/float64(el)*1000) / 10
		}
		sort.Slice(eligibleMembers, func(a, b int) bool { return rsOf(eligibleMembers[a]) > rsOf(eligibleMembers[b]) })
		for r, m := range eligibleMembers {
			m.RankInIndustry = r + 1
		}
		snap.Industries = append(snap.Industries, iv)
		snap.Stocks = append(snap.Stocks, members...)
	}
	sort.Slice(snap.Industries, func(a, b int) bool {
		ra, rb := snap.Industries[a].Rank, snap.Industries[b].Rank
		if (ra == 0) != (rb == 0) {
			return rb == 0
		}
		if ra != rb {
			return ra < rb
		}
		return snap.Industries[a].Code < snap.Industries[b].Code
	})
	sort.Slice(snap.Stocks, func(a, b int) bool {
		if snap.Stocks[a].Candidate != snap.Stocks[b].Candidate {
			return snap.Stocks[a].Candidate
		}
		return rsOf(&snap.Stocks[a]) > rsOf(&snap.Stocks[b])
	})

	st := u.Stats(StatsFrom)
	snap.Stats = StatsView{Stage2Weeks: qview(st.Stage2Weeks), Stage2Runs: st.Stage2Runs,
		LeadershipWeeks: qview(st.LeadershipWeeks), LeadershipRuns: st.LeadershipRuns, TopFifthWeeks: qview(st.TopFifthWeeks),
		NewLeadersPerQuarter: ptr(st.NewLeadersPerQuarter), From: StatsFrom.Format("2006-01-02")}
	return snap
}

func rsOf(s *StockView) float64 {
	if s.RS26Pct == nil {
		return math.Inf(-1)
	}
	return *s.RS26Pct
}
