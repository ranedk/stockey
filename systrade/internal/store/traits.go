package store

import (
	"context"
	"math"
	"sort"
	"time"
)

// Trait inputs that do not live in the bar cache: what a stock's trading
// looks like, rather than its price. Both tables are stockey-owned; we read.

// DatedValue is one symbol's value on one date.
type DatedValue struct {
	Date  time.Time
	Value float64
}

// TradeSizes returns each EQ-series symbol's average rupees per trade (total
// traded value ÷ number of trades) from the raw bhavcopy, ascending by date.
// Unadjusted on purpose: a split changes the price and the share count but
// not the rupees a typical trade is worth.
func (s *Store) TradeSizes(ctx context.Context, from, to time.Time) (map[string][]DatedValue, error) {
	return s.datedValues(ctx, `
		SELECT symbol, date, (total_value / number_of_trades)::float8 FROM nseindia_ohlcv
		WHERE series = 'EQ' AND date >= $1 AND date <= $2
		  AND number_of_trades > 0 AND total_value > 0
		ORDER BY symbol, date`, from, to)
}

// DeliveryPercents returns each EQ-series symbol's delivery percentage — the
// share of traded quantity that was actually delivered rather than squared
// off the same day. EQ only: the other series are settled by delivery by
// rule, or are debt, and would read as investor conviction when they are
// regulation.
func (s *Store) DeliveryPercents(ctx context.Context, from, to time.Time) (map[string][]DatedValue, error) {
	return s.datedValues(ctx, `
		SELECT symbol, date, deliverable_percent FROM nseindia_mto
		WHERE series = 'EQ' AND date >= $1 AND date <= $2
		  AND deliverable_percent IS NOT NULL
		ORDER BY symbol, date`, from, to)
}

func (s *Store) datedValues(ctx context.Context, q string, from, to time.Time, extra ...any) (map[string][]DatedValue, error) {
	rows, err := s.pool.Query(ctx, q, append([]any{from, to}, extra...)...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string][]DatedValue{}
	for rows.Next() {
		var sym string
		var d time.Time
		var v float64
		if err := rows.Scan(&sym, &d, &v); err != nil {
			return nil, err
		}
		u := d.UTC()
		out[sym] = append(out[sym], DatedValue{Date: time.Date(u.Year(), u.Month(), u.Day(), 0, 0, 0, 0, time.UTC), Value: v})
	}
	return out, rows.Err()
}

// MCapAt is the latest market cap reported on or before d — never one
// published afterwards — or NaN if none had been reported yet.
func MCapAt(pts []MCapPoint, d time.Time) float64 {
	i := sort.Search(len(pts), func(i int) bool { return pts[i].Date.After(d) })
	if i == 0 {
		return math.NaN()
	}
	return pts[i-1].MCap
}

// CircuitHits returns, per symbol, the days it hit its price band: +1 for the
// upper band, -1 for the lower. NSE's band-hit file lists hits only, so a day
// with no row is a day with no hit — zero, not missing.
//
// Stocks with futures are dropped from FuturesBandHitsUnreliableFrom on
// (found 2026-09-28): from August 2026 NSE's file lists them ~200-270 times a
// month, against a handful before, and the labels are inverted — 'H' rows
// closed down on 75-92% of days. 2013-2025 rows for the same names are right
// (H up-days 89-100%), so earlier research (rows 28, 29, 39) is unaffected.
// Membership is today's futures list — the rule only applies to recent dates.
func (s *Store) CircuitHits(ctx context.Context, from, to time.Time) (map[string][]DatedValue, error) {
	return s.datedValues(ctx, `
		SELECT symbol, date, CASE WHEN circuit_hit = 'H' THEN 1.0 ELSE -1.0 END
		FROM nseindia_circuit_hit
		WHERE circuit_hit IN ('H', 'L') AND date >= $1 AND date <= $2
		  AND NOT (date >= $3 AND symbol IN (
		        SELECT underlying_symbol FROM master_dhan_instruments
		         WHERE instrument = 'FUTSTK' AND valid_to IS NULL))
		ORDER BY symbol, date`, from, to, FuturesBandHitsUnreliableFrom)
}

// FuturesBandHitsUnreliableFrom is when NSE's band-hit file stopped meaning a
// band hit for stocks with futures (see CircuitHits).
var FuturesBandHitsUnreliableFrom = time.Date(2026, 8, 1, 0, 0, 0, 0, time.UTC)
