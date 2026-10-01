// Package rotation computes industry rotation, relative strength and Weinstein stages on
// a weekly clock (docs/SECTOR_ROTATION_PRD.md). It is REPORTING: a panel of facts the
// screener's Rotation page shows and the pre-registered backtest reads. It decides nothing.
//
// Definitions (fixed with the PRD; a change is a new version):
//   - weekly bar = the last daily close of the ISO week; weekly volume = the week's sum;
//   - eligible in week w = 60-bar median traded value >= MinTurnover at the end of week w-1
//     (the Rs 10 cr floor of rows 37-40, decided before the week it is used in);
//   - market index = equal-weight weekly return of eligible names, each capped at
//     +-WeeklyReturnCap so one bad print cannot move a thin group;
//   - industry index = the same over an industry's eligible members, when it has at least
//     MinMembers; otherwise that week's return is 0 and the week is marked thin;
//   - RS26 = 26-week return of an index (or stock) minus the market's;
//   - stages = internal/stage on the weekly series (row 10's classifier, unchanged);
//   - leading industry = RS26 rank in the top fifth AND the industry index in Stage 2.
//
// Industry membership is TODAY's Sharpely/NSE classification applied to all history --
// classifications rarely change, but the backtest report names this as a caveat.
package rotation

import (
	"math"
	"sort"
	"time"

	"github.com/ranedk/systrader/internal/bars"
	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/stage"
)

const (
	MinTurnover     = 1e8 // Rs 10 crore, 60-bar median traded value
	TurnoverWindow  = 60
	RSWeeks         = 26
	MinMembers      = 5 // 3 let one stock be a whole 'industry' (Telecom Equipment ranked #1 on 3 names)
	LeadingFraction = 0.2
)

// WeeklyReturnCap bounds one name's weekly return inside an index; a var only so the
// pre-registered no-cap sensitivity can be run.
var WeeklyReturnCap = 0.5

// Membership is one symbol's classification.
type Membership struct {
	Sector, Industry string
}

// Stock is one symbol on the weekly calendar (index i = Panel.Weeks[i]); NaN where absent.
type Stock struct {
	Symbol   string
	Close    []float64
	Volume   []float64
	Eligible []bool // liquid at the END of that week (usable for the following week)
}

// Panel is the weekly universe.
type Panel struct {
	Weeks   []time.Time // last trading date seen in each ISO week, ascending
	Stocks  map[string]*Stock
	weekIdx map[int]int
}

// CompletedWeek is the index of the last week COMPLETED at the close of day t: t's own
// week if t is that week's last trading day, else the week before. -1 if none.
func (p *Panel) CompletedWeek(t time.Time) int {
	k, ok := p.weekIdx[WeekKey(t)]
	if !ok {
		return -1
	}
	if t.Before(p.Weeks[k]) {
		return k - 1
	}
	return k
}

// WeekKey identifies an ISO week.
func WeekKey(t time.Time) int { y, w := t.ISOWeek(); return y*100 + w }

// Builder accumulates daily bar series into a weekly Panel.
type Builder struct {
	weekIndex map[int]int
	weekDate  map[int]time.Time
	stocks    map[string]map[int][3]float64 // week key -> close, volume, eligible(0/1)
}

func NewBuilder() *Builder {
	return &Builder{weekIndex: map[int]int{}, weekDate: map[int]time.Time{}, stocks: map[string]map[int][3]float64{}}
}

// Add folds one symbol's daily bars (ascending) into weekly rows.
func (b *Builder) Add(ser bars.Series) {
	if len(ser.Bars) == 0 {
		return
	}
	turnover := bars.MedianTurnover(ser.Bars, TurnoverWindow)
	rows := map[int][3]float64{}
	for i, bar := range ser.Bars {
		k := WeekKey(bar.Date)
		r := rows[k]
		elig := 0.0
		if !math.IsNaN(turnover[i]) && turnover[i] >= MinTurnover {
			elig = 1
		}
		r[0] = bar.Close
		r[1] += bar.Vol
		r[2] = elig
		rows[k] = r
		if d, ok := b.weekDate[k]; !ok || bar.Date.After(d) {
			b.weekDate[k] = bar.Date
		}
	}
	b.stocks[ser.Symbol] = rows
}

// Panel finalises the weekly calendar.
func (b *Builder) Panel() *Panel {
	keys := make([]int, 0, len(b.weekDate))
	for k := range b.weekDate {
		keys = append(keys, k)
	}
	sort.Ints(keys)
	p := &Panel{Weeks: make([]time.Time, len(keys)), Stocks: map[string]*Stock{}, weekIdx: b.weekIndex}
	for i, k := range keys {
		b.weekIndex[k] = i
		p.Weeks[i] = b.weekDate[k]
	}
	n := len(keys)
	for sym, rows := range b.stocks {
		s := &Stock{Symbol: sym, Close: nanSlice(n), Volume: nanSlice(n), Eligible: make([]bool, n)}
		for k, r := range rows {
			i := b.weekIndex[k]
			s.Close[i], s.Volume[i], s.Eligible[i] = r[0], r[1], r[2] == 1
		}
		p.Stocks[sym] = s
	}
	return p
}

