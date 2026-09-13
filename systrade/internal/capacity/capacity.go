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

// Trade is one order in whole shares.
type Trade struct {
	Date                 time.Time
	Symbol               string
	Side                 string // BUY, SELL or EXIT, as paper.Order
	FromShares, ToShares float64
	Price                float64 // the raw price the shares are counted at
	Value                float64 // rupees traded
	Cost                 float64 // rupees: statutory + DP + modelled impact
	Impact               float64 // the modelled part of Cost
}

// Result is one policy's run.
type Result struct {
	Policy   Policy
	Dates    []time.Time
	Gross    []float64 // daily return on deployed rupees, before costs
	Net      []float64 // after costs
	Deployed []float64 // rupees in positions at the close
	Capital  []float64 // the capital the last rebalance sized off
	// Per day, as fractions of the rupees deployed after trading: what was
	// paid, and what was traded one way. Held counts the positions.
	DayCost     []float64
	DayTurnover []float64
	Held        []int
	Rebalanced  []bool

	Statutory, DP, Impact float64 // rupees paid over the run
	Traded                float64 // rupees traded, buys plus sells

	PositionDays, SmallPositionDays int // held names per day; those under 2 shares
	Targeted, RoundedToZero         int // names the spec wanted; those whole shares could not buy
	Rebalances                      []Rebalance
	Trades                          []Trade

	hold map[string]*holding // the book after the last day
	last map[string]paper.Obs
}

type holding struct {
	value      float64 // rupees, marked with adjusted returns
	shares     float64 // raw shares, as of the last rebalance
	entryDate  time.Time
	entryPrice float64 // adjusted open at the first fill, as the weight book records it
}

