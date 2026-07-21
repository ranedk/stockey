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
	IDM               float64            // instrument diversification multiplier (≤ 2.5)

	VolSpan int // EWMA span for price vol; default 36 (≈ 25-day window)
	Workers int // parallelism for per-instrument prep; default GOMAXPROCS
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
}

type Result struct {
	Config      Config
	Daily       core.Series // portfolio daily cash P&L on the union calendar
	EndCapital  float64
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

	dailyPnL := make([]float64, len(days))
	for di, day := range days {
		var dayPnL float64
		// Mark-to-market with positions decided at the previous close.
		for pi, p := range preps {
			i, ok := p.dateIdx[day]
			if !ok {
				continue
			}
			price := p.inst.Prices.Values[i]
			st := &state[pi]
			if st.hasPrice && st.pos != 0 {
				move := (price - st.lastPrice) * p.inst.Meta.PointValue * st.pos
				irs[pi].PnL.Values[i] += move
				dayPnL += move
			}
			st.lastPrice, st.hasPrice = price, true
		}
		if cfg.Compounding {
			capital += dayPnL
			if capital < 0 {
				capital = 0 // busted; positions will go to zero below
			}
		}
		dailyCashVolTarget := sizing.DailyCashVolTarget(capital, cfg.VolTargetPct)

		// Decide new positions at today's close (info ≤ today only).
		for pi, p := range preps {
			i, ok := p.dateIdx[day]
			if !ok {
				continue
			}
			st := &state[pi]
			price := p.inst.Prices.Values[i]
			ivv := p.vol.Values[i] * p.inst.Meta.PointValue
			vs := sizing.VolScalar(dailyCashVolTarget, ivv)
			sub := sizing.Subsystem(vs, p.forecast.Values[i])
			w := cfg.InstrumentWeights[p.inst.Meta.Symbol]
			target := portfolio.Target(sub, w, cfg.IDM)
			newPos := portfolio.ApplyInertia(st.pos, target, p.inst.Meta.Block)

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
		dailyPnL[di] = dayPnL
	}

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
	res.EndCapital = cfg.Capital + total(dailyPnL)
	res.Metrics = ComputeMetrics(res, cfg.Capital)
	return res, nil
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