func nanSlice(n int) []float64 {
	out := make([]float64, n)
	for i := range out {
		out[i] = math.NaN()
	}
	return out
}

// Index is an equal-weight weekly index.
type Index struct {
	Level   []float64 // starts at 100
	Members []int     // eligible members contributing each week
}

// weeklyReturn of a stock into week i, or NaN; eligibility is judged at the end of week i-1.
func weeklyReturn(s *Stock, i int) float64 {
	if i == 0 || !s.Eligible[i-1] {
		return math.NaN()
	}
	a, b := s.Close[i-1], s.Close[i]
	if math.IsNaN(a) || math.IsNaN(b) || a <= 0 {
		return math.NaN()
	}
	r := b/a - 1
	return math.Max(-WeeklyReturnCap, math.Min(WeeklyReturnCap, r))
}

// BuildIndex averages members' weekly returns; minMembers 1 for the market.
func BuildIndex(p *Panel, members []*Stock, minMembers int) Index {
	n := len(p.Weeks)
	idx := Index{Level: make([]float64, n), Members: make([]int, n)}
	level := 100.0
	for i := 0; i < n; i++ {
		sum, cnt := 0.0, 0
		for _, s := range members {
			if r := weeklyReturn(s, i); !math.IsNaN(r) {
				sum += r
				cnt++
			}
		}
		if cnt >= minMembers {
			level *= 1 + sum/float64(cnt)
		}
		idx.Level[i], idx.Members[i] = level, cnt
	}
	return idx
}

// Ret is the trailing return over w weeks ending at i, or NaN.
func Ret(levels []float64, i, w int) float64 {
	if i-w < 0 || math.IsNaN(levels[i]) || math.IsNaN(levels[i-w]) || levels[i-w] <= 0 {
		return math.NaN()
	}
	return levels[i]/levels[i-w] - 1
}

// Stages classifies a weekly series on the panel calendar (NaN weeks skipped, results
// re-expanded onto the calendar; StageUnknown where absent).
func Stages(weeks []time.Time, close, volume []float64) []stage.Classification {
	var t []time.Time
	var c, v []float64
	var pos []int
	for i := range weeks {
		if !math.IsNaN(close[i]) && close[i] > 0 {
			t = append(t, weeks[i])
			c = append(c, close[i])
			vv := math.NaN()
			if volume != nil {
				vv = volume[i]
			}
			v = append(v, vv)
			pos = append(pos, i)
		}
	}
	out := make([]stage.Classification, len(weeks))
	if len(t) == 0 {
		return out
	}
	var vol *core.Series
	if volume != nil {
		s := core.New(t, v)
		vol = &s
	}
	cls := stage.Classify(core.New(t, c), vol)
	for k, i := range pos {
		out[i] = cls[k]
	}
	return out
}

// Industry is one industry's weekly state.
type Industry struct {
	Code, Sector string
	Members      []*Stock
	Index        Index
	RS           []float64 // RS26 each week
	Rank         []int     // 1 = strongest; 0 = not ranked (thin or warm-up)
	Stage        []stage.Classification
	Leading      []bool
}

// Universe is everything the view and the backtest read.
type Universe struct {
	Panel      *Panel
	Market     Index
	MarketCls  []stage.Classification
	Industries map[string]*Industry
	StockCls   map[string][]stage.Classification
	StockRS    map[string][]float64
	Membership map[string]Membership
}

