// Package backtest simulates the full Carver pipeline day by day:
// forecasts → combined forecast → vol targeting → subsystem position →
// portfolio position → rounding+inertia → P&L with costs.
//
// Execution model: positions are decided at the close of day t using only
// information up to t, and earn the price change from t to t+1. Trades pay
// the cost model at the close they are executed.
package backtest

import (
	"fmt"
	"math"
	"sort"
	"time"

	"github.com/ranedk/systrader/internal/combine"
	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/data"
	"github.com/ranedk/systrader/internal/parallel"
	"github.com/ranedk/systrader/internal/portfolio"
	"github.com/ranedk/systrader/internal/rules"
	"github.com/ranedk/systrader/internal/sizing"
)

type RuleSpec struct {
	Rule   rules.Rule
	Weight float64 // forecast weight (handcrafted; must sum ≈ 1 across specs)
}

type Config struct {
	Capital      float64 // initial trading capital, account currency
	VolTargetPct float64 // e.g. 0.20; Law 10 says ≤ 0.5, start ≤ 0.20
	Compounding  bool    // Kelly roll-up: cash vol target tracks current capital

	Rules []RuleSpec // same rule set for all instruments (pooled, Law 4)
	FDM   float64    // forecast diversification multiplier (≤ 2.5)

	InstrumentWeights map[string]float64 // handcrafted, sum ≈ 1
	// IDM: instrument diversification multiplier (≤ 2.5). 0 = derive it
	// point-in-time from realized return correlations (recomputed quarterly
	// on an expanding window; 1.0 until 250 days of history exist).
	IDM float64

	// MaxGrossLeverage caps Σ|position notional| / current capital.
	// 0 = uncapped (futures margin). Cash sleeves must set 1.0: a delivery
	// account cannot borrow, whatever the vol math asks for.
	MaxGrossLeverage float64

	// Schedule: how often positions are re-decided. Daily (default) or
	// Weekly (decide at Friday's close; holiday Fridays skip that week).
	// Between decisions positions are held — cheaper, slower, Law 15's bias.
	Schedule Schedule

	// ExecuteAtOpen: decisions made at close(T) fill at open(T+1). REQUIRES
	// every instrument to have Instrument.Opens -- Run() fails fast if any
	// instrument is missing it (2026-08-22: used to silently fall back to
	// close fills per instrument, reintroducing look-ahead bias with no
	// error). A per-day gap inside an existing Opens series still degrades to
	// a close fill, but is counted via InstrumentResult.DegradedOpenFills.
	// False = legacy model: fill at the decision close itself.
	ExecuteAtOpen bool

	VolSpan int // EWMA span for price vol; default 36 (≈ 25-day window)
	Workers int // parallelism for per-instrument prep; default GOMAXPROCS
}

type Schedule int

const (
	Daily Schedule = iota
	Weekly
)

func (s Schedule) decisionDay(t time.Time) bool {
	if s == Weekly {
		return t.Weekday() == time.Friday
	}
	return true
}

type InstrumentResult struct {
	Symbol       string
	Positions    core.Series // held blocks, per instrument date
	Forecast     core.Series // combined forecast
	PnL          core.Series // daily cash P&L (price move + costs)
	CostCash     float64
	BlocksTraded float64
	AvgAbsPos    float64
	Turnover     float64 // round trips per year of average position
	MaxAbsPos    float64
	// DegradedOpenFills counts pending ExecuteAtOpen fills where the day's
	// Opens value was missing/NaN/<=0 and the close price was used instead --
	// see Run()'s ExecuteAtOpen validation: every instrument is REQUIRED to
	// have an Opens series when ExecuteAtOpen is set, so this only counts
	// per-day gaps inside an existing series, not a structurally-absent one.
	DegradedOpenFills int
}

type Result struct {
	Config      Config
	Daily       core.Series // portfolio daily cash P&L on the union calendar
	Equity      core.Series // end-of-day capital (initial + cumulative P&L)
	EndCapital  float64
	IDMUsed     float64 // last IDM applied (dynamic or configured)
	Instruments map[string]*InstrumentResult
	Metrics     Metrics
}

// prepared holds everything computable per instrument before the daily loop.
type prepared struct {
	inst     *data.Instrument
	forecast core.Series // combined, capped
	vol      core.Series // price-unit daily vol
	dateIdx  map[time.Time]int
}

