// Package paper keeps the forward record for a frozen strategy: the orders it
// would place, the book it would hold, and what it earned against its
// benchmarks — day by day, from the day it was frozen.
//
// This is the gate `docs/RESEARCH_PROTOCOL.md` and bible Law 4 both point at.
// Everything else in this repo scores a rule against history that has already
// happened, and history can be mined. A forward record cannot: every day it
// gains one observation nobody chose.
//
// The track is recomputed from the start date on every run rather than updated
// incrementally. That makes it idempotent (running twice changes nothing),
// self-healing (a day the job did not run fills itself in), and auditable (the
// whole record is a pure function of the spec, the price data and the start
// date). It costs a few seconds and removes an entire class of drift between
// what the database says and what the strategy actually did.
package paper

import (
	"fmt"
	"math"
	"sort"
	"time"
)

// Spec is the frozen configuration. It mirrors
// docs/strategies/2026-09-08_trend_quintile.md; changing any field here means
// a different strategy, a new spec document and a new clock.
type Spec struct {
	Name             string
	Start            time.Time
	MinTurnover      float64 // 60-bar median traded value, INR
	TurnoverWindow   int
	Quantile         int     // 5 = hold the top fifth by forecast
	RebalanceEvery   int     // trading days between rebalances
	CostBpsRoundTrip float64 // charged as half per side on turnover
	RandomSeed       int64   // for the random-ranking benchmark
}

// FrozenSpec is the configuration signed off on 2026-09-08. The Rs 10 crore
// floor is deliberate: the Rs 1 crore version backtests better and cannot
// absorb capital.
func FrozenSpec() Spec {
	return Spec{
		Name:             "trend-quintile",
		Start:            time.Date(2026, 9, 8, 0, 0, 0, 0, time.UTC),
		MinTurnover:      1e8,
		TurnoverWindow:   60,
		Quantile:         5,
		RebalanceEvery:   20,
		CostBpsRoundTrip: 50,
		RandomSeed:       20260908,
	}
}

// Book names. The strategy is tracked against both benchmarks from day one,
// because "it made 12%" means nothing without them.
const (
	BookStrategy = "strategy"
	BookEqual    = "equal-weight"
	BookRandom   = "random-ranking"
)

// Obs is one symbol on one date: what it was worth and what the rule thought.
type Obs struct {
	Symbol    string
	Open      float64
	Close     float64
	PrevClose float64
	Forecast  float64
	Turnover  float64
	Eligible  bool
}

// Day is one trading date's cross-section.
type Day struct {
	Date time.Time
	Obs  []Obs
}

// NavPoint is one book's record for one day.
type NavPoint struct {
	Date       time.Time
	NAV        float64
	Return     float64
	Turnover   float64 // one-way, as a fraction of the book
	Cost       float64 // charged that day, as a fraction of NAV
	Holdings   int
	Rebalanced bool
}

// Order is one intended trade at a rebalance. Weights are fractions of NAV;
// the fill price is the open of that day, which is what the backtest assumed
// and what a human placing this at the open would get.
type Order struct {
	Date       time.Time
	Book       string
	Symbol     string
	Side       string // BUY, SELL, EXIT
	FromWeight float64
	ToWeight   float64
	FillPrice  float64
}

// Book is one tracked portfolio.
type Book struct {
	Name     string
	NAV      []NavPoint
	Holdings map[string]float64 // weights as of the last computed day
	Orders   []Order
}

// Track is the whole forward record.
type Track struct {
	Spec      Spec
	Books     map[string]*Book
	Dates     []time.Time
	NextRebal time.Time // zero if the next rebalance date is not yet known
	LastRebal time.Time
}

