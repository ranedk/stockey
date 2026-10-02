package store

import (
	"context"
	"fmt"
	"time"

	"github.com/ranedk/systrader/internal/bars"
)

// Inputs to the fill-cost study (cmd/costs): what the first minutes of a day
// traded at, and the raw daily values behind the liquidity tiers.

// IntradayBars returns every NSE equity's 1-minute bars in [from, to), by
// ticker, ascending. The window must be under a day: dhan_ohlcv_intraday is a
// compressed hypertable read through postgres_fdw, and an unbounded scan of it
// is what once exhausted this machine's memory.
func (s *Store) IntradayBars(ctx context.Context, from, to time.Time) (map[string][]bars.Bar, error) {
	if !to.After(from) || to.Sub(from) > 24*time.Hour {
		return nil, fmt.Errorf("store: intraday window %s..%s must be positive and under a day", from, to)
	}
	rows, err := s.pool.Query(ctx, `
		SELECT ticker, timestamp, open, high, low, close, volume
		FROM dhan_ohlcv_intraday
		WHERE timestamp >= $1 AND timestamp < $2
		  AND interval_minutes = 1 AND exchange_segment = 'NSE_EQ'
		ORDER BY ticker, timestamp`, from, to)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string][]bars.Bar{}
	for rows.Next() {
		var t string
		var b bars.Bar
		if err := rows.Scan(&t, &b.Date, &b.Open, &b.High, &b.Low, &b.Close, &b.Vol); err != nil {
			return nil, err
		}
		b.Date = b.Date.UTC()
		out[t] = append(out[t], b)
	}
	return out, rows.Err()
}

// TradedValues returns each EQ symbol's raw daily traded value (rupees),
// ascending by date.
func (s *Store) TradedValues(ctx context.Context, from, to time.Time) (map[string][]DatedValue, error) {
	return s.datedValues(ctx, `
		SELECT symbol, date, total_value::float8 FROM nseindia_ohlcv
		WHERE series = 'EQ' AND date >= $1 AND date <= $2 AND total_value > 0
		ORDER BY symbol, date`, from, to)
}

// RawCloses returns each EQ symbol's raw (unadjusted) daily close.
func (s *Store) RawCloses(ctx context.Context, from, to time.Time) (map[string][]DatedValue, error) {
	return s.datedValues(ctx, `
		SELECT symbol, date, close::float8 FROM nseindia_ohlcv
		WHERE series = 'EQ' AND date >= $1 AND date <= $2 AND close > 0
		ORDER BY symbol, date`, from, to)
}

// RawRatios returns, for every EQ symbol trading on date, the factor that
// turns an adjusted price back into the price actually quoted that day:
// close / adj_close. Share counts are whole numbers of RAW shares, so a
// capital-aware book needs it on every day it trades.
func (s *Store) RawRatios(ctx context.Context, date time.Time) (map[string]float64, error) {
	d := time.Date(date.Year(), date.Month(), date.Day(), 0, 0, 0, 0, time.UTC)
	rows, err := s.pool.Query(ctx, `
		SELECT symbol, close / adj_close FROM advisory_adjusted_ohlcv_daily
		WHERE series = 'EQ' AND date >= $1 AND date < $2 AND adj_close > 0 AND close > 0`,
		d, d.AddDate(0, 0, 1))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]float64{}
	for rows.Next() {
		var sym string
		var r float64
		if err := rows.Scan(&sym, &r); err != nil {
			return nil, err
		}
		out[sym] = r
	}
	return out, rows.Err()
}

// RawClosesAnySeries returns each symbol's raw close on date in whichever equity series it
// traded (EQ preferred, else BE / BZ), with that series. The fallback price for a held name
// NSE has moved out of EQ: it no longer has an EQ bar, but it trades and can be sold (2026-10-02).
func (s *Store) RawClosesAnySeries(ctx context.Context, date time.Time) (map[string]float64, map[string]string, error) {
	d := time.Date(date.Year(), date.Month(), date.Day(), 0, 0, 0, 0, time.UTC)
	rows, err := s.pool.Query(ctx, `
		SELECT DISTINCT ON (symbol) symbol, close::float8, series FROM nseindia_ohlcv
		WHERE series IN ('EQ', 'BE', 'BZ') AND date >= $1 AND date < $2 AND close > 0
		ORDER BY symbol, (series = 'EQ') DESC`, d, d.AddDate(0, 0, 1))
	if err != nil {
		return nil, nil, err
	}
	defer rows.Close()
	closes, series := map[string]float64{}, map[string]string{}
	for rows.Next() {
		var sym, ser string
		var c float64
		if err := rows.Scan(&sym, &c, &ser); err != nil {
			return nil, nil, err
		}
		closes[sym], series[sym] = c, ser
	}
	return closes, series, rows.Err()
}