func Run(cfg Config, instruments []*data.Instrument) (*Result, error) {
	if len(instruments) == 0 {
		return nil, fmt.Errorf("backtest: no instruments")
	}
	if cfg.VolTargetPct <= 0 || cfg.VolTargetPct > 0.5 {
		return nil, fmt.Errorf("backtest: vol target %.2f violates Law 10 (0 < t ≤ 0.5)", cfg.VolTargetPct)
	}
	if len(cfg.Rules) == 0 {
		return nil, fmt.Errorf("backtest: no rules")
	}
	for _, rs := range cfg.Rules {
		if err := rules.Validate(rs.Rule); err != nil {
			return nil, err // Law 1: no story, no backtest
		}
	}
	// BUG FOUND LIVE 2026-08-22 (code review): the pending-fill loop used to
	// silently fall back to same-day-close fills whenever an instrument's
	// Opens was nil, defeating ExecuteAtOpen's whole purpose (Law 7: assume
	// look-ahead first) with no error, warning, or way to tell which
	// instruments were affected. A portfolio mixing instruments with and
	// without Opens got inconsistent execution assumptions per instrument,
	// silently. Fail fast here instead: ExecuteAtOpen requires Opens on every
	// instrument, structurally, before the simulation starts -- the only
	// remaining fallback path (see the pending-fill loop below) is now a
	// genuine per-day data gap inside an existing series, which is counted
	// via DegradedOpenFills rather than silent.
	if cfg.ExecuteAtOpen {
		for _, inst := range instruments {
			if inst.Opens == nil {
				return nil, fmt.Errorf("backtest: ExecuteAtOpen requires Opens on every instrument (missing for %s) -- provide Opens or set ExecuteAtOpen=false; a silent close-fill fallback would quietly reintroduce the look-ahead bias this flag exists to prevent", inst.Meta.Symbol)
			}
		}
	}
	volSpan := cfg.VolSpan
	if volSpan == 0 {
		volSpan = 36
	}

	// Stage 1 (parallel): per-instrument forecasts and volatilities.
	preps := parallel.Map(instruments, cfg.Workers, func(inst *data.Instrument) prepared {
		vol := core.PriceUnitVol(inst.Prices, volSpan, 10)
		fcs := make([]core.Series, len(cfg.Rules))
		wts := make([]float64, len(cfg.Rules))
		for i, rs := range cfg.Rules {
			fcs[i] = rules.Forecast(rs.Rule, inst, vol)
			wts[i] = rs.Weight
		}
		comb := combine.Combined(fcs, wts, cfg.FDM)
		idx := make(map[time.Time]int, inst.Prices.Len())
		for i, t := range inst.Prices.Times {
			idx[t] = i
		}
		return prepared{inst: inst, forecast: comb, vol: vol, dateIdx: idx}
	})

	// Union calendar.
	daySet := map[time.Time]struct{}{}
	for _, p := range preps {
		for _, t := range p.inst.Prices.Times {
			daySet[t] = struct{}{}
		}
	}
	days := make([]time.Time, 0, len(daySet))
	for t := range daySet {
		days = append(days, t)
	}
	sort.Slice(days, func(i, j int) bool { return days[i].Before(days[j]) })

	// Stage 2 (sequential; compounding couples instruments through capital).
	capital := cfg.Capital
	res := &Result{Config: cfg, Instruments: map[string]*InstrumentResult{}}
	state := make([]struct {
		pos       float64
		lastPrice float64
		hasPrice  bool
		pending   bool    // a decision awaits execution at the next open
		target    float64 // the pending unrounded target
	}, len(preps))
	irs := make([]*InstrumentResult, len(preps))
	for i, p := range preps {
		irs[i] = &InstrumentResult{
			Symbol:    p.inst.Meta.Symbol,
			Positions: core.New(p.inst.Prices.Times, filledNaN(p.inst.Prices.Len())),
			Forecast:  p.forecast,
			PnL:       core.New(p.inst.Prices.Times, make([]float64, p.inst.Prices.Len())),
		}
		res.Instruments[p.inst.Meta.Symbol] = irs[i]
	}

	// Dynamic IDM state: per-instrument daily %returns on the union calendar
	// (NaN when absent), correlation recomputed quarterly, expanding window.
	idm := cfg.IDM
	dynamicIDM := idm == 0
	if dynamicIDM {
		idm = 1 // until 250 days of evidence exist (Law 6: don't guess)
	}
	retHist := make([][]float64, len(preps))
	for i := range retHist {
		retHist[i] = filledNaN(len(days))
	}
	normW := normalizedWeights(cfg.InstrumentWeights, preps)

	dailyPnL := make([]float64, len(days))
	equity := make([]float64, len(days))
	targets := make([]float64, len(preps))
	var cumPnL float64
	for di, day := range days {
		var dayPnL float64
		// Phase 1: mark-to-market close→close — unless a pending decision
		// executes at today's open, in which case the old position earns
		// lastClose→open and the new one earns open→close.
		for pi, p := range preps {
			i, ok := p.dateIdx[day]
			if !ok {
				continue
			}
			price := p.inst.Prices.Values[i]
			st := &state[pi]
			pv := p.inst.Meta.PointValue
			if st.hasPrice && st.pending {
				// Opens is guaranteed non-nil here (Run()'s upfront check) --
				// this only degrades to a close fill on a genuine per-day gap
				// (short series, NaN, or <=0) inside an existing series, and
				// that gap is now counted, not silent.
				openP := price
				haveValidOpen := false
				if i < p.inst.Opens.Len() {
					if v := p.inst.Opens.Values[i]; !math.IsNaN(v) && v > 0 {
						openP = v
						haveValidOpen = true
					}
				}
				if !haveValidOpen {
					irs[pi].DegradedOpenFills++
				}
				if st.pos != 0 {
					move := (openP - st.lastPrice) * pv * st.pos
					irs[pi].PnL.Values[i] += move
					dayPnL += move
				}
				newPos := portfolio.ApplyInertia(st.pos, st.target, p.inst.Meta.Block)
				if newPos != st.pos {
					blocks := math.Abs(newPos - st.pos)
					cost := blocks * p.inst.Meta.CostPerBlockCash(openP)
					irs[pi].PnL.Values[i] -= cost
					irs[pi].CostCash += cost
					irs[pi].BlocksTraded += blocks
					dayPnL -= cost
					if cfg.Compounding {
						capital -= cost
					}
					st.pos = newPos
				}
				if st.pos != 0 {
					move := (price - openP) * pv * st.pos
					irs[pi].PnL.Values[i] += move
					dayPnL += move
				}
				st.pending = false
			} else if st.hasPrice && st.pos != 0 {
				move := (price - st.lastPrice) * pv * st.pos
				irs[pi].PnL.Values[i] += move
				dayPnL += move
			}
			if st.hasPrice && st.lastPrice > 0 {
				retHist[pi][di] = price/st.lastPrice - 1
			}
			st.lastPrice, st.hasPrice = price, true
			irs[pi].Positions.Values[i] = st.pos
			if a := math.Abs(st.pos); a > irs[pi].MaxAbsPos {
				irs[pi].MaxAbsPos = a
			}
		}
		if cfg.Compounding {
			capital += dayPnL
			if capital < 0 {
				capital = 0 // busted; positions will go to zero below
			}
		}
		if dynamicIDM && di >= 250 && di%63 == 0 {
			idm = portfolio.IDMFromCorrelation(normW, corrMatrix(retHist, di))
		}
		dailyCashVolTarget := sizing.DailyCashVolTarget(capital, cfg.VolTargetPct)

		// Decision at today's close (info ≤ today only) — on schedule days.
		if cfg.Schedule.decisionDay(day) {
			// Pass 1: unrounded targets.
			for pi, p := range preps {
				targets[pi] = math.NaN()
				i, ok := p.dateIdx[day]
				if !ok {
					continue
				}
				ivv := p.vol.Values[i] * p.inst.Meta.PointValue
				vs := sizing.VolScalar(dailyCashVolTarget, ivv)
				sub := sizing.Subsystem(vs, p.forecast.Values[i])
				w := cfg.InstrumentWeights[p.inst.Meta.Symbol]
				targets[pi] = portfolio.Target(sub, w, idm)
			}

			// Pass 2: gross-leverage cap — a cash account cannot borrow.
			// Scale ALL targets proportionally (preserves relative forecasts).
			if cfg.MaxGrossLeverage > 0 && capital > 0 {
				var gross float64
				for pi, p := range preps {
					i, ok := p.dateIdx[day]
					if !ok || math.IsNaN(targets[pi]) {
						continue
					}
					gross += math.Abs(targets[pi]) * p.inst.Prices.Values[i] * p.inst.Meta.PointValue
				}
				if gross > cfg.MaxGrossLeverage*capital {
					scale := cfg.MaxGrossLeverage * capital / gross
					for pi := range targets {
						targets[pi] *= scale
					}
				}
			}

			// Pass 3: execute. ExecuteAtOpen defers the fill to the next
			// session's open (phase 1 above); legacy mode fills at this close.
			for pi, p := range preps {
				i, ok := p.dateIdx[day]
				if !ok {
					continue
				}
				st := &state[pi]
				if cfg.ExecuteAtOpen {
					st.pending, st.target = true, targets[pi]
					continue
				}
				price := p.inst.Prices.Values[i]
				newPos := portfolio.ApplyInertia(st.pos, targets[pi], p.inst.Meta.Block)
				if newPos != st.pos {
					blocks := math.Abs(newPos - st.pos)
					cost := blocks * p.inst.Meta.CostPerBlockCash(price)
					irs[pi].PnL.Values[i] -= cost
					irs[pi].CostCash += cost
					irs[pi].BlocksTraded += blocks
					dayPnL -= cost
					if cfg.Compounding {
						capital -= cost
					}
					st.pos = newPos
				}
				irs[pi].Positions.Values[i] = st.pos
				if a := math.Abs(st.pos); a > irs[pi].MaxAbsPos {
					irs[pi].MaxAbsPos = a
				}
			}
		}
		dailyPnL[di] = dayPnL
		cumPnL += dayPnL
		if cfg.Compounding {
			equity[di] = capital
		} else {
			equity[di] = cfg.Capital + cumPnL
		}
	}
	res.IDMUsed = idm

	// Per-instrument turnover stats.
	years := float64(len(days)) / 256.0
	for _, ir := range irs {
		var sum float64
		n := 0
		for _, v := range ir.Positions.Values {
			if !math.IsNaN(v) {
				sum += math.Abs(v)
				n++
			}
		}
		if n > 0 {
			ir.AvgAbsPos = sum / float64(n)
		}
		if ir.AvgAbsPos > 0 && years > 0 {
			ir.Turnover = ir.BlocksTraded / (2 * ir.AvgAbsPos) / years
		}
	}

	res.Daily = core.New(days, dailyPnL)
	res.Equity = core.New(days, equity)
	res.EndCapital = cfg.Capital + total(dailyPnL)
	res.Metrics = ComputeMetrics(res, cfg.Capital)
	return res, nil
}