// Compute walks the calendar and produces the record. days must be sorted
// ascending and contain only dates on or after spec.Start.
func Compute(spec Spec, days []Day) (*Track, error) {
	if spec.RebalanceEvery < 1 {
		return nil, fmt.Errorf("paper: RebalanceEvery must be at least 1")
	}
	if spec.Quantile < 2 {
		return nil, fmt.Errorf("paper: Quantile must be at least 2")
	}
	tr := &Track{Spec: spec, Books: map[string]*Book{}}
	for _, n := range []string{BookStrategy, BookEqual, BookRandom} {
		tr.Books[n] = &Book{Name: n, Holdings: map[string]float64{}}
	}
	navs := map[string]float64{BookStrategy: 100, BookEqual: 100, BookRandom: 100}

	for di, d := range days {
		tr.Dates = append(tr.Dates, d.Date)
		rebalance := di%spec.RebalanceEvery == 0
		if rebalance {
			tr.LastRebal = d.Date
		}
		prices := make(map[string]Obs, len(d.Obs))
		for _, o := range d.Obs {
			prices[o.Symbol] = o
		}

		for _, name := range []string{BookStrategy, BookEqual, BookRandom} {
			b := tr.Books[name]

			// Overnight: yesterday's book carried to today's open.
			rPre := weightedReturn(b.Holdings, prices, func(o Obs) float64 {
				if o.PrevClose <= 0 || o.Open <= 0 {
					return 0
				}
				return o.Open/o.PrevClose - 1
			})
			drifted := drift(b.Holdings, prices, func(o Obs) float64 {
				if o.PrevClose <= 0 || o.Open <= 0 {
					return 1
				}
				return o.Open / o.PrevClose
			})

			var turnover, cost float64
			target := drifted
			if rebalance {
				target = targetWeights(name, spec, d)
				turnover = turnoverBetween(drifted, target)
				cost = turnover * spec.CostBpsRoundTrip / 2 / 10000
				b.Orders = append(b.Orders, ordersBetween(d.Date, name, drifted, target, prices)...)
			}

			// The rest of the session, held at the new weights.
			rPost := weightedReturn(target, prices, func(o Obs) float64 {
				if o.Open <= 0 || o.Close <= 0 {
					return 0
				}
				return o.Close/o.Open - 1
			})
			end := drift(target, prices, func(o Obs) float64 {
				if o.Open <= 0 || o.Close <= 0 {
					return 1
				}
				return o.Close / o.Open
			})

			prev := navs[name]
			navs[name] = prev * (1 + rPre) * (1 - cost) * (1 + rPost)
			b.NAV = append(b.NAV, NavPoint{
				Date: d.Date, NAV: navs[name], Return: navs[name]/prev - 1,
				Turnover: turnover, Cost: cost, Holdings: len(end), Rebalanced: rebalance,
			})
			b.Holdings = end
		}
	}
	if len(days) > 0 {
		tr.NextRebal = nextRebalanceIndexDate(days, spec.RebalanceEvery)
	}
	return tr, nil
}

// targetWeights is where the three books differ, and the only place they do.
func targetWeights(book string, spec Spec, d Day) map[string]float64 {
	var eligible []Obs
	for _, o := range d.Obs {
		if o.Eligible {
			eligible = append(eligible, o)
		}
	}
	if len(eligible) < spec.Quantile {
		return map[string]float64{}
	}
	n := len(eligible) / spec.Quantile
	if n < 1 {
		n = 1
	}
	switch book {
	case BookEqual:
		// Own the whole eligible universe: the beta control.
		w := 1 / float64(len(eligible))
		out := make(map[string]float64, len(eligible))
		for _, o := range eligible {
			out[o.Symbol] = w
		}
		return out
	case BookRandom:
		// A book of the same size picked by a fixed per-symbol key: same
		// concentration, no information. The key is drawn once per symbol,
		// not once per day, so the book holds its positions like a real one.
		sort.Slice(eligible, func(i, j int) bool {
			return symKey(eligible[i].Symbol, spec.RandomSeed) < symKey(eligible[j].Symbol, spec.RandomSeed)
		})
	default:
		sort.Slice(eligible, func(i, j int) bool { return eligible[i].Forecast > eligible[j].Forecast })
	}
	out := make(map[string]float64, n)
	for _, o := range eligible[:n] {
		out[o.Symbol] = 1 / float64(n)
	}
	return out
}

// symKey is splitmix64 over (symbol, seed): a deterministic random ranking
// that never needs storing and never changes between runs.
func symKey(sym string, seed int64) uint64 {
	x := uint64(seed) * 0x9E3779B97F4A7C15
	for i := 0; i < len(sym); i++ {
		x ^= uint64(sym[i])
		x *= 0x100000001B3
	}
	x ^= x >> 30
	x *= 0xBF58476D1CE4E5B9
	x ^= x >> 27
	x *= 0x94D049BB133111EB
	x ^= x >> 31
	return x
}