// Simulate walks days on the spec's rebalance clock. days must be sorted and
// start on a rebalance day, exactly as paper.Compute takes them. Specs with an
// exposure overlay or a stop are refused: none of the frozen tracks has one,
// and sizing them would need the overlay's own rupee logic.
func Simulate(spec paper.Spec, days []paper.Day, raw RawRatios, pol Policy) (*Result, error) {
	if !Supports(spec) {
		return nil, fmt.Errorf("capacity: %s has an overlay or stop; not supported", spec.Name)
	}
	if spec.RebalanceEvery < 1 || pol.Capital <= 0 {
		return nil, fmt.Errorf("capacity: need a rebalance clock and positive capital")
	}
	res := &Result{Policy: pol}
	hold := map[string]*holding{}
	capital := pol.Capital
	var prices map[string]paper.Obs

	for di, d := range days {
		prices = make(map[string]paper.Obs, len(d.Obs))
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
		tradedBefore := res.Traded
		rebal := di%spec.RebalanceEvery == 0
		if rebal {
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

		var rPre, rPost, c, turn float64
		if prevVal > 0 {
			rPre = openVal/prevVal - 1
		}
		if afterVal > 0 {
			rPost = closeVal/afterVal - 1
			c = cost / afterVal
			turn = (res.Traded - tradedBefore) / 2 / afterVal
		}
		res.Dates = append(res.Dates, d.Date)
		res.Gross = append(res.Gross, (1+rPre)*(1+rPost)-1)
		res.Net = append(res.Net, (1+rPre)*(1-c)*(1+rPost)-1)
		res.Deployed = append(res.Deployed, closeVal)
		res.Capital = append(res.Capital, capital)
		res.DayCost = append(res.DayCost, c)
		res.DayTurnover = append(res.DayTurnover, turn)
		res.Held = append(res.Held, len(hold))
		res.Rebalanced = append(res.Rebalanced, rebal)
	}
	res.hold, res.last = hold, prices
	return res, nil
}

// Supports says whether a spec can be sized in rupees here.
func Supports(spec paper.Spec) bool {
	return spec.Overlay == paper.OverlayNone && spec.Stop == paper.StopNone
}

// rebalance trades the book to the day's targets at the open and returns the
// rupees it cost, and the capital it sized off.
func rebalance(res *Result, hold map[string]*holding, spec paper.Spec, d paper.Day,
	prices map[string]paper.Obs, ratios map[string]float64, pol Policy) (float64, float64, error) {

	price := rawPrice(prices, ratios, func(o paper.Obs) float64 { return o.Open })
	vals := make(map[string]float64, len(hold))
	for s, h := range hold {
		vals[s] = h.value
	}
	trades, capital, targeted, zero, err := plan(d.Date, paper.TargetWeights(spec, d), vals, price, prices, pol)
	if err != nil {
		return 0, 0, err
	}
	res.Targeted += targeted
	res.RoundedToZero += zero

	var cost float64
	for _, t := range trades {
		cost += t.Cost
		res.Traded += t.Value
		res.Impact += t.Impact
		if t.Side == "BUY" {
			res.Statutory += t.Cost - t.Impact
		} else {
			res.DP += costs.DPCharge
			res.Statutory += t.Cost - t.Impact - costs.DPCharge
		}
		if t.ToShares == 0 {
			delete(hold, t.Symbol)
			continue
		}
		h := hold[t.Symbol]
		if h == nil {
			h = &holding{entryDate: d.Date, entryPrice: prices[t.Symbol].Open}
			hold[t.Symbol] = h
		}
		h.value = t.ToShares * t.Price
	}
	res.Trades = append(res.Trades, trades...)

	rb := Rebalance{Date: d.Date}
	for s, h := range hold {
		// Recount every position at today's raw price: a split changes the
		// number of shares without a trade.
		if p, ok := price(s); ok {
			h.shares = h.value / p
		}
		rb.Positions = append(rb.Positions, h.value)
	}
	sort.Float64s(rb.Positions)
	res.Rebalances = append(res.Rebalances, rb)
	return cost, capital, nil
}

func rawPrice(prices map[string]paper.Obs, ratios map[string]float64, field func(paper.Obs) float64) func(string) (float64, bool) {
	return func(sym string) (float64, bool) {
		o, ok := prices[sym]
		r, rok := ratios[sym]
		if !ok || !rok || field(o) <= 0 || r <= 0 {
			return 0, false
		}
		return field(o) * r, true
	}
}

// plan is the policy, and the only place it lives: which trades take a book
// holding vals (rupees) to its targets. Simulate executes them at the open;
// PlanOrders prints them for the next session.
func plan(date time.Time, weights, vals map[string]float64, price func(string) (float64, bool),
	obs map[string]paper.Obs, pol Policy) (trades []Trade, capital float64, targeted, zero int, err error) {

	capital = pol.Capital
	if pol.RefNames > 0 {
		capital *= float64(len(weights)) / pol.RefNames
	}
	target := map[string]float64{} // raw shares
	for sym, w := range weights {
		if w <= 0 {
			continue
		}
		targeted++
		p, ok := price(sym)
		if !ok {
			return nil, 0, 0, 0, fmt.Errorf("capacity: %s targeted on %s with no raw price", sym, date.Format("2006-01-02"))
		}
		rs := w * capital
		if rs < pol.MinPosition {
			rs = pol.MinPosition
		}
		n := math.Round(rs / p)
		if n == 0 {
			zero++
			continue
		}
		target[sym] = n
	}

	syms := make([]string, 0, len(vals)+len(target))
	for s := range vals {
		syms = append(syms, s)
	}
	for s := range target {
		if _, ok := vals[s]; !ok {
			syms = append(syms, s)
		}
	}
	sort.Strings(syms)

	for _, sym := range syms {
		v, held := vals[sym]
		tgt := target[sym]
		p, ok := price(sym)
		if !ok {
			// Held but not trading today — suspended or delisted. The weight
			// book drops such a name at its last value on the next rebalance;
			// so does this one, paying the statutory sale and DP charge on it.
			if tgt == 0 && held {
				trades = append(trades, Trade{Date: date, Symbol: sym, Side: "EXIT", Value: v, Cost: costs.Sell(v)})
			}
			continue
		}
		var cur float64
		if held {
			cur = v / p
		}
		if !((tgt == 0 && cur > 0) || (cur == 0 && tgt > 0) || math.Abs(cur-tgt) > pol.Inertia*tgt) {
			continue // Law 12: within 10% of target, no trade
		}
		o := obs[sym]
		dv := (tgt - cur) * p
		imp := costs.Impact(math.Abs(dv), o.Turnover, o.AnnVol/16)
		t := Trade{Date: date, Symbol: sym, FromShares: cur, ToShares: tgt, Price: p, Value: math.Abs(dv), Impact: imp}
		switch {
		case tgt == 0:
			t.Side, t.Cost = "EXIT", costs.Sell(-dv)+imp
		case dv > 0:
			t.Side, t.Cost = "BUY", costs.Buy(dv)+imp
		default:
			t.Side, t.Cost = "SELL", costs.Sell(-dv)+imp
		}
		trades = append(trades, t)
	}
	return trades, capital, targeted, zero, nil
}

// PlanOrders is the next session's orders for the book Simulate left behind,
// priced off latest's raw close, and the shares it holds going in. latest
// should be the last day simulated (or any day, for an empty run).
func (r *Result) PlanOrders(spec paper.Spec, latest paper.Day, ratios map[string]float64) ([]Trade, map[string]float64, error) {
	obs := make(map[string]paper.Obs, len(latest.Obs))
	for _, o := range latest.Obs {
		obs[o.Symbol] = o
	}
	price := rawPrice(obs, ratios, func(o paper.Obs) float64 { return o.Close })
	vals := map[string]float64{}
	held := map[string]float64{}
	for s, h := range r.hold {
		vals[s] = h.value
		if p, ok := price(s); ok {
			held[s] = h.value / p
		} else {
			held[s] = h.shares
		}
	}
	trades, _, _, _, err := plan(latest.Date, paper.TargetWeights(spec, latest), vals, price, obs, r.Policy)
	return trades, held, err
}

// Book renders the run as a paper.Book, so the account is stored and shown
// beside the weight books: NAV indexed to 100 after costs, cost and turnover
// as fractions of the rupees deployed, weights as each position's share of
// them, and every trade as an order counted in shares.
func (r *Result) Book(name string) *paper.Book {
	b := &paper.Book{Name: name, Holdings: map[string]float64{}, Shares: map[string]float64{}, Entries: map[string]paper.Entry{}}
	nav := 100.0
	capAt := map[time.Time]float64{}
	for i, d := range r.Dates {
		nav *= 1 + r.Net[i]
		capAt[d] = r.Capital[i]
		exposure := 0.0
		if r.Capital[i] > 0 {
			exposure = r.Deployed[i] / r.Capital[i]
		}
		b.NAV = append(b.NAV, paper.NavPoint{Date: d, NAV: nav, Return: r.Net[i],
			Turnover: r.DayTurnover[i], Cost: r.DayCost[i], Holdings: r.Held[i],
			Rebalanced: r.Rebalanced[i], Exposure: exposure})
	}
	var total float64
	for _, h := range r.hold {
		total += h.value
	}
	for s, h := range r.hold {
		if total > 0 {
			b.Holdings[s] = h.value / total
		}
		b.Shares[s] = math.Round(h.shares)
		b.Entries[s] = paper.Entry{Date: h.entryDate, Price: h.entryPrice, LastPrice: r.last[s].Close}
	}
	for _, t := range r.Trades {
		b.Orders = append(b.Orders, ToOrder(t, name, capAt[t.Date]))
	}
	return b
}

// ToOrder is a trade as a paper.Order: shares, rupees and cost filled in,
// weights as fractions of the capital sized off.
func ToOrder(t Trade, book string, capital float64) paper.Order {
	o := paper.Order{Date: t.Date, Book: book, Symbol: t.Symbol, Side: t.Side, FillPrice: t.Price,
		InShares: true, FromShares: t.FromShares, ToShares: t.ToShares, ValueRs: t.Value, CostRs: t.Cost}
	if capital > 0 {
		o.FromWeight = t.FromShares * t.Price / capital
		o.ToWeight = t.ToShares * t.Price / capital
		if t.Price == 0 {
			o.FromWeight = t.Value / capital
		}
	}
	return o
}

// AccountPolicy is the paper account every forward track keeps beside its
// weight books: Rs 1 crore, whole shares, Law 12 inertia, no minimum
// position — the configuration LEDGER row 36 found faithful for all five.
func AccountPolicy() Policy {
	return Policy{Capital: paper.AccountCapital, Inertia: 0.10}
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