// normalizedWeights returns cfg weights for the loaded instruments, in prep
// order, normalized to sum 1 (equal weights if none configured).
func normalizedWeights(w map[string]float64, preps []prepared) []float64 {
	out := make([]float64, len(preps))
	var sum float64
	for i, p := range preps {
		out[i] = w[p.inst.Meta.Symbol]
		sum += out[i]
	}
	if sum <= 0 {
		for i := range out {
			out[i] = 1 / float64(len(out))
		}
		return out
	}
	for i := range out {
		out[i] /= sum
	}
	return out
}

// corrMatrix computes pairwise Pearson correlations of daily returns using
// history up to (and excluding) day index `until` — point-in-time only.
func corrMatrix(retHist [][]float64, until int) [][]float64 {
	n := len(retHist)
	c := make([][]float64, n)
	for i := range c {
		c[i] = make([]float64, n)
		c[i][i] = 1
	}
	for i := range n {
		for j := i + 1; j < n; j++ {
			c[i][j] = pearson(retHist[i][:until], retHist[j][:until])
			c[j][i] = c[i][j]
		}
	}
	return c
}

func pearson(a, b []float64) float64 {
	var sx, sy, sxx, syy, sxy float64
	n := 0
	for k := range a {
		x, y := a[k], b[k]
		if math.IsNaN(x) || math.IsNaN(y) {
			continue
		}
		sx += x
		sy += y
		sxx += x * x
		syy += y * y
		sxy += x * y
		n++
	}
	if n < 60 { // too little overlap to trust
		return math.NaN()
	}
	fn := float64(n)
	cov := sxy/fn - sx/fn*sy/fn
	vx := sxx/fn - sx/fn*sx/fn
	vy := syy/fn - sy/fn*sy/fn
	if vx <= 0 || vy <= 0 {
		return math.NaN()
	}
	return cov / math.Sqrt(vx*vy)
}

func filledNaN(n int) []float64 {
	v := make([]float64, n)
	for i := range v {
		v[i] = math.NaN()
	}
	return v
}

func total(x []float64) float64 {
	var s float64
	for _, v := range x {
		s += v
	}
	return s
}