func weightedReturn(w map[string]float64, prices map[string]Obs, ret func(Obs) float64) float64 {
	var out float64
	for sym, weight := range w {
		o, ok := prices[sym]
		if !ok {
			continue // did not trade today: the position is stale, not sold
		}
		r := ret(o)
		if math.IsNaN(r) {
			continue
		}
		out += weight * r
	}
	return out
}

// drift moves weights by a price factor and renormalizes, so a book that
// traded nothing still adds to 1 tomorrow.
func drift(w map[string]float64, prices map[string]Obs, factor func(Obs) float64) map[string]float64 {
	out := make(map[string]float64, len(w))
	var sum float64
	for sym, weight := range w {
		f := 1.0
		if o, ok := prices[sym]; ok {
			if v := factor(o); !math.IsNaN(v) && v > 0 {
				f = v
			}
		}
		out[sym] = weight * f
		sum += out[sym]
	}
	if sum > 0 {
		for sym := range out {
			out[sym] /= sum
		}
	}
	return out
}

func turnoverBetween(from, to map[string]float64) float64 {
	var t float64
	for sym, w := range to {
		t += math.Abs(w - from[sym])
	}
	for sym, w := range from {
		if _, held := to[sym]; !held {
			t += math.Abs(w)
		}
	}
	return t
}

func ordersBetween(date time.Time, book string, from, to map[string]float64, prices map[string]Obs) []Order {
	var out []Order
	seen := map[string]bool{}
	emit := func(sym string, a, b float64) {
		if math.Abs(b-a) < 1e-9 {
			return
		}
		side := "BUY"
		switch {
		case b == 0:
			side = "EXIT"
		case b < a:
			side = "SELL"
		}
		out = append(out, Order{Date: date, Book: book, Symbol: sym, Side: side,
			FromWeight: a, ToWeight: b, FillPrice: prices[sym].Open})
	}
	for sym, w := range to {
		seen[sym] = true
		emit(sym, from[sym], w)
	}
	for sym, w := range from {
		if !seen[sym] {
			emit(sym, w, 0)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Side != out[j].Side {
			return out[i].Side < out[j].Side
		}
		return out[i].Symbol < out[j].Symbol
	})
	return out
}

// nextRebalanceIndexDate reports the date of the next rebalance if it has
// already happened in the data, or the zero time when it is still in the
// future — the caller turns that into "in N trading days".
func nextRebalanceIndexDate(days []Day, every int) time.Time {
	last := len(days) - 1
	next := (last/every + 1) * every
	if next < len(days) {
		return days[next].Date
	}
	return time.Time{}
}

// DaysToNextRebalance counts trading days from the last computed day to the
// next scheduled rebalance.
func (t *Track) DaysToNextRebalance() int {
	if len(t.Dates) == 0 {
		return 0
	}
	last := len(t.Dates) - 1
	next := (last/t.Spec.RebalanceEvery + 1) * t.Spec.RebalanceEvery
	return next - last
}

// Pending computes the order sheet for the NEXT session: the trades that move
// `current` to the spec's target, priced off the most recent close available.
//
// It exists because the useful daily artifact is not the history, it is the
// list of what to place at tomorrow's open — and that list has to be
// computable on day one, when the forward record is still empty and Compute
// has nothing to walk.
//
// `due` says whether the schedule actually calls for a rebalance next session.
// The sheet is produced either way: seeing what the strategy WOULD buy on a
// hold day is how a human keeps an eye on it without touching it.
type PendingSheet struct {
	BasedOn   time.Time // the close these forecasts come from
	Due       bool      // is the next session a scheduled rebalance
	DaysToDue int
	Orders    []Order
	Target    map[string]float64
}

func Pending(spec Spec, latest Day, current map[string]float64, daysToDue int) PendingSheet {
	target := targetWeights(BookStrategy, spec, latest)
	prices := make(map[string]Obs, len(latest.Obs))
	for _, o := range latest.Obs {
		prices[o.Symbol] = o
	}
	return PendingSheet{
		BasedOn:   latest.Date,
		Due:       daysToDue <= 1,
		DaysToDue: daysToDue,
		Orders:    ordersBetween(latest.Date, BookStrategy, current, target, prices),
		Target:    target,
	}
}
