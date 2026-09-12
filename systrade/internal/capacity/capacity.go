// Package capacity turns a paper book of weights into the book a real account
// would hold: whole shares at the day's raw price, Law 12's position inertia,
// and every trade charged what it actually costs at Dhan (internal/costs).
// Pre-registered in research/preregistrations/2026-09-12_capacity.md.
//
// It changes nothing about WHICH names are held — the targets are the frozen
// spec's own (paper.TargetWeights) — only how many rupees, in whole shares,
// and whether a small drift is worth a trade.
package capacity

import (
	"fmt"
	"math"
	"sort"
	"time"

	"github.com/ranedk/systrader/internal/costs"
	"github.com/ranedk/systrader/internal/paper"
)

// Policy is one way of putting capital to work.
type Policy struct {
	Capital float64 // rupees, held constant: targets size off this, not NAV
	// MinPosition lifts every name in the book to at least this many rupees;
	// the total deployed may then exceed Capital. Zero means no floor.
	MinPosition float64
	// Inertia is Law 12: a held name trades only when its share count is more
	// than this fraction away from target. Carver's 10%.
	Inertia float64
	// RefNames, when set, scales Capital at each rebalance by (names in that
	// day's book ÷ RefNames), so a historical position is the size today's
	// book of RefNames names gives it — the pre-registration's "today-sized"
	// run. The universe was far smaller in 2013 than it is now.
	RefNames float64
}

// Label names a policy in reports.
func (p Policy) Label() string {
	s := rupees(p.Capital)
	if p.MinPosition > 0 {
		s += ", min " + rupees(p.MinPosition)
	}
	if p.RefNames > 0 {
		s += ", today-sized"
	}
	return s
}

// RawRatios returns close/adj_close for every symbol on a date: the factor
// from the adjusted price the book is computed in to the price a broker quotes.
type RawRatios func(date time.Time) (map[string]float64, error)

// Rebalance is one decision day's book, after trading.
type Rebalance struct {
	Date      time.Time
	Positions []float64 // rupees per held name, at the fill
}

// Result is one policy's run.
type Result struct {
	Policy   Policy
	Dates    []time.Time
	Gross    []float64 // daily return on deployed rupees, before costs
	Net      []float64 // after costs
	Deployed []float64 // rupees in positions at the close
	Capital  []float64 // the capital the last rebalance sized off

	Statutory, DP, Impact float64 // rupees paid over the run
	Traded                float64 // rupees traded, buys plus sells

	PositionDays, SmallPositionDays int // held names per day; those under 2 shares
	Targeted, RoundedToZero         int // names the spec wanted; those whole shares could not buy
	Rebalances                      []Rebalance
}

type holding struct {
	value  float64 // rupees, marked with adjusted returns
	shares float64 // raw shares at the last trade (for the Law 14 count)
}

// Simulate walks days on the spec's rebalance clock. days must be sorted and
// start on a rebalance day, exactly as paper.Compute takes them. Specs with an
// exposure overlay or a stop are refused: none of the frozen tracks has one,
// and sizing them would need the overlay's own rupee logic.
func Simulate(spec paper.Spec, days []paper.Day, raw RawRatios, pol Policy) (*Result, error) {
	if spec.Overlay != paper.OverlayNone || spec.Stop != paper.StopNone {
		return nil, fmt.Errorf("capacity: %s has an overlay or stop; not supported", spec.Name)
	}
	if spec.RebalanceEvery < 1 || pol.Capital <= 0 {
		return nil, fmt.Errorf("capacity: need a rebalance clock and positive capital")
	}
	res := &Result{Policy: pol}
	hold := map[string]*holding{}
	capital := pol.Capital

	for di, d := range days {
		prices := make(map[string]paper.Obs, len(d.Obs))
		for _, o := range d.Obs {
			prices[o.Symbol] = o
		}

		// Overnight: yesterday's book carried to today's open.
		var prevVal, openVal float64
		for sym, h := range hold {
			prevVal += h.value
			if o, ok := prices[sym]; ok && o.PrevClose > 0 && o.Open > 0 {
				h.value *= o.Open / o.PrevClose
			}
			openVal += h.value
		}

		var cost float64
		if di%spec.RebalanceEvery == 0 {
			ratios, err := raw(d.Date)
			if err != nil {
				return nil, err
			}
			c, k, err := rebalance(res, hold, spec, d, prices, ratios, pol)
			if err != nil {
				return nil, err
			}
			cost, capital = c, k
		}

		var afterVal, closeVal float64
		for sym, h := range hold {
			afterVal += h.value
			if o, ok := prices[sym]; ok && o.Open > 0 && o.Close > 0 {
				h.value *= o.Close / o.Open
			}
			closeVal += h.value
			res.PositionDays++
			if h.shares < 2 {
				res.SmallPositionDays++
			}
		}

		var rPre, rPost, c float64
		if prevVal > 0 {
			rPre = openVal/prevVal - 1
		}
		if afterVal > 0 {
			rPost = closeVal/afterVal - 1
			c = cost / afterVal
		}
		res.Dates = append(res.Dates, d.Date)
		res.Gross = append(res.Gross, (1+rPre)*(1+rPost)-1)
		res.Net = append(res.Net, (1+rPre)*(1-c)*(1+rPost)-1)
		res.Deployed = append(res.Deployed, closeVal)
		res.Capital = append(res.Capital, capital)
	}
	return res, nil
}

