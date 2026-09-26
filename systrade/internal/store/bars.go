package store

import (
	"context"
	"fmt"
	"time"

	"github.com/ranedk/systrader/internal/bars"
)

// StreamAdjustedBars reads corporate-action-adjusted daily OHLCV for every
// EQ-series symbol in [from, to] and calls fn once per symbol, in symbol
// order, with that symbol's bars ascending by date.
//
// This is the ONLY function in the repo that reads the whole universe's
// price history, and it is meant to be called once per cache build, never
// inside a research loop. It is date-bounded on purpose: on stockey's side
// advisory_adjusted_ohlcv_daily is a VIEW over a compressed hypertable where
// an unbounded scan forces a full decompression (that is what caused this
// workspace's OOM incident). Locally it is a plain table, but the bound
// stays so the same call is safe against either shape.
//
// Rows arrive already grouped because of the ORDER BY, so only one symbol's
// bars are held in memory at a time.
func (s *Store) StreamAdjustedBars(ctx context.Context, from, to time.Time, fn func(bars.Series) error) error {
	rows, err := s.pool.Query(ctx, `
		SELECT symbol, date, adj_open, adj_high, adj_low, adj_close, adj_volume
		FROM advisory_adjusted_ohlcv_daily
		WHERE series = 'EQ'
		  AND date >= $1 AND date <= $2
		  AND adj_open > 0 AND adj_high > 0 AND adj_low > 0 AND adj_close > 0
		ORDER BY symbol ASC, date ASC`, from, to)
	if err != nil {
		return err
	}
	defer rows.Close()

	var cur bars.Series
	flush := func() error {
		if cur.Symbol == "" || len(cur.Bars) == 0 {
			return nil
		}
		return fn(cur)
	}

	for rows.Next() {
		var sym string
		var t time.Time
		var o, h, l, c float64
		var v *float64
		if err := rows.Scan(&sym, &t, &o, &h, &l, &c, &v); err != nil {
			return err
		}
		if sym != cur.Symbol {
			if err := flush(); err != nil {
				return err
			}
			cur = bars.Series{Symbol: sym}
		}
		vol := 0.0
		if v != nil {
			vol = *v
		}
		d := time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, time.UTC)
		b := bars.Bar{Date: d, Open: o, High: h, Low: l, Close: c, Vol: vol}
		// The source has a unique (symbol, date, series) key, but a duplicate
		// would silently create a two-bar pattern out of one day, so collapse
		// on last-wins rather than trusting it.
		if n := len(cur.Bars); n > 0 && cur.Bars[n-1].Date.Equal(d) {
			cur.Bars[n-1] = b
			continue
		}
		cur.Bars = append(cur.Bars, b)
	}
	if err := rows.Err(); err != nil {
		return err
	}
	if err := flush(); err != nil {
		return err
	}
	if cur.Symbol == "" {
		return fmt.Errorf("store: no adjusted bars in [%s, %s]",
			from.Format("2006-01-02"), to.Format("2006-01-02"))
	}
	return nil
}
