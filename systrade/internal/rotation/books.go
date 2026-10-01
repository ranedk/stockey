package rotation

import (
	"math"
	"sort"

	"github.com/ranedk/systrader/internal/stage"
)

// The pre-registered books (research/preregistrations/2026-10-01_industry_rotation.md).
const (
	BookSize        = 20
	PerIndustryCap  = 4
	RebalanceWeeks  = 4
	KeepMultiple    = 2 // keep while in the top 2 x BookSize, industry in the top 2 x LeadingFraction
)

// BookRule selects one of the books.
type BookRule struct {
	Industry     bool // R1/R2: entry only in leading industries, keep only in top-two-fifths industries
	MarketFilter bool // R2: hold nothing while the market index is in Stage 4
}

// BookWeek is one week's decision.
type BookWeek struct {
	Held       []string
	Rebalanced bool
	MarketOff  bool
}

type ranked struct {
	sym string
	rs  float64
	ind string
}

// Holdings walks the weeks from start and returns each week's holdings, decided at that
// week's close from data through it.
func (u *Universe) Holdings(rule BookRule, start int) []BookWeek {
	p := u.Panel
	out := make([]BookWeek, len(p.Weeks))
	held := map[string]bool{}
	wasOff := false
	for i := start; i < len(p.Weeks); i++ {
		// between rebalances: sell on a weekly close below the 30-week MA, or when it stops trading
		for s := range held {
			c := u.StockCls[s][i]
			if math.IsNaN(p.Stocks[s].Close[i]) || c.Stage == stage.StageUnknown || c.Close < c.MA30 {
				delete(held, s)
			}
		}
		if rule.MarketFilter && u.MarketCls[i].Stage == stage.Stage4Declining {
			held = map[string]bool{}
			wasOff = true
			out[i] = BookWeek{MarketOff: true}
			continue
		}
		rebalance := (i-start)%RebalanceWeeks == 0 || wasOff
		wasOff = false
		if rebalance {
			u.rebalance(rule, i, held)
		}
		w := BookWeek{Rebalanced: rebalance}
		for s := range held {
			w.Held = append(w.Held, s)
		}
		sort.Strings(w.Held)
		out[i] = w
	}
	return out
}

// industryCut is the rank at or above which an industry is inside `fraction` of the ranked ones.
func (u *Universe) industryCut(i int, fraction float64) int {
	n := 0
	for _, ind := range u.Industries {
		if ind.Rank[i] > 0 {
			n++
		}
	}
	return int(math.Ceil(fraction * float64(n)))
}

func (u *Universe) stage2List(i int, ok func(ind string) bool) []ranked {
	var out []ranked
	for sym, s := range u.Panel.Stocks {
		if !s.Eligible[i] || u.StockCls[sym][i].Stage != stage.Stage2Advancing {
			continue
		}
		rs := u.StockRS[sym][i]
		if math.IsNaN(rs) {
			continue
		}
		ind := u.Membership[sym].Industry
		if !ok(ind) {
			continue
		}
		out = append(out, ranked{sym, rs, ind})
	}
	sort.Slice(out, func(a, b int) bool {
		if out[a].rs != out[b].rs {
			return out[a].rs > out[b].rs
		}
		return out[a].sym < out[b].sym
	})
	return out
}

func (u *Universe) rebalance(rule BookRule, i int, held map[string]bool) {
	keepCut := u.industryCut(i, KeepMultiple*LeadingFraction)
	inKeepIndustry := func(ind string) bool {
		if !rule.Industry {
			return true
		}
		x := u.Industries[ind]
		return x != nil && x.Rank[i] > 0 && x.Rank[i] <= keepCut
	}
	keep := map[string]bool{}
	for k, r := range u.stage2List(i, inKeepIndustry) {
		if k >= KeepMultiple*BookSize {
			break
		}
		keep[r.sym] = true
	}
	for s := range held {
		if !keep[s] || !u.Panel.Stocks[s].Eligible[i] {
			delete(held, s)
		}
	}
	perInd := map[string]int{}
	for s := range held {
		if ind := u.Membership[s].Industry; ind != "" {
			perInd[ind]++
		}
	}
	entryOK := func(ind string) bool {
		if !rule.Industry {
			return true
		}
		x := u.Industries[ind]
		return x != nil && x.Leading[i]
	}
	for _, r := range u.stage2List(i, entryOK) {
		if len(held) >= BookSize {
			break
		}
		if held[r.sym] {
			continue
		}
		if r.ind != "" && perInd[r.ind] >= PerIndustryCap {
			continue
		}
		held[r.sym] = true
		if r.ind != "" {
			perInd[r.ind]++
		}
	}
}