// Build computes the universe from a panel and today's membership.
func Build(p *Panel, membership map[string]Membership) *Universe {
	u := &Universe{Panel: p, Industries: map[string]*Industry{}, StockCls: map[string][]stage.Classification{},
		StockRS: map[string][]float64{}, Membership: membership}
	all := make([]*Stock, 0, len(p.Stocks))
	for _, s := range p.Stocks {
		all = append(all, s)
	}
	sort.Slice(all, func(i, j int) bool { return all[i].Symbol < all[j].Symbol })
	u.Market = BuildIndex(p, all, 1)
	u.MarketCls = Stages(p.Weeks, u.Market.Level, nil)

	for _, s := range all {
		m, ok := membership[s.Symbol]
		if !ok || m.Industry == "" {
			continue
		}
		ind := u.Industries[m.Industry]
		if ind == nil {
			ind = &Industry{Code: m.Industry, Sector: m.Sector}
			u.Industries[m.Industry] = ind
		}
		ind.Members = append(ind.Members, s)
	}
	n := len(p.Weeks)
	for _, ind := range u.Industries {
		ind.Index = BuildIndex(p, ind.Members, MinMembers)
		ind.RS = make([]float64, n)
		for i := 0; i < n; i++ {
			ind.RS[i] = Ret(ind.Index.Level, i, RSWeeks) - Ret(u.Market.Level, i, RSWeeks)
			if ind.Index.Members[i] < MinMembers {
				ind.RS[i] = math.NaN()
			}
		}
		ind.Stage = Stages(p.Weeks, ind.Index.Level, nil)
		ind.Rank = make([]int, n)
		ind.Leading = make([]bool, n)
	}
	// ranks and leaders, week by week
	for i := 0; i < n; i++ {
		var ranked []*Industry
		for _, ind := range u.Industries {
			if !math.IsNaN(ind.RS[i]) {
				ranked = append(ranked, ind)
			}
		}
		sort.Slice(ranked, func(a, b int) bool {
			if ranked[a].RS[i] != ranked[b].RS[i] {
				return ranked[a].RS[i] > ranked[b].RS[i]
			}
			return ranked[a].Code < ranked[b].Code
		})
		top := int(math.Ceil(LeadingFraction * float64(len(ranked))))
		for r, ind := range ranked {
			ind.Rank[i] = r + 1
			ind.Leading[i] = r < top && ind.Stage[i].Stage == stage.Stage2Advancing
		}
	}
	for _, s := range all {
		u.StockCls[s.Symbol] = Stages(p.Weeks, s.Close, s.Volume)
		rs := make([]float64, n)
		for i := 0; i < n; i++ {
			rs[i] = Ret(s.Close, i, RSWeeks) - Ret(u.Market.Level, i, RSWeeks)
		}
		u.StockRS[s.Symbol] = rs
	}
	return u
}

// WeeksInStage2 counts consecutive Stage 2 weeks ending at i (0 if not in Stage 2).
func WeeksInStage2(cls []stage.Classification, i int) int {
	n := 0
	for k := i; k >= 0 && cls[k].Stage == stage.Stage2Advancing; k-- {
		n++
	}
	return n
}

// RunStats are the "how long does it play out" measurements.
type RunStats struct {
	Stage2Weeks          Quantiles // completed Stage 2 runs of names eligible when the run began
	Stage2Runs           int
	LeadershipWeeks      Quantiles // consecutive weeks an industry stays leading (top fifth AND Stage 2)
	LeadershipRuns       int
	TopFifthWeeks        Quantiles // consecutive weeks in the RS top fifth alone (no stage condition)
	NewLeadersPerQuarter float64   // industries newly leading, per 13 weeks
	FromWeek             time.Time
}

// Quantiles of a run-length distribution, in weeks.
type Quantiles struct{ P50, P75, P90, Mean float64 }

func quantiles(v []float64) Quantiles {
	if len(v) == 0 {
		return Quantiles{math.NaN(), math.NaN(), math.NaN(), math.NaN()}
	}
	sort.Float64s(v)
	sum := 0.0
	for _, x := range v {
		sum += x
	}
	at := func(q float64) float64 { return v[int(q*float64(len(v)-1))] }
	return Quantiles{at(0.5), at(0.75), at(0.9), sum / float64(len(v))}
}

// Stats measures run lengths over weeks [from, end) of the panel.
func (u *Universe) Stats(from time.Time) RunStats {
	p := u.Panel
	start := sort.Search(len(p.Weeks), func(i int) bool { return !p.Weeks[i].Before(from) })
	var s2 []float64
	for sym, cls := range u.StockCls {
		st := p.Stocks[sym]
		run, began := 0, -1
		for i := start; i < len(cls); i++ {
			if cls[i].Stage == stage.Stage2Advancing {
				if run == 0 {
					began = i
				}
				run++
				continue
			}
			if run > 0 && began > 0 && st.Eligible[began-1] {
				s2 = append(s2, float64(run))
			}
			run = 0
		}
	}
	var lead, top []float64
	entries := 0
	for _, ind := range u.Industries {
		run, trun := 0, 0
		for i := start; i < len(p.Weeks); i++ {
			ranked := 0
			for _, o := range u.Industries {
				if o.Rank[i] > 0 {
					ranked++
				}
			}
			inTop := ind.Rank[i] > 0 && ind.Rank[i] <= int(math.Ceil(LeadingFraction*float64(ranked)))
			if inTop {
				trun++
			} else if trun > 0 {
				top = append(top, float64(trun))
				trun = 0
			}
			if ind.Leading[i] {
				if run == 0 && i > start {
					entries++
				}
				run++
				continue
			}
			if run > 0 {
				lead = append(lead, float64(run))
			}
			run = 0
		}
	}
	weeks := float64(len(p.Weeks) - start)
	out := RunStats{Stage2Weeks: quantiles(s2), Stage2Runs: len(s2), LeadershipWeeks: quantiles(lead),
		LeadershipRuns: len(lead), TopFifthWeeks: quantiles(top), FromWeek: from}
	if weeks > 0 {
		out.NewLeadersPerQuarter = float64(entries) / weeks * 13
	}
	return out
}
