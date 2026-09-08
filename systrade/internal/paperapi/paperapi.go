// Package paperapi serves the forward paper-trading record to the screener
// frontend: the order sheet for the next session, the current book, and how
// the strategy is doing against its two benchmarks.
//
// Read-only over what cmd/paper wrote. Nothing here computes a strategy or
// places anything — the API cannot rebalance, only report.
package paperapi

import (
	"context"
	"fmt"
	"math"
	"sort"

	"github.com/ranedk/systrader/internal/paper"
	"github.com/ranedk/systrader/internal/store"
)

// Summary is one book's headline numbers.
type Summary struct {
	Book        string  `json:"book"`
	NAV         float64 `json:"nav"`
	TotalReturn float64 `json:"total_return"`
	Days        int     `json:"days"`
	Holdings    int     `json:"holdings"`
	MaxDrawdown float64 `json:"max_drawdown"`
	AnnCost     float64 `json:"ann_cost"`
}

// NavPoint is one date across all three books, shaped for charting.
type NavPoint struct {
	Date       string             `json:"date"`
	Values     map[string]float64 `json:"values"`
	Rebalanced bool               `json:"rebalanced"`
}

// Strategy is the full payload for one tracked strategy.
type Strategy struct {
	Name           string                  `json:"name"`
	IsForward      bool                    `json:"is_forward"`
	Start          string                  `json:"start"`
	Days           int                     `json:"days"`
	AsOf           string                  `json:"as_of"`
	Summaries      []Summary               `json:"summaries"`
	Nav            []NavPoint              `json:"nav"`
	Holdings       []store.PaperHoldingRow `json:"holdings"`
	HoldingsAsOf   string                  `json:"holdings_as_of"`
	Pending        store.PaperPending      `json:"pending"`
	LastOrders     []store.PaperOrderRow   `json:"last_orders"`
	LastOrdersDate string                  `json:"last_orders_date"`
	Spec           SpecView                `json:"spec"`
}

// SpecView is the frozen configuration, echoed so the page can state exactly
// what is being tracked without anyone having to open the repo.
type SpecView struct {
	Universe      string  `json:"universe"`
	Signal        string  `json:"signal"`
	Selection     string  `json:"selection"`
	Weighting     string  `json:"weighting"`
	Rebalance     string  `json:"rebalance"`
	Execution     string  `json:"execution"`
	Costs         string  `json:"costs"`
	MinTurnoverCr float64 `json:"min_turnover_cr"`
	Doc           string  `json:"doc"`
}

func specView(s paper.Spec) SpecView {
	return SpecView{
		Universe:      fmt.Sprintf("NSE cash equity, 60-bar median turnover at or above Rs %.0f crore", s.MinTurnover/1e7),
		Signal:        "EWMAC 32/128 — exponential moving-average crossover, volatility-normalised, long-only",
		Selection:     fmt.Sprintf("top %dth of the eligible universe by forecast", s.Quantile),
		Weighting:     "equal weight across the held names, fully invested, no leverage or shorting",
		Rebalance:     fmt.Sprintf("every %d trading days; hold in between", s.RebalanceEvery),
		Execution:     "decide at the close, fill at the next open",
		Costs:         fmt.Sprintf("%.0f bps round trip charged on turnover", s.CostBpsRoundTrip),
		MinTurnoverCr: s.MinTurnover / 1e7,
		Doc:           "docs/strategies/2026-09-08_trend_quintile.md",
	}
}

// List returns every tracked strategy, forward record first.
func List(ctx context.Context, st *store.Store) ([]Strategy, error) {
	names, err := st.PaperStrategies(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]Strategy, 0, len(names))
	for _, n := range names {
		s, err := Detail(ctx, st, n)
		if err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].IsForward != out[j].IsForward {
			return out[i].IsForward
		}
		return out[i].Name < out[j].Name
	})
	return out, nil
}

// Detail assembles one strategy's payload.
func Detail(ctx context.Context, st *store.Store, name string) (Strategy, error) {
	spec := paper.FrozenSpec()
	s := Strategy{
		Name:      name,
		IsForward: name == spec.Name,
		Start:     spec.Start.Format("2006-01-02"),
		Spec:      specView(spec),
	}

	navRows, err := st.PaperNav(ctx, name)
	if err != nil {
		return s, err
	}
	byDate := map[string]*NavPoint{}
	var order []string
	perBook := map[string][]store.PaperNavRow{}
	for _, r := range navRows {
		key := r.Date.Format("2006-01-02")
		p, ok := byDate[key]
		if !ok {
			p = &NavPoint{Date: key, Values: map[string]float64{}}
			byDate[key] = p
			order = append(order, key)
		}
		p.Values[r.Book] = r.NAV
		if r.Book == paper.BookStrategy {
			p.Rebalanced = r.Rebalanced
		}
		perBook[r.Book] = append(perBook[r.Book], r)
	}
	for _, k := range order {
		s.Nav = append(s.Nav, *byDate[k])
	}
	s.Days = len(order)
	if s.Days > 0 {
		s.AsOf = order[len(order)-1]
		s.Start = order[0]
	}
	for _, book := range []string{paper.BookStrategy, paper.BookEqual, paper.BookRandom} {
		rows := perBook[book]
		if len(rows) == 0 {
			continue
		}
		s.Summaries = append(s.Summaries, summarize(book, rows))
	}

	if s.Holdings, s.HoldingsAsOf, err = holdings(ctx, st, name); err != nil {
		return s, err
	}
	if s.Pending, err = st.PendingSheet(ctx, name); err != nil {
		return s, err
	}
	orders, orderDate, err := st.PaperOrders(ctx, name, paper.BookStrategy)
	if err != nil {
		return s, err
	}
	s.LastOrders = orders
	if !orderDate.IsZero() {
		s.LastOrdersDate = orderDate.Format("2006-01-02")
	}
	return s, nil
}

func holdings(ctx context.Context, st *store.Store, name string) ([]store.PaperHoldingRow, string, error) {
	rows, asOf, err := st.PaperHoldings(ctx, name, paper.BookStrategy)
	if err != nil {
		return nil, "", err
	}
	if asOf.IsZero() {
		return rows, "", nil
	}
	return rows, asOf.Format("2006-01-02"), nil
}

func summarize(book string, rows []store.PaperNavRow) Summary {
	s := Summary{Book: book, Days: len(rows)}
	peak, dd := 0.0, 0.0
	var cost float64
	for _, r := range rows {
		if r.NAV > peak {
			peak = r.NAV
		}
		if peak > 0 {
			if d := r.NAV/peak - 1; d < dd {
				dd = d
			}
		}
		cost += r.Cost
	}
	last := rows[len(rows)-1]
	s.NAV = last.NAV
	s.Holdings = last.Holdings
	s.TotalReturn = last.NAV/100 - 1
	s.MaxDrawdown = dd
	if len(rows) > 0 {
		s.AnnCost = cost / float64(len(rows)) * 252
	}
	if math.IsNaN(s.TotalReturn) {
		s.TotalReturn = 0
	}
	return s
}