// rebalance trades the book to the day's targets at the open and returns the
// rupees it cost, and the capital it sized off.
func rebalance(res *Result, hold map[string]*holding, spec paper.Spec, d paper.Day,
	prices map[string]paper.Obs, ratios map[string]float64, pol Policy) (float64, float64, error) {

	rawOpen := func(sym string) (float64, bool) {
		o, ok := prices[sym]
		r, rok := ratios[sym]
		if !ok || !rok || o.Open <= 0 || r <= 0 {
			return 0, false
		}
		return o.Open * r, true
	}

	weights := paper.TargetWeights(spec, d)
	capital := pol.Capital
	if pol.RefNames > 0 {
		capital *= float64(len(weights)) / pol.RefNames
	}
	target := map[string]float64{} // raw shares
	for sym, w := range weights {
		if w <= 0 {
			continue
		}
		res.Targeted++
		p, ok := rawOpen(sym)
		if !ok {
			return 0, 0, fmt.Errorf("capacity: %s targeted on %s with no raw price", sym, d.Date.Format("2006-01-02"))
		}
		rs := w * capital
		if rs < pol.MinPosition {
			rs = pol.MinPosition
		}
		n := math.Round(rs / p)
		if n == 0 {
			res.RoundedToZero++
			continue
		}
		target[sym] = n
	}

	syms := make([]string, 0, len(hold)+len(target))
	for s := range hold {
		syms = append(syms, s)
	}
	for s := range target {
		if _, ok := hold[s]; !ok {
			syms = append(syms, s)
		}
	}
	sort.Strings(syms)

	var cost float64
	for _, sym := range syms {
		h := hold[sym]
		tgt := target[sym]
		p, ok := rawOpen(sym)
		if !ok {
			// Held but not trading today — suspended or delisted. The weight
			// book drops such a name at its last value on the next rebalance;
			// so does this one, paying the statutory sale and DP charge on it.
			if tgt == 0 && h != nil {
				c := costs.Sell(h.value)
				res.Statutory += c - costs.DPCharge
				res.DP += costs.DPCharge
				res.Traded += h.value
				cost += c
				delete(hold, sym)
			}
			continue
		}
		var cur float64
		if h != nil {
			cur = h.value / p
		}
		trade := (tgt == 0 && cur > 0) || (cur == 0 && tgt > 0) ||
			math.Abs(cur-tgt) > pol.Inertia*tgt
		if !trade {
			if h != nil {
				h.shares = cur // a split changes the count without a trade
			}
			continue
		}
		o := prices[sym]
		dv := (tgt - cur) * p
		imp := costs.Impact(math.Abs(dv), o.Turnover, o.AnnVol/16)
		res.Impact += imp
		res.Traded += math.Abs(dv)
		if dv > 0 {
			c := costs.Buy(dv)
			res.Statutory += c
			cost += c + imp
		} else {
			c := costs.Sell(-dv)
			res.Statutory += c - costs.DPCharge
			res.DP += costs.DPCharge
			cost += c + imp
		}
		if tgt == 0 {
			delete(hold, sym)
			continue
		}
		hold[sym] = &holding{value: tgt * p, shares: tgt}
	}

	rb := Rebalance{Date: d.Date}
	for _, h := range hold {
		rb.Positions = append(rb.Positions, h.value)
	}
	sort.Float64s(rb.Positions)
	res.Rebalances = append(res.Rebalances, rb)
	return cost, capital, nil
}

