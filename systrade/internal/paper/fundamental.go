package paper

import (
	"sort"
	"time"
)

// FundamentalPass is the stockey filter of a FundamentalFilter book
// (docs/strategies/2026-10-01_stage2_rs_leaders_clean.md): on each score date, a symbol
// passes when it has a score, no flaw, and a score at or above that date's median.
// Only the newest score_version on a date counts.
type FundamentalPass struct {
	dates []time.Time
	pass  []map[string]bool
}

// FilterRow is one input row (store.StoryFilterRow, decoupled from the store package).
type FilterRow struct {
	Date    time.Time
	Symbol  string
	Score   float64
	HasFlaw bool
	Version int
}

// NewFundamentalPass builds the per-date pass sets.
func NewFundamentalPass(rows []FilterRow) *FundamentalPass {
	byDate := map[time.Time][]FilterRow{}
	for _, r := range rows {
		byDate[r.Date] = append(byDate[r.Date], r)
	}
	fp := &FundamentalPass{}
	for d := range byDate {
		fp.dates = append(fp.dates, d)
	}
	sort.Slice(fp.dates, func(i, j int) bool { return fp.dates[i].Before(fp.dates[j]) })
	for _, d := range fp.dates {
		rs := byDate[d]
		top := 0
		for _, r := range rs {
			if r.Version > top {
				top = r.Version
			}
		}
		var scores []float64
		for _, r := range rs {
			if r.Version == top {
				scores = append(scores, r.Score)
			}
		}
		sort.Float64s(scores)
		median := scores[len(scores)/2]
		pass := map[string]bool{}
		for _, r := range rs {
			if r.Version == top && !r.HasFlaw && r.Score >= median {
				pass[r.Symbol] = true
			}
		}
		fp.pass = append(fp.pass, pass)
	}
	return fp
}

// Passes says whether sym passes on the newest score date on or before t, and that date.
// No score date yet -> nothing passes (zero date returned).
func (f *FundamentalPass) Passes(sym string, t time.Time) (bool, time.Time) {
	i := sort.Search(len(f.dates), func(i int) bool { return f.dates[i].After(t) }) - 1
	if i < 0 {
		return false, time.Time{}
	}
	return f.pass[i][sym], f.dates[i]
}

// Latest is the newest score date, zero if none.
func (f *FundamentalPass) Latest() time.Time {
	if len(f.dates) == 0 {
		return time.Time{}
	}
	return f.dates[len(f.dates)-1]
}