// Summary is one policy's run set against the weight book it implements.
type Summary struct {
	Names             float64 // median names held after a rebalance
	MedianPos, P10Pos float64 // rupees, pooled over every rebalance
	Deployed          float64 // mean rupees deployed / mean capital sized off
	StatYr, DPYr      float64 // cost per year, fraction of deployed
	ImpactYr          float64
	ModelYr           float64 // the weight book's cost per year at the spec's flat rate
	TurnoverYr        float64 // one-way, per year, against mean deployed
	Small             float64 // share of position-days under 2 shares
	Zero              float64 // share of targeted names whole shares could not buy
	GrossAnn          float64 // the rupee book, annualised, before costs
	IdealAnn          float64 // the weight book, same days, before costs
	TE                float64 // tracking error of gross daily returns, annualised
	MeanDiff          float64 // mean daily difference, annualised
}

// CostYr is the realised cost per year, all three parts.
func (s Summary) CostYr() float64 { return s.StatYr + s.DPYr + s.ImpactYr }

// Fit applies the pre-registered rule: F1 tracking error ≤ 2%/yr, F2 realised
// cost ≤ the flat model's, F3 under 5% of position-days below 2 shares.
func (s Summary) Fit() (f1, f2, f3 bool) {
	return s.TE <= 0.02, s.CostYr() <= s.ModelYr, s.Small < 0.05
}

// Summarize compares a run with the weight book's gross and net records on the
// same days (paper.Compute at zero cost and at the spec's cost).
func Summarize(res *Result, idealGross, idealNet []paper.NavPoint) Summary {
	var s Summary
	g := map[time.Time]float64{}
	for _, p := range idealGross {
		g[p.Date] = p.Return
	}
	var modelCost float64
	for _, p := range idealNet {
		modelCost += p.Cost
	}
	n := float64(len(res.Dates))
	if n == 0 {
		return s
	}
	years := n / 252
	s.ModelYr = modelCost / float64(len(idealNet)) * 252

	var diffs []float64
	var navR, navI = 1.0, 1.0
	var sumDep, sumCap float64
	for i, d := range res.Dates {
		sumDep += res.Deployed[i]
		sumCap += res.Capital[i]
		navR *= 1 + res.Gross[i]
		gi, ok := g[d]
		if !ok {
			continue
		}
		navI *= 1 + gi
		diffs = append(diffs, res.Gross[i]-gi)
	}
	meanDep := sumDep / n
	s.Deployed = sumDep / sumCap
	s.GrossAnn = math.Pow(navR, 1/years) - 1
	s.IdealAnn = math.Pow(navI, 1/years) - 1
	if meanDep > 0 {
		s.StatYr = res.Statutory / meanDep / years
		s.DPYr = res.DP / meanDep / years
		s.ImpactYr = res.Impact / meanDep / years
		s.TurnoverYr = res.Traded / 2 / meanDep / years
	}
	if res.PositionDays > 0 {
		s.Small = float64(res.SmallPositionDays) / float64(res.PositionDays)
	}
	if res.Targeted > 0 {
		s.Zero = float64(res.RoundedToZero) / float64(res.Targeted)
	}
	m, sd := meanSD(diffs)
	s.MeanDiff, s.TE = m*252, sd*math.Sqrt(252)

	var names, pos []float64
	for _, rb := range res.Rebalances {
		names = append(names, float64(len(rb.Positions)))
		pos = append(pos, rb.Positions...)
	}
	s.Names = Quantile(names, 0.5)
	s.MedianPos = Quantile(pos, 0.5)
	s.P10Pos = Quantile(pos, 0.1)
	return s
}

// Quantile is the q-th quantile of x by nearest rank; x need not be sorted.
func Quantile(x []float64, q float64) float64 {
	if len(x) == 0 {
		return math.NaN()
	}
	c := append([]float64(nil), x...)
	sort.Float64s(c)
	i := int(q * float64(len(c)-1))
	return c[i]
}

func meanSD(x []float64) (float64, float64) {
	if len(x) < 2 {
		return 0, 0
	}
	var s float64
	for _, v := range x {
		s += v
	}
	m := s / float64(len(x))
	var ss float64
	for _, v := range x {
		ss += (v - m) * (v - m)
	}
	return m, math.Sqrt(ss / float64(len(x)-1))
}

func rupees(v float64) string {
	switch {
	case v >= 1e7:
		return fmt.Sprintf("Rs %g cr", v/1e7)
	case v >= 1e5:
		return fmt.Sprintf("Rs %g lakh", v/1e5)
	default:
		return fmt.Sprintf("Rs %gk", v/1e3)
	}
}
